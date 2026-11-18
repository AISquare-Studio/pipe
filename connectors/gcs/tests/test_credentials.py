"""The credential blob: what loads, what is "not configured", ADC never used."""

from __future__ import annotations

import json

import google.auth
import pytest

from aisquare_pipe_gcs import GcsToolkit
from aisquare_pipe_gcs.credentials import (
    READ_ONLY_SCOPE,
    GcsNotConfigured,
    access_token,
    load_credentials,
)
from tests.fakes import FakeGoogle, StoredObject, blob, service_account


def test_a_complete_blob_loads_as_an_object_or_as_json_text():
    creds = load_credentials(blob())
    assert (creds.bucket, creds.prefix) == ("acme-policies", "policies/")
    assert creds.client_email == "reader@acme-proj.iam.gserviceaccount.com"
    as_text = load_credentials(blob(service_account_json=json.dumps(service_account())))
    assert as_text.client_email == creds.client_email
    assert load_credentials(blob(prefix=None)).prefix == ""
    assert "PRIVATE KEY" not in repr(creds)


def _without(key: str) -> dict:
    info = service_account()
    info.pop(key)
    return blob(service_account_json=info)


@pytest.mark.parametrize(
    ("config", "reason"),
    [
        (None, "no Google Cloud Storage credentials"),
        ({}, "service_account_json is missing"),
        (blob(service_account_json="{not json"), "not valid JSON"),
        (
            blob(service_account_json={**service_account(), "type": "authorized_user"}),
            "not a service account",
        ),
        (_without("private_key"), "no private_key"),
        (_without("client_email"), "no client_email"),
        (
            blob(service_account_json={**service_account(), "private_key": "abc"}),
            "not a PEM",
        ),
        (
            blob(
                service_account_json={
                    **service_account(),
                    "token_uri": "https://evil.example/token",
                }
            ),
            "token_uri is not Google's",
        ),
        (
            blob(
                service_account_json={
                    **service_account(),
                    "universe_domain": "example.com",
                }
            ),
            "not for googleapis.com",
        ),
        (blob(bucket=None), "bucket is missing"),
        (blob(bucket="Bad_Bucket!"), "not a bucket name"),
        (blob(prefix="/abs"), "prefix must be"),
    ],
)
def test_a_missing_or_malformed_blob_is_not_configured(config, reason):
    with pytest.raises(GcsNotConfigured, match=reason) as raised:
        load_credentials(config)
    assert "PRIVATE KEY" not in str(raised.value)
    assert not GcsToolkit().validate_config(config)  # type: ignore[arg-type]


def test_application_default_credentials_are_never_consulted(monkeypatch, tmp_path):
    """A decoy ADC file and project in the environment, and google.auth.default
    rigged to fail the test: without a blob the toolkit is "not configured"
    with no request; with one, the token is minted with the BLOB's key."""
    decoy = tmp_path / "adc.json"
    decoy.write_text(
        json.dumps(
            service_account(email="decoy@other.iam.gserviceaccount.com", label="decoy")
        )
    )
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", str(decoy))
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "decoy-project")

    def no_adc(*args, **kwargs):
        raise AssertionError("Application Default Credentials were consulted")

    monkeypatch.setattr(google.auth, "default", no_adc)
    google_ = FakeGoogle(objects={"policies/a.txt": StoredObject(b"hi", "text/plain")})

    for config in (None, {}, {"bucket": "acme-policies"}):
        result = GcsToolkit(session=google_).call_tool(
            "GCS_LIST_OBJECTS",
            {},
            config,  # type: ignore[arg-type]
        )
        assert not result.successful and result.error_code == "not_connected"
        assert "not configured" in (result.error or "")
    assert google_.requests == []

    result = GcsToolkit(session=google_).call_tool("GCS_LIST_OBJECTS", {}, blob())
    assert result.successful, result.error
    (claims,) = google_.token_requests
    assert claims["iss"] == "reader@acme-proj.iam.gserviceaccount.com"
    assert claims["scope"] == READ_ONLY_SCOPE


def test_tokens_are_minted_once_per_key_and_reused():
    google_ = FakeGoogle(objects={"policies/a.txt": StoredObject(b"hi", "text/plain")})
    for _ in range(3):
        assert (
            GcsToolkit(session=google_)
            .call_tool("GCS_LIST_OBJECTS", {}, blob())
            .successful
        )
    assert len(google_.token_requests) == 1
    other = blob(
        service_account_json=service_account(
            email="b@acme-proj.iam.gserviceaccount.com", label="b"
        )
    )
    assert (
        GcsToolkit(session=google_).call_tool("GCS_LIST_OBJECTS", {}, other).successful
    )
    assert [c["iss"] for c in google_.token_requests] == [
        "reader@acme-proj.iam.gserviceaccount.com",
        "b@acme-proj.iam.gserviceaccount.com",
    ]


def test_a_key_google_refuses_is_unauthorized_and_never_leaks_the_key():
    google_ = FakeGoogle()
    google_.token_error = (
        400,
        {"error": "invalid_grant", "error_description": "Invalid JWT Signature."},
    )
    result = GcsToolkit(session=google_).call_tool("GCS_LIST_OBJECTS", {}, blob())
    assert not result.successful and result.error_code == "unauthorized"
    assert "service account key" in (result.error or "")
    assert "PRIVATE KEY" not in (result.error or "")
    assert google_.storage_calls() == []


def test_access_token_needs_no_environment(monkeypatch):
    for name in (
        "GOOGLE_APPLICATION_CREDENTIALS",
        "GOOGLE_CLOUD_PROJECT",
        "GCLOUD_PROJECT",
    ):
        monkeypatch.delenv(name, raising=False)
    google_ = FakeGoogle()
    token = access_token(load_credentials(blob()), google_, timeout=5)
    assert token.startswith("ya29.fake-token-")
