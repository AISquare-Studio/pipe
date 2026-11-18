"""The read-only ``redshift`` toolkit: browse a customer's Amazon Redshift and run one SELECT.

Its config is the tenant's server-side blob (:mod:`.config`): the shared AWS
cross-account credential plus the ``redshift`` section. No tool argument
names an account, role, region, cluster, workgroup, database, database user
or secret, and the schemas refuse every argument they don't list.

- ``REDSHIFT_RUN_SELECT`` (primary): ONE read-only query, checked before any
  AWS call (:mod:`.sqlguard`), run through the Data API (ExecuteStatement →
  DescribeStatement → GetStatementResult) within the host's time budget
  (``config["timeout_seconds"]``, else :data:`DEFAULT_TIMEOUT_SECONDS`), and
  cancelled if it runs past it.
- ``REDSHIFT_LIST_SCHEMAS``, ``REDSHIFT_LIST_TABLES``,
  ``REDSHIFT_DESCRIBE_TABLE``: the Data API's own metadata calls
  (ListSchemas, ListTables, DescribeTable), paged with ``next_token``.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from typing import Any, ClassVar

from aisquare.pipe import (
    ERROR_INVALID_ARGUMENTS,
    ERROR_NOT_CONNECTED,
    ERROR_NOT_FOUND,
    ERROR_TIMEOUT,
    PRIMARY_TAG,
    ToolError,
    ToolResult,
    ToolSpec,
    time_budget,
)

from aisquare_pipe_aws.credentials import AwsAccessError, AwsNotConfiguredError
from aisquare_pipe_aws.redshift import dataapi, rows
from aisquare_pipe_aws.redshift.config import RedshiftConfig, is_configured
from aisquare_pipe_aws.redshift.query import run_select
from aisquare_pipe_aws.redshift.sqlguard import MAX_SQL_CHARS, NotReadOnly
from aisquare_pipe_aws.toolkit import _AwsToolkit

VERSION = "0.1.0"

#: A tool call's deadline when the host gives no time budget.
DEFAULT_TIMEOUT_SECONDS = 60.0
#: Rows one RUN_SELECT returns at most, and by default.
MAX_ROWS = 5000
DEFAULT_ROWS = 100
#: Bytes of JSON rows one RUN_SELECT returns at most (what reaches the model).
MAX_RESULT_BYTES = 4 * 1024 * 1024
MAX_PAGE_SIZE = 1000
DEFAULT_PAGE_SIZE = 100
MAX_NEXT_TOKEN = 8192
#: Redshift allows 1600 columns; DescribeTable pages them.
MAX_COLUMNS = 1600
MAX_NAME_CHARS = 127
#: A LIKE pattern for ListSchemas / ListTables: name characters, % and _.
_PATTERN = r"^[A-Za-z0-9_%$-]{1,128}$"
#: A schema or table name (sent as a Data API param, never as SQL text).
_NAME = re.compile(rf"^[^\x00-\x1f\x7f]{{1,{MAX_NAME_CHARS}}}$")


def _schema(
    properties: dict[str, Any], required: tuple[str, ...] = ()
) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": list(required),
        "additionalProperties": False,
    }


def _limit(description: str, default: int, maximum: int) -> dict[str, Any]:
    return {
        "type": "integer",
        "minimum": 1,
        "maximum": maximum,
        "default": default,
        "description": description,
    }


def _bounded(args: Mapping[str, Any], name: str, default: int, maximum: int) -> int:
    """The framework checks types, not bounds: clamp here."""
    value = args.get(name, default)
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 1 <= value <= maximum
    ):
        raise ValueError(f"{name} must be an integer from 1 to {maximum}")
    return value


def _name(args: Mapping[str, Any], key: str, *, required: bool) -> str | None:
    value = args.get(key)
    if value is None and not required:
        return None
    if not isinstance(value, str) or not _NAME.match(value):
        raise ValueError(f"{key} must be a name of 1-{MAX_NAME_CHARS} characters")
    return value


def _pattern(args: Mapping[str, Any], key: str) -> str | None:
    value = args.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not re.match(_PATTERN, value):
        raise ValueError(f"{key} must be a LIKE pattern of name characters, % and _")
    return value


def _token(args: Mapping[str, Any]) -> str | None:
    value = args.get("next_token")
    if value is None:
        return None
    if not isinstance(value, str) or not 1 <= len(value) <= MAX_NEXT_TOKEN:
        raise ValueError("next_token is not a token from a previous call")
    return value


def _page(items: list[dict[str, Any]], next_token: Any) -> dict[str, Any]:
    token = next_token if isinstance(next_token, str) and next_token else None
    return {"items": items, "has_more": token is not None, "next_token": token}


_NEXT_TOKEN = {
    "type": "string",
    "minLength": 1,
    "maxLength": MAX_NEXT_TOKEN,
    "description": "next_token from the previous call, to continue the listing.",
}
_NAME_ARG = {"type": "string", "minLength": 1, "maxLength": MAX_NAME_CHARS}
_PATTERN_ARG = {"type": "string", "pattern": _PATTERN, "minLength": 1, "maxLength": 128}

RUN_SELECT = ToolSpec(
    "REDSHIFT_RUN_SELECT",
    "Run ONE read-only SQL query (SELECT, or WITH ... SELECT) on the company's Amazon "
    "Redshift data warehouse and return its rows as JSON objects keyed by column, with the "
    "columns' names and types. Anything else (INSERT, UPDATE, DELETE, CREATE, DROP, COPY, "
    "UNLOAD, SELECT INTO, several statements, SET, CALL) is refused before it is sent. "
    "Returns at most max_rows rows (truncated says when more exist) and result_sha256, a hash "
    "of the rows to tell whether the data changed. Find tables with REDSHIFT_LIST_TABLES and "
    "their columns with REDSHIFT_DESCRIBE_TABLE first. Quote a column named like a keyword in "
    'double quotes ("comment").',
    _schema(
        {
            "sql": {
                "type": "string",
                "minLength": 1,
                "maxLength": MAX_SQL_CHARS,
                "description": "One SELECT or WITH ... SELECT statement (Redshift SQL).",
            },
            "max_rows": _limit(
                f"Most rows to return (default {DEFAULT_ROWS}).", DEFAULT_ROWS, MAX_ROWS
            ),
        },
        ("sql",),
    ),
    read_only=True,
    tags=(
        "sql",
        "query",
        "select",
        "rows",
        "warehouse",
        "redshift",
        "data",
        PRIMARY_TAG,
    ),
)

LIST_SCHEMAS = ToolSpec(
    "REDSHIFT_LIST_SCHEMAS",
    "List the schemas in the configured Amazon Redshift database (the namespaces holding its "
    "tables), optionally only those matching a LIKE pattern such as policy%. Then list a "
    "schema's tables with REDSHIFT_LIST_TABLES. While has_more is true, pass next_token back "
    "for the rest.",
    _schema(
        {
            "schema_pattern": {
                **_PATTERN_ARG,
                "description": "Only schemas whose name matches this LIKE pattern (% and _).",
            },
            "limit": _limit(
                f"Most schemas to return (default {DEFAULT_PAGE_SIZE}).",
                DEFAULT_PAGE_SIZE,
                MAX_PAGE_SIZE,
            ),
            "next_token": _NEXT_TOKEN,
        }
    ),
    read_only=True,
    tags=("list", "schemas", "namespaces", "database", "redshift", "browse"),
)

LIST_TABLES = ToolSpec(
    "REDSHIFT_LIST_TABLES",
    "List the tables and views in the configured Amazon Redshift database: each one's schema, "
    "name and type (TABLE, VIEW, ...). Narrow it to one schema, or to names matching a LIKE "
    "pattern. Read a table's columns with REDSHIFT_DESCRIBE_TABLE. While has_more is true, "
    "pass next_token back (with the same filters) for the rest.",
    _schema(
        {
            "schema": {
                **_NAME_ARG,
                "description": "Only this schema's tables (exact name).",
            },
            "table_pattern": {
                **_PATTERN_ARG,
                "description": "Only tables whose name matches this LIKE pattern (% and _).",
            },
            "limit": _limit(
                f"Most tables to return (default {DEFAULT_PAGE_SIZE}).",
                DEFAULT_PAGE_SIZE,
                MAX_PAGE_SIZE,
            ),
            "next_token": _NEXT_TOKEN,
        }
    ),
    read_only=True,
    tags=("list", "tables", "views", "redshift", "warehouse", "browse"),
)

DESCRIBE_TABLE = ToolSpec(
    "REDSHIFT_DESCRIBE_TABLE",
    "Describe one Amazon Redshift table or view: its columns in order, with each column's "
    "type, length, precision, scale, nullability and default. Use it to write a correct "
    "REDSHIFT_RUN_SELECT query.",
    _schema(
        {
            "schema": {**_NAME_ARG, "description": "The table's schema (e.g. public)."},
            "table": {**_NAME_ARG, "description": "The table or view name."},
        },
        ("schema", "table"),
    ),
    read_only=True,
    tags=("describe", "columns", "schema", "table", "types", "redshift"),
)


class RedshiftToolkit(_AwsToolkit):
    """Amazon Redshift (provisioned or Serverless), read-only, through the Data API.

    An :class:`~aisquare_pipe_aws.toolkit._AwsToolkit`: its ``auth_type``, its
    ``base_session`` (the service's own principal) and its ``_session``, which
    assumes the customer role through
    :func:`aisquare_pipe_aws.credentials.cached_session`, are the AWS
    toolkits' own."""

    name = "redshift"
    version = VERSION
    display_name = "Amazon Redshift"
    description = (
        "Query the company's Amazon Redshift data warehouse with read-only SQL (one SELECT), and "
        "list its schemas, tables and columns, through the Redshift Data API on a cross-account "
        "AWS role."
    )
    oauth = None
    category = "data"
    keywords = (
        "redshift",
        "amazon redshift",
        "aws",
        "data warehouse",
        "warehouse",
        "sql",
        "query",
        "tables",
        "analytics",
    )
    examples = (
        "run a SQL query on our Redshift warehouse",
        "list the tables in the Redshift policies schema",
        "which columns does the Redshift policy table have?",
    )
    docs_url = "https://docs.aws.amazon.com/redshift/latest/mgmt/data-api.html"

    _TOOLS = (RUN_SELECT, LIST_SCHEMAS, LIST_TABLES, DESCRIBE_TABLE)
    _HANDLERS: ClassVar[Mapping[str, str]] = {
        "REDSHIFT_RUN_SELECT": "_run_select",
        "REDSHIFT_LIST_SCHEMAS": "_list_schemas",
        "REDSHIFT_LIST_TABLES": "_list_tables",
        "REDSHIFT_DESCRIBE_TABLE": "_describe_table",
    }

    def tools(self) -> list[ToolSpec]:
        return list(self._TOOLS)

    def validate_config(self, config: dict[str, Any]) -> bool:
        return is_configured(config)

    def run_tool(
        self, tool: ToolSpec, arguments: dict[str, Any], config: dict[str, Any]
    ) -> ToolResult:
        handler: Callable[
            [RedshiftConfig, dict[str, Any], dataapi.Deadline], ToolResult
        ] = getattr(self, self._HANDLERS[tool.slug])
        budget = time_budget(config) or DEFAULT_TIMEOUT_SECONDS
        deadline = dataapi.Deadline(budget)
        try:
            cfg = RedshiftConfig.from_config(config)
            return handler(cfg, arguments, deadline)
        except AwsNotConfiguredError as e:
            raise ToolError(str(e), code=ERROR_NOT_CONNECTED) from None
        except AwsAccessError as e:
            raise ToolError(str(e), code=e.code) from None
        except NotReadOnly as e:
            raise ToolError(str(e), code=ERROR_INVALID_ARGUMENTS) from None
        except dataapi.StatementTimeout:
            raise ToolError(
                f"{tool.slug} did not finish within {budget:g} s; the statement was cancelled",
                code=ERROR_TIMEOUT,
            ) from None
        except dataapi.StatementFailed as e:
            raise ToolError(str(e), code=e.code) from None
        except ValueError as e:
            raise ToolError(str(e), code=ERROR_INVALID_ARGUMENTS) from None

    # -- the session: always #31's AssumeRole (_AwsToolkit._session) ------------------

    def _api(self, cfg: RedshiftConfig, deadline: dataapi.Deadline) -> Any:
        return dataapi.client(self._session(cfg.aws), cfg.target, deadline)

    # -- tools ---------------------------------------------------------------------------

    def _run_select(
        self, cfg: RedshiftConfig, args: dict[str, Any], deadline: dataapi.Deadline
    ) -> ToolResult:
        max_rows = _bounded(args, "max_rows", DEFAULT_ROWS, MAX_ROWS)
        result = run_select(
            str(args.get("sql", "")),
            open_session=lambda: self._session(cfg.aws),
            target=cfg.target,
            deadline=deadline,
            max_rows=max_rows,
            max_result_bytes=MAX_RESULT_BYTES,
        )
        return ToolResult.ok(
            {
                "columns": result.columns,
                "rows": result.rows,
                "row_count": len(result.rows),
                "total_rows": result.total_rows,
                "truncated": result.truncated,
                "result_sha256": result.result_sha256,
                "statement_id": result.statement_id,
                "duration_ms": result.duration_ms,
            }
        )

    def _list_schemas(
        self, cfg: RedshiftConfig, args: dict[str, Any], deadline: dataapi.Deadline
    ) -> ToolResult:
        pattern = _pattern(args, "schema_pattern")
        limit = _bounded(args, "limit", DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE)
        token = _token(args)
        page = dataapi.list_schemas(
            self._api(cfg, deadline),
            cfg.target,
            pattern=pattern,
            limit=limit,
            next_token=token,
        )
        items = [{"schema": s} for s in page.get("Schemas") or [] if isinstance(s, str)]
        return ToolResult.ok(_page(items, page.get("NextToken")))

    def _list_tables(
        self, cfg: RedshiftConfig, args: dict[str, Any], deadline: dataapi.Deadline
    ) -> ToolResult:
        schema = _name(args, "schema", required=False)
        pattern = _pattern(args, "table_pattern")
        limit = _bounded(args, "limit", DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE)
        token = _token(args)
        page = dataapi.list_tables(
            self._api(cfg, deadline),
            cfg.target,
            schema_pattern=schema,
            table_pattern=pattern,
            limit=limit,
            next_token=token,
        )
        items = []
        for table in page.get("Tables") or []:
            if not isinstance(table, dict):
                continue
            # The Data API reads the schema as a LIKE pattern ('_' matches any
            # character): keep the exact schema asked for only.
            if schema is not None and table.get("schema") != schema:
                continue
            items.append(
                {
                    "schema": table.get("schema"),
                    "name": table.get("name"),
                    "type": table.get("type"),
                }
            )
        return ToolResult.ok(_page(items, page.get("NextToken")))

    def _describe_table(
        self, cfg: RedshiftConfig, args: dict[str, Any], deadline: dataapi.Deadline
    ) -> ToolResult:
        schema = str(_name(args, "schema", required=True))
        table = str(_name(args, "table", required=True))
        api = self._api(cfg, deadline)
        listed: list[dict[str, Any]] = []
        token: str | None = None
        while True:
            deadline.check()
            page = dataapi.describe_table(
                api,
                cfg.target,
                schema=schema,
                table=table,
                limit=MAX_PAGE_SIZE,
                next_token=token,
            )
            for column in page.get("ColumnList") or []:
                if not isinstance(column, dict):
                    continue
                # Only the table asked for, should the Data API match more.
                if column.get("schemaName") not in (None, schema) or column.get(
                    "tableName"
                ) not in (None, table):
                    continue
                listed.append(column)
            token = page.get("NextToken") or None
            if token is None or len(listed) >= MAX_COLUMNS:
                break
        if not listed:
            raise ToolError(
                "no such table, or the database user can't see it", code=ERROR_NOT_FOUND
            )
        return ToolResult.ok(
            {"schema": schema, "table": table, "columns": rows.describe_columns(listed)}
        )
