"""A small read-only client for the BigQuery REST API (v2).

Endpoints (cloud.google.com/bigquery/docs/reference/rest/v2), all under
``https://bigquery.googleapis.com/bigquery/v2`` (a fixed host):

* ``GET  /projects/{p}/datasets`` (datasets.list)
* ``GET  /projects/{p}/datasets/{d}/tables`` (tables.list)
* ``GET  /projects/{p}/datasets/{d}/tables/{t}`` (tables.get)
* ``GET  /projects/{p}/datasets/{d}/tables/{t}/data`` (tabledata.list)
* ``POST /projects/{p}/queries`` (jobs.query: the dry run, then the query)
* ``GET  /projects/{p}/queries/{jobId}`` (jobs.getQueryResults)

Every one of them accepts the ``bigquery.readonly`` scope the token
carries. Jobs always run in the tenant's ``project_id``, at its
``location``. Redirects are never followed. Every failure, the token
exchange's included, is a :class:`BigQueryAPIError` with BigQuery's
``reason`` (``accessDenied``, ``rateLimitExceeded``, ...) when it gave one,
and ``timed_out`` for a timeout. A 401 drops the token from the cache, and a
call whose cached token was rejected retries once with a new one. Error
messages never carry the token or the key.

Row pages (tabledata.list, jobs.query, jobs.getQueryResults) are read with a
:class:`PageCap`: the body is streamed, and its bytes and cells are counted
as they arrive, before any of it is parsed. A body over the cap is dropped
(:class:`PageTooLarge`); the caller asks for fewer rows.

Time budget: with ``budget`` (seconds; the host's
``config["timeout_seconds"]``) the whole call fits inside it, from the
client's creation on. Every request, the token POST included, gets the time
remaining as its timeout (never more than ``timeout``), and a call with no
time left fails ``timed_out`` before it sends anything. A query's job
deadline (:meth:`BigQueryClient.job_seconds`) is the smaller of its own
timeout and the budget left; BigQuery is told it as ``jobTimeoutMs``.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Any, Self
from urllib.parse import quote

import requests
import urllib3.exceptions

from aisquare_pipe_bigquery._http import retry_after_seconds, timed_out
from aisquare_pipe_bigquery.credentials import (
    TOKEN_RATE_LIMITED,
    TOKEN_REFUSED,
    TOKEN_TIMED_OUT,
    BigQueryCredentials,
    BigQueryTokenError,
    forget_token,
    obtain_token,
)
from aisquare_pipe_bigquery.refs import TableRef

API_ROOT = "https://bigquery.googleapis.com/bigquery/v2"
DEFAULT_TIMEOUT = 30.0
#: The longest one jobs.query or getQueryResults call waits for the job
#: server-side (``timeoutMs``) before it answers "not complete yet".
MAX_WAIT_SECONDS = 10.0
#: How long to pause before polling again when BigQuery answered "not
#: complete" sooner than that (it may), so a poll never spins.
POLL_PAUSE_SECONDS = 1.0
#: Labels on every query job, so the customer can find AISquare's jobs
#: (INFORMATION_SCHEMA.JOBS, billing exports).
JOB_LABELS = {"client": "aisquare-pipe"}
_JOB_ID = re.compile(r"^[A-Za-z0-9_-]{1,1024}$")
_LOCATION = re.compile(r"^[A-Za-z0-9-]{1,64}$")
_MAX_DETAIL = 500
#: A row page's body is read this many bytes at a time.
READ_CHUNK = 64 * 1024
#: What a dropped page's remainder is read through at most, discarded as it
#: is read, to measure it (:attr:`PageCap.measure`) or to find its job:
#: past BigQuery's own page limits (about 10 MB and 20 MB).
DRAIN_BYTES = 64 * 1024 * 1024
#: Every REST cell is one ``{"v": ...}`` object (a ``REPEATED`` column's every
#: element and a ``RECORD``'s every field too): its key, counted in the raw
#: JSON. Inside a JSON string a quote is escaped, so this is only ever a key.
_CELL_KEY = b'"v":'
#: Every REST row is one ``{"f": [...]}`` object (and so is every ``RECORD``
#: value): counted the same way, at least the rows a body has begun.
_ROW_KEY = b'"f":'
#: A query answer's job (``{"projectId", "jobId", "location"}``, flat).
_JOB_REFERENCE = re.compile(rb'"jobReference"\s*:\s*(\{[^{}]{0,4096}\})')
_JOB_TAIL = 8192


class BigQueryAPIError(Exception):
    """A failed BigQuery call. ``status`` is the HTTP status (None when
    nothing came back); ``reason`` BigQuery's error reason, if any;
    ``retry_after`` is set for a 429/503 that said when to retry;
    ``timed_out`` for a timeout; ``key_refused`` when Google's token
    endpoint refused the service account key itself; ``www_authenticate``
    the answer's ``WWW-Authenticate`` header.

    ``message`` (the str) is ``summary``, the connector's own words, then
    ``detail``, BigQuery's own error text when it gave one (redacted and
    bounded). That text can quote the statement and name tables, so only
    the toolkit, which answers the caller, shows it; the source's errors
    carry ``summary`` alone (hosts log those)."""

    def __init__(
        self,
        message: str,
        *,
        detail: str = "",
        status: int | None = None,
        reason: str | None = None,
        retry_after: float | None = None,
        timed_out: bool = False,
        key_refused: bool = False,
        www_authenticate: str | None = None,
    ) -> None:
        full = f"{message}: {detail}" if detail else message
        super().__init__(full)
        self.message = full
        self.summary = message
        self.detail = detail
        self.status = status
        self.reason = reason
        self.retry_after = retry_after
        self.timed_out = timed_out
        self.key_refused = key_refused
        self.www_authenticate = www_authenticate


@dataclass(frozen=True)
class PageCap:
    """The most one row page's body may hold, counted as it is read and
    before it is parsed: ``max_bytes`` of JSON and ``max_cells`` cells (its
    ``"v":`` keys: what :func:`~aisquare_pipe_bigquery.rows.rest_cells`
    counts, and what parsing costs). ``measure``: when the body is over the
    cap, read the rest of it too, discarding it, so :class:`PageTooLarge`
    says how large the whole body is."""

    max_bytes: int
    max_cells: int
    measure: bool = False


class PageTooLarge(BigQueryAPIError):
    """A row page whose body went over its :class:`PageCap`: it was dropped
    unparsed, and the caller asks for fewer rows. ``body_bytes``, ``cells``
    and ``rows`` (its ``"f":`` keys: the rows it began, and every ``RECORD``
    value) are what was read of it; with ``complete``, the whole body's.
    ``job`` is the answer's ``jobReference`` when it was asked for and could
    be found (a jobs.query answer: the job to page from).

    A :class:`BigQueryAPIError` (upstream, 502) should one ever reach a
    host: the readers catch it."""

    def __init__(
        self,
        body_bytes: int,
        cells: int,
        rows: int,
        *,
        complete: bool,
        job: dict[str, Any] | None,
    ) -> None:
        super().__init__("a BigQuery page was larger than one read may hold")
        self.body_bytes = body_bytes
        self.cells = cells
        self.rows = rows
        self.complete = complete
        self.job = job


class _Counts:
    """The cells (``"v":`` keys) and rows (``"f":`` keys) of a body read in
    chunks: a key split across two chunks is counted once."""

    def __init__(self) -> None:
        self.cells = 0
        self.rows = 0
        self._tail = b""

    def add(self, chunk: bytes) -> None:
        window = self._tail + chunk
        self.cells += window.count(_CELL_KEY)
        self.rows += window.count(_ROW_KEY)
        # Both keys are 4 bytes: a 3-byte tail never holds a whole one.
        self._tail = window[-(len(_CELL_KEY) - 1) :]


def _job_reference(data: bytes | bytearray) -> dict[str, Any] | None:
    match = _JOB_REFERENCE.search(data)
    if match is None:
        return None
    try:
        job = json.loads(match.group(1))
    except ValueError:
        return None
    if not isinstance(job, dict) or not isinstance(job.get("jobId"), str):
        return None
    return job


def _segment(value: str) -> str:
    """One URL path segment (``/`` and ``:`` encoded too)."""
    if value in (".", ".."):
        raise BigQueryAPIError(f"{value!r} is not an id")
    return quote(value, safe="")


def _transport_error(e: BaseException, what: str) -> BigQueryAPIError:
    if timed_out(e):
        return BigQueryAPIError(
            f"BigQuery did not answer in time ({what})", timed_out=True
        )
    return BigQueryAPIError(f"could not {what} ({type(e).__name__})")


def _token_failure(e: BigQueryTokenError) -> BigQueryAPIError:
    """A token the endpoint didn't issue, as the BigQuery failure it is:
    only a refused key is 401 (unauthorized); a timeout stays a timeout, and
    an unreachable or failing endpoint is an upstream error a host may
    retry."""
    if e.kind == TOKEN_REFUSED:
        return BigQueryAPIError(
            e.summary, detail=e.detail, status=401, key_refused=True
        )
    if e.kind == TOKEN_TIMED_OUT:
        return BigQueryAPIError(e.summary, detail=e.detail, timed_out=True)
    if e.kind == TOKEN_RATE_LIMITED:
        return BigQueryAPIError(
            e.summary, detail=e.detail, status=429, retry_after=e.retry_after
        )
    return BigQueryAPIError(e.summary, detail=e.detail, retry_after=e.retry_after)


def _monotonic() -> float:
    """The clock a budget runs on (tests replace it)."""
    return time.monotonic()


class BigQueryClient:
    """One credential's view of BigQuery. Use it as a context manager: a
    session it opened itself is closed on exit; a passed-in ``session``
    (tests, or a host that pools connections) is left open.

    ``timeout`` bounds each request; ``budget`` (None: unbounded) bounds the
    whole call, every request together (see the module docstring)."""

    def __init__(
        self,
        credentials: BigQueryCredentials,
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
        #: Every token this client sent: redacted from any error text.
        self._sent_tokens: set[str] = set()
        #: The bytes of the last JSON body read (a row page is sized from them).
        self.last_body_bytes = 0

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        if self._own_session:
            self._session.close()

    @property
    def project_id(self) -> str:
        return self._creds.project_id

    # -- time ----------------------------------------------------------------

    def now(self) -> float:
        return self._monotonic()

    def pause(self, seconds: float) -> None:
        if seconds > 0:
            self._sleep(seconds)

    def remaining(self) -> float | None:
        """Seconds left in the budget (None: unbounded)."""
        if self._deadline is None:
            return None
        return self._deadline - self._monotonic()

    def out_of_time(self) -> BigQueryAPIError:
        return BigQueryAPIError(
            f"the BigQuery call ran out of its {self.budget:g}s time budget",
            timed_out=True,
        )

    def _request_timeout(self) -> float:
        """The next request's timeout: ``timeout``, or the time left in the
        budget when that is less. No time left fails the call (``timed_out``)."""
        remaining = self.remaining()
        if remaining is None:
            return self._timeout
        if remaining <= 0:
            raise self.out_of_time()
        return min(self._timeout, remaining)

    def job_seconds(self, job_timeout: float) -> float:
        """A query's job deadline: ``job_timeout``, or the budget left when
        that is less. Under a second left fails the call before the job is
        started."""
        remaining = self.remaining()
        seconds = job_timeout if remaining is None else min(job_timeout, remaining)
        if seconds < 1:
            raise self.out_of_time()
        return seconds

    def wait_ms(self, job_left: float) -> int:
        """How long the next jobs.query/getQueryResults may wait server-side:
        at most :data:`MAX_WAIT_SECONDS`, the job's time left, and a second
        under the request's own timeout (so the answer arrives before it)."""
        seconds = min(MAX_WAIT_SECONDS, job_left, self._request_timeout() - 1.0)
        return max(0, int(seconds * 1000))

    # -- endpoints -----------------------------------------------------------

    def _project_url(self, project: str | None = None) -> str:
        return f"{API_ROOT}/projects/{_segment(project or self._creds.project_id)}"

    def list_datasets(
        self, project: str | None, *, page_size: int, page_token: str | None = None
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"maxResults": page_size}
        if page_token:
            params["pageToken"] = page_token
        return self._json("GET", f"{self._project_url(project)}/datasets", params)

    def list_tables(
        self,
        project: str | None,
        dataset: str,
        *,
        page_size: int,
        page_token: str | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"maxResults": page_size}
        if page_token:
            params["pageToken"] = page_token
        url = f"{self._project_url(project)}/datasets/{_segment(dataset)}/tables"
        return self._json("GET", url, params)

    def _table_url(self, ref: TableRef) -> str:
        return (
            f"{self._project_url(ref.project)}/datasets/{_segment(ref.dataset)}"
            f"/tables/{_segment(ref.table)}"
        )

    def get_table(self, ref: TableRef) -> dict[str, Any]:
        return self._json("GET", self._table_url(ref), {})

    def list_rows(
        self,
        ref: TableRef,
        *,
        page_size: int,
        page_token: str | None = None,
        start_index: int | None = None,
        selected_fields: list[str] | None = None,
        cap: PageCap | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "maxResults": page_size,
            "formatOptions.useInt64Timestamp": "true",
        }
        if page_token:
            params["pageToken"] = page_token
        elif start_index:
            params["startIndex"] = start_index
        if selected_fields:
            params["selectedFields"] = ",".join(selected_fields)
        return self._json("GET", f"{self._table_url(ref)}/data", params, cap=cap)

    def _query_body(
        self, sql: str, default_dataset: tuple[str | None, str] | None
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"query": sql, "useLegacySql": False}
        if self._creds.location:
            body["location"] = self._creds.location
        if default_dataset is not None:
            project, dataset = default_dataset
            body["defaultDataset"] = {
                "projectId": project or self._creds.project_id,
                "datasetId": dataset,
            }
        return body

    def dry_run(
        self, sql: str, *, default_dataset: tuple[str | None, str] | None = None
    ) -> dict[str, Any]:
        """jobs.query with ``dryRun``: BigQuery validates the statement and
        says how many bytes it would process. Nothing runs or is billed."""
        body = {**self._query_body(sql, default_dataset), "dryRun": True}
        return self._json("POST", f"{self._project_url()}/queries", {}, body=body)

    def start_query(
        self,
        sql: str,
        *,
        page_size: int,
        max_bytes_billed: int,
        job_timeout_ms: int,
        wait_ms: int,
        request_id: str,
        default_dataset: tuple[str | None, str] | None = None,
        cap: PageCap | None = None,
    ) -> dict[str, Any]:
        """jobs.query: starts the job (``maximumBytesBilled`` and
        ``jobTimeoutMs`` set, so BigQuery itself enforces the byte cap and
        the deadline) and waits up to ``wait_ms`` for its first page. A first
        page over ``cap`` is :class:`PageTooLarge` with the job it named."""
        body = {
            **self._query_body(sql, default_dataset),
            "maxResults": page_size,
            "timeoutMs": wait_ms,
            "jobTimeoutMs": str(job_timeout_ms),
            "maximumBytesBilled": str(max_bytes_billed),
            "requestId": request_id,
            "useQueryCache": True,
            "createSession": False,
            "labels": dict(JOB_LABELS),
            "formatOptions": {"useInt64Timestamp": True},
        }
        return self._json(
            "POST", f"{self._project_url()}/queries", {}, body=body, cap=cap, job=True
        )

    def query_results(
        self,
        job: dict[str, Any],
        *,
        page_size: int,
        page_token: str | None = None,
        wait_ms: int | None = None,
        cap: PageCap | None = None,
    ) -> dict[str, Any]:
        """jobs.getQueryResults for the job ``start_query`` started (its
        ``jobReference``): the job's state, then its pages."""
        job_id = job.get("jobId") if isinstance(job, dict) else None
        if not isinstance(job_id, str) or not _JOB_ID.match(job_id):
            raise BigQueryAPIError("BigQuery answered without a usable job reference")
        params: dict[str, Any] = {
            "maxResults": page_size,
            "formatOptions.useInt64Timestamp": "true",
        }
        location = job.get("location") or self._creds.location
        if isinstance(location, str) and _LOCATION.match(location):
            params["location"] = location
        if page_token:
            params["pageToken"] = page_token
        if wait_ms is not None:
            params["timeoutMs"] = wait_ms
        url = f"{self._project_url()}/queries/{_segment(job_id)}"
        return self._json("GET", url, params, cap=cap)

    # -- transport -----------------------------------------------------------

    def _auth(self) -> dict[str, str]:
        if self._token is None:
            try:
                self._token, self._token_from_cache = obtain_token(
                    self._creds, self._session, timeout=self._request_timeout()
                )
            except BigQueryTokenError as e:
                raise _token_failure(e) from None
            self._sent_tokens.add(self._token)
        return {"Authorization": f"Bearer {self._token}"}

    def _drop_token(self) -> None:
        forget_token(self._creds, self._token)
        self._token = None

    def _send(
        self,
        method: str,
        url: str,
        params: dict[str, Any],
        body: Any,
        stream: bool = False,
    ) -> Any:
        headers = {"Accept": "application/json", **self._auth()}
        kwargs: dict[str, Any] = {}
        if body is not None:
            headers["Content-Type"] = "application/json"
            kwargs["data"] = json.dumps(body).encode()
        if stream:
            kwargs["stream"] = True
        try:
            return self._session.request(
                method,
                url,
                params=params,
                headers=headers,
                timeout=self._request_timeout(),
                allow_redirects=False,
                **kwargs,
            )
        except requests.RequestException as e:
            # Bodies that aren't streamed: a connect failure and an answer
            # that broke off midway both surface here (a streamed row page's
            # body breaking off surfaces in _read_page).
            raise _transport_error(e, "complete the request to BigQuery") from e

    def _call(
        self,
        method: str,
        url: str,
        params: dict[str, Any],
        body: Any = None,
        stream: bool = False,
    ) -> Any:
        resp = self._send(method, url, params, body, stream)
        if resp.status_code == 401:
            # Google no longer takes this token (key deleted or disabled,
            # account disabled, token revoked): drop it so no later call
            # reuses it for the rest of its hour. A token from the cache gets
            # one retry with a new one; a new one that fails is final. A
            # retried jobs.query carries the same requestId, so BigQuery
            # runs it once.
            from_cache = self._token_from_cache
            self._drop_token()
            if from_cache:
                resp.close()
                resp = self._send(method, url, params, body, stream)
                if resp.status_code == 401:
                    self._drop_token()
        status = resp.status_code
        if 300 <= status < 400:
            resp.close()
            raise BigQueryAPIError(
                f"BigQuery answered a redirect ({status}); redirects are not followed",
                status=status,
            )
        if status >= 400:
            message, reason = self._detail(resp)
            resp.close()
            headers = resp.headers or {}
            challenge = headers.get("WWW-Authenticate")
            raise BigQueryAPIError(
                f"BigQuery failed ({status}{', ' + reason if reason else ''})",
                detail=message,
                status=status,
                reason=reason,
                retry_after=retry_after_seconds(headers.get("Retry-After"))
                if status in (429, 503)
                else None,
                www_authenticate=challenge if isinstance(challenge, str) else None,
            )
        return resp

    def _json(
        self,
        method: str,
        url: str,
        params: dict[str, Any],
        *,
        body: Any = None,
        cap: PageCap | None = None,
        job: bool = False,
    ) -> dict[str, Any]:
        """The answer's JSON object. With ``cap`` (a row page), its body is
        read within it (:meth:`_read_page`) and only then parsed; ``job``:
        a body over it names its ``jobReference``."""
        resp = self._call(method, url, params, body, stream=cap is not None)
        try:
            try:
                if cap is not None:
                    data = self._page_json(resp, cap, job=job)
                else:
                    content = getattr(resp, "content", None)
                    self.last_body_bytes = (
                        len(content) if isinstance(content, bytes) else 0
                    )
                    data = resp.json()
            except ValueError as e:
                raise BigQueryAPIError(
                    "BigQuery answered with a body that is not JSON",
                    status=resp.status_code,
                ) from e
        finally:
            resp.close()
        if not isinstance(data, dict):
            raise BigQueryAPIError(
                "BigQuery answered with an unexpected body", status=resp.status_code
            )
        return data

    def _page_json(self, resp: Any, cap: PageCap, *, job: bool) -> Any:
        """A row page's JSON, read within ``cap`` (:meth:`_read_page`). Its
        bytes are decoded, then let go before it is parsed, so they and its
        objects are never held at once. (A body that isn't JSON, or isn't
        text, is a ``ValueError``.)"""
        body = self._read_page(resp, cap, job=job)
        self.last_body_bytes = len(body)
        text = body.decode(json.detect_encoding(body), "surrogatepass")
        del body
        return json.loads(text)

    def _read_page(self, resp: Any, cap: PageCap, *, job: bool) -> bytearray:
        """A streamed row page's body, read :data:`READ_CHUNK` bytes at a
        time while its bytes and cells (``"v":`` keys) are within ``cap``.
        Over either, it is dropped: :class:`PageTooLarge`. A body that breaks
        off is a transport failure, as for any other call."""
        body = bytearray()
        counts = _Counts()
        chunks: Iterator[bytes] = resp.iter_content(READ_CHUNK)
        try:
            for chunk in chunks:
                counts.add(chunk)
                body += chunk
                if len(body) > cap.max_bytes or counts.cells > cap.max_cells:
                    raise self._dropped(body, counts, chunks, cap, job)
        except (requests.RequestException, urllib3.exceptions.HTTPError, OSError) as e:
            raise _transport_error(e, "read BigQuery's answer") from None
        return body

    @staticmethod
    def _dropped(
        body: bytearray,
        counts: _Counts,
        chunks: Iterator[bytes],
        cap: PageCap,
        want_job: bool,
    ) -> PageTooLarge:
        """The :class:`PageTooLarge` for a body over ``cap``: its job from
        what was read, and with ``measure`` (or a job still to find) the
        rest read through, discarded as it comes, up to :data:`DRAIN_BYTES`."""
        found = _job_reference(body) if want_job else None
        size = len(body)
        job_tail = bytes(body[-_JOB_TAIL:])
        body.clear()
        find = want_job and found is None
        if not (cap.measure or find):
            return PageTooLarge(
                size, counts.cells, counts.rows, complete=False, job=found
            )
        complete = True
        for chunk in chunks:
            counts.add(chunk)
            size += len(chunk)
            if find:
                job_window = job_tail + chunk
                found = _job_reference(job_window)
                find = found is None
                job_tail = job_window[-_JOB_TAIL:]
            if not (cap.measure or find):
                complete = False
                break
            if size > DRAIN_BYTES:
                complete = False
                break
        return PageTooLarge(
            size, counts.cells, counts.rows, complete=complete, job=found
        )

    def redact(self, text: str) -> str:
        for token in self._sent_tokens:
            text = text.replace(token, "[redacted]")
        return " ".join(text.split())[:_MAX_DETAIL]

    def _detail(self, resp: Any) -> tuple[str, str | None]:
        """BigQuery's error message and reason (``{"error": {"message",
        "errors": [{"reason", ...}]}}``), redacted and bounded."""
        try:
            data = resp.json()
        except (ValueError, requests.RequestException):
            data = None
        return error_detail(data, self.redact)


def error_detail(
    data: Any, redact: Callable[[str], str] = lambda s: s
) -> tuple[str, str | None]:
    """``(message, reason)`` from a BigQuery error body or an ``errors``
    list (a job's errors), redacted."""
    text, reason = "", None
    error = data.get("error") if isinstance(data, dict) else None
    items: Any = None
    if isinstance(error, dict):
        if isinstance(error.get("message"), str):
            text = error["message"]
        items = error.get("errors")
    elif isinstance(error, str):
        text = error
    elif isinstance(data, list):
        items = data
    if isinstance(items, list) and items and isinstance(items[0], dict):
        first = items[0]
        if isinstance(first.get("reason"), str) and re.match(
            r"^[A-Za-z]{1,64}$", first["reason"]
        ):
            reason = first["reason"]
        if not text and isinstance(first.get("message"), str):
            text = first["message"]
    return redact(text), reason


__all__ = [
    "API_ROOT",
    "DRAIN_BYTES",
    "JOB_LABELS",
    "MAX_WAIT_SECONDS",
    "POLL_PAUSE_SECONDS",
    "READ_CHUNK",
    "BigQueryAPIError",
    "BigQueryClient",
    "PageCap",
    "PageTooLarge",
    "error_detail",
]
