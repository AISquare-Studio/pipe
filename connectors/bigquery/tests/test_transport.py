"""The same fake behind a REAL requests.Session: requests encodes the JSON
bodies and query strings, and urllib3 reads (or breaks off) the answers."""

from __future__ import annotations

import json

from aisquare.pipe.core.envelope import PullParams

from aisquare_pipe_bigquery import BigQueryQuerySource, BigQueryToolkit
from tests.fakes import (
    PROJECT,
    CannedQuery,
    FakeGoogle,
    FakeTable,
    blob,
    field_,
    real_session,
)

SCHEMA = [field_("id", "INT64"), field_("updated", "TIMESTAMP")]


def _google() -> FakeGoogle:
    google = FakeGoogle()
    google.add_table(
        f"{PROJECT}.policies.documents",
        FakeTable(SCHEMA, [[1, "1763373600000000"], [2, None]]),
    )
    google.add_query(
        "SELECT * FROM policies.documents",
        CannedQuery(SCHEMA, [[1, "1763373600000000"]], polls=1),
    )
    return google


def test_tools_and_the_source_work_over_a_real_requests_session():
    google = _google()
    session = real_session(google)
    toolkit = BigQueryToolkit(session=session)
    result = toolkit.call_tool(
        "BIGQUERY_RUN_SELECT", {"sql": "SELECT * FROM policies.documents"}, blob()
    )
    assert result.successful, result.error
    assert result.data["rows"] == [{"id": 1, "updated": "2025-11-17T10:00:00.000000Z"}]
    run = google.runs()[0]
    assert run["maximumBytesBilled"] == str(1024**3) and run["useLegacySql"] is False
    poll = next(c for c in google.api_calls() if "/queries/job_1" in c["url"])
    assert poll["params"]["formatOptions.useInt64Timestamp"] == "true"
    assert poll["headers"]["Authorization"].startswith("Bearer ya29.")

    rows = toolkit.call_tool(
        "BIGQUERY_READ_TABLE_ROWS", {"table": "policies.documents"}, blob()
    )
    assert rows.successful, rows.error
    assert [r["id"] for r in rows.data["rows"]] == [1, 2]

    (envelope,) = BigQueryQuerySource(session=session).pull(
        blob(), PullParams({"table": "policies.documents"})
    )
    assert json.loads(envelope.data)["rows"][1] == {"id": 2, "updated": None}


def test_an_answer_that_breaks_off_midway_is_an_upstream_error():
    google = _google()
    session = real_session(google, body_break=ConnectionResetError("reset"))
    result = BigQueryToolkit(session=session).call_tool(
        "BIGQUERY_LIST_DATASETS", {}, blob()
    )
    assert not result.successful and result.error_code == "upstream_error", result.error
    assert "could not complete the request to BigQuery (ChunkedEncodingError)" in (
        result.error or ""
    )


def test_an_answer_that_times_out_midway_is_a_timeout():
    google = _google()
    session = real_session(google, body_break=TimeoutError("timed out"))
    result = BigQueryToolkit(session=session).call_tool(
        "BIGQUERY_LIST_DATASETS", {}, blob()
    )
    assert not result.successful and result.error_code == "timeout", result.error
