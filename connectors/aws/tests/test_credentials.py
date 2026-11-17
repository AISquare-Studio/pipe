"""The shared cross-account credential: the blob, AssumeRole and its refusals."""

from __future__ import annotations

from typing import Any

import boto3
import pytest
from botocore.stub import Stubber

from aisquare_pipe_aws.credentials import (
    ROLE_SESSION_NAME,
    AwsAccessError,
    AwsNotConfiguredError,
    CrossAccountConfig,
    assume_role_session,
    is_configured,
)
from tests.conftest import (
    AMBIENT_KEY,
    CUSTOMER_ACCOUNT,
    EXTERNAL_ID,
    OWN_ACCOUNT,
    ROLE_ARN,
    blob,
)

# -- the blob -------------------------------------------------------------------


def test_a_full_blob_parses():
    cfg = CrossAccountConfig.from_config(
        blob(regions=["us-east-1", "eu-west-1", "us-east-1"])
    )
    assert cfg.role_arn == ROLE_ARN
    assert cfg.account_id == CUSTOMER_ACCOUNT and cfg.partition == "aws"
    assert cfg.regions == ("us-east-1", "eu-west-1")  # de-duplicated, order kept
    assert cfg.s3 is not None
    assert (cfg.s3.bucket, cfg.s3.prefix, cfg.s3.region) == (
        "policy-docs",
        "rulebooks/",
        "us-east-1",
    )


def test_s3_is_optional():
    assert CrossAccountConfig.from_config(blob(s3=None)).s3 is None


@pytest.mark.parametrize(
    ("config", "problem"),
    [
        (None, "no credential blob"),
        ({}, "role_arn is missing"),
        (blob(role_arn="arn:aws:iam::123:role/x"), "role_arn is not an IAM role ARN"),
        (
            blob(role_arn=f"arn:aws:iam::{CUSTOMER_ACCOUNT}:user/someone"),
            "not an IAM role ARN",
        ),
        (blob(external_id=None), "external_id is missing"),
        (blob(external_id="x"), "external_id must be"),
        (blob(external_id="has space"), "external_id must be"),
        (blob(regions=[]), "regions must be a non-empty list"),
        (blob(regions="us-east-1"), "regions must be a non-empty list"),
        (blob(regions=["US-EAST-1"]), "region name"),
        (blob(s3={"prefix": "x/"}), "s3.bucket is not a bucket name"),
        (blob(s3={"bucket": "Bad_Bucket"}), "s3.bucket is not a bucket name"),
        (blob(s3={"bucket": "ok-bucket", "region": "nowhere"}), "s3.region"),
    ],
)
def test_a_bad_blob_is_not_configured_and_names_the_problem_not_the_value(
    config, problem
):
    with pytest.raises(AwsNotConfiguredError) as raised:
        CrossAccountConfig.from_config(config)
    message = str(raised.value)
    assert message.startswith(("AWS is not configured", "AWS is not configured:"))
    assert problem in message
    assert EXTERNAL_ID not in message
    assert not is_configured(config)


# -- AssumeRole -------------------------------------------------------------------


def _recording_base(calls: list[dict[str, Any]]) -> Any:
    def base() -> Any:
        session = boto3.Session()
        session.events.register(
            "provide-client-params.sts.AssumeRole",
            lambda params, **_: calls.append(dict(params)),
        )
        return session

    return base


def test_assume_role_sends_the_external_id_and_the_session_is_the_customer_role(aws):
    calls: list[dict[str, Any]] = []
    session = assume_role_session(
        CrossAccountConfig.from_config(blob()), base_session=_recording_base(calls)
    )
    assert len(calls) == 1
    assert calls[0]["RoleArn"] == ROLE_ARN
    assert calls[0]["ExternalId"] == EXTERNAL_ID
    assert calls[0]["RoleSessionName"] == ROLE_SESSION_NAME
    frozen = session.get_credentials().get_frozen_credentials()
    assert frozen.access_key != AMBIENT_KEY and frozen.access_key.startswith("ASIA")
    assert frozen.token  # temporary credentials
    # The session acts in the customer's account, not ours.
    identity = session.client("sts", region_name="us-east-1").get_caller_identity()
    assert identity["Account"] == CUSTOMER_ACCOUNT


def test_a_role_in_our_own_account_is_refused_before_any_assume_role(aws):
    calls: list[dict[str, Any]] = []
    own = blob(role_arn=f"arn:aws:iam::{OWN_ACCOUNT}:role/Anything")
    with pytest.raises(AwsAccessError) as raised:
        assume_role_session(
            CrossAccountConfig.from_config(own), base_session=_recording_base(calls)
        )
    assert raised.value.code == "forbidden"
    assert "own AWS account" in str(raised.value)
    assert calls == []


class _NoPrincipal:
    def get_credentials(self) -> None:
        return None

    def client(
        self, *args: Any, **kwargs: Any
    ) -> Any:  # pragma: no cover - must not be reached
        raise AssertionError("no client may be built without a principal")


def test_no_service_principal_fails_closed():
    with pytest.raises(AwsNotConfiguredError) as raised:
        assume_role_session(
            CrossAccountConfig.from_config(blob()), base_session=_NoPrincipal
        )
    assert "no service principal" in str(raised.value)


class _StubbedSts:
    """A base session whose STS refuses the AssumeRole (no trust / wrong external id)."""

    def __init__(self) -> None:
        self.session = boto3.Session()
        self.sts = self.session.client("sts", region_name="us-east-1")
        self.stub = Stubber(self.sts)
        self.stub.add_response(
            "get_caller_identity",
            {
                "Account": OWN_ACCOUNT,
                "Arn": "arn:aws:iam::123456789012:role/pipe",
                "UserId": "AROA:pipe",
            },
        )
        self.stub.add_client_error(
            "assume_role",
            service_error_code="AccessDenied",
            http_status_code=403,
            expected_params={
                "RoleArn": ROLE_ARN,
                "RoleSessionName": ROLE_SESSION_NAME,
                "ExternalId": EXTERNAL_ID,
                "DurationSeconds": 3600,
            },
        )
        self.stub.activate()

    def get_credentials(self) -> Any:
        return self.session.get_credentials()

    def client(self, service: str, **kwargs: Any) -> Any:
        assert service == "sts"
        return self.sts


def test_a_refused_assume_role_is_forbidden_and_never_falls_back_to_our_credentials():
    base = _StubbedSts()
    with pytest.raises(AwsAccessError) as raised:
        assume_role_session(
            CrossAccountConfig.from_config(blob()), base_session=lambda: base
        )
    assert raised.value.code == "forbidden"
    assert "does not trust" in str(raised.value) and "external ID" in str(raised.value)
    base.stub.assert_no_pending_responses()
