"""Redshift test helpers: the blob, a fake clock, a stubbed Data API, and its responses.

The Data API is a botocore ``Stubber`` on the real ``redshift-data`` client,
so every request is checked against AWS's own model; STS is moto, as for the
other AWS tests (``conftest.py``). The Data API's clock and sleep are a fake
clock that moves only when the code sleeps (the ``clock`` fixture).
"""

from __future__ import annotations

from collections import deque
from datetime import UTC, datetime
from typing import Any

import pytest
from botocore.exceptions import ReadTimeoutError
from botocore.stub import ANY, Stubber

from aisquare_pipe_aws.redshift import dataapi
from tests.conftest import CUSTOMER_ACCOUNT, EXTERNAL_ID, ROLE_ARN

SECRET_ARN = f"arn:aws:secretsmanager:us-east-1:{CUSTOMER_ACCOUNT}:secret:redshift/aisquare-reader-AbCdEf"
STATEMENT_ID = "d9b6c0c9-0747-4bf4-b142-e8883122f766"
T0 = datetime(2026, 11, 17, 12, 0, tzinfo=UTC)

#: The connection params of blob()'s default target.
CLUSTER = {"ClusterIdentifier": "analytics", "Database": "policies"}


def blob(**redshift: Any) -> dict[str, Any]:
    """The tenant's ``redshift`` secret: the AWS blob plus its section
    (a provisioned cluster on the role's IAM identity unless overridden;
    a value of None drops that key)."""
    section: dict[str, Any] = {
        "cluster_identifier": "analytics",
        "database": "policies",
    }
    section.update(redshift)
    return {
        "role_arn": ROLE_ARN,
        "external_id": EXTERNAL_ID,
        "regions": ["us-east-1"],
        "redshift": {k: v for k, v in section.items() if v is not None},
    }


class Clock:
    """``dataapi.monotonic`` and ``dataapi.sleep``: time passes only in sleep."""

    def __init__(self) -> None:
        self.now = 1000.0
        self.slept: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        assert seconds >= 0
        self.slept.append(seconds)
        self.now += seconds


class DataApi:
    """Queue Data API responses; the redshift-data clients the code builds
    answer them in order (a ``Stubber`` on each, checking every request
    against AWS's model). A call builds a new client when its deadline no
    longer covers the current one's timeouts, so all of a test's stubs read
    ONE queue: botocore's ``Stubber`` keeps its responses in ``_queue``."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, clock: Clock) -> None:
        self.queue: list[tuple[str, str, Any, Any]] = []
        self.clients: list[tuple[Any, Stubber]] = []
        self.calls: list[tuple[str, dict[str, Any]]] = []
        #: The fake clock's time at each request, in the order of ``calls``.
        self.times: list[float] = []
        #: The connect timeout plus read timeout of the client each request
        #: went out on, in the order of ``calls``.
        self.timeouts: list[float] = []
        self.clock = clock
        #: How long each request takes (fake seconds), unless ``latencies``
        #: names its operation. One that takes longer than its client's
        #: connect plus read timeout raises ReadTimeoutError at that point,
        #: as botocore would.
        self.latency = 0.0
        self.latencies: dict[str, float] = {}
        shared: deque[Any] = deque()
        real = dataapi.build_client

        def build(session: Any, region: str, seconds: float) -> Any:
            client = real(session, region, seconds)
            stub = Stubber(client)
            stub._queue = shared
            for kind, method, payload, expected in self.queue:
                if kind == "ok":
                    stub.add_response(method, payload, expected)
                else:
                    code, status = payload
                    stub.add_client_error(
                        method,
                        service_error_code=code,
                        service_message="AWS says: secret-looking detail",
                        http_status_code=status,
                        expected_params=expected,
                    )
            self.queue.clear()

            def record(params: dict[str, Any], model: Any, **_: Any) -> None:
                config = client.meta.config
                if model.name == "CancelStatement":
                    # A test queues more polls than the deadline allows: the
                    # ones the code never made are dropped, so the cancel
                    # gets the response queued for it.
                    while shared and shared[0]["operation_name"] == "DescribeStatement":
                        shared.popleft()
                timeouts = config.connect_timeout + config.read_timeout
                self.calls.append((model.name, dict(params)))
                self.times.append(self.clock.now)
                self.timeouts.append(timeouts)
                took = self.latencies.get(model.name, self.latency)
                if took > timeouts:
                    self.clock.now += timeouts
                    raise ReadTimeoutError(endpoint_url=client.meta.endpoint_url)
                self.clock.now += took

            client.meta.events.register("provide-client-params.redshift-data.*", record)
            stub.activate()
            self.clients.append((client, stub))
            return client

        monkeypatch.setattr(dataapi, "build_client", build)

    def add(self, method: str, response: dict[str, Any], expected: Any = None) -> None:
        self.queue.append(("ok", method, response, expected))

    def error(
        self, method: str, code: str, status: int = 400, expected: Any = None
    ) -> None:
        self.queue.append(("error", method, (code, status), expected))

    def operations(self) -> list[str]:
        return [name for name, _ in self.calls]

    def assert_drained(self) -> None:
        assert not self.queue, "responses queued but no client was built"
        for _, stub in self.clients:
            stub.assert_no_pending_responses()


def column(name: str, type_name: str = "varchar", **extra: Any) -> dict[str, Any]:
    meta: dict[str, Any] = {
        "name": name,
        "label": name,
        "typeName": type_name,
        "nullable": 1,
        "schemaName": "policies",
        "tableName": "rules",
        "precision": 256 if type_name == "varchar" else 19,
        "scale": 0,
        "isCaseSensitive": type_name == "varchar",
        "isCurrency": False,
        "isSigned": type_name != "varchar",
    }
    meta.update(extra)
    return meta


COLUMNS = [column("id", "int8"), column("title"), column("active", "bool")]


def record(
    i: int, title: str | None = None, active: bool = True
) -> list[dict[str, Any]]:
    """One row of :data:`COLUMNS`: ``(i, title or "rule i", active)``."""
    return [
        {"longValue": i},
        {"stringValue": title if title is not None else f"rule {i}"},
        {"booleanValue": active},
    ]


def submit(
    api: DataApi,
    sql: str,
    connection: dict[str, Any] | None = None,
) -> None:
    """ExecuteStatement for ``sql``, expecting exactly these params."""
    api.add(
        "execute_statement",
        {
            "Id": STATEMENT_ID,
            "CreatedAt": T0,
            "Database": (connection or CLUSTER)["Database"],
        },
        {
            "Sql": sql,
            "StatementName": "aisquare-pipe",
            "ClientToken": ANY,
            **(connection or CLUSTER),
        },
    )


def describe(api: DataApi, status: str, **extra: Any) -> None:
    response: dict[str, Any] = {"Id": STATEMENT_ID, "Status": status, "CreatedAt": T0}
    if status == "FINISHED":
        response.setdefault("HasResultSet", True)
    response.update(extra)
    api.add("describe_statement", response, {"Id": STATEMENT_ID})


def result(
    api: DataApi,
    records: list[list[dict[str, Any]]],
    *,
    columns: list[dict[str, Any]] | None = None,
    token_in: str | None = None,
    token_out: str | None = None,
    total: int | None = None,
) -> None:
    response: dict[str, Any] = {
        "Records": records,
        "ColumnMetadata": columns if columns is not None else COLUMNS,
    }
    if total is not None:
        response["TotalNumRows"] = total
    if token_out:
        response["NextToken"] = token_out
    expected: dict[str, Any] = {"Id": STATEMENT_ID}
    if token_in:
        expected["NextToken"] = token_in
    api.add("get_statement_result", response, expected)


def finished_query(
    api: DataApi,
    sql: str,
    pages: list[list[list[dict[str, Any]]]],
    *,
    connection: dict[str, Any] | None = None,
    columns: list[dict[str, Any]] | None = None,
) -> None:
    """A statement that starts, finishes and returns ``pages``."""
    submit(api, sql, connection)
    describe(api, "STARTED")
    total = sum(len(p) for p in pages)
    describe(api, "FINISHED", ResultRows=total, Duration=12_000_000)
    for n, page in enumerate(pages):
        result(
            api,
            page,
            columns=columns,
            token_in=f"page-{n}" if n else None,
            token_out=f"page-{n + 1}" if n + 1 < len(pages) else None,
            total=total,
        )
