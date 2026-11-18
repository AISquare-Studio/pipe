"""The credential blob: what loads, what is "not configured", ADC never used,
and how the token exchange fails."""

from __future__ import annotations

import json
import time

import google.auth
import google.auth.exceptions
import pytest
import requests
from aisquare.pipe.core.envelope import PullParams
from aisquare.pipe.errors import PipelineError
from aisquare.pipe.testing.host import host_answer

from aisquare_pipe_bigquery import (
    BigQueryNotConfigured,
    BigQueryQuerySource,
    BigQueryToolkit,
)
from aisquare_pipe_bigquery.credentials import (
    DEFAULT_MAX_BYTES_BILLED,
    READ_ONLY_SCOPE,
    access_token,
    load_credentials,
)
from tests.fakes import (
    PROJECT,
    FakeGoogle,
    FakeTable,
    blob,
    ec_private_key_pem,
    field_,
    service_account,
)

_BAD_KEY_HINT = "Google rejected the service account key"


def _google() -> FakeGoogle:
    google = FakeGoogle()
    google.add_dataset(f"{PROJECT}.policies")
    return google


def test_a_complete_blob_loads_as_an_object_or_as_json_text():
    creds = load_credentials(blob())
    assert (creds.project_id, creds.location) == (PROJECT, "US")
    assert creds.max_bytes_billed == DEFAULT_MAX_BYTES_BILLED
    assert (
        creds.client_email == "aisquare-reader@acme-analytics.iam.gserviceaccount.com"
    )
    as_text = load_credentials(blob(service_account_json=json.dumps(service_account())))
    assert as_text.client_email == creds.client_email
    assert load_credentials(blob(location=None)).location is None
    assert load_credentials(blob(max_bytes_billed=50 * 1024**3)).max_bytes_billed == (
        50 * 1024**3
    )
    assert load_credentials(blob(project_id="example.com:acme-proj")).project_id
    assert "PRIVATE KEY" not in repr(creds)


def _without(key: str) -> dict:
    info = service_account()
    info.pop(key)
    return blob(service_account_json=info)


@pytest.mark.parametrize(
    ("config", "reason"),
    [
        (None, "no BigQuery credentials"),
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
        ({"service_account_json": service_account()}, "project_id is missing"),
        (blob(project_id="Not A Project!"), "not a Google Cloud project id"),
        (blob(location="us central"), "location must be"),
        (blob(max_bytes_billed=0), "max_bytes_billed must be"),
        (blob(max_bytes_billed="10GB"), "max_bytes_billed must be"),
        (blob(max_bytes_billed=2 * 1024**4), "max_bytes_billed must be"),
    ],
)
def test_a_missing_or_malformed_blob_is_not_configured(config, reason):
    with pytest.raises(BigQueryNotConfigured, match=reason) as raised:
        load_credentials(config)
    assert "PRIVATE KEY" not in str(raised.value)
    assert not BigQueryToolkit().validate_config(config)  # type: ignore[arg-type]
    assert not BigQueryQuerySource().validate_config(config)  # type: ignore[arg-type]


def test_application_default_credentials_are_never_consulted(monkeypatch, tmp_path):
    """A decoy ADC key file and project in the environment, and
    google.auth.default rigged to record any call: without a blob every tool
    and the source are "not configured" with no request; with one, the
    token is minted with the BLOB's key, and google.auth.default was never
    called."""
    decoy = tmp_path / "adc.json"
    decoy.write_text(
        json.dumps(
            service_account(email="decoy@other.iam.gserviceaccount.com", label="decoy")
        )
    )
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", str(decoy))
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "decoy-project")
    monkeypatch.setenv("CLOUDSDK_CORE_PROJECT", "decoy-project")

    consulted: list[str] = []

    def no_adc(*args, **kwargs):
        # Recorded, not only raised: call_tool turns any exception into a
        # failed result, which would hide a consultation that raised.
        consulted.append("google.auth.default")
        raise google.auth.exceptions.DefaultCredentialsError(
            "ADC must not be consulted"
        )

    monkeypatch.setattr(google.auth, "default", no_adc)
    google_ = _google()

    for config in (None, {}, {"project_id": PROJECT}, {"location": "US"}):
        for slug, arguments in (
            ("BIGQUERY_LIST_DATASETS", {}),
            ("BIGQUERY_RUN_SELECT", {"sql": "SELECT 1"}),
        ):
            result = BigQueryToolkit(session=google_).call_tool(
                slug,
                arguments,
                config,  # type: ignore[arg-type]
            )
            assert not result.successful and result.error_code == "not_connected"
            assert "not configured" in (result.error or "")
        with pytest.raises(BigQueryNotConfigured):
            list(
                BigQueryQuerySource(session=google_).pull(
                    config,  # type: ignore[arg-type]
                    PullParams({"table": "policies.documents"}),
                )
            )
    assert google_.requests == []

    result = BigQueryToolkit(session=google_).call_tool(
        "BIGQUERY_LIST_DATASETS", {}, blob()
    )
    assert result.successful, result.error
    (claims,) = google_.token_requests
    assert claims["iss"] == "aisquare-reader@acme-analytics.iam.gserviceaccount.com"
    assert claims["scope"] == READ_ONLY_SCOPE
    (call,) = google_.api_calls()
    assert f"/projects/{PROJECT}/datasets" in call["url"]
    assert "decoy" not in call["url"]
    assert consulted == []


def test_the_token_is_read_only():
    assert READ_ONLY_SCOPE == "https://www.googleapis.com/auth/bigquery.readonly"


def test_tokens_are_minted_once_per_key_and_reused():
    google_ = _google()
    for _ in range(3):
        assert (
            BigQueryToolkit(session=google_)
            .call_tool("BIGQUERY_LIST_DATASETS", {}, blob())
            .successful
        )
    assert len(google_.token_requests) == 1
    other = blob(
        service_account_json=service_account(
            email="b@acme-analytics.iam.gserviceaccount.com", label="b"
        )
    )
    assert (
        BigQueryToolkit(session=google_)
        .call_tool("BIGQUERY_LIST_DATASETS", {}, other)
        .successful
    )
    assert [c["iss"] for c in google_.token_requests] == [
        "aisquare-reader@acme-analytics.iam.gserviceaccount.com",
        "b@acme-analytics.iam.gserviceaccount.com",
    ]


def test_access_token_needs_no_environment(monkeypatch):
    for name in (
        "GOOGLE_APPLICATION_CREDENTIALS",
        "GOOGLE_CLOUD_PROJECT",
        "GCLOUD_PROJECT",
    ):
        monkeypatch.delenv(name, raising=False)
    token = access_token(load_credentials(blob()), FakeGoogle(), timeout=5)
    assert token.startswith("ya29.fake-token-")


def _list(google_: FakeGoogle):
    return BigQueryToolkit(session=google_).call_tool(
        "BIGQUERY_LIST_DATASETS", {}, blob()
    )


def _pull(google_: FakeGoogle) -> list:
    google_.add_table(
        f"{PROJECT}.policies.documents", FakeTable([field_("id", "INT64")], [[1]])
    )
    return list(
        BigQueryQuerySource(session=google_).pull(
            blob(), PullParams({"table": "policies.documents"})
        )
    )


@pytest.mark.parametrize(
    "error",
    [
        requests.ConnectTimeout("connect timed out"),
        requests.ReadTimeout("read timed out"),
    ],
)
def test_a_token_endpoint_timeout_is_a_timeout_not_a_bad_key(error):
    google_ = _google()
    google_.token_raise = error
    result = _list(google_)
    assert not result.successful and result.error_code == "timeout", result.error
    assert _BAD_KEY_HINT not in (result.error or "")
    assert len(google_.token_requests) == 1 and google_.api_calls() == []
    with pytest.raises(PipelineError) as raised:
        _pull(google_)
    assert host_answer(raised.value).status == 502
    assert "did not answer in time" in str(raised.value)
    assert _BAD_KEY_HINT not in str(raised.value)


def test_an_unreachable_token_endpoint_is_an_upstream_error_not_a_bad_key():
    google_ = _google()
    google_.token_raise = requests.ConnectionError("Name or service not known")
    result = _list(google_)
    assert not result.successful and result.error_code == "upstream_error"
    assert "could not reach Google's token endpoint (ConnectionError)" in result.error
    assert _BAD_KEY_HINT not in result.error


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
    """No hidden retries or backoff sleeps: one POST, then the host decides."""
    sleeps: list[float] = []
    monkeypatch.setattr(time, "sleep", sleeps.append)
    google_ = _google()
    google_.token_error = (status, body, {"Retry-After": "9"})
    result = _list(google_)
    assert not result.successful and result.error_code == "upstream_error", result.error
    assert _BAD_KEY_HINT not in (result.error or "")
    assert len(google_.token_requests) == 1 and sleeps == []
    assert result.retry_after == (9.0 if status == 503 else None)


def test_a_rate_limited_token_endpoint_is_rate_limited():
    google_ = _google()
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
    google_ = _google()
    google_.token_error = (status, body)
    result = _list(google_)
    assert not result.successful and result.error_code == "unauthorized"
    assert _BAD_KEY_HINT in (result.error or "")
    assert "PRIVATE KEY" not in (result.error or "")
    with pytest.raises(PipelineError) as raised:
        _pull(google_)
    # The tenant's key, not the caller: upstream (502), and it says so.
    assert host_answer(raised.value).status == 502
    assert _BAD_KEY_HINT in str(raised.value)
    assert "PRIVATE KEY" not in str(raised.value)


def test_our_clock_skew_is_not_blamed_on_the_key():
    google_ = _google()
    google_.token_error = (
        400,
        {
            "error": "invalid_grant",
            "error_description": "Invalid JWT: Token must be a short-lived token (60 "
            "minutes) and in a reasonable timeframe. Check your iat and exp values in "
            "the JWT claim.",
        },
    )
    result = _list(google_)
    assert not result.successful and result.error_code == "upstream_error"
    assert "clock is off" in (result.error or "")
    assert _BAD_KEY_HINT not in (result.error or "")


def test_an_ec_key_is_not_configured_and_nothing_escapes():
    config = blob(
        service_account_json={**service_account(), "private_key": ec_private_key_pem()}
    )
    google_ = _google()
    result = BigQueryToolkit(session=google_).call_tool(
        "BIGQUERY_LIST_DATASETS", {}, config
    )
    assert not result.successful and result.error_code == "not_connected", result.error
    assert "RSA service account key" in (result.error or "")
    assert "PRIVATE KEY" not in (result.error or "")
    assert google_.requests == []


def test_a_token_bigquery_rejects_is_dropped_and_reminted_once():
    google_ = _google()
    assert _list(google_).successful
    google_.revoke_tokens()
    result = _list(google_)
    assert result.successful, result.error
    assert len(google_.token_requests) == 2
    sent = [c["headers"]["Authorization"] for c in google_.api_calls()]
    # token 1 ok; token 1 rejected; token 2 ok
    assert sent == [f"Bearer ya29.fake-token-{n}" for n in (1, 1, 2)]


def test_a_new_token_bigquery_rejects_is_final_and_not_kept():
    google_ = _google()
    google_.reject_all_tokens = True
    result = _list(google_)
    assert not result.successful and result.error_code == "unauthorized"
    assert len(google_.token_requests) == 1 and len(google_.api_calls()) == 1
    google_.reject_all_tokens = False
    assert _list(google_).successful
    assert len(google_.token_requests) == 2
