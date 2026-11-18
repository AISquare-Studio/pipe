"""The credential blob: what loads, what is "not configured", ADC never used."""

from __future__ import annotations

import json
import time

import google.auth
import google.auth.exceptions
import pytest
import requests
from aisquare.pipe.core.envelope import PullParams

from aisquare_pipe_gcs import GcsAPIError, GcsSource, GcsToolkit
from aisquare_pipe_gcs.credentials import (
    READ_ONLY_SCOPE,
    GcsNotConfigured,
    access_token,
    load_credentials,
)
from tests.fakes import (
    FakeGoogle,
    StoredObject,
    blob,
    ec_private_key_pem,
    service_account,
)

_BAD_KEY_HINT = "Google rejected the service account key"


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
        (blob(prefix="policies"), "prefix must end with '/'"),
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

    consulted: list[str] = []

    def no_adc(*args, **kwargs):
        # Recorded, not only raised: call_tool turns any exception into a
        # failed result, which would hide a consultation that raised.
        consulted.append("google.auth.default")
        raise google.auth.exceptions.DefaultCredentialsError(
            "ADC must not be consulted"
        )

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
    assert consulted == []


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


def test_a_prefix_without_a_trailing_slash_is_refused_not_widened():
    """'policies' would admit 'policies-hr/...' with a plain startswith: the
    blob is "not configured" and nothing is requested."""
    google_ = FakeGoogle(
        objects={"policies-hr/pay.csv": StoredObject(b"secret", "text/csv")}
    )
    result = GcsToolkit(session=google_).call_tool(
        "GCS_GET_OBJECT", {"name": "policies-hr/pay.csv"}, blob(prefix="policies")
    )
    assert not result.successful and result.error_code == "not_connected"
    assert "must end with '/'" in (result.error or "")
    assert google_.requests == []
    assert load_credentials(blob(prefix="policies/")).contains("policies/a.pdf")
    assert not load_credentials(blob(prefix="policies/")).contains("policies-hr/x")


def _list(google_: FakeGoogle):
    return GcsToolkit(session=google_).call_tool("GCS_LIST_OBJECTS", {}, blob())


def _pull(google_: FakeGoogle) -> list:
    return list(GcsSource(session=google_).pull(blob(), PullParams(params={})))


@pytest.mark.parametrize(
    "error",
    [
        requests.ConnectTimeout("connect timed out"),
        requests.ReadTimeout("read timed out"),
    ],
)
def test_a_token_endpoint_timeout_is_a_timeout_not_a_bad_key(error):
    google_ = FakeGoogle()
    google_.token_raise = error
    result = _list(google_)
    assert not result.successful and result.error_code == "timeout", result.error
    assert _BAD_KEY_HINT not in (result.error or "")
    assert len(google_.token_requests) == 1 and google_.storage_calls() == []
    with pytest.raises(GcsAPIError) as raised:
        _pull(google_)
    assert raised.value.timed_out and raised.value.status != 401


def test_an_unreachable_token_endpoint_is_an_upstream_error_not_a_bad_key():
    google_ = FakeGoogle()
    google_.token_raise = requests.ConnectionError("Name or service not known")
    result = _list(google_)
    assert not result.successful and result.error_code == "upstream_error"
    assert "could not reach Google's token endpoint (ConnectionError)" in result.error
    assert _BAD_KEY_HINT not in result.error
    with pytest.raises(GcsAPIError) as raised:
        _pull(google_)
    assert raised.value.status is None and not raised.value.key_refused


@pytest.mark.parametrize(
    ("status", "body"),
    [
        (503, {"error": "backendError"}),
        (500, {"error": "internal_failure"}),
        (502, "<html>Bad Gateway</html>"),
        (400, {"error": "temporarily_unavailable"}),
    ],
)
def test_a_token_endpoint_outage_is_one_post_and_an_upstream_error(
    monkeypatch, status, body
):
    """Not a bad key, and no hidden retries or backoff sleeps outside the
    host's time budget: one POST, then the host decides (upstream_error is
    retryable)."""
    sleeps: list[float] = []
    monkeypatch.setattr(time, "sleep", sleeps.append)
    google_ = FakeGoogle()
    google_.token_error = (status, body, {"Retry-After": "9"})
    result = _list(google_)
    assert not result.successful and result.error_code == "upstream_error", result.error
    assert _BAD_KEY_HINT not in (result.error or "")
    assert len(google_.token_requests) == 1 and sleeps == []
    assert result.retry_after == (9.0 if status == 503 else None)


def test_a_rate_limited_token_endpoint_is_rate_limited():
    google_ = FakeGoogle()
    google_.token_error = (429, {"error": "rate_limit_exceeded"}, {"Retry-After": "3"})
    result = _list(google_)
    assert not result.successful and result.error_code == "rate_limited"
    assert result.retry_after == 3.0 and _BAD_KEY_HINT not in (result.error or "")


@pytest.mark.parametrize(
    ("status", "body"),
    [
        (
            400,
            {"error": "invalid_grant", "error_description": "Invalid JWT Signature."},
        ),
        (401, {"error": "unauthorized_client"}),
        (403, {"error": "access_denied", "error_description": "account disabled"}),
    ],
)
def test_only_a_refused_key_is_unauthorized(status, body):
    google_ = FakeGoogle()
    google_.token_error = (status, body)
    result = _list(google_)
    assert not result.successful and result.error_code == "unauthorized"
    assert _BAD_KEY_HINT in (result.error or "")
    assert body["error"] in (result.error or "")
    with pytest.raises(GcsAPIError) as raised:
        _pull(google_)
    assert raised.value.status == 401 and raised.value.key_refused


def test_an_ec_key_is_not_configured_and_nothing_escapes():
    """PEM-shaped, so the blob loads; signing is where it fails, and that
    is the tenant's blob to fix (not_connected), with no request made."""
    config = blob(
        service_account_json={**service_account(), "private_key": ec_private_key_pem()}
    )
    google_ = FakeGoogle()
    result = GcsToolkit(session=google_).call_tool("GCS_LIST_OBJECTS", {}, config)
    assert not result.successful and result.error_code == "not_connected", result.error
    assert "RSA service account key" in (result.error or "")
    assert "PRIVATE KEY" not in (result.error or "")
    with pytest.raises(GcsNotConfigured, match="can't be loaded or used to sign"):
        list(GcsSource(session=google_).pull(config, PullParams(params={})))
    assert google_.requests == []


def test_a_token_storage_rejects_is_dropped_and_reminted_once():
    """The key's account was disabled and re-enabled (or the token revoked):
    the cached token gets a 401, so it is dropped and one new token is
    minted. Before, it was reused (failing) until it expired."""
    google_ = FakeGoogle(objects={"policies/a.txt": StoredObject(b"hi", "text/plain")})
    assert _list(google_).successful
    google_.revoke_tokens()
    result = _list(google_)
    assert result.successful, result.error
    assert len(google_.token_requests) == 2
    sent = [c["headers"]["Authorization"] for c in google_.storage_calls()]
    # token 1 ok; token 1 rejected; token 2 ok
    assert sent == [f"Bearer ya29.fake-token-{n}" for n in (1, 1, 2)]
    assert _list(google_).successful and len(google_.token_requests) == 2


def test_a_new_token_storage_rejects_is_final_and_not_kept():
    """A just-minted token that gets a 401 is not retried (a new one would
    fail the same way) but is dropped: the next call mints again."""
    google_ = FakeGoogle(objects={"policies/a.txt": StoredObject(b"hi", "text/plain")})
    google_.reject_all_tokens = True
    result = _list(google_)
    assert not result.successful and result.error_code == "unauthorized"
    assert len(google_.token_requests) == 1 and len(google_.storage_calls()) == 1
    google_.reject_all_tokens = False
    assert _list(google_).successful
    assert len(google_.token_requests) == 2


def test_a_cached_token_rejected_after_the_key_was_deleted_is_unauthorized():
    google_ = FakeGoogle(objects={"policies/a.txt": StoredObject(b"hi", "text/plain")})
    assert _list(google_).successful
    google_.revoke_tokens()
    google_.token_error = (
        400,
        {"error": "invalid_grant", "error_description": "deleted"},
    )
    result = _list(google_)
    assert not result.successful and result.error_code == "unauthorized"
    assert "invalid_grant" in (result.error or "")
    google_.token_error = None  # the customer fixed it: the next call recovers
    assert _list(google_).successful
