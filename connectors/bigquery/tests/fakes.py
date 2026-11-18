"""A fake Google: the OAuth token endpoint and the BigQuery REST API (v2).

``FakeGoogle`` is a ``requests.Session`` stand-in: the toolkit, the source and
the token exchange all call ``session.request(...)``. The token endpoint
decodes the signed JWT assertion (no signature check: a real key is never
used) and records who asked. The BigQuery API serves in-memory projects,
datasets and tables (datasets.list, tables.list, tables.get,
tabledata.list) and CANNED query results keyed by the exact SQL text
(jobs.query, dry runs included, and jobs.getQueryResults with polling and
paging). Like BigQuery, it refuses a query whose bytes exceed the job's
``maximumBytesBilled``. Every request is recorded; a wrong bearer token is a
401.

:func:`real_session` puts the same fake behind a REAL ``requests.Session``
(an ``HTTPAdapter`` whose ``send`` answers with urllib3 responses), so the
JSON bodies and query strings go through requests' own encoding. No socket
is opened.
"""

from __future__ import annotations

import base64
import io
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs, unquote

import requests
import requests.adapters
import urllib3
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa

TOKEN_URL = "https://oauth2.googleapis.com/token"
API = "https://bigquery.googleapis.com/bigquery/v2/projects/"
PROJECT = "acme-analytics"

_KEYS: dict[str, str] = {}


def private_key_pem(label: str = "default") -> str:
    """A throwaway RSA key (made once per label per test run)."""
    if label not in _KEYS:
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        _KEYS[label] = key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ).decode()
    return _KEYS[label]


def ec_private_key_pem() -> str:
    """A throwaway P-256 key: PEM-shaped, but not a key Google signs with."""
    key = ec.generate_private_key(ec.SECP256R1())
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()


def service_account(
    email: str = "aisquare-reader@acme-analytics.iam.gserviceaccount.com",
    label: str = "default",
) -> dict[str, Any]:
    return {
        "type": "service_account",
        "project_id": PROJECT,
        "private_key_id": f"kid-{label}",
        "private_key": private_key_pem(label),
        "client_email": email,
        "client_id": "1234567890",
        "token_uri": TOKEN_URL,
        "universe_domain": "googleapis.com",
    }


def blob(**overrides: Any) -> dict[str, Any]:
    config: dict[str, Any] = {
        "service_account_json": service_account(),
        "project_id": PROJECT,
        "location": "US",
    }
    config.update(overrides)
    return {k: v for k, v in config.items() if v is not None}


def field_(name: str, kind: str = "STRING", mode: str = "NULLABLE", **extra: Any):
    return {"name": name, "type": kind, "mode": mode, **extra}


def encode(fields: list[dict[str, Any]], values: list[Any]) -> dict[str, Any]:
    """Python values as one REST row ({"f": [{"v": ...}]}), every scalar a
    string, as BigQuery sends them."""
    return {"f": [{"v": _cell(f, v)} for f, v in zip(fields, values, strict=True)]}


def _cell(f: dict[str, Any], value: Any) -> Any:
    if value is None:
        return None
    if f.get("mode") == "REPEATED":
        scalar = {**f, "mode": "NULLABLE"}
        return [{"v": _cell(scalar, item)} for item in value]
    if f["type"] in ("RECORD", "STRUCT"):
        sub = f["fields"]
        items = [value[s["name"]] for s in sub] if isinstance(value, dict) else value
        return encode(sub, items)
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


@dataclass
class FakeTable:
    schema: list[dict[str, Any]]
    rows: list[list[Any]] = field(default_factory=list)
    type: str = "TABLE"
    last_modified_ms: str = "1763373600000"  # 2025-11-17T10:00:00Z
    location: str = "US"
    description: str | None = None

    def resource(self, project: str, dataset: str, table: str) -> dict[str, Any]:
        out: dict[str, Any] = {
            "kind": "bigquery#table",
            "id": f"{project}:{dataset}.{table}",
            "tableReference": {
                "projectId": project,
                "datasetId": dataset,
                "tableId": table,
            },
            "type": self.type,
            "schema": {"fields": self.schema},
            "numRows": str(len(self.rows)),
            "numBytes": str(100 * len(self.rows)),
            "creationTime": "1762000000000",
            "lastModifiedTime": self.last_modified_ms,
            "location": self.location,
        }
        if self.description:
            out["description"] = self.description
        return out


@dataclass
class CannedQuery:
    """What jobs.query answers for one exact SQL text."""

    schema: list[dict[str, Any]]
    rows: list[list[Any]] = field(default_factory=list)
    bytes_processed: int = 2048
    #: What the dry run estimates (default: bytes_processed).
    estimate: int | None = None
    statement_type: str | None = "SELECT"
    #: getQueryResults answers "not complete" this many times first.
    polls: int = 0
    #: The finished job answers this error (status, reason, message).
    error: tuple[int, str, str] | None = None


@dataclass
class _Job:
    canned: CannedQuery
    polls_left: int
    body: dict[str, Any]


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

    def close(self) -> None:
        pass


def _error(status: int, reason: str, message: str) -> FakeResponse:
    return FakeResponse(
        status,
        {
            "error": {
                "code": status,
                "message": message,
                "errors": [{"domain": "global", "reason": reason, "message": message}],
            }
        },
    )


def _jwt_claims(assertion: str) -> dict[str, Any]:
    payload = assertion.split(".")[1]
    return json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))


class FakeGoogle:
    """The session every call goes through (see the module docstring)."""

    def __init__(self) -> None:
        #: project -> dataset -> table -> FakeTable
        self.data: dict[str, dict[str, dict[str, FakeTable]]] = {}
        self.dataset_locations: dict[tuple[str, str], str] = {}
        self.queries: dict[str, CannedQuery] = {}
        self.jobs: dict[str, _Job] = {}
        self.requests: list[dict[str, Any]] = []
        self.token_requests: list[dict[str, Any]] = []
        self.tokens: set[str] = set()
        #: The token endpoint answers (status, body[, headers]) instead.
        self.token_error: tuple[Any, ...] | None = None
        #: The token request raises this (as requests would).
        self.token_raise: BaseException | None = None
        #: Every BigQuery call answers (status, body, headers); "{token}" in
        #: the body's text is replaced by the request's bearer token.
        self.fail_with: tuple[int, dict[str, Any], dict[str, str]] | None = None
        self.raise_timeout = False
        #: Every bearer token is refused, even one just minted.
        self.reject_all_tokens = False
        #: Called with the URL before every request is answered (time
        #: passing, in the budget tests).
        self.on_request: Callable[[str], None] | None = None
        #: Query result pages hold this many rows whatever maxResults asked
        #: (BigQuery may answer with fewer; a client must cope with more).
        self.result_page_rows: int | None = None

    # -- set-up ----------------------------------------------------------------------

    def add_table(
        self, ref: str, table: FakeTable, *, location: str = "US"
    ) -> FakeTable:
        project, dataset, name = ref.split(".")
        self.data.setdefault(project, {}).setdefault(dataset, {})[name] = table
        self.dataset_locations[(project, dataset)] = location
        return table

    def add_dataset(self, ref: str, *, location: str = "US") -> None:
        project, dataset = ref.split(".")
        self.data.setdefault(project, {}).setdefault(dataset, {})
        self.dataset_locations[(project, dataset)] = location

    def add_query(self, sql: str, canned: CannedQuery) -> CannedQuery:
        self.queries[sql] = canned
        return canned

    # -- the requests.Session surface ------------------------------------------------

    def request(self, method: str, url: str, **kwargs: Any) -> FakeResponse:
        self.requests.append({"method": method, "url": url, **kwargs})
        if self.on_request is not None:
            self.on_request(url)
        if url == TOKEN_URL:
            return self._token(kwargs)
        if self.raise_timeout:
            raise requests.Timeout("timed out")
        auth = (kwargs.get("headers") or {}).get("Authorization", "")
        if (
            not auth.startswith("Bearer ")
            or auth[7:] not in self.tokens
            or self.reject_all_tokens
        ):
            return _error(
                401, "authError", "Request had invalid authentication credentials."
            )
        if self.fail_with is not None:
            status, body, headers = self.fail_with
            echoed = json.loads(json.dumps(body).replace("{token}", auth[7:]))
            return FakeResponse(status, echoed, headers=dict(headers))
        if not url.startswith(API):
            return _error(404, "notFound", "Not found: URL")
        parts = [unquote(p) for p in url[len(API) :].split("/")]
        params = dict(kwargs.get("params") or {})
        body = kwargs.get("data")
        payload = json.loads(body) if body else {}
        project = parts[0]
        if method == "POST" and parts[1:] == ["queries"]:
            return self._query(project, payload)
        if method == "GET" and len(parts) == 3 and parts[1] == "queries":
            return self._results(parts[2], params)
        if method == "GET" and parts[1:] == ["datasets"]:
            return self._datasets(project, params)
        if method == "GET" and len(parts) == 4 and parts[3] == "tables":
            return self._tables(project, parts[2], params)
        if method == "GET" and len(parts) >= 5 and parts[3] == "tables":
            table = self.data.get(project, {}).get(parts[2], {}).get(parts[4])
            if table is None:
                return _error(
                    404, "notFound", f"Not found: Table {project}:{parts[2]}.{parts[4]}"
                )
            if len(parts) == 5:
                return FakeResponse(200, table.resource(project, parts[2], parts[4]))
            if parts[5:] == ["data"]:
                return self._rows(table, params)
        return _error(404, "notFound", "Not found: URL")

    def close(self) -> None:
        pass

    def revoke_tokens(self) -> None:
        """Every token minted so far stops working (key or account disabled)."""
        self.tokens.clear()

    # -- endpoints -----------------------------------------------------------------------

    def _token(self, kwargs: dict[str, Any]) -> FakeResponse:
        body = kwargs.get("data") or b""
        form = parse_qs(body.decode() if isinstance(body, bytes) else str(body))
        assert form["grant_type"] == ["urn:ietf:params:oauth:grant-type:jwt-bearer"]
        claims = _jwt_claims(form["assertion"][0])
        self.token_requests.append(claims)
        if self.token_raise is not None:
            raise self.token_raise
        if self.token_error is not None:
            status, error, *headers = self.token_error
            return FakeResponse(
                status, error, headers=dict(headers[0] if headers else {})
            )
        token = f"ya29.fake-token-{len(self.token_requests)}"
        self.tokens.add(token)
        return FakeResponse(
            200, {"access_token": token, "expires_in": 3600, "token_type": "Bearer"}
        )

    @staticmethod
    def _page(items: list[Any], params: dict[str, Any]) -> tuple[list[Any], str | None]:
        size = int(params.get("maxResults") or 50)
        start = int(params.get("pageToken") or 0)
        page = items[start : start + size]
        return page, (str(start + size) if start + size < len(items) else None)

    def _datasets(self, project: str, params: dict[str, Any]) -> FakeResponse:
        names = sorted(self.data.get(project, {}))
        page, token = self._page(names, params)
        body: dict[str, Any] = {"kind": "bigquery#datasetList"}
        if page:
            body["datasets"] = [
                {
                    "kind": "bigquery#dataset",
                    "id": f"{project}:{d}",
                    "datasetReference": {"projectId": project, "datasetId": d},
                    "location": self.dataset_locations.get((project, d), "US"),
                }
                for d in page
            ]
        if token:
            body["nextPageToken"] = token
        return FakeResponse(200, body)

    def _tables(
        self, project: str, dataset: str, params: dict[str, Any]
    ) -> FakeResponse:
        tables = self.data.get(project, {}).get(dataset)
        if tables is None:
            return _error(404, "notFound", f"Not found: Dataset {project}:{dataset}")
        names = sorted(tables)
        page, token = self._page(names, params)
        body: dict[str, Any] = {"kind": "bigquery#tableList", "totalItems": len(names)}
        if page:
            body["tables"] = [
                {
                    "tableReference": {
                        "projectId": project,
                        "datasetId": dataset,
                        "tableId": t,
                    },
                    "type": tables[t].type,
                    "creationTime": "1762000000000",
                }
                for t in page
            ]
        if token:
            body["nextPageToken"] = token
        return FakeResponse(200, body)

    def _rows(self, table: FakeTable, params: dict[str, Any]) -> FakeResponse:
        if table.type not in ("TABLE", "SNAPSHOT"):
            return _error(400, "invalid", "Cannot list a table of type VIEW.")
        fields = table.schema
        rows = table.rows
        if params.get("selectedFields"):
            wanted = str(params["selectedFields"]).split(",")
            index = [i for i, f in enumerate(fields) if f["name"] in wanted]
            # BigQuery sends the cells in the order asked for.
            order = sorted(index, key=lambda i: wanted.index(fields[i]["name"]))
            fields = [fields[i] for i in order]
            rows = [[r[i] for i in order] for r in rows]
        size = int(params.get("maxResults") or 100)
        start = int(params.get("pageToken") or params.get("startIndex") or 0)
        page = rows[start : start + size]
        body: dict[str, Any] = {
            "kind": "bigquery#tableDataList",
            "totalRows": str(len(rows)),
        }
        if page:
            body["rows"] = [encode(fields, r) for r in page]
        if start + size < len(rows):
            body["pageToken"] = str(start + size)
        return FakeResponse(200, body)

    def _query(self, project: str, payload: dict[str, Any]) -> FakeResponse:
        sql = payload.get("query")
        canned = self.queries.get(sql) if isinstance(sql, str) else None
        if canned is None:
            return _error(
                400, "invalidQuery", "Unrecognized name: no_such_column at [1:8]"
            )
        if payload.get("useLegacySql") is not False:
            return _error(400, "invalidQuery", "legacy SQL is not faked")
        if payload.get("dryRun"):
            body: dict[str, Any] = {
                "kind": "bigquery#queryResponse",
                "jobComplete": True,
                "totalBytesProcessed": str(
                    canned.estimate
                    if canned.estimate is not None
                    else canned.bytes_processed
                ),
                "schema": {"fields": canned.schema},
            }
            if canned.statement_type is not None:
                body["statementType"] = canned.statement_type
            return FakeResponse(200, body)
        cap = payload.get("maximumBytesBilled")
        if cap is not None and canned.bytes_processed > int(cap):
            return _error(
                400,
                "bytesBilledLimitExceeded",
                f"Query exceeded limit for bytes billed: {cap}.",
            )
        job_id = f"job_{len(self.jobs) + 1}"
        self.jobs[job_id] = _Job(canned, canned.polls, payload)
        params = {"maxResults": payload.get("maxResults")}
        return self._answer(project, job_id, params)

    def _results(self, job_id: str, params: dict[str, Any]) -> FakeResponse:
        job = self.jobs.get(job_id)
        if job is None:
            return _error(404, "notFound", f"Not found: Job {job_id}")
        if job.polls_left > 0:
            job.polls_left -= 1
        return self._answer(PROJECT, job_id, params)

    def _answer(
        self, project: str, job_id: str, params: dict[str, Any]
    ) -> FakeResponse:
        job = self.jobs[job_id]
        reference = {
            "projectId": project,
            "jobId": job_id,
            "location": job.body.get("location") or "US",
        }
        if job.polls_left > 0:
            return FakeResponse(
                200,
                {
                    "kind": "bigquery#getQueryResultsResponse",
                    "jobReference": reference,
                    "jobComplete": False,
                },
            )
        canned = job.canned
        if canned.error is not None:
            return _error(*canned.error)
        size = self.result_page_rows or int(params.get("maxResults") or 100)
        start = int(params.get("pageToken") or 0)
        page = canned.rows[start : start + size]
        body = {
            "kind": "bigquery#getQueryResultsResponse",
            "jobReference": reference,
            "jobComplete": True,
            "schema": {"fields": canned.schema},
            "totalRows": str(len(canned.rows)),
            "totalBytesProcessed": str(canned.bytes_processed),
            "cacheHit": False,
        }
        if page:
            body["rows"] = [encode(canned.schema, r) for r in page]
        if start + size < len(canned.rows):
            body["pageToken"] = str(start + size)
        return FakeResponse(200, body)

    # -- what was asked ------------------------------------------------------------------

    def api_calls(self) -> list[dict[str, Any]]:
        return [r for r in self.requests if r["url"] != TOKEN_URL]

    def query_posts(self) -> list[dict[str, Any]]:
        """The bodies of every jobs.query POST (dry runs included)."""
        return [
            json.loads(r["data"])
            for r in self.api_calls()
            if r["method"] == "POST" and r["url"].endswith("/queries")
        ]

    def runs(self) -> list[dict[str, Any]]:
        """The jobs.query bodies that started a job (not dry runs)."""
        return [b for b in self.query_posts() if not b.get("dryRun")]


class _Body(io.RawIOBase):
    """A socket-like body: ``data``, or its first ``after`` bytes and then
    ``error``."""

    def __init__(
        self, data: bytes, broken: tuple[int, BaseException] | None = None
    ) -> None:
        self._data = data
        self._pos = 0
        self._broken = broken

    def readable(self) -> bool:
        return True

    def readinto(self, buffer: Any) -> int:
        end = len(self._data)
        if self._broken is not None:
            after, error = self._broken
            if self._pos >= after:
                raise error
            end = min(end, after)
        n = min(len(buffer), end - self._pos)
        buffer[:n] = self._data[self._pos : self._pos + n]
        self._pos += n
        return n


class _FakeGoogleAdapter(requests.adapters.HTTPAdapter):
    """Answers every request from a FakeGoogle with a urllib3 response.
    ``body_break`` makes every BigQuery body break off after a few bytes."""

    def __init__(
        self, google: FakeGoogle, body_break: BaseException | None = None
    ) -> None:
        super().__init__()
        self._google = google
        self._break = body_break

    def send(self, request: Any, stream: bool = False, timeout: Any = None, **_: Any):
        url, _sep, query = str(request.url).partition("?")
        params = {k: v[-1] for k, v in parse_qs(query, keep_blank_values=True).items()}
        answer = self._google.request(
            str(request.method),
            url,
            params=params,
            data=request.body,
            headers=dict(request.headers),
            timeout=timeout,
            allow_redirects=False,
        )
        broken = (
            (8, self._break) if self._break is not None and url != TOKEN_URL else None
        )
        raw = urllib3.HTTPResponse(
            body=_Body(answer.content, broken),
            headers=answer.headers,
            status=answer.status_code,
            preload_content=False,
            request_method=str(request.method),
        )
        return self.build_response(request, raw)


def real_session(
    google: FakeGoogle, *, body_break: BaseException | None = None
) -> requests.Session:
    """A real ``requests.Session`` whose HTTPS goes to ``google``. It ignores
    proxy settings (a proxy lookup may resolve names on macOS)."""
    session = requests.Session()
    session.trust_env = False
    session.mount("https://", _FakeGoogleAdapter(google, body_break))
    return session
