"""SnowflakeToolkit: read-only Snowflake: databases, schemas, tables, columns, SELECT.

``config`` is the tenant's credential blob (``account``, ``user``, ``auth``,
the key or the OAuth client, ``role``, ``warehouse``, optional ``database``
and ``schema``; see :mod:`.credentials`). Hosts build it server-side; no
tool argument can change the account, the user, the key, the role or the
warehouse. There is no per-person OAuth: the credential belongs to a
Snowflake service user the customer creates, so a host classifies this
toolkit as a per-tenant credential (first party).

The toolkit is ``snowflake``, the same name as Composio's Snowflake
toolkit, and it keeps Composio's slugs and argument names for the read
tools both have (``SNOWFLAKE_SHOW_DATABASES``, ``_SHOW_SCHEMAS``,
``_SHOW_TABLES``), so a caller's call keeps working when a host routes it
here. ``SNOWFLAKE_RUN_SELECT`` (primary) is the SELECT-only counterpart of
Composio's ``SNOWFLAKE_EXECUTE_SQL`` (same ``statement``, ``database``,
``schema_name`` and ``timeout`` arguments). ``role`` and ``warehouse``,
which Composio's tools take, are server-controlled here and refused.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from aisquare.pipe import (
    ERROR_FORBIDDEN,
    ERROR_INVALID_ARGUMENTS,
    ERROR_NOT_CONNECTED,
    ERROR_NOT_FOUND,
    ERROR_RATE_LIMITED,
    ERROR_TIMEOUT,
    ERROR_UNAUTHORIZED,
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

from aisquare_pipe_snowflake.client import (
    SnowflakeAPIError,
    SnowflakeClient,
    StatementResult,
)
from aisquare_pipe_snowflake.credentials import (
    AUTH_KEYPAIR,
    SnowflakeCredentials,
    SnowflakeNotConfigured,
    load_credentials,
)
from aisquare_pipe_snowflake.metadata import describe_table
from aisquare_pipe_snowflake.refs import (
    MAX_REF_CHARS,
    BadReference,
    SchemaRef,
    parse_identifier,
    parse_schema,
    parse_table,
    quote,
    shown,
)
from aisquare_pipe_snowflake.sqlguard import (
    MAX_SQL_CHARS,
    NotReadOnly,
    check_context,
    check_select_only,
)

#: Rows one RUN_SELECT returns at most, and by default.
MAX_ROWS = 5000
DEFAULT_ROWS = 100
#: Bytes of JSON rows one tool call returns at most (what reaches the model).
MAX_RESULT_BYTES = 4 * 1024 * 1024
#: Objects one SHOW call lists at most, and by default.
MAX_LIMIT = 1000
DEFAULT_LIMIT = 100
#: A statement's deadline when neither the caller (``timeout``) nor the host
#: budget asks for less; the most a caller may ask for.
RUN_SELECT_TIMEOUT = 60
METADATA_TIMEOUT = 30
MAX_TIMEOUT = 600
MAX_PATTERN_CHARS = 255

#: SHOW output columns each tool returns (TERSE output has a subset).
_DATABASE_FIELDS = (
    "name",
    "kind",
    "created_on",
    "owner",
    "comment",
    "origin",
    "retention_time",
    "dropped_on",
)
_SCHEMA_FIELDS = (
    "name",
    "database_name",
    "kind",
    "created_on",
    "owner",
    "comment",
    "retention_time",
    "dropped_on",
)
_TABLE_FIELDS = (
    "name",
    "database_name",
    "schema_name",
    "kind",
    "created_on",
    "rows",
    "bytes",
    "owner",
    "comment",
    "cluster_by",
    "dropped_on",
)

_TIMEOUT = {
    "type": "integer",
    "minimum": 1,
    "maximum": MAX_TIMEOUT,
    "description": f"Seconds the statement may run, 1-{MAX_TIMEOUT} (never more than the "
    "host's time budget).",
}
_LIKE = {
    "type": "string",
    "maxLength": MAX_PATTERN_CHARS,
    "description": "Only names matching this SQL LIKE pattern (case-insensitive; % and _ "
    "are wildcards), e.g. '%POLICY%'.",
}
_STARTS_WITH = {
    "type": "string",
    "maxLength": MAX_PATTERN_CHARS,
    "description": "Only names starting with this text (case-sensitive: unquoted names are "
    "upper case).",
}
_LIMIT = {
    "type": "integer",
    "minimum": 1,
    "maximum": MAX_LIMIT,
    "default": DEFAULT_LIMIT,
    "description": f"Most objects to list, 1-{MAX_LIMIT}.",
}
_FROM_NAME = {
    "type": "string",
    "maxLength": MAX_PATTERN_CHARS,
    "description": "List from this name on (the previous call's next_from_name), for the "
    "next page.",
}
_TERSE = {
    "type": "boolean",
    "default": False,
    "description": "true: only each object's name, kind and creation time.",
}
_HISTORY = {
    "type": "boolean",
    "default": False,
    "description": "true: also list dropped objects still in Time Travel.",
}
_DATABASE = {
    "type": "string",
    "maxLength": MAX_REF_CHARS,
    "description": "A database name (default: the configured database). Unquoted names are "
    'case-insensitive; "Quoted" keeps the case.',
}
# Composio's in_scope: which scope a SHOW lists. Each value maps to the scope
# the tool builds from database / schema (and their configured defaults).
_SCHEMA_SCOPES = ("ACCOUNT", "DATABASE")
_TABLE_SCOPES = ("ACCOUNT", "DATABASE", "SCHEMA")


def _in_scope_schema(scopes: tuple[str, ...], meaning: str) -> dict[str, Any]:
    # No "enum": core would refuse "database" for DATABASE, and Composio's
    # callers may send either case.
    return {
        "type": "string",
        "maxLength": 16,
        "description": f"Where to list: {' or '.join(scopes)} (any case). {meaning} "
        "Default: the narrowest scope the arguments and the configured defaults give.",
    }


SHOW_DATABASES = ToolSpec(
    slug="SNOWFLAKE_SHOW_DATABASES",
    description=(
        "List the Snowflake databases the company's warehouse lets us see: each database's "
        "name, kind, owner, comment and creation time. Then list its schemas with "
        "SNOWFLAKE_SHOW_SCHEMAS."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "like_pattern": _LIKE,
            "starts_with": _STARTS_WITH,
            "limit": _LIMIT,
            "from_name": _FROM_NAME,
            "terse": _TERSE,
            "history": _HISTORY,
            "timeout": _TIMEOUT,
        },
        "additionalProperties": False,
    },
    read_only=True,
    tags=("list", "databases", "warehouse", "snowflake", "browse", "catalog"),
)

SHOW_SCHEMAS = ToolSpec(
    slug="SNOWFLAKE_SHOW_SCHEMAS",
    description=(
        "List the schemas in one Snowflake database (default: the configured database; "
        "without one, every database we can see): each schema's name, database, owner and "
        "comment. Then list a schema's tables with SNOWFLAKE_SHOW_TABLES."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "database": _DATABASE,
            "in_scope": _in_scope_schema(
                _SCHEMA_SCOPES,
                "ACCOUNT lists every database's schemas (without database); DATABASE "
                "lists one database's (database, or the configured one).",
            ),
            "like_pattern": _LIKE,
            "starts_with": _STARTS_WITH,
            "limit": _LIMIT,
            "from_name": _FROM_NAME,
            "terse": _TERSE,
            "history": _HISTORY,
            "timeout": _TIMEOUT,
        },
        "additionalProperties": False,
    },
    read_only=True,
    tags=("list", "schemas", "database", "warehouse", "snowflake", "browse"),
)

SHOW_TABLES = ToolSpec(
    slug="SNOWFLAKE_SHOW_TABLES",
    description=(
        "List the tables in one Snowflake schema or database (default: the configured "
        "ones): each table's name, database, schema, kind, row count, size, owner and "
        "comment. Read a table's columns and when it last changed with "
        "SNOWFLAKE_DESCRIBE_TABLE."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "database": _DATABASE,
            "schema": {
                "type": "string",
                "maxLength": MAX_REF_CHARS,
                "description": "A schema name, or DATABASE.SCHEMA (default: the configured "
                "schema; without one, the whole database).",
            },
            "in_scope": _in_scope_schema(
                _TABLE_SCOPES,
                "ACCOUNT lists every table we can see (without database or schema); "
                "DATABASE one database's (database, or the configured one; without "
                "schema); SCHEMA one schema's (schema, or the configured one).",
            ),
            "like_pattern": _LIKE,
            "starts_with": _STARTS_WITH,
            "limit": _LIMIT,
            "from_name": _FROM_NAME,
            "terse": _TERSE,
            "history": _HISTORY,
            "timeout": _TIMEOUT,
        },
        "additionalProperties": False,
    },
    read_only=True,
    tags=("list", "tables", "schema", "warehouse", "snowflake", "browse"),
)

DESCRIBE_TABLE = ToolSpec(
    slug="SNOWFLAKE_DESCRIBE_TABLE",
    description=(
        "Describe one Snowflake table or view: its columns (name, type, nullable, default, "
        "comment), its kind, row count and size, and last_altered, which changes when the "
        "table's data or definition changes. Use it before writing a query, and to check "
        "whether a table changed."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "table": {
                "type": "string",
                "minLength": 1,
                "maxLength": MAX_REF_CHARS,
                "description": "The table: DATABASE.SCHEMA.TABLE, or SCHEMA.TABLE / TABLE "
                "with the configured database and schema.",
            },
            "timeout": _TIMEOUT,
        },
        "required": ["table"],
        "additionalProperties": False,
    },
    read_only=True,
    tags=(
        "describe",
        "schema",
        "columns",
        "table",
        "metadata",
        "last altered",
        "snowflake",
    ),
)

RUN_SELECT = ToolSpec(
    slug="SNOWFLAKE_RUN_SELECT",
    description=(
        "Run ONE read-only Snowflake SQL query (SELECT or WITH ... SELECT) on the company's "
        "Snowflake data warehouse and return the rows: each row as an object keyed by "
        "column name, with the columns' types. Anything else (INSERT, UPDATE, DELETE, "
        "MERGE, CREATE, DROP, COPY, CALL, scripts, several statements) is refused. Name "
        "tables DATABASE.SCHEMA.TABLE, or set database and schema_name. Results stop at "
        "max_rows rows (truncated says so); the query is stopped at its timeout."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "statement": {
                "type": "string",
                "minLength": 1,
                "maxLength": MAX_SQL_CHARS,
                "description": "One Snowflake SQL SELECT (or WITH ... SELECT) statement.",
            },
            "database": {
                **_DATABASE,
                "description": "The database for table names written without one "
                "(default: the configured database).",
            },
            "schema_name": {
                "type": "string",
                "maxLength": MAX_REF_CHARS,
                "description": "The schema for table names written without one (default: "
                "the configured schema).",
            },
            "max_rows": {
                "type": "integer",
                "minimum": 1,
                "maximum": MAX_ROWS,
                "default": DEFAULT_ROWS,
                "description": f"Most rows to return, 1-{MAX_ROWS}.",
            },
            "timeout": _TIMEOUT,
        },
        "required": ["statement"],
        "additionalProperties": False,
    },
    read_only=True,
    tags=(
        PRIMARY_TAG,
        "query",
        "sql",
        "select",
        "warehouse",
        "rows",
        "analytics",
        "report",
        "snowflake",
    ),
)

TOOL_SPECS = (RUN_SELECT, DESCRIBE_TABLE, SHOW_TABLES, SHOW_SCHEMAS, SHOW_DATABASES)

#: Snowflake error codes, by what they mean for a caller.
_TIMEOUT_CODES = frozenset({"000604", "000630"})
_NOT_FOUND_CODES = frozenset({"002003", "002043"})
_NOT_FOUND_STATES = frozenset({"02000", "42S02"})
_PERMISSION_CODES = frozenset({"003001", "003003", "003011", "390189"})
_PERMISSION_STATES = frozenset({"42501"})
_NO_WAREHOUSE_CODES = frozenset({"000606"})


def _classify(
    e: SnowflakeAPIError, creds: SnowflakeCredentials
) -> tuple[str, str | None]:
    """``(error_code, hint)``: the failure's kind, and what to fix where the
    cause is known (no Snowflake text in it)."""
    role, warehouse = shown(creds.role), shown(creds.warehouse)
    if e.timed_out or e.code in _TIMEOUT_CODES:
        return ERROR_TIMEOUT, None
    if e.status == 429:
        return ERROR_RATE_LIMITED, None
    if e.credential_refused or e.status == 401:
        if creds.auth == AUTH_KEYPAIR:
            hint = (
                f"Snowflake refused the key-pair JWT for user {creds.user}: the user's "
                "RSA_PUBLIC_KEY is not this key's public key, the account identifier is "
                "wrong, or the user is disabled; the tenant's Snowflake credential must be "
                "fixed"
            )
        else:
            hint = (
                "Snowflake or the identity provider refused the OAuth client: a wrong or "
                "expired client secret, or a token the External OAuth integration does not "
                "accept; the tenant's Snowflake credential must be fixed"
            )
        return ERROR_UNAUTHORIZED, hint
    if e.code in _NO_WAREHOUSE_CODES:
        return ERROR_FORBIDDEN, (
            f"role {role} cannot use warehouse {warehouse} (grant it USAGE, or check "
            "that the warehouse exists and can resume)"
        )
    if (
        e.code in _PERMISSION_CODES
        or e.sql_state in _PERMISSION_STATES
        or e.status == 403
    ):
        return ERROR_FORBIDDEN, (
            f"role {role} lacks a grant for this (USAGE on the database and schema, "
            "SELECT on the table), or the role is not granted to the user"
        )
    if e.code in _NOT_FOUND_CODES or e.sql_state in _NOT_FOUND_STATES:
        return ERROR_NOT_FOUND, f"it does not exist, or role {role} has no grant on it"
    if e.status == 422:
        return ERROR_INVALID_ARGUMENTS, None
    if e.status is None or 300 <= e.status < 400:
        return ERROR_UPSTREAM_ERROR, None
    return error_code_for_status(e.status), None


def _describe(e: SnowflakeAPIError, creds: SnowflakeCredentials) -> tuple[str, str]:
    """``(error_code, message)``: the failure, Snowflake's own text included,
    and what to fix where the cause is known: what the toolkit answers."""
    code, hint = _classify(e, creds)
    return code, f"{e.message} — {hint}" if hint else e.message


def failure(e: SnowflakeAPIError, creds: SnowflakeCredentials) -> ToolResult:
    """A Snowflake failure as a canonical ``ToolResult`` error."""
    code, message = _describe(e, creds)
    return ToolResult.fail(message, code=code, retry_after=e.retry_after)


class SnowflakeRequestError(ValueError):
    """Snowflake refused what the caller asked for: a statement that does not
    compile, a table that does not exist (or the role can't see). A
    ``ValueError``, so pipe-as-service answers it 422. ``status``, ``code``
    and ``sql_state`` are Snowflake's."""

    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        code: str | None = None,
        sql_state: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.sql_state = sql_state


#: Where Snowflake points into the statement ("line 3 at position 15").
_POSITION = re.compile(r"\bline (\d{1,6}) at position (\d{1,6})\b")


def source_error(e: SnowflakeAPIError, creds: SnowflakeCredentials) -> Exception:
    """A Snowflake failure as the source raises it: what the caller can fix
    (``invalid_arguments``, ``not_found``) is a
    :class:`SnowflakeRequestError` (422 in pipe-as-service); everything
    else (upstream, throttling, a timeout, a refused credential or grant) is
    a :class:`~aisquare.pipe.errors.PipelineError` (502), with
    ``retry_after`` when Snowflake gave one. The message is ``summary`` and
    the hint, never Snowflake's own text (it can quote the SQL, and hosts
    log these), but for a statement that does not compile it keeps where
    ("line 1 at position 7")."""
    code, hint = _classify(e, creds)
    text = e.summary
    if code == ERROR_INVALID_ARGUMENTS:
        where = _POSITION.search(e.detail)
        if where is not None:
            text += f" at line {where.group(1)}, position {where.group(2)}"
    if hint:
        text += f" — {hint}"
    if code in (ERROR_INVALID_ARGUMENTS, ERROR_NOT_FOUND):
        return SnowflakeRequestError(
            text, status=e.status, code=e.code, sql_state=e.sql_state
        )
    return PipelineError(text, retry_after=e.retry_after)


def _bounded_int(
    arguments: Mapping[str, Any], name: str, low: int, high: int
) -> int | None:
    value = arguments.get(name)
    if value is None:
        return None
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


def _text(arguments: Mapping[str, Any], name: str, limit: int) -> str | None:
    """A string argument up to ``limit`` characters with no control
    character (core checks types only), or None when absent or empty."""
    value = arguments.get(name)
    if value is None or value == "":
        return None
    if not isinstance(value, str) or len(value) > limit or not value.isprintable():
        raise ToolError(
            f"{name} must be a string of at most {limit} printable characters",
            code=ERROR_INVALID_ARGUMENTS,
        )
    return value


def literal(text: str) -> str:
    """``text`` as one Snowflake string literal (backslashes and quotes
    escaped)."""
    return "'" + text.replace("\\", "\\\\").replace("'", "''") + "'"


def _scope_word(arguments: Mapping[str, Any], scopes: tuple[str, ...]) -> str | None:
    """Composio's ``in_scope``, upper-cased: one of ``scopes``, or None."""
    value = arguments.get("in_scope")
    if value is None or value == "":
        return None
    word = value.strip().upper() if isinstance(value, str) else None
    if word not in scopes:
        raise ToolError(
            "in_scope must be " + " or ".join(scopes), code=ERROR_INVALID_ARGUMENTS
        )
    return word


def _bad_scope(message: str) -> ToolError:
    return ToolError(message, code=ERROR_INVALID_ARGUMENTS)


def _reference(parse: Any, *args: Any, **kwargs: Any) -> Any:
    try:
        return parse(*args, **kwargs)
    except BadReference as e:
        raise ToolError(str(e), code=ERROR_INVALID_ARGUMENTS) from None


def deadline(arguments: Mapping[str, Any], default: int) -> int:
    """A statement's deadline: the caller's ``timeout`` (1-600), else
    ``default``. The client lowers it to the host budget left."""
    asked = _bounded_int(arguments, "timeout", 1, MAX_TIMEOUT)
    return asked if asked is not None else default


def _show(
    what: str,
    arguments: Mapping[str, Any],
    *,
    scope: str | None,
    limit: int,
) -> str:
    """``SHOW [TERSE] <what> [HISTORY] [LIKE] [IN ...] [STARTS WITH] LIMIT
    [FROM]``, every caller value a quoted literal or a quoted identifier."""
    like = _text(arguments, "like_pattern", MAX_PATTERN_CHARS)
    starts = _text(arguments, "starts_with", MAX_PATTERN_CHARS)
    after = _text(arguments, "from_name", MAX_PATTERN_CHARS)
    parts = ["SHOW"]
    if arguments.get("terse") is True:
        parts.append("TERSE")
    parts.append(what)
    if arguments.get("history") is True:
        parts.append("HISTORY")
    if like is not None:
        parts.append(f"LIKE {literal(like)}")
    if scope is not None:
        parts.append(scope)
    if starts is not None:
        parts.append(f"STARTS WITH {literal(starts)}")
    parts.append(f"LIMIT {limit + 1}")
    if after is not None:
        parts.append(f"FROM {literal(after)}")
    return " ".join(parts)


def _listing(
    result: StatementResult, fields: tuple[str, ...], limit: int
) -> dict[str, Any]:
    items = [{k: row[k] for k in fields if k in row} for row in result.rows[:limit]]
    more = len(result.rows) > limit
    next_name = result.rows[limit].get("name") if more else None
    return {
        "items": items,
        "count": len(items),
        "has_more": more,
        "next_from_name": next_name if isinstance(next_name, str) else None,
        "query_id": result.query_id,
    }


class SnowflakeToolkit(Toolkit):
    """Snowflake, read-only: ``SNOWFLAKE_RUN_SELECT`` (primary),
    ``SNOWFLAKE_DESCRIBE_TABLE``, ``SNOWFLAKE_SHOW_TABLES``,
    ``SNOWFLAKE_SHOW_SCHEMAS`` and ``SNOWFLAKE_SHOW_DATABASES``. ``session``
    (tests, or a host that pools connections) is the ``requests.Session``
    every call goes through, the OAuth token request included."""

    name = "snowflake"
    version = "0.1.0"
    display_name = "Snowflake"
    description = (
        "Snowflake data warehouse, read-only: run SELECT queries, list databases, schemas "
        "and tables, and describe a table's columns and when it last changed."
    )
    auth_type = AuthType.CUSTOM
    oauth = None
    docs_url = "https://docs.snowflake.com/en/developer-guide/sql-api/reference"
    category = "data"
    keywords = (
        "snowflake",
        "data warehouse",
        "sql",
        "query",
        "tables",
        "schema",
        "database",
        "analytics",
    )
    examples = (
        "run a SQL query on our Snowflake warehouse",
        "what columns does the policies table in Snowflake have",
        "list the tables in our snowflake schema",
    )

    def __init__(self, *, session: Any = None) -> None:
        self._session = session

    def tools(self) -> list[ToolSpec]:
        return list(TOOL_SPECS)

    def validate_config(self, config: dict[str, Any]) -> bool:
        try:
            load_credentials(config)
        except SnowflakeNotConfigured:
            return False
        return True

    def call_tool(
        self,
        slug: str,
        arguments: Mapping[str, Any] | None,
        config: Mapping[str, Any] | None,
    ) -> ToolResult:
        """Core's ``call_tool``, except that a known tool on an unusable
        credential fails with why Snowflake is not configured (no request is
        made)."""
        if isinstance(slug, str) and self.get_tool(slug) is not None:
            try:
                load_credentials(config)
            except SnowflakeNotConfigured as e:
                return ToolResult.fail(
                    f"Snowflake is not configured: {e}", code=ERROR_NOT_CONNECTED
                )
            except Exception:  # noqa: BLE001 — call_tool never raises
                return ToolResult.fail(
                    "Snowflake is not configured", code=ERROR_NOT_CONNECTED
                )
        return super().call_tool(slug, arguments, config)

    def run_tool(
        self, tool: ToolSpec, arguments: dict[str, Any], config: dict[str, Any]
    ) -> ToolResult:
        creds = load_credentials(config)
        # Arguments, the SQL guard and the caps are all checked before the
        # client exists: a refused call sends nothing, not even for a token.
        handler = {
            SHOW_DATABASES.slug: self._show_databases,
            SHOW_SCHEMAS.slug: self._show_schemas,
            SHOW_TABLES.slug: self._show_tables,
            DESCRIBE_TABLE.slug: self._describe_table,
            RUN_SELECT.slug: self._run_select,
        }.get(tool.slug)
        if handler is None:
            raise ToolError(f"{tool.slug} is not implemented")
        call = handler(creds, arguments)
        with SnowflakeClient(
            creds, session=self._session, budget=time_budget(config)
        ) as client:
            try:
                return call(client)
            except SnowflakeAPIError as e:
                return failure(e, creds)

    # Each handler checks its arguments and returns the call to make.

    def _limit(self, arguments: Mapping[str, Any]) -> int:
        value = _bounded_int(arguments, "limit", 1, MAX_LIMIT)
        return DEFAULT_LIMIT if value is None else value

    def _show_databases(
        self, creds: SnowflakeCredentials, arguments: dict[str, Any]
    ) -> Any:
        limit = self._limit(arguments)
        statement = _show("DATABASES", arguments, scope=None, limit=limit)
        seconds = deadline(arguments, METADATA_TIMEOUT)

        def call(client: SnowflakeClient) -> ToolResult:
            result = client.execute(
                statement,
                row_limit=limit + 1,
                byte_limit=MAX_RESULT_BYTES,
                timeout_seconds=seconds,
            )
            listing = _listing(result, _DATABASE_FIELDS, limit)
            return ToolResult.ok({"databases": listing.pop("items"), **listing})

        return call

    def _show_schemas(
        self, creds: SnowflakeCredentials, arguments: dict[str, Any]
    ) -> Any:
        limit = self._limit(arguments)
        in_scope = _scope_word(arguments, _SCHEMA_SCOPES)
        text = _text(arguments, "database", MAX_REF_CHARS)
        database = (
            _reference(parse_identifier, text, "database")
            if text is not None
            else creds.database
        )
        if in_scope == "ACCOUNT":
            if text is not None:
                raise _bad_scope(
                    "in_scope ACCOUNT lists every database's schemas: leave database out"
                )
            database = None
        elif in_scope == "DATABASE" and not database:
            raise _bad_scope(
                "in_scope DATABASE needs a database (none is given or configured)"
            )
        scope = f"IN DATABASE {quote(database)}" if database else "IN ACCOUNT"
        statement = _show("SCHEMAS", arguments, scope=scope, limit=limit)
        seconds = deadline(arguments, METADATA_TIMEOUT)

        def call(client: SnowflakeClient) -> ToolResult:
            result = client.execute(
                statement,
                row_limit=limit + 1,
                byte_limit=MAX_RESULT_BYTES,
                timeout_seconds=seconds,
            )
            listing = _listing(result, _SCHEMA_FIELDS, limit)
            return ToolResult.ok(
                {
                    "database": shown(database) if database else None,
                    "schemas": listing.pop("items"),
                    **listing,
                }
            )

        return call

    def _show_tables(
        self, creds: SnowflakeCredentials, arguments: dict[str, Any]
    ) -> Any:
        limit = self._limit(arguments)
        in_scope = _scope_word(arguments, _TABLE_SCOPES)
        db_text = _text(arguments, "database", MAX_REF_CHARS)
        database = (
            _reference(parse_identifier, db_text, "database")
            if db_text is not None
            else creds.database
        )
        schema_text = _text(arguments, "schema", MAX_REF_CHARS)
        where: SchemaRef | str | None
        if in_scope == "ACCOUNT":
            if db_text is not None or schema_text is not None:
                raise _bad_scope(
                    "in_scope ACCOUNT lists every table: leave database and schema out"
                )
            where = None
        elif in_scope == "DATABASE":
            if schema_text is not None:
                raise _bad_scope(
                    "in_scope DATABASE lists a whole database: leave schema out"
                )
            if not database:
                raise _bad_scope(
                    "in_scope DATABASE needs a database (none is given or configured)"
                )
            where = database
        elif schema_text is not None:
            where = _reference(parse_schema, schema_text, "schema", database=database)
        elif database and creds.schema and (db_text is None or in_scope == "SCHEMA"):
            where = SchemaRef(database, creds.schema)
        elif in_scope == "SCHEMA":
            raise _bad_scope(
                "in_scope SCHEMA needs a schema and its database (none is given or "
                "configured)"
            )
        else:
            where = database
        if isinstance(where, SchemaRef):
            scope, named = f"IN SCHEMA {where.sql()}", where.display()
        elif where:
            scope, named = f"IN DATABASE {quote(where)}", shown(where)
        else:
            scope, named = "IN ACCOUNT", None
        statement = _show("TABLES", arguments, scope=scope, limit=limit)
        seconds = deadline(arguments, METADATA_TIMEOUT)

        def call(client: SnowflakeClient) -> ToolResult:
            result = client.execute(
                statement,
                row_limit=limit + 1,
                byte_limit=MAX_RESULT_BYTES,
                timeout_seconds=seconds,
            )
            listing = _listing(result, _TABLE_FIELDS, limit)
            return ToolResult.ok(
                {"scope": named, "tables": listing.pop("items"), **listing}
            )

        return call

    def _describe_table(
        self, creds: SnowflakeCredentials, arguments: dict[str, Any]
    ) -> Any:
        ref = _reference(
            parse_table,
            arguments.get("table"),
            "table",
            database=creds.database,
            schema=creds.schema,
        )
        seconds = deadline(arguments, METADATA_TIMEOUT)

        def call(client: SnowflakeClient) -> ToolResult:
            described = describe_table(client, ref, timeout_seconds=seconds)
            if described is None:
                return ToolResult.fail(
                    f"no table or view {ref.display()} is visible to role "
                    f"{shown(creds.role)} (it does not exist, or the role has no grant on it)",
                    code=ERROR_NOT_FOUND,
                )
            return ToolResult.ok(described)

        return call

    def _run_select(
        self, creds: SnowflakeCredentials, arguments: dict[str, Any]
    ) -> Any:
        raw = arguments.get("statement")
        if not isinstance(raw, str):
            raise ToolError("statement must be a string", code=ERROR_INVALID_ARGUMENTS)
        try:
            statement = check_select_only(raw)
        except NotReadOnly as e:
            raise ToolError(str(e), code=ERROR_INVALID_ARGUMENTS) from None
        db_text = _text(arguments, "database", MAX_REF_CHARS)
        database = (
            _reference(parse_identifier, db_text, "database")
            if db_text is not None
            else None
        )
        schema_text = _text(arguments, "schema_name", MAX_REF_CHARS)
        schema = (
            _reference(parse_identifier, schema_text, "schema_name")
            if schema_text is not None
            else None
        )
        try:
            check_context(database or creds.database, schema or creds.schema)
        except NotReadOnly as e:
            raise ToolError(str(e), code=ERROR_INVALID_ARGUMENTS) from None
        max_rows = _bounded_int(arguments, "max_rows", 1, MAX_ROWS) or DEFAULT_ROWS
        seconds = deadline(arguments, RUN_SELECT_TIMEOUT)

        def call(client: SnowflakeClient) -> ToolResult:
            result = client.execute(
                statement,
                row_limit=max_rows,
                byte_limit=MAX_RESULT_BYTES,
                timeout_seconds=seconds,
                database=database,
                schema=schema,
            )
            return ToolResult.ok(
                {
                    "columns": result.columns,
                    "rows": result.rows,
                    "row_count": len(result.rows),
                    "truncated": result.truncated,
                    "query_id": result.query_id,
                }
            )

        return call


__all__ = [
    "DESCRIBE_TABLE",
    "RUN_SELECT",
    "SHOW_DATABASES",
    "SHOW_SCHEMAS",
    "SHOW_TABLES",
    "TOOL_SPECS",
    "SnowflakeToolkit",
    "deadline",
    "failure",
    "literal",
]
