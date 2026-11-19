"""The credential blob: the shared AWS part (parsed by #31's helper) and the redshift section."""

from __future__ import annotations

from typing import Any

import pytest

from aisquare_pipe_aws import credentials as aws_credentials
from aisquare_pipe_aws.credentials import AwsNotConfiguredError, CrossAccountConfig
from aisquare_pipe_aws.redshift.config import (
    SERVER_KEYS,
    RedshiftConfig,
    RedshiftNotConfiguredError,
    is_configured,
)
from tests.conftest import CUSTOMER_ACCOUNT, EXTERNAL_ID, ROLE_ARN
from tests.redshift_fakes import SECRET_ARN, blob


def test_a_cluster_on_the_roles_iam_identity():
    cfg = RedshiftConfig.from_config(blob())
    assert cfg.aws.role_arn == ROLE_ARN
    assert cfg.target.auth_mode == "iam_cluster"
    assert cfg.target.region == "us-east-1"
    assert cfg.target.connection() == {
        "ClusterIdentifier": "analytics",
        "Database": "policies",
    }


def test_a_cluster_with_a_database_user():
    cfg = RedshiftConfig.from_config(blob(db_user="aisquare_reader"))
    assert cfg.target.auth_mode == "db_user"
    assert cfg.target.connection() == {
        "ClusterIdentifier": "analytics",
        "Database": "policies",
        "DbUser": "aisquare_reader",
    }


def test_a_workgroup_on_the_roles_iam_identity():
    cfg = RedshiftConfig.from_config(
        blob(cluster_identifier=None, workgroup_name="policies-wg", region="eu-west-1")
    )
    assert cfg.target.auth_mode == "iam_serverless"
    assert cfg.target.region == "eu-west-1"
    assert cfg.target.connection() == {
        "WorkgroupName": "policies-wg",
        "Database": "policies",
    }


def test_a_secret_for_a_cluster_or_a_workgroup():
    for section in ({}, {"cluster_identifier": None, "workgroup_name": "policies-wg"}):
        cfg = RedshiftConfig.from_config(blob(secret_arn=SECRET_ARN, **section))
        assert cfg.target.auth_mode == "secret"
        assert cfg.target.connection()["SecretArn"] == SECRET_ARN
        assert "DbUser" not in cfg.target.connection()


def test_the_aws_part_is_parsed_by_the_shared_helper(monkeypatch):
    seen: list[Any] = []
    real = CrossAccountConfig.from_config.__func__  # type: ignore[attr-defined]

    def spy(cls: type, config: Any) -> CrossAccountConfig:
        seen.append(config)
        return real(cls, config)

    monkeypatch.setattr(
        aws_credentials.CrossAccountConfig, "from_config", classmethod(spy)
    )
    cfg = RedshiftConfig.from_config(blob())
    assert len(seen) == 1 and cfg.aws.account_id == CUSTOMER_ACCOUNT


def _without(key: str) -> dict[str, Any]:
    value = blob()
    value.pop(key)
    return value


MALFORMED: dict[str, Any] = {
    "no-blob": None,
    "empty-blob": {},
    "no-role": _without("role_arn"),
    "no-external-id": _without("external_id"),
    "short-external-id": {**blob(), "external_id": "too-short"},
    "no-regions": _without("regions"),
    "no-redshift-section": _without("redshift"),
    "section-not-an-object": {**blob(), "redshift": "analytics"},
    "neither-cluster-nor-workgroup": blob(cluster_identifier=None),
    "both-cluster-and-workgroup": blob(workgroup_name="policies-wg"),
    "bad-cluster-identifier": blob(cluster_identifier="Analytics_1"),
    "long-cluster-identifier": blob(cluster_identifier="a" * 64),
    "workgroup-arn": blob(
        cluster_identifier=None,
        workgroup_name=f"arn:aws:redshift-serverless:us-east-1:{CUSTOMER_ACCOUNT}:workgroup/x",
    ),
    "no-database": blob(database=None),
    "bad-database": blob(database="policies; drop"),
    "empty-database": blob(database=""),
    "db-user-and-secret": blob(db_user="reader", secret_arn=SECRET_ARN),
    "db-user-with-a-workgroup": blob(
        cluster_identifier=None, workgroup_name="policies-wg", db_user="reader"
    ),
    "bad-db-user": blob(db_user="reader name"),
    "secret-not-an-arn": blob(secret_arn="redshift/aisquare-reader"),
    "secret-in-another-account": blob(
        secret_arn="arn:aws:secretsmanager:us-east-1:111122223333:secret:redshift/x-AbCdEf"
    ),
    "secret-in-another-partition": blob(
        secret_arn=f"arn:aws-cn:secretsmanager:cn-north-1:{CUSTOMER_ACCOUNT}:secret:x-AbCdEf"
    ),
    "bad-region": blob(region="mars-1"),
    "unknown-section-key": blob(host="analytics.example.com"),
    "non-string-value": blob(database=42),
}


@pytest.mark.parametrize("config", list(MALFORMED.values()), ids=list(MALFORMED))
def test_a_malformed_blob_is_not_configured(config):
    assert not is_configured(config)
    with pytest.raises(AwsNotConfiguredError) as raised:
        RedshiftConfig.from_config(config)
    message = str(raised.value)
    assert "not configured" in message
    for secret in (
        EXTERNAL_ID,
        "policies; drop",
        "analytics.example.com",
        "111122223333",
    ):
        assert secret not in message


def test_redshift_problems_are_their_own_error():
    with pytest.raises(RedshiftNotConfiguredError, match="exactly one"):
        RedshiftConfig.from_config(blob(cluster_identifier=None))


def test_the_server_keys_are_the_blobs():
    assert {"role_arn", "external_id", "regions", "redshift"} <= SERVER_KEYS
    assert {
        "cluster_identifier",
        "workgroup_name",
        "database",
        "db_user",
        "secret_arn",
        "region",
    } <= SERVER_KEYS
