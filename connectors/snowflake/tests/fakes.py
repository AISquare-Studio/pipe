"""A fake Snowflake: the SQL API (v2) of one account, and an OAuth token endpoint.

``FakeSnowflake`` is a ``requests.Session`` stand-in: the toolkit, the source
and the OAuth token request all call ``session.request(...)``. The SQL API
checks the bearer like Snowflake does: a key-pair JWT must be signed by the
key whose public half is registered for its user (RS256 verified with
``cryptography``), with Snowflake's ``iss``/``sub`` claims and at most an
hour of life; an OAuth token must be one the token endpoint issued. It
serves:

* ``SHOW [TERSE] DATABASES | SCHEMAS | TABLES ...`` from in-memory objects
  (``LIKE``, ``IN``, ``STARTS WITH``, ``LIMIT ... FROM``, ``HISTORY``);
* the connector's INFORMATION_SCHEMA.TABLES / COLUMNS reads (bind
  variables);
* the source's own ``SELECT ... FROM "DB"."SCHEMA"."TABLE"`` (columns,
  ``ORDER BY``, ``LIMIT``/``OFFSET``);
* CANNED results for any other statement, keyed by its exact text, with
  polling (202 answers before the result), partitions and failures.

Like Snowflake, it honours ``ROWS_PER_RESULTSET`` and refuses a request
whose ``MULTI_STATEMENT_COUNT`` isn't 1. Every request is recorded.

:func:`real_session` puts the same fake behind a REAL ``requests.Session``
(an ``HTTPAdapter`` whose ``send`` answers with urllib3 responses), so the
JSON bodies, forms and query strings go through requests' own encoding. No
socket is opened.
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import re
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs

import requests
import requests.adapters
import urllib3
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa

ACCOUNT = "acme-analytics"
HOST = "https://acme-analytics.snowflakecomputing.com"
TOKEN_URL = "https://idp.example.com/oauth2/v2.0/token"
USER = "AISQUARE_READER"
ROLE = "AISQUARE_READONLY"
WAREHOUSE = "AISQUARE_WH"
CLIENT_ID = "client-123"
CLIENT_SECRET = "s3cr3t-client-value"

_KEYS: dict[tuple[str, int], rsa.RSAPrivateKey] = {}


def rsa_key(label: str = "default", bits: int = 2048) -> rsa.RSAPrivateKey:
    """A throwaway RSA key (made once per label and size per test run)."""
    if (label, bits) not in _KEYS:
        _KEYS[(label, bits)] = rsa.generate_private_key(
            public_exponent=65537, key_size=bits
        )
    return _KEYS[(label, bits)]


def private_key_pem(
    label: str = "default",
    *,
    bits: int = 2048,
    passphrase: str | None = None,
    pkcs1: bool = False,
) -> str:
    encryption: Any = (
        serialization.BestAvailableEncryption(passphrase.encode())
        if passphrase
        else serialization.NoEncryption()
    )
    return (
        rsa_key(label, bits)
        .private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL
            if pkcs1
            else serialization.PrivateFormat.PKCS8,
            encryption,
        )
        .decode()
    )


def ec_private_key_pem() -> str:
    key = ec.generate_private_key(ec.SECP256R1())
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()


def fingerprint(label: str = "default") -> str:
    der = (
        rsa_key(label)
        .public_key()
        .public_bytes(
            serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
        )
    )
    return "SHA256:" + base64.b64encode(hashlib.sha256(der).digest()).decode()


def blob(**overrides: Any) -> dict[str, Any]:
    """A key-pair credential blob (the default auth)."""
    config: dict[str, Any] = {
        "account": ACCOUNT,
        "user": USER,
        "private_key_pem": private_key_pem(),
        "role": ROLE,
        "warehouse": WAREHOUSE,
        "database": "POLICIES",
        "schema": "PUBLIC",
    }
    config.update(overrides)
    return {k: v for k, v in config.items() if v is not None}


def oauth_blob(**overrides: Any) -> dict[str, Any]:
    config: dict[str, Any] = {
        "account": ACCOUNT,
        "auth": "oauth_client",
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "token_url": TOKEN_URL,
        "scope": "api://snowflake/.default",
        "role": ROLE,
        "warehouse": WAREHOUSE,
        "database": "POLICIES",
        "schema": "PUBLIC",
    }
    config.update(overrides)
    return {k: v for k, v in config.items() if v is not None}


def col(name: str, kind: str = "text", **extra: Any) -> dict[str, Any]:
    """One ``rowType`` entry."""
    return {"name": name, "type": kind, "nullable": True, **extra}


@dataclass
class FakeTable:
    columns: list[dict[str, Any]]
    rows: list[list[Any]] = field(default_factory=list)
    kind: str = "BASE TABLE"
    #: LAST_ALTERED as the SQL API sends a TIMESTAMP_LTZ: epoch seconds.
    last_altered: str = "1763373600.000"  # 2025-11-17T10:00:00.000Z
    comment: str | None = None


@dataclass
class Canned:
    """A canned result for one statement text."""

    columns: list[dict[str, Any]] = field(default_factory=list)
    rows: list[list[Any]] = field(default_factory=list)
    #: 202 answers before the result.
    polls: int = 0
    #: Rows per partition (None: one partition).
    partition_rows: int | None = None
    #: (status, code, sqlState, message) to fail with instead.
    error: tuple[int, str, str, str] | None = None
    #: Never finishes (always 202).
    endless: bool = False


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

    def iter_content(self, chunk_size: int = 1) -> Iterator[bytes]:
        """The body in ``chunk_size`` pieces, each a fresh copy (as the
        network gives them)."""
        for start in range(0, len(self.content), chunk_size):
            yield self.content[start : start + chunk_size]

    def close(self) -> None:
        pass


def _b64(part: str) -> bytes:
    return base64.urlsafe_b64decode(part + "=" * (-len(part) % 4))


def _cell(value: Any) -> Any:
    """A Python value as the SQL API sends it: a string, or null."""
    if value is None:
        return None
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (dict, list)):
        return json.dumps(value)
    return str(value)


def _literal(text: str) -> str:
    return text.replace("''", "'").replace("\\\\", "\\")


def _ident(text: str) -> str:
    return text.replace('""', '"')


_IDENT = r'"((?:[^"]|"")+)"'
_LIT = r"'((?:[^'\\]|''|\\\\)*)'"
_SHOW = re.compile(
    rf"^SHOW(?P<terse> TERSE)? (?P<what>DATABASES|SCHEMAS|TABLES)(?P<history> HISTORY)?"
    rf"(?: LIKE {_LIT})?"
    rf"(?: IN (?:(?P<account>ACCOUNT)|DATABASE {_IDENT}|SCHEMA {_IDENT}\.{_IDENT}))?"
    rf"(?: STARTS WITH {_LIT})? LIMIT (?P<limit>\d+)(?: FROM {_LIT})?$"
)
_INFO = re.compile(rf"FROM {_IDENT}\.INFORMATION_SCHEMA\.(TABLES|COLUMNS) WHERE")
_SELECT = re.compile(
    rf"^SELECT (?P<cols>\*|.+?) FROM {_IDENT}\.{_IDENT}\.{_IDENT}"
    rf"(?: ORDER BY (?P<order>.+?))? LIMIT (?P<limit>\d+)(?: OFFSET (?P<offset>\d+))?$"
)


def _sort_key(value: Any) -> tuple[int, Any]:
    if value is None:
        return (2, "")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return (0, value)
    return (1, str(value))


def _like(pattern: str, name: str) -> bool:
    regex = "".join(
        ".*" if ch == "%" else "." if ch == "_" else re.escape(ch) for ch in pattern
    )
    return re.fullmatch(regex, name, re.IGNORECASE | re.DOTALL) is not None


class FakeSnowflake:
    """The session every call goes through (see the module docstring)."""

    def __init__(self) -> None:
        #: (database, schema, table) -> FakeTable
        self.tables: dict[tuple[str, str, str], FakeTable] = {}
        self.databases: list[str] = []
        self.schemas: list[tuple[str, str]] = []
        self.canned: dict[str, Canned] = {}
        #: Users whose RSA_PUBLIC_KEY is set: user -> key label.
        self.users: dict[str, str] = {USER: "default"}
        self.oauth_clients: dict[str, str] = {CLIENT_ID: CLIENT_SECRET}
        self.issued: list[str] = []
        self.revoked: set[str] = set()
        self.requests: list[dict[str, Any]] = []
        self.cancelled: list[str] = []
        #: Answers to give instead, in order, for the next requests to a path
        #: kind ("submit", "poll", "partition", "token").
        self.overrides: dict[str, list[FakeResponse]] = {}
        #: Called with the URL before each request is answered (tests move a
        #: fake clock here).
        self.on_request: Callable[[str], None] | None = None
        self._statements: dict[str, dict[str, Any]] = {}
        self._handles = 0

    # -- setup ---------------------------------------------------------------

    def add_table(self, ref: str, table: FakeTable) -> FakeTable:
        db, schema, name = ref.split(".")
        self.tables[(db, schema, name)] = table
        if db not in self.databases:
            self.databases.append(db)
        if (db, schema) not in self.schemas:
            self.schemas.append((db, schema))
        return table

    def add(self, statement: str, canned: Canned) -> Canned:
        self.canned[statement] = canned
        return canned

    def override(self, kind: str, *answers: FakeResponse) -> None:
        self.overrides.setdefault(kind, []).extend(answers)

    # -- the session ---------------------------------------------------------

    def request(self, method: str, url: str, **kwargs: Any) -> FakeResponse:
        headers = dict(kwargs.get("headers") or {})
        data = kwargs.get("data")
        body: Any = None
        if isinstance(data, (bytes, str)) and url.startswith(HOST):
            body = json.loads(data)
        self.requests.append(
            {
                "method": method,
                "url": url,
                "params": dict(kwargs.get("params") or {}),
                "headers": headers,
                "body": body,
                "data": data,
                "timeout": kwargs.get("timeout"),
                "allow_redirects": kwargs.get("allow_redirects"),
            }
        )
        if self.on_request is not None:
            self.on_request(url)
        if url == TOKEN_URL and method == "POST":
            return self._pop("token") or self._token(data)
        if not url.startswith(HOST + "/api/v2/statements"):
            return FakeResponse(404, {"message": f"unexpected URL {url}"})
        refused = self._authenticate(headers)
        if refused is not None:
            return refused
        path = url[len(HOST) :]
        params = dict(kwargs.get("params") or {})
        if method == "POST" and path == "/api/v2/statements":
            return self._pop("submit") or self._submit(body, params)
        match = re.fullmatch(r"/api/v2/statements/([A-Za-z0-9-]+)(/cancel)?", path)
        if match is None:
            return FakeResponse(404, {"message": "unknown path"})
        handle = match.group(1)
        if match.group(2):
            self.cancelled.append(handle)
            return FakeResponse(200, {"code": "000604", "message": "canceled"})
        if "partition" in params:
            return self._pop("partition") or self._partition(
                handle, int(params["partition"])
            )
        return self._pop("poll") or self._poll(handle)

    def close(self) -> None:
        pass

    def _pop(self, kind: str) -> FakeResponse | None:
        queue = self.overrides.get(kind)
        return queue.pop(0) if queue else None

    # -- auth ----------------------------------------------------------------

    def _token(self, data: Any) -> FakeResponse:
        form = (
            data
            if isinstance(data, dict)
            else {
                k: v[-1]
                for k, v in parse_qs(
                    str(data if isinstance(data, str) else data.decode())
                ).items()
            }
        )
        if form.get("grant_type") != "client_credentials":
            return FakeResponse(400, {"error": "unsupported_grant_type"})
        if self.oauth_clients.get(str(form.get("client_id"))) != form.get(
            "client_secret"
        ):
            return FakeResponse(401, {"error": "invalid_client"})
        token = f"oat-{len(self.issued) + 1}-abcdef"
        self.issued.append(token)
        return FakeResponse(
            200, {"access_token": token, "expires_in": 3599, "token_type": "Bearer"}
        )

    def _authenticate(self, headers: dict[str, str]) -> FakeResponse | None:
        bearer = headers.get("Authorization", "")
        kind = headers.get("X-Snowflake-Authorization-Token-Type")
        if not bearer.startswith("Bearer "):
            return _unauthorized("390101", "Authorization header is missing.")
        token = bearer[len("Bearer ") :]
        if kind == "OAUTH":
            if token not in self.issued or token in self.revoked:
                return _unauthorized("390303", "Invalid OAuth access token.")
            return None
        if kind != "KEYPAIR_JWT":
            return _unauthorized("390101", "Unsupported token type.")
        try:
            header_b64, claims_b64, signature_b64 = token.split(".")
            header = json.loads(_b64(header_b64))
            claims = json.loads(_b64(claims_b64))
        except ValueError:
            return _unauthorized("390144", "JWT token is invalid.")
        user = str(claims.get("sub", "")).partition(".")[2]
        label = self.users.get(user)
        expected_sub = f"{ACCOUNT.upper()}.{user}"
        if (
            header != {"alg": "RS256", "typ": "JWT"}
            or label is None
            or claims.get("sub") != expected_sub
            or claims.get("iss") != f"{expected_sub}.{fingerprint(label)}"
            or not isinstance(claims.get("iat"), int)
            or not isinstance(claims.get("exp"), int)
            or not 0 < claims["exp"] - claims["iat"] <= 3600
            or claims["exp"] <= time.time()
        ):
            return _unauthorized("390144", "JWT token is invalid.")
        try:
            rsa_key(label).public_key().verify(
                _b64(signature_b64),
                f"{header_b64}.{claims_b64}".encode(),
                padding.PKCS1v15(),
                hashes.SHA256(),
            )
        except InvalidSignature:
            return _unauthorized("390144", "JWT token is invalid.")
        return None

    # -- statements ----------------------------------------------------------

    def _submit(self, body: Any, params: dict[str, Any]) -> FakeResponse:
        if not isinstance(body, dict) or not isinstance(body.get("statement"), str):
            return FakeResponse(400, {"code": "391900", "message": "bad request"})
        parameters = body.get("parameters") or {}
        if str(parameters.get("MULTI_STATEMENT_COUNT", "1")) != "1":
            return _failed(
                "000008", "0A000", "multi-statement requests are not allowed"
            )
        statement = body["statement"]
        canned = self.canned.get(statement)
        if canned is None:
            canned = self._answer(statement, body)
        cap = int(parameters.get("ROWS_PER_RESULTSET", "0") or 0)
        rows = canned.rows[:cap] if cap else canned.rows
        self._handles += 1
        handle = f"01bc0000-0000-0000-0000-{self._handles:012d}"
        self._statements[handle] = {
            "canned": canned,
            "rows": rows,
            "polls_left": canned.polls,
        }
        return FakeResponse(
            202,
            {
                "code": "333334",
                "message": "Asynchronous execution in progress.",
                "statementHandle": handle,
                "statementStatusUrl": f"/api/v2/statements/{handle}",
            },
        )

    def _poll(self, handle: str) -> FakeResponse:
        state = self._statements.get(handle)
        if state is None:
            return FakeResponse(
                404, {"code": "000709", "message": "statement not found"}
            )
        canned: Canned = state["canned"]
        if canned.endless or state["polls_left"] > 0:
            state["polls_left"] -= 1
            return FakeResponse(
                202,
                {"code": "333334", "message": "in progress", "statementHandle": handle},
            )
        if canned.error is not None:
            status, code, sql_state, message = canned.error
            return FakeResponse(
                status,
                {
                    "code": code,
                    "sqlState": sql_state,
                    "message": message,
                    "statementHandle": handle,
                },
            )
        parts = self._partitions(state)
        return FakeResponse(
            200,
            {
                "code": "090001",
                "statementHandle": handle,
                "sqlState": "00000",
                "message": "Statement executed successfully.",
                "resultSetMetaData": {
                    "numRows": len(state["rows"]),
                    "format": "jsonv2",
                    "rowType": canned.columns,
                    "partitionInfo": [
                        {"rowCount": len(p), "uncompressedSize": len(json.dumps(p))}
                        for p in parts
                    ],
                },
                "data": parts[0],
            },
        )

    def _partition(self, handle: str, index: int) -> FakeResponse:
        state = self._statements.get(handle)
        if state is None:
            return FakeResponse(
                404, {"code": "000709", "message": "statement not found"}
            )
        parts = self._partitions(state)
        if not 0 <= index < len(parts):
            return FakeResponse(422, {"code": "000709", "message": "no such partition"})
        return FakeResponse(200, {"data": parts[index]})

    @staticmethod
    def _partitions(state: dict[str, Any]) -> list[list[list[Any]]]:
        rows = [[_cell(v) for v in row] for row in state["rows"]]
        size = state["canned"].partition_rows
        if not size or not rows:
            return [rows]
        return [rows[i : i + size] for i in range(0, len(rows), size)]

    # -- the statements the connector writes itself ---------------------------

    def _answer(self, statement: str, body: dict[str, Any]) -> Canned:
        show = _SHOW.match(statement)
        if show is not None:
            return self._show(show)
        info = _INFO.search(statement)
        if info is not None:
            return self._info(
                _ident(info.group(1)), info.group(2), body.get("bindings") or {}
            )
        select = _SELECT.match(statement)
        if select is not None:
            return self._select(select)
        return Canned(
            error=(
                422,
                "002003",
                "42S02",
                "SQL compilation error: Object 'X' does not exist or not authorized.",
            )
        )

    def _show(self, match: re.Match[str]) -> Canned:
        groups = match.groups()
        what = match.group("what")
        like = groups[3]
        in_db, in_schema_db, in_schema = groups[5], groups[6], groups[7]
        starts, after = groups[8], groups[10]
        created = "1762000000.000"
        rows: list[list[Any]]
        if what == "DATABASES":
            columns = [
                col("created_on", "timestamp_ltz", scale=3),
                col("name"),
                col("kind"),
                col("owner"),
                col("comment"),
            ]
            rows = [
                [created, d, "STANDARD", "SYSADMIN", None]
                for d in sorted(self.databases)
            ]
        elif what == "SCHEMAS":
            columns = [
                col("created_on", "timestamp_ltz", scale=3),
                col("name"),
                col("database_name"),
                col("owner"),
                col("comment"),
            ]
            rows = [
                [created, s, d, "SYSADMIN", None]
                for d, s in sorted(self.schemas, key=lambda x: (x[0], x[1]))
                if in_db is None or d == _ident(in_db)
            ]
        else:
            columns = [
                col("created_on", "timestamp_ltz", scale=3),
                col("name"),
                col("database_name"),
                col("schema_name"),
                col("kind"),
                col("rows", "fixed", scale=0),
                col("bytes", "fixed", scale=0),
                col("owner"),
                col("comment"),
                col("automatic_clustering"),
            ]
            rows = [
                [
                    created,
                    t,
                    d,
                    s,
                    "TABLE",
                    len(tab.rows),
                    1024 * len(tab.rows),
                    "SYSADMIN",
                    tab.comment,
                    "OFF",
                ]
                for (d, s, t), tab in sorted(self.tables.items())
                if (in_db is None or d == _ident(in_db))
                and (
                    in_schema is None
                    or (d, s) == (_ident(in_schema_db), _ident(in_schema))
                )
            ]
        if like is not None:
            rows = [r for r in rows if _like(_literal(like), r[1])]
        if starts is not None:
            rows = [r for r in rows if r[1].startswith(_literal(starts))]
        if after is not None:
            rows = [r for r in rows if r[1] >= _literal(after)]
        if match.group("terse"):
            keep = [
                i
                for i, c in enumerate(columns)
                if c["name"]
                in ("created_on", "name", "kind", "database_name", "schema_name")
            ]
            columns = [columns[i] for i in keep]
            rows = [[r[i] for i in keep] for r in rows]
        return Canned(columns, rows[: int(match.group("limit"))])

    def _info(self, database: str, view: str, bindings: dict[str, Any]) -> Canned:
        values = [bindings[k]["value"] for k in sorted(bindings, key=int)]
        pairs = {(values[i], values[i + 1]) for i in range(0, len(values), 2)}
        if database not in self.databases:
            return Canned(
                error=(
                    422,
                    "002003",
                    "02000",
                    (
                        f"SQL compilation error: Database '{database}' does not exist or "
                        "not authorized."
                    ),
                )
            )
        if view == "TABLES":
            columns = [
                col("TABLE_SCHEMA"),
                col("TABLE_NAME"),
                col("TABLE_TYPE"),
                col("ROW_COUNT", "fixed", scale=0),
                col("BYTES", "fixed", scale=0),
                col("CREATED", "timestamp_ltz", scale=3),
                col("LAST_ALTERED", "timestamp_ltz", scale=3),
                col("COMMENT"),
                col("CLUSTERING_KEY"),
            ]
            rows = [
                [
                    s,
                    t,
                    tab.kind,
                    len(tab.rows),
                    1024 * len(tab.rows),
                    "1762000000.000",
                    tab.last_altered,
                    tab.comment,
                    None,
                ]
                for (d, s, t), tab in sorted(self.tables.items())
                if d == database and (s, t) in pairs
            ]
            return Canned(columns, rows)
        columns = [
            col("COLUMN_NAME"),
            col("DATA_TYPE"),
            col("IS_NULLABLE"),
            col("COLUMN_DEFAULT"),
            col("CHARACTER_MAXIMUM_LENGTH", "fixed", scale=0),
            col("NUMERIC_PRECISION", "fixed", scale=0),
            col("NUMERIC_SCALE", "fixed", scale=0),
            col("COMMENT"),
        ]
        rows = []
        for (d, s, t), tab in sorted(self.tables.items()):
            if d == database and (s, t) in pairs:
                for c in tab.columns:
                    kind = {"fixed": "NUMBER", "text": "TEXT", "real": "FLOAT"}.get(
                        c["type"], c["type"].upper()
                    )
                    rows.append(
                        [
                            c["name"],
                            kind,
                            "YES",
                            None,
                            16777216 if kind == "TEXT" else None,
                            38 if kind == "NUMBER" else None,
                            c.get("scale") if kind == "NUMBER" else None,
                            None,
                        ]
                    )
        return Canned(columns, rows)

    def _select(self, match: re.Match[str]) -> Canned:
        db, schema, name = (_ident(match.group(i)) for i in (2, 3, 4))
        table = self.tables.get((db, schema, name))
        if table is None:
            return Canned(
                error=(
                    422,
                    "002003",
                    "42S02",
                    (
                        f"SQL compilation error: Object '{db}.{schema}.{name}' does not "
                        "exist or not authorized."
                    ),
                )
            )
        names = [c["name"] for c in table.columns]
        picked = (
            names
            if match.group("cols") == "*"
            else [_ident(c) for c in re.findall(_IDENT, match.group("cols"))]
        )
        index = [names.index(c) for c in picked]
        rows = [[row[i] for i in index] for row in table.rows]
        if match.group("order"):
            keys = [
                names.index(_ident(c)) for c in re.findall(_IDENT, match.group("order"))
            ]
            order = sorted(
                range(len(table.rows)),
                key=lambda r: [_sort_key(table.rows[r][k]) for k in keys],
            )
            rows = [rows[i] for i in order]
        offset = int(match.group("offset") or 0)
        rows = rows[offset : offset + int(match.group("limit"))]
        return Canned([table.columns[i] for i in index], rows)

    # -- inspection ----------------------------------------------------------

    def submits(self) -> list[dict[str, Any]]:
        """The statement bodies submitted, in order."""
        return [
            r["body"]
            for r in self.requests
            if r["method"] == "POST" and r["url"] == HOST + "/api/v2/statements"
        ]

    def statements(self) -> list[str]:
        return [b["statement"] for b in self.submits()]

    def api_calls(self) -> list[dict[str, Any]]:
        return [r for r in self.requests if r["url"].startswith(HOST)]


def _unauthorized(code: str, message: str) -> FakeResponse:
    return FakeResponse(401, {"code": code, "message": message})


def _failed(code: str, state: str, message: str) -> FakeResponse:
    return FakeResponse(422, {"code": code, "sqlState": state, "message": message})


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


class _FakeSnowflakeAdapter(requests.adapters.HTTPAdapter):
    """Answers every request from a FakeSnowflake with a urllib3 response.
    ``body_break`` makes every SQL API body (or each one ``break_when(url,
    params)`` picks) break off after ``break_after`` bytes; ``gzip`` sends
    200 bodies gzip-encoded (``Content-Encoding: gzip``), as Snowflake sends
    later partitions."""

    def __init__(
        self,
        fake: FakeSnowflake,
        body_break: BaseException | None = None,
        *,
        break_when: Callable[[str, dict[str, str]], bool] | None = None,
        break_after: int = 8,
        gzip: bool = False,
    ) -> None:
        super().__init__()
        self._fake = fake
        self._break = body_break
        self._break_when = break_when
        self._break_after = break_after
        self._gzip = gzip

    def send(self, request: Any, stream: bool = False, timeout: Any = None, **_: Any):
        url, _sep, query = str(request.url).partition("?")
        params = {k: v[-1] for k, v in parse_qs(query, keep_blank_values=True).items()}
        answer = self._fake.request(
            str(request.method),
            url,
            params=params,
            data=request.body,
            headers=dict(request.headers),
            timeout=timeout,
            allow_redirects=False,
        )
        broken = None
        if (
            self._break is not None
            and url != TOKEN_URL
            and (self._break_when is None or self._break_when(url, params))
        ):
            broken = (self._break_after, self._break)
        content, headers = answer.content, dict(answer.headers)
        if self._gzip and answer.status_code == 200 and url != TOKEN_URL:
            import gzip as gzip_module

            content = gzip_module.compress(content)
            headers["Content-Encoding"] = "gzip"
        raw = urllib3.HTTPResponse(
            body=_Body(content, broken),
            headers=headers,
            status=answer.status_code,
            preload_content=False,
            request_method=str(request.method),
        )
        return self.build_response(request, raw)


def real_session(
    fake: FakeSnowflake,
    *,
    body_break: BaseException | None = None,
    break_when: Callable[[str, dict[str, str]], bool] | None = None,
    break_after: int = 8,
    gzip: bool = False,
) -> requests.Session:
    """A real ``requests.Session`` whose HTTPS goes to ``fake`` (see
    :class:`_FakeSnowflakeAdapter`). It ignores proxy settings (a proxy
    lookup may resolve names on macOS)."""
    session = requests.Session()
    session.trust_env = False
    session.mount(
        "https://",
        _FakeSnowflakeAdapter(
            fake,
            body_break,
            break_when=break_when,
            break_after=break_after,
            gzip=gzip,
        ),
    )
    return session
