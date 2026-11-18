"""Running one checked SELECT, and reading a table's rows, inside the caps.

A query (:func:`run_select`) goes:

1. :func:`~aisquare_pipe_bigquery.sqlguard.check_select_only` (the caller
   runs it first, before any request);
2. a dry run (jobs.query ``dryRun``; free): BigQuery's ``statementType``,
   when it gives one, must be ``SELECT``, and ``totalBytesProcessed`` must
   be within ``max_bytes_processed``, else :class:`CapExceeded` and nothing
   runs;
3. the query (jobs.query) with ``maximumBytesBilled`` = that cap (BigQuery
   refuses to bill more, whatever the estimate said) and ``jobTimeoutMs`` =
   the job deadline (BigQuery stops a job that outlives it);
4. polls (jobs.getQueryResults) until the job completes or the deadline
   passes (``timed_out``), then pages until ``max_rows`` rows or
   ``max_result_bytes`` bytes of JSON rows: a result that stops there says
   ``truncated`` (``max_rows`` or ``max_result_bytes``).

:func:`read_table_rows` reads one table's rows (tabledata.list, free: no
job) within the same row and byte caps.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from aisquare_pipe_bigquery.client import (
    POLL_PAUSE_SECONDS,
    BigQueryAPIError,
    BigQueryClient,
    error_detail,
)
from aisquare_pipe_bigquery.refs import TableRef
from aisquare_pipe_bigquery.rows import canonical, iter_rows, schema_fields
from aisquare_pipe_bigquery.sqlguard import NotReadOnly

TRUNCATED_MAX_ROWS = "max_rows"
TRUNCATED_MAX_RESULT_BYTES = "max_result_bytes"
#: Rows asked for per page at most (BigQuery also caps a page at 10 MB).
PAGE_ROWS = 10_000
#: Cells asked for per page at most. A page is parsed whole, and each cell
#: arrives as a ``{"v": "<string>"}`` object (about 250 bytes of Python
#: objects), so a page is held to about 12 MiB of them however wide the
#: rows are: 1,000 rows of 50 columns, 10,000 of 5.
PAGE_CELLS = 50_000
#: Rows per page while the schema is unknown (a dry run that gave none).
UNKNOWN_SCHEMA_PAGE_ROWS = 1_000
#: Pages followed at most for one result, whatever the page token says.
MAX_PAGES = 1000
#: Table types tabledata.list reads. Views, materialized views and external
#: tables are read with a query.
READABLE_TABLE_TYPES = frozenset({"TABLE", "SNAPSHOT"})


class CapExceeded(ValueError):
    """The dry run says the query would process more than its cap. Nothing
    ran and nothing was billed."""

    def __init__(self, would_process: int, cap: int) -> None:
        self.would_process = would_process
        self.cap = cap
        super().__init__(
            f"the query would process {would_process} bytes, over max_bytes_processed "
            f"({cap}); select fewer columns, filter on the partition column, or add a "
            "smaller date range"
        )


class NotATable(ValueError):
    """tabledata.list can't read this table type (a view, ...)."""


def _int(value: Any) -> int | None:
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return None


def cells_per_row(fields: list[dict[str, Any]]) -> int:
    """The cells one REST row of ``fields`` carries: one per column, plus
    a record's own sub-fields (a ``REPEATED`` column counts once; it may
    carry more)."""
    count = 0
    for f in fields:
        sub = f.get("fields")
        count += 1 + (cells_per_row(sub) if isinstance(sub, list) and sub else 0)
    return max(1, count)


def page_rows(fields: list[dict[str, Any]] | None, wanted: int) -> int:
    """Rows to ask for in one page: at most ``wanted``, :data:`PAGE_ROWS`
    and :data:`PAGE_CELLS` cells (:data:`UNKNOWN_SCHEMA_PAGE_ROWS` without a
    schema)."""
    if fields:
        by_cells = PAGE_CELLS // cells_per_row(fields)
    else:
        by_cells = UNKNOWN_SCHEMA_PAGE_ROWS
    return max(1, min(PAGE_ROWS, by_cells, wanted))


@dataclass
class Estimate:
    total_bytes_processed: int | None
    statement_type: str | None
    #: The result's columns, when the dry run gave them.
    fields: list[dict[str, Any]] = field(default_factory=list)


def dry_run(
    client: BigQueryClient,
    sql: str,
    *,
    max_bytes_processed: int,
    default_dataset: tuple[str | None, str] | None = None,
) -> Estimate:
    """The dry run (step 2): raises :class:`NotReadOnly` when BigQuery says
    the statement is not a SELECT, :class:`CapExceeded` when it would
    process more than ``max_bytes_processed``."""
    answer = client.dry_run(sql, default_dataset=default_dataset)
    statement = answer.get("statementType")
    statement = statement.upper() if isinstance(statement, str) else None
    if statement is not None and statement != "SELECT":
        raise NotReadOnly(
            f"only one SELECT (or WITH ... SELECT) may run: BigQuery reads it as a "
            f"{statement} statement"
        )
    processed = _int(answer.get("totalBytesProcessed"))
    if processed is not None and processed > max_bytes_processed:
        raise CapExceeded(processed, max_bytes_processed)
    return Estimate(
        total_bytes_processed=processed,
        statement_type=statement,
        fields=schema_fields(answer.get("schema")),
    )


@dataclass
class Rows:
    """A result read within the caps: ``rows`` as objects, or, read with
    ``encoded=True``, ``encoded`` (each row's canonical JSON, as the
    source's envelopes carry them: about a fifth of the memory objects
    take). ``count`` is the rows read, either way."""

    fields: list[dict[str, Any]]
    rows: list[dict[str, Any]] = field(default_factory=list)
    total_rows: int | None = None
    truncated: str | None = None
    next_start_index: int | None = None
    result_bytes: int = 0
    total_bytes_processed: int | None = None
    job_id: str | None = None
    location: str | None = None
    cache_hit: bool | None = None
    encoded: list[bytes] | None = None

    @property
    def count(self) -> int:
        return len(self.rows) if self.encoded is None else len(self.encoded)


def _take(
    result: Rows,
    page: Iterable[dict[str, Any]],
    *,
    max_rows: int,
    max_result_bytes: int,
) -> bool:
    """Add ``page``'s rows until a cap; True when a cap cut the page."""
    for row in page:
        if result.count >= max_rows:
            result.truncated = TRUNCATED_MAX_ROWS
            return True
        encoded = canonical(row)
        size = len(encoded)
        if result.result_bytes + size > max_result_bytes:
            result.truncated = TRUNCATED_MAX_RESULT_BYTES
            return True
        if result.encoded is None:
            result.rows.append(row)
        else:
            result.encoded.append(encoded)
        result.result_bytes += size
    return False


#: The HTTP status a job-level error stands for, by its reason (the answer
#: itself was a 200): what BigQuery answers the same failure with when it
#: fails the call. Any other reason is a 400.
_JOB_REASON_STATUS = {
    "accessDenied": 403,
    "notFound": 404,
    "backendError": 500,
    "internalError": 500,
    "jobBackendError": 500,
    "jobInternalError": 500,
}


def _job_failed(answer: dict[str, Any]) -> BigQueryAPIError | None:
    """A complete answer that carries the job's errors and no schema: the
    job failed (BigQuery usually says so with an HTTP error instead)."""
    errors = answer.get("errors")
    if answer.get("schema") or not isinstance(errors, list) or not errors:
        return None
    message, reason = error_detail(errors)
    return BigQueryAPIError(
        "the BigQuery job failed" + (f" ({reason})" if reason else ""),
        detail=message,
        status=_JOB_REASON_STATUS.get(reason or "", 400),
        reason=reason,
    )


def run_select(
    client: BigQueryClient,
    sql: str,
    *,
    max_rows: int,
    max_result_bytes: int,
    max_bytes_processed: int,
    job_timeout: float,
    default_dataset: tuple[str | None, str] | None = None,
    estimate: Estimate | None = None,
    encoded: bool = False,
) -> Rows:
    """Steps 2-4 for a CHECKED statement. Pass ``estimate`` when the dry run
    already ran (the source runs it before reading markers), and
    ``encoded`` for the rows as canonical JSON (:class:`Rows`)."""
    if estimate is None:
        estimate = dry_run(
            client,
            sql,
            max_bytes_processed=max_bytes_processed,
            default_dataset=default_dataset,
        )
    job_seconds = client.job_seconds(job_timeout)
    job_deadline = client.now() + job_seconds
    page_size = page_rows(estimate.fields, max_rows)
    answer = client.start_query(
        sql,
        page_size=page_size,
        max_bytes_billed=max_bytes_processed,
        job_timeout_ms=int(job_seconds * 1000),
        wait_ms=client.wait_ms(job_seconds),
        request_id=str(uuid.uuid4()),
        default_dataset=default_dataset,
    )
    job = answer.get("jobReference")
    job = job if isinstance(job, dict) else {}
    for _ in range(MAX_PAGES):
        if answer.get("jobComplete") is True:
            break
        left = job_deadline - client.now()
        if left <= 0:
            raise BigQueryAPIError(
                f"the query did not finish within {job_seconds:g}s (BigQuery stops the "
                "job at that deadline)",
                timed_out=True,
            )
        asked = client.now()
        answer = client.query_results(
            job, page_size=page_size, wait_ms=client.wait_ms(left)
        )
        if (
            answer.get("jobComplete") is not True
            and client.now() - asked < POLL_PAUSE_SECONDS
        ):
            client.pause(min(POLL_PAUSE_SECONDS, max(0.0, job_deadline - client.now())))
    else:
        raise BigQueryAPIError(
            "the query did not finish (too many polls)", timed_out=True
        )
    failed = _job_failed(answer)
    if failed is not None:
        raise failed

    result = Rows(
        fields=schema_fields(answer.get("schema")),
        total_rows=_int(answer.get("totalRows")),
        total_bytes_processed=_int(answer.get("totalBytesProcessed"))
        if answer.get("totalBytesProcessed") is not None
        else estimate.total_bytes_processed,
        job_id=job.get("jobId") if isinstance(job.get("jobId"), str) else None,
        location=job.get("location") if isinstance(job.get("location"), str) else None,
        cache_hit=answer.get("cacheHit")
        if isinstance(answer.get("cacheHit"), bool)
        else None,
        encoded=[] if encoded else None,
    )
    for _ in range(MAX_PAGES):
        # Converted one row at a time; the parsed page goes (with ``page``)
        # before the next one is read.
        page = iter_rows(result.fields, answer.get("rows"))
        token = answer.get("pageToken")
        answer = {}
        if _take(result, page, max_rows=max_rows, max_result_bytes=max_result_bytes):
            return result
        if not isinstance(token, str) or not token:
            return result
        if result.count >= max_rows:
            result.truncated = TRUNCATED_MAX_ROWS
            return result
        del page
        answer = client.query_results(
            job,
            page_size=page_rows(result.fields, max_rows - result.count),
            page_token=token,
        )
    result.truncated = result.truncated or TRUNCATED_MAX_ROWS
    return result


def table_info(client: BigQueryClient, ref: TableRef) -> dict[str, Any]:
    """tables.get, refusing a table type tabledata.list can't read."""
    table = client.get_table(ref)
    kind = str(table.get("type") or "TABLE").upper()
    if kind not in READABLE_TABLE_TYPES:
        raise NotATable(
            f"{ref.qualified(client.project_id)} is a {kind}; read it with a SELECT "
            "query instead"
        )
    return table


def read_table_rows(
    client: BigQueryClient,
    ref: TableRef,
    table: dict[str, Any],
    *,
    max_rows: int,
    max_result_bytes: int,
    start_index: int = 0,
    selected_fields: list[str] | None = None,
    encoded: bool = False,
) -> Rows:
    """Up to ``max_rows`` rows of ``ref`` (described by ``table``, its
    tables.get) from row ``start_index`` on, within ``max_result_bytes``.
    When rows remain, ``truncated`` says which cap stopped the read and
    ``next_start_index`` is the row to continue from. ``selected_fields``
    (top-level column names) are sent in the table's column order, the order
    the rows then come in. ``encoded``: the rows as canonical JSON
    (:class:`Rows`)."""
    fields = schema_fields(table.get("schema"))
    if selected_fields:
        wanted = {name.lower() for name in selected_fields}
        known = {f["name"].lower() for f in fields}
        missing = sorted(wanted - known)
        if missing:
            raise ValueError(f"selected_fields names no column {missing[0]!r}")
        fields = [f for f in fields if f["name"].lower() in wanted]
    result = Rows(
        fields=fields,
        total_rows=_int(table.get("numRows")),
        encoded=[] if encoded else None,
    )
    token: str | None = None
    for page_number in range(MAX_PAGES):
        answer = client.list_rows(
            ref,
            page_size=page_rows(fields, max_rows - result.count),
            page_token=token,
            start_index=start_index if page_number == 0 else None,
            selected_fields=[f["name"] for f in fields] if selected_fields else None,
        )
        total = _int(answer.get("totalRows"))
        if total is not None:
            result.total_rows = total
        # Converted one row at a time; the parsed page goes (with ``page``)
        # before the next one is read.
        page = iter_rows(fields, answer.get("rows"))
        next_token = answer.get("pageToken")
        token = next_token if isinstance(next_token, str) and next_token else None
        del answer
        if _take(result, page, max_rows=max_rows, max_result_bytes=max_result_bytes):
            break
        if token is None:
            return result
        if result.count >= max_rows:
            result.truncated = TRUNCATED_MAX_ROWS
            break
        del page
    else:
        result.truncated = TRUNCATED_MAX_ROWS
    result.next_start_index = start_index + result.count
    return result


__all__ = [
    "PAGE_CELLS",
    "PAGE_ROWS",
    "READABLE_TABLE_TYPES",
    "TRUNCATED_MAX_RESULT_BYTES",
    "TRUNCATED_MAX_ROWS",
    "CapExceeded",
    "Estimate",
    "NotATable",
    "Rows",
    "cells_per_row",
    "dry_run",
    "page_rows",
    "read_table_rows",
    "run_select",
    "table_info",
]
