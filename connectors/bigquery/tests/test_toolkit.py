"""BigQueryToolkit against a fake Google: listing, paging, schemas, SELECT,
table rows, the caps, failures, refusals and search."""

from __future__ import annotations

import pytest
from aisquare.pipe import PRIMARY_TAG

from aisquare_pipe_bigquery import BigQueryToolkit, toolkit
from aisquare_pipe_bigquery.credentials import GIB
from aisquare_pipe_bigquery.toolkit import RUN_SELECT, TOOL_SPECS
from tests.fakes import PROJECT, CannedQuery, FakeGoogle, FakeTable, blob, field_

DOCS = f"{PROJECT}.policies.documents"
SCHEMA = [
    field_("id", "INT64", "REQUIRED"),
    field_("title", "STRING", description="The policy's title"),
    field_("score", "FLOAT64"),
    field_("active", "BOOL"),
    field_("updated", "TIMESTAMP"),
    field_("amount", "NUMERIC"),
    field_("tags", "STRING", "REPEATED"),
    field_("owner", "RECORD", fields=[field_("name"), field_("team")]),
]
ROWS = [
    [1, "Travel", 0.5, True, "1763373600123456", "12.30", ["a", "b"], ["Ann", "ops"]],
    [2, "Expenses", None, False, None, None, [], None],
]
SQL = "SELECT * FROM policies.documents"


def _google() -> FakeGoogle:
    google = FakeGoogle()
    google.add_table(
        DOCS,
        FakeTable(
            SCHEMA,
            ROWS,
            last_modified_ms="1763373600000",
            description="Company policies",
        ),
    )
    for name in ("alpha", "beta", "gamma"):
        google.add_dataset(f"{PROJECT}.{name}")
    google.add_table(
        f"{PROJECT}.policies.current_view", FakeTable(SCHEMA, ROWS, type="VIEW")
    )
    google.add_dataset("other-proj.shared")
    google.add_query(SQL, CannedQuery(SCHEMA, ROWS, bytes_processed=4096))
    return google


def _call(google: FakeGoogle, slug: str, arguments: dict, config: dict | None = None):
    return BigQueryToolkit(session=google).call_tool(
        slug, arguments, blob() if config is None else config
    )


EXPECTED_ROW = {
    "id": 1,
    "title": "Travel",
    "score": 0.5,
    "active": True,
    "updated": "2025-11-17T10:00:00.123456Z",
    "amount": "12.30",
    "tags": ["a", "b"],
    "owner": {"name": "Ann", "team": "ops"},
}


# -- listing ----------------------------------------------------------------------


def test_list_datasets_pages_with_the_token():
    google = _google()
    first = _call(google, "BIGQUERY_LIST_DATASETS", {"page_size": 2})
    assert first.successful, first.error
    assert first.data["project_id"] == PROJECT and first.data["has_more"]
    second = _call(
        google,
        "BIGQUERY_LIST_DATASETS",
        {"page_size": 2, "page_token": first.data["next_page_token"]},
    )
    names = [d["dataset_id"] for d in first.data["datasets"] + second.data["datasets"]]
    assert names == ["alpha", "beta", "gamma", "policies"]
    assert second.data["has_more"] is False
    assert first.data["datasets"][0]["location"] == "US"


def test_list_datasets_of_another_project():
    data = _call(
        google := _google(), "BIGQUERY_LIST_DATASETS", {"project": "other-proj"}
    )
    assert [d["dataset_id"] for d in data.data["datasets"]] == ["shared"]
    assert "/projects/other-proj/datasets" in google.api_calls()[-1]["url"]


def test_list_tables_returns_ids_and_types():
    result = _call(_google(), "BIGQUERY_LIST_TABLES", {"dataset": "policies"})
    assert result.successful, result.error
    tables = {t["table_id"]: t["type"] for t in result.data["tables"]}
    assert tables == {"documents": "TABLE", "current_view": "VIEW"}
    assert result.data["dataset"] == f"{PROJECT}.policies"


def test_get_table_schema_returns_columns_and_the_version_marker():
    result = _call(
        _google(), "BIGQUERY_GET_TABLE_SCHEMA", {"table": "policies.documents"}
    )
    assert result.successful, result.error
    data = result.data
    assert data["table"] == DOCS and data["type"] == "TABLE"
    assert [c["name"] for c in data["columns"]] == [f["name"] for f in SCHEMA]
    assert data["columns"][1]["description"] == "The policy's title"
    assert data["columns"][-1]["fields"][0]["name"] == "name"
    assert data["last_modified_time"] == "2025-11-17T10:00:00.000Z"
    assert data["last_modified_ms"] == "1763373600000"
    assert (data["num_rows"], data["description"]) == (2, "Company policies")


# -- run_select ---------------------------------------------------------------------


def test_run_select_returns_typed_rows_after_a_dry_run():
    google = _google()
    result = _call(google, "BIGQUERY_RUN_SELECT", {"sql": SQL})
    assert result.successful, result.error
    data = result.data
    assert data["rows"][0] == EXPECTED_ROW
    assert data["rows"][1]["tags"] == [] and data["rows"][1]["owner"] is None
    assert (data["row_count"], data["total_rows"], data["truncated"]) == (2, 2, None)
    assert data["total_bytes_processed"] == 4096 and data["job_id"] == "job_1"
    assert [c["name"] for c in data["columns"]][:2] == ["id", "title"]
    dry, run = google.query_posts()
    assert dry["dryRun"] is True and "maximumBytesBilled" not in dry
    assert run["useLegacySql"] is False and run["location"] == "US"
    assert run["maximumBytesBilled"] == str(1 * GIB)
    assert int(run["jobTimeoutMs"]) == 60_000
    assert run["labels"] == {"client": "aisquare-pipe"}
    assert run["requestId"] and run["formatOptions"] == {"useInt64Timestamp": True}
    assert all(c["allow_redirects"] is False for c in google.api_calls())


def test_run_select_polls_until_the_job_completes():
    google = _google()
    google.add_query("SELECT 2", CannedQuery([field_("n", "INT64")], [[2]], polls=3))
    result = BigQueryToolkit(session=google).call_tool(
        "BIGQUERY_RUN_SELECT", {"sql": "SELECT 2"}, blob()
    )
    assert result.successful, result.error
    assert result.data["rows"] == [{"n": 2}]
    polls = [c for c in google.api_calls() if "/queries/job_1" in c["url"]]
    assert len(polls) == 3
    assert all(c["params"]["location"] == "US" for c in polls)
    assert all(0 <= c["params"]["timeoutMs"] <= 10_000 for c in polls)


def test_run_select_follows_result_pages():
    google = _google()
    rows = [[n] for n in range(25_000)]
    google.add_query("SELECT n", CannedQuery([field_("n", "INT64")], rows))
    result = _call(google, "BIGQUERY_RUN_SELECT", {"sql": "SELECT n", "max_rows": 5000})
    assert result.successful, result.error
    assert len(result.data["rows"]) == 5000 and result.data["truncated"] == "max_rows"
    assert result.data["total_rows"] == 25_000
    google.add_query("SELECT m", CannedQuery([field_("m", "INT64")], rows[:4500]))
    toolkit_ = BigQueryToolkit(session=google)
    toolkit_rows = toolkit_.call_tool(
        "BIGQUERY_RUN_SELECT", {"sql": "SELECT m", "max_rows": 5000}, blob()
    ).data
    assert len(toolkit_rows["rows"]) == 4500 and toolkit_rows["truncated"] is None


def test_run_select_pages_when_a_page_is_smaller_than_asked(monkeypatch):
    monkeypatch.setattr("aisquare_pipe_bigquery.query.PAGE_ROWS", 2)
    google = _google()
    google.add_query("SELECT n", CannedQuery([field_("n", "INT64")], [[1], [2], [3]]))
    result = _call(google, "BIGQUERY_RUN_SELECT", {"sql": "SELECT n"})
    assert [r["n"] for r in result.data["rows"]] == [1, 2, 3]
    tokens = [
        c["params"].get("pageToken")
        for c in google.api_calls()
        if "/queries/job_1" in c["url"]
    ]
    assert tokens == ["2"]


def test_max_rows_cuts_the_result_and_says_so():
    result = _call(_google(), "BIGQUERY_RUN_SELECT", {"sql": SQL, "max_rows": 1})
    assert result.successful
    assert len(result.data["rows"]) == 1 and result.data["truncated"] == "max_rows"


def test_max_rows_holds_when_bigquery_sends_more_rows_than_asked():
    google = _google()
    google.result_page_rows = 50
    google.add_query(
        "SELECT n", CannedQuery([field_("n", "INT64")], [[n] for n in range(60)])
    )
    result = _call(google, "BIGQUERY_RUN_SELECT", {"sql": "SELECT n", "max_rows": 3})
    assert result.successful, result.error
    assert [r["n"] for r in result.data["rows"]] == [0, 1, 2]
    assert result.data["truncated"] == "max_rows"


def test_the_result_byte_cap_cuts_the_result_and_says_so(monkeypatch):
    monkeypatch.setattr(
        toolkit, "MAX_RESULT_BYTES", 200
    )  # row 1 is 161 bytes, row 2 is 107
    result = _call(_google(), "BIGQUERY_RUN_SELECT", {"sql": SQL})
    assert result.successful
    assert len(result.data["rows"]) == 1
    assert result.data["truncated"] == "max_result_bytes"


def test_a_query_over_its_byte_cap_is_refused_by_the_dry_run_before_it_runs():
    google = _google()
    google.add_query(
        "SELECT big", CannedQuery([field_("x")], [], bytes_processed=5 * GIB)
    )
    result = _call(google, "BIGQUERY_RUN_SELECT", {"sql": "SELECT big"})
    assert not result.successful and result.error_code == "invalid_arguments"
    assert f"would process {5 * GIB} bytes" in (result.error or "")
    assert len(google.query_posts()) == 1 and google.runs() == []  # the dry run only
    ok = _call(
        google,
        "BIGQUERY_RUN_SELECT",
        {"sql": "SELECT big", "max_bytes_processed": 6 * GIB},
    )
    assert ok.successful, ok.error
    assert google.runs()[-1]["maximumBytesBilled"] == str(6 * GIB)


def test_bigquery_enforces_the_cap_when_the_estimate_was_low():
    """maximumBytesBilled is the same cap, so a dry run that under-estimated
    (clustering, a changed table) still can't bill more."""
    google = _google()
    google.add_query(
        "SELECT sneaky",
        CannedQuery([field_("x")], [], estimate=1000, bytes_processed=3 * GIB),
    )
    result = _call(google, "BIGQUERY_RUN_SELECT", {"sql": "SELECT sneaky"})
    assert not result.successful and result.error_code == "invalid_arguments"
    assert "bytesBilledLimitExceeded" in (result.error or "")
    assert "nothing was billed" in (result.error or "")


def test_the_tenant_ceiling_bounds_what_a_caller_may_ask_for():
    google = _google()
    config = blob(max_bytes_billed=100 * 1024**2)
    over = _call(
        google,
        "BIGQUERY_RUN_SELECT",
        {"sql": SQL, "max_bytes_processed": 1 * GIB},
        config,
    )
    assert not over.successful and over.error_code == "invalid_arguments"
    assert "over this connection's ceiling" in (over.error or "")
    assert google.requests == []
    default = _call(google, "BIGQUERY_RUN_SELECT", {"sql": SQL}, config)
    assert default.successful, default.error
    assert google.runs()[-1]["maximumBytesBilled"] == str(100 * 1024**2)


def test_dry_run_true_estimates_without_running():
    google = _google()
    result = _call(google, "BIGQUERY_RUN_SELECT", {"sql": SQL, "dry_run": True})
    assert result.successful and result.data["total_bytes_processed"] == 4096
    assert google.runs() == []


def test_bigquery_saying_it_is_not_a_select_refuses_it():
    """The dry run's statementType is BigQuery's own reading: anything but
    SELECT is refused before the job runs, whatever the guard admitted."""
    google = _google()
    google.add_query(
        "SELECT 3", CannedQuery([field_("x")], [], statement_type="SCRIPT")
    )
    result = _call(google, "BIGQUERY_RUN_SELECT", {"sql": "SELECT 3"})
    assert not result.successful and result.error_code == "invalid_arguments"
    assert "SCRIPT statement" in (result.error or "")
    assert google.runs() == []


def test_default_dataset_is_sent_as_bigquerys_default_dataset():
    google = _google()
    google.add_query("SELECT * FROM documents", CannedQuery(SCHEMA, ROWS))
    result = _call(
        google,
        "BIGQUERY_RUN_SELECT",
        {"sql": "SELECT * FROM documents", "default_dataset": "policies"},
    )
    assert result.successful, result.error
    assert google.runs()[0]["defaultDataset"] == {
        "projectId": PROJECT,
        "datasetId": "policies",
    }


def test_a_failed_job_is_reported():
    google = _google()
    google.add_query(
        "SELECT 4",
        CannedQuery(
            [field_("x")],
            [],
            polls=1,
            error=(400, "resourcesExceeded", "Resources exceeded during execution"),
        ),
    )
    result = _call(google, "BIGQUERY_RUN_SELECT", {"sql": "SELECT 4"})
    assert not result.successful and result.error_code == "invalid_arguments"
    assert "Resources exceeded" in (result.error or "")


# -- read_table_rows ----------------------------------------------------------------


def test_read_table_rows_reads_without_a_job_and_pages_by_start_index():
    google = _google()
    first = _call(
        google,
        "BIGQUERY_READ_TABLE_ROWS",
        {"table": "policies.documents", "max_rows": 1},
    )
    assert first.successful, first.error
    assert first.data["rows"] == [EXPECTED_ROW]
    assert (first.data["truncated"], first.data["next_start_index"]) == ("max_rows", 1)
    assert first.data["last_modified_time"] == "2025-11-17T10:00:00.000Z"
    second = _call(
        google,
        "BIGQUERY_READ_TABLE_ROWS",
        {"table": "policies.documents", "max_rows": 1, "start_index": 1},
    )
    assert second.data["rows"][0]["id"] == 2
    assert (second.data["truncated"], second.data["next_start_index"]) == (None, None)
    assert google.query_posts() == []  # no job, nothing billed


def test_read_table_rows_selected_fields_come_back_in_the_tables_order():
    google = _google()
    result = _call(
        google,
        "BIGQUERY_READ_TABLE_ROWS",
        {"table": f"`{DOCS}`", "selected_fields": ["active", "id"]},
    )
    assert result.successful, result.error
    assert result.data["rows"][0] == {"id": 1, "active": True}
    sent = next(c for c in google.api_calls() if c["url"].endswith("/data"))
    assert sent["params"]["selectedFields"] == "id,active"
    unknown = _call(
        google,
        "BIGQUERY_READ_TABLE_ROWS",
        {"table": "policies.documents", "selected_fields": ["nope"]},
    )
    assert not unknown.successful and unknown.error_code == "invalid_arguments"


def test_read_table_rows_refuses_a_view_and_points_at_select():
    google = _google()
    result = _call(
        google, "BIGQUERY_READ_TABLE_ROWS", {"table": "policies.current_view"}
    )
    assert not result.successful and result.error_code == "invalid_arguments"
    assert "VIEW" in (result.error or "") and "SELECT" in (result.error or "")
    assert not [c for c in google.api_calls() if c["url"].endswith("/data")]


# -- refusals, failures --------------------------------------------------------------


@pytest.mark.parametrize(
    "arguments",
    [
        {"sql": SQL, "project_id": "other-proj"},
        {"sql": SQL, "location": "EU"},
        {"sql": SQL, "service_account_json": {"type": "service_account"}},
        {"sql": SQL, "max_bytes_billed": 10**15},
        {"sql": SQL, "access_token": "ya29.attacker"},
    ],
)
def test_a_caller_cannot_override_a_server_key(arguments):
    google = _google()
    result = _call(google, "BIGQUERY_RUN_SELECT", arguments)
    assert not result.successful and result.error_code == "invalid_arguments"
    assert google.requests == []


def test_without_credentials_every_tool_fails_closed_without_a_request():
    google = _google()
    arguments = {
        "BIGQUERY_LIST_DATASETS": {},
        "BIGQUERY_LIST_TABLES": {"dataset": "policies"},
        "BIGQUERY_GET_TABLE_SCHEMA": {"table": "policies.documents"},
        "BIGQUERY_RUN_SELECT": {"sql": SQL},
        "BIGQUERY_READ_TABLE_ROWS": {"table": "policies.documents"},
    }
    assert set(arguments) == {t.slug for t in TOOL_SPECS}
    for slug, args in arguments.items():
        for config in (None, {}, {"project_id": PROJECT}):
            result = BigQueryToolkit(session=google).call_tool(slug, args, config)
            assert not result.successful and result.error_code == "not_connected"
            assert "BigQuery is not configured" in (result.error or "")
    assert google.requests == []


@pytest.mark.parametrize(
    ("arguments", "slug"),
    [
        ({"table": "documents"}, "BIGQUERY_GET_TABLE_SCHEMA"),
        ({"table": "policies.events_*"}, "BIGQUERY_GET_TABLE_SCHEMA"),
        ({"table": "a.b.c.d"}, "BIGQUERY_READ_TABLE_ROWS"),
        ({"dataset": "bad-dataset"}, "BIGQUERY_LIST_TABLES"),
        ({"project": "Not_A_Project"}, "BIGQUERY_LIST_DATASETS"),
        ({"page_token": "x" * 5000}, "BIGQUERY_LIST_DATASETS"),
        ({"sql": SQL, "default_dataset": "a.b.c"}, "BIGQUERY_RUN_SELECT"),
        ({"sql": SQL, "max_bytes_processed": 1}, "BIGQUERY_RUN_SELECT"),
    ],
)
def test_bad_references_and_arguments_are_refused_before_any_request(arguments, slug):
    google = _google()
    result = _call(google, slug, arguments)
    assert not result.successful and result.error_code == "invalid_arguments", result
    assert google.requests == []


@pytest.mark.parametrize(
    ("status", "reason", "message", "code", "hint"),
    [
        (403, "rateLimitExceeded", "Exceeded rate limits", "rate_limited", None),
        (403, "quotaExceeded", "Quota exceeded", "rate_limited", None),
        (429, "rateLimitExceeded", "Too many requests", "rate_limited", None),
        (
            403,
            "accessDenied",
            "Access Denied: Table acme-analytics:policies.documents",
            "forbidden",
            "roles/bigquery.dataViewer",
        ),
        (
            403,
            "accessDenied",
            "User does not have bigquery.jobs.create permission in project",
            "forbidden",
            "roles/bigquery.jobUser",
        ),
        (
            403,
            "accessDenied",
            "Request is prohibited by organization's policy. VPC Service Controls",
            "forbidden",
            "ingress rule",
        ),
        (
            403,
            "billingNotEnabled",
            "Billing has not been enabled",
            "forbidden",
            "billing",
        ),
        (404, "notFound", "Not found: Dataset acme-analytics:nope", "not_found", None),
        (400, "invalidQuery", "Syntax error at [1:8]", "invalid_arguments", None),
        (403, "responseTooLarge", "Response too large", "invalid_arguments", "LIMIT"),
        (500, "backendError", "Backend error", "upstream_error", None),
        (503, "backendError", "Service unavailable", "upstream_error", None),
        (400, "jobBackendError", "Job backend error", "upstream_error", None),
        (400, "timeout", "Query exceeded the job timeout", "timeout", None),
    ],
)
def test_bigquery_failures_map_to_canonical_codes(status, reason, message, code, hint):
    google = _google()
    google.fail_with = (
        status,
        {
            "error": {
                "code": status,
                "message": message,
                "errors": [{"reason": reason, "message": message}],
            }
        },
        {"Retry-After": "7"} if status in (429, 503) else {},
    )
    result = _call(google, "BIGQUERY_RUN_SELECT", {"sql": SQL})
    assert not result.successful and result.error_code == code, result.error
    assert reason in (result.error or "")
    if hint:
        assert hint in (result.error or "")
    if status in (429, 503):
        assert result.retry_after == 7.0
    if code == "forbidden" and reason == "accessDenied":
        assert "Google rejected the service account key" not in (result.error or "")


def test_a_timeout_is_a_timeout():
    google = _google()
    google.raise_timeout = True
    result = _call(google, "BIGQUERY_LIST_DATASETS", {})
    assert not result.successful and result.error_code == "timeout"


@pytest.mark.parametrize("status", [400, 403, 500])
def test_an_error_body_that_echoes_the_bearer_token_is_redacted(status):
    google = _google()
    google.fail_with = (
        status,
        {"error": {"message": "bad token {token} here", "errors": [{"reason": "x"}]}},
        {},
    )
    result = _call(google, "BIGQUERY_LIST_DATASETS", {})
    assert not result.successful
    assert "ya29." not in (result.error or "") and "[redacted]" in (result.error or "")


def test_a_redirect_is_not_followed():
    google = _google()
    google.fail_with = (302, {}, {"Location": "https://evil.example/"})
    result = _call(google, "BIGQUERY_LIST_DATASETS", {})
    assert not result.successful and result.error_code == "upstream_error"
    assert "redirects are not followed" in (result.error or "")


# -- catalogue ------------------------------------------------------------------------


def test_the_tools_are_read_only_strict_and_run_select_is_primary():
    tools = BigQueryToolkit().tools()
    assert {t.slug for t in tools} == {
        "BIGQUERY_LIST_DATASETS",
        "BIGQUERY_LIST_TABLES",
        "BIGQUERY_GET_TABLE_SCHEMA",
        "BIGQUERY_RUN_SELECT",
        "BIGQUERY_READ_TABLE_ROWS",
    }
    assert all(t.read_only and not t.destructive and t.risk == "read" for t in tools)
    assert [t.slug for t in tools if PRIMARY_TAG in t.tags] == [RUN_SELECT.slug]
    for spec in tools:
        assert spec.input_schema["additionalProperties"] is False
    assert (
        BigQueryToolkit.name == "bigquery"
        and BigQueryToolkit.slug_prefix == "BIGQUERY_"
    )


def test_the_toolkit_is_found_by_search_scored_and_search_connectors():
    from aisquare.pipe.core.search import search_connectors, search_scored

    assert search_connectors("bigquery data warehouse sql", limit=3)[0][0] == "bigquery"
    hits = search_scored(
        "run a sql query on our bigquery warehouse", connectors=["bigquery"], limit=2
    )
    assert hits[0].tool.slug == "BIGQUERY_RUN_SELECT"
    hits = search_scored(
        "what columns does the policies table have", connectors=["bigquery"], limit=1
    )
    assert hits[0].tool.slug == "BIGQUERY_GET_TABLE_SCHEMA"
    top = search_scored("query the bigquery data warehouse with sql", limit=5)
    assert any(h.connector == "bigquery" for h in top)
