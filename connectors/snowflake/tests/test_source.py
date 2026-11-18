"""snowflake-query-source: batches, the version markers, the caps, refusals."""

from __future__ import annotations

import json
import logging

import pytest
from aisquare.pipe.core.envelope import PullParams

from aisquare_pipe_snowflake import SnowflakeNotConfigured, SnowflakeQuerySource
from tests.fakes import Canned, FakeSnowflake, FakeTable, blob, col

COLUMNS = [col("ID", "fixed", scale=0), col("TITLE"), col("BODY")]
ROWS = [[i, f"policy {i}", f"text {i}"] for i in range(1, 6)]


def _fake(rows=None, **table) -> FakeSnowflake:
    fake = FakeSnowflake()
    fake.add_table(
        "POLICIES.PUBLIC.DOCS",
        FakeTable(COLUMNS, list(ROWS if rows is None else rows), **table),
    )
    return fake


def _pull(fake: FakeSnowflake, params: dict, config: dict | None = None):
    return list(
        SnowflakeQuerySource(session=fake).pull(config or blob(), PullParams(params))
    )


def _rows(envelopes) -> list[dict]:
    return [row for e in envelopes for row in json.loads(e.data)["rows"]]


def test_a_table_pull_yields_batches_with_the_version_markers_read_first():
    fake = _fake()
    envelopes = _pull(fake, {"table": "docs", "batch_size": 2})
    assert [e.metadata["row_count"] for e in envelopes] == [2, 2, 1]
    assert [r["ID"] for r in _rows(envelopes)] == [1, 2, 3, 4, 5]
    first = envelopes[0].metadata
    assert first["kind"] == "table" and first["table"] == "POLICIES.PUBLIC.DOCS"
    assert first["table_last_altered"] == "2025-11-17T10:00:00.000Z"
    assert first["version"] == f"2025-11-17T10:00:00.000Z|{first['result_sha256']}"
    assert {e.metadata["result_sha256"] for e in envelopes} == {first["result_sha256"]}
    assert [e.metadata["batch_index"] for e in envelopes] == [0, 1, 2]
    assert {e.metadata["batch_count"] for e in envelopes} == {3}
    assert [e.metadata["first_row"] for e in envelopes] == [0, 2, 4]
    assert first["result_row_count"] == 5 and "truncated" not in first
    assert envelopes[0].content_type == "application/json"
    assert envelopes[0].source_id == "snowflake:POLICIES.PUBLIC.DOCS#rows=0-2"
    assert envelopes[0].schema == {"fields": json.loads(envelopes[0].data)["columns"]}
    marker, select = fake.statements()
    assert "INFORMATION_SCHEMA.TABLES" in marker  # LAST_ALTERED before the rows
    assert select == 'SELECT * FROM "POLICIES"."PUBLIC"."DOCS" LIMIT 10001'


def test_the_version_changes_when_the_table_or_its_data_changes():
    fake = _fake()
    before = _pull(fake, {"table": "docs"})[0].metadata
    again = _pull(fake, {"table": "docs"})[0].metadata
    assert again["version"] == before["version"]
    table = fake.tables[("POLICIES", "PUBLIC", "DOCS")]
    table.rows[0][2] = "edited"
    table.last_altered = "1763377200.000"
    after = _pull(fake, {"table": "docs"})[0].metadata
    assert after["table_last_altered"] == "2025-11-17T11:00:00.000Z"
    assert after["result_sha256"] != before["result_sha256"]
    assert after["version"] != before["version"]
    # Background maintenance moves LAST_ALTERED alone: the hash shows no change.
    table.last_altered = "1763380800.000"
    maintained = _pull(fake, {"table": "docs"})[0].metadata
    assert maintained["result_sha256"] == after["result_sha256"]
    assert maintained["version"] != after["version"]


def test_the_result_hash_ignores_row_order():
    one = _pull(_fake(ROWS), {"table": "docs"})[0].metadata["result_sha256"]
    two = _pull(_fake(list(reversed(ROWS))), {"table": "docs"})[0].metadata[
        "result_sha256"
    ]
    assert one == two


def test_an_empty_table_is_one_envelope_with_its_markers():
    (envelope,) = _pull(_fake([]), {"table": "docs"})
    assert envelope.metadata["row_count"] == 0 and envelope.metadata["batch_count"] == 1
    assert envelope.metadata["table_last_altered"] == "2025-11-17T10:00:00.000Z"
    assert json.loads(envelope.data)["rows"] == []


def test_a_view_has_no_table_time_only_the_hash():
    (envelope,) = _pull(_fake(kind="VIEW"), {"table": "docs"})
    assert "table_last_altered" not in envelope.metadata
    assert envelope.metadata["version"] == envelope.metadata["result_sha256"]


def test_a_pull_cut_at_max_rows_says_so_and_resumes_with_start_index(caplog):
    caplog.set_level(logging.DEBUG)
    rows = [[i, f"t{i}", "b"] for i in range(10, 0, -1)]  # stored out of order
    fake = _fake(rows)
    seen: list[int] = []
    start = 0
    while True:
        envelopes = _pull(
            fake,
            {"table": "docs", "order_by": ["id"], "start_index": start, "max_rows": 3},
        )
        seen += [r["ID"] for r in _rows(envelopes)]
        meta = envelopes[0].metadata
        if "truncated" not in meta:
            break
        assert meta["truncated"] == "max_rows"
        assert {e.metadata["next_start_index"] for e in envelopes} == {start + 3}
        start = meta["next_start_index"]
    assert seen == list(range(1, 11))  # nothing skipped or repeated
    assert fake.statements()[1] == (
        'SELECT * FROM "POLICIES"."PUBLIC"."DOCS" ORDER BY "ID" LIMIT 4 OFFSET 0'
    )
    assert "DOCS" not in caplog.text and "SELECT" not in caplog.text


def test_a_truncated_pull_without_order_by_says_so_but_cannot_resume():
    envelopes = _pull(_fake(), {"table": "docs", "max_rows": 2})
    meta = envelopes[0].metadata
    assert meta["truncated"] == "max_rows" and "next_start_index" not in meta
    assert meta["result_row_count"] == 2


def test_a_pull_cut_at_max_result_bytes_says_so():
    big = [[i, "t", "x" * 1000] for i in range(10)]
    envelopes = _pull(_fake(big), {"table": "docs", "max_result_bytes": 3500})
    meta = envelopes[0].metadata
    assert meta["truncated"] == "max_result_bytes"
    assert meta["result_row_count"] == 3


def test_columns_and_order_by_are_quoted_identifiers():
    fake = _fake()
    envelopes = _pull(
        fake, {"table": "docs", "columns": ["id", "title"], "order_by": ["title"]}
    )
    assert _rows(envelopes)[0] == {"ID": 1, "TITLE": "policy 1"}
    assert fake.statements()[1] == (
        'SELECT "ID", "TITLE" FROM "POLICIES"."PUBLIC"."DOCS" ORDER BY "TITLE" LIMIT 10001 '
        "OFFSET 0"
    )


def test_a_query_pull_carries_the_latest_table_time_and_the_hash():
    fake = _fake()
    fake.add_table(
        "POLICIES.PUBLIC.TAGS",
        FakeTable([col("ID", "fixed", scale=0)], [[1]], last_altered="1763380800.000"),
    )
    sql = "SELECT d.id, d.title FROM docs d JOIN tags t ON t.id = d.id"
    fake.add(
        sql, Canned([col("ID", "fixed", scale=0), col("TITLE")], [[1, "policy 1"]])
    )
    (envelope,) = _pull(fake, {"sql": sql + ";"})
    meta = envelope.metadata
    assert meta["kind"] == "query" and len(meta["query_sha256"]) == 64
    assert meta["tables_last_altered"] == {
        "POLICIES.PUBLIC.DOCS": "2025-11-17T10:00:00.000Z",
        "POLICIES.PUBLIC.TAGS": "2025-11-17T12:00:00.000Z",
    }
    assert meta["table_last_altered"] == "2025-11-17T12:00:00.000Z"
    assert meta["version"] == f"2025-11-17T12:00:00.000Z|{meta['result_sha256']}"
    marker, query = fake.submits()
    assert (
        "INFORMATION_SCHEMA.TABLES" in marker["statement"]
    )  # one read, before the query
    assert [b["value"] for b in marker["bindings"].values()] == [
        "PUBLIC",
        "DOCS",
        "PUBLIC",
        "TAGS",
    ]
    assert query["statement"] == sql
    assert query["database"] == "POLICIES" and query["schema"] == "PUBLIC"
    assert sql not in json.dumps(meta)


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM docs, LATERAL FLATTEN(input => PARSE_JSON(body)) f",
        "SELECT * FROM POLICIES.INFORMATION_SCHEMA.TABLES",
        "SELECT * FROM doc_view",
        "SELECT * FROM docs JOIN missing ON 1 = 1",
    ],
)
def test_a_query_with_anything_but_visible_base_tables_has_only_the_hash(sql):
    fake = _fake()
    fake.add_table("POLICIES.PUBLIC.DOC_VIEW", FakeTable(COLUMNS, kind="VIEW"))
    fake.add(sql, Canned([col("N", "fixed")], [["1"]]))
    (envelope,) = _pull(fake, {"sql": sql})
    assert "table_last_altered" not in envelope.metadata
    assert envelope.metadata["version"] == envelope.metadata["result_sha256"]


def test_a_query_naming_a_database_the_role_cannot_see_still_runs():
    fake = _fake()
    sql = "SELECT * FROM hidden.s.t"
    fake.add(sql, Canned([col("N", "fixed")], [["1"]]))
    (envelope,) = _pull(fake, {"sql": sql})
    assert envelope.metadata["tables_last_altered"] == {}
    assert "table_last_altered" not in envelope.metadata


def test_database_and_schema_params_set_the_context():
    fake = _fake()
    fake.add_table("ARCHIVE.OLD.DOCS", FakeTable(COLUMNS, ROWS[:1]))
    envelopes = _pull(fake, {"table": "docs", "database": "archive", "schema": "old"})
    assert envelopes[0].metadata["table"] == "ARCHIVE.OLD.DOCS"
    assert _rows(envelopes)[0]["ID"] == 1


@pytest.mark.parametrize(
    "params",
    [
        {},
        {"table": "docs", "sql": "SELECT 1"},
        {"sql": "SELECT 1", "columns": ["a"]},
        {"sql": "SELECT 1", "order_by": ["a"]},
        {"sql": "SELECT 1", "start_index": 3},
        {"table": "docs", "start_index": 3},
        {"table": "docs", "columns": []},
        {"table": "docs", "columns": ["bad name"]},
        {"table": "docs", "order_by": "id"},
        {"table": "a.b.c.d"},
        {"table": "docs", "batch_size": 0},
        {"table": "docs", "max_rows": 100_001},
        {"table": "docs", "max_result_bytes": 0},
        {"table": "docs", "timeout_seconds": 601},
        {"table": "docs", "database": "a.b"},
        {"table": "docs", "unknown": 1},
        {"table": "docs", "role": "ACCOUNTADMIN"},
        {"table": "docs", "warehouse": "BIG_WH"},
        {"table": "docs", "account": "evil-account"},
        {"table": "docs", "user": "ADMIN"},
        {"table": "docs", "private_key_pem": "x"},
        {"table": "docs", "token_url": "https://evil.example.com/token"},
        {"table": "docs", "client_secret": "x"},
    ],
)
def test_bad_params_and_server_keys_are_refused_before_any_request(params):
    fake = _fake()
    with pytest.raises(ValueError):
        _pull(fake, params)
    assert fake.requests == []


@pytest.mark.parametrize(
    "config", [None, {}, {"account": "acme-analytics"}, blob(role="")]
)
def test_pull_without_credentials_fails_closed_before_any_request(config):
    fake = _fake()
    with pytest.raises(SnowflakeNotConfigured):
        list(
            SnowflakeQuerySource(session=fake).pull(
                config, PullParams({"table": "docs"})
            )
        )
    assert fake.requests == []


def test_a_name_without_its_database_needs_a_default():
    fake = _fake()
    with pytest.raises(ValueError, match="DB.SCHEMA.TABLE"):
        _pull(fake, {"table": "docs"}, blob(database=None, schema=None))
    assert fake.requests == []
