"""The same fake behind a REAL requests.Session: requests encodes the JSON
bodies, forms and query strings, and urllib3 reads (or breaks off) the answers."""

from __future__ import annotations

import json

from aisquare.pipe.core.envelope import PullParams

from aisquare_pipe_snowflake import SnowflakeQuerySource, SnowflakeToolkit
from tests.fakes import (
    Canned,
    FakeSnowflake,
    FakeTable,
    blob,
    col,
    oauth_blob,
    real_session,
)

COLUMNS = [col("ID", "fixed", scale=0), col("UPDATED", "timestamp_ltz", scale=3)]


def _fake() -> FakeSnowflake:
    fake = FakeSnowflake()
    fake.add_table(
        "POLICIES.PUBLIC.DOCS",
        FakeTable(COLUMNS, [[1, "1763373600.000"], [2, None]]),
    )
    fake.add(
        "SELECT * FROM docs",
        Canned(COLUMNS, [[1, "1763373600.000"], [2, None]], polls=1, partition_rows=1),
    )
    return fake


def test_tools_and_the_source_work_over_a_real_requests_session():
    fake = _fake()
    session = real_session(fake)
    toolkit = SnowflakeToolkit(session=session)
    result = toolkit.call_tool(
        "SNOWFLAKE_RUN_SELECT", {"statement": "SELECT * FROM docs"}, blob()
    )
    assert result.successful, result.error
    assert result.data["rows"] == [
        {"ID": 1, "UPDATED": "2025-11-17T10:00:00.000Z"},
        {"ID": 2, "UPDATED": None},
    ]
    submit = fake.submits()[0]
    assert submit["parameters"]["MULTI_STATEMENT_COUNT"] == "1"
    partition = [r for r in fake.api_calls() if r["params"].get("partition") == "1"]
    assert partition and partition[0]["headers"]["Authorization"].startswith(
        "Bearer ey"
    )

    described = toolkit.call_tool("SNOWFLAKE_DESCRIBE_TABLE", {"table": "docs"}, blob())
    assert described.successful, described.error
    assert described.data["last_altered"] == "2025-11-17T10:00:00.000Z"

    (envelope,) = SnowflakeQuerySource(session=session).pull(
        blob(), PullParams({"table": "docs"})
    )
    assert json.loads(envelope.data)["rows"][1] == {"ID": 2, "UPDATED": None}


def test_the_oauth_client_works_over_a_real_requests_session():
    fake = _fake()
    result = SnowflakeToolkit(session=real_session(fake)).call_tool(
        "SNOWFLAKE_SHOW_TABLES", {}, oauth_blob()
    )
    assert result.successful, result.error
    assert [t["name"] for t in result.data["tables"]] == ["DOCS"]


def test_an_answer_that_breaks_off_midway_is_an_upstream_error():
    session = real_session(_fake(), body_break=ConnectionResetError("reset"))
    result = SnowflakeToolkit(session=session).call_tool(
        "SNOWFLAKE_SHOW_DATABASES", {}, blob()
    )
    assert not result.successful and result.error_code == "upstream_error", result.error
    assert "could not complete the request to Snowflake (ChunkedEncodingError)" in (
        result.error or ""
    )


def test_an_answer_that_times_out_midway_is_a_timeout():
    session = real_session(_fake(), body_break=TimeoutError("timed out"))
    result = SnowflakeToolkit(session=session).call_tool(
        "SNOWFLAKE_SHOW_DATABASES", {}, blob()
    )
    assert not result.successful and result.error_code == "timeout", result.error
