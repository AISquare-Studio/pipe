"""The credential blob: what loads, what is "not configured", M2M tokens."""

from __future__ import annotations

import pytest

from aisquare_pipe_databricks import DatabricksToolkit
from aisquare_pipe_databricks.credentials import (
    DatabricksNotConfigured,
    load_credentials,
)
from tests.fakes import (
    CLIENT_SECRET,
    PAT,
    FakeDatabricks,
    StoredFile,
    m2m_blob,
    pat_blob,
)

FILE = "/Volumes/main/policies/docs/a.txt"


@pytest.mark.parametrize(
    "url",
    [
        "https://acme.cloud.databricks.com",
        "https://adb-1234567890123456.7.azuredatabricks.net/",
        "https://1234567890123456.7.gcp.databricks.com",
    ],
)
def test_every_databricks_cloud_host_loads(url):
    assert load_credentials(pat_blob(workspace_url=url)).host == url.split("//")[
        1
    ].rstrip("/")


@pytest.mark.parametrize(
    ("config", "reason"),
    [
        (None, "no Databricks credentials"),
        ({}, "workspace_url is missing"),
        (
            pat_blob(workspace_url="http://acme.cloud.databricks.com"),
            "workspace_url must be",
        ),
        (pat_blob(workspace_url="https://evil.example.com"), "workspace_url must be"),
        (
            pat_blob(workspace_url="https://acme.cloud.databricks.com.evil.example"),
            "workspace_url must be",
        ),
        (
            pat_blob(workspace_url="https://acme.cloud.databricks.com:8443"),
            "workspace_url must be",
        ),
        (
            pat_blob(workspace_url="https://u:p@acme.cloud.databricks.com"),
            "workspace_url must be",
        ),
        (
            pat_blob(workspace_url="https://acme.cloud.databricks.com/api"),
            "workspace_url must be",
        ),
        (pat_blob(auth="basic"), "auth must be"),
        (pat_blob(token=None), "token is missing"),
        (pat_blob(token="has space"), "whitespace"),
        (m2m_blob(client_secret=None), "client_secret is missing"),
        (m2m_blob(client_id=""), "client_id is missing"),
        (pat_blob(warehouse_id="../x"), "warehouse_id must be"),
    ],
)
def test_a_missing_or_malformed_blob_is_not_configured(config, reason):
    with pytest.raises(DatabricksNotConfigured, match=reason) as raised:
        load_credentials(config)
    assert PAT not in str(raised.value) and CLIENT_SECRET not in str(raised.value)
    assert not DatabricksToolkit().validate_config(config)  # type: ignore[arg-type]


def test_secrets_stay_out_of_reprs():
    for blob in (pat_blob(), m2m_blob()):
        text = repr(load_credentials(blob))
        assert PAT not in text and CLIENT_SECRET not in text


def test_ambient_databricks_configuration_is_never_read(monkeypatch, tmp_path):
    monkeypatch.setenv("DATABRICKS_HOST", "https://decoy.cloud.databricks.com")
    monkeypatch.setenv("DATABRICKS_TOKEN", "dapi-decoy")
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / ".databrickscfg").write_text(
        "[DEFAULT]\nhost = https://decoy\ntoken = dapi-decoy\n"
    )
    fake = FakeDatabricks()
    result = DatabricksToolkit(session=fake).call_tool(
        "DATABRICKS_READ_FILE", {"path": FILE}, {}
    )
    assert not result.successful and result.error_code == "not_connected"
    assert result.error.startswith("Databricks is not configured")
    assert fake.requests == []


def test_an_m2m_token_is_minted_once_and_reused():
    fake = FakeDatabricks()
    fake.volume[FILE] = StoredFile(b"hello", "text/plain")
    for _ in range(3):
        result = DatabricksToolkit(session=fake).call_tool(
            "DATABRICKS_READ_FILE", {"path": FILE}, m2m_blob()
        )
        assert result.successful, result.error
    assert fake.minted == [("6f1f1f1f-sp-app-id", "all-apis")]
    token_call = next(r for r in fake.requests if r["url"].endswith("/oidc/v1/token"))
    assert token_call["allow_redirects"] is False


def test_a_refused_service_principal_is_unauthorized_without_leaking_the_secret():
    fake = FakeDatabricks()
    fake.token_status = 401
    result = DatabricksToolkit(session=fake).call_tool(
        "DATABRICKS_READ_FILE", {"path": FILE}, m2m_blob()
    )
    assert not result.successful and result.error_code == "unauthorized"
    assert CLIENT_SECRET not in (result.error or "")
    assert fake.api_calls() == []
