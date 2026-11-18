"""The shared cross-account credential: the blob, AssumeRole and its refusals."""

from __future__ import annotations

from typing import Any

import boto3
import pytest
from botocore.exceptions import ClientError
from botocore.stub import Stubber

from aisquare_pipe_aws.credentials import (
    EXTERNAL_ID_MIN,
    REFUSED_ACCOUNTS_ENV,
    ROLE_SESSION_NAME,
    AwsAccessError,
    AwsNotConfiguredError,
    CrossAccountConfig,
    assume_role_session,
    classify_aws_error,
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


def test_the_external_id_is_not_in_the_repr():
    cfg = CrossAccountConfig.from_config(blob())
    assert EXTERNAL_ID not in repr(cfg) and ROLE_ARN in repr(cfg)


def test_an_external_id_of_32_characters_is_the_shortest_accepted():
    assert EXTERNAL_ID_MIN == 32
    assert is_configured(blob(external_id="a" * 32))
    assert not is_configured(blob(external_id="a" * 31))


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
        (blob(external_id="x"), "external_id must be 32-1224 characters"),
        (blob(external_id="a" * 31), "external_id must be 32-1224 characters"),
        (blob(external_id="has space" + "a" * 32), "external_id must be"),
        # STS can't assume across partitions: only commercial-partition roles.
        (
            blob(role_arn=f"arn:aws-cn:iam::{CUSTOMER_ACCOUNT}:role/AISquareReadOnly"),
            "commercial partition",
        ),
        (
            blob(
                role_arn=f"arn:aws-us-gov:iam::{CUSTOMER_ACCOUNT}:role/AISquareReadOnly"
            ),
            "commercial partition",
        ),
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

#: What STS says when our Pod Identity session's transitive tags reach a trust
#: policy without sts:TagSession.
AWS_TAG_SESSION_DENIED = (
    "User: arn:aws:sts::123456789012:assumed-role/pipe/eks-pipe is not authorized to "
    "perform: sts:TagSession on resource: arn:aws:iam::210987654321:role/AISquareReadOnly"
)


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


def test_a_role_in_an_account_the_host_refuses_is_refused_before_any_sts_call(
    aws, monkeypatch
):
    monkeypatch.setenv(REFUSED_ACCOUNTS_ENV, f"111111111111, {CUSTOMER_ACCOUNT}")

    def no_sts() -> Any:  # pragma: no cover - must not be reached
        raise AssertionError("no STS call for a refused account")

    with pytest.raises(AwsAccessError) as raised:
        assume_role_session(CrossAccountConfig.from_config(blob()), base_session=no_sts)
    assert raised.value.code == "forbidden"
    assert "set to refuse" in str(raised.value)


def test_other_refused_accounts_leave_the_customer_alone(aws, monkeypatch):
    monkeypatch.setenv(REFUSED_ACCOUNTS_ENV, "111111111111,222222222222")
    calls: list[dict[str, Any]] = []
    assume_role_session(
        CrossAccountConfig.from_config(blob()), base_session=_recording_base(calls)
    )
    assert len(calls) == 1


@pytest.mark.parametrize("value", ["12345", "111111111111,not-an-account"])
def test_a_malformed_refused_accounts_setting_fails_closed(aws, monkeypatch, value):
    monkeypatch.setenv(REFUSED_ACCOUNTS_ENV, value)
    calls: list[dict[str, Any]] = []
    with pytest.raises(AwsNotConfiguredError) as raised:
        assume_role_session(
            CrossAccountConfig.from_config(blob()), base_session=_recording_base(calls)
        )
    assert REFUSED_ACCOUNTS_ENV in str(raised.value)
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
    """A base session whose STS refuses the AssumeRole, as it does when the
    trust policy lacks sts:TagSession for our Pod Identity session's tags."""

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
            service_message=AWS_TAG_SESSION_DENIED,
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
    message = str(raised.value)
    # Every cause an operator should check, including the Pod Identity one.
    assert "does not trust" in message and "external ID" in message
    assert "trust policy does not allow sts:TagSession" in message
    assert "is not authorized" not in message  # AWS's own message is never echoed
    base.stub.assert_no_pending_responses()


@pytest.mark.parametrize(
    ("aws_code", "code"),
    [
        ("AccessDenied", "forbidden"),
        ("AccessDeniedException", "forbidden"),
        ("Forbidden", "forbidden"),
        ("403", "forbidden"),  # a body-less S3 HEAD refusal
        ("ValidationException", "invalid_arguments"),  # AgentCore
        ("InvalidArgument", "invalid_arguments"),  # S3
        ("400", "invalid_arguments"),  # a body-less S3 HEAD: a bad version id
        ("404", "not_found"),
        ("NoSuchKey", "not_found"),
        ("ResourceNotFoundException", "not_found"),
        ("SlowDown", "rate_limited"),
        ("ThrottlingException", "rate_limited"),
        ("ExpiredToken", "unauthorized"),
        ("InternalError", "upstream_error"),
        ("", "upstream_error"),
    ],
)
def test_aws_error_codes_map_to_toolkit_error_codes(aws_code, code):
    error = ClientError({"Error": {"Code": aws_code, "Message": "m"}}, "AnyOperation")
    assert classify_aws_error(error) == (code, aws_code)
