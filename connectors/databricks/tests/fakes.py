"""A fake Databricks workspace behind a ``requests.Session`` stand-in.

Serves the OIDC token endpoint (client credentials), the Files API
(directories, files, HEAD), DBFS (list, get-status, base64 read), SQL
statement execution and the Unity Catalog listings. Every request is
recorded; a wrong bearer is a 401.

A statement either follows a scripted list of states (one per answer), or,
with ``run_seconds`` and a :class:`Clock`, runs on that clock as a warehouse
does: the submit is held for ``min(wait_timeout, run_seconds)``; past
``wait_timeout`` it is answered RUNNING (``on_wait_timeout`` CONTINUE) or
cancelled by the warehouse (CANCEL); and a hold longer than the request's
``timeout`` raises ``requests.ReadTimeout`` after ``timeout``, with the
statement still running (or cancelled at ``wait_timeout``, under CANCEL), as
a real client would see it. ``running()`` lists what is still running.

With a :class:`Clock`, file reads take time too: each DBFS read takes
``dbfs_read_seconds`` (a ``ReadTimeout`` after the request's ``timeout`` when
that is shorter), and a volume download set to ``trickle`` sends its body a
few bytes a read, each read taking that long (:class:`TrickleBody`).
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import unquote

import requests
import urllib3

HOST = "acme.cloud.databricks.com"
BASE = f"https://{HOST}"
PAT = "dapi-fake-pat-secret-0123456789"
CLIENT_ID = "6f1f1f1f-sp-app-id"
CLIENT_SECRET = "dose-fake-client-secret-abcdef"


def pat_blob(**overrides: Any) -> dict[str, Any]:
    config: dict[str, Any] = {
        "workspace_url": BASE,
        "auth": "pat",
        "token": PAT,
        "warehouse_id": "abc123def456",
    }
    config.update(overrides)
    return config


def m2m_blob(**overrides: Any) -> dict[str, Any]:
    config: dict[str, Any] = {
        "workspace_url": BASE,
        "auth": "oauth_m2m",
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "warehouse_id": "abc123def456",
    }
    config.update(overrides)
    return config


@dataclass
class FakeResponse:
    status_code: int
    body: Any = None
    content: bytes = b""
    headers: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.body is not None and not self.content:
            self.content = json.dumps(self.body).encode()
            self.headers.setdefault("Content-Type", "application/json")

    @property
    def text(self) -> str:
        return self.content.decode("utf-8", "replace")

    def json(self) -> Any:
        return json.loads(self.content)

    def iter_content(self, size: int = 1024):
        for start in range(0, len(self.content), size):
            yield self.content[start : start + size]

    def close(self) -> None:
        pass


@dataclass
class StoredFile:
    data: bytes
    content_type: str = "application/octet-stream"
    modified_ms: int = 1_763_373_600_000  # 2025-11-17T10:00:00Z
    etag: str = '"etag-1"'


class Clock:
    """A fake monotonic clock; sleeping advances it (at least 0.5 s a sleep,
    as a slow scheduler might)."""

    def __init__(self) -> None:
        self.now = 1000.0
        self.slept: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += max(seconds, 0.5)


@dataclass
class Statement:
    states: list[str]
    rows: list[list[Any]] = field(default_factory=list)
    columns: list[dict[str, str]] = field(default_factory=list)
    total: int | None = None
    error: dict[str, str] | None = None
    #: How long it runs on the fake's clock (None: the scripted states;
    #: math.inf: it never finishes).
    run_seconds: float | None = None
    #: How long the submit is held whatever wait_timeout says (a slow or
    #: lost answer), on the fake's clock.
    submit_hold: float | None = None
    started: float = 0.0
    #: "warehouse" (on_wait_timeout CANCEL) or "cancel" (a cancel request).
    cancelled_by: str | None = None


class FakeDatabricks:
    def __init__(self, clock: Clock | None = None) -> None:
        self.clock = clock
        #: How long each statement poll takes, on the clock.
        self.poll_seconds = 0.0
        #: Raised by a file download's body midway, when set.
        self.body_breaks: Exception | None = None
        #: The most a DBFS read returns.
        self.dbfs_read_size = 4
        #: How long each DBFS read takes, on the clock.
        self.dbfs_read_seconds = 0.0
        #: A volume download's body, a read at a time: (bytes a read, seconds
        #: a read, whether the response has urllib3 2's ``raw.read1``).
        self.trickle: tuple[int, float, bool] | None = None
        self.volume: dict[str, StoredFile] = {}
        self.dbfs: dict[str, StoredFile] = {}
        self.catalog: dict[str, list[dict[str, Any]]] = {
            "catalogs": [],
            "schemas": [],
            "tables": [],
        }
        self.statements: dict[str, Statement] = {}
        self.next_statement: Statement | None = None
        self.requests: list[dict[str, Any]] = []
        self.minted: list[tuple[str, str]] = []
        self.tokens: set[str] = {PAT}
        self.cancelled: list[str] = []
        self.fail_with: tuple[int, dict[str, Any], dict[str, str]] | None = None
        self.token_status = 200

    # -- the requests.Session surface -------------------------------------------------

    def request(self, method: str, url: str, **kwargs: Any) -> FakeResponse:
        at = self.clock.now if self.clock is not None else None
        self.requests.append({"method": method, "url": url, "at": at, **kwargs})
        if not url.startswith(BASE + "/"):
            return FakeResponse(404, {"message": "wrong host"})
        path = url[len(BASE) :]
        if path == "/oidc/v1/token":
            return self._token(kwargs)
        auth = (kwargs.get("headers") or {}).get("Authorization", "")
        if not auth.startswith("Bearer ") or auth[7:] not in self.tokens:
            return FakeResponse(
                401,
                {"error_code": "UNAUTHENTICATED", "message": "Invalid access token"},
            )
        if self.fail_with is not None:
            status, body, headers = self.fail_with
            return FakeResponse(status, body, headers=dict(headers))
        params = dict(kwargs.get("params") or {})
        if path.startswith("/api/2.0/fs/directories"):
            return self._directory(
                unquote(path[len("/api/2.0/fs/directories") :]), params
            )
        if path.startswith("/api/2.0/fs/files"):
            return self._file(
                method, unquote(path[len("/api/2.0/fs/files") :]), kwargs.get("timeout")
            )
        if path == "/api/2.0/dbfs/list":
            return self._dbfs_list(params["path"])
        if path == "/api/2.0/dbfs/get-status":
            return self._dbfs_status(params["path"])
        if path == "/api/2.0/dbfs/read":
            return self._dbfs_read(params, kwargs.get("timeout"))
        if path == "/api/2.0/sql/statements" and method == "POST":
            return self._submit(kwargs.get("json") or {}, kwargs.get("timeout"))
        if path.startswith("/api/2.0/sql/statements/"):
            rest = path[len("/api/2.0/sql/statements/") :]
            if rest.endswith("/cancel"):
                statement_id = rest[: -len("/cancel")]
                self.cancelled.append(statement_id)
                if statement_id in self.statements:
                    self.statements[statement_id].cancelled_by = "cancel"
                return FakeResponse(200, {})
            return self._poll(rest, kwargs.get("timeout"))
        if path.startswith("/api/2.1/unity-catalog/"):
            kind = path.rsplit("/", 1)[-1]
            return FakeResponse(200, {kind: self.catalog.get(kind, [])})
        return FakeResponse(404, {"message": "no such endpoint"})

    def close(self) -> None:
        pass

    # -- endpoints ---------------------------------------------------------------------

    def _token(self, kwargs: dict[str, Any]) -> FakeResponse:
        client_id, secret = kwargs.get("auth") or ("", "")
        self.minted.append((client_id, (kwargs.get("data") or {}).get("scope", "")))
        if self.token_status != 200 or (client_id, secret) != (
            CLIENT_ID,
            CLIENT_SECRET,
        ):
            return FakeResponse(
                self.token_status if self.token_status != 200 else 401,
                {"error": "invalid_client"},
            )
        token = f"m2m-token-{len(self.minted)}"
        self.tokens.add(token)
        return FakeResponse(
            200, {"access_token": token, "token_type": "Bearer", "expires_in": 3600}
        )

    def _directory(self, path: str, params: dict[str, Any]) -> FakeResponse:
        prefix = path.rstrip("/") + "/"
        names: dict[str, dict[str, Any]] = {}
        for full, stored in self.volume.items():
            if not full.startswith(prefix):
                continue
            rest = full[len(prefix) :]
            head, _, tail = rest.partition("/")
            if tail:
                names.setdefault(
                    prefix + head,
                    {"path": prefix + head + "/", "name": head, "is_directory": True},
                )
            else:
                names[full] = {
                    "path": full,
                    "name": head,
                    "is_directory": False,
                    "file_size": len(stored.data),
                    "last_modified": stored.modified_ms,
                }
        if not names:
            return FakeResponse(
                404, {"error_code": "NOT_FOUND", "message": "directory not found"}
            )
        ordered = [names[k] for k in sorted(names)]
        size = int(params.get("page_size") or 1000)
        start = int(params.get("page_token") or 0)
        body: dict[str, Any] = {"contents": ordered[start : start + size]}
        if start + size < len(ordered):
            body["next_page_token"] = str(start + size)
        return FakeResponse(200, body)

    def _file(self, method: str, path: str, timeout: Any = None) -> Any:
        stored = self.volume.get(path)
        if stored is None:
            return FakeResponse(
                404, {"error_code": "NOT_FOUND", "message": "file not found"}
            )
        headers = {
            "Content-Type": stored.content_type,
            "Content-Length": str(len(stored.data)),
            "Last-Modified": "Mon, 17 Nov 2025 10:00:00 GMT",
            "ETag": stored.etag,
        }
        response = FakeResponse(
            200, content=b"" if method == "HEAD" else stored.data, headers=headers
        )
        if self.body_breaks is not None and method == "GET":
            return BrokenBody(response, self.body_breaks)
        if self.trickle is not None and method == "GET" and self.clock is not None:
            per_read, seconds, read1 = self.trickle
            return TrickleBody(
                response, self.clock, per_read, seconds, timeout, read1=read1
            )
        return response

    def _dbfs_list(self, path: str) -> FakeResponse:
        prefix = path.rstrip("/") + "/"
        entries: dict[str, dict[str, Any]] = {}
        for full, stored in self.dbfs.items():
            if full.startswith(prefix):
                head, _, tail = full[len(prefix) :].partition("/")
                if tail:
                    entries.setdefault(
                        prefix + head,
                        {"path": prefix + head, "is_dir": True, "file_size": 0},
                    )
                else:
                    entries[full] = {
                        "path": full,
                        "is_dir": False,
                        "file_size": len(stored.data),
                        "modification_time": stored.modified_ms,
                    }
        if not entries:
            return FakeResponse(
                404, {"error_code": "RESOURCE_DOES_NOT_EXIST", "message": "no such dir"}
            )
        return FakeResponse(200, {"files": [entries[k] for k in sorted(entries)]})

    def _dbfs_status(self, path: str) -> FakeResponse:
        stored = self.dbfs.get(path)
        if stored is None:
            return FakeResponse(
                404,
                {"error_code": "RESOURCE_DOES_NOT_EXIST", "message": "no such file"},
            )
        return FakeResponse(
            200,
            {
                "path": path,
                "is_dir": False,
                "file_size": len(stored.data),
                "modification_time": stored.modified_ms,
            },
        )

    def _dbfs_read(self, params: dict[str, Any], timeout: Any = None) -> FakeResponse:
        if self.clock is not None and self.dbfs_read_seconds:
            if not isinstance(timeout, int | float) or self.dbfs_read_seconds > timeout:
                self.clock.now += timeout if isinstance(timeout, int | float) else 0
                raise requests.ReadTimeout("fake DBFS: read timed out")
            self.clock.now += self.dbfs_read_seconds
        stored = self.dbfs[params["path"]]
        offset, length = int(params["offset"]), int(params["length"])
        # Tiny reads by default: they exercise the loop.
        chunk = stored.data[offset : offset + min(length, self.dbfs_read_size)]
        return FakeResponse(
            200, {"bytes_read": len(chunk), "data": base64.b64encode(chunk).decode()}
        )

    def _submit(self, body: dict[str, Any], timeout: Any) -> FakeResponse:
        statement = self.next_statement or Statement(["SUCCEEDED"])
        statement_id = f"stmt-{len(self.statements) + 1}"
        self.statements[statement_id] = statement
        if statement.run_seconds is None or self.clock is None:
            return self._answer(statement_id)
        statement.started = self.clock.now
        wait = int(str(body["wait_timeout"]).removesuffix("s"))
        cancel = body.get("on_wait_timeout") == "CANCEL"
        hold = min(wait, statement.run_seconds)
        if statement.submit_hold is not None:
            hold = max(hold, statement.submit_hold)
        if cancel and statement.run_seconds > wait:
            # The warehouse cancels it at wait_timeout, answered or not.
            statement.cancelled_by = "warehouse"
            statement.run_seconds = float(wait)
        if not isinstance(timeout, int | float) or hold > timeout:
            self.clock.now += timeout if isinstance(timeout, int | float) else hold
            raise requests.ReadTimeout("fake warehouse: read timed out")
        self.clock.now += hold
        return self._answer(statement_id)

    def _poll(self, statement_id: str, timeout: Any) -> FakeResponse:
        statement = self.statements[statement_id]
        if self.clock is not None and self.poll_seconds:
            if not isinstance(timeout, int | float) or self.poll_seconds > timeout:
                self.clock.now += timeout if isinstance(timeout, int | float) else 0
                raise requests.ReadTimeout("fake warehouse: read timed out")
            self.clock.now += self.poll_seconds
        if statement.run_seconds is None and len(statement.states) > 1:
            statement.states.pop(0)
        return self._answer(statement_id)

    def _state(self, statement: Statement) -> str:
        if statement.run_seconds is None or self.clock is None:
            return statement.states[0]
        if statement.cancelled_by == "cancel":
            return "CANCELED"
        if statement.cancelled_by == "warehouse":
            done = self.clock.now - statement.started >= statement.run_seconds
            return "CANCELED" if done else "RUNNING"
        if self.clock.now - statement.started >= statement.run_seconds:
            return "SUCCEEDED"
        return "RUNNING"

    def running(self) -> list[str]:
        """The statements still running on the warehouse now."""
        return [
            statement_id
            for statement_id, statement in self.statements.items()
            if self._state(statement) in ("PENDING", "RUNNING")
        ]

    def _answer(self, statement_id: str) -> FakeResponse:
        statement = self.statements[statement_id]
        state = self._state(statement)
        body: dict[str, Any] = {
            "statement_id": statement_id,
            "status": {"state": state},
        }
        if state == "FAILED" and statement.error:
            body["status"]["error"] = statement.error
        if state == "SUCCEEDED":
            body["manifest"] = {
                "schema": {"columns": statement.columns},
                "total_row_count": statement.total
                if statement.total is not None
                else len(statement.rows),
                "truncated": False,
            }
            body["result"] = {"data_array": statement.rows, "chunk_index": 0}
        return FakeResponse(200, body)

    # -- what was asked -----------------------------------------------------------------

    def api_calls(self) -> list[dict[str, Any]]:
        return [r for r in self.requests if not r["url"].endswith("/oidc/v1/token")]

    def sql_submissions(self) -> list[dict[str, Any]]:
        return [
            r for r in self.requests if r["url"].endswith("/api/2.0/sql/statements")
        ]


class BrokenBody:
    """A download whose body breaks off after its first chunk."""

    def __init__(self, response: FakeResponse, error: Exception) -> None:
        self._response = response
        self._error = error
        self.status_code = response.status_code
        self.headers = response.headers

    def iter_content(self, size: int = 1024):
        yield self._response.content[:size]
        raise self._error

    def close(self) -> None:
        pass


class FakeSocket:
    """The socket under a streamed response: records each ``settimeout``
    with the clock's time, as urllib3 2's ``raw.connection.sock``."""

    def __init__(self, clock: Clock, timeout: float) -> None:
        self._clock = clock
        self.timeout = timeout
        self.timeouts: list[tuple[float, float]] = []

    def settimeout(self, timeout: float) -> None:
        self.timeout = timeout
        self.timeouts.append((self._clock.now, timeout))


class _Connection:
    def __init__(self, sock: FakeSocket) -> None:
        self.sock = sock


class _Raw:
    """urllib3 2's response surface the client reads with: ``read1`` (one
    socket read, bounded by the socket's timeout) and ``connection.sock``."""

    def __init__(self, body: TrickleBody) -> None:
        self._body = body
        self.connection = _Connection(body.sock)

    def read1(
        self, amt: int | None = None, decode_content: bool | None = None
    ) -> bytes:
        return self._body.read(amt or self._body.per_read)


class TrickleBody:
    """A download whose body comes ``per_read`` bytes a read, each read taking
    ``seconds`` on the clock. A read slower than the socket's timeout (the
    request's ``timeout``, or what the client last set) raises urllib3's
    ``ReadTimeoutError`` after that timeout, as a real socket read does. With
    ``read1``, the response has urllib3 2's ``raw`` (one socket read a call);
    without, only ``iter_content``, whose every chunk takes ``seconds``."""

    def __init__(
        self,
        response: FakeResponse,
        clock: Clock,
        per_read: int,
        seconds: float,
        timeout: Any,
        *,
        read1: bool,
    ) -> None:
        self._response = response
        self._clock = clock
        self.per_read = per_read
        self._seconds = seconds
        self._at = 0
        self.status_code = response.status_code
        self.headers = response.headers
        self.sock = FakeSocket(clock, float(timeout))
        self.reads = 0
        if read1:
            self.raw = _Raw(self)

    def read(self, amt: int) -> bytes:
        data = self._response.content
        if self._at >= len(data):
            return b""
        if self._seconds > self.sock.timeout:
            self._clock.now += self.sock.timeout
            raise urllib3.exceptions.ReadTimeoutError(None, None, "Read timed out.")  # type: ignore[arg-type]
        self._clock.now += self._seconds
        self.reads += 1
        chunk = data[self._at : self._at + min(amt, self.per_read)]
        self._at += len(chunk)
        return chunk

    def iter_content(self, size: int = 1024):
        while chunk := self.read(size):
            yield chunk

    def close(self) -> None:
        pass
