"""bigquery-query-source against a fake Google: batches, version markers,
truncation and resume, caps and refusals."""

from __future__ import annotations

import json
import logging

import pytest
from aisquare.pipe.core.envelope import PullParams

from aisquare_pipe_bigquery import (
    BigQueryNotConfigured,
    BigQueryQuerySource,
    CapExceeded,
    NotATable,
)
from aisquare_pipe_bigquery.credentials import GIB
from tests.fakes import PROJECT, CannedQuery, FakeGoogle, FakeTable, blob, field_

DOCS = f"{PROJECT}.policies.documents"
SCHEMA = [field_("id", "INT64"), field_("title"), field_("body")]


def _rows(n: int) -> list[list]:
    return [[i, f"Policy {i}", f"text {i}"] for i in range(n)]


def _google(rows: int = 5) -> FakeGoogle:
    google = FakeGoogle()
    google.add_table(
        DOCS, FakeTable(SCHEMA, _rows(rows), last_modified_ms="1763373600000")
    )
    google.add_table(
        f"{PROJECT}.policies.owners",
        FakeTable(
            [field_("id", "INT64"), field_("owner")],
            [[0, "Ann"]],
            last_modified_ms="1763380800000",
        ),
    )
    google.add_table(f"{PROJECT}.policies.current", FakeTable(SCHEMA, [], type="VIEW"))
    return google


def _pull(google: FakeGoogle, params: dict, config: dict | None = None) -> list:
    return list(
        BigQueryQuerySource(session=google).pull(
            blob() if config is None else config, PullParams(params)
        )
    )


def _data(envelope) -> dict:
    return json.loads(envelope.data)


# -- table pulls ----------------------------------------------------------------------


def test_a_table_pull_yields_one_envelope_per_batch_with_the_version_markers():
    google = _google(rows=5)
    envelopes = _pull(google, {"table": "policies.documents", "batch_size": 2})
    assert [e.metadata["row_count"] for e in envelopes] == [2, 2, 1]
    assert [e.metadata["first_row"] for e in envelopes] == [0, 2, 4]
    assert [e.metadata["batch_index"] for e in envelopes] == [0, 1, 2]
    first = envelopes[0]
    assert first.content_type == "application/json"
    assert _data(first)["rows"] == [
        {"id": 0, "title": "Policy 0", "body": "text 0"},
        {"id": 1, "title": "Policy 1", "body": "text 1"},
    ]
    assert [c["name"] for c in _data(first)["columns"]] == ["id", "title", "body"]
    assert first.schema == {"fields": _data(first)["columns"]}
    meta = first.metadata
    assert meta["kind"] == "table" and meta["table"] == DOCS
    assert meta["table_last_modified"] == "2025-11-17T10:00:00.000Z"
    assert meta["table_last_modified_ms"] == "1763373600000"
    assert meta["version"] == f"1763373600000:{meta['result_sha256']}"
    assert (meta["batch_count"], meta["result_row_count"], meta["total_rows"]) == (
        3,
        5,
        5,
    )
    assert len({e.metadata["result_sha256"] for e in envelopes}) == 1
    assert len({e.metadata["batch_sha256"] for e in envelopes}) == 3
    assert "truncated" not in meta
    assert first.source_id == f"bigquery:{DOCS}#rows=0-2"
    assert google.query_posts() == []  # tabledata.list: no job, nothing billed
    # The marker (tables.get) is read once, BEFORE the rows.
    calls = [c["url"].rsplit("/", 1)[-1] for c in google.api_calls()]
    assert calls == ["documents", "data"]


def test_a_table_pull_reads_across_api_pages(monkeypatch):
    monkeypatch.setattr("aisquare_pipe_bigquery.query.PAGE_ROWS", 3)
    google = _google(rows=8)
    envelopes = _pull(google, {"table": "policies.documents", "batch_size": 100})
    ((envelope),) = envelopes
    assert [r["id"] for r in _data(envelope)["rows"]] == list(range(8))
    pages = [c for c in google.api_calls() if c["url"].endswith("/data")]
    assert [c["params"].get("pageToken") for c in pages] == [None, "3", "6"]


def test_the_version_changes_when_the_table_or_its_data_changes():
    google = _google(rows=3)
    before = _pull(google, {"table": "policies.documents"})[0].metadata
    again = _pull(google, {"table": "policies.documents"})[0].metadata
    assert again["version"] == before["version"]

    table = google.data[PROJECT]["policies"]["documents"]
    table.rows[1][2] = "edited text"
    table.last_modified_ms = "1763384400000"
    after = _pull(google, {"table": "policies.documents"})[0].metadata
    assert after["result_sha256"] != before["result_sha256"]
    assert after["table_last_modified"] == "2025-11-17T13:00:00.000Z"
    assert after["version"] != before["version"]


def test_the_result_hash_ignores_row_order():
    google = _google(rows=4)
    before = _pull(google, {"table": "policies.documents"})[0].metadata["result_sha256"]
    google.data[PROJECT]["policies"]["documents"].rows.reverse()
    after = _pull(google, {"table": "policies.documents"})[0].metadata["result_sha256"]
    assert after == before


def test_an_empty_table_is_one_envelope_with_its_markers():
    google = FakeGoogle()
    google.add_table(DOCS, FakeTable(SCHEMA, []))
    (envelope,) = _pull(google, {"table": "policies.documents"})
    assert _data(envelope)["rows"] == [] and envelope.metadata["row_count"] == 0
    assert envelope.metadata["table_last_modified_ms"] == "1763373600000"


def test_a_pull_cut_at_max_rows_says_so_and_resumes_with_start_index(caplog):
    caplog.set_level(logging.INFO)
    google = _google(rows=7)
    seen: list[int] = []
    params: dict = {"table": "policies.documents", "max_rows": 3, "batch_size": 2}
    for _ in range(5):
        envelopes = _pull(google, params)
        seen += [r["id"] for e in envelopes for r in _data(e)["rows"]]
        meta = envelopes[-1].metadata
        if "truncated" not in meta:
            break
        assert meta["truncated"] == "max_rows"
        assert all(e.metadata["truncated"] == "max_rows" for e in envelopes)
        params = {**params, "start_index": meta["next_start_index"]}
    assert seen == list(range(7))  # nothing skipped, nothing repeated
    assert envelopes[0].metadata["first_row"] == 6
    assert "documents" not in caplog.text and "policies" not in caplog.text


def test_a_pull_cut_at_max_result_bytes_says_where_to_continue():
    google = _google(rows=4)
    envelopes = _pull(google, {"table": "policies.documents", "max_result_bytes": 100})
    rows = [r for e in envelopes for r in _data(e)["rows"]]
    meta = envelopes[0].metadata
    assert meta["truncated"] == "max_result_bytes"
    assert meta["next_start_index"] == len(rows) < 4


def test_selected_fields_read_only_those_columns():
    envelopes = _pull(
        _google(rows=2), {"table": "policies.documents", "selected_fields": ["title"]}
    )
    assert _data(envelopes[0])["rows"] == [{"title": "Policy 0"}, {"title": "Policy 1"}]


def test_a_view_is_refused_for_a_table_pull():
    with pytest.raises(NotATable, match="VIEW"):
        _pull(_google(), {"table": "policies.current"})


# -- query pulls -----------------------------------------------------------------------


JOIN = (
    "SELECT d.id, d.title, o.owner FROM policies.documents AS d "
    "JOIN policies.owners AS o USING (id)"
)
JOIN_SCHEMA = [field_("id", "INT64"), field_("title"), field_("owner")]


def test_a_query_pull_carries_the_latest_table_time_and_the_result_hash():
    google = _google()
    google.add_query(JOIN, CannedQuery(JOIN_SCHEMA, [[0, "Policy 0", "Ann"]]))
    (envelope,) = _pull(google, {"sql": JOIN})
    meta = envelope.metadata
    assert meta["kind"] == "query" and "table" not in meta
    assert len(meta["query_sha256"]) == 64 and JOIN not in json.dumps(meta)
    assert meta["tables_last_modified"] == {
        DOCS: "2025-11-17T10:00:00.000Z",
        f"{PROJECT}.policies.owners": "2025-11-17T12:00:00.000Z",
    }
    assert meta["table_last_modified_ms"] == "1763380800000"  # the later one
    assert meta["version"] == f"1763380800000:{meta['result_sha256']}"
    assert meta["job_id"] == "job_1" and meta["total_bytes_processed"] == 2048
    assert _data(envelope)["rows"] == [{"id": 0, "title": "Policy 0", "owner": "Ann"}]
    # The markers are read BEFORE the job runs.
    order = [
        ("get" if "/tables/" in c["url"] else "run")
        for c in google.api_calls()
        if "/tables/" in c["url"]
        or (c["method"] == "POST" and not json.loads(c["data"]).get("dryRun"))
    ]
    assert order == ["get", "get", "run"]


def test_a_query_over_a_view_has_no_table_time_only_the_hash():
    google = _google()
    google.add_query("SELECT * FROM policies.current", CannedQuery(SCHEMA, _rows(1)))
    (envelope,) = _pull(google, {"sql": "SELECT * FROM policies.current"})
    meta = envelope.metadata
    assert "table_last_modified" not in meta and "table_last_modified_ms" not in meta
    assert meta["version"] == meta["result_sha256"]


def test_a_query_naming_a_missing_table_still_runs_without_a_table_time():
    google = _google()
    sql = "SELECT * FROM policies.documents JOIN policies.gone USING (id)"
    google.add_query(sql, CannedQuery(SCHEMA, _rows(1)))
    meta = _pull(google, {"sql": sql})[0].metadata
    assert "table_last_modified" not in meta
    assert list(meta["tables_last_modified"]) == [DOCS]


def test_a_query_pull_in_batches_with_one_result_hash():
    google = _google()
    google.add_query("SELECT * FROM policies.documents", CannedQuery(SCHEMA, _rows(5)))
    envelopes = _pull(
        google, {"sql": "SELECT * FROM policies.documents", "batch_size": 2}
    )
    assert [e.metadata["row_count"] for e in envelopes] == [2, 2, 1]
    assert len({e.metadata["result_sha256"] for e in envelopes}) == 1


def test_a_query_over_its_byte_cap_is_refused_before_it_runs():
    google = _google()
    sql = "SELECT * FROM policies.documents"
    google.add_query(sql, CannedQuery(SCHEMA, _rows(1), bytes_processed=2 * GIB))
    with pytest.raises(CapExceeded):
        _pull(google, {"sql": sql})
    assert google.runs() == []
    assert len(_pull(google, {"sql": sql, "max_bytes_processed": 3 * GIB})) == 1
    assert google.runs()[-1]["maximumBytesBilled"] == str(3 * GIB)
    with pytest.raises(ValueError, match="ceiling"):
        _pull(
            google,
            {"sql": sql, "max_bytes_processed": 3 * GIB},
            blob(max_bytes_billed=GIB),
        )


def test_the_job_timeout_is_sent_as_bigquerys_job_deadline():
    google = _google()
    google.add_query("SELECT 1", CannedQuery([field_("x", "INT64")], [[1]]))
    _pull(google, {"sql": "SELECT 1", "job_timeout_seconds": 45})
    assert google.runs()[0]["jobTimeoutMs"] == "45000"
    _pull(google, {"sql": "SELECT 1"})
    assert google.runs()[1]["jobTimeoutMs"] == "120000"


# -- refusals ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "params",
    [
        {"table": "policies.documents", "project_id": "other-proj"},
        {"table": "policies.documents", "location": "EU"},
        {"table": "policies.documents", "service_account_json": {}},
        {"sql": "SELECT 1", "max_bytes_billed": 10**15},
        {"table": "policies.documents", "nope": 1},
        {},
        {"table": "policies.documents", "sql": "SELECT 1"},
        {"table": "policies.documents", "max_bytes_processed": GIB},
        {"table": "policies.documents", "job_timeout_seconds": 5},
        {"sql": "SELECT 1", "start_index": 3},
        {"sql": "SELECT 1", "selected_fields": ["id"]},
        {"table": "policies.events_*"},
        {"table": "policies.documents", "batch_size": 0},
        {"table": "policies.documents", "max_rows": 10**6},
        {"sql": "SELECT 1", "job_timeout_seconds": 3600},
        {"sql": "SELECT 1", "default_dataset": "a.b.c"},
    ],
)
def test_bad_params_and_server_keys_are_refused_before_any_request(params):
    google = _google()
    with pytest.raises(ValueError):
        _pull(google, params)
    assert google.requests == []


@pytest.mark.parametrize("config", [None, {}, {"project_id": PROJECT}])
def test_pull_without_credentials_fails_closed_before_any_request(config):
    google = _google()
    with pytest.raises(BigQueryNotConfigured, match="configured|missing"):
        list(
            BigQueryQuerySource(session=google).pull(
                config,  # type: ignore[arg-type]
                PullParams({"table": "policies.documents"}),
            )
        )
    assert google.requests == []
