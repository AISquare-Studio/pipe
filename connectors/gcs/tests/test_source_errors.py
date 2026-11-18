"""How the source fails, as pipe-as-service answers it: 502 upstream_error
(a PipelineError, Retry-After when Cloud Storage gave one) for upstream and
transient failures, 422 (a ValueError) for what the caller can fix. Never a
bare Exception (500, with Cloud Storage's text in the host's log), and never
Cloud Storage's own error text in the message (the #37 review's m2)."""

from __future__ import annotations

import pytest
import requests
from aisquare.pipe.core.envelope import PullParams
from aisquare.pipe.errors import PipelineError
from aisquare.pipe.testing.host import host_answer

from aisquare_pipe_gcs import GcsSource
from aisquare_pipe_gcs.toolkit import GcsRequestError
from tests.fakes import FakeGoogle, StoredObject, blob

#: In every faked Cloud Storage error text: it must never reach the source's error.
LEAK = "policies/secret-payroll-xyz.pdf"


def _google() -> FakeGoogle:
    return FakeGoogle(objects={"policies/a.txt": StoredObject(b"hello", "text/plain")})


def _pull(google: FakeGoogle, **params) -> list:
    return list(GcsSource(session=google).pull(blob(), PullParams(params=params)))


def _failing(status: int, headers=None) -> FakeGoogle:
    google = _google()
    google.fail_with = (
        status,
        {"error": {"code": status, "message": f"failed for {LEAK}"}},
        headers or {},
    )
    return google


@pytest.mark.parametrize(
    ("status", "headers", "retry_after", "words"),
    [
        (403, {}, None, "objectViewer"),
        (429, {"Retry-After": "12"}, 12.0, "429"),
        (500, {}, None, "500"),
        (503, {"Retry-After": "4"}, 4.0, "503"),
        (401, {}, None, "Cloud Storage failed (401)"),
    ],
)
def test_upstream_failures_are_a_502_pipeline_error(
    status, headers, retry_after, words
):
    with pytest.raises(PipelineError) as raised:
        _pull(_failing(status, headers))
    e = raised.value
    assert type(e) is PipelineError  # the host maps the exact type
    assert host_answer(e).status == 502
    assert e.retry_after == retry_after
    assert words in str(e)
    assert LEAK not in str(e)
    assert e.__cause__ is None and e.__suppress_context__


@pytest.mark.parametrize("status", [400, 404])
def test_what_the_caller_can_fix_is_a_422_value_error(status):
    with pytest.raises(GcsRequestError) as raised:
        _pull(_failing(status))
    e = raised.value
    assert isinstance(e, ValueError) and e.status == status
    assert host_answer(e).status == 422
    assert LEAK not in str(e)


def test_a_named_object_that_does_not_exist_is_a_422():
    with pytest.raises(GcsRequestError) as raised:
        _pull(_google(), object_names=["policies/no-such-object.txt"])
    assert host_answer(raised.value).status == 422
    assert "no-such-object" not in str(raised.value)


def test_a_timeout_is_a_502():
    google = _google()
    google.raise_timeout = True
    with pytest.raises(PipelineError) as raised:
        _pull(google)
    assert host_answer(raised.value).status == 502
    assert "in time" in str(raised.value)


def test_a_connection_failure_is_a_502():
    google = _google()
    original = google.request

    def request(method, url, **kwargs):
        if "storage.googleapis.com" in url:
            raise requests.ConnectionError(f"refused {LEAK}")
        return original(method, url, **kwargs)

    google.request = request  # type: ignore[method-assign]
    with pytest.raises(PipelineError) as raised:
        _pull(google)
    assert host_answer(raised.value).status == 502
    assert LEAK not in str(raised.value)


@pytest.mark.parametrize(
    "params",
    [
        {"max_objects": 0},
        {"prefix": "elsewhere/"},
        {"bucket": "other"},
        {"nope": 1},
        {"modified_after": "yesterday"},
        {"object_names": ["policies/a.txt"], "prefix": "policies/"},
    ],
)
def test_refused_params_are_a_422(params):
    google = _google()
    with pytest.raises(ValueError) as raised:
        _pull(google, **params)
    assert host_answer(raised.value).status == 422
    assert google.requests == []


def test_params_that_are_not_a_mapping_are_a_422():
    with pytest.raises(ValueError) as raised:
        list(GcsSource(session=_google()).pull(blob(), ["prefix"]))  # type: ignore[arg-type]
    assert host_answer(raised.value).status == 422


def test_a_missing_credential_is_a_422():
    google = _google()
    with pytest.raises(ValueError) as raised:
        list(GcsSource(session=google).pull({}, PullParams(params={})))
    assert host_answer(raised.value).status == 422
    assert google.requests == []
