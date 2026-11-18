"""The Redshift Data API, on the assumed role's session, inside one deadline.

A query is asynchronous: ``ExecuteStatement`` submits it,
``DescribeStatement`` is polled until it is ``FINISHED`` (or ``FAILED`` /
``ABORTED``), and ``GetStatementResult`` pages its rows. The whole call fits
inside one :class:`Deadline`:

- Each request goes out on a client whose connect timeout plus read timeout
  fits inside what is left of the deadline when it is sent (:class:`Api`
  builds a client with shorter ones when the current client's don't fit; the
  read timeout bounds each wait for data). botocore never retries: a retry
  would have to fit too. A throttled or dropped request fails the call with
  its code. With less than :data:`MIN_REQUEST_SECONDS` left, no request is
  sent and the call fails with ``timeout``.
- While a statement runs, a slice of the deadline is kept back for
  ``CancelStatement`` (:data:`CANCEL_SHARE` of it, at most
  :data:`CANCEL_RESERVE_MAX` seconds, at least :data:`MIN_REQUEST_SECONDS`).
  Polling stops where that slice starts, and the statement is cancelled
  inside it (best effort) before the call fails with ``timeout``. The
  cancel goes out on a client of its own whose connect timeout plus read
  timeout is all of what is left (at least :data:`MIN_REQUEST_SECONDS`), not
  on the last poll's, which is sized for a poll. A request that fails or
  times out while the statement runs cancels it the same way. An
  ``ExecuteStatement`` that times out leaves no statement id to cancel.

The deadline covers the Data API requests. The AssumeRole before them is the
shared helper's (:mod:`aisquare_pipe_aws.credentials`, #31), on botocore's
own timeouts; the time it takes counts against the deadline.

The metadata tools use the Data API's own synchronous ``ListSchemas``,
``ListTables`` and ``DescribeTable``: no SQL text is built from a caller's
names.

Errors keep the shared rule (:mod:`aisquare_pipe_aws.credentials`): a refused
AWS call is an :class:`~aisquare_pipe_aws.credentials.AwsAccessError` with
AWS's error code, never AWS's message. A statement that ran and FAILED is
different: its error is the database's own (``permission denied for
relation ...``), about the caller's own SQL, so :class:`StatementFailed`
keeps it (bounded) as ``detail``: the toolkit passes it on, and the source
leaves it out of its error, which hosts log (it can echo the caller's
values). Never logged here.
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
#: The longest one request waits to connect, and for each read.
MAX_CONNECT_TIMEOUT = 5.0
MAX_READ_TIMEOUT = 20.0
#: No request is sent with less of the deadline left than this.
MIN_REQUEST_SECONDS = 0.2
#: Kept back for CancelStatement while a statement runs: this share of the
#: deadline, at most CANCEL_RESERVE_MAX seconds (at least MIN_REQUEST_SECONDS).
CANCEL_SHARE = 0.2
CANCEL_RESERVE_MAX = 3.0
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
    """The deadline passed before the call finished. ``cancelled`` says what
    happened to a statement that was still running: True (cancelled), False
    (CancelStatement failed), or None (none was: it had finished, or the
    ExecuteStatement itself timed out)."""

    def __init__(self, *, cancelled: bool | None = None) -> None:
        self.cancelled = cancelled
        super().__init__(
            "the call did not finish before its deadline" + cancel_note(cancelled)
        )


def cancel_note(cancelled: bool | None) -> str:
    """What a timeout message adds about the statement (``cancelled`` as on
    :class:`StatementTimeout`)."""
    if cancelled is None:
        return ""
    if cancelled:
        return "; the statement was cancelled"
    return "; cancelling the statement failed"


class StatementFailed(PipeError):
    """The statement ran and Redshift reported ``FAILED`` or ``ABORTED``.
    ``code`` is a toolkit error code. The message is ``summary`` (the
    connector's own words) then ``detail``, the database's own error,
    bounded: it can echo the caller's values (``invalid input syntax for
    integer: "..."``), so the toolkit, which answers the caller, shows it,
    and the source's errors carry ``summary`` alone (hosts log those)."""

    def __init__(self, message: str, *, code: str, detail: str = "") -> None:
        super().__init__(f"{message}: {detail}" if detail else message)
        self.code = code
        self.summary = message
        self.detail = detail


@dataclass
class Deadline:
    """A point in time (``monotonic()`` seconds) every call fits before."""

    seconds: float
    at: float = field(init=False)

    def __post_init__(self) -> None:
        self.at = monotonic() + self.seconds

    def left(self) -> float:
        return self.at - monotonic()

    def cancel_reserve(self) -> float:
        """What is kept back for CancelStatement while a statement runs."""
        return max(
            MIN_REQUEST_SECONDS, min(CANCEL_RESERVE_MAX, self.seconds * CANCEL_SHARE)
        )


def build_client(session: Any, region: str, seconds: float) -> Any:
    """A redshift-data client on ``session`` (the assumed role's) in
    ``region``, whose connect timeout plus read timeout is ``seconds`` (at
    most :data:`MAX_CONNECT_TIMEOUT` + :data:`MAX_READ_TIMEOUT`, split in that
    ratio), and which never retries."""
    from botocore.config import Config

    seconds = min(seconds, MAX_CONNECT_TIMEOUT + MAX_READ_TIMEOUT)
    connect = seconds * MAX_CONNECT_TIMEOUT / (MAX_CONNECT_TIMEOUT + MAX_READ_TIMEOUT)
    return session.client(
        SERVICE,
        region_name=region,
        config=Config(
            connect_timeout=connect,
            read_timeout=seconds - connect,
            retries={"total_max_attempts": 1, "mode": "standard"},
        ),
    )


class Api:
    """The redshift-data requests of one call, inside its deadline: each goes
    out on a client whose connect plus read timeout fits inside what is left
    when it is sent. A client is built (no AWS call) only for a first
    request, or when what is left no longer covers the current one's
    timeouts; the new one gets half of what is left (all of it near the end),
    so a call builds a handful at most. The cancel always gets a client of
    its own (:meth:`cancel`)."""

    def __init__(self, session: Any, target: RedshiftTarget, deadline: Deadline):
        self._session = session
        self._region = target.region
        self.deadline = deadline
        self._client: Any = None
        #: The current client's connect timeout plus read timeout.
        self._timeouts = 0.0

    def request(
        self, operation: str, *, what: str, reserve: float = 0.0, **params: Any
    ) -> Any:
        """One request (``operation`` is the client method), with ``reserve``
        seconds of the deadline kept back. :class:`StatementTimeout` without
        sending it when less than :data:`MIN_REQUEST_SECONDS` would be left."""
        budget = self.deadline.left() - reserve
        if budget < MIN_REQUEST_SECONDS:
            raise StatementTimeout()
        return _call(getattr(self._within(budget), operation), what=what, **params)

    def cancel(self, statement_id: str) -> bool:
        """CancelStatement, best effort, in what is left (the slice kept back
        for it): whether it was accepted. A failure is logged (no SQL), never
        raised.

        It goes out on a new client whose connect plus read timeout is all of
        what is left (at least :data:`MIN_REQUEST_SECONDS`; at most
        :data:`MAX_CONNECT_TIMEOUT` + :data:`MAX_READ_TIMEOUT`). The current
        client is never reused: after the polls have run the deadline down
        to the slice, it is the last poll's, often 0.2 s, and a cancel on it
        would leave the rest of the slice unused (seq 13843)."""
        seconds = max(self.deadline.left(), MIN_REQUEST_SECONDS)
        try:
            build_client(self._session, self._region, seconds).cancel_statement(
                Id=statement_id
            )
        except Exception as e:  # noqa: BLE001 — the caller is already failing
            logger.info("redshift: CancelStatement failed (%s)", type(e).__name__)
            return False
        return True

    def _within(self, budget: float) -> Any:
        if self._client is None or self._timeouts > budget:
            half = budget / 2
            seconds = min(
                MAX_CONNECT_TIMEOUT + MAX_READ_TIMEOUT,
                half if half >= MIN_REQUEST_SECONDS else budget,
            )
            self._client = build_client(self._session, self._region, seconds)
            self._timeouts = seconds
        return self._client


def client(session: Any, target: RedshiftTarget, deadline: Deadline) -> Api:
    """The redshift-data requests of one call on ``session`` (the assumed
    role's), in the target's region, inside ``deadline`` (:class:`Api`)."""
    return Api(session, target, deadline)


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


def execute(api: Api, target: RedshiftTarget, sql: str) -> Statement:
    """Submit ``sql`` (already checked SELECT-only) and wait for it to
    finish, keeping the cancel's slice of the deadline back throughout.
    :class:`StatementTimeout` (after cancelling it) when the deadline comes
    first; :class:`StatementFailed` when it fails; an
    :class:`~aisquare_pipe_aws.credentials.AwsAccessError` from a polling
    request after cancelling it."""
    reserve = api.deadline.cancel_reserve()
    submitted = api.request(
        "execute_statement",
        what="redshift-data:ExecuteStatement",
        reserve=reserve,
        Sql=sql,
        StatementName=STATEMENT_NAME,
        ClientToken=uuid.uuid4().hex,
        **target.connection(),
    )
    statement = Statement(id=str(submitted["Id"]))
    try:
        _wait(api, statement, reserve)
    except StatementTimeout:
        raise StatementTimeout(cancelled=api.cancel(statement.id)) from None
    except AwsAccessError:
        api.cancel(statement.id)
        raise
    return statement


def _wait(api: Api, statement: Statement, reserve: float) -> None:
    """Poll DescribeStatement until ``statement`` is FINISHED (filled in) or
    fails, while more than ``reserve`` (plus one request) is left."""
    wait = POLL_FIRST
    last = False
    while True:
        described = api.request(
            "describe_statement",
            what="redshift-data:DescribeStatement",
            reserve=reserve,
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
            return
        if statement.status in ("FAILED", "ABORTED"):
            raise _failed(statement.status, described.get("Error"))
        # Sleep only into time a next poll can still use. A sleep cut short
        # leads to the last poll.
        spare = api.deadline.left() - reserve - MIN_REQUEST_SECONDS
        if last or spare <= 0:
            raise StatementTimeout()
        last = spare <= wait
        sleep(min(wait, spare))
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
    return StatementFailed(message, code=code, detail=text)


def result_pages(api: Api, statement: Statement) -> Iterator[dict[str, Any]]:
    """Each GetStatementResult page of a finished statement, while the
    deadline lasts (:class:`StatementTimeout` once too little is left for a
    request)."""
    if not statement.has_result_set:
        return
    token: str | None = None
    while True:
        kwargs: dict[str, Any] = {"Id": statement.id}
        if token:
            kwargs["NextToken"] = token
        page = api.request(
            "get_statement_result", what="redshift-data:GetStatementResult", **kwargs
        )
        yield page
        token = page.get("NextToken") or None
        # The parsed page goes before the next one is read (the caller lets
        # go of its reference too): one page in memory at a time.
        del page
        if token is None:
            return


# -- metadata (synchronous) -----------------------------------------------------------


def list_schemas(
    api: Api,
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
    return dict(api.request("list_schemas", what="redshift-data:ListSchemas", **kwargs))


def list_tables(
    api: Api,
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
    return dict(api.request("list_tables", what="redshift-data:ListTables", **kwargs))


def describe_table(
    api: Api,
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
    return dict(
        api.request("describe_table", what="redshift-data:DescribeTable", **kwargs)
    )
