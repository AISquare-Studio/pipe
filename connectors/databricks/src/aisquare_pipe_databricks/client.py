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
  ``POST /api/2.0/sql/statements/{id}/cancel`` when the deadline passes.
* Unity Catalog: ``GET /api/2.1/unity-catalog/{catalogs|schemas|tables}``.

Every request goes to the credential's workspace host only, with its bearer
token. Redirects are never followed. Downloads stop at ``max_bytes``. Error
messages never carry a secret.
"""

from __future__ import annotations

import base64
import time
from collections.abc import Callable
from typing import Any, Self
from urllib.parse import quote

import requests

from aisquare_pipe_databricks.credentials import (
    DatabricksCredentials,
    DatabricksTokenError,
    bearer_token,
)

DEFAULT_TIMEOUT = 30.0
#: DBFS returns at most 1 MB per read.
DBFS_READ_CHUNK = 1024 * 1024
_STREAM_CHUNK = 64 * 1024
_MAX_DETAIL = 300
#: SQL states after which the statement will not change.
TERMINAL_STATES = frozenset({"SUCCEEDED", "FAILED", "CANCELED", "CLOSED"})


class DatabricksAPIError(Exception):
    """A failed Databricks call. ``status`` is the HTTP status (None when
    nothing came back); ``retry_after`` is set for a 429/503 that said when."""

    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        retry_after: float | None = None,
        timed_out: bool = False,
    ) -> None:
        super().__init__(message)
        self.message = message
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
    or a host that pools connections) is left open. ``clock`` and ``sleep``
    drive statement polling (tests replace them)."""

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
        self._timeout = timeout if budget is None else max(0.1, min(timeout, budget))
        self._clock = clock
        self._sleep = sleep
        self._token: str | None = None

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        if self._own_session:
            self._session.close()

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
        resp = self._request("GET", f"/api/2.0/fs/files{files_path(path)}", stream=True)
        try:
            chunks: list[bytes] = []
            total = 0
            for chunk in resp.iter_content(_STREAM_CHUNK):
                total += len(chunk)
                if total > max_bytes:
                    raise FileTooLarge(f"{path} is larger than {max_bytes} bytes")
                chunks.append(chunk)
            return b"".join(chunks), _file_headers(resp.headers)
        finally:
            resp.close()

    # -- DBFS ----------------------------------------------------------------------------

    def dbfs_list(self, path: str) -> dict[str, Any]:
        return self._json("GET", "/api/2.0/dbfs/list", params={"path": path})

    def dbfs_status(self, path: str) -> dict[str, Any]:
        return self._json("GET", "/api/2.0/dbfs/get-status", params={"path": path})

    def dbfs_read(self, path: str, *, size: int, max_bytes: int) -> bytes:
        if size > max_bytes:
            raise FileTooLarge(f"{path} is larger than {max_bytes} bytes")
        data = bytearray()
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
            data.extend(base64.b64decode(chunk))
            offset += read
            if len(data) > max_bytes:
                raise FileTooLarge(f"{path} is larger than {max_bytes} bytes")
        return bytes(data)

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
        """Run one statement (already checked SELECT-only) and wait for it,
        at most ``deadline_seconds`` from now. Past the deadline the statement
        is cancelled and a timed-out :class:`DatabricksAPIError` raised."""
        started = self._clock()
        wait = int(max(5, min(50, deadline_seconds)))
        body: dict[str, Any] = {
            "statement": statement,
            "warehouse_id": warehouse_id,
            "wait_timeout": f"{wait}s" if deadline_seconds >= 5 else "0s",
            "on_wait_timeout": "CONTINUE",
            "disposition": "INLINE",
            "format": "JSON_ARRAY",
            "row_limit": row_limit,
            "byte_limit": byte_limit,
        }
        if catalog:
            body["catalog"] = catalog
        if schema:
            body["schema"] = schema
        result = self._json("POST", "/api/2.0/sql/statements", body=body)
        delay = 0.5
        while _state(result) not in TERMINAL_STATES:
            statement_id = result.get("statement_id")
            if not isinstance(statement_id, str) or not statement_id:
                raise DatabricksAPIError(
                    "the statement API answered without a statement_id"
                )
            if self._clock() - started >= deadline_seconds:
                self._cancel(statement_id)
                raise DatabricksAPIError(
                    f"the statement did not finish within {deadline_seconds:g}s and was cancelled",
                    timed_out=True,
                )
            self._sleep(
                min(delay, max(0.0, deadline_seconds - (self._clock() - started)))
            )
            delay = min(delay * 2, 5.0)
            result = self._json(
                "GET", f"/api/2.0/sql/statements/{quote(statement_id, safe='')}"
            )
        return result

    def _cancel(self, statement_id: str) -> None:
        try:
            self._request(
                "POST", f"/api/2.0/sql/statements/{quote(statement_id, safe='')}/cancel"
            ).close()
        except DatabricksAPIError:
            pass  # best effort: the deadline error is what the caller needs

    # -- Unity Catalog ---------------------------------------------------------------------

    def list_catalog(self, kind: str, params: dict[str, Any]) -> dict[str, Any]:
        return self._json("GET", f"/api/2.1/unity-catalog/{kind}", params=params)

    # -- transport -------------------------------------------------------------------------

    def _auth(self) -> dict[str, str]:
        if self._token is None:
            try:
                self._token = bearer_token(
                    self._creds, self._session, timeout=self._timeout
                )
            except DatabricksTokenError as e:
                raise DatabricksAPIError(str(e), status=401) from None
        return {"Authorization": f"Bearer {self._token}"}

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        body: dict[str, Any] | None = None,
        stream: bool = False,
    ) -> Any:
        try:
            resp = self._session.request(
                method,
                f"{self._creds.base_url}{path}",
                params=params,
                json=body,
                headers={"Accept": "application/json", **self._auth()},
                timeout=self._timeout,
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
    ) -> dict[str, Any]:
        resp = self._request(method, path, params=params, body=body)
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
    "TERMINAL_STATES",
    "DatabricksAPIError",
    "DatabricksClient",
    "FileTooLarge",
    "files_path",
]
