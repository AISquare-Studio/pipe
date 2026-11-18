"""BigQueryToolkit: read-only BigQuery: datasets, tables, schemas, rows, SELECT.

``config`` is the tenant's credential blob (``service_account_json``,
``project_id``, optional ``location`` and ``max_bytes_billed``; see
:mod:`.credentials`). Hosts build it server-side; no tool argument can
change the key, the job project, the location or the cost ceiling. There is
no OAuth: the key belongs to a service account the customer creates, so a
host classifies this toolkit as a per-tenant credential (first party).

The toolkit is ``bigquery``, a name of its own: Composio's BigQuery toolkit
is ``googlebigquery``. Its tools map onto Composio's read tools (see the
README): ``GOOGLEBIGQUERY_QUERY`` -> ``BIGQUERY_RUN_SELECT`` (SELECT only),
``GOOGLEBIGQUERY_LIST_TABLE_DATA`` -> ``BIGQUERY_READ_TABLE_ROWS``,
``GOOGLEBIGQUERY_LIST_DATASETS`` / ``_LIST_TABLES`` / ``_GET_TABLE_SCHEMA``
-> ``BIGQUERY_LIST_DATASETS`` / ``_LIST_TABLES`` / ``_GET_TABLE_SCHEMA``.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from aisquare.pipe import (
    ERROR_FORBIDDEN,
    ERROR_INSUFFICIENT_SCOPE,
    ERROR_INVALID_ARGUMENTS,
    ERROR_NOT_CONNECTED,
    ERROR_NOT_FOUND,
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

from aisquare_pipe_bigquery.client import BigQueryAPIError, BigQueryClient
from aisquare_pipe_bigquery.credentials import (
    GIB,
    MAX_BYTES_BILLED_LIMIT,
    MIN_BYTES_BILLED,
    PROJECT_ID,
    BigQueryCredentials,
    BigQueryNotConfigured,
    load_credentials,
)
from aisquare_pipe_bigquery.query import (
    CapExceeded,
    NotATable,
    Rows,
    dry_run,
    read_table_rows,
    run_select,
    table_info,
)
from aisquare_pipe_bigquery.refs import (
    BadReference,
    TableRef,
    parse_dataset,
    parse_table,
)
from aisquare_pipe_bigquery.rows import millis_rfc3339, schema_fields
from aisquare_pipe_bigquery.sqlguard import (
    MAX_SQL_CHARS,
    NotReadOnly,
    check_select_only,
)

MAX_PAGE_SIZE = 1000
DEFAULT_PAGE_SIZE = 100
#: Rows one tool call returns at most, and by default.
MAX_ROWS = 5000
DEFAULT_ROWS = 100
#: Bytes of JSON rows one tool call returns at most (what reaches the model).
MAX_RESULT_BYTES = 4 * 1024 * 1024
#: The bytes a query may process unless the caller asks for less (or the
#: tenant's ceiling is lower).
DEFAULT_BYTES_PROCESSED = 1 * GIB
#: A tool's query job deadline when the host gives no time budget.
JOB_TIMEOUT_SECONDS = 60.0
MAX_PAGE_TOKEN_CHARS = 4096
MAX_REF_CHARS = 2200
MAX_SELECTED_FIELDS = 200
MAX_FIELD_CHARS = 300

_SIZE = {
    "type": "integer",
    "minimum": 1,
    "maximum": MAX_PAGE_SIZE,
    "default": DEFAULT_PAGE_SIZE,
    "description": f"Items per page, 1-{MAX_PAGE_SIZE}.",
}
_TOKEN = {
    "type": "string",
    "maxLength": MAX_PAGE_TOKEN_CHARS,
    "description": "next_page_token from the previous call, for the next page.",
}
_MAX_ROWS = {
    "type": "integer",
    "minimum": 1,
    "maximum": MAX_ROWS,
    "default": DEFAULT_ROWS,
    "description": f"Most rows to return, 1-{MAX_ROWS}.",
}

LIST_DATASETS = ToolSpec(
    slug="BIGQUERY_LIST_DATASETS",
    description=(
        "List the BigQuery datasets (schemas) the company's warehouse lets us read, in the "
        "configured Google Cloud project or in another project by id. Returns each dataset's "
        "id, project and location. Then list a dataset's tables with BIGQUERY_LIST_TABLES."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "project": {
                "type": "string",
                "maxLength": 100,
                "description": "Another Google Cloud project id whose datasets to list "
                "(default: the configured project).",
            },
            "page_size": _SIZE,
            "page_token": _TOKEN,
        },
        "additionalProperties": False,
    },
    read_only=True,
    tags=("list", "datasets", "schemas", "warehouse", "bigquery", "browse"),
)

LIST_TABLES = ToolSpec(
    slug="BIGQUERY_LIST_TABLES",
    description=(
        "List the tables and views in one BigQuery dataset: each table's id, its type "
        "(TABLE, VIEW, MATERIALIZED_VIEW, EXTERNAL, SNAPSHOT) and when it was created. "
        "Read a table's columns with BIGQUERY_GET_TABLE_SCHEMA."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "dataset": {
                "type": "string",
                "minLength": 1,
                "maxLength": MAX_REF_CHARS,
                "description": "The dataset: 'dataset' or 'project.dataset'.",
            },
            "page_size": _SIZE,
            "page_token": _TOKEN,
        },
        "required": ["dataset"],
        "additionalProperties": False,
    },
    read_only=True,
    tags=("list", "tables", "views", "dataset", "warehouse", "bigquery"),
)

GET_TABLE_SCHEMA = ToolSpec(
    slug="BIGQUERY_GET_TABLE_SCHEMA",
    description=(
        "Describe one BigQuery table: its columns (name, type, mode, description, nested "
        "fields), row count, size, partitioning and clustering columns, and "
        "last_modified_time, which changes when the table's data changes. Use it before "
        "writing a query, and to check whether a table changed."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "table": {
                "type": "string",
                "minLength": 1,
                "maxLength": MAX_REF_CHARS,
                "description": "The table: 'dataset.table' or 'project.dataset.table'.",
            },
        },
        "required": ["table"],
        "additionalProperties": False,
    },
    read_only=True,
    tags=(
        "schema",
        "columns",
        "describe",
        "table",
        "metadata",
        "last modified",
        "bigquery",
    ),
)

RUN_SELECT = ToolSpec(
    slug="BIGQUERY_RUN_SELECT",
    description=(
        "Run ONE read-only GoogleSQL query (SELECT or WITH ... SELECT) on the company's "
        "BigQuery data warehouse and return the rows: each row as an object keyed by column "
        "name, with the columns' types. Anything else (INSERT, UPDATE, DELETE, MERGE, CREATE, "
        "DROP, scripts, several statements) is refused. Reference tables as "
        "`project.dataset.table` or dataset.table, or set default_dataset. A dry run first "
        "checks how many bytes the query would scan; one over max_bytes_processed is refused "
        "before it runs, so filter on partition columns and select only the columns needed. "
        "dry_run=true returns that estimate only. Results stop at max_rows rows (truncated "
        "says so)."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "sql": {
                "type": "string",
                "minLength": 1,
                "maxLength": MAX_SQL_CHARS,
                "description": "One GoogleSQL SELECT (or WITH ... SELECT) statement.",
            },
            "default_dataset": {
                "type": "string",
                "maxLength": MAX_REF_CHARS,
                "description": "'dataset' or 'project.dataset' for table names written "
                "without one.",
            },
            "max_rows": _MAX_ROWS,
            "max_bytes_processed": {
                "type": "integer",
                "minimum": MIN_BYTES_BILLED,
                "maximum": MAX_BYTES_BILLED_LIMIT,
                "description": "Most bytes the query may scan (default 1 GiB, or the "
                "configured ceiling when that is lower; never above the ceiling).",
            },
            "dry_run": {
                "type": "boolean",
                "default": False,
                "description": "true: only estimate the bytes the query would scan.",
            },
        },
        "required": ["sql"],
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
        "bigquery",
    ),
)

READ_TABLE_ROWS = ToolSpec(
    slug="BIGQUERY_READ_TABLE_ROWS",
    description=(
        "Read rows straight from one BigQuery table without running a query (free: nothing "
        "is scanned or billed): up to max_rows rows from start_index, optionally only some "
        "columns. Returns the rows, the columns' types, total_rows, and the table's "
        "last_modified_time. When more rows remain, pass next_start_index back as "
        "start_index. Views can't be read this way: use BIGQUERY_RUN_SELECT."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "table": {
                "type": "string",
                "minLength": 1,
                "maxLength": MAX_REF_CHARS,
                "description": "The table: 'dataset.table' or 'project.dataset.table'.",
            },
            "max_rows": _MAX_ROWS,
            "start_index": {
                "type": "integer",
                "minimum": 0,
                "default": 0,
                "description": "The first row to read (0-based).",
            },
            "selected_fields": {
                "type": "array",
                "items": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": MAX_FIELD_CHARS,
                },
                "maxItems": MAX_SELECTED_FIELDS,
                "description": "Only these top-level columns.",
            },
        },
        "required": ["table"],
        "additionalProperties": False,
    },
    read_only=True,
    tags=("read", "rows", "table", "data", "records", "preview", "bigquery"),
)

TOOL_SPECS = (RUN_SELECT, READ_TABLE_ROWS, GET_TABLE_SCHEMA, LIST_TABLES, LIST_DATASETS)

#: BigQuery error reasons, by what they mean for a caller.
_RATE_LIMIT_REASONS = frozenset(
    {"rateLimitExceeded", "quotaExceeded", "jobRateLimitExceeded"}
)
_UPSTREAM_REASONS = frozenset(
    {"backendError", "internalError", "jobBackendError", "jobInternalError"}
)
_ARGUMENT_REASONS = frozenset(
    {
        "invalidQuery",
        "invalid",
        "bytesBilledLimitExceeded",
        "responseTooLarge",
        "resourcesExceeded",
        "billingTierLimitExceeded",
    }
)


#: A "Custom quota exceeded" 403: a quota the customer's administrator set
#: (query usage per day, per project or per user). Retrying fails until it
#: resets, so it is not a rate limit.
_CUSTOM_QUOTA = "custom quota"
#: Phrases in a 403 about a Google Drive-backed external table (a Sheet or a
#: Drive file): its read needs a Drive scope, which no role grants.
_DRIVE_PHRASES = ("drive credentials", "google drive")


def _custom_quota(e: BigQueryAPIError) -> bool:
    return e.reason == "quotaExceeded" and _CUSTOM_QUOTA in e.message.lower()


def _hint(e: BigQueryAPIError, project: str) -> str | None:
    """What to fix, where the cause is known (no BigQuery text in it). Only
    a 401 (a key or token Google refused) blames the key."""
    text = e.message.lower()
    if e.status == 401:
        return (
            "Google rejected the service account key (deleted, disabled or expired); the "
            "tenant's BigQuery credential must be fixed"
        )
    if _custom_quota(e):
        return (
            "a custom query quota the customer's administrator set (query usage per day, "
            "for the project or per user) is used up; retries fail until it resets at "
            "midnight Pacific time, or until the administrator raises it (see the "
            "bigquery connector README)"
        )
    if e.reason == "bytesBilledLimitExceeded":
        return (
            "BigQuery stopped the query at max_bytes_processed (nothing was billed); "
            "narrow it with a partition filter or fewer columns"
        )
    if e.reason == "responseTooLarge":
        return "add a LIMIT or select fewer columns"
    if e.reason == "billingNotEnabled":
        return f"billing is not enabled on project {project}, where the query jobs run"
    if (
        e.status == 403
        and error_code_for_status(403, www_authenticate=e.www_authenticate)
        == ERROR_INSUFFICIENT_SCOPE
    ):
        return (
            "the token lacks a scope the call needs; this connector only ever asks for "
            "bigquery.readonly"
        )
    if e.status == 403 and "vpc service controls" in text:
        return (
            "a VPC Service Controls perimeter blocks the request; the customer must add "
            "an ingress rule for the service account (see the bigquery connector README)"
        )
    if e.status == 403 and any(phrase in text for phrase in _DRIVE_PHRASES):
        return (
            "the table is backed by Google Drive (a Sheet or a Drive file); reading it "
            "needs a Drive scope this connector never asks for, so it is not supported "
            "(see the bigquery connector README)"
        )
    if e.status == 403 and "bigquery.jobs.create" in text:
        return f"the service account lacks roles/bigquery.jobUser on project {project}"
    if e.status == 403 and e.reason not in _RATE_LIMIT_REASONS:
        return (
            "the service account lacks roles/bigquery.dataViewer on that dataset (or the "
            "table is outside what it was granted)"
        )
    return None


def _describe(e: BigQueryAPIError, project: str) -> str:
    """The failure, BigQuery's own text included, plus what to fix where the
    cause is known: what the toolkit answers the caller."""
    hint = _hint(e, project)
    return f"{e.message} — {hint}" if hint else e.message


def error_code(e: BigQueryAPIError) -> str:
    """The canonical ``error_code`` for a BigQuery failure. BigQuery answers
    rate limits and quotas with 403, so its ``reason`` decides before the
    status does; a custom quota is ``forbidden`` (a retry can't help until
    it resets)."""
    if e.timed_out or e.reason == "timeout":
        return ERROR_TIMEOUT
    if _custom_quota(e):
        return ERROR_FORBIDDEN
    if e.status == 429 or e.reason in _RATE_LIMIT_REASONS:
        return ERROR_RATE_LIMITED
    if e.reason in _UPSTREAM_REASONS:
        return ERROR_UPSTREAM_ERROR
    if e.reason in _ARGUMENT_REASONS:
        return ERROR_INVALID_ARGUMENTS
    if e.reason == "notFound":
        return ERROR_NOT_FOUND
    if e.status is None or 300 <= e.status < 400:
        return ERROR_UPSTREAM_ERROR
    return error_code_for_status(e.status, www_authenticate=e.www_authenticate)


def failure(e: BigQueryAPIError, project: str) -> ToolResult:
    """A BigQuery failure as a canonical ``ToolResult`` error."""
    return ToolResult.fail(
        _describe(e, project), code=error_code(e), retry_after=e.retry_after
    )


class BigQueryRequestError(ValueError):
    """BigQuery refused what the caller asked for: an invalid query, a table
    or dataset that does not exist, a result too large. A ``ValueError``, so
    pipe-as-service answers it 422. ``status`` and ``reason`` are
    BigQuery's."""

    def __init__(
        self, message: str, *, status: int | None = None, reason: str | None = None
    ) -> None:
        super().__init__(message)
        self.status = status
        self.reason = reason


#: Where BigQuery points into the statement ("at [3:15]").
_POSITION = re.compile(r"\bat \[(\d{1,6}):(\d{1,6})\]")


def source_error(e: BigQueryAPIError, project: str) -> Exception:
    """A BigQuery failure as the source raises it: what the caller can fix
    (``invalid_arguments``, ``not_found``) is a :class:`BigQueryRequestError`
    (422 in pipe-as-service); everything else (upstream, throttling, a
    timeout, a refused key or permission) is a
    :class:`~aisquare.pipe.errors.PipelineError` (502), with
    ``retry_after`` when BigQuery gave one. The message is ``summary`` and
    the hint, never BigQuery's own text (it can quote the SQL and name
    tables, and hosts log these), but for a statement BigQuery calls invalid
    it keeps where ("at [line:column]")."""
    code = error_code(e)
    hint = _hint(e, project)
    text = e.summary
    if code == ERROR_INVALID_ARGUMENTS:
        where = _POSITION.search(e.detail)
        if where is not None:
            text += f" at [{where.group(1)}:{where.group(2)}]"
    if hint:
        text += f" — {hint}"
    if code in (ERROR_INVALID_ARGUMENTS, ERROR_NOT_FOUND):
        return BigQueryRequestError(text, status=e.status, reason=e.reason)
    return PipelineError(text, retry_after=e.retry_after)


def _bounded_int(arguments: Mapping[str, Any], name: str, low: int, high: int) -> int:
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


def _string(arguments: Mapping[str, Any], name: str, limit: int) -> str | None:
    """A string argument up to ``limit`` characters (core checks types only),
    or None when absent or empty."""
    value = arguments.get(name)
    if value is None or value == "":
        return None
    if not isinstance(value, str) or len(value) > limit:
        raise ToolError(
            f"{name} must be a string of at most {limit} characters",
            code=ERROR_INVALID_ARGUMENTS,
        )
    return value


def _reference(parse: Any, value: Any, name: str) -> Any:
    try:
        return parse(value, name)
    except BadReference as e:
        raise ToolError(str(e), code=ERROR_INVALID_ARGUMENTS) from None


def bytes_cap(requested: Any, creds: BigQueryCredentials) -> int:
    """The bytes a query may process: the caller's ask (default
    :data:`DEFAULT_BYTES_PROCESSED`), never above the tenant's ceiling."""
    if requested is None:
        return min(DEFAULT_BYTES_PROCESSED, creds.max_bytes_billed)
    if (
        isinstance(requested, bool)
        or not isinstance(requested, int)
        or requested < MIN_BYTES_BILLED
    ):
        raise ValueError(
            f"max_bytes_processed must be an integer of at least {MIN_BYTES_BILLED} (10 MiB)"
        )
    if requested > creds.max_bytes_billed:
        raise ValueError(
            f"max_bytes_processed ({requested}) is over this connection's ceiling "
            f"({creds.max_bytes_billed} bytes)"
        )
    return requested


def table_record(table: dict[str, Any]) -> dict[str, Any]:
    """tables.get as the tools report it: ``last_modified_time`` (RFC 3339)
    and ``last_modified_ms`` (BigQuery's epoch milliseconds) are the version
    markers."""
    ref = (
        table.get("tableReference")
        if isinstance(table.get("tableReference"), dict)
        else {}
    )
    partitioning = table.get("timePartitioning")
    range_partitioning = table.get("rangePartitioning")
    clustering = table.get("clustering")
    record: dict[str, Any] = {
        "table": ".".join(
            str(ref.get(k) or "") for k in ("projectId", "datasetId", "tableId")
        )
        if ref
        else None,
        "type": table.get("type"),
        "columns": schema_fields(table.get("schema")),
        "num_rows": _int_or_none(table.get("numRows")),
        "num_bytes": _int_or_none(table.get("numBytes")),
        "last_modified_time": millis_rfc3339(table.get("lastModifiedTime")),
        "last_modified_ms": table.get("lastModifiedTime"),
        "creation_time": millis_rfc3339(table.get("creationTime")),
        "location": table.get("location"),
        "description": table.get("description"),
        "partitioning": (
            {
                "type": partitioning.get("type"),
                "field": partitioning.get("field") or "_PARTITIONTIME",
            }
            if isinstance(partitioning, dict)
            else {"type": "RANGE", "field": range_partitioning.get("field")}
            if isinstance(range_partitioning, dict)
            else None
        ),
        "clustering": clustering.get("fields")
        if isinstance(clustering, dict) and isinstance(clustering.get("fields"), list)
        else None,
    }
    if record["table"] is None:
        record.pop("table")
    return record


def _int_or_none(value: Any) -> int | None:
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return None


def _rows_payload(result: Rows) -> dict[str, Any]:
    return {
        "columns": result.fields,
        "rows": result.rows,
        "row_count": len(result.rows),
        "total_rows": result.total_rows,
        "truncated": result.truncated,
    }


class BigQueryToolkit(Toolkit):
    """BigQuery, read-only: ``BIGQUERY_RUN_SELECT`` (primary),
    ``BIGQUERY_READ_TABLE_ROWS``, ``BIGQUERY_GET_TABLE_SCHEMA``,
    ``BIGQUERY_LIST_TABLES`` and ``BIGQUERY_LIST_DATASETS``. ``session``
    (tests, or a host that pools connections) is the ``requests.Session``
    every call goes through, token minting included."""

    name = "bigquery"
    version = "0.1.0"
    display_name = "Google BigQuery"
    description = (
        "Google BigQuery data warehouse, read-only: run SELECT queries and read tables, "
        "list datasets and tables, and describe a table's columns and when it last changed."
    )
    auth_type = AuthType.CUSTOM
    oauth = None
    docs_url = "https://cloud.google.com/bigquery/docs/reference/rest"
    category = "data"
    keywords = (
        "bigquery",
        "google bigquery",
        "gcp",
        "data warehouse",
        "sql",
        "query",
        "tables",
        "dataset",
        "analytics",
        "googlebigquery",
    )
    examples = (
        "run a SQL query on our BigQuery warehouse",
        "what columns does the policies table in BigQuery have",
        "list the datasets in our bigquery project",
    )

    def __init__(self, *, session: Any = None) -> None:
        self._session = session

    def tools(self) -> list[ToolSpec]:
        return list(TOOL_SPECS)

    def validate_config(self, config: dict[str, Any]) -> bool:
        try:
            load_credentials(config)
        except BigQueryNotConfigured:
            return False
        return True

    def call_tool(
        self,
        slug: str,
        arguments: Mapping[str, Any] | None,
        config: Mapping[str, Any] | None,
    ) -> ToolResult:
        """Core's ``call_tool``, except that a known tool on an unusable
        credential fails with why BigQuery is not configured (no request is
        made)."""
        if isinstance(slug, str) and self.get_tool(slug) is not None:
            try:
                load_credentials(config)
            except BigQueryNotConfigured as e:
                return ToolResult.fail(
                    f"BigQuery is not configured: {e}", code=ERROR_NOT_CONNECTED
                )
            except Exception:  # noqa: BLE001 — call_tool never raises
                return ToolResult.fail(
                    "BigQuery is not configured", code=ERROR_NOT_CONNECTED
                )
        return super().call_tool(slug, arguments, config)

    def run_tool(
        self, tool: ToolSpec, arguments: dict[str, Any], config: dict[str, Any]
    ) -> ToolResult:
        creds = load_credentials(config)
        # Arguments, the SQL guard and the caps are all checked before the
        # client exists: a refused call sends nothing, not even for a token.
        handler = {
            LIST_DATASETS.slug: self._list_datasets,
            LIST_TABLES.slug: self._list_tables,
            GET_TABLE_SCHEMA.slug: self._get_table_schema,
            RUN_SELECT.slug: self._run_select,
            READ_TABLE_ROWS.slug: self._read_table_rows,
        }.get(tool.slug)
        if handler is None:
            raise ToolError(f"{tool.slug} is not implemented")
        call = handler(creds, arguments)
        with BigQueryClient(
            creds, session=self._session, budget=time_budget(config)
        ) as client:
            try:
                return call(client)
            except BigQueryNotConfigured as e:
                return ToolResult.fail(
                    f"BigQuery is not configured: {e}", code=ERROR_NOT_CONNECTED
                )
            except BigQueryAPIError as e:
                return failure(e, creds.project_id)
            except (NotReadOnly, CapExceeded, NotATable) as e:
                return ToolResult.fail(str(e), code=ERROR_INVALID_ARGUMENTS)

    # Each handler checks its arguments and returns the call to make.

    def _list_datasets(
        self, creds: BigQueryCredentials, arguments: dict[str, Any]
    ) -> Any:
        project = _string(arguments, "project", 100)
        if project is not None and not PROJECT_ID.match(project):
            raise ToolError(
                "project must be a Google Cloud project id",
                code=ERROR_INVALID_ARGUMENTS,
            )
        page_size = _bounded_int(arguments, "page_size", 1, MAX_PAGE_SIZE)
        token = _string(arguments, "page_token", MAX_PAGE_TOKEN_CHARS)

        def call(client: BigQueryClient) -> ToolResult:
            answer = client.list_datasets(
                project, page_size=page_size, page_token=token
            )
            items = answer.get("datasets")
            datasets = []
            for item in items if isinstance(items, list) else []:
                ref = item.get("datasetReference") if isinstance(item, dict) else None
                if not isinstance(ref, dict):
                    continue
                datasets.append(
                    {
                        "dataset_id": ref.get("datasetId"),
                        "project_id": ref.get("projectId"),
                        "location": item.get("location"),
                        "friendly_name": item.get("friendlyName"),
                    }
                )
            return ToolResult.ok(
                {
                    "project_id": project or creds.project_id,
                    "datasets": datasets,
                    **_next(answer),
                }
            )

        return call

    def _list_tables(
        self, creds: BigQueryCredentials, arguments: dict[str, Any]
    ) -> Any:
        project, dataset = _reference(
            parse_dataset, arguments.get("dataset"), "dataset"
        )
        page_size = _bounded_int(arguments, "page_size", 1, MAX_PAGE_SIZE)
        token = _string(arguments, "page_token", MAX_PAGE_TOKEN_CHARS)

        def call(client: BigQueryClient) -> ToolResult:
            answer = client.list_tables(
                project, dataset, page_size=page_size, page_token=token
            )
            items = answer.get("tables")
            tables = []
            for item in items if isinstance(items, list) else []:
                ref = item.get("tableReference") if isinstance(item, dict) else None
                if not isinstance(ref, dict):
                    continue
                tables.append(
                    {
                        "table_id": ref.get("tableId"),
                        "dataset_id": ref.get("datasetId"),
                        "project_id": ref.get("projectId"),
                        "type": item.get("type"),
                        "creation_time": millis_rfc3339(item.get("creationTime")),
                    }
                )
            return ToolResult.ok(
                {
                    "dataset": f"{project or creds.project_id}.{dataset}",
                    "tables": tables,
                    "total_items": _int_or_none(answer.get("totalItems")),
                    **_next(answer),
                }
            )

        return call

    def _get_table_schema(
        self, creds: BigQueryCredentials, arguments: dict[str, Any]
    ) -> Any:
        ref: TableRef = _reference(parse_table, arguments.get("table"), "table")

        def call(client: BigQueryClient) -> ToolResult:
            return ToolResult.ok(table_record(client.get_table(ref)))

        return call

    def _run_select(self, creds: BigQueryCredentials, arguments: dict[str, Any]) -> Any:
        raw = arguments.get("sql")
        if not isinstance(raw, str):
            raise ToolError("sql must be a string", code=ERROR_INVALID_ARGUMENTS)
        try:
            sql = check_select_only(raw)
        except NotReadOnly as e:
            raise ToolError(str(e), code=ERROR_INVALID_ARGUMENTS) from None
        dataset_text = _string(arguments, "default_dataset", MAX_REF_CHARS)
        default_dataset = (
            _reference(parse_dataset, dataset_text, "default_dataset")
            if dataset_text is not None
            else None
        )
        max_rows = _bounded_int(arguments, "max_rows", 1, MAX_ROWS)
        try:
            cap = bytes_cap(arguments.get("max_bytes_processed"), creds)
        except ValueError as e:
            raise ToolError(str(e), code=ERROR_INVALID_ARGUMENTS) from None
        estimate_only = arguments.get("dry_run") is True

        def call(client: BigQueryClient) -> ToolResult:
            if estimate_only:
                estimate = dry_run(
                    client,
                    sql,
                    max_bytes_processed=cap,
                    default_dataset=default_dataset,
                )
                return ToolResult.ok(
                    {
                        "dry_run": True,
                        "total_bytes_processed": estimate.total_bytes_processed,
                        "max_bytes_processed": cap,
                    }
                )
            result = run_select(
                client,
                sql,
                max_rows=max_rows,
                max_result_bytes=MAX_RESULT_BYTES,
                max_bytes_processed=cap,
                # With a host budget the job may use what is left of it.
                job_timeout=client.budget or JOB_TIMEOUT_SECONDS,
                default_dataset=default_dataset,
            )
            return ToolResult.ok(
                {
                    **_rows_payload(result),
                    "total_bytes_processed": result.total_bytes_processed,
                    "max_bytes_processed": cap,
                    "job_id": result.job_id,
                    "location": result.location,
                    "cache_hit": result.cache_hit,
                }
            )

        return call

    def _read_table_rows(
        self, creds: BigQueryCredentials, arguments: dict[str, Any]
    ) -> Any:
        ref: TableRef = _reference(parse_table, arguments.get("table"), "table")
        max_rows = _bounded_int(arguments, "max_rows", 1, MAX_ROWS)
        start = arguments.get("start_index", 0)
        if isinstance(start, bool) or not isinstance(start, int) or start < 0:
            raise ToolError(
                "start_index must be an integer of at least 0",
                code=ERROR_INVALID_ARGUMENTS,
            )
        selected = arguments.get("selected_fields")
        if selected is not None and (
            not isinstance(selected, list)
            or len(selected) > MAX_SELECTED_FIELDS
            or not all(
                isinstance(s, str) and 0 < len(s) <= MAX_FIELD_CHARS for s in selected
            )
        ):
            raise ToolError(
                f"selected_fields must be at most {MAX_SELECTED_FIELDS} column names",
                code=ERROR_INVALID_ARGUMENTS,
            )

        def call(client: BigQueryClient) -> ToolResult:
            table = table_info(client, ref)
            try:
                result = read_table_rows(
                    client,
                    ref,
                    table,
                    max_rows=max_rows,
                    max_result_bytes=MAX_RESULT_BYTES,
                    start_index=start,
                    selected_fields=list(selected) if selected else None,
                )
            except ValueError as e:
                raise ToolError(str(e), code=ERROR_INVALID_ARGUMENTS) from None
            return ToolResult.ok(
                {
                    "table": ref.qualified(creds.project_id),
                    **_rows_payload(result),
                    "start_index": start,
                    "next_start_index": result.next_start_index,
                    "last_modified_time": millis_rfc3339(table.get("lastModifiedTime")),
                    "last_modified_ms": table.get("lastModifiedTime"),
                }
            )

        return call


def _next(answer: dict[str, Any]) -> dict[str, Any]:
    token = answer.get("nextPageToken")
    token = token if isinstance(token, str) and token else None
    return {"has_more": token is not None, "next_page_token": token}


__all__ = [
    "GET_TABLE_SCHEMA",
    "LIST_DATASETS",
    "LIST_TABLES",
    "READ_TABLE_ROWS",
    "RUN_SELECT",
    "TOOL_SPECS",
    "BigQueryToolkit",
    "bytes_cap",
    "failure",
    "table_record",
]
