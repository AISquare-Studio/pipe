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
   ``max_result_bytes`` bytes of JSON rows, or :data:`MAX_PAGES` pages: a
   result that stops there says ``truncated`` (``max_rows``,
   ``max_result_bytes`` or ``max_pages``).

:func:`read_table_rows` reads one table's rows (tabledata.list, free: no
job) within the same row and byte caps.

Every page, the first one included, is read within :data:`READ_BYTES` and
:data:`READ_CELLS` (counted on the body as it streams in, before it is
parsed): a page over them is dropped and asked for again with at most half
the rows, down to one; a single row over them is :class:`RowTooLarge`.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from functools import partial
from typing import Any

from aisquare.pipe.core.batches import BatchPlan

from aisquare_pipe_bigquery.client import (
    POLL_PAUSE_SECONDS,
    BigQueryAPIError,
    BigQueryClient,
    PageCap,
    PageTooLarge,
    error_detail,
)
from aisquare_pipe_bigquery.refs import TableRef
from aisquare_pipe_bigquery.rows import (
    canonical,
    encoded_batch,
    iter_rows,
    mean_cells,
    schema_fields,
)
from aisquare_pipe_bigquery.sqlguard import NotReadOnly

TRUNCATED_MAX_ROWS = "max_rows"
TRUNCATED_MAX_RESULT_BYTES = "max_result_bytes"
#: :data:`MAX_PAGES` pages were read and rows remained.
TRUNCATED_MAX_PAGES = "max_pages"
#: Rows asked for per page at most (BigQuery also caps a page at 10 MB).
PAGE_ROWS = 10_000
#: Cells asked for per page at most. A page is parsed whole, and each cell
#: arrives as a ``{"v": "<string>"}`` object (about 250 bytes of Python
#: objects), so a page is held to about 12 MiB of them however wide the
#: rows are: 1,000 rows of 50 columns, 10,000 of 5.
PAGE_CELLS = 50_000
#: Rows per page while the schema is unknown (a dry run that gave none).
UNKNOWN_SCHEMA_PAGE_ROWS = 1_000
#: What a ``REPEATED`` column is guessed to hold (its elements, each a cell)
#: for the FIRST page: a schema can't say how long its arrays are. After it,
#: each page is sized from the last one's mean cells a row (:func:`page_rows`'s
#: ``last_cells``). Neither bounds a page: the read cap does
#: (:data:`READ_CELLS`).
REPEATED_CELLS = 1_000
#: JSON bytes a page is sized to hold at most once one has come back: the next
#: page asks for the rows that fit at the last page's bytes a row. A cell's
#: cost is about 250 bytes of objects plus its text, so long values are
#: bounded by bytes as short ones are by :data:`PAGE_CELLS`. BigQuery's own
#: page limit is about 10 MB (tabledata.list) and 20 MB (jobs.getQueryResults).
PAGE_BYTES = 4 * 1024 * 1024
#: A next page asks for at most this many times the rows the last page held,
#: so a page that came back short (by its bytes, its cells, or BigQuery's page
#: limit) grows back a step at a time: after a page of one row of more than
#: 25,000 cells (:data:`PAGE_CELLS` // its cells is 1), narrow rows are back
#: at :data:`PAGE_ROWS` a page within 14 pages.
PAGE_GROWTH = 2
#: The most one page's body may hold, counted as it is read and before it is
#: parsed (:class:`~aisquare_pipe_bigquery.client.PageCap`): bytes of JSON,
#: and cells (``{"v": ...}`` objects, a ``REPEATED`` column's every element
#: included). A page is SIZED to half of each (:data:`PAGE_BYTES`,
#: :data:`PAGE_CELLS`); one over either, whatever its arrays hold, is dropped
#: unparsed and asked for again with half the rows, down to one row, and a
#: single row over either is :class:`RowTooLarge`. So a page parsed holds at
#: most 100,000 cells from at most 8 MiB of JSON. What parsing it costs
#: depends on the text: about 32 MiB for ASCII, and up to about 90 MiB when
#: astral characters (emoji) make the page's text 4 bytes a character in
#: Python (README "Memory": 160 MiB at the caps, the worst measured).
READ_BYTES = 2 * PAGE_BYTES
READ_CELLS = 2 * PAGE_CELLS
#: Pages read at most for one result, whatever the page token says (a
#: dropped page asked for again counts once); rows left then are
#: ``truncated`` ``max_pages``. Rows of more than 25,000 cells (every
#: column's, a ``REPEATED`` one's every element) page one at a time, and
#: ``max_result_bytes`` stops those within about 1,350 pages at its 64 MiB
#: cap (rows of 25,001 one-digit integers, the fewest bytes such a row holds:
#: 1,342 row pages measured).
MAX_PAGES = 5_000
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


class RowTooLarge(ValueError):
    """One row is more than a page may hold (:data:`READ_BYTES` bytes of
    JSON or :data:`READ_CELLS` cells): nothing is returned. A ``ValueError``,
    so pipe-as-service answers 422: the caller can select fewer or shorter
    columns, or bound the arrays. ``row`` is its 0-based index (the table's
    row for a table read), ``page_bytes`` and ``cells`` the size of the page
    that held it alone (at least that, unless ``complete``). The message
    carries no value of it."""

    def __init__(
        self, row: int, page_bytes: int, cells: int, *, complete: bool, table: bool
    ) -> None:
        self.row = row
        self.page_bytes = page_bytes
        self.cells = cells
        self.complete = complete
        over = "" if complete else "over "
        where = "the table" if table else "the result"
        fix = (
            "read fewer columns (selected_fields), or read it with a SELECT that "
            "bounds the arrays"
            if table
            else "select fewer or shorter columns, or bound the arrays "
            "(ARRAY_AGG(... LIMIT n), ARRAY_LENGTH in a WHERE, or UNNEST them into "
            "rows)"
        )
        super().__init__(
            f"row {row} of {where} is too large to read: the page that holds it "
            f"alone is {over}{page_bytes:,} bytes of JSON with {over}{cells:,} values, "
            f"and a page may hold at most {READ_BYTES:,} bytes and {READ_CELLS:,} "
            f"values; {fix}"
        )


def _int(value: Any) -> int | None:
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return None


def cells_per_row(fields: list[dict[str, Any]]) -> int:
    """The cells one REST row of ``fields`` is guessed to carry from its
    schema: one per column, plus a record's own sub-fields, and a
    ``REPEATED`` column's :data:`REPEATED_CELLS` elements."""
    count = 0
    for f in fields:
        sub = f.get("fields")
        one = 1 + (cells_per_row(sub) if isinstance(sub, list) and sub else 0)
        count += one * REPEATED_CELLS if f.get("mode") == "REPEATED" else one
    return max(1, count)


def page_rows(
    fields: list[dict[str, Any]] | None,
    wanted: int,
    *,
    last_cells: int = 0,
    last_rows: int = 0,
    last_bytes: int = 0,
) -> int:
    """Rows to ask for in one page: at most ``wanted``, :data:`PAGE_ROWS`
    and :data:`PAGE_CELLS` cells.

    A row's cells are ``last_cells`` (the mean a row of the LAST page held,
    :func:`_page_shape`) once rows have come back; before that the schema's
    guess (:func:`cells_per_row`, which counts a ``REPEATED`` column as
    :data:`REPEATED_CELLS` elements), else :data:`UNKNOWN_SCHEMA_PAGE_ROWS`
    rows. Not the widest row of the whole pull: one wide row would hold every
    later page to one row. Not the last page's widest row either: a wide row
    that recurs (every 500th, say) would hold the page after each one to a
    row, and the pull to about 10 times the requests (#46 review seq 14138
    nit a: 100,000 rows in 1,793 requests, 104 sized from the mean).

    Once a page has come back (``last_rows`` rows in ``last_bytes`` bytes of
    JSON), also at most the rows that fit :data:`PAGE_BYTES` at its bytes a
    row, and :data:`PAGE_GROWTH` times its rows (so after a page of one wide
    row, the page grows back a doubling at a time).

    This is what a page is SIZED to; what bounds it is the read cap
    (:func:`_read_capped`): a wide row in the next page costs one dropped
    read."""
    per_row = last_cells if last_cells > 0 else (cells_per_row(fields) if fields else 0)
    by_cells = PAGE_CELLS // per_row if per_row > 0 else UNKNOWN_SCHEMA_PAGE_ROWS
    size = min(PAGE_ROWS, by_cells, wanted)
    if last_rows > 0 and last_bytes > 0:
        size = min(size, PAGE_GROWTH * last_rows, PAGE_BYTES * last_rows // last_bytes)
    return max(1, size)


def _page_shape(answer: dict[str, Any], client: BigQueryClient) -> tuple[int, int, int]:
    """(mean cells a row held, rows, bytes of JSON) of the page ``answer``,
    the client's last response. The mean, not the most: a next page sized
    from it may hold a wider row than the mean, and the read cap
    (:data:`READ_CELLS`) bounds that page all the same (dropped and asked for
    again with fewer rows), so this is speed only, never memory."""
    rows = answer.get("rows")
    count = len(rows) if isinstance(rows, list) else 0
    return mean_cells(rows), count, client.last_body_bytes


def _cap(rows: int) -> PageCap:
    """The read cap for a page of ``rows``: one row's page over it is read
    through (discarded) to say how large it is."""
    return PageCap(READ_BYTES, READ_CELLS, measure=rows <= 1)


def _fewer(dropped: PageTooLarge, rows: int, first_row: int, *, table: bool) -> int:
    """The rows to ask for after a page of ``rows`` from ``first_row`` came
    back over the cap: half, or fewer when what was read of it says that
    fewer fit :data:`PAGE_BYTES` and :data:`PAGE_CELLS` (so BigQuery's own
    page limit, which a page of long rows always reaches, costs one dropped
    read, not one per halving); :class:`RowTooLarge` for one row."""
    if rows <= 1:
        raise RowTooLarge(
            first_row,
            dropped.body_bytes,
            dropped.cells,
            complete=dropped.complete,
            table=table,
        ) from None
    fewer = rows // 2
    # The rows read whole: the cap fell inside the last one begun. A RECORD
    # value's "f" counts too, so this may be more, never fewer: min() keeps
    # the half.
    whole = dropped.rows - (0 if dropped.complete else 1)
    if whole > 0:
        fewer = min(
            fewer,
            whole * PAGE_BYTES // max(1, dropped.body_bytes),
            whole * PAGE_CELLS // max(1, dropped.cells),
        )
    return max(1, fewer)


def _read_capped(
    fetch: Callable[..., dict[str, Any]],
    rows: int,
    first_row: int,
    *,
    table: bool,
    again: Callable[..., dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """``fetch(page_size=, cap=)`` within the read cap: a page over it is
    asked for again with fewer rows (:func:`_fewer`), down to one, and one
    row over it is :class:`RowTooLarge`.

    ``again`` asks for the page again from its first row (``startIndex``,
    which both tabledata.list and getQueryResults document), not with the
    page token ``fetch`` sent: BigQuery doesn't document that a token sent
    again with a smaller ``maxResults`` starts at the same row."""
    while True:
        try:
            return fetch(page_size=rows, cap=_cap(rows))
        except PageTooLarge as e:
            rows = _fewer(e, rows, first_row, table=table)
            if again is not None:
                fetch = again


def _first_page_again(
    client: BigQueryClient, job: dict[str, Any], dropped: PageTooLarge, rows: int
) -> dict[str, Any]:
    """A query's first page, asked for again from its ``job``
    (getQueryResults with no page token) after the answer that brought it
    (jobs.query's, or a poll's: the job is done) was over the cap with
    ``rows`` rows."""
    return _read_capped(
        partial(client.query_results, job),
        _fewer(dropped, rows, 0, table=False),
        0,
        table=False,
    )


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
    #: Read with ``batching``: the envelopes the rows are cut into, planned
    #: as they are read; ``result_bytes`` is then every envelope's bytes,
    #: each one's columns header included.
    plan: BatchPlan | None = None

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
        cost = result.plan.cost(size) if result.plan is not None else size
        if result.result_bytes + cost > max_result_bytes:
            result.truncated = TRUNCATED_MAX_RESULT_BYTES
            return True
        if result.encoded is None:
            result.rows.append(row)
        else:
            result.encoded.append(encoded)
        if result.plan is not None:
            result.plan.add(size)
        result.result_bytes += cost
    return False


def _plan(result: Rows, batching: tuple[int, int] | None) -> None:
    """With ``batching`` (rows, bytes per envelope), plan the envelopes as
    the rows are read, so ``max_result_bytes`` counts every envelope's
    columns header too (:class:`~aisquare.pipe.core.batches.BatchPlan`)."""
    if batching is None:
        return
    result.plan = BatchPlan(
        max_rows=batching[0],
        max_bytes=batching[1],
        header=len(encoded_batch(result.fields, [])) - 2,
        separator=1,
        closing=2,
    )
    result.result_bytes = result.plan.total


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
    batching: tuple[int, int] | None = None,
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
    try:
        answer = client.start_query(
            sql,
            page_size=page_size,
            max_bytes_billed=max_bytes_processed,
            job_timeout_ms=int(job_seconds * 1000),
            wait_ms=client.wait_ms(job_seconds),
            request_id=str(uuid.uuid4()),
            default_dataset=default_dataset,
            cap=_cap(page_size),
        )
        job = answer.get("jobReference")
        job = job if isinstance(job, dict) else {}
    except PageTooLarge as e:
        # The job is done (its first page came back), but the page was over
        # the cap: the job it named gives it again, with fewer rows.
        job = e.job or {}
        if page_size > 1 and not job:
            raise BigQueryAPIError(
                "BigQuery's first page of results was too large to read, and its "
                "answer named no job to read it from"
            ) from None
        # Its traceback holds the dropped answer's frames: let them go first.
        answer = _first_page_again(client, job, e.with_traceback(None), page_size)
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
        try:
            answer = client.query_results(
                job,
                page_size=page_size,
                wait_ms=client.wait_ms(left),
                cap=_cap(page_size),
            )
        except PageTooLarge as e:
            answer = _first_page_again(client, job, e.with_traceback(None), page_size)
            break
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
    _plan(result, batching)
    pages = 1
    while True:
        per_row, last_rows, last_bytes = _page_shape(answer, client)
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
        if pages >= MAX_PAGES:
            result.truncated = TRUNCATED_MAX_PAGES
            return result
        del page
        answer = _read_capped(
            partial(client.query_results, job, page_token=token),
            page_rows(
                result.fields,
                max_rows - result.count,
                last_cells=per_row,
                last_rows=last_rows,
                last_bytes=last_bytes,
            ),
            result.count,
            table=False,
            again=partial(client.query_results, job, start_index=result.count),
        )
        pages += 1


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
    batching: tuple[int, int] | None = None,
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
    _plan(result, batching)
    token: str | None = None
    per_row = last_rows = last_bytes = 0
    names = [f["name"] for f in fields] if selected_fields else None
    for page_number in range(MAX_PAGES):
        answer = _read_capped(
            partial(
                client.list_rows,
                ref,
                page_token=token,
                start_index=start_index if page_number == 0 else None,
                selected_fields=names,
            ),
            page_rows(
                fields,
                max_rows - result.count,
                last_cells=per_row,
                last_rows=last_rows,
                last_bytes=last_bytes,
            ),
            start_index + result.count,
            table=True,
            again=partial(
                client.list_rows,
                ref,
                start_index=start_index + result.count,
                selected_fields=names,
            ),
        )
        total = _int(answer.get("totalRows"))
        if total is not None:
            result.total_rows = total
        per_row, last_rows, last_bytes = _page_shape(answer, client)
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
        # MAX_PAGES pages read, and the last one named a next one.
        result.truncated = TRUNCATED_MAX_PAGES
    result.next_start_index = start_index + result.count
    return result


__all__ = [
    "PAGE_BYTES",
    "PAGE_CELLS",
    "PAGE_GROWTH",
    "PAGE_ROWS",
    "READABLE_TABLE_TYPES",
    "READ_BYTES",
    "READ_CELLS",
    "TRUNCATED_MAX_PAGES",
    "TRUNCATED_MAX_RESULT_BYTES",
    "TRUNCATED_MAX_ROWS",
    "CapExceeded",
    "Estimate",
    "NotATable",
    "RowTooLarge",
    "Rows",
    "cells_per_row",
    "dry_run",
    "page_rows",
    "read_table_rows",
    "run_select",
    "table_info",
]
