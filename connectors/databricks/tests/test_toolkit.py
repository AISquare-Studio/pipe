"""DatabricksToolkit against a fake workspace: files, SQL, catalog, refusals."""

from __future__ import annotations

import pytest
import requests
from aisquare.pipe import PRIMARY_TAG

from aisquare_pipe_databricks import DatabricksToolkit
from aisquare_pipe_databricks.toolkit import READ_FILE, TOOL_SPECS
from tests.fakes import PAT, Clock, FakeDatabricks, Statement, StoredFile, pat_blob

VOL = "/Volumes/main/policies/docs"
TEXT = b"Travel policy: economy class.\n"


def _fake() -> FakeDatabricks:
    fake = FakeDatabricks()
    fake.volume[f"{VOL}/travel.txt"] = StoredFile(TEXT, "text/plain")
    fake.volume[f"{VOL}/handbook.pdf"] = StoredFile(
        b"%PDF-1.7 hb", "application/pdf", etag='"etag-pdf"'
    )
    fake.volume[f"{VOL}/2026/retention.md"] = StoredFile(b"# r", "text/markdown")
    fake.dbfs["/mnt/policies/a.txt"] = StoredFile(b"dbfs text body", "text/plain")
    return fake


def _call(
    fake: FakeDatabricks,
    slug: str,
    arguments: dict,
    config: dict | None = None,
    clock: Clock | None = None,
):
    kit = DatabricksToolkit(
        session=fake, clock=clock, sleep=clock.sleep if clock else None
    )
    return kit.call_tool(slug, arguments, pat_blob() if config is None else config)


# -- files -----------------------------------------------------------------------------


def test_read_a_volume_file_with_its_version_markers():
    fake = _fake()
    result = _call(fake, "DATABRICKS_READ_FILE", {"path": f"{VOL}/travel.txt"})
    assert result.successful, result.error
    data = result.data
    assert data["content"] == TEXT.decode()
    assert (data["storage"], data["size"], data["content_type"]) == (
        "volume",
        len(TEXT),
        "text/plain",
    )
    assert data["modified_time"] == "2025-11-17T10:00:00Z"
    assert data["etag"] == '"etag-1"' and len(data["sha256"]) == 64
    (envelope,) = result.files
    assert envelope.data == TEXT
    assert {"modified_time", "etag", "sha256"} <= set(envelope.metadata)
    call = fake.api_calls()[-1]
    assert call["url"].endswith(
        "/api/2.0/fs/files/Volumes/main/policies/docs/travel.txt"
    )
    assert (
        call["allow_redirects"] is False
        and call["headers"]["Authorization"] == f"Bearer {PAT}"
    )


def test_read_a_dbfs_file_across_reads():
    result = _call(
        _fake(),
        "DATABRICKS_READ_FILE",
        {"path": "dbfs:/mnt/policies/a.txt"},
        pat_blob(dbfs=True),
    )
    assert result.successful, result.error
    assert result.files[0].data == b"dbfs text body"
    assert (
        result.data["storage"] == "dbfs"
        and result.data["modified_time"] == "2025-11-17T10:00:00Z"
    )


def test_metadata_only_and_the_size_cap():
    fake = _fake()
    meta = _call(
        fake,
        "DATABRICKS_READ_FILE",
        {"path": f"{VOL}/handbook.pdf", "include_content": False},
    )
    assert meta.successful and meta.files == [] and meta.data["etag"] == '"etag-pdf"'
    assert fake.api_calls()[-1]["method"] == "HEAD"
    big = _call(
        fake, "DATABRICKS_READ_FILE", {"path": f"{VOL}/handbook.pdf", "max_bytes": 3}
    )
    assert not big.successful and big.error_code == "invalid_arguments"


def test_list_files_pages_and_marks_directories():
    fake = _fake()
    first = _call(
        fake, "DATABRICKS_LIST_FILES", {"path": f"{VOL}/", "page_size": 2}
    ).data
    assert first["has_more"] and first["next_page_token"]
    second = _call(
        fake,
        "DATABRICKS_LIST_FILES",
        {"path": VOL, "page_size": 2, "page_token": first["next_page_token"]},
    ).data
    entries = first["entries"] + second["entries"]
    assert {e["name"]: e["is_directory"] for e in entries} == {
        "2026": True,
        "handbook.pdf": False,
        "travel.txt": False,
    }
    assert all(e["modified_time"] for e in entries if not e["is_directory"])
    dbfs = _call(
        fake,
        "DATABRICKS_LIST_FILES",
        {"path": "dbfs:/mnt/policies"},
        pat_blob(dbfs=True),
    ).data
    assert [e["path"] for e in dbfs["entries"]] == ["dbfs:/mnt/policies/a.txt"]


@pytest.mark.parametrize(
    "path",
    [
        "/Workspace/Users/x",
        "/Volumes/main",
        "dbfs:/mnt/../etc",
        "/Volumes/main/p/docs/",
        "s3://bucket/x",
    ],
)
def test_paths_outside_volumes_and_dbfs_are_refused_without_a_request(path):
    fake = _fake()
    result = _call(fake, "DATABRICKS_READ_FILE", {"path": path}, pat_blob(dbfs=True))
    assert not result.successful and result.error_code == "invalid_arguments"
    assert fake.api_calls() == []


@pytest.mark.parametrize("config", [pat_blob(), pat_blob(dbfs=False)])
@pytest.mark.parametrize(
    ("slug", "path"),
    [
        ("DATABRICKS_READ_FILE", "dbfs:/mnt/policies/a.txt"),
        ("DATABRICKS_LIST_FILES", "dbfs:/"),
        ("DATABRICKS_LIST_FILES", "dbfs:/mnt/policies"),
    ],
)
def test_dbfs_is_forbidden_unless_the_credential_enables_it(config, slug, path):
    """DBFS has no Unity Catalog grants: its root and every mount are
    readable by every workspace user. So it is off unless the tenant's blob
    says "dbfs": true, and a volume-only tenant can't be read through it
    (seq 13990 m4)."""
    fake = _fake()
    result = _call(fake, slug, {"path": path}, config)
    assert not result.successful and result.error_code == "forbidden"
    assert 'no "dbfs": true' in (result.error or "")
    assert fake.requests == []


@pytest.mark.parametrize("flag", ["true", 1, "yes", None])
def test_a_dbfs_flag_that_is_not_a_boolean_is_not_configured(flag):
    fake = _fake()
    result = _call(
        fake, "DATABRICKS_READ_FILE", {"path": f"{VOL}/travel.txt"}, pat_blob(dbfs=flag)
    )
    assert not result.successful and result.error_code == "not_connected"
    assert "dbfs must be true or false" in (result.error or "")
    assert fake.requests == []


@pytest.mark.parametrize(
    ("slug", "arguments"),
    [
        (
            "DATABRICKS_READ_FILE",
            {"path": f"{VOL}/travel.txt", "workspace_url": "https://evil.example"},
        ),
        ("DATABRICKS_READ_FILE", {"path": f"{VOL}/travel.txt", "token": "dapi-mine"}),
        ("DATABRICKS_RUN_SELECT", {"statement": "SELECT 1", "warehouse_id": "other"}),
        ("DATABRICKS_LIST_FILES", {"path": "dbfs:/mnt/policies", "dbfs": True}),
    ],
)
def test_a_caller_cannot_override_a_server_key(slug, arguments):
    fake = _fake()
    result = _call(fake, slug, arguments)
    assert not result.successful and result.error_code == "invalid_arguments"
    assert fake.requests == []


def test_without_credentials_every_tool_fails_closed_without_a_request():
    fake = _fake()
    for config in (
        {},
        {"workspace_url": "https://acme.cloud.databricks.com"},
        pat_blob(token=""),
    ):
        for spec in TOOL_SPECS:
            result = DatabricksToolkit(session=fake).call_tool(spec.slug, {}, config)
            assert not result.successful and result.error_code == "not_connected", (
                spec.slug
            )
            assert result.error.startswith("Databricks is not configured")
    assert fake.requests == []


# -- SQL ---------------------------------------------------------------------------------


def test_run_select_returns_rows_with_the_caps_sent():
    fake = _fake()
    fake.next_statement = Statement(
        ["SUCCEEDED"],
        rows=[["travel", "2026-01-01"], ["retention", "2026-02-01"]],
        columns=[
            {"name": "title", "type_name": "STRING"},
            {"name": "updated", "type_name": "DATE"},
        ],
    )
    result = _call(
        fake,
        "DATABRICKS_RUN_SELECT",
        {
            "statement": "SELECT title, updated FROM docs",
            "row_limit": 50,
            "catalog": "main",
            "schema": "policies",
        },
    )
    assert result.successful, result.error
    assert result.data["columns"] == [
        {"name": "title", "type": "STRING"},
        {"name": "updated", "type": "DATE"},
    ]
    assert result.data["rows"][0] == ["travel", "2026-01-01"]
    assert (result.data["row_count"], result.data["truncated"]) == (2, False)
    (submit,) = fake.sql_submissions()
    body = submit["json"]
    assert body["warehouse_id"] == "abc123def456" and body["row_limit"] == 50
    assert body["byte_limit"] == 8 * 1024 * 1024 and body["disposition"] == "INLINE"
    assert (body["catalog"], body["schema"]) == ("main", "policies")
    assert body["on_wait_timeout"] == "CONTINUE"


@pytest.mark.parametrize(
    "statement",
    [
        "DELETE FROM main.policies.docs",
        "SELECT 1; DROP TABLE t",
        "CREATE TABLE x AS SELECT 1",
        "WITH a AS (SELECT 1) INSERT INTO t SELECT * FROM a",
        "UPDATE t SET a = 1",
        "SET spark.x = 1",
    ],
)
def test_writes_ddl_and_multi_statements_are_refused_before_any_http_call(statement):
    fake = _fake()
    for config in (pat_blob(), pat_blob(warehouse_id=None)):
        result = _call(fake, "DATABRICKS_RUN_SELECT", {"statement": statement}, config)
        assert not result.successful and result.error_code == "forbidden"
        assert "only one SELECT" in (result.error or "")
    assert fake.requests == []


def test_select_without_a_warehouse_is_not_configured():
    fake = _fake()
    result = _call(
        fake,
        "DATABRICKS_RUN_SELECT",
        {"statement": "SELECT 1"},
        pat_blob(warehouse_id=None),
    )
    assert not result.successful and result.error_code == "not_connected"
    assert "warehouse_id" in (result.error or "") and fake.requests == []


def test_a_running_statement_is_polled_until_it_succeeds():
    fake = _fake()
    fake.next_statement = Statement(
        ["PENDING", "RUNNING", "SUCCEEDED"],
        rows=[[1]],
        columns=[{"name": "x", "type_name": "INT"}],
    )
    clock = Clock()
    result = _call(
        fake, "DATABRICKS_RUN_SELECT", {"statement": "SELECT 1 AS x"}, clock=clock
    )
    assert result.successful and result.data["rows"] == [[1]]
    polls = [
        r
        for r in fake.requests
        if r["method"] == "GET" and "/sql/statements/" in r["url"]
    ]
    assert len(polls) == 2 and fake.cancelled == []


def test_the_statement_deadline_is_honoured_and_the_statement_cancelled():
    fake = _fake()
    fake.next_statement = Statement(["RUNNING"])  # never finishes
    clock = Clock()
    started = clock.now
    result = _call(
        fake,
        "DATABRICKS_RUN_SELECT",
        {"statement": "SELECT 1", "timeout_seconds": 10},
        clock=clock,
    )
    assert not result.successful and result.error_code == "timeout"
    assert "cancelled" in (result.error or "")
    assert fake.cancelled == ["stmt-1"]
    # Polling stops with the cancel's reserve left, so the cancel fits too.
    assert 8 <= clock.now - started <= 10
    body = fake.sql_submissions()[0]["json"]
    assert (body["wait_timeout"], body["on_wait_timeout"]) == ("5s", "CANCEL")


def test_a_failed_statement_is_reported_and_a_permission_error_is_forbidden():
    fake = _fake()
    fake.next_statement = Statement(
        ["FAILED"],
        error={
            "error_code": "PERMISSION_DENIED",
            "message": "User does not have SELECT on table",
        },
    )
    result = _call(fake, "DATABRICKS_RUN_SELECT", {"statement": "SELECT * FROM t"})
    assert not result.successful and result.error_code == "forbidden"
    assert "does not have SELECT" in (result.error or "")


def test_a_truncated_result_says_so():
    fake = _fake()
    fake.next_statement = Statement(
        ["SUCCEEDED"], rows=[[i] for i in range(3)], total=500
    )
    result = _call(
        fake, "DATABRICKS_RUN_SELECT", {"statement": "SELECT * FROM t", "row_limit": 3}
    )
    assert result.data["truncated"] is True and result.data["row_count"] == 3


# -- catalog ---------------------------------------------------------------------------


def test_catalog_schema_and_table_listings():
    fake = _fake()
    fake.catalog["catalogs"] = [
        {"name": "main", "comment": "c", "owner": "o", "secret_field": "x"}
    ]
    fake.catalog["schemas"] = [
        {"name": "policies", "full_name": "main.policies", "catalog_name": "main"}
    ]
    fake.catalog["tables"] = [
        {
            "name": "docs",
            "full_name": "main.policies.docs",
            "table_type": "MANAGED",
            "columns": [{"name": "title", "type_text": "string"}],
            "updated_at": 1700000000000,
        }
    ]
    assert _call(fake, "DATABRICKS_LIST_CATALOGS", {}).data["catalogs"] == [
        {"name": "main", "comment": "c", "owner": "o", "catalog_type": None}
    ]
    schemas = _call(fake, "DATABRICKS_LIST_SCHEMAS", {"catalog_name": "main"}).data[
        "schemas"
    ]
    assert schemas[0]["full_name"] == "main.policies"
    tables = _call(
        fake,
        "DATABRICKS_LIST_TABLES",
        {"catalog_name": "main", "schema_name": "policies"},
    ).data
    assert tables["tables"][0]["columns"] == [{"name": "title", "type": "string"}]
    last = fake.api_calls()[-1]
    assert (
        last["params"]["catalog_name"] == "main"
        and last["params"]["schema_name"] == "policies"
    )
    bad = _call(fake, "DATABRICKS_LIST_SCHEMAS", {"catalog_name": "../x"})
    assert not bad.successful and bad.error_code == "invalid_arguments"


# -- failures and discovery --------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "headers", "code"),
    [
        (403, {}, "forbidden"),
        (404, {}, "not_found"),
        (429, {"Retry-After": "9"}, "rate_limited"),
        (500, {}, "upstream_error"),
        (307, {}, "upstream_error"),
    ],
)
def test_databricks_failures_map_to_canonical_codes(status, headers, code):
    fake = _fake()
    fake.fail_with = (status, {"error_code": "X", "message": f"boom {PAT}"}, headers)
    result = _call(fake, "DATABRICKS_LIST_FILES", {"path": VOL})
    assert not result.successful and result.error_code == code
    assert PAT not in (result.error or "")
    if code == "rate_limited":
        assert result.retry_after == 9.0


def test_the_tools_are_read_only_and_the_file_read_is_primary():
    assert all(t.read_only for t in TOOL_SPECS)
    assert [t.slug for t in TOOL_SPECS if PRIMARY_TAG in t.tags] == [READ_FILE.slug]
    assert all(t.input_schema["additionalProperties"] is False for t in TOOL_SPECS)


def test_the_toolkit_is_found_by_search_scored_and_search_connectors():
    from aisquare.pipe.core.search import search_connectors, search_scored

    assert (
        search_connectors("databricks unity catalog volume", limit=3)[0][0]
        == "databricks"
    )
    files = search_scored(
        "read the policy pdf from our databricks volume",
        connectors=["databricks"],
        limit=2,
    )
    assert files[0].tool.slug == "DATABRICKS_READ_FILE"
    sql = search_scored(
        "run a select query on the databricks sql warehouse",
        connectors=["databricks"],
        limit=1,
    )
    assert sql[0].tool.slug == "DATABRICKS_RUN_SELECT"


@pytest.mark.parametrize(
    ("error", "code"),
    [
        (requests.exceptions.ChunkedEncodingError("broken"), "upstream_error"),
        (requests.exceptions.ConnectionError("Read timed out."), "timeout"),
    ],
)
def test_a_download_that_breaks_off_is_a_tool_error(error, code):
    fake = _fake()
    fake.body_breaks = error
    result = _call(fake, "DATABRICKS_READ_FILE", {"path": f"{VOL}/travel.txt"})
    assert not result.successful and result.error_code == code
    assert "broke off" in (result.error or "")
