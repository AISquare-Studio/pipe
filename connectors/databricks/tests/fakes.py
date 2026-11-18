"""A fake Databricks workspace behind a ``requests.Session`` stand-in.

Serves the OIDC token endpoint (client credentials), the Files API
(directories, files, HEAD), DBFS (list, get-status, base64 read), SQL
statement execution (a scripted list of states per statement, cancel
recorded) and the Unity Catalog listings. Every request is recorded; a wrong
bearer is a 401.
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import unquote

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


@dataclass
class Statement:
    states: list[str]
    rows: list[list[Any]] = field(default_factory=list)
    columns: list[dict[str, str]] = field(default_factory=list)
    total: int | None = None
    error: dict[str, str] | None = None


class FakeDatabricks:
    def __init__(self) -> None:
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
        self.requests.append({"method": method, "url": url, **kwargs})
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
            return self._file(method, unquote(path[len("/api/2.0/fs/files") :]))
        if path == "/api/2.0/dbfs/list":
            return self._dbfs_list(params["path"])
        if path == "/api/2.0/dbfs/get-status":
            return self._dbfs_status(params["path"])
        if path == "/api/2.0/dbfs/read":
            return self._dbfs_read(params)
        if path == "/api/2.0/sql/statements" and method == "POST":
            return self._submit(kwargs.get("json") or {})
        if path.startswith("/api/2.0/sql/statements/"):
            rest = path[len("/api/2.0/sql/statements/") :]
            if rest.endswith("/cancel"):
                self.cancelled.append(rest[: -len("/cancel")])
                return FakeResponse(200, {})
            return self._poll(rest)
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

    def _file(self, method: str, path: str) -> FakeResponse:
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
        return FakeResponse(
            200, content=b"" if method == "HEAD" else stored.data, headers=headers
        )

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

    def _dbfs_read(self, params: dict[str, Any]) -> FakeResponse:
        stored = self.dbfs[params["path"]]
        offset, length = int(params["offset"]), int(params["length"])
        chunk = stored.data[
            offset : offset + min(length, 4)
        ]  # tiny reads: exercises the loop
        return FakeResponse(
            200, {"bytes_read": len(chunk), "data": base64.b64encode(chunk).decode()}
        )

    def _submit(self, body: dict[str, Any]) -> FakeResponse:
        statement = self.next_statement or Statement(["SUCCEEDED"])
        statement_id = f"stmt-{len(self.statements) + 1}"
        self.statements[statement_id] = statement
        return self._answer(statement_id)

    def _poll(self, statement_id: str) -> FakeResponse:
        statement = self.statements[statement_id]
        if len(statement.states) > 1:
            statement.states.pop(0)
        return self._answer(statement_id)

    def _answer(self, statement_id: str) -> FakeResponse:
        statement = self.statements[statement_id]
        state = statement.states[0]
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
