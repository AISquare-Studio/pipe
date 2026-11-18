"""The Redshift Data API, on the assumed role's session, inside one deadline.

A query is asynchronous: ``ExecuteStatement`` submits it,
``DescribeStatement`` is polled until it is ``FINISHED`` (or ``FAILED`` /
``ABORTED``), and ``GetStatementResult`` pages its rows. Everything happens
before one :class:`Deadline`: polling stops there, the statement is cancelled
(``CancelStatement``, best effort) and the call fails with ``timeout``. Each
request's own connect and read timeouts fit inside what is left, and
botocore retries at most once.

The metadata tools use the Data API's own synchronous ``ListSchemas``,
``ListTables`` and ``DescribeTable``: no SQL text is built from a caller's
names.

Errors keep the shared rule (:mod:`aisquare_pipe_aws.credentials`): a refused
AWS call is an :class:`~aisquare_pipe_aws.credentials.AwsAccessError` with
AWS's error code, never AWS's message. A statement that ran and FAILED is
different: its error is the database's own (``permission denied for
relation ...``), about the caller's own SQL, so it is passed on (bounded),
never logged.
"""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import Any

from aisquare.pipe.errors import PipeError

from aisquare_pipe_aws.credentials import AwsAccessError, call
from aisquare_pipe_aws.redshift.config import RedshiftTarget

logger = logging.getLogger("aisquare.pipe.redshift")

SERVICE = "redshift-data"
#: What ListStatements and CloudTrail in the customer's account show.
STATEMENT_NAME = "aisquare-pipe"
#: The longest one request may wait for a response.
MAX_READ_TIMEOUT = 20.0
MAX_CONNECT_TIMEOUT = 5.0
#: DescribeStatement polling: first wait, growth, longest wait.
POLL_FIRST = 0.05
POLL_FACTOR = 2.0
POLL_MAX = 1.0
#: The most of a failed statement's database error passed on.
MAX_ERROR_CHARS = 500

#: Data API errors the shared classifier doesn't know.
_DATA_API_CODES = {
    "ActiveStatementsExceededException": "rate_limited",
    "ActiveSessionsExceededException": "rate_limited",
    "QueryTimeoutException": "timeout",
    "DatabaseConnectionException": "upstream_error",
    "ExecuteStatementException": "upstream_error",
    "BatchExecuteStatementException": "upstream_error",
    "InternalServerException": "upstream_error",
}

#: Tests replace these (a fake clock); production uses real time.
monotonic: Callable[[], float] = time.monotonic
sleep: Callable[[float], None] = time.sleep


class StatementTimeout(PipeError):
    """The deadline passed before the statement finished (it was cancelled)."""


class StatementFailed(PipeError):
    """The statement ran and Redshift reported ``FAILED`` or ``ABORTED``.
    ``code`` is a toolkit error code, the message the database's own error,
    bounded."""

    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass
class Deadline:
    """A point in time (``monotonic()`` seconds) every call fits before."""

    seconds: float
    at: float = field(init=False)

    def __post_init__(self) -> None:
        self.at = monotonic() + self.seconds

    def left(self) -> float:
        return self.at - monotonic()

    def check(self) -> None:
        if self.left() <= 0:
            raise StatementTimeout()


def client(session: Any, target: RedshiftTarget, deadline: Deadline) -> Any:
    """A redshift-data client on ``session`` (the assumed role's), in the
    target's region, with request timeouts inside the deadline."""
    from botocore.config import Config

    left = max(deadline.left(), 1.0)
    return session.client(
        SERVICE,
        region_name=target.region,
        config=Config(
            connect_timeout=min(MAX_CONNECT_TIMEOUT, left),
            read_timeout=min(MAX_READ_TIMEOUT, left),
            retries={"total_max_attempts": 2, "mode": "standard"},
        ),
    )


def _call(fn: Callable[..., Any], *, what: str, **kwargs: Any) -> Any:
    """:func:`aisquare_pipe_aws.credentials.call`, with the Data API's own
    error codes mapped too, and a request that timed out as
    :class:`StatementTimeout`."""
    from botocore.exceptions import ConnectTimeoutError, ReadTimeoutError

    def timed(**params: Any) -> Any:
        try:
            return fn(**params)
        except (ConnectTimeoutError, ReadTimeoutError):
            raise StatementTimeout() from None

    try:
        return call(timed, what=what, **kwargs)
    except AwsAccessError as e:
        mapped = _DATA_API_CODES.get(e.aws_code)
        if mapped is None:
            raise
        raise AwsAccessError(
            f"AWS refused {what} ({e.aws_code})", code=mapped, aws_code=e.aws_code
        ) from None


# -- one query --------------------------------------------------------------------------


@dataclass
class Statement:
    """A submitted statement and what DescribeStatement said about it."""

    id: str
    status: str = "SUBMITTED"
    has_result_set: bool = False
    result_rows: int | None = None
    duration_ns: int | None = None


def execute(
    api: Any, target: RedshiftTarget, sql: str, deadline: Deadline
) -> Statement:
    """Submit ``sql`` (already checked SELECT-only) and wait for it to
    finish. :class:`StatementTimeout` (after cancelling it) when the
    deadline passes first; :class:`StatementFailed` when it fails."""
    deadline.check()
    submitted = _call(
        api.execute_statement,
        what="redshift-data:ExecuteStatement",
        Sql=sql,
        StatementName=STATEMENT_NAME,
        ClientToken=uuid.uuid4().hex,
        **target.connection(),
    )
    statement = Statement(id=str(submitted["Id"]))
    wait = POLL_FIRST
    while True:
        if deadline.left() <= 0:
            cancel(api, statement.id)
            raise StatementTimeout()
        described = _call(
            api.describe_statement,
            what="redshift-data:DescribeStatement",
            Id=statement.id,
        )
        statement.status = str(described.get("Status") or "")
        if statement.status == "FINISHED":
            statement.has_result_set = bool(described.get("HasResultSet"))
            rows = described.get("ResultRows")
            statement.result_rows = (
                rows if isinstance(rows, int) and rows >= 0 else None
            )
            duration = described.get("Duration")
            statement.duration_ns = duration if isinstance(duration, int) else None
            return statement
        if statement.status in ("FAILED", "ABORTED"):
            raise _failed(statement.status, described.get("Error"))
        left = deadline.left()
        if left <= 0:
            cancel(api, statement.id)
            raise StatementTimeout()
        sleep(min(wait, left))
        wait = min(wait * POLL_FACTOR, POLL_MAX)


def _failed(status: str, error: Any) -> StatementFailed:
    text = str(error or "").strip()
    if len(text) > MAX_ERROR_CHARS:
        text = text[:MAX_ERROR_CHARS] + "..."
    lowered = text.lower()
    if status == "ABORTED":
        code = "upstream_error"
    elif "permission denied" in lowered:
        code = "forbidden"
    elif "does not exist" in lowered:
        code = "not_found"
    else:
        code = "invalid_arguments"
    message = f"the statement {status.lower()} in Redshift"
    return StatementFailed(f"{message}: {text}" if text else message, code=code)


def cancel(api: Any, statement_id: str) -> None:
    """CancelStatement, best effort: a failure is logged (no SQL), never raised."""
    try:
        api.cancel_statement(Id=statement_id)
    except Exception as e:  # noqa: BLE001 — the caller is already failing with timeout
        logger.info("redshift: CancelStatement failed (%s)", type(e).__name__)


def result_pages(
    api: Any, statement: Statement, deadline: Deadline
) -> Iterator[dict[str, Any]]:
    """Each GetStatementResult page of a finished statement, while the
    deadline lasts (:class:`StatementTimeout` once it passes)."""
    if not statement.has_result_set:
        return
    token: str | None = None
    while True:
        deadline.check()
        kwargs: dict[str, Any] = {"Id": statement.id}
        if token:
            kwargs["NextToken"] = token
        page = _call(
            api.get_statement_result, what="redshift-data:GetStatementResult", **kwargs
        )
        yield page
        token = page.get("NextToken") or None
        if token is None:
            return


# -- metadata (synchronous) -----------------------------------------------------------


def list_schemas(
    api: Any,
    target: RedshiftTarget,
    *,
    pattern: str | None,
    limit: int,
    next_token: str | None,
) -> dict[str, Any]:
    kwargs: dict[str, Any] = {**target.connection(), "MaxResults": limit}
    if pattern:
        kwargs["SchemaPattern"] = pattern
    if next_token:
        kwargs["NextToken"] = next_token
    return dict(_call(api.list_schemas, what="redshift-data:ListSchemas", **kwargs))


def list_tables(
    api: Any,
    target: RedshiftTarget,
    *,
    schema_pattern: str | None,
    table_pattern: str | None,
    limit: int,
    next_token: str | None,
) -> dict[str, Any]:
    kwargs: dict[str, Any] = {**target.connection(), "MaxResults": limit}
    if schema_pattern:
        kwargs["SchemaPattern"] = schema_pattern
    if table_pattern:
        kwargs["TablePattern"] = table_pattern
    if next_token:
        kwargs["NextToken"] = next_token
    return dict(_call(api.list_tables, what="redshift-data:ListTables", **kwargs))


def describe_table(
    api: Any,
    target: RedshiftTarget,
    *,
    schema: str,
    table: str,
    limit: int,
    next_token: str | None,
) -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        **target.connection(),
        "Schema": schema,
        "Table": table,
        "MaxResults": limit,
    }
    if next_token:
        kwargs["NextToken"] = next_token
    return dict(_call(api.describe_table, what="redshift-data:DescribeTable", **kwargs))
