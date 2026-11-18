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
from dataclasses import dataclass, field
from typing import Any

from aisquare_pipe_bigquery.client import (
    POLL_PAUSE_SECONDS,
    BigQueryAPIError,
    BigQueryClient,
    error_detail,
)
from aisquare_pipe_bigquery.refs import TableRef
from aisquare_pipe_bigquery.rows import convert_rows, row_size, schema_fields
from aisquare_pipe_bigquery.sqlguard import NotReadOnly

TRUNCATED_MAX_ROWS = "max_rows"
TRUNCATED_MAX_RESULT_BYTES = "max_result_bytes"
#: Rows asked for per page (BigQuery also caps a page at 10 MB).
PAGE_ROWS = 10_000
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


@dataclass
class Estimate:
    total_bytes_processed: int | None
    statement_type: str | None


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
    return Estimate(total_bytes_processed=processed, statement_type=statement)


@dataclass
class Rows:
    """A result read within the caps."""

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


def _take(
    result: Rows, page: list[dict[str, Any]], *, max_rows: int, max_result_bytes: int
) -> bool:
    """Add ``page``'s rows until a cap; True when a cap cut the page."""
    for row in page:
        if len(result.rows) >= max_rows:
            result.truncated = TRUNCATED_MAX_ROWS
            return True
        size = row_size(row)
        if result.result_bytes + size > max_result_bytes:
            result.truncated = TRUNCATED_MAX_RESULT_BYTES
            return True
        result.rows.append(row)
        result.result_bytes += size
    return False


def _job_failed(answer: dict[str, Any]) -> BigQueryAPIError | None:
    """A complete answer that carries the job's errors and no schema: the
    job failed (BigQuery usually says so with an HTTP error instead)."""
    errors = answer.get("errors")
    if answer.get("schema") or not isinstance(errors, list) or not errors:
        return None
    message, reason = error_detail(errors)
    return BigQueryAPIError(
        "the BigQuery job failed"
        + (f" ({reason})" if reason else "")
        + (f": {message}" if message else ""),
        status=400,
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
) -> Rows:
    """Steps 2-4 for a CHECKED statement. Pass ``estimate`` when the dry run
    already ran (the source runs it before reading markers)."""
    if estimate is None:
        estimate = dry_run(
            client,
            sql,
            max_bytes_processed=max_bytes_processed,
            default_dataset=default_dataset,
        )
    job_seconds = client.job_seconds(job_timeout)
    job_deadline = client.now() + job_seconds
    page_size = min(PAGE_ROWS, max_rows)
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
    )
    for _ in range(MAX_PAGES):
        page = convert_rows(result.fields, answer.get("rows"))
        if _take(result, page, max_rows=max_rows, max_result_bytes=max_result_bytes):
            return result
        token = answer.get("pageToken")
        if not isinstance(token, str) or not token:
            return result
        if len(result.rows) >= max_rows:
            result.truncated = TRUNCATED_MAX_ROWS
            return result
        answer = client.query_results(
            job,
            page_size=min(PAGE_ROWS, max_rows - len(result.rows)),
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
) -> Rows:
    """Up to ``max_rows`` rows of ``ref`` (described by ``table``, its
    tables.get) from row ``start_index`` on, within ``max_result_bytes``.
    When rows remain, ``truncated`` says which cap stopped the read and
    ``next_start_index`` is the row to continue from. ``selected_fields``
    (top-level column names) are sent in the table's column order, the order
    the rows then come in."""
    fields = schema_fields(table.get("schema"))
    if selected_fields:
        wanted = {name.lower() for name in selected_fields}
        known = {f["name"].lower() for f in fields}
        missing = sorted(wanted - known)
        if missing:
            raise ValueError(f"selected_fields names no column {missing[0]!r}")
        fields = [f for f in fields if f["name"].lower() in wanted]
    result = Rows(fields=fields, total_rows=_int(table.get("numRows")))
    token: str | None = None
    for page_number in range(MAX_PAGES):
        answer = client.list_rows(
            ref,
            page_size=min(PAGE_ROWS, max_rows - len(result.rows)),
            page_token=token,
            start_index=start_index if page_number == 0 else None,
            selected_fields=[f["name"] for f in fields] if selected_fields else None,
        )
        total = _int(answer.get("totalRows"))
        if total is not None:
            result.total_rows = total
        page = convert_rows(fields, answer.get("rows"))
        next_token = answer.get("pageToken")
        token = next_token if isinstance(next_token, str) and next_token else None
        if _take(result, page, max_rows=max_rows, max_result_bytes=max_result_bytes):
            break
        if token is None:
            return result
        if len(result.rows) >= max_rows:
            result.truncated = TRUNCATED_MAX_ROWS
            break
    else:
        result.truncated = TRUNCATED_MAX_ROWS
    result.next_start_index = start_index + len(result.rows)
    return result


__all__ = [
    "PAGE_ROWS",
    "READABLE_TABLE_TYPES",
    "TRUNCATED_MAX_RESULT_BYTES",
    "TRUNCATED_MAX_ROWS",
    "CapExceeded",
    "Estimate",
    "NotATable",
    "Rows",
    "dry_run",
    "read_table_rows",
    "run_select",
    "table_info",
]
