"""``redshift-query-source`` against a stubbed Data API (moto STS for the role)."""

from __future__ import annotations

import hashlib
import json
import logging
from typing import Any

import pytest
from aisquare.pipe import PullParams
from aisquare.pipe.errors import ConfigValidationError, PipelineError

from aisquare_pipe_aws import connector as aws_connector
from aisquare_pipe_aws import credentials as aws_credentials
from aisquare_pipe_aws.credentials import AwsNotConfiguredError
from aisquare_pipe_aws.redshift.config import SERVER_KEYS
from aisquare_pipe_aws.redshift.connector import RedshiftQuerySource
from aisquare_pipe_aws.redshift.sqlguard import NotReadOnly
from tests import redshift_fakes as fakes
from tests.conftest import AMBIENT_KEY, EXTERNAL_ID, ROLE_ARN, access_key_of
from tests.redshift_fakes import STATEMENT_ID, blob

SQL = "SELECT id, title, active FROM policies.rules"

pytestmark = pytest.mark.usefixtures("aws", "clock")


def pull(params: dict[str, Any], config: dict[str, Any] | None = None) -> list[Any]:
    source = RedshiftQuerySource()
    return list(source.pull(blob() if config is None else config, PullParams(params)))


def body(envelope: Any) -> dict[str, Any]:
    return json.loads(envelope.data)


@pytest.fixture
def every_record(caplog):
    """Every log record at DEBUG and up: root (caplog) and a handler on the
    "sqlglot" logger itself."""
    records: list[logging.LogRecord] = []

    class Keep(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    sqlglot_logger = logging.getLogger("sqlglot")
    keep = Keep(level=logging.DEBUG)
    caplog.set_level(logging.DEBUG)
    caplog.set_level(logging.DEBUG, logger="sqlglot")
    sqlglot_logger.addHandler(keep)
    try:
        yield records
    finally:
        sqlglot_logger.removeHandler(keep)


#: Admitted, and sqlglot logs its path while parsing it (seq 13800).
JSON_PATH_SQL = (
    "SELECT JSON_EXTRACT(payload, 'member.ssn-123-45-6789') AS v FROM claims"
)


def _no_record_carries_the_path(caplog, records) -> None:
    assert [r.getMessage() for r in records] == []
    for record in caplog.records:
        assert not record.name.startswith("sqlglot"), record.getMessage()
        assert "ssn-123-45-6789" not in record.getMessage(), record.name


def test_a_json_path_query_pulls_and_nothing_logs_its_path(
    data_api, caplog, every_record
):
    fakes.finished_query(data_api, JSON_PATH_SQL, [[fakes.record(1)]])
    envelopes = pull({"sql": JSON_PATH_SQL})
    assert sum(e.metadata["row_count"] for e in envelopes) == 1
    assert data_api.calls[0] == (
        "ExecuteStatement",
        {**data_api.calls[0][1], "Sql": JSON_PATH_SQL},
    )
    data_api.assert_drained()
    _no_record_carries_the_path(caplog, every_record)


def test_a_query_in_batches_with_version_markers(data_api, assume_calls):
    records = [fakes.record(i) for i in range(5)]
    fakes.finished_query(data_api, SQL, [records[:3], records[3:]])
    envelopes = pull({"sql": SQL, "batch_size": 2})
    assert [len(body(e)["rows"]) for e in envelopes] == [2, 2, 1]
    first = envelopes[0].metadata
    assert {e.metadata["version"] for e in envelopes} == {first["result_sha256"]}
    assert [e.metadata["batch_index"] for e in envelopes] == [0, 1, 2]
    assert [e.metadata["first_row"] for e in envelopes] == [0, 2, 4]
    assert all(e.metadata["batch_count"] == 3 for e in envelopes)
    assert first["result_row_count"] == 5 and first["total_rows"] == 5
    assert first["statement_id"] == STATEMENT_ID
    assert (
        first["database"] == "policies" and first["cluster_identifier"] == "analytics"
    )
    assert first["query_sha256"] == hashlib.sha256(SQL.encode()).hexdigest()
    assert "truncated" not in first
    assert body(envelopes[0])["columns"][0]["name"] == "id"
    assert envelopes[0].content_type == "application/json"
    assert SQL not in json.dumps([e.metadata for e in envelopes])
    data_api.assert_drained()
    (call,) = assume_calls
    assert call["RoleArn"] == ROLE_ARN and call["ExternalId"] == EXTERNAL_ID


def _version(data_api: Any, records: list[Any]) -> str:
    fakes.finished_query(data_api, SQL, [records])
    return pull({"sql": SQL})[0].metadata["version"]


def test_the_version_ignores_row_order_and_follows_the_data(data_api):
    rows = [fakes.record(i) for i in range(3)]
    v1 = _version(data_api, rows)
    v2 = _version(data_api, list(reversed(rows)))
    v3 = _version(data_api, [*rows[:2], fakes.record(2, title="rule 2, edited")])
    assert v1 == v2
    assert v3 != v1


def test_a_table_is_read_with_a_quoted_select(data_api):
    fakes.finished_query(
        data_api, 'SELECT * FROM "policies"."rules"', [[fakes.record(1)]]
    )
    (envelope,) = pull({"table": "policies.rules"})
    assert envelope.metadata["table"] == "policies.rules"
    assert "query_sha256" not in envelope.metadata
    data_api.assert_drained()


def test_an_empty_result_is_one_envelope(data_api):
    fakes.finished_query(data_api, SQL, [[]])
    (envelope,) = pull({"sql": SQL})
    assert body(envelope)["rows"] == []
    assert envelope.metadata["row_count"] == 0 and envelope.metadata["batch_count"] == 1
    assert envelope.metadata["version"]


def test_max_rows_says_truncated_on_every_envelope(data_api):
    fakes.submit(data_api, SQL)
    fakes.describe(data_api, "FINISHED", ResultRows=50)
    fakes.result(data_api, [fakes.record(i) for i in range(4)], token_out="page-1")
    envelopes = pull({"sql": SQL, "max_rows": 3, "batch_size": 2})
    assert sum(e.metadata["row_count"] for e in envelopes) == 3
    assert {e.metadata["truncated"] for e in envelopes} == {"max_rows"}
    assert envelopes[0].metadata["total_rows"] == 50


def test_the_statement_is_cancelled_at_timeout_seconds(data_api, clock):
    fakes.submit(data_api, SQL)
    for _ in range(40):
        fakes.describe(data_api, "STARTED")
    data_api.add("cancel_statement", {"Status": True}, {"Id": STATEMENT_ID})
    started = clock.now
    with pytest.raises(PipelineError, match="the statement was cancelled"):
        pull({"sql": SQL, "timeout_seconds": 5})
    assert clock.now - started <= 5
    assert data_api.operations()[-1] == "CancelStatement"


def test_the_hosts_budget_caps_timeout_seconds(data_api, clock):
    fakes.submit(data_api, SQL)
    for _ in range(40):
        fakes.describe(data_api, "STARTED")
    data_api.add("cancel_statement", {"Status": True}, {"Id": STATEMENT_ID})
    started = clock.now
    with pytest.raises(PipelineError, match="deadline"):
        pull({"sql": SQL, "timeout_seconds": 600}, {**blob(), "timeout_seconds": 2})
    assert clock.now - started <= 2


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT 1 --\r/* /* */; DROP TABLE ds.t; SELECT 1 /* */ -- */",
        "SELECT 'a\\'; DROP TABLE t; --'",
        "SELECT * INTO copy_of_rules FROM policies.rules",
        "DROP TABLE policies.rules",
    ],
)
def test_refused_sql_reaches_no_aws_at_all(sql, data_api, assume_calls):
    with pytest.raises(NotReadOnly):
        pull({"sql": sql})
    assert assume_calls == [] and data_api.calls == []


@pytest.mark.parametrize("key", sorted(SERVER_KEYS))
def test_a_param_naming_a_server_key_is_refused(key, data_api, assume_calls):
    with pytest.raises(ConfigValidationError, match="server-controlled"):
        pull({"sql": SQL, key: "evil"})
    assert assume_calls == [] and data_api.calls == []


@pytest.mark.parametrize(
    "params",
    [
        {},
        {"sql": SQL, "table": "policies.rules"},
        {"table": "rules"},
        {"table": 'policies."rules"; DROP TABLE t; --'},
        {"table": "policies.rules.extra"},
        {"sql": ""},
        {"sql": SQL, "where": "1=1"},
        {"sql": SQL, "batch_size": 0},
        {"sql": SQL, "max_rows": 100_001},
        {"sql": SQL, "max_result_bytes": True},
        {"sql": SQL, "timeout_seconds": 601},
    ],
)
def test_bad_params_are_refused_before_aws(params, data_api, assume_calls):
    with pytest.raises(ConfigValidationError):
        pull(params)
    assert assume_calls == [] and data_api.calls == []


@pytest.mark.parametrize(
    "config", [{}, {"redshift": blob()["redshift"]}, {**blob(), "redshift": {}}]
)
def test_without_credentials_it_fails_closed(config, data_api, assume_calls):
    assert not RedshiftQuerySource().validate_config(config)
    with pytest.raises(AwsNotConfiguredError):
        pull({"sql": SQL}, config)
    assert assume_calls == [] and data_api.calls == []


def test_the_source_assumes_the_role_through_the_shared_helper(monkeypatch, data_api):
    seen: list[Any] = []
    real = aws_credentials.assume_role_session

    def spy(cfg: Any, **kwargs: Any) -> Any:
        seen.append(cfg)
        return real(cfg, **kwargs)

    # _AwsSource._session (the S3 and AgentCore sources' own) calls the name
    # aisquare_pipe_aws.connector imported from the helper.
    monkeypatch.setattr(aws_connector, "assume_role_session", spy)
    fakes.finished_query(data_api, SQL, [[fakes.record(1)]])
    pull({"sql": SQL})
    assert [c.role_arn for c in seen] == [ROLE_ARN]
    ((client, _),) = data_api.clients
    assert access_key_of(client) != AMBIENT_KEY


def test_our_logs_carry_no_sql_and_no_values(data_api, caplog):
    # botocore's own DEBUG request log is the host's to switch off; ours never
    # carries SQL, names or values at any level.
    caplog.set_level(logging.DEBUG, logger="aisquare")
    fakes.finished_query(data_api, SQL, [[fakes.record(1, title="Confidential rule")]])
    pull({"sql": SQL})
    fakes.submit(data_api, SQL)
    fakes.describe(
        data_api, "FAILED", Error="ERROR: permission denied for relation rules"
    )
    # The source's error leaves the database's text out too (hosts log it).
    with pytest.raises(PipelineError, match="lacks a grant") as raised:
        pull({"sql": SQL})
    assert "permission denied" not in str(raised.value)
    ours = "\n".join(
        r.getMessage() for r in caplog.records if r.name.startswith("aisquare")
    )
    for secret in (SQL, "Confidential rule", "permission denied", EXTERNAL_ID):
        assert secret not in ours
