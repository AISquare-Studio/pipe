"""A small read-only client for one Databricks workspace's REST API.

Endpoints (docs.databricks.com/api):

* Files API (Unity Catalog volumes): ``GET /api/2.0/fs/directories{path}``
  (``contents``, ``next_page_token``), ``GET /api/2.0/fs/files{path}`` (the
  bytes; ``Last-Modified``, ``ETag``, ``Content-Length``),
  ``HEAD /api/2.0/fs/files{path}`` (those headers only).
* DBFS: ``GET /api/2.0/dbfs/list?path``, ``GET /api/2.0/dbfs/get-status?path``,
  ``GET /api/2.0/dbfs/read?path&offset&length`` (base64, at most 1 MB a read).
* SQL Statement Execution: ``POST /api/2.0/sql/statements`` then
  ``GET /api/2.0/sql/statements/{id}`` until a terminal state;
  ``POST /api/2.0/sql/statements/{id}/cancel`` when the deadline passes
  (:meth:`DatabricksClient.run_statement` says how the deadline holds).
* Unity Catalog: ``GET /api/2.1/unity-catalog/{catalogs|schemas|tables}``.

Every request goes to the credential's workspace host only, with its bearer
token. Redirects are never followed. Downloads stop at ``max_bytes`` and
accumulate in one buffer. Error messages never carry a secret; a
:class:`DatabricksAPIError`'s ``summary`` carries none of Databricks's own
text either.

Time budget: with ``budget`` (seconds; the host's ``config["timeout_seconds"]``)
the whole call fits inside it, from the client's creation on. Every request,
the OAuth token's included, gets at most the time left as its timeout, and a
request with no time left (under :data:`MIN_REQUEST_TIMEOUT`) fails
``timed_out`` before it is sent. So a DBFS read, 1 MB a request, stops at the
deadline. requests' timeout bounds each connect and each socket read, not a
whole body, so a volume download also checks the deadline before every read:
with urllib3 2 each read is ONE socket read (``read1``), sent with the time
left as its socket timeout, so a body that trickles in still stops by the
deadline; with urllib3 1.26 (no ``read1``), the deadline is checked between
64 KiB chunks. A statement's own deadline (:meth:`DatabricksClient.run_statement`)
is cut to the budget too.
"""

from __future__ import annotations

import base64
import binascii
import io
import time
from collections.abc import Callable, Iterator
from typing import Any, Self
from urllib.parse import quote

import requests
import urllib3

from aisquare_pipe_databricks.credentials import (
    DatabricksCredentials,
    DatabricksTokenError,
    bearer_token,
)

DEFAULT_TIMEOUT = 30.0
#: The statement API's ``wait_timeout``: ``0s`` (asynchronous), or 5 to 50 s.
API_MIN_WAIT = 5
API_MAX_WAIT = 50
#: How much longer than ``wait_timeout`` the submit's HTTP read timeout is:
#: the warehouse answers when ``wait_timeout`` passes, and this covers that
#: answer and the network.
ANSWER_SLACK = 5.0
#: Kept back from a statement's deadline for its cancel (at most a quarter of
#: the deadline).
CANCEL_RESERVE = 2.0
#: The shortest timeout a request is sent with.
MIN_REQUEST_TIMEOUT = 0.2
#: DBFS returns at most 1 MB per read.
DBFS_READ_CHUNK = 1024 * 1024
_STREAM_CHUNK = 64 * 1024
_MAX_DETAIL = 300
#: SQL states after which the statement will not change.
TERMINAL_STATES = frozenset({"SUCCEEDED", "FAILED", "CANCELED", "CLOSED"})


class DatabricksAPIError(Exception):
    """A failed Databricks call. ``status`` is the HTTP status (None when
    nothing came back); ``retry_after`` is set for a 429/503 that said when.
    ``message`` may end with Databricks's own error text (secrets redacted),
    for the toolkit's caller; ``summary`` is the message without it, for a
    host's logs (Databricks's text can echo paths, names or values)."""

    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        retry_after: float | None = None,
        timed_out: bool = False,
        summary: str | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.summary = message if summary is None else summary
        self.status = status
        self.retry_after = retry_after
        self.timed_out = timed_out


class FileTooLarge(DatabricksAPIError):
    """A file is bigger than the caller's ``max_bytes``."""


def _retry_after(value: str | None) -> float | None:
    if not value:
        return None
    try:
        seconds = float(value)
    except ValueError:
        return None
    return seconds if 0 <= seconds <= 3600 else None


def files_path(path: str) -> str:
    """A ``/Volumes/...`` path for the Files API URL, each segment encoded."""
    return "/".join(quote(part, safe="") for part in path.split("/"))


class DatabricksClient:
    """One credential's view of its workspace. Use it as a context manager: a
    session it opened itself is closed on exit; a passed-in ``session`` (tests,
    or a host that pools connections) is left open. ``timeout`` bounds each
    request; ``budget`` (None: unbounded) bounds the whole call, every request
    together (the module docstring says how). ``clock`` (the budget's and
    statement polling's) and ``sleep`` are replaced by tests."""

    def __init__(
        self,
        credentials: DatabricksCredentials,
        *,
        session: Any = None,
        timeout: float = DEFAULT_TIMEOUT,
        budget: float | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._creds = credentials
        self._own_session = session is None
        self._session = session if session is not None else requests.Session()
        self._timeout = timeout
        self._clock = clock
        self._sleep = sleep
        self.budget = budget if budget is not None and budget > 0 else None
        self._deadline = None if self.budget is None else clock() + self.budget
        self._token: str | None = None

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        if self._own_session:
            self._session.close()

    # -- the time budget -------------------------------------------------------------

    def remaining(self) -> float | None:
        """Seconds left in the budget (None: unbounded)."""
        if self._deadline is None:
            return None
        return self._deadline - self._clock()

    def _out_of_time(self) -> DatabricksAPIError:
        return DatabricksAPIError(
            f"the Databricks call ran out of its {self.budget:g}s time budget",
            timed_out=True,
        )

    def _fitted(self, timeout: float | None) -> float:
        """A request's timeout: ``timeout`` (None: the client's), cut to what is
        left of the budget. With less than :data:`MIN_REQUEST_TIMEOUT` left,
        a timed-out :class:`DatabricksAPIError` instead: nothing is sent."""
        wanted = self._timeout if timeout is None else timeout
        remaining = self.remaining()
        if remaining is None:
            return wanted
        if remaining < MIN_REQUEST_TIMEOUT:
            raise self._out_of_time()
        return min(wanted, remaining)

    # -- Files API (Unity Catalog volumes) ------------------------------------------

    def list_directory(
        self, path: str, *, page_size: int, page_token: str | None = None
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"page_size": page_size}
        if page_token:
            params["page_token"] = page_token
        return self._json(
            "GET", f"/api/2.0/fs/directories{files_path(path)}", params=params
        )

    def file_metadata(self, path: str) -> dict[str, Any]:
        resp = self._request("HEAD", f"/api/2.0/fs/files{files_path(path)}")
        resp.close()
        return _file_headers(resp.headers)

    def download_file(
        self, path: str, *, max_bytes: int
    ) -> tuple[bytes, dict[str, Any]]:
        """The file's bytes, at most ``max_bytes``. They accumulate in one
        buffer whose ``getvalue()`` hands them over without a copy, so a
        download holds about one copy of the file, not its chunks and then
        their join."""
        resp = self._request("GET", f"/api/2.0/fs/files{files_path(path)}", stream=True)
        try:
            buffer = io.BytesIO()
            total = 0
            for chunk in self._body(resp):
                total += len(chunk)
                if total > max_bytes:
                    raise FileTooLarge(f"{path} is larger than {max_bytes} bytes")
                buffer.write(chunk)
            return buffer.getvalue(), _file_headers(resp.headers)
        except (requests.RequestException, urllib3.exceptions.HTTPError, OSError) as e:
            # The body broke off midway: a read timeout (requests wraps it in
            # ConnectionError), a reset (ChunkedEncodingError), ...
            raise _transport_error(e) from e
        finally:
            resp.close()

    def _body(self, resp: Any) -> Iterator[bytes]:
        """A streamed body, read by read, within the budget (the module
        docstring says how). Without a budget, requests' chunks."""
        raw = getattr(resp, "raw", None)
        read1 = getattr(raw, "read1", None)
        if self._deadline is None or not callable(read1):
            for chunk in resp.iter_content(_STREAM_CHUNK):
                if self._deadline is not None and self._clock() >= self._deadline:
                    raise self._out_of_time()
                yield chunk
            return
        sock = getattr(getattr(raw, "connection", None), "sock", None)
        settimeout = getattr(sock, "settimeout", None)
        while True:
            timeout = self._fitted(None)
            if callable(settimeout):
                settimeout(timeout)
            chunk = read1(_STREAM_CHUNK, decode_content=True)
            if not chunk:
                return
            yield chunk

    # -- DBFS ----------------------------------------------------------------------------

    def dbfs_list(self, path: str) -> dict[str, Any]:
        return self._json("GET", "/api/2.0/dbfs/list", params={"path": path})

    def dbfs_status(self, path: str) -> dict[str, Any]:
        return self._json("GET", "/api/2.0/dbfs/get-status", params={"path": path})

    def dbfs_read(self, path: str, *, size: int, max_bytes: int) -> bytes:
        if size > max_bytes:
            raise FileTooLarge(f"{path} is larger than {max_bytes} bytes")
        buffer = io.BytesIO()  # getvalue() hands the bytes over without a copy
        offset = 0
        while offset < size:
            body = self._json(
                "GET",
                "/api/2.0/dbfs/read",
                params={"path": path, "offset": offset, "length": DBFS_READ_CHUNK},
            )
            read = body.get("bytes_read")
            chunk = body.get("data")
            if not isinstance(read, int) or not isinstance(chunk, str) or read <= 0:
                break
            try:
                buffer.write(base64.b64decode(chunk, validate=True))
            except (binascii.Error, ValueError):
                raise DatabricksAPIError(
                    "DBFS answered a read whose data is not base64"
                ) from None
            offset += read
            if buffer.tell() > max_bytes:
                raise FileTooLarge(f"{path} is larger than {max_bytes} bytes")
        return buffer.getvalue()

    # -- SQL Statement Execution -----------------------------------------------------------

    def run_statement(
        self,
        statement: str,
        *,
        warehouse_id: str,
        row_limit: int,
        byte_limit: int,
        deadline_seconds: float,
        catalog: str | None = None,
        schema: str | None = None,
    ) -> dict[str, Any]:
        """Run one statement (already checked SELECT-only) and wait for it, at
        most ``deadline_seconds`` from now (never past the client's budget),
        the OAuth token included. Every request's timeout fits what is left;
        past the deadline the statement is cancelled and a timed-out
        :class:`DatabricksAPIError` raised.

        * With up to 55 s left (:data:`API_MAX_WAIT` + :data:`ANSWER_SLACK`),
          one synchronous call with ``on_wait_timeout`` ``CANCEL``, so the
          warehouse cancels the statement itself at ``wait_timeout``, even if
          its answer never arrives (a client timeout). ``wait_timeout`` is
          what is left less ``ANSWER_SLACK`` (5 to 50 s) and the HTTP read
          timeout ``wait_timeout`` + ``ANSWER_SLACK``, never past what is
          left. Under 10 s left, ``wait_timeout`` is the API's least, 5 s,
          and the read timeout is what is left: the client may give up
          first, and the statement still ends 5 s after it was sent.
        * With more (``timeout_seconds`` above 55), asynchronously:
          ``wait_timeout`` ``0s`` answers at once with the ``statement_id``,
          which is polled until the statement ends and cancelled when the
          deadline comes (:data:`CANCEL_RESERVE` is kept back for the
          cancel). A poll that fails or times out cancels it too.
        """
        started = self._clock()
        budget_left = self.remaining()
        if budget_left is not None:
            deadline_seconds = min(deadline_seconds, budget_left)

        def left() -> float:
            return deadline_seconds - (self._clock() - started)

        reserve = min(CANCEL_RESERVE, deadline_seconds / 4)
        self._auth(timeout=_fit(left() - reserve))
        remaining = left()
        if remaining - reserve < MIN_REQUEST_TIMEOUT:
            raise DatabricksAPIError(
                f"the statement could not start within {deadline_seconds:g}s",
                timed_out=True,
            )
        # Up to 55 s, synchronous: the warehouse cancels the statement itself
        # at wait_timeout, answered or not. Under 10 s that is 5 s (the API's
        # least) with a read timeout of what is left: the client may give up
        # first, and the statement still ends 5 s after it was sent.
        synchronous = remaining <= API_MAX_WAIT + ANSWER_SLACK
        body: dict[str, Any] = {
            "statement": statement,
            "warehouse_id": warehouse_id,
            "disposition": "INLINE",
            "format": "JSON_ARRAY",
            "row_limit": row_limit,
            "byte_limit": byte_limit,
        }
        if synchronous:
            wait = int(min(API_MAX_WAIT, max(API_MIN_WAIT, remaining - ANSWER_SLACK)))
            body["wait_timeout"] = f"{wait}s"
            body["on_wait_timeout"] = "CANCEL"
            timeout = min(wait + ANSWER_SLACK, remaining)
        else:
            wait = 0
            body["wait_timeout"] = "0s"
            body["on_wait_timeout"] = "CONTINUE"
            timeout = _fit(remaining - reserve)
        if catalog:
            body["catalog"] = catalog
        if schema:
            body["schema"] = schema
        try:
            result = self._json(
                "POST", "/api/2.0/sql/statements", body=body, timeout=timeout
            )
        except DatabricksAPIError as e:
            if not e.timed_out:
                raise
            then = (
                f"the warehouse cancels it after {wait}s (on_wait_timeout CANCEL)"
                if synchronous
                else "if it started, the warehouse's STATEMENT_TIMEOUT ends it"
            )
            raise DatabricksAPIError(
                f"Databricks did not answer the statement within {timeout:g}s; {then}",
                timed_out=True,
            ) from None
        if (
            synchronous
            and _state(result) == "CANCELED"
            and self._clock() - started >= wait - 1
        ):
            raise DatabricksAPIError(
                f"the statement did not finish within {wait}s and the warehouse "
                f"cancelled it (deadline {deadline_seconds:g}s)",
                timed_out=True,
            )
        delay = 0.5
        while _state(result) not in TERMINAL_STATES:
            statement_id = result.get("statement_id")
            if not isinstance(statement_id, str) or not statement_id:
                raise DatabricksAPIError(
                    "the statement API answered without a statement_id"
                )
            if left() - reserve >= MIN_REQUEST_TIMEOUT:
                self._sleep(min(delay, left() - reserve - MIN_REQUEST_TIMEOUT))
                delay = min(delay * 2, 5.0)
            if left() - reserve < MIN_REQUEST_TIMEOUT:
                # Only the reserve is left: it is the cancel's.
                self._cancel(statement_id, left)
                raise DatabricksAPIError(
                    f"the statement did not finish within {deadline_seconds:g}s and was cancelled",
                    timed_out=True,
                )
            try:
                result = self._json(
                    "GET",
                    f"/api/2.0/sql/statements/{quote(statement_id, safe='')}",
                    timeout=_fit(left() - reserve),
                )
            except DatabricksAPIError as e:
                self._cancel(statement_id, left)
                if e.timed_out:
                    raise DatabricksAPIError(
                        "Databricks did not answer a statement poll in time; the "
                        "statement was cancelled",
                        timed_out=True,
                    ) from None
                raise
        return result

    def _cancel(self, statement_id: str, left: Callable[[], float]) -> None:
        """Cancel the statement, on what is left of its deadline (the reserve)."""
        try:
            self._request(
                "POST",
                f"/api/2.0/sql/statements/{quote(statement_id, safe='')}/cancel",
                timeout=_fit(left()),
            ).close()
        except DatabricksAPIError:
            pass  # best effort: the deadline error is what the caller needs

    # -- Unity Catalog ---------------------------------------------------------------------

    def list_catalog(self, kind: str, params: dict[str, Any]) -> dict[str, Any]:
        return self._json("GET", f"/api/2.1/unity-catalog/{kind}", params=params)

    # -- transport -------------------------------------------------------------------------

    def _auth(self, *, timeout: float | None = None) -> dict[str, str]:
        if self._token is None:
            try:
                self._token = bearer_token(
                    self._creds, self._session, timeout=self._fitted(timeout)
                )
            except DatabricksTokenError as e:
                raise DatabricksAPIError(
                    str(e),
                    status=None if e.unreachable else 401,
                    timed_out=e.timed_out,
                ) from None
        return {"Authorization": f"Bearer {self._token}"}

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        body: dict[str, Any] | None = None,
        stream: bool = False,
        timeout: float | None = None,
    ) -> Any:
        headers = {"Accept": "application/json", **self._auth(timeout=timeout)}
        sent_with = self._fitted(timeout)
        try:
            resp = self._session.request(
                method,
                f"{self._creds.base_url}{path}",
                params=params,
                json=body,
                headers=headers,
                timeout=sent_with,
                allow_redirects=False,
                stream=stream,
            )
        except requests.Timeout as e:
            raise DatabricksAPIError(
                "Databricks did not answer in time", timed_out=True
            ) from e
        except requests.RequestException as e:
            raise DatabricksAPIError(
                f"could not reach Databricks ({type(e).__name__})"
            ) from e
        status = resp.status_code
        if 300 <= status < 400:
            resp.close()
            raise DatabricksAPIError(
                f"Databricks answered a redirect ({status}); redirects are not followed",
                status=status,
            )
        if status >= 400:
            detail = self._detail(resp)
            resp.close()
            raise DatabricksAPIError(
                f"Databricks failed ({status})" + detail,
                summary=f"Databricks failed ({status})",
                status=status,
                retry_after=_retry_after(resp.headers.get("Retry-After"))
                if status in (429, 503)
                else None,
            )
        return resp

    def _json(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        body: dict[str, Any] | None = None,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        resp = self._request(method, path, params=params, body=body, timeout=timeout)
        try:
            data = resp.json()
        except ValueError as e:
            raise DatabricksAPIError(
                "Databricks answered with a body that is not JSON",
                status=resp.status_code,
            ) from e
        if not isinstance(data, dict):
            raise DatabricksAPIError(
                "Databricks answered with an unexpected body", status=resp.status_code
            )
        return data

    def _detail(self, resp: Any) -> str:
        try:
            data = resp.json()
        except ValueError:
            data = None
        text = ""
        if isinstance(data, dict):
            for key in ("message", "error_code"):
                if isinstance(data.get(key), str):
                    text = data[key]
                    break
        for secret in (*self._creds.secrets, self._token or ""):
            if secret:
                text = text.replace(secret, "[redacted]")
        text = " ".join(text.split())[:_MAX_DETAIL]
        return f": {text}" if text else ""


def _fit(seconds: float) -> float:
    """A request timeout for ``seconds`` left: at most :data:`DEFAULT_TIMEOUT`,
    at least :data:`MIN_REQUEST_TIMEOUT`."""
    return max(MIN_REQUEST_TIMEOUT, min(DEFAULT_TIMEOUT, seconds))


def _transport_error(e: BaseException) -> DatabricksAPIError:
    """A body that broke off midway, as a :class:`DatabricksAPIError`."""
    timed_out = (
        isinstance(e, (requests.Timeout, urllib3.exceptions.ReadTimeoutError))
        or "timed out" in str(e).lower()
    )
    return DatabricksAPIError(
        f"the download from Databricks broke off ({type(e).__name__})",
        timed_out=timed_out,
    )


def _state(result: dict[str, Any]) -> str:
    status = result.get("status")
    state = status.get("state") if isinstance(status, dict) else None
    return state if isinstance(state, str) else ""


def _file_headers(headers: Any) -> dict[str, Any]:
    length = headers.get("Content-Length")
    try:
        size: int | None = int(length) if length is not None else None
    except (TypeError, ValueError):
        size = None
    return {
        "size": size,
        "content_type": headers.get("Content-Type"),
        "last_modified": headers.get("Last-Modified"),
        "etag": headers.get("ETag"),
    }


__all__ = [
    "ANSWER_SLACK",
    "API_MAX_WAIT",
    "API_MIN_WAIT",
    "CANCEL_RESERVE",
    "TERMINAL_STATES",
    "DatabricksAPIError",
    "DatabricksClient",
    "FileTooLarge",
    "files_path",
]
