"""BigQueryQuerySource: a table's rows, or one SELECT's, as envelopes of row batches.

``config`` is the tenant's credential blob (:mod:`.credentials`). Pull params
pick the rows, never the key, the job project, the location or the cost
ceiling. Exactly one of:

* ``table``: ``dataset.table`` or ``project.dataset.table``, read with
  tabledata.list (free: no query job). Optional ``selected_fields`` (column
  names) and ``start_index`` (the first row; pass a truncated pull's
  ``next_start_index`` to continue it). Views can't be read this way: use
  ``sql``.
* ``sql``: ONE read-only GoogleSQL statement (SELECT or WITH ... SELECT),
  checked before any request (:mod:`.sqlguard`), dry-run against
  ``max_bytes_processed`` (default 1 GiB, never above the tenant's
  ceiling), then run with that cap as ``maximumBytesBilled`` and
  ``job_timeout_seconds`` (default 120, at most 600) as ``jobTimeoutMs``.
  Optional ``default_dataset``.

And for both: ``batch_size`` rows per envelope (default 500, at most
10,000), ``max_rows`` (default 10,000, at most 100,000) and
``max_result_bytes`` (JSON bytes of rows, default 25 MiB, at most 100 MiB).

Each envelope is one batch: ``data`` is JSON ``{"columns": [...], "rows":
[{column: value}]}`` (``application/json``; ``schema`` carries the columns
too), and ``metadata`` carries the version markers:

* ``result_sha256``: the whole result's hash (schema plus rows, whatever
  their order), the same on every batch of one pull. It changes when the
  data the pull read changes.
* ``table_last_modified`` (RFC 3339) and ``table_last_modified_ms``
  (BigQuery's ``lastModifiedTime``): the table's last change, read BEFORE
  the rows, so a change made while they were read shows at the next pull.
  For ``sql``, the latest over the tables the statement names, set only when
  every one could be read as a table (not for views, wildcards,
  INFORMATION_SCHEMA...); ``tables_last_modified`` lists the ones read.
* ``version``: ``<table_last_modified_ms>:<result_sha256>``, or
  ``result_sha256`` alone without the time. Keep it and re-read when it
  changes; comparing ``table_last_modified`` against a fresh tables.get
  (``BIGQUERY_GET_TABLE_SCHEMA``) is the cheap check.

plus ``batch_index``, ``batch_count``, ``first_row``, ``row_count``,
``batch_sha256``, ``result_row_count``, ``total_rows``, and ``table`` or
``query_sha256`` (the statement's hash, never its text); for ``sql`` also
``job_id`` and ``total_bytes_processed``. A result with no rows is one
envelope with no rows, so its markers still come out.

A result cut short by ``max_rows`` or ``max_result_bytes`` says so on every
envelope: ``truncated`` (``max_rows`` or ``max_result_bytes``), and for a
table ``next_start_index``. Its ``result_sha256`` covers the rows read only.

A param naming a credential key is refused, and so is SQL that is not one
SELECT. Logs carry no SQL, no table names and no row values.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Iterator
from typing import Any

from aisquare.pipe.core.connector import AuthType, SourceConnector
from aisquare.pipe.core.envelope import DataEnvelope, MetaField, PullParams

from aisquare_pipe_bigquery.client import BigQueryAPIError, BigQueryClient
from aisquare_pipe_bigquery.credentials import (
    SERVER_KEYS,
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
from aisquare_pipe_bigquery.refs import TableRef, parse_dataset, parse_table
from aisquare_pipe_bigquery.rows import (
    canonical,
    millis_rfc3339,
    result_sha256,
    rows_sha256,
)
from aisquare_pipe_bigquery.sqlguard import (
    NotReadOnly,
    check_select_only,
    table_references,
)
from aisquare_pipe_bigquery.toolkit import bytes_cap

logger = logging.getLogger("aisquare.pipe.bigquery")

DEFAULT_BATCH_SIZE = 500
MAX_BATCH_SIZE = 10_000
DEFAULT_MAX_ROWS = 10_000
MAX_ROWS = 100_000
DEFAULT_MAX_RESULT_BYTES = 25 * 1024 * 1024
MAX_RESULT_BYTES = 100 * 1024 * 1024
DEFAULT_JOB_TIMEOUT = 120
MAX_JOB_TIMEOUT = 600
#: Tables whose lastModifiedTime a query's marker reads at most.
MAX_MARKER_TABLES = 20
MAX_SELECTED_FIELDS = 200
#: Types whose lastModifiedTime moves with their data.
_DATA_TABLE_TYPES = frozenset({"TABLE", "SNAPSHOT"})
_PARAMS = frozenset(
    {
        "table",
        "sql",
        "default_dataset",
        "selected_fields",
        "start_index",
        "batch_size",
        "max_rows",
        "max_result_bytes",
        "max_bytes_processed",
        "job_timeout_seconds",
    }
)
_TABLE_ONLY = ("selected_fields", "start_index")
_SQL_ONLY = ("default_dataset", "max_bytes_processed", "job_timeout_seconds")


def _options(params: PullParams | dict[str, Any] | None) -> dict[str, Any]:
    if params is None:
        return {}
    raw = params.params if isinstance(params, PullParams) else params
    if not isinstance(raw, dict):
        raise TypeError("pull params must be a mapping")
    server = sorted(SERVER_KEYS.intersection(raw))
    if server:
        raise ValueError(
            f"pull param {server[0]!r} is server-controlled and cannot be set by a caller"
        )
    unknown = sorted(set(raw) - _PARAMS)
    if unknown:
        raise ValueError(f"unknown pull param {unknown[0]!r}")
    return raw


def _bounded(
    options: dict[str, Any], name: str, default: int, low: int, high: int
) -> int:
    value = options.get(name, default)
    if value is None:
        value = default
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not low <= value <= high
    ):
        raise ValueError(f"{name} must be an integer from {low} to {high}")
    return value


class BigQueryQuerySource(SourceConnector):
    """Pulls a table's rows, or one SELECT's, in batches with version markers."""

    name = "bigquery-query-source"
    version = "0.1.0"
    # Framework class attributes, declared plain on SourceConnector.
    output_types = ["application/json"]  # noqa: RUF012
    auth_type = AuthType.CUSTOM
    description = (
        "Pulls the rows of a BigQuery table, or of one read-only SELECT, as JSON batches "
        "with version markers (the result's hash and the table's last-modified time)."
    )
    docs_url = "https://cloud.google.com/bigquery/docs/reference/rest/v2/jobs/query"

    CONFIG_SPEC: dict[str, MetaField] = {  # noqa: RUF012
        "service_account_json": MetaField(
            type=dict,
            required=True,
            description="The service account's JSON key (server-side only)",
        ),
        "project_id": MetaField(
            type=str,
            required=True,
            description="The project query jobs run in and bill to (server-side only)",
        ),
        "location": MetaField(
            type=str,
            required=False,
            description="Where query jobs run, e.g. US or EU (server-side only)",
        ),
        "max_bytes_billed": MetaField(
            type=int,
            required=False,
            description="Ceiling on the bytes one query may process (server-side only)",
        ),
    }

    metadata_spec = {  # noqa: RUF012
        "kind": MetaField(type=str, required=True, description="table or query"),
        "table": MetaField(
            type=str, required=False, description="project.dataset.table (table pulls)"
        ),
        "query_sha256": MetaField(
            type=str,
            required=False,
            description="SHA-256 of the statement run (query pulls; never its text)",
        ),
        "batch_index": MetaField(type=int, required=True, description="0-based batch"),
        "batch_count": MetaField(type=int, required=True, description="Batches in all"),
        "first_row": MetaField(
            type=int, required=True, description="The batch's first row (0-based)"
        ),
        "row_count": MetaField(
            type=int, required=True, description="Rows in the batch"
        ),
        "batch_sha256": MetaField(
            type=str, required=True, description="SHA-256 of the batch's rows"
        ),
        "result_sha256": MetaField(
            type=str,
            required=True,
            description="SHA-256 of the whole result (schema and rows, any order): a "
            "version marker",
        ),
        "result_row_count": MetaField(
            type=int, required=True, description="Rows in the whole result"
        ),
        "table_last_modified": MetaField(
            type=str,
            required=False,
            description="RFC 3339 last change of the table(s) read: a version marker",
        ),
        "table_last_modified_ms": MetaField(
            type=str,
            required=False,
            description="BigQuery's lastModifiedTime (epoch ms) of the table(s) read",
        ),
        "tables_last_modified": MetaField(
            type=dict,
            required=False,
            description="Query pulls: each table read's RFC 3339 last change",
        ),
        "version": MetaField(
            type=str,
            required=True,
            description="<table_last_modified_ms>:<result_sha256>, or result_sha256",
        ),
        "total_rows": MetaField(
            type=int, required=False, description="Rows in the table or query result"
        ),
        "job_id": MetaField(type=str, required=False, description="The query job's id"),
        "total_bytes_processed": MetaField(
            type=int, required=False, description="Bytes the query processed"
        ),
        "truncated": MetaField(
            type=str,
            required=False,
            description="When a cap cut the result: max_rows or max_result_bytes",
        ),
        "next_start_index": MetaField(
            type=int,
            required=False,
            description="With truncated, table pulls: the start_index that continues",
        ),
    }

    def __init__(self, *, session: Any = None) -> None:
        self._session = session

    def validate_config(self, config: dict) -> bool:
        try:
            load_credentials(config)
        except BigQueryNotConfigured:
            return False
        return True

    def pull(
        self, config: dict, params: PullParams | None = None
    ) -> Iterator[DataEnvelope]:
        """Yield one envelope per batch of rows. Raises
        :class:`BigQueryNotConfigured` without a usable credential (before any
        request), ``ValueError`` for bad params, SQL that is not one SELECT
        (:class:`~aisquare_pipe_bigquery.sqlguard.NotReadOnly`) or a query
        over its byte cap (:class:`~aisquare_pipe_bigquery.query.CapExceeded`),
        and :class:`BigQueryAPIError` when BigQuery fails."""
        creds = load_credentials(config)
        options = _options(params)
        has_table = options.get("table") not in (None, "")
        has_sql = options.get("sql") not in (None, "")
        if has_table == has_sql:
            raise ValueError("give exactly one of table or sql")
        wrong = [k for k in (_SQL_ONLY if has_table else _TABLE_ONLY) if k in options]
        if wrong:
            raise ValueError(
                f"{wrong[0]} goes with {'sql' if has_table else 'table'}, not "
                f"{'table' if has_table else 'sql'}"
            )
        batch_size = _bounded(
            options, "batch_size", DEFAULT_BATCH_SIZE, 1, MAX_BATCH_SIZE
        )
        max_rows = _bounded(options, "max_rows", DEFAULT_MAX_ROWS, 1, MAX_ROWS)
        max_bytes = _bounded(
            options, "max_result_bytes", DEFAULT_MAX_RESULT_BYTES, 1, MAX_RESULT_BYTES
        )
        if has_table:
            ref = parse_table(options["table"])
            start = _bounded(options, "start_index", 0, 0, 2**63 - 1)
            selected = options.get("selected_fields")
            if selected is not None and (
                not isinstance(selected, list)
                or not selected
                or len(selected) > MAX_SELECTED_FIELDS
                or not all(isinstance(s, str) and s for s in selected)
            ):
                raise ValueError(
                    f"selected_fields must be 1-{MAX_SELECTED_FIELDS} column names"
                )
            with BigQueryClient(creds, session=self._session) as client:
                yield from self._table(
                    client, creds, ref, start, selected, batch_size, max_rows, max_bytes
                )
            return

        sql = check_select_only(options["sql"])
        dataset = options.get("default_dataset")
        default_dataset = None if dataset in (None, "") else parse_dataset(dataset)
        cap = bytes_cap(options.get("max_bytes_processed"), creds)
        job_timeout = _bounded(
            options, "job_timeout_seconds", DEFAULT_JOB_TIMEOUT, 1, MAX_JOB_TIMEOUT
        )
        with BigQueryClient(creds, session=self._session) as client:
            yield from self._query(
                client,
                creds,
                sql,
                default_dataset,
                cap,
                job_timeout,
                batch_size,
                max_rows,
                max_bytes,
            )

    # -- the two kinds of pull ------------------------------------------------

    def _table(
        self,
        client: BigQueryClient,
        creds: BigQueryCredentials,
        ref: TableRef,
        start: int,
        selected: list[str] | None,
        batch_size: int,
        max_rows: int,
        max_bytes: int,
    ) -> Iterator[DataEnvelope]:
        table = table_info(client, ref)  # the marker, read before the rows
        result = read_table_rows(
            client,
            ref,
            table,
            max_rows=max_rows,
            max_result_bytes=max_bytes,
            start_index=start,
            selected_fields=selected,
        )
        name = ref.qualified(creds.project_id)
        common: dict[str, Any] = {"kind": "table", "table": name}
        common.update(_time_marker(table.get("lastModifiedTime")))
        if result.truncated is not None and result.next_start_index is not None:
            common["next_start_index"] = result.next_start_index
        yield from _batches(
            result, common, batch_size, start, f"bigquery:{name}", "table"
        )

    def _query(
        self,
        client: BigQueryClient,
        creds: BigQueryCredentials,
        sql: str,
        default_dataset: tuple[str | None, str] | None,
        cap: int,
        job_timeout: int,
        batch_size: int,
        max_rows: int,
        max_bytes: int,
    ) -> Iterator[DataEnvelope]:
        estimate = dry_run(
            client, sql, max_bytes_processed=cap, default_dataset=default_dataset
        )
        markers = self._markers(client, creds, sql, default_dataset)
        result = run_select(
            client,
            sql,
            max_rows=max_rows,
            max_result_bytes=max_bytes,
            max_bytes_processed=cap,
            job_timeout=job_timeout,
            default_dataset=default_dataset,
            estimate=estimate,
        )
        digest = hashlib.sha256(sql.encode()).hexdigest()
        common: dict[str, Any] = {"kind": "query", "query_sha256": digest, **markers}
        if result.job_id:
            common["job_id"] = result.job_id
        if result.total_bytes_processed is not None:
            common["total_bytes_processed"] = result.total_bytes_processed
        yield from _batches(
            result, common, batch_size, 0, f"bigquery:query:{digest[:16]}", "query"
        )

    @staticmethod
    def _markers(
        client: BigQueryClient,
        creds: BigQueryCredentials,
        sql: str,
        default_dataset: tuple[str | None, str] | None,
    ) -> dict[str, Any]:
        """The query's tables' lastModifiedTime, read before it runs: the
        latest as ``table_last_modified`` only when every table it names
        was read as a table."""
        refs, complete = table_references(sql, default_dataset=default_dataset)
        if len(refs) > MAX_MARKER_TABLES:
            refs, complete = refs[:MAX_MARKER_TABLES], False
        times: dict[str, str] = {}
        latest: int | None = None
        for ref in refs:
            try:
                table = client.get_table(ref)
            except BigQueryAPIError as e:
                if e.timed_out or e.status in (None, 429) or (e.status or 0) >= 500:
                    raise
                complete = False  # a model, a routine, a view's hidden base...
                continue
            millis = _millis(table.get("lastModifiedTime"))
            kind = str(table.get("type") or "TABLE").upper()
            if millis is None or kind not in _DATA_TABLE_TYPES:
                complete = False
                continue
            times[ref.qualified(creds.project_id)] = millis_rfc3339(millis) or ""
            latest = millis if latest is None else max(latest, millis)
        markers: dict[str, Any] = {"tables_last_modified": times}
        if complete and latest is not None:
            markers.update(_time_marker(latest))
        return markers


def _millis(value: Any) -> int | None:
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return None


def _time_marker(value: Any) -> dict[str, Any]:
    millis = _millis(value)
    if millis is None:
        return {}
    return {
        "table_last_modified_ms": str(millis),
        "table_last_modified": millis_rfc3339(millis),
    }


def _batches(
    result: Rows,
    common: dict[str, Any],
    batch_size: int,
    first: int,
    source_id: str,
    kind: str,
) -> Iterator[DataEnvelope]:
    """The result as envelopes of ``batch_size`` rows (one when empty)."""
    rows = result.rows
    whole = result_sha256(result.fields, rows)
    millis = common.get("table_last_modified_ms")
    common = {
        **common,
        "result_sha256": whole,
        "result_row_count": len(rows),
        "version": f"{millis}:{whole}" if millis else whole,
    }
    if result.total_rows is not None:
        common["total_rows"] = result.total_rows
    if result.truncated is not None:
        common["truncated"] = result.truncated
        logger.warning(
            "bigquery-query-source: a %s pull stopped at %s; the envelopes say where "
            "to continue (table pulls) or to narrow the query",
            kind,
            result.truncated,
        )
    starts = list(range(0, len(rows), batch_size)) or [0]
    columns = {"fields": result.fields}
    for index, offset in enumerate(starts):
        batch = rows[offset : offset + batch_size]
        yield DataEnvelope(
            content_type="application/json",
            data=canonical({"columns": result.fields, "rows": batch}),
            source_id=f"{source_id}#rows={first + offset}-{first + offset + len(batch)}",
            metadata={
                **common,
                "batch_index": index,
                "batch_count": len(starts),
                "first_row": first + offset,
                "row_count": len(batch),
                "batch_sha256": rows_sha256(batch),
            },
            schema=json.loads(json.dumps(columns)),
        )


__all__ = [
    "BigQueryAPIError",
    "BigQueryNotConfigured",
    "BigQueryQuerySource",
    "CapExceeded",
    "NotATable",
    "NotReadOnly",
]
