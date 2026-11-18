"""How the source fails, as pipe-as-service answers it: 502 upstream_error
(a PipelineError, Retry-After when Glean gave one) for upstream and
transient failures, 422 (a ValueError) for what the caller can fix. Never a
bare Exception (500, with Glean's text in the host's log), and never Glean's
own error text in the message (the #37 review's m2)."""

from __future__ import annotations

import pytest
import requests
from aisquare.pipe.core.envelope import PullParams
from aisquare.pipe.errors import PipelineError
from aisquare.pipe.testing.host import host_answer

from aisquare_pipe_glean import GleanSource
from aisquare_pipe_glean.toolkit import GleanRequestError
from tests.fakes import USER_BLOB, FakeSession, documents, response, search_page

#: In every faked Glean error text: it must never reach the source's error.
LEAK = "secret payroll query xyz"


def _pull(*answers, **params):
    session = FakeSession().queue(*answers)
    params = params or {"query": "policy"}
    return list(GleanSource(session=session).pull(USER_BLOB, PullParams(params)))


@pytest.mark.parametrize(
    ("status", "headers", "retry_after", "words"),
    [
        (401, {}, None, "credential must be fixed"),
        (403, {}, None, "lacks the scope"),
        (429, {"Retry-After": "9"}, 9.0, "429"),
        (500, {}, None, "500"),
        (503, {}, None, "503"),
    ],
)
def test_upstream_failures_are_a_502_pipeline_error(
    status, headers, retry_after, words
):
    with pytest.raises(PipelineError) as raised:
        _pull(response(status, {"message": f"failed: {LEAK}"}, headers))
    e = raised.value
    assert type(e) is PipelineError  # the host maps the exact type
    assert host_answer(e).status == 502
    assert e.retry_after == retry_after
    assert words in str(e)
    assert LEAK not in str(e)
    assert e.__cause__ is None and e.__suppress_context__


@pytest.mark.parametrize("status", [400, 404, 422])
def test_what_the_caller_can_fix_is_a_422_value_error(status):
    with pytest.raises(GleanRequestError) as raised:
        _pull(response(status, {"message": f"bad request: {LEAK}"}))
    e = raised.value
    assert isinstance(e, ValueError) and e.status == status
    assert host_answer(e).status == 422
    assert LEAK not in str(e)


def test_a_failure_reading_the_documents_is_a_502_too():
    with pytest.raises(PipelineError) as raised:
        _pull(response(200, search_page("d1")), response(500, {"message": LEAK}))
    assert host_answer(raised.value).status == 502
    assert "getdocuments" in str(raised.value) and LEAK not in str(raised.value)


@pytest.mark.parametrize(
    "error", [requests.ReadTimeout(f"timed out {LEAK}"), requests.ConnectionError(LEAK)]
)
def test_timeouts_and_connection_failures_are_a_502(error):
    with pytest.raises(PipelineError) as raised:
        _pull(error)
    assert host_answer(raised.value).status == 502
    assert LEAK not in str(raised.value)
    assert raised.value.__cause__ is None and raised.value.__suppress_context__


@pytest.mark.parametrize(
    "params",
    [
        {"query": ""},
        {"query": "q", "document_ids": ["d1"]},
        {"query": "q", "max_documents": 0},
        {"query": "q", "token": "x"},
        {"nope": 1, "query": "q"},
    ],
)
def test_refused_params_are_a_422(params):
    session = FakeSession()
    with pytest.raises(ValueError) as raised:
        list(GleanSource(session=session).pull(USER_BLOB, PullParams(params)))
    assert host_answer(raised.value).status == 422
    assert session.calls == []


def test_params_that_are_not_a_mapping_are_a_422():
    with pytest.raises(ValueError) as raised:
        list(GleanSource(session=FakeSession()).pull(USER_BLOB, ["query"]))  # type: ignore[arg-type]
    assert host_answer(raised.value).status == 422


def test_a_missing_credential_is_a_422():
    session = FakeSession()
    with pytest.raises(ValueError) as raised:
        list(GleanSource(session=session).pull({}, PullParams({"query": "q"})))
    assert host_answer(raised.value).status == 422
    assert session.calls == []


def test_documents_glean_cannot_read_are_skipped_not_raised():
    envelopes = _pull(
        response(200, search_page("d1")),
        response(200, documents()),
    )
    assert envelopes == []
