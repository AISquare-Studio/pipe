"""The ``redshift`` toolkit against a stubbed Data API (moto STS for the role)."""

from __future__ import annotations

import base64
import logging
from typing import Any

import pytest
from aisquare.pipe import (
    ERROR_FORBIDDEN,
    ERROR_INVALID_ARGUMENTS,
    ERROR_NOT_CONNECTED,
    ERROR_NOT_FOUND,
    ERROR_RATE_LIMITED,
    ERROR_TIMEOUT,
    ERROR_UPSTREAM_ERROR,
)

from aisquare_pipe_aws import credentials as aws_credentials
from aisquare_pipe_aws.redshift import dataapi, rows
from aisquare_pipe_aws.redshift.config import SERVER_KEYS
from aisquare_pipe_aws.redshift.toolkit import (
    DEFAULT_TIMEOUT_SECONDS,
    RedshiftToolkit,
)
from tests import redshift_fakes as fakes
from tests.conftest import AMBIENT_KEY, EXTERNAL_ID, ROLE_ARN, access_key_of
from tests.redshift_fakes import SECRET_ARN, STATEMENT_ID, blob

SQL = "SELECT id, title, active FROM policies.rules"

pytestmark = pytest.mark.usefixtures("aws", "clock")


def run(slug: str, args: dict[str, Any], config: dict[str, Any] | None = None) -> Any:
    return RedshiftToolkit().call_tool(slug, args, blob() if config is None else config)


# -- REDSHIFT_RUN_SELECT -------------------------------------------------------------


def test_run_select_reads_every_page(data_api, assume_calls):
    fakes.finished_query(
        data_api,
        SQL,
        [[fakes.record(1), fakes.record(2)], [fakes.record(3, active=False)]],
    )
    result = run("REDSHIFT_RUN_SELECT", {"sql": SQL + ";"})
    assert result.successful, result.error
    data = result.data
    assert [c["name"] for c in data["columns"]] == ["id", "title", "active"]
    assert data["columns"][0]["type"] == "int8"
    assert data["rows"] == [
        {"id": 1, "title": "rule 1", "active": True},
        {"id": 2, "title": "rule 2", "active": True},
        {"id": 3, "title": "rule 3", "active": False},
    ]
    assert data["row_count"] == 3 and data["total_rows"] == 3
    assert data["truncated"] is None
    assert data["statement_id"] == STATEMENT_ID
    assert data["duration_ms"] == 12
    assert data["result_sha256"] == rows.result_sha256(data["columns"], data["rows"])
    assert data_api.operations() == [
        "ExecuteStatement",
        "DescribeStatement",
        "DescribeStatement",
        "GetStatementResult",
        "GetStatementResult",
    ]
    data_api.assert_drained()
    assert len(assume_calls) == 1


def test_values_of_every_type(data_api):
    cols = [
        fakes.column("n", "int4"),
        fakes.column("x", "float8"),
        fakes.column("nan", "float8"),
        fakes.column("amount", "numeric"),
        fakes.column("ok", "bool"),
        fakes.column("note"),
        fakes.column("raw", "varbyte"),
        fakes.column("n", "int4"),
    ]
    record = [
        {"longValue": 7},
        {"doubleValue": 1.5},
        {"doubleValue": float("nan")},
        {"stringValue": "12345678901234567890.01"},
        {"booleanValue": False},
        {"isNull": True},
        {"blobValue": b"\x00\x01"},
        {"longValue": 8},
    ]
    fakes.finished_query(data_api, SQL, [[record]], columns=cols)
    result = run("REDSHIFT_RUN_SELECT", {"sql": SQL})
    assert result.successful, result.error
    assert result.data["rows"] == [
        {
            "n": 7,
            "x": 1.5,
            "nan": "nan",
            "amount": "12345678901234567890.01",
            "ok": False,
            "note": None,
            "raw": base64.b64encode(b"\x00\x01").decode(),
            "n_2": 8,
        }
    ]


def test_max_rows_stops_paging(data_api):
    fakes.submit(data_api, SQL)
    fakes.describe(data_api, "FINISHED", ResultRows=5000)
    fakes.result(
        data_api, [fakes.record(i) for i in range(3)], token_out="page-1", total=5000
    )
    result = run("REDSHIFT_RUN_SELECT", {"sql": SQL, "max_rows": 2})
    assert result.successful, result.error
    assert result.data["row_count"] == 2
    assert result.data["truncated"] == "max_rows"
    assert result.data["total_rows"] == 5000
    data_api.assert_drained()  # the second page was never asked for


def test_the_result_byte_cap(data_api, monkeypatch):
    from aisquare_pipe_aws.redshift import toolkit

    monkeypatch.setattr(toolkit, "MAX_RESULT_BYTES", 100)
    fakes.finished_query(
        data_api, SQL, [[fakes.record(i, title="x" * 40) for i in range(5)]]
    )
    result = run("REDSHIFT_RUN_SELECT", {"sql": SQL})
    assert result.successful, result.error
    assert result.data["truncated"] == "max_result_bytes"
    assert 0 < result.data["row_count"] < 5


def test_a_statement_without_a_result_set(data_api):
    fakes.submit(data_api, SQL)
    fakes.describe(data_api, "FINISHED", HasResultSet=False)
    result = run("REDSHIFT_RUN_SELECT", {"sql": SQL})
    assert result.successful
    assert result.data["rows"] == [] and result.data["columns"] == []
    assert "GetStatementResult" not in data_api.operations()


REFUSED_SQL = [
    "SELECT 1 --\r/* /* */; DROP TABLE ds.t; SELECT 1 /* */ -- */",
    "SELECT 1 --\r; DROP TABLE t",
    "SELECT 'a\\'; DROP TABLE t; --'",
    "SELECT $$a; DROP TABLE t; $$",
    "SELECT * INTO copy_of_rules FROM policies.rules",
    "UNLOAD ('SELECT * FROM t') TO 's3://bucket/out' IAM_ROLE default",
    "DELETE FROM policies.rules",
    "SELECT 1; SELECT 2",
    "SELECT pg_terminate_backend(1)",
]


@pytest.mark.parametrize("sql", REFUSED_SQL)
def test_refused_sql_reaches_no_aws_at_all(sql, data_api, assume_calls):
    result = run("REDSHIFT_RUN_SELECT", {"sql": sql})
    assert not result.successful
    assert result.error_code == ERROR_INVALID_ARGUMENTS
    assert "only one SELECT" in result.error
    assert assume_calls == []  # not even sts:AssumeRole
    assert data_api.calls == [] and data_api.clients == []


@pytest.mark.parametrize(
    "error, code",
    [
        ("ERROR: permission denied for schema hr", ERROR_FORBIDDEN),
        ('ERROR: relation "policies.nope" does not exist', ERROR_NOT_FOUND),
        ('ERROR: syntax error at or near "FORM"', ERROR_INVALID_ARGUMENTS),
    ],
)
def test_a_failed_statement_passes_on_the_database_error(data_api, caplog, error, code):
    caplog.set_level(logging.DEBUG, logger="aisquare")
    fakes.submit(data_api, SQL)
    fakes.describe(data_api, "FAILED", Error=error)
    result = run("REDSHIFT_RUN_SELECT", {"sql": SQL})
    assert result.error_code == code
    assert "failed in Redshift" in result.error and error in result.error
    ours = "\n".join(
        r.getMessage() for r in caplog.records if r.name.startswith("aisquare")
    )
    assert error not in ours and SQL not in ours


def test_an_aborted_statement(data_api):
    fakes.submit(data_api, SQL)
    fakes.describe(data_api, "ABORTED")
    result = run("REDSHIFT_RUN_SELECT", {"sql": SQL})
    assert result.error_code == ERROR_UPSTREAM_ERROR
    assert "aborted in Redshift" in result.error


# -- the deadline ---------------------------------------------------------------------


def test_polling_stops_at_the_hosts_budget_and_cancels(data_api, clock):
    fakes.submit(data_api, SQL)
    for _ in range(40):
        fakes.describe(data_api, "STARTED")
    data_api.add("cancel_statement", {"Status": True}, {"Id": STATEMENT_ID})
    started = clock.now
    result = run("REDSHIFT_RUN_SELECT", {"sql": SQL}, {**blob(), "timeout_seconds": 3})
    assert result.error_code == ERROR_TIMEOUT
    assert "within 3 s" in result.error and "cancelled" in result.error
    assert data_api.operations()[-1] == "CancelStatement"
    assert clock.now - started <= 3.0 + 1e-9
    # No DescribeStatement goes out once the deadline has passed.
    polls = [
        t
        for (op, _), t in zip(data_api.calls, data_api.times)
        if op == "DescribeStatement"
    ]
    assert polls and max(polls) < started + 3.0
    assert max(clock.slept) <= dataapi.POLL_MAX
    assert "GetStatementResult" not in data_api.operations()


def test_without_a_budget_the_deadline_is_the_default(data_api, clock):
    fakes.submit(data_api, SQL)
    for _ in range(200):
        fakes.describe(data_api, "SUBMITTED")
    data_api.add("cancel_statement", {"Status": True}, {"Id": STATEMENT_ID})
    started = clock.now
    result = run("REDSHIFT_RUN_SELECT", {"sql": SQL})
    assert result.error_code == ERROR_TIMEOUT
    assert DEFAULT_TIMEOUT_SECONDS - 1 < clock.now - started <= DEFAULT_TIMEOUT_SECONDS


def test_a_cancel_that_fails_still_times_out(data_api):
    fakes.submit(data_api, SQL)
    for _ in range(40):
        fakes.describe(data_api, "STARTED")
    data_api.error("cancel_statement", "ValidationException")
    result = run("REDSHIFT_RUN_SELECT", {"sql": SQL}, {**blob(), "timeout_seconds": 2})
    assert result.error_code == ERROR_TIMEOUT


def test_paging_stops_at_the_deadline(data_api):
    data_api.latency = 1.0  # every request takes a (fake) second
    fakes.submit(data_api, SQL)
    fakes.describe(data_api, "FINISHED", ResultRows=900)
    for n in range(5):
        fakes.result(
            data_api,
            [fakes.record(n)],
            token_in=f"page-{n}" if n else None,
            token_out=f"page-{n + 1}",
        )
    result = run("REDSHIFT_RUN_SELECT", {"sql": SQL}, {**blob(), "timeout_seconds": 4})
    assert result.error_code == ERROR_TIMEOUT
    assert data_api.operations().count("GetStatementResult") < 5


def test_request_timeouts_fit_inside_the_budget(data_api):
    fakes.finished_query(data_api, SQL, [[fakes.record(1)]])
    result = run("REDSHIFT_RUN_SELECT", {"sql": SQL}, {**blob(), "timeout_seconds": 4})
    assert result.successful, result.error
    ((client, _),) = data_api.clients
    config = client.meta.config
    assert config.read_timeout <= 4 and config.connect_timeout <= 4
    assert config.retries["total_max_attempts"] == 2


# -- the metadata tools -----------------------------------------------------------------


def test_list_schemas_pages(data_api):
    data_api.add(
        "list_schemas",
        {"Schemas": ["policies", "public"], "NextToken": "schemas-2"},
        {**fakes.CLUSTER, "MaxResults": 2, "SchemaPattern": "p%"},
    )
    first = run("REDSHIFT_LIST_SCHEMAS", {"schema_pattern": "p%", "limit": 2})
    assert first.successful, first.error
    assert first.data == {
        "items": [{"schema": "policies"}, {"schema": "public"}],
        "has_more": True,
        "next_token": "schemas-2",
    }
    data_api.add(
        "list_schemas",
        {"Schemas": ["pii"]},
        {
            **fakes.CLUSTER,
            "MaxResults": 2,
            "SchemaPattern": "p%",
            "NextToken": "schemas-2",
        },
    )
    second = run(
        "REDSHIFT_LIST_SCHEMAS",
        {"schema_pattern": "p%", "limit": 2, "next_token": "schemas-2"},
    )
    assert second.data == {
        "items": [{"schema": "pii"}],
        "has_more": False,
        "next_token": None,
    }
    data_api.assert_drained()


def test_list_tables_keeps_the_exact_schema_only(data_api):
    data_api.add(
        "list_tables",
        {
            "Tables": [
                {"name": "rules", "schema": "pol_a", "type": "TABLE"},
                {"name": "rules_v", "schema": "pol_a", "type": "VIEW"},
                {
                    "name": "other",
                    "schema": "polxa",
                    "type": "TABLE",
                },  # '_' matched 'x'
            ]
        },
        {
            **fakes.CLUSTER,
            "MaxResults": 100,
            "SchemaPattern": "pol_a",
            "TablePattern": "rul%",
        },
    )
    result = run("REDSHIFT_LIST_TABLES", {"schema": "pol_a", "table_pattern": "rul%"})
    assert result.successful, result.error
    assert result.data["items"] == [
        {"schema": "pol_a", "name": "rules", "type": "TABLE"},
        {"schema": "pol_a", "name": "rules_v", "type": "VIEW"},
    ]
    assert result.data["has_more"] is False


def test_describe_table_reads_every_column_page(data_api):
    expected = {
        **fakes.CLUSTER,
        "Schema": "policies",
        "Table": "rules",
        "MaxResults": 1000,
    }
    data_api.add(
        "describe_table",
        {
            "TableName": "rules",
            "ColumnList": [
                fakes.column("id", "int8", nullable=0),
                fakes.column("title"),
            ],
            "NextToken": "cols-2",
        },
        expected,
    )
    data_api.add(
        "describe_table",
        {
            "TableName": "rules",
            "ColumnList": [
                fakes.column("body", length=65535, columnDefault="''"),
                fakes.column("elsewhere", tableName="rules_archive"),
            ],
        },
        {**expected, "NextToken": "cols-2"},
    )
    result = run("REDSHIFT_DESCRIBE_TABLE", {"schema": "policies", "table": "rules"})
    assert result.successful, result.error
    columns = result.data["columns"]
    assert [c["name"] for c in columns] == ["id", "title", "body"]
    assert columns[0]["nullable"] is False and columns[0]["ordinal"] == 1
    assert columns[2]["length"] == 65535 and columns[2]["default"] == "''"


def test_describe_a_table_that_isnt_there(data_api):
    data_api.add("describe_table", {"ColumnList": []})
    result = run("REDSHIFT_DESCRIBE_TABLE", {"schema": "policies", "table": "nope"})
    assert result.error_code == ERROR_NOT_FOUND


# -- what a caller can't do ------------------------------------------------------------

TOOL_ARGS = {
    "REDSHIFT_RUN_SELECT": {"sql": SQL},
    "REDSHIFT_LIST_SCHEMAS": {},
    "REDSHIFT_LIST_TABLES": {},
    "REDSHIFT_DESCRIBE_TABLE": {"schema": "policies", "table": "rules"},
}


@pytest.mark.parametrize("key", sorted(SERVER_KEYS))
@pytest.mark.parametrize("slug", sorted(TOOL_ARGS))
def test_a_caller_cannot_set_a_server_key(slug, key, data_api, assume_calls):
    value: Any = "analytics-evil" if key != "regions" else ["us-west-2"]
    result = run(slug, {**TOOL_ARGS[slug], key: value})
    assert result.error_code == ERROR_INVALID_ARGUMENTS
    assert assume_calls == [] and data_api.calls == []


def test_every_schema_refuses_unlisted_arguments():
    for tool in RedshiftToolkit().tools():
        assert tool.input_schema["additionalProperties"] is False
        assert tool.read_only


@pytest.mark.parametrize(
    "config",
    [{}, None, {"redshift": blob()["redshift"]}, {**blob(), "redshift": None}],
    ids=["empty", "none", "no-role", "no-section"],
)
@pytest.mark.parametrize("slug", sorted(TOOL_ARGS))
def test_without_credentials_it_fails_closed(slug, config, data_api, assume_calls):
    result = RedshiftToolkit().call_tool(slug, TOOL_ARGS[slug], config)
    assert result.error_code == ERROR_NOT_CONNECTED
    assert "not connected" in result.error
    assert assume_calls == [] and data_api.calls == []


def test_without_a_service_principal_it_fails_closed(monkeypatch, data_api):
    class NoCredentials:
        def get_credentials(self) -> None:
            return None

    monkeypatch.setattr(RedshiftToolkit, "base_session", staticmethod(NoCredentials))
    result = run("REDSHIFT_RUN_SELECT", {"sql": SQL})
    assert result.error_code == ERROR_NOT_CONNECTED
    assert "no service principal" in result.error
    assert data_api.clients == []


# -- the shared AWS credential (#31) --------------------------------------------------


def test_assume_role_goes_through_the_shared_helper(
    monkeypatch, data_api, assume_calls
):
    seen: list[Any] = []
    real = aws_credentials.assume_role_session

    def spy(cfg: Any, **kwargs: Any) -> Any:
        seen.append(cfg)
        return real(cfg, **kwargs)

    monkeypatch.setattr(aws_credentials, "assume_role_session", spy)
    fakes.finished_query(data_api, SQL, [[fakes.record(1)]])
    assert run("REDSHIFT_RUN_SELECT", {"sql": SQL}).successful
    assert [c.role_arn for c in seen] == [ROLE_ARN]
    (call,) = assume_calls
    assert call["RoleArn"] == ROLE_ARN
    assert call["ExternalId"] == EXTERNAL_ID
    assert call["RoleSessionName"] == aws_credentials.ROLE_SESSION_NAME
    ((client, _),) = data_api.clients
    assert access_key_of(client) != AMBIENT_KEY


def test_redshift_has_no_assume_role_of_its_own():
    """The only AssumeRole is #31's: the Redshift classes inherit the AWS
    base classes' session and principal, and no Redshift module builds an STS
    client or credentials."""
    from pathlib import Path

    import aisquare_pipe_aws.redshift as package
    from aisquare_pipe_aws.connector import _AwsSource
    from aisquare_pipe_aws.redshift.connector import RedshiftQuerySource
    from aisquare_pipe_aws.toolkit import _AwsToolkit

    assert issubclass(RedshiftToolkit, _AwsToolkit)
    assert issubclass(RedshiftQuerySource, _AwsSource)
    for cls in (RedshiftToolkit, RedshiftQuerySource):
        assert "_session" not in vars(cls) and "base_session" not in vars(cls)
    for path in Path(package.__file__).parent.glob("*.py"):
        text = path.read_text()
        for own in (
            '"sts"',
            "assume_role(",
            "RefreshableCredentials",
            "boto3.Session(",
        ):
            assert own not in text, f"{path.name} has {own}"


def test_the_session_is_reused_across_tool_calls(data_api, assume_calls):
    for _ in range(2):
        data_api.add("list_schemas", {"Schemas": []})
        assert run("REDSHIFT_LIST_SCHEMAS", {}).successful
    assert len(assume_calls) == 1


def test_a_role_in_the_services_own_account_is_refused(data_api):
    config = {**blob(), "role_arn": "arn:aws:iam::123456789012:role/Ours"}
    result = run("REDSHIFT_LIST_SCHEMAS", {}, config)
    assert result.error_code == ERROR_FORBIDDEN
    assert data_api.clients == []


@pytest.mark.parametrize(
    "aws_code, code",
    [
        ("AccessDeniedException", ERROR_FORBIDDEN),
        ("ValidationException", ERROR_INVALID_ARGUMENTS),
        ("ResourceNotFoundException", ERROR_NOT_FOUND),
        ("ActiveStatementsExceededException", ERROR_RATE_LIMITED),
        ("ActiveSessionsExceededException", ERROR_RATE_LIMITED),
        ("ThrottlingException", ERROR_RATE_LIMITED),
        ("QueryTimeoutException", ERROR_TIMEOUT),
        ("DatabaseConnectionException", ERROR_UPSTREAM_ERROR),
        ("ExecuteStatementException", ERROR_UPSTREAM_ERROR),
    ],
)
def test_aws_refusals_carry_awss_code_never_its_message(data_api, aws_code, code):
    data_api.error("execute_statement", aws_code)
    result = run("REDSHIFT_RUN_SELECT", {"sql": SQL})
    assert result.error_code == code
    assert aws_code in result.error
    assert "secret-looking detail" not in result.error


@pytest.mark.parametrize(
    "section, connection",
    [
        (
            {"db_user": "aisquare_reader"},
            {**fakes.CLUSTER, "DbUser": "aisquare_reader"},
        ),
        ({"secret_arn": SECRET_ARN}, {**fakes.CLUSTER, "SecretArn": SECRET_ARN}),
        (
            {"cluster_identifier": None, "workgroup_name": "policies-wg"},
            {"WorkgroupName": "policies-wg", "Database": "policies"},
        ),
    ],
    ids=["db-user", "secret", "workgroup"],
)
def test_each_connection_mode_sends_its_params(data_api, section, connection):
    fakes.finished_query(data_api, SQL, [[fakes.record(1)]], connection=connection)
    result = run("REDSHIFT_RUN_SELECT", {"sql": SQL}, blob(**section))
    assert result.successful, result.error
    data_api.assert_drained()


def test_the_client_is_in_the_targets_region(data_api):
    data_api.add("list_schemas", {"Schemas": []})
    config = {**blob(region="eu-west-1"), "regions": ["us-east-1", "eu-west-1"]}
    assert run("REDSHIFT_LIST_SCHEMAS", {}, config).successful
    ((client, _),) = data_api.clients
    assert client.meta.region_name == "eu-west-1"
