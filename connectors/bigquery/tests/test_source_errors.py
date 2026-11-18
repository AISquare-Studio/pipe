"""How the source fails, as pipe-as-service answers it: 502 upstream_error
(a PipelineError, Retry-After when BigQuery gave one) for upstream and
transient failures, 422 (a ValueError) for what the caller can fix. Never a
bare Exception (500, with BigQuery's text in the host's log), and never
BigQuery's own error text in the message (review #37 m2)."""

from __future__ import annotations

import pytest
import requests
from aisquare.pipe.core.envelope import PullParams
from aisquare.pipe.errors import PipelineError
from aisquare.pipe.testing.host import host_answer

from aisquare_pipe_bigquery import BigQueryQuerySource, BigQueryToolkit
from aisquare_pipe_bigquery.toolkit import BigQueryRequestError
from tests.fakes import (
    PROJECT,
    CannedQuery,
    FakeGoogle,
    FakeResponse,
    FakeTable,
    blob,
    field_,
)

#: In every faked BigQuery error text: it must never reach the source's error.
LEAK = "secret_payroll_xyz"
SQL = "SELECT id FROM policies.documents"


def _google() -> FakeGoogle:
    google = FakeGoogle()
    google.add_dataset(f"{PROJECT}.policies")
    google.add_table(
        f"{PROJECT}.policies.documents", FakeTable([field_("id", "INT64")], [[1]])
    )
    google.add_query(SQL, CannedQuery([field_("id", "INT64")], [[1]]))
    return google


def _error_body(status: int, reason: str, message: str) -> dict:
    return {
        "error": {
            "code": status,
            "message": message,
            "errors": [{"domain": "global", "reason": reason, "message": message}],
        }
    }


def _pull(google: FakeGoogle, **params) -> list:
    params = params or {"table": "policies.documents"}
    return list(BigQueryQuerySource(session=google).pull(blob(), PullParams(params)))


def _failing(status: int, reason: str, message: str, headers=None) -> FakeGoogle:
    google = _google()
    google.fail_with = (status, _error_body(status, reason, message), headers or {})
    return google


# (status, reason, BigQuery's message, Retry-After) -> (host status, retry_after,
# words the message must carry)
UPSTREAM = [
    (403, "accessDenied", f"Access Denied: Table {LEAK}", None, None, "dataViewer"),
    (403, "rateLimitExceeded", f"Exceeded rate limits: {LEAK}", None, None, "403"),
    (429, "rateLimitExceeded", f"Too many requests {LEAK}", "30", 30.0, "429"),
    (500, "backendError", f"Backend error {LEAK}", None, None, "backendError"),
    (503, "backendError", f"Service unavailable {LEAK}", "7", 7.0, "503"),
    (403, "billingNotEnabled", f"Billing {LEAK}", None, None, "billing is not enabled"),
    (
        403,
        "accessDenied",
        (
            f"Access Denied: BigQuery BigQuery: Permission denied while getting Drive "
            f"credentials. {LEAK}"
        ),
        None,
        None,
        "backed by Google Drive",
    ),
    (
        403,
        "quotaExceeded",
        (
            f"Custom quota exceeded: Your usage exceeded the custom quota for "
            f"QueryUsagePerDay, which is set by your administrator. {LEAK}"
        ),
        None,
        None,
        "resets at midnight Pacific time",
    ),
]


@pytest.mark.parametrize(
    ("status", "reason", "message", "header", "retry_after", "words"), UPSTREAM
)
def test_upstream_failures_are_a_502_pipeline_error(
    status, reason, message, header, retry_after, words
):
    headers = {"Retry-After": header} if header else {}
    with pytest.raises(PipelineError) as raised:
        _pull(_failing(status, reason, message, headers))
    error = raised.value
    assert type(error) is PipelineError  # the host maps the exact type
    assert host_answer(error).status == 502
    assert error.retry_after == retry_after
    assert words in str(error)
    assert LEAK not in str(error)
    assert error.__cause__ is None and error.__suppress_context__


# (status, reason, message, words the 422 carries)
CALLER = [
    (404, "notFound", f"Not found: Table {PROJECT}:policies.{LEAK}", "404, notFound"),
    (
        400,
        "invalidQuery",
        f"Unrecognized name: {LEAK} at [1:8]",
        "invalidQuery) at [1:8]",
    ),
    (400, "invalid", f"Invalid field {LEAK}", "400, invalid"),
    (400, "responseTooLarge", f"Response too large {LEAK}", "add a LIMIT"),
    (
        400,
        "bytesBilledLimitExceeded",
        f"Query exceeded limit for bytes billed {LEAK}",
        "nothing was billed",
    ),
]


@pytest.mark.parametrize(("status", "reason", "message", "words"), CALLER)
@pytest.mark.parametrize("kind", ["table", "sql"])
def test_what_the_caller_can_fix_is_a_422_value_error(
    status, reason, message, words, kind
):
    params = {"table": "policies.documents"} if kind == "table" else {"sql": SQL}
    with pytest.raises(BigQueryRequestError) as raised:
        _pull(_failing(status, reason, message), **params)
    error = raised.value
    assert isinstance(error, ValueError)
    assert host_answer(error).status == 422
    assert (error.status, error.reason) == (status, reason)
    assert words in str(error)
    assert LEAK not in str(error)


def test_a_missing_table_is_a_422():
    with pytest.raises(BigQueryRequestError) as raised:
        _pull(_google(), table="policies.no_such_table")
    assert host_answer(raised.value).status == 422
    assert "no_such_table" not in str(raised.value)  # BigQuery's text stays out


def test_a_timeout_is_a_502():
    google = _google()
    google.raise_timeout = True
    with pytest.raises(PipelineError) as raised:
        _pull(google)
    assert host_answer(raised.value).status == 502
    assert "did not answer in time" in str(raised.value)


def test_a_connection_failure_is_a_502():
    google = _google()
    original = google.request

    def request(method, url, **kwargs):
        if "bigquery.googleapis.com" in url:
            raise requests.ConnectionError(f"refused {LEAK}")
        return original(method, url, **kwargs)

    google.request = request  # type: ignore[method-assign]
    with pytest.raises(PipelineError) as raised:
        _pull(google)
    assert host_answer(raised.value).status == 502
    assert LEAK not in str(raised.value)


def test_a_refused_key_is_a_502_that_names_the_key():
    google = _google()
    google.token_error = (400, {"error": "invalid_grant", "error_description": "x"})
    with pytest.raises(PipelineError) as raised:
        _pull(google)
    assert host_answer(raised.value).status == 502
    assert "the tenant's BigQuery credential must be fixed" in str(raised.value)


def test_a_job_that_fails_access_denied_inside_a_200_is_a_502(monkeypatch):
    """BigQuery can answer a finished job's error inside a 200: its reason,
    not a hard-coded 400, decides (review #37 n4)."""
    google = _google()

    def answer(project, job_id, params):
        return FakeResponse(
            200,
            {
                "jobReference": {"projectId": project, "jobId": job_id},
                "jobComplete": True,
                "errors": [{"reason": "accessDenied", "message": f"Denied {LEAK}"}],
            },
        )

    monkeypatch.setattr(google, "_answer", answer)
    with pytest.raises(PipelineError) as raised:
        _pull(google, sql=SQL)
    assert host_answer(raised.value).status == 502
    assert "dataViewer" in str(raised.value) and LEAK not in str(raised.value)
    # The toolkit says forbidden, not invalid_arguments.
    result = BigQueryToolkit(session=google).call_tool(
        "BIGQUERY_RUN_SELECT", {"sql": SQL}, blob()
    )
    assert result.error_code == "forbidden", result.error


@pytest.mark.parametrize(
    "params",
    [
        {"sql": "DELETE FROM policies.documents WHERE true"},  # NotReadOnly
        {"sql": SQL, "max_bytes_processed": 10},  # CapExceeded (the dry run)
        {"table": "policies.documents", "batch_size": 0},  # a bad param
        {"table": "x", "sql": SQL},  # both
        {"project_id": "other"},  # server-controlled
        {"nope": 1},  # unknown
    ],
)
def test_refused_params_are_a_422(params):
    google = _google()
    if params.get("max_bytes_processed") == 10:
        google.queries[SQL].bytes_processed = 10**12
        params["max_bytes_processed"] = 10 * 1024 * 1024
    with pytest.raises(ValueError) as raised:
        _pull(google, **params)
    assert host_answer(raised.value).status == 422


def test_params_that_are_not_a_mapping_are_a_422():
    with pytest.raises(ValueError) as raised:
        list(BigQueryQuerySource(session=_google()).pull(blob(), ["table"]))  # type: ignore[arg-type]
    assert host_answer(raised.value).status == 422


def test_a_missing_credential_is_a_422():
    google = _google()
    with pytest.raises(ValueError) as raised:
        list(
            BigQueryQuerySource(session=google).pull(
                {}, PullParams({"table": "policies.documents"})
            )
        )
    assert host_answer(raised.value).status == 422
    assert google.requests == []


def test_the_toolkit_still_shows_bigquery_s_own_text():
    """The toolkit answers the caller directly, so it keeps BigQuery's message
    (it says which name is unknown); only the source leaves it out."""
    result = BigQueryToolkit(
        session=_failing(400, "invalidQuery", f"bad {LEAK}")
    ).call_tool("BIGQUERY_RUN_SELECT", {"sql": SQL}, blob())
    assert result.error_code == "invalid_arguments" and LEAK in (result.error or "")


def test_a_custom_quota_is_forbidden_in_the_toolkit_not_retry_later():
    message = (
        "Custom quota exceeded: Your usage exceeded the custom quota for "
        "QueryUsagePerDay, which is set by your administrator."
    )
    result = BigQueryToolkit(session=_failing(403, "quotaExceeded", message)).call_tool(
        "BIGQUERY_RUN_SELECT", {"sql": SQL}, blob()
    )
    assert result.error_code == "forbidden"
    assert result.retry_after is None
    assert "resets at midnight Pacific time" in (result.error or "")
    # A plain quotaExceeded is still a rate limit.
    plain = BigQueryToolkit(
        session=_failing(403, "quotaExceeded", "Quota exceeded: too many concurrent")
    ).call_tool("BIGQUERY_RUN_SELECT", {"sql": SQL}, blob())
    assert plain.error_code == "rate_limited"


def test_an_insufficient_scope_challenge_is_insufficient_scope():
    """failure() passes WWW-Authenticate, so core's insufficient_scope applies
    (review #37 n4)."""
    google = _failing(
        403,
        "accessDenied",
        "Request had insufficient authentication scopes.",
        {"WWW-Authenticate": 'Bearer realm="x", error="insufficient_scope"'},
    )
    google.fail_with[1]["error"]["errors"][0]["reason"] = "insufficientPermissions"
    result = BigQueryToolkit(session=google).call_tool(
        "BIGQUERY_LIST_DATASETS", {}, blob()
    )
    assert result.error_code == "insufficient_scope", result.error
