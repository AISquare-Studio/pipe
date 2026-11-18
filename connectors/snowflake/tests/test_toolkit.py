"""The toolkit: listing, describing, SELECT, the caps, server keys and errors."""

from __future__ import annotations

import json

import pytest
import requests

from aisquare_pipe_snowflake import SnowflakeToolkit
from aisquare_pipe_snowflake.toolkit import MAX_RESULT_BYTES, TOOL_SPECS
from tests.fakes import (
    HOST,
    Canned,
    FakeResponse,
    FakeSnowflake,
    FakeTable,
    blob,
    col,
)

DOCS = [
    col("ID", "fixed", scale=0, precision=38),
    col("TITLE"),
    col("BODY"),
    col("UPDATED", "timestamp_ltz", scale=3),
]


def _fake() -> FakeSnowflake:
    fake = FakeSnowflake()
    fake.add_table(
        "POLICIES.PUBLIC.DOCS",
        FakeTable(
            DOCS, [[1, "Leave", "text a", "1763373600.000"], [2, "Travel", "b", None]]
        ),
    )
    fake.add_table(
        "POLICIES.PUBLIC.DOC_INDEX", FakeTable([col("ID", "fixed", scale=0)])
    )
    fake.add_table("POLICIES.HR.HANDBOOK", FakeTable([col("ID", "fixed", scale=0)]))
    fake.add_table("ARCHIVE.OLD.DOCS", FakeTable([col("ID", "fixed", scale=0)]))
    return fake


def _call(fake: FakeSnowflake, slug: str, arguments: dict, config: dict | None = None):
    return SnowflakeToolkit(session=fake).call_tool(slug, arguments, config or blob())


# -- listing -------------------------------------------------------------------------


def test_show_databases_lists_and_pages_with_from_name():
    fake = _fake()
    first = _call(fake, "SNOWFLAKE_SHOW_DATABASES", {"limit": 1})
    assert first.successful, first.error
    assert [d["name"] for d in first.data["databases"]] == ["ARCHIVE"]
    assert first.data["has_more"] is True and first.data["next_from_name"] == "POLICIES"
    second = _call(
        fake, "SNOWFLAKE_SHOW_DATABASES", {"limit": 1, "from_name": "POLICIES"}
    )
    assert [d["name"] for d in second.data["databases"]] == ["POLICIES"]
    assert second.data["has_more"] is False and second.data["next_from_name"] is None
    assert fake.statements() == [
        "SHOW DATABASES LIMIT 2",
        "SHOW DATABASES LIMIT 2 FROM 'POLICIES'",
    ]


def test_show_schemas_defaults_to_the_configured_database():
    fake = _fake()
    result = _call(fake, "SNOWFLAKE_SHOW_SCHEMAS", {})
    assert result.successful, result.error
    assert result.data["database"] == "POLICIES"
    assert [s["name"] for s in result.data["schemas"]] == ["HR", "PUBLIC"]
    other = _call(fake, "SNOWFLAKE_SHOW_SCHEMAS", {"database": "archive"})
    assert [s["name"] for s in other.data["schemas"]] == ["OLD"]
    everywhere = _call(
        fake, "SNOWFLAKE_SHOW_SCHEMAS", {}, blob(database=None, schema=None)
    )
    assert len(everywhere.data["schemas"]) == 3
    assert fake.statements()[-1] == "SHOW SCHEMAS IN ACCOUNT LIMIT 101"


def test_show_tables_in_the_configured_schema_with_filters():
    fake = _fake()
    result = _call(fake, "SNOWFLAKE_SHOW_TABLES", {"like_pattern": "doc%"})
    assert result.successful, result.error
    assert result.data["scope"] == "POLICIES.PUBLIC"
    assert [t["name"] for t in result.data["tables"]] == ["DOCS", "DOC_INDEX"]
    first = result.data["tables"][0]
    assert first == {
        "name": "DOCS",
        "database_name": "POLICIES",
        "schema_name": "PUBLIC",
        "kind": "TABLE",
        "created_on": "2025-11-01T12:26:40.000Z",
        "rows": 2,
        "bytes": 2048,
        "owner": "SYSADMIN",
        "comment": None,
    }  # automatic_clustering and other noise columns are left out
    whole_db = _call(fake, "SNOWFLAKE_SHOW_TABLES", {"database": "POLICIES"})
    assert whole_db.data["scope"] == "POLICIES"
    assert len(whole_db.data["tables"]) == 3
    hr = _call(fake, "SNOWFLAKE_SHOW_TABLES", {"schema": "hr", "starts_with": "HAND"})
    assert [t["name"] for t in hr.data["tables"]] == ["HANDBOOK"]
    assert fake.statements()[-1] == (
        'SHOW TABLES IN SCHEMA "POLICIES"."HR" STARTS WITH \'HAND\' LIMIT 101'
    )


def test_terse_and_history_shape_the_show_statement():
    fake = _fake()
    result = _call(fake, "SNOWFLAKE_SHOW_TABLES", {"terse": True, "history": True})
    assert result.successful, result.error
    assert set(result.data["tables"][0]) == {
        "created_on",
        "name",
        "kind",
        "database_name",
        "schema_name",
    }
    assert fake.statements() == [
        'SHOW TERSE TABLES HISTORY IN SCHEMA "POLICIES"."PUBLIC" LIMIT 101'
    ]


def test_every_caller_value_in_a_show_is_one_quoted_literal_or_identifier():
    fake = _fake()
    result = _call(
        fake,
        "SNOWFLAKE_SHOW_TABLES",
        {
            "database": '"we""ird db"',
            "like_pattern": "x' OR 1=1; DROP TABLE t; --",
            "starts_with": "a\\'",
        },
    )
    assert (
        result.successful and result.data["tables"] == []
    )  # no such database: no rows
    assert result.data["scope"] == '"we""ird db"'
    (statement,) = fake.statements()
    assert statement == (
        "SHOW TABLES LIKE 'x'' OR 1=1; DROP TABLE t; --' IN DATABASE \"we\"\"ird db\" "
        "STARTS WITH 'a\\\\''' LIMIT 101"
    )


# -- describe ------------------------------------------------------------------------


def test_describe_table_returns_columns_and_last_altered():
    fake = _fake()
    result = _call(fake, "SNOWFLAKE_DESCRIBE_TABLE", {"table": "docs"})
    assert result.successful, result.error
    data = result.data
    assert data["table"] == "POLICIES.PUBLIC.DOCS"
    assert data["kind"] == "BASE TABLE"
    assert data["row_count"] == 2
    assert data["last_altered"] == "2025-11-17T10:00:00.000Z"
    assert [c["name"] for c in data["columns"]] == ["ID", "TITLE", "BODY", "UPDATED"]
    assert data["columns"][0] == {
        "name": "ID",
        "type": "NUMBER",
        "nullable": True,
        "precision": 38,
        "scale": 0,
    }
    tables, columns = fake.submits()
    # Names travel as bind variables, never in the SQL text.
    assert "DOCS" not in tables["statement"] and "PUBLIC" not in tables["statement"]
    assert tables["bindings"] == {
        "1": {"type": "TEXT", "value": "PUBLIC"},
        "2": {"type": "TEXT", "value": "DOCS"},
    }
    assert columns["bindings"] == tables["bindings"]


def test_describe_table_takes_qualified_and_quoted_names():
    fake = _fake()
    fake.add_table("POLICIES.PUBLIC.Mixed Case", FakeTable([col("A")]))
    assert _call(
        fake, "SNOWFLAKE_DESCRIBE_TABLE", {"table": "archive.old.docs"}
    ).successful
    quoted = _call(fake, "SNOWFLAKE_DESCRIBE_TABLE", {"table": 'public."Mixed Case"'})
    assert quoted.successful, quoted.error
    assert quoted.data["table"] == 'POLICIES.PUBLIC."Mixed Case"'


def test_a_table_the_role_cannot_see_is_not_found():
    fake = _fake()
    result = _call(fake, "SNOWFLAKE_DESCRIBE_TABLE", {"table": "secret_table"})
    assert not result.successful and result.error_code == "not_found"
    assert "AISQUARE_READONLY" in result.error
    missing_db = _call(fake, "SNOWFLAKE_DESCRIBE_TABLE", {"table": "nope.s.t"})
    assert missing_db.error_code == "not_found", missing_db.error


# -- SELECT --------------------------------------------------------------------------


def test_run_select_returns_typed_rows_keyed_by_column():
    fake = _fake()
    fake.add(
        "SELECT * FROM typed",
        Canned(
            [
                col("N", "fixed", scale=0),
                col("AMOUNT", "fixed", scale=2),
                col("RATIO", "real"),
                col("OK", "boolean"),
                col("DAY", "date"),
                col("AT", "time", scale=3),
                col("NTZ", "timestamp_ntz", scale=6),
                col("LTZ", "timestamp_ltz", scale=3),
                col("TZ", "timestamp_tz", scale=0),
                col("DOC", "variant"),
                col("TAGS", "array"),
                col("RAW", "binary"),
                col("NOTHING"),
            ],
            [
                [
                    "42",
                    "1234.50",
                    "0.25",
                    "true",
                    "20409",
                    "45296.789",
                    "1763373600.123456",
                    "1763373600.5",
                    "1763373600 1080",
                    '{"a": [1, 2]}',
                    '["x"]',
                    "DEADBEEF",
                    None,
                ]
            ],
        ),
    )
    result = _call(fake, "SNOWFLAKE_RUN_SELECT", {"statement": "SELECT * FROM typed"})
    assert result.successful, result.error
    (row,) = result.data["rows"]
    assert row == {
        "N": 42,
        "AMOUNT": "1234.50",
        "RATIO": 0.25,
        "OK": True,
        "DAY": "2025-11-17",
        "AT": "12:34:56.789",
        "NTZ": "2025-11-17T10:00:00.123456",
        "LTZ": "2025-11-17T10:00:00.500Z",
        "TZ": "2025-11-17T04:00:00-06:00",
        "DOC": {"a": [1, 2]},
        "TAGS": ["x"],
        "RAW": "DEADBEEF",
        "NOTHING": None,
    }
    assert result.data["columns"][1] == {
        "name": "AMOUNT",
        "type": "FIXED",
        "nullable": True,
        "scale": 2,
    }
    assert result.data["row_count"] == 1 and result.data["truncated"] is None
    assert result.data["query_id"].startswith("01bc")


def test_repeated_column_names_get_unique_keys():
    fake = _fake()
    fake.add(
        "SELECT 1 a, 2 a", Canned([col("A", "fixed"), col("A", "fixed")], [["1", "2"]])
    )
    result = _call(fake, "SNOWFLAKE_RUN_SELECT", {"statement": "SELECT 1 a, 2 a"})
    assert result.data["rows"] == [{"A": 1, "A_2": 2}]
    assert result.data["columns"][1]["source_name"] == "A"


def test_every_statement_runs_alone_as_the_servers_role_and_warehouse():
    fake = _fake()
    fake.add("SELECT 1", Canned([col("X", "fixed")], [["1"]]))
    result = _call(fake, "SNOWFLAKE_RUN_SELECT", {"statement": "SELECT 1;"})
    assert result.successful, result.error
    (body,) = fake.submits()
    assert body["statement"] == "SELECT 1"
    assert body["role"] == "AISQUARE_READONLY"
    assert body["warehouse"] == "AISQUARE_WH"
    assert body["database"] == "POLICIES" and body["schema"] == "PUBLIC"
    assert body["parameters"] == {
        "MULTI_STATEMENT_COUNT": "1",
        "ROWS_PER_RESULTSET": "101",
        "QUERY_TAG": "aisquare-pipe",
    }
    assert body["timeout"] == 60
    assert body["resultSetMetaData"] == {"format": "jsonv2"}
    submit = fake.requests[0]
    assert submit["params"]["async"] == "true" and submit["params"]["requestId"]
    assert submit["headers"]["X-Snowflake-Authorization-Token-Type"] == "KEYPAIR_JWT"
    assert submit["allow_redirects"] is False


def test_database_and_schema_name_set_the_statements_context():
    fake = _fake()
    fake.add("SELECT * FROM docs", Canned([col("ID", "fixed")], [["1"]]))
    result = _call(
        fake,
        "SNOWFLAKE_RUN_SELECT",
        {
            "statement": "SELECT * FROM docs",
            "database": "archive",
            "schema_name": '"Old"',
        },
    )
    assert result.successful, result.error
    (body,) = fake.submits()
    assert body["database"] == "ARCHIVE" and body["schema"] == "Old"


def test_run_select_polls_until_the_statement_finishes(monkeypatch):
    sleeps: list[float] = []
    monkeypatch.setattr("time.sleep", sleeps.append)
    fake = _fake()
    fake.add("SELECT 1", Canned([col("X", "fixed")], [["1"]], polls=3))
    result = _call(fake, "SNOWFLAKE_RUN_SELECT", {"statement": "SELECT 1"})
    assert result.successful, result.error
    polls = [r for r in fake.api_calls() if r["method"] == "GET"]
    assert len(polls) == 4
    assert sleeps == [0.25, 0.5, 1.0]  # the first status read goes out at once


def test_run_select_reads_every_partition():
    fake = _fake()
    fake.add(
        "SELECT * FROM big",
        Canned([col("N", "fixed")], [[str(i)] for i in range(10)], partition_rows=3),
    )
    result = _call(fake, "SNOWFLAKE_RUN_SELECT", {"statement": "SELECT * FROM big"})
    assert result.successful, result.error
    assert [r["N"] for r in result.data["rows"]] == list(range(10))
    partitions = [
        r["params"].get("partition") for r in fake.api_calls() if r["method"] == "GET"
    ]
    assert partitions == [None, 1, 2, 3]


def test_max_rows_cuts_the_result_and_says_so():
    fake = _fake()
    fake.add(
        "SELECT * FROM big",
        Canned([col("N", "fixed")], [[str(i)] for i in range(50)], partition_rows=4),
    )
    result = _call(
        fake, "SNOWFLAKE_RUN_SELECT", {"statement": "SELECT * FROM big", "max_rows": 5}
    )
    assert result.successful, result.error
    assert [r["N"] for r in result.data["rows"]] == [0, 1, 2, 3, 4]
    assert result.data["truncated"] == "max_rows"
    assert fake.submits()[0]["parameters"]["ROWS_PER_RESULTSET"] == "6"
    # Only the partitions holding the first max_rows + 1 rows were read.
    assert len([r for r in fake.api_calls() if r["method"] == "GET"]) == 2


def test_max_rows_holds_when_snowflake_sends_more_rows_than_asked():
    fake = _fake()
    canned = fake.add(
        "SELECT * FROM big", Canned([col("N", "fixed")], [[str(i)] for i in range(20)])
    )
    fake.override("submit")  # nothing queued: the normal answer
    # A server that ignores ROWS_PER_RESULTSET.
    original = fake._submit

    def ignoring(body, params):
        body = {**body, "parameters": {**body["parameters"], "ROWS_PER_RESULTSET": "0"}}
        return original(body, params)

    fake._submit = ignoring  # type: ignore[method-assign]
    result = _call(
        fake, "SNOWFLAKE_RUN_SELECT", {"statement": "SELECT * FROM big", "max_rows": 3}
    )
    assert len(canned.rows) == 20
    assert [r["N"] for r in result.data["rows"]] == [0, 1, 2]
    assert result.data["truncated"] == "max_rows"


def test_the_result_byte_cap_cuts_the_result_and_says_so():
    fake = _fake()
    big = "x" * (1024 * 1024)
    fake.add("SELECT * FROM blobs", Canned([col("B")], [[big] for _ in range(6)]))
    result = _call(fake, "SNOWFLAKE_RUN_SELECT", {"statement": "SELECT * FROM blobs"})
    assert result.successful, result.error
    assert result.data["truncated"] == "max_result_bytes"
    assert result.data["row_count"] == 3
    assert len(json.dumps(result.data["rows"])) <= MAX_RESULT_BYTES


def test_a_partition_past_the_byte_cap_is_never_downloaded():
    fake = _fake()
    big = "y" * (1024 * 1024)
    fake.add(
        "SELECT * FROM blobs",
        Canned([col("B")], [[big] for _ in range(8)], partition_rows=2),
    )
    result = _call(fake, "SNOWFLAKE_RUN_SELECT", {"statement": "SELECT * FROM blobs"})
    assert result.data["truncated"] == "max_result_bytes"
    partitions = [
        r["params"].get("partition") for r in fake.api_calls() if r["method"] == "GET"
    ]
    assert partitions == [None]  # partition 1 would pass the cap: not read


# -- server keys and bad arguments ---------------------------------------------------


@pytest.mark.parametrize(
    "key",
    [
        "role",
        "warehouse",
        "account",
        "user",
        "private_key_pem",
        "private_key_passphrase",
        "client_secret",
        "token_url",
        "auth",
        "host",
        "url",
    ],
)
def test_a_caller_cannot_override_a_server_key(key):
    fake = _fake()
    for slug, arguments in [
        ("SNOWFLAKE_RUN_SELECT", {"statement": "SELECT 1"}),
        ("SNOWFLAKE_SHOW_TABLES", {}),
        ("SNOWFLAKE_SHOW_SCHEMAS", {}),
        ("SNOWFLAKE_SHOW_DATABASES", {}),
        ("SNOWFLAKE_DESCRIBE_TABLE", {"table": "docs"}),
    ]:
        result = _call(fake, slug, {**arguments, key: "ACCOUNTADMIN"})
        assert not result.successful and result.error_code == "invalid_arguments", (
            slug,
            result.error,
        )
    assert fake.requests == []


def test_without_credentials_every_tool_fails_closed_without_a_request():
    fake = _fake()
    for spec in TOOL_SPECS:
        arguments = (
            {"statement": "SELECT 1"} if spec.slug.endswith("RUN_SELECT") else {}
        )
        if spec.slug.endswith("DESCRIBE_TABLE"):
            arguments = {"table": "docs"}
        for config in (None, {}, {"timeout_seconds": 10}):
            result = SnowflakeToolkit(session=fake).call_tool(
                spec.slug, arguments, config
            )
            assert not result.successful and result.error_code == "not_connected"
            assert result.error.startswith("Snowflake is not configured")
    assert fake.requests == []


@pytest.mark.parametrize(
    ("slug", "arguments"),
    [
        ("SNOWFLAKE_DESCRIBE_TABLE", {"table": "a.b.c.d"}),
        ("SNOWFLAKE_DESCRIBE_TABLE", {"table": "bad name"}),
        ("SNOWFLAKE_DESCRIBE_TABLE", {"table": '"unterminated'}),
        ("SNOWFLAKE_SHOW_TABLES", {"schema": "a.b.c"}),
        ("SNOWFLAKE_SHOW_SCHEMAS", {"database": "a.b"}),
        ("SNOWFLAKE_SHOW_DATABASES", {"limit": 0}),
        ("SNOWFLAKE_SHOW_DATABASES", {"limit": 1001}),
        ("SNOWFLAKE_SHOW_DATABASES", {"like_pattern": "x\n"}),
        ("SNOWFLAKE_SHOW_DATABASES", {"like_pattern": "x" * 256}),
        ("SNOWFLAKE_RUN_SELECT", {"statement": "SELECT 1", "max_rows": 0}),
        ("SNOWFLAKE_RUN_SELECT", {"statement": "SELECT 1", "max_rows": 5001}),
        ("SNOWFLAKE_RUN_SELECT", {"statement": "SELECT 1", "timeout": 601}),
        ("SNOWFLAKE_RUN_SELECT", {"statement": "SELECT 1", "timeout": True}),
        ("SNOWFLAKE_RUN_SELECT", {"statement": "SELECT 1", "database": "a.b"}),
        ("SNOWFLAKE_RUN_SELECT", {"statement": "SELECT 1", "schema_name": "a b"}),
        ("SNOWFLAKE_RUN_SELECT", {}),
    ],
)
def test_bad_references_and_arguments_are_refused_before_any_request(slug, arguments):
    fake = _fake()
    result = _call(fake, slug, arguments)
    assert not result.successful and result.error_code == "invalid_arguments", (
        result.error
    )
    assert fake.requests == []


def test_a_name_without_its_database_needs_a_configured_default():
    fake = _fake()
    result = _call(
        fake,
        "SNOWFLAKE_DESCRIBE_TABLE",
        {"table": "docs"},
        blob(database=None, schema=None),
    )
    assert not result.successful and result.error_code == "invalid_arguments"
    assert "DB.SCHEMA.TABLE" in result.error
    assert fake.requests == []


# -- failures ------------------------------------------------------------------------


def _failing(status: int, code: str = "", state: str = "", message: str = "it failed"):
    return Canned(error=(status, code, state, message))


@pytest.mark.parametrize(
    ("canned", "error_code", "hint"),
    [
        (
            _failing(422, "001003", "42000", "SQL compilation error: syntax error"),
            "invalid_arguments",
            None,
        ),
        (
            _failing(
                422, "002003", "42S02", "Object 'T' does not exist or not authorized."
            ),
            "not_found",
            "has no grant",
        ),
        (
            _failing(422, "003001", "42501", "Insufficient privileges"),
            "forbidden",
            "lacks a grant",
        ),
        (
            _failing(422, "000606", "57P03", "No active warehouse selected"),
            "forbidden",
            "warehouse AISQUARE_WH",
        ),
        (
            _failing(422, "390189", "08004", "Role 'X' is not granted to this user"),
            "forbidden",
            "not granted",
        ),
        (
            _failing(422, "000630", "57014", "Statement reached its statement timeout"),
            "timeout",
            None,
        ),
        (_failing(422, "000604", "57014", "SQL execution canceled"), "timeout", None),
        (_failing(408, "", "", "timeout"), "timeout", None),
        (_failing(403, "390186", "", "forbidden"), "forbidden", None),
        (_failing(500, "", "", "internal"), "upstream_error", None),
        (_failing(503, "", "", "unavailable"), "upstream_error", None),
        (_failing(504, "", "", "gateway"), "timeout", None),
    ],
)
def test_snowflake_failures_map_to_canonical_codes(canned, error_code, hint):
    fake = _fake()
    fake.add("SELECT 1", canned)
    result = _call(fake, "SNOWFLAKE_RUN_SELECT", {"statement": "SELECT 1"})
    assert not result.successful
    assert result.error_code == error_code, result.error
    if hint:
        assert hint in result.error


def test_a_rate_limited_submit_carries_retry_after():
    fake = _fake()
    fake.override(
        "submit",
        FakeResponse(429, {"message": "slow down"}, headers={"Retry-After": "12"}),
    )
    result = _call(fake, "SNOWFLAKE_RUN_SELECT", {"statement": "SELECT 1"})
    assert result.error_code == "rate_limited" and result.retry_after == 12


def test_a_timeout_is_a_timeout():
    class Slow(FakeSnowflake):
        def request(self, method, url, **kwargs):
            raise requests.ReadTimeout("read timed out")

    result = _call(Slow(), "SNOWFLAKE_SHOW_DATABASES", {})
    assert not result.successful and result.error_code == "timeout", result.error


def test_an_error_body_that_echoes_the_bearer_is_redacted():
    fake = _fake()
    seen: list[str] = []

    def capture(url: str) -> None:
        if fake.requests and "Authorization" in fake.requests[-1]["headers"]:
            seen.append(fake.requests[-1]["headers"]["Authorization"][len("Bearer ") :])

    fake.on_request = capture

    class Echo(FakeSnowflake):
        pass

    fake.override("poll", FakeResponse(500, {"message": "placeholder"}))
    fake.add("SELECT 1", Canned([col("X", "fixed")], [["1"]]))
    # Answer the poll with an error that quotes the bearer just sent.
    original = fake._pop

    def echoing(kind):
        answer = original(kind)
        if answer is not None and kind == "poll":
            return FakeResponse(500, {"message": f"bad token {seen[-1]} here"})
        return answer

    fake._pop = echoing  # type: ignore[method-assign]
    result = _call(fake, "SNOWFLAKE_RUN_SELECT", {"statement": "SELECT 1"})
    assert not result.successful and result.error_code == "upstream_error"
    assert seen and seen[-1] not in result.error
    assert "[redacted]" in result.error


def test_a_redirect_is_not_followed():
    fake = _fake()
    fake.override(
        "submit", FakeResponse(302, headers={"Location": "https://evil.example.com/"})
    )
    result = _call(fake, "SNOWFLAKE_RUN_SELECT", {"statement": "SELECT 1"})
    assert not result.successful and result.error_code == "upstream_error"
    assert "redirect" in result.error
    assert all(r["url"].startswith(HOST) for r in fake.requests)


def test_every_request_goes_to_the_accounts_own_host():
    fake = _fake()
    fake.add(
        "SELECT 1", Canned([col("X", "fixed")], [["1"]], polls=1, partition_rows=1)
    )
    for slug, arguments in [
        ("SNOWFLAKE_RUN_SELECT", {"statement": "SELECT 1"}),
        ("SNOWFLAKE_SHOW_TABLES", {}),
        ("SNOWFLAKE_DESCRIBE_TABLE", {"table": "docs"}),
    ]:
        assert _call(fake, slug, arguments).successful
    assert fake.requests and all(
        r["url"].startswith(
            "https://acme-analytics.snowflakecomputing.com/api/v2/statements"
        )
        for r in fake.requests
    )


# -- shape and search ----------------------------------------------------------------


def test_the_tools_are_read_only_strict_and_run_select_is_primary():
    from aisquare.pipe.core.search import PRIMARY_TAG

    toolkit = SnowflakeToolkit()
    tools = toolkit.tools()
    assert [t.slug for t in tools] == [
        "SNOWFLAKE_RUN_SELECT",
        "SNOWFLAKE_DESCRIBE_TABLE",
        "SNOWFLAKE_SHOW_TABLES",
        "SNOWFLAKE_SHOW_SCHEMAS",
        "SNOWFLAKE_SHOW_DATABASES",
    ]
    for tool in tools:
        assert tool.read_only and not tool.destructive and tool.risk == "read"
        assert tool.input_schema["additionalProperties"] is False
        assert (PRIMARY_TAG in tool.tags) == (tool.slug == "SNOWFLAKE_RUN_SELECT")
    assert toolkit.oauth is None and toolkit.auth_type.value == "custom"


def test_composio_era_slugs_and_arguments_still_work():
    fake = _fake()
    for slug, arguments in [
        (
            "SNOWFLAKE_SHOW_DATABASES",
            {
                "like_pattern": "%",
                "starts_with": "P",
                "limit": 5,
                "terse": False,
                "history": False,
                "timeout": 20,
            },
        ),
        (
            "SNOWFLAKE_SHOW_SCHEMAS",
            {
                "database": "POLICIES",
                "like_pattern": "%",
                "limit": 5,
                "from_name": "A",
                "timeout": 20,
            },
        ),
        (
            "SNOWFLAKE_SHOW_TABLES",
            {
                "database": "POLICIES",
                "schema": "PUBLIC",
                "limit": 5,
                "like_pattern": "%",
                "timeout": 20,
            },
        ),
    ]:
        result = _call(fake, slug, arguments)
        assert result.successful, (slug, result.error)
    assert all(b["timeout"] == 20 for b in fake.submits())


# Reviewer seq 13859 m3: Composio's SHOW_SCHEMAS and SHOW_TABLES take in_scope
# (pipe-as-service composio/guard.py); the native tools answered "unknown
# argument(s) 'in_scope'".
@pytest.mark.parametrize(
    ("slug", "arguments", "statement", "names"),
    [
        (
            "SNOWFLAKE_SHOW_SCHEMAS",
            {"in_scope": "ACCOUNT"},
            "SHOW SCHEMAS IN ACCOUNT LIMIT 101",
            ["OLD", "HR", "PUBLIC"],
        ),
        (
            "SNOWFLAKE_SHOW_SCHEMAS",
            {"in_scope": "database"},
            'SHOW SCHEMAS IN DATABASE "POLICIES" LIMIT 101',
            ["HR", "PUBLIC"],
        ),
        (
            "SNOWFLAKE_SHOW_SCHEMAS",
            {"in_scope": "DATABASE", "database": "archive"},
            'SHOW SCHEMAS IN DATABASE "ARCHIVE" LIMIT 101',
            ["OLD"],
        ),
        (
            "SNOWFLAKE_SHOW_TABLES",
            {"in_scope": "ACCOUNT"},
            "SHOW TABLES IN ACCOUNT LIMIT 101",
            ["DOCS", "DOCS", "DOC_INDEX", "HANDBOOK"],
        ),
        (
            "SNOWFLAKE_SHOW_TABLES",
            {"in_scope": "DATABASE"},
            'SHOW TABLES IN DATABASE "POLICIES" LIMIT 101',
            ["DOCS", "DOC_INDEX", "HANDBOOK"],
        ),
        (
            "SNOWFLAKE_SHOW_TABLES",
            {"in_scope": " Database ", "database": "ARCHIVE"},
            'SHOW TABLES IN DATABASE "ARCHIVE" LIMIT 101',
            ["DOCS"],
        ),
        (
            "SNOWFLAKE_SHOW_TABLES",
            {"in_scope": "SCHEMA"},
            'SHOW TABLES IN SCHEMA "POLICIES"."PUBLIC" LIMIT 101',
            ["DOCS", "DOC_INDEX"],
        ),
        (
            "SNOWFLAKE_SHOW_TABLES",
            {"in_scope": "schema", "schema": "hr"},
            'SHOW TABLES IN SCHEMA "POLICIES"."HR" LIMIT 101',
            ["HANDBOOK"],
        ),
        (
            "SNOWFLAKE_SHOW_TABLES",
            {"in_scope": "SCHEMA", "schema": "ARCHIVE.OLD"},
            'SHOW TABLES IN SCHEMA "ARCHIVE"."OLD" LIMIT 101',
            ["DOCS"],
        ),
        (
            # With a database but no schema: the configured schema in it.
            "SNOWFLAKE_SHOW_TABLES",
            {"in_scope": "SCHEMA", "database": "archive"},
            'SHOW TABLES IN SCHEMA "ARCHIVE"."PUBLIC" LIMIT 101',
            [],
        ),
    ],
)
def test_composio_in_scope_maps_to_the_scope_the_tool_builds(
    slug, arguments, statement, names
):
    fake = _fake()
    result = _call(fake, slug, arguments)
    assert result.successful, result.error
    assert fake.statements() == [statement]
    items = result.data["schemas" if slug.endswith("SCHEMAS") else "tables"]
    assert sorted(i["name"] for i in items) == sorted(names)


@pytest.mark.parametrize(
    ("slug", "arguments", "config", "why"),
    [
        ("SNOWFLAKE_SHOW_SCHEMAS", {"in_scope": "SCHEMA"}, None, "ACCOUNT or DATABASE"),
        (
            "SNOWFLAKE_SHOW_TABLES",
            {"in_scope": "TABLE"},
            None,
            "ACCOUNT or DATABASE or SCHEMA",
        ),
        ("SNOWFLAKE_SHOW_TABLES", {"in_scope": 1}, None, "'in_scope' must be string"),
        (
            "SNOWFLAKE_SHOW_SCHEMAS",
            {"in_scope": "ACCOUNT", "database": "POLICIES"},
            None,
            "leave database out",
        ),
        (
            "SNOWFLAKE_SHOW_SCHEMAS",
            {"in_scope": "DATABASE"},
            blob(database=None, schema=None),
            "needs a database",
        ),
        (
            "SNOWFLAKE_SHOW_TABLES",
            {"in_scope": "ACCOUNT", "schema": "PUBLIC"},
            None,
            "leave database and schema out",
        ),
        (
            "SNOWFLAKE_SHOW_TABLES",
            {"in_scope": "DATABASE", "schema": "PUBLIC"},
            None,
            "leave schema out",
        ),
        (
            "SNOWFLAKE_SHOW_TABLES",
            {"in_scope": "DATABASE"},
            blob(database=None, schema=None),
            "needs a database",
        ),
        (
            "SNOWFLAKE_SHOW_TABLES",
            {"in_scope": "SCHEMA"},
            blob(schema=None),
            "needs a schema",
        ),
    ],
)
def test_an_in_scope_the_arguments_contradict_is_refused_before_any_request(
    slug, arguments, config, why
):
    fake = _fake()
    result = _call(fake, slug, arguments, config)
    assert not result.successful
    assert result.error_code == "invalid_arguments"
    assert why in result.error
    assert fake.requests == []


def test_the_toolkit_is_found_by_search_scored_and_search_connectors():
    from aisquare.pipe.core.search import search_connectors, search_scored

    assert (
        search_connectors("snowflake data warehouse sql", limit=3)[0][0] == "snowflake"
    )
    hits = search_scored(
        "run a sql query on our snowflake warehouse", connectors=["snowflake"], limit=2
    )
    assert hits[0].tool.slug == "SNOWFLAKE_RUN_SELECT"
    hits = search_scored(
        "what columns does the policies table have", connectors=["snowflake"], limit=1
    )
    assert hits[0].tool.slug == "SNOWFLAKE_DESCRIBE_TABLE"
    hits = search_scored(
        "list the tables in a schema", connectors=["snowflake"], limit=1
    )
    assert hits[0].tool.slug == "SNOWFLAKE_SHOW_TABLES"
    top = search_scored("query the snowflake data warehouse with sql", limit=5)
    assert any(h.connector == "snowflake" for h in top)
