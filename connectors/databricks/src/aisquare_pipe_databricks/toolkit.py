"""DatabricksToolkit: read-only files (volumes, DBFS), SELECT-only SQL, catalog.

``config`` is the tenant's credential blob (``workspace_url``, ``auth``,
``token`` | ``client_id`` + ``client_secret``, ``warehouse_id``; see
:mod:`.credentials`). Hosts build it server-side; no tool argument can name a
host, a token or a warehouse. There is no per-person OAuth: a PAT or a
service principal belongs to the tenant, so a host classifies this toolkit as
a per-tenant credential (first party).

SQL runs only after :func:`.sqlguard.check_select_only` accepts it (one
SELECT or WITH ... SELECT, checked by a parser and a lexer, before any HTTP
call), with a row cap, a byte cap and a deadline after which the statement is
cancelled.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from aisquare.pipe import (
    ERROR_FORBIDDEN,
    ERROR_INVALID_ARGUMENTS,
    ERROR_NOT_CONNECTED,
    ERROR_RATE_LIMITED,
    ERROR_TIMEOUT,
    ERROR_UPSTREAM_ERROR,
    PRIMARY_TAG,
    AuthType,
    ToolError,
    Toolkit,
    ToolResult,
    ToolSpec,
    error_code_for_status,
    time_budget,
)
from aisquare.pipe.errors import PipelineError

from aisquare_pipe_databricks.client import (
    DatabricksAPIError,
    DatabricksClient,
    FileTooLarge,
)
from aisquare_pipe_databricks.credentials import (
    DatabricksCredentials,
    DatabricksNotConfigured,
    load_credentials,
)
from aisquare_pipe_databricks.files import (
    BadPath,
    file_envelope,
    file_metadata,
    list_entries,
    parse_path,
    read_file,
)
from aisquare_pipe_databricks.sqlguard import (
    MAX_SQL_CHARS,
    NotReadOnly,
    check_select_only,
)

MAX_PAGE_SIZE = 1000
DEFAULT_PAGE_SIZE = 100
MAX_FILE_BYTES = 100 * 1024 * 1024
DEFAULT_MAX_BYTES = 25 * 1024 * 1024
INLINE_TEXT_MAX = 1024 * 1024
MAX_ROWS = 10_000
DEFAULT_ROWS = 1000
#: The statement API's inline result cap (25 MiB) is the ceiling; this is
#: ours, sized for a 512 MiB pod: a result of many short values parses into
#: about 12 times its size in Python objects (117 MiB at 8 MiB of JSON, 232
#: MiB at 16 MiB; tests/test_memory.py).
SQL_BYTE_LIMIT = 8 * 1024 * 1024
DEFAULT_SQL_SECONDS = 60
MAX_SQL_SECONDS = 120
MIN_SQL_SECONDS = 5
_NAME = {
    "type": "string",
    "minLength": 1,
    "maxLength": 255,
    "pattern": "^[^\\x00-\\x1f/.]+$",
}
# The same rule in code (core doesn't enforce ``pattern``): one name part, no
# dots, slashes or control characters.
_NAME_RULE = re.compile(r"^[^\x00-\x1f/.]{1,255}$")


def _name(arguments: Mapping[str, Any], key: str) -> str | None:
    value = arguments.get(key)
    if value in (None, ""):
        return None
    if not isinstance(value, str) or not _NAME_RULE.match(value):
        raise ToolError(
            f"{key} must be one catalog/schema name", code=ERROR_INVALID_ARGUMENTS
        )
    return value


def _paging(max_results: int, what: str) -> dict[str, Any]:
    return {
        "max_results": {
            "type": "integer",
            "minimum": 1,
            "maximum": max_results,
            "default": min(100, max_results),
            "description": f"{what} per page, 1-{max_results}.",
        },
        "page_token": {
            "type": "string",
            "description": "next_page_token from the previous call.",
        },
    }


LIST_FILES = ToolSpec(
    slug="DATABRICKS_LIST_FILES",
    description=(
        "List the files and folders in a Databricks Unity Catalog volume directory "
        "(/Volumes/<catalog>/<schema>/<volume>/...) or a DBFS directory (dbfs:/...). Returns "
        "each entry's path, name, is_directory, size and modified_time (a version marker: it "
        "changes when the file is rewritten). Pass next_page_token back as page_token for "
        "more. Read a file with DATABRICKS_READ_FILE."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "minLength": 1,
                "maxLength": 4096,
                "description": "A directory: /Volumes/main/policies/docs/ or dbfs:/mnt/policies/.",
            },
            "page_size": {
                "type": "integer",
                "minimum": 1,
                "maximum": MAX_PAGE_SIZE,
                "default": DEFAULT_PAGE_SIZE,
                "description": f"Entries per page, 1-{MAX_PAGE_SIZE} (volumes only).",
            },
            "page_token": {
                "type": "string",
                "description": "next_page_token from the last call.",
            },
        },
        "required": ["path"],
        "additionalProperties": False,
    },
    read_only=True,
    tags=("list", "files", "folder", "volume", "dbfs", "unity catalog", "version"),
)

READ_FILE = ToolSpec(
    slug="DATABRICKS_READ_FILE",
    description=(
        "Read one file from a Databricks Unity Catalog volume (/Volumes/...) or DBFS "
        "(dbfs:/...): its bytes (returned as a file; text up to 1 MiB also inline as content) "
        "plus size, content_type and the version markers modified_time, etag and sha256, "
        "which change when the file is edited. include_content=false reads the metadata only, "
        "e.g. to check whether a policy document changed."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "minLength": 1,
                "maxLength": 4096,
                "description": "The file: /Volumes/main/policies/docs/travel.pdf or dbfs:/mnt/a.txt.",
            },
            "include_content": {
                "type": "boolean",
                "default": True,
                "description": "false: metadata and version markers only, no download.",
            },
            "max_bytes": {
                "type": "integer",
                "minimum": 1,
                "maximum": MAX_FILE_BYTES,
                "default": DEFAULT_MAX_BYTES,
                "description": f"Largest file to download, at most {MAX_FILE_BYTES} bytes.",
            },
        },
        "required": ["path"],
        "additionalProperties": False,
    },
    read_only=True,
    tags=(
        PRIMARY_TAG,
        "read",
        "download",
        "file",
        "document",
        "volume",
        "policy document",
    ),
)

RUN_SELECT = ToolSpec(
    slug="DATABRICKS_RUN_SELECT",
    description=(
        "Run ONE read-only SQL query (SELECT, or WITH ... SELECT) on the company's Databricks "
        "SQL warehouse and return the columns and rows. Anything that writes or changes "
        "something (INSERT, UPDATE, DELETE, MERGE, CREATE, DROP, ALTER, SET, USE, ...) or more "
        "than one statement is refused before it is sent. Rows are capped at row_limit "
        f"(at most {MAX_ROWS}); truncated says when there were more. A query that runs longer "
        "than timeout_seconds is cancelled."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "statement": {
                "type": "string",
                "minLength": 1,
                "maxLength": MAX_SQL_CHARS,
                "description": "The query, e.g. SELECT title, body FROM main.policies.docs LIMIT 50.",
            },
            "row_limit": {
                "type": "integer",
                "minimum": 1,
                "maximum": MAX_ROWS,
                "default": DEFAULT_ROWS,
                "description": f"Most rows returned, 1-{MAX_ROWS}.",
            },
            "catalog": {
                **_NAME,
                "description": "Default catalog for unqualified names.",
            },
            "schema": {**_NAME, "description": "Default schema for unqualified names."},
            "timeout_seconds": {
                "type": "integer",
                "minimum": MIN_SQL_SECONDS,
                "maximum": MAX_SQL_SECONDS,
                "default": DEFAULT_SQL_SECONDS,
                "description": f"Cancel the query after this long, {MIN_SQL_SECONDS}-{MAX_SQL_SECONDS}s.",
            },
        },
        "required": ["statement"],
        "additionalProperties": False,
    },
    read_only=True,
    tags=("sql", "query", "select", "warehouse", "table", "rows"),
)

LIST_CATALOGS = ToolSpec(
    slug="DATABRICKS_LIST_CATALOGS",
    description="List the Unity Catalog catalogs this Databricks principal can see: name, comment, owner.",
    input_schema={
        "type": "object",
        "properties": _paging(1000, "Catalogs"),
        "additionalProperties": False,
    },
    read_only=True,
    tags=("catalog", "unity catalog", "list", "metadata"),
)

LIST_SCHEMAS = ToolSpec(
    slug="DATABRICKS_LIST_SCHEMAS",
    description="List the schemas in one Unity Catalog catalog: name, full_name, comment.",
    input_schema={
        "type": "object",
        "properties": {"catalog_name": _NAME, **_paging(1000, "Schemas")},
        "required": ["catalog_name"],
        "additionalProperties": False,
    },
    read_only=True,
    tags=("schema", "unity catalog", "list", "metadata"),
)

LIST_TABLES = ToolSpec(
    slug="DATABRICKS_LIST_TABLES",
    description=(
        "List the tables and views in one Unity Catalog schema: name, full_name, table_type, "
        "columns (name and type), comment and updated_at."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "catalog_name": _NAME,
            "schema_name": _NAME,
            **_paging(50, "Tables"),
        },
        "required": ["catalog_name", "schema_name"],
        "additionalProperties": False,
    },
    read_only=True,
    tags=("table", "view", "columns", "unity catalog", "list", "metadata"),
)

TOOL_SPECS = (
    READ_FILE,
    LIST_FILES,
    RUN_SELECT,
    LIST_CATALOGS,
    LIST_SCHEMAS,
    LIST_TABLES,
)


def _describe(e: DatabricksAPIError) -> str:
    if e.status == 401:
        return (
            f"{e.message} — Databricks rejected the token or the service principal's "
            "secret; the tenant's Databricks credential must be fixed"
        )
    if e.status == 403:
        return (
            f"{e.message} — the Databricks principal lacks the grant this needs (READ VOLUME "
            "on the volume, SELECT on the table, CAN USE on the warehouse)"
        )
    return e.message


class DatabricksRequestError(ValueError):
    """Databricks refused what the caller asked for: a path that does not
    exist, a request it calls invalid (400). A ``ValueError``, so
    pipe-as-service answers it 422. ``status`` is Databricks's."""

    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


def _source_hint(e: DatabricksAPIError) -> str:
    if e.timed_out:
        return ""
    if e.status == 401:
        return "the tenant's Databricks credential was refused and must be fixed"
    if e.status == 403:
        return (
            "the Databricks principal lacks the grant this needs (READ VOLUME on the "
            "volume, or access to the DBFS path)"
        )
    if e.status == 404:
        return "the path does not exist"
    if e.status == 400:
        return "Databricks called the request invalid (check the path)"
    if e.status == 429:
        return "Databricks throttled the pull; retry later"
    return ""


def source_error(e: DatabricksAPIError) -> Exception:
    """A Databricks failure as the source raises it: what the caller can fix
    (a 400, a path that does not exist: 404) is a
    :class:`DatabricksRequestError` (422 in pipe-as-service); everything else
    (a refused credential or grant, throttling, a timeout, a 5xx, no answer)
    is a :class:`~aisquare.pipe.errors.PipelineError` (502), with
    ``retry_after`` when Databricks gave one. The message is ``summary`` and
    a hint, never Databricks's own text (it can name paths, and hosts log
    these)."""
    hint = _source_hint(e)
    text = "databricks-files-source: " + e.summary + (f" — {hint}" if hint else "")
    if not e.timed_out and e.status in (400, 404):
        return DatabricksRequestError(text, status=e.status)
    return PipelineError(text, retry_after=e.retry_after)


def failure(e: DatabricksAPIError) -> ToolResult:
    """A Databricks failure as a canonical ``ToolResult`` error."""
    if e.timed_out:
        code = ERROR_TIMEOUT
    elif e.status == 429:
        code = ERROR_RATE_LIMITED
    elif e.status is None or 300 <= e.status < 400:
        code = ERROR_UPSTREAM_ERROR
    else:
        code = error_code_for_status(e.status)
    return ToolResult.fail(_describe(e), code=code, retry_after=e.retry_after)


def _int(arguments: Mapping[str, Any], name: str, low: int, high: int) -> int:
    value = arguments[name]
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not low <= value <= high
    ):
        raise ToolError(
            f"{name} must be an integer from {low} to {high}",
            code=ERROR_INVALID_ARGUMENTS,
        )
    return value


def _textual(content_type: str) -> bool:
    return (
        content_type.startswith("text/")
        or content_type in ("application/json", "application/xml")
        or content_type.endswith(("+json", "+xml"))
    )


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def sql_result(result: dict[str, Any], row_limit: int) -> dict[str, Any]:
    """The statement API's answer as ``{statement_id, columns, rows,
    row_count, truncated}``; a FAILED/CANCELED state is a ToolError."""
    status = _dict(result.get("status"))
    state = status.get("state")
    if state != "SUCCEEDED":
        error = _dict(status.get("error"))
        message = str(error.get("message") or state or "the statement did not succeed")[
            :300
        ]
        permission = "PERMISSION" in str(error.get("error_code", "")).upper()
        raise ToolError(
            f"the query {str(state).lower()}: {message}",
            code=ERROR_FORBIDDEN if permission else ERROR_UPSTREAM_ERROR,
        )
    manifest = _dict(result.get("manifest"))
    columns = [
        {"name": c.get("name"), "type": c.get("type_name")}
        for c in _list(_dict(manifest.get("schema")).get("columns"))
        if isinstance(c, dict)
    ]
    data = _dict(result.get("result"))
    rows = _list(data.get("data_array"))[:row_limit]
    total = manifest.get("total_row_count")
    truncated = (
        bool(manifest.get("truncated"))
        or (isinstance(total, int) and total > len(rows))
        or isinstance(data.get("next_chunk_index"), int)
    )
    return {
        "statement_id": result.get("statement_id"),
        "columns": columns,
        "rows": rows,
        "row_count": len(rows),
        "truncated": truncated,
    }


class DatabricksToolkit(Toolkit):
    """Databricks, read-only: ``DATABRICKS_READ_FILE`` (primary),
    ``DATABRICKS_LIST_FILES``, ``DATABRICKS_RUN_SELECT`` and the Unity Catalog
    listings. ``session`` (tests, or a host that pools connections) is the
    ``requests.Session`` every call goes through; ``clock``/``sleep`` drive
    statement polling (tests)."""

    name = "databricks"
    version = "0.1.0"
    display_name = "Databricks"
    description = (
        "Databricks: read files in Unity Catalog volumes and DBFS, run read-only SQL "
        "(SELECT) on a SQL warehouse, and browse catalogs, schemas and tables."
    )
    auth_type = AuthType.API_KEY
    oauth = None
    docs_url = "https://docs.databricks.com/api/workspace/files"
    category = "data"
    keywords = (
        "databricks",
        "lakehouse",
        "unity catalog",
        "volume",
        "dbfs",
        "sql warehouse",
        "delta table",
        "query",
        "policy documents",
    )
    examples = (
        "read the travel policy pdf from our databricks volume",
        "select the latest rows from main.policies.rules in databricks",
        "list the tables in the policies schema",
    )

    def __init__(
        self, *, session: Any = None, clock: Any = None, sleep: Any = None
    ) -> None:
        self._session = session
        self._timing = {
            k: v for k, v in (("clock", clock), ("sleep", sleep)) if v is not None
        }

    def tools(self) -> list[ToolSpec]:
        return list(TOOL_SPECS)

    def validate_config(self, config: dict[str, Any]) -> bool:
        try:
            load_credentials(config)
        except DatabricksNotConfigured:
            return False
        return True

    def call_tool(
        self,
        slug: str,
        arguments: Mapping[str, Any] | None,
        config: Mapping[str, Any] | None,
    ) -> ToolResult:
        """Core's ``call_tool``, except that a known tool on an unusable
        credential fails with why Databricks is not configured, and SQL that
        is not one SELECT is refused (``forbidden``): neither makes a request."""
        if isinstance(slug, str) and self.get_tool(slug) is not None:
            try:
                load_credentials(config)
            except DatabricksNotConfigured as e:
                return ToolResult.fail(
                    f"Databricks is not configured: {e}", code=ERROR_NOT_CONNECTED
                )
            except Exception:  # noqa: BLE001 — call_tool never raises
                return ToolResult.fail(
                    "Databricks is not configured", code=ERROR_NOT_CONNECTED
                )
            if slug.upper() == RUN_SELECT.slug and isinstance(arguments, Mapping):
                statement = arguments.get("statement")
                if isinstance(statement, str):
                    try:
                        check_select_only(statement)
                    except NotReadOnly as e:
                        return ToolResult.fail(str(e), code=ERROR_FORBIDDEN)
        return super().call_tool(slug, arguments, config)

    def run_tool(
        self, tool: ToolSpec, arguments: dict[str, Any], config: dict[str, Any]
    ) -> ToolResult:
        creds = load_credentials(config)
        budget = time_budget(config)
        with DatabricksClient(
            creds, session=self._session, budget=budget, **self._timing
        ) as client:
            try:
                if tool.slug == READ_FILE.slug:
                    return self._read(client, arguments)
                if tool.slug == LIST_FILES.slug:
                    return self._list(client, arguments)
                if tool.slug == RUN_SELECT.slug:
                    return self._select(client, creds, arguments, budget)
                if tool.slug == LIST_CATALOGS.slug:
                    return self._catalog(client, "catalogs", {}, arguments)
                if tool.slug == LIST_SCHEMAS.slug:
                    scope = {"catalog_name": _name(arguments, "catalog_name")}
                    return self._catalog(client, "schemas", scope, arguments)
                if tool.slug == LIST_TABLES.slug:
                    scope = {
                        "catalog_name": _name(arguments, "catalog_name"),
                        "schema_name": _name(arguments, "schema_name"),
                    }
                    return self._catalog(client, "tables", scope, arguments)
            except BadPath as e:
                raise ToolError(str(e), code=ERROR_INVALID_ARGUMENTS) from None
            except DatabricksAPIError as e:
                return failure(e)
        raise ToolError(f"{tool.slug} is not implemented")

    def _read(self, client: DatabricksClient, arguments: dict[str, Any]) -> ToolResult:
        where = parse_path(arguments["path"])
        max_bytes = _int(arguments, "max_bytes", 1, MAX_FILE_BYTES)
        if arguments.get("include_content") is False:
            return ToolResult.ok(file_metadata(client, where))
        try:
            content, record = read_file(client, where, max_bytes=max_bytes)
        except FileTooLarge as e:
            raise ToolError(
                f"{e.message}; raise max_bytes or read the metadata only (include_content=false)",
                code=ERROR_INVALID_ARGUMENTS,
            ) from None
        data = dict(record)
        if len(content) <= INLINE_TEXT_MAX and _textual(
            str(record.get("content_type") or "")
        ):
            try:
                data["content"] = content.decode("utf-8")
            except UnicodeDecodeError:
                pass
        return ToolResult.ok(
            data,
            files=[
                file_envelope(record, content, source_id=f"databricks:{where.path}")
            ],
        )

    def _list(self, client: DatabricksClient, arguments: dict[str, Any]) -> ToolResult:
        where = parse_path(arguments["path"], directory=True)
        page_size = _int(arguments, "page_size", 1, MAX_PAGE_SIZE)
        entries, token = list_entries(
            client,
            where,
            page_size=page_size,
            page_token=arguments.get("page_token") or None,
        )
        return ToolResult.ok(
            {
                "path": where.path,
                "entries": entries,
                "has_more": token is not None,
                "next_page_token": token,
            }
        )

    def _select(
        self,
        client: DatabricksClient,
        creds: DatabricksCredentials,
        arguments: dict[str, Any],
        budget: float | None,
    ) -> ToolResult:
        if not creds.warehouse_id:
            return ToolResult.fail(
                "Databricks is not configured for SQL: the credential names no warehouse_id",
                code=ERROR_NOT_CONNECTED,
            )
        try:
            statement = check_select_only(
                arguments["statement"]
            )  # again: never trust a path
        except NotReadOnly as e:
            return ToolResult.fail(str(e), code=ERROR_FORBIDDEN)
        row_limit = _int(arguments, "row_limit", 1, MAX_ROWS)
        seconds = float(
            _int(arguments, "timeout_seconds", MIN_SQL_SECONDS, MAX_SQL_SECONDS)
        )
        if budget is not None:
            seconds = min(seconds, budget)
        result = client.run_statement(
            statement,
            warehouse_id=creds.warehouse_id,
            row_limit=row_limit,
            byte_limit=SQL_BYTE_LIMIT,
            deadline_seconds=seconds,
            catalog=_name(arguments, "catalog"),
            schema=_name(arguments, "schema"),
        )
        return ToolResult.ok(sql_result(result, row_limit))

    def _catalog(
        self,
        client: DatabricksClient,
        kind: str,
        scope: dict[str, Any],
        arguments: dict[str, Any],
    ) -> ToolResult:
        params = {**scope, "max_results": arguments.get("max_results") or 50}
        if arguments.get("page_token"):
            params["page_token"] = arguments["page_token"]
        body = client.list_catalog(kind, params)
        items = body.get(kind)
        keep = {
            "catalogs": ("name", "comment", "owner", "catalog_type"),
            "schemas": ("name", "full_name", "catalog_name", "comment", "owner"),
            "tables": (
                "name",
                "full_name",
                "table_type",
                "comment",
                "owner",
                "updated_at",
            ),
        }[kind]
        rows = []
        for item in items if isinstance(items, list) else []:
            if not isinstance(item, dict):
                continue
            row = {k: item.get(k) for k in keep}
            if kind == "tables" and isinstance(item.get("columns"), list):
                row["columns"] = [
                    {
                        "name": c.get("name"),
                        "type": c.get("type_text") or c.get("type_name"),
                    }
                    for c in item["columns"]
                    if isinstance(c, dict)
                ]
            rows.append(row)
        token = body.get("next_page_token")
        token = token if isinstance(token, str) and token else None
        return ToolResult.ok(
            {kind: rows, "has_more": token is not None, "next_page_token": token}
        )


__all__ = [
    "LIST_CATALOGS",
    "LIST_FILES",
    "LIST_SCHEMAS",
    "LIST_TABLES",
    "READ_FILE",
    "RUN_SELECT",
    "TOOL_SPECS",
    "DatabricksRequestError",
    "DatabricksToolkit",
    "failure",
    "source_error",
    "sql_result",
]
