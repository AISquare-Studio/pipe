"""Hermetic AWS for every test: no socket opens, no real credential is read.

The service's own principal is a fake static key (``AMBIENT_KEY``); moto plays
STS and S3; AgentCore (which moto does not model) is a botocore ``Stubber``.
"""

from __future__ import annotations

import socket
from collections.abc import Callable, Iterator
from typing import Any

import boto3
import pytest
from moto import mock_aws

from aisquare_pipe_aws import connector, credentials, toolkit

#: The service's own (ambient) access key in tests. It must never sign a
#: customer call.
AMBIENT_KEY = "AKIAAMBIENTSERVICEKEY"
#: moto's default account: the service's own account in these tests.
OWN_ACCOUNT = "123456789012"
CUSTOMER_ACCOUNT = "210987654321"
ROLE_ARN = f"arn:aws:iam::{CUSTOMER_ACCOUNT}:role/AISquareReadOnly"
#: At least 32 characters, like every external ID AISquare issues.
EXTERNAL_ID = "aisq-ext-7f3c9b2e5d81a04c6e29b73f"


def blob(**overrides: Any) -> dict[str, Any]:
    value: dict[str, Any] = {
        "role_arn": ROLE_ARN,
        "external_id": EXTERNAL_ID,
        "regions": ["us-east-1"],
        "s3": {"bucket": "policy-docs", "prefix": "rulebooks/"},
    }
    value.update(overrides)
    return value


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*args: Any, **kwargs: Any) -> None:
        raise AssertionError(f"network access attempted: {args!r}")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)


@pytest.fixture(autouse=True)
def ambient_principal(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> Iterator[None]:
    for name in (
        credentials.REFUSED_ACCOUNTS_ENV,
        "AWS_PROFILE",
        "AWS_SESSION_TOKEN",
        "AWS_SECURITY_TOKEN",
        "AWS_ROLE_ARN",
        "AWS_WEB_IDENTITY_TOKEN_FILE",
        "AWS_CONTAINER_CREDENTIALS_FULL_URI",
        "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", AMBIENT_KEY)
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "ambient-service-secret")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")
    monkeypatch.setenv("AWS_CONFIG_FILE", str(tmp_path / "no-config"))
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", str(tmp_path / "no-credentials"))
    credentials.clear_session_cache()
    yield
    credentials.clear_session_cache()


@pytest.fixture
def aws() -> Iterator[None]:
    with mock_aws():
        yield


@pytest.fixture
def assume_calls(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Every sts:AssumeRole request the sources and toolkits make, as sent."""
    calls: list[dict[str, Any]] = []

    def base() -> Any:
        session = boto3.Session()

        def record(params: dict[str, Any], **_: Any) -> None:
            calls.append(dict(params))

        session.events.register("provide-client-params.sts.AssumeRole", record)
        return session

    monkeypatch.setattr(connector._AwsSource, "base_session", staticmethod(base))
    monkeypatch.setattr(toolkit._AwsToolkit, "base_session", staticmethod(base))
    return calls


def access_key_of(client: Any) -> str:
    """The access key a botocore client signs with."""
    return client._request_signer._credentials.get_frozen_credentials().access_key


ClientHook = Callable[[Any, str], Any]
