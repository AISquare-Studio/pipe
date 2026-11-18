"""A small read-only client for one Snowflake account's SQL API (v2).

Endpoints (docs.snowflake.com/en/developer-guide/sql-api), all on the
credential's own host, ``https://<account>.snowflakecomputing.com``:

* ``POST /api/v2/statements?async=true&requestId=<uuid>`` submits ONE
  statement with the credential's ``role`` and ``warehouse``, the
  ``database``/``schema`` context, a server-side ``timeout`` (the statement
  deadline, so Snowflake itself stops it), and the parameters
  ``MULTI_STATEMENT_COUNT`` 1 (Snowflake refuses a second statement),
  ``ROWS_PER_RESULTSET`` (the row cap plus one, so a cut shows) and
  ``QUERY_TAG`` ``aisquare-pipe`` (the customer finds our queries in
  QUERY_HISTORY). It answers 202 with a ``statementHandle``.
* ``GET /api/v2/statements/{handle}`` until 200 (the result, with rows of
  partition 0) or 422 (the statement failed); 202 (and 429 while polling)
  mean "still running".
* ``GET /api/v2/statements/{handle}?partition=N`` for later partitions, read
  only while under the row and byte caps.
* ``POST /api/v2/statements/{handle}/cancel`` when the deadline passes.

Every request carries the bearer (a key-pair JWT or an OAuth token) and
``X-Snowflake-Authorization-Token-Type``. Redirects are never followed.
Every failure, the OAuth token exchange's included, is a
:class:`SnowflakeAPIError` carrying Snowflake's error ``code`` and
``sqlState`` when it gave them. Error messages never carry a token or a
secret.

Time budget: with ``budget`` (seconds; the host's
``config["timeout_seconds"]``) the whole call fits inside it, from the
client's creation on. Each request gets the time left as its timeout (at
most ``timeout``), and a call with no time left fails ``timed_out`` before
it sends anything. A statement's deadline is the smaller of its own
timeout and the budget left; it is sent as the statement's ``timeout``,
polling stops there, and the statement is then cancelled.
"""

from __future__ import annotations

import json
import math
import re
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Self
from urllib.parse import quote

import requests
from aisquare.pipe.core.batches import BatchPlan

from aisquare_pipe_snowflake._http import retry_after_seconds, timed_out
from aisquare_pipe_snowflake.credentials import (
    TOKEN_RATE_LIMITED,
    TOKEN_REFUSED,
    TOKEN_TIMED_OUT,
    SnowflakeCredentials,
    SnowflakeTokenError,
    bearer_token,
    forget_token,
)
from aisquare_pipe_snowflake.rows import canonical, columns_of, encoded_batch, iter_rows

STATEMENTS_PATH = "/api/v2/statements"
DEFAULT_TIMEOUT = 30.0
USER_AGENT = "aisquare-pipe-snowflake/0.1.0"
QUERY_TAG = "aisquare-pipe"
#: The first pause between status polls (after one immediate read), and the
#: longest.
POLL_FIRST_SECONDS = 0.25
POLL_MAX_SECONDS = 2.0
#: Partitions followed at most for one result.
MAX_PARTITIONS = 1000
TRUNCATED_MAX_ROWS = "max_rows"
TRUNCATED_MAX_RESULT_BYTES = "max_result_bytes"
_HANDLE = re.compile(r"^[A-Za-z0-9-]{1,64}$")
_CODE = re.compile(r"^[0-9A-Za-z]{1,16}$")
_MAX_DETAIL = 400


class SnowflakeAPIError(Exception):
    """A failed Snowflake call. ``status`` is the HTTP status (None when
    nothing came back); ``code`` and ``sql_state`` are Snowflake's;
    ``retry_after`` is set for a 429/503 that said when; ``timed_out`` for a
    timeout or a deadline; ``credential_refused`` when Snowflake or the
    token endpoint refused the credential itself.

    ``message`` (the str) is ``summary``, the connector's own words, then
    ``detail``, Snowflake's own error text when it gave one (redacted and
    bounded). That text can quote the statement and name objects, so only
    the toolkit, which answers the caller, shows it; the source's errors
    carry ``summary`` alone (hosts log those)."""

    def __init__(
        self,
        message: str,
        *,
        detail: str = "",
        status: int | None = None,
        code: str | None = None,
        sql_state: str | None = None,
        retry_after: float | None = None,
        timed_out: bool = False,
        credential_refused: bool = False,
    ) -> None:
        full = f"{message}: {detail}" if detail else message
        super().__init__(full)
        self.message = full
        self.summary = message
        self.detail = detail
        self.status = status
        self.code = code
        self.sql_state = sql_state
        self.retry_after = retry_after
        self.timed_out = timed_out
        self.credential_refused = credential_refused


@dataclass
class StatementResult:
    """One statement's result, read within the caps: ``columns``
    (:func:`~aisquare_pipe_snowflake.rows.columns_of`), ``rows`` as objects
    keyed by column name, ``truncated`` (``max_rows`` or
    ``max_result_bytes``) when rows were left out, ``query_id`` (the
    statement handle, Snowflake's query id)."""

    query_id: str | None
    columns: list[dict[str, Any]] = field(default_factory=list)
    rows: list[dict[str, Any]] = field(default_factory=list)
    truncated: str | None = None
    result_bytes: int = 0
    num_rows: int | None = None
    #: Read with ``encoded=True``: each row's canonical JSON instead of
    #: ``rows`` (as the source's envelopes carry them; about a fifth of the
    #: memory objects take).
    encoded: list[bytes] | None = None
    #: Read with ``batching``: the envelopes the rows are cut into, planned
    #: as they are read; ``result_bytes`` is then every envelope's bytes,
    #: each one's columns header included.
    plan: BatchPlan | None = None

    @property
    def count(self) -> int:
        """The rows read, as objects or encoded."""
        return len(self.rows) if self.encoded is None else len(self.encoded)


def _monotonic() -> float:
    """The clock a budget runs on (tests replace it)."""
    return time.monotonic()


def _token_failure(e: SnowflakeTokenError) -> SnowflakeAPIError:
    """No OAuth token, as the failure it is: only a refused client is 401;
    a timeout stays a timeout; an unreachable or failing endpoint is an
    upstream error a host may retry."""
    if e.kind == TOKEN_REFUSED:
        return SnowflakeAPIError(e.message, status=401, credential_refused=True)
    if e.kind == TOKEN_TIMED_OUT:
        return SnowflakeAPIError(e.message, timed_out=True)
    if e.kind == TOKEN_RATE_LIMITED:
        return SnowflakeAPIError(e.message, status=429, retry_after=e.retry_after)
    return SnowflakeAPIError(e.message, retry_after=e.retry_after)


class SnowflakeClient:
    """One credential's view of its account. Use it as a context manager: a
    session it opened itself is closed on exit; a passed-in ``session``
    (tests, or a host that pools connections) is left open.

    ``timeout`` bounds each request; ``budget`` (None: unbounded) bounds the
    whole call, every request together (see the module docstring)."""

    def __init__(
        self,
        credentials: SnowflakeCredentials,
        *,
        session: Any = None,
        timeout: float = DEFAULT_TIMEOUT,
        budget: float | None = None,
        monotonic: Callable[[], float] | None = None,
        sleep: Callable[[float], None] | None = None,
    ) -> None:
        self._creds = credentials
        self._own_session = session is None
        self._session = session if session is not None else requests.Session()
        self._timeout = timeout
        self._monotonic = monotonic if monotonic is not None else _monotonic
        self._sleep = sleep if sleep is not None else time.sleep
        self.budget = budget if budget is not None and budget > 0 else None
        self._deadline = (
            self._monotonic() + self.budget if self.budget is not None else None
        )
        self._token: str | None = None
        self._token_from_cache = False
        #: Every bearer this client sent: redacted from any error text.
        self._sent_tokens: set[str] = set()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        if self._own_session:
            self._session.close()

    @property
    def credentials(self) -> SnowflakeCredentials:
        return self._creds

    # -- time ----------------------------------------------------------------

    def remaining(self) -> float | None:
        """Seconds left in the budget (None: unbounded)."""
        if self._deadline is None:
            return None
        return self._deadline - self._monotonic()

    def out_of_time(self) -> SnowflakeAPIError:
        return SnowflakeAPIError(
            f"the Snowflake call ran out of its {self.budget:g}s time budget",
            timed_out=True,
        )

    def _request_timeout(self) -> float:
        remaining = self.remaining()
        if remaining is None:
            return self._timeout
        if remaining <= 0:
            raise self.out_of_time()
        return min(self._timeout, remaining)

    def statement_seconds(self, limit: float) -> float:
        """A statement's deadline: ``limit``, or the budget left when that is
        less. Under a second left fails the call before it is submitted."""
        remaining = self.remaining()
        seconds = limit if remaining is None else min(limit, remaining)
        if seconds < 1:
            raise self.out_of_time()
        return seconds

    # -- statements ----------------------------------------------------------

    def execute(
        self,
        statement: str,
        *,
        row_limit: int,
        byte_limit: int,
        timeout_seconds: float,
        database: str | None = None,
        schema: str | None = None,
        bindings: Mapping[str, Mapping[str, str]] | None = None,
        encoded: bool = False,
        batching: tuple[int, int] | None = None,
    ) -> StatementResult:
        """Run ONE statement (a caller's, already checked SELECT-only, or one
        of the connector's own fixed SHOW / INFORMATION_SCHEMA forms) and
        wait for it, at most ``timeout_seconds`` (and the budget). At most
        ``row_limit`` rows and ``byte_limit`` bytes of JSON rows come back
        (``encoded``: as canonical JSON, :class:`StatementResult`).
        Past the deadline the statement is cancelled and a timed-out
        :class:`SnowflakeAPIError` raised."""
        seconds = self.statement_seconds(timeout_seconds)
        deadline = self._monotonic() + seconds
        body: dict[str, Any] = {
            "statement": statement,
            "timeout": max(1, math.ceil(seconds)),
            "role": self._creds.role,
            "warehouse": self._creds.warehouse,
            "resultSetMetaData": {"format": "jsonv2"},
            "parameters": {
                "MULTI_STATEMENT_COUNT": "1",
                "ROWS_PER_RESULTSET": str(row_limit + 1),
                "QUERY_TAG": QUERY_TAG,
            },
        }
        database = database or self._creds.database
        schema = schema or self._creds.schema
        if database:
            body["database"] = database
        if schema:
            body["schema"] = schema
        if bindings:
            body["bindings"] = {k: dict(v) for k, v in bindings.items()}
        params = {"async": "true", "nullable": "true", "requestId": str(uuid.uuid4())}
        status, answer = self._json("POST", STATEMENTS_PATH, params, body=body)
        # The first status read goes out at once (most reads finish in well
        # under a second), then the pauses double up to POLL_MAX_SECONDS.
        delay, next_delay = 0.0, POLL_FIRST_SECONDS
        while status != 200:
            handle = _handle(answer)
            now = self._monotonic()
            if now >= deadline:
                self._cancel(handle)
                if status == 429:
                    raise SnowflakeAPIError(
                        "Snowflake kept answering 429 (too many requests) until the "
                        f"statement's {seconds:g}s deadline; it was cancelled",
                        status=429,
                        retry_after=answer.get("_retry_after"),
                    )
                raise SnowflakeAPIError(
                    f"the statement did not finish within its {seconds:g}s deadline and "
                    "was cancelled",
                    timed_out=True,
                )
            pause = min(delay, deadline - now)
            if status == 429:
                hinted = answer.get("_retry_after")
                if isinstance(hinted, float):
                    pause = min(max(pause, hinted), deadline - now)
            if pause > 0:
                self._sleep(pause)
            delay, next_delay = next_delay, min(next_delay * 2, POLL_MAX_SECONDS)
            status, answer = self._json(
                "GET", f"{STATEMENTS_PATH}/{quote(handle, safe='')}", {}, polling=True
            )
        return self._collect(
            answer,
            row_limit=row_limit,
            byte_limit=byte_limit,
            encoded=encoded,
            batching=batching,
        )

    def _collect(
        self,
        answer: dict[str, Any],
        *,
        row_limit: int,
        byte_limit: int,
        encoded: bool = False,
        batching: tuple[int, int] | None = None,
    ) -> StatementResult:
        meta = answer.get("resultSetMetaData")
        meta = meta if isinstance(meta, dict) else {}
        columns = columns_of(meta.get("rowType"))
        handle = answer.get("statementHandle")
        handle = handle if isinstance(handle, str) and _HANDLE.match(handle) else None
        num_rows = meta.get("numRows")
        result = StatementResult(
            query_id=handle,
            columns=columns,
            num_rows=num_rows if isinstance(num_rows, int) and num_rows >= 0 else None,
            encoded=[] if encoded else None,
        )
        if batching is not None:
            # max_result_bytes counts every envelope's columns header too.
            result.plan = BatchPlan(
                max_rows=batching[0],
                max_bytes=batching[1],
                header=len(encoded_batch(columns, [])) - 2,
                separator=1,
                closing=2,
            )
            result.result_bytes = result.plan.total
        partitions = [p for p in meta.get("partitionInfo") or [] if isinstance(p, dict)]
        # Taken out of the answer (its caller still holds it), so each parsed
        # partition goes before the next one is read.
        page: Any = answer.pop("data", None)
        index = 0
        while True:
            cut = self._take(result, page, row_limit=row_limit, byte_limit=byte_limit)
            page = None
            if cut:
                break
            index += 1
            if index >= len(partitions) or index >= MAX_PARTITIONS:
                break
            if handle is None:
                raise SnowflakeAPIError(
                    "the SQL API answered more partitions without a usable statementHandle"
                )
            if result.result_bytes + _size(partitions[index]) > byte_limit:
                result.truncated = TRUNCATED_MAX_RESULT_BYTES
                break
            _, more = self._json(
                "GET",
                f"{STATEMENTS_PATH}/{quote(handle, safe='')}",
                {"partition": index},
            )
            page = more.pop("data", None)
            del more
        return result

    @staticmethod
    def _take(
        result: StatementResult, page: Any, *, row_limit: int, byte_limit: int
    ) -> bool:
        """Add ``page``'s rows within the caps; True when a cap stopped it."""
        for row in iter_rows(result.columns, page):
            if result.count >= row_limit:
                result.truncated = TRUNCATED_MAX_ROWS
                return True
            encoded = canonical(row)
            if result.plan is not None:
                cost = result.plan.cost(len(encoded))
            else:
                cost = len(encoded) + 1
            if result.result_bytes + cost > byte_limit:
                result.truncated = TRUNCATED_MAX_RESULT_BYTES
                return True
            if result.encoded is None:
                result.rows.append(row)
            else:
                result.encoded.append(encoded)
            if result.plan is not None:
                result.plan.add(len(encoded))
            result.result_bytes += cost
        return False

    def _cancel(self, handle: str) -> None:
        """Best effort: Snowflake stops the statement at its own ``timeout``
        anyway, so a cancel that fails (or has no time left) is dropped."""
        remaining = self.remaining()
        if remaining is not None and remaining <= 0.5:
            return
        try:
            self._json("POST", f"{STATEMENTS_PATH}/{quote(handle, safe='')}/cancel", {})
        except SnowflakeAPIError:
            pass

    # -- transport -----------------------------------------------------------

    def _auth(self) -> dict[str, str]:
        if self._token is None:
            try:
                self._token, self._token_from_cache = bearer_token(
                    self._creds, self._session, timeout=self._request_timeout()
                )
            except SnowflakeTokenError as e:
                raise _token_failure(e) from None
            self._sent_tokens.add(self._token)
        return {
            "Authorization": f"Bearer {self._token}",
            "X-Snowflake-Authorization-Token-Type": self._creds.token_type,
        }

    def _send(self, method: str, path: str, params: dict[str, Any], body: Any) -> Any:
        headers = {
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
            **self._auth(),
        }
        kwargs: dict[str, Any] = {}
        if body is not None:
            headers["Content-Type"] = "application/json"
            kwargs["data"] = json.dumps(body).encode()
        try:
            return self._session.request(
                method,
                f"{self._creds.base_url}{path}",
                params=params,
                headers=headers,
                timeout=self._request_timeout(),
                allow_redirects=False,
                **kwargs,
            )
        except requests.RequestException as e:
            if timed_out(e):
                raise SnowflakeAPIError(
                    "Snowflake did not answer in time", timed_out=True
                ) from e
            raise SnowflakeAPIError(
                f"could not complete the request to Snowflake ({type(e).__name__})"
            ) from e

    def _json(
        self,
        method: str,
        path: str,
        params: dict[str, Any],
        *,
        body: Any = None,
        polling: bool = False,
    ) -> tuple[int, dict[str, Any]]:
        """``(status, JSON object)`` for a 200 or 202 (and, while
        ``polling``, a 429: still running). Anything else raises."""
        resp = self._send(method, path, params, body)
        if resp.status_code == 401 and self._token_from_cache:
            # An OAuth token from the cache that Snowflake no longer takes
            # (revoked, or the integration changed): drop it and retry once
            # with a new one. A retried POST keeps its requestId, and
            # retry=true makes Snowflake answer the first submission's
            # statement instead of running it twice.
            resp.close()
            forget_token(self._creds, self._token)
            self._token = None
            if method == "POST" and "requestId" in params:
                params = {**params, "retry": "true"}
            resp = self._send(method, path, params, body)
        try:
            status = resp.status_code
            if status == 401:
                forget_token(self._creds, self._token)
                self._token = None
            if 300 <= status < 400:
                raise SnowflakeAPIError(
                    f"Snowflake answered a redirect ({status}); redirects are not followed",
                    status=status,
                )
            if status == 429 and polling:
                hint = retry_after_seconds((resp.headers or {}).get("Retry-After"))
                return 429, {"_retry_after": hint, **self._handle_of(params, path)}
            try:
                data = resp.json()
            except (ValueError, requests.RequestException):
                data = None
            if status in (200, 202):
                if not isinstance(data, dict):
                    raise SnowflakeAPIError(
                        "Snowflake answered with a body that is not a JSON object",
                        status=status,
                    )
                return status, data
            raise self._failure(status, data, resp)
        finally:
            resp.close()

    @staticmethod
    def _handle_of(params: dict[str, Any], path: str) -> dict[str, Any]:
        """The handle a status poll was for (a 429 carries no body)."""
        handle = path.rsplit("/", 1)[-1]
        return {"statementHandle": handle} if _HANDLE.match(handle) else {}

    def _failure(self, status: int, data: Any, resp: Any) -> SnowflakeAPIError:
        info = data if isinstance(data, dict) else {}
        code = info.get("code") if isinstance(info.get("code"), str) else None
        code = code if code is not None and _CODE.match(code) else None
        state = info.get("sqlState") if isinstance(info.get("sqlState"), str) else None
        state = state if state is not None and _CODE.match(state) else None
        message = info.get("message")
        detail = self.redact(message if isinstance(message, str) else "")
        label = ", ".join(x for x in (str(status), code) if x)
        if status == 408:
            return SnowflakeAPIError(
                "the statement reached its timeout and Snowflake cancelled it",
                status=status,
                code=code,
                sql_state=state,
                timed_out=True,
            )
        if status == 422:
            return SnowflakeAPIError(
                f"the statement failed ({label})",
                detail=detail,
                status=status,
                code=code,
                sql_state=state,
            )
        return SnowflakeAPIError(
            f"Snowflake failed ({label})",
            detail=detail,
            status=status,
            code=code,
            sql_state=state,
            credential_refused=status == 401,
            retry_after=retry_after_seconds((resp.headers or {}).get("Retry-After"))
            if status in (429, 503)
            else None,
        )

    def redact(self, text: str) -> str:
        for secret in (*self._sent_tokens, *self._creds.secrets):
            if secret:
                text = text.replace(secret, "[redacted]")
        return " ".join(text.split())[:_MAX_DETAIL]


def _handle(answer: dict[str, Any]) -> str:
    handle = answer.get("statementHandle")
    if not isinstance(handle, str) or not _HANDLE.match(handle):
        raise SnowflakeAPIError("the SQL API answered without a usable statementHandle")
    return handle


def _size(partition: dict[str, Any]) -> int:
    value = partition.get("uncompressedSize")
    return value if isinstance(value, int) and value >= 0 else 0


__all__ = [
    "QUERY_TAG",
    "STATEMENTS_PATH",
    "TRUNCATED_MAX_RESULT_BYTES",
    "TRUNCATED_MAX_ROWS",
    "SnowflakeAPIError",
    "SnowflakeClient",
    "StatementResult",
]
