"""The source's peak memory (tracemalloc) at the defaults and at the caps'
maxima, so a pull fits a 512 MiB pod (review #37 m1).

The fake serves each REST page as fresh JSON bytes, as the network would:
what is measured is the source's own work (each page parsed, its rows
converted and kept, the envelopes built). The README's "Memory" table has
the numbers.

What the consumer holds, the same model in every source's memory test: as
pipe-as-service's pull loop does, it converts each envelope as it arrives
and keeps the converted one (at most 1,000 in a sync pull, 10,000 async):
inline (base64) up to 256 KiB, else a blob-store reference (an async pull;
a sync pull refuses it). Its loop variable also holds the last envelope
while the next one is pulled. So the row sources' tests (BigQuery,
Snowflake, Redshift), whose envelopes are at most 256 KiB, KEEP every
envelope: one inline copy each. The GCS test, whose objects are far larger
than that, DROPS each one but holds the previous while the next downloads.
"""

from __future__ import annotations

import gc
import json
import tracemalloc
from typing import Any

import pytest
from aisquare.pipe.core.envelope import PullParams

from aisquare_pipe_bigquery import BigQueryQuerySource
from aisquare_pipe_bigquery.connector import (
    DEFAULT_MAX_RESULT_BYTES,
    DEFAULT_MAX_ROWS,
    MAX_BATCH_SIZE,
    MAX_RESULT_BYTES,
    MAX_ROWS,
)
from aisquare_pipe_bigquery.query import PAGE_ROWS
from aisquare_pipe_bigquery.rows import canonical, encoded_batch, rows_sha256
from tests.fakes import CannedQuery, FakeGoogle, FakeResponse, FakeTable, field_

MIB = 1024 * 1024
#: The bar from the review: half of a 512 MiB pod.
POD_SHARE = 256 * MIB
SQL = "SELECT * FROM sales.big"


class PagedGoogle(FakeGoogle):
    """FakeGoogle whose row pages are JSON bytes made once per page size
    (outside the measurement) and served as a fresh copy per request."""

    def __init__(self, fields: list[dict[str, Any]], total: int, cell) -> None:
        super().__init__()
        self.fields, self.total, self.cell = fields, total, cell
        self._pages: dict[int, bytes] = {}
        #: Each row page served: its rows, cells (-1: not counted), bytes, and
        #: the bytes of it the client read before it closed it.
        self.pages: list[dict[str, int]] = []
        self.add_dataset("acme-analytics.sales")
        self.add_table("acme-analytics.sales.big", FakeTable(schema=fields))
        self.add_query(SQL, CannedQuery(schema=fields))

    def page(self, n: int) -> bytes:
        if n not in self._pages:
            rows = (
                json.dumps({"f": [{"v": self.cell(f, i)} for f in self.fields]})
                for i in range(n)
            )
            self._pages[n] = ",".join(rows).encode()
        return self._pages[n]

    def rows_json(self, start: int, n: int) -> bytes:
        """The JSON of rows ``start`` to ``start + n``, comma-joined."""
        return self.page(n)

    def clamp(self, start: int, n: int) -> int:
        """The rows a page holds when ``n`` are asked for from ``start``."""
        return n

    def cells(self, start: int, n: int) -> int:
        """The cells of rows ``start`` to ``start + n`` (-1: not counted)."""
        return -1

    def _serve(self, head: dict[str, Any], params: dict[str, Any]) -> FakeResponse:
        size = int(params.get("maxResults") or 100)
        start = int(params.get("pageToken") or params.get("startIndex") or 0)
        n = self.clamp(start, max(0, min(size, self.total - start)))
        parts = [json.dumps(head).encode()[:-1]]
        if n:
            parts.append(b',"rows":[' + bytes(self.rows_json(start, n)) + b"]")
        if start + n < self.total:
            parts.append(b',"pageToken":"' + str(start + n).encode() + b'"')
        parts.append(b"}")
        content = b"".join(parts)
        page = {"rows": n, "cells": self.cells(start, n), "bytes": len(content)}
        self.pages.append(page)

        def closed(response: FakeResponse) -> None:
            page["read"] = response.consumed

        return FakeResponse(200, None, content=content, on_close=closed)

    @property
    def served(self) -> list[tuple[int, int]]:
        """(rows, cells) of each row page served with rows."""
        return [(p["rows"], p["cells"]) for p in self.pages if p["rows"]]

    def _rows(self, table, params):
        return self._serve({"totalRows": str(self.total)}, params)

    def _answer(self, project, job_id, params):
        head = {
            "jobReference": {"projectId": project, "jobId": job_id, "location": "US"},
            "jobComplete": True,
            "schema": {"fields": self.fields},
            "totalRows": str(self.total),
            "totalBytesProcessed": "1000",
        }
        return self._serve(head, params)


def _int_cell(field: dict[str, Any], i: int) -> str:
    return str(1_000_000 + i)


def _wide_cell(field: dict[str, Any], i: int) -> str:
    return "x" * 240 + f"{i:010d}"


def _peak(google: PagedGoogle, params: dict[str, Any]) -> tuple[int, int, int]:
    """(peak bytes, rows, data bytes) of one pull, every envelope kept."""
    from tests.fakes import blob

    config = blob()
    # Warm up outside the measurement: imports, the token, the page bytes.
    list(
        BigQueryQuerySource(session=google).pull(
            config, PullParams({"table": "sales.big", "max_rows": 1})
        )
    )
    google.page(min(params.get("max_rows", DEFAULT_MAX_ROWS), 10_000))
    for n in (1_000, 5_000, 10_000):
        google.page(min(n, google.total))
    gc.collect()
    tracemalloc.start()
    try:
        base = tracemalloc.get_traced_memory()[0]
        kept = list(
            BigQueryQuerySource(session=google).pull(config, PullParams(params))
        )
        peak = tracemalloc.get_traced_memory()[1] - base
    finally:
        tracemalloc.stop()
    rows = sum(e.metadata["row_count"] for e in kept)
    return peak, rows, sum(len(e.data) for e in kept)


def test_the_defaults_peak_well_under_the_pod_share():
    fields = [field_(f"c{i:02d}", "INT64") for i in range(50)]
    google = PagedGoogle(fields, DEFAULT_MAX_ROWS, _int_cell)
    peak, rows, data = _peak(google, {"table": "sales.big"})
    assert rows == DEFAULT_MAX_ROWS and data < DEFAULT_MAX_RESULT_BYTES
    # Measured 21 MiB (6.7 MiB of JSON); 149 MiB before pages were bounded.
    assert peak < 48 * MIB, peak / MIB


def test_the_byte_cap_at_its_maximum_peaks_under_the_pod_share():
    """The widest rows reach MAX_RESULT_BYTES first: the most bytes held."""
    fields = [field_(f"s{i}", "STRING") for i in range(4)]
    google = PagedGoogle(fields, 2 * MAX_ROWS, _wide_cell)
    peak, _rows, data = _peak(
        google,
        {
            "sql": SQL,
            "max_rows": MAX_ROWS,
            "max_result_bytes": MAX_RESULT_BYTES,
            "batch_size": MAX_BATCH_SIZE,
        },
    )
    assert data > MAX_RESULT_BYTES * 0.95, data / MIB
    # Measured ~90 MiB for 64 MiB of JSON; 241 MiB at 100 MiB before.
    assert peak < POD_SHARE / 2, peak / MIB


def test_the_row_cap_at_its_maximum_peaks_under_the_pod_share():
    """Many narrow rows: the most objects per byte."""
    fields = [field_(f"c{i}", "INT64") for i in range(10)]
    google = PagedGoogle(fields, 2 * MAX_ROWS, _int_cell)
    peak, rows, _data = _peak(
        google,
        {
            "table": "sales.big",
            "max_rows": MAX_ROWS,
            "max_result_bytes": MAX_RESULT_BYTES,
            "batch_size": MAX_BATCH_SIZE,
        },
    )
    assert rows == MAX_ROWS
    assert peak < POD_SHARE / 4, peak / MIB


def test_envelopes_built_from_encoded_rows_are_byte_for_byte_the_same():
    """The source builds each batch from the rows' canonical JSON; it must be
    exactly what canonical({"columns", "rows"}) gives (and so the hashes)."""
    fields = [
        field_("id", "INT64"),
        field_("név", "STRING"),
        field_("rec", "RECORD", fields=[field_("a", "FLOAT64"), field_("b", "BOOL")]),
        field_("tags", "STRING", "REPEATED"),
    ]
    rows = [
        {"id": 1, "név": 'Ünïcödé   "q" \\', "rec": {"a": 1.5, "b": True}, "tags": []},
        {"id": None, "név": None, "rec": None, "tags": ["x", "y"]},
    ]
    encoded = [canonical(r) for r in rows]
    assert encoded_batch(fields, encoded) == canonical(
        {"columns": fields, "rows": rows}
    )
    assert encoded_batch(fields, []) == canonical({"columns": fields, "rows": []})
    assert rows_sha256(rows)  # unchanged API


# -- envelope size: what a host returns inline (#46, seq 13867 (b)) ----------------


def test_default_envelopes_fit_a_sync_pull_inline():
    """pipe-as-service returns at most 256 KiB per envelope inline in a sync
    pull and answers 400 for more; 500 rows of 1 KiB were twice that."""
    from aisquare.pipe.core.batches import INLINE_BATCH_BYTES

    from tests.fakes import blob

    fields = [field_(f"s{i}", "STRING") for i in range(4)]
    google = PagedGoogle(fields, 2000, _wide_cell)
    envelopes = list(
        BigQueryQuerySource(session=google).pull(
            blob(), PullParams({"table": "sales.big"})
        )
    )
    assert sum(e.metadata["row_count"] for e in envelopes) == 2000
    assert all(len(e.data) <= INLINE_BATCH_BYTES for e in envelopes)
    assert all(e.metadata["row_count"] < 500 for e in envelopes)
    assert {e.metadata["batch_count"] for e in envelopes} == {len(envelopes)}
    firsts = [e.metadata["first_row"] for e in envelopes]
    assert firsts == sorted(firsts) and firsts[0] == 0
    assert all(json.loads(e.data)["rows"] for e in envelopes)  # valid JSON


@pytest.mark.parametrize("limit", [1024, 64 * 1024, 1024 * 1024])
def test_max_batch_bytes_bounds_each_envelope(limit):
    from tests.fakes import blob

    fields = [field_(f"s{i}", "STRING") for i in range(4)]
    google = PagedGoogle(fields, 3000, _wide_cell)
    envelopes = list(
        BigQueryQuerySource(session=google).pull(
            blob(),
            PullParams(
                {"table": "sales.big", "max_batch_bytes": limit, "batch_size": 10_000}
            ),
        )
    )
    assert sum(e.metadata["row_count"] for e in envelopes) == 3000
    assert all(len(e.data) <= limit or e.metadata["row_count"] == 1 for e in envelopes)


@pytest.mark.parametrize("bad", [0, 1023, MAX_RESULT_BYTES + 1, "1", True])
def test_max_batch_bytes_is_checked(bad):
    from tests.fakes import blob

    google = PagedGoogle([field_("s0", "STRING")], 1, _wide_cell)
    with pytest.raises(ValueError, match="max_batch_bytes"):
        list(
            BigQueryQuerySource(session=google).pull(
                blob(), PullParams({"table": "sales.big", "max_batch_bytes": bad})
            )
        )


# -- REPEATED columns (#46 reopen seq 13961, FAIL 3) -------------------------------


def _array_cell(elements: int):
    def cell(field: dict[str, Any], i: int) -> Any:
        return [{"v": str(1_000_000 + j)} for j in range(elements)]

    return cell


@pytest.mark.parametrize(
    ("elements", "params", "bound"),
    [
        # Measured 21 MiB (277 MiB before: 10,000-row pages of 100 elements).
        (100, {}, 48 * MIB),
        # Arrays ten times the first guess's. What's bounded is each page, so a
        # smaller byte cap keeps the test quick; at the full caps (64 MiB) it
        # measured 78 MiB, and 100-element rows 81 MiB (342 MiB before).
        (1000, {"max_rows": MAX_ROWS, "max_result_bytes": 16 * MIB}, 64 * MIB),
    ],
)
def test_a_repeated_column_is_paged_by_its_elements(elements, params, bound):
    fields = [field_("tags", "INT64", "REPEATED")]
    google = PagedGoogle(fields, 2 * MAX_ROWS, _array_cell(elements))
    peak, rows, _data = _peak(
        google, {"table": "sales.big", "batch_size": MAX_BATCH_SIZE, **params}
    )
    assert rows > 0
    assert peak < bound, peak / MIB


def _asked(google: PagedGoogle) -> list[int]:
    """The rows each tabledata.list request asked for."""
    return [
        int(r["params"]["maxResults"])
        for r in google.requests
        if r["url"].endswith("/data")
    ]


def test_the_schema_guess_sizes_the_first_page_and_rows_the_next():
    """The schema's guess (a REPEATED column as REPEATED_CELLS elements)
    sizes the first page only: no longer a floor (#46 reopen seq 14031), as
    the read cap, not the guess, bounds every page. Rows that held fewer
    cells let the next page grow, PAGE_GROWTH times at most; rows that held
    more make it shorter."""
    from aisquare_pipe_bigquery import query

    fields = [field_("tags", "INT64", "REPEATED"), field_("id", "INT64")]
    guess = query.PAGE_CELLS // ((1 * query.REPEATED_CELLS) + 1)
    # Rows of 3 elements: far under the guess, and the pages grow from it.
    google = PagedGoogle(
        fields, 1_000, lambda f, i: [{"v": "1"}] * 3 if f["name"] == "tags" else "1"
    )
    from tests.fakes import blob

    list(
        BigQueryQuerySource(session=google).pull(
            blob(), PullParams({"table": "sales.big", "max_rows": 1_000})
        )
    )
    asked = _asked(google)
    assert asked[:4] == [guess, 2 * guess, 4 * guess, 8 * guess]
    assert sum(google.served[i][0] for i in range(len(asked))) == 1_000
    # Arrays over the guess shrink the next page.
    google = PagedGoogle(
        fields,
        3_000,
        lambda f, i: [{"v": "1"}] * 1_999 if f["name"] == "tags" else "1",
    )
    list(
        BigQueryQuerySource(session=google).pull(
            blob(), PullParams({"table": "sales.big", "max_rows": 3_000})
        )
    )
    asked = _asked(google)
    assert asked[0] == guess
    assert asked[1] == query.PAGE_CELLS // 2_001


def test_a_page_is_sized_from_the_last_page_s_bytes_and_grows_a_step_at_a_time():
    from aisquare_pipe_bigquery import query

    fields = [field_("s", "STRING")]
    # 500 rows came back in 10 MB (BigQuery's page limit cut them short): the
    # next page asks for what fits PAGE_BYTES at 20 KB a row.
    assert query.page_rows(
        fields, 10_000, last_cells=1, last_rows=500, last_bytes=10_000_000
    ) == (query.PAGE_BYTES * 500 // 10_000_000)
    # 10 small rows came back: the next page asks for at most PAGE_GROWTH times
    # as many, not the 10,000 their bytes would fit.
    assert (
        query.page_rows(fields, 10_000, last_cells=1, last_rows=10, last_bytes=1_000)
        == query.PAGE_GROWTH * 10
    )
    # Neither bound grows a page past the cells', and an empty page says nothing.
    assert query.page_rows(
        fields, 10_000, last_cells=1, last_rows=10_000, last_bytes=1
    ) == min(query.PAGE_ROWS, query.PAGE_CELLS)
    assert query.page_rows(
        fields, 10_000, last_cells=1, last_rows=0, last_bytes=99
    ) == (query.PAGE_ROWS)


class SkewedGoogle(PagedGoogle):
    """PagedGoogle whose rows change at row ``skew_at``: each row below it is
    ``before`` (its cells, column by column), each from it on ``after``.
    ``limit`` is BigQuery's own page limit in bytes (none: every row asked
    for). Pages are made once per (rows before, rows after); ``served`` has
    each page's (rows, cells)."""

    def __init__(
        self,
        fields: list[dict[str, Any]],
        total: int,
        *,
        skew_at: int,
        before: list[Any],
        after: list[Any],
        limit: int | None = None,
    ) -> None:
        super().__init__(fields, total, lambda f, i: None)
        from aisquare_pipe_bigquery.rows import rest_cells

        self.skew_at, self.limit = skew_at, limit
        rows = [{"f": [{"v": v} for v in cells]} for cells in (before, after)]
        self.row_bytes = [json.dumps(row).encode() for row in rows]
        self.row_cells = [rest_cells(row) for row in rows]
        self._split: dict[tuple[int, int], bytes] = {}

    def _counts(self, start: int, n: int) -> tuple[int, int]:
        low = max(0, min(n, self.skew_at - start))
        return low, n - low

    def clamp(self, start: int, n: int) -> int:
        if self.limit is None:
            return n
        size = 0
        for i in range(start, start + n):
            size += len(self.row_bytes[i >= self.skew_at]) + 1
            if size > self.limit:
                return max(1, i - start)
        return n

    def cells(self, start: int, n: int) -> int:
        low, high = self._counts(start, n)
        return low * self.row_cells[0] + high * self.row_cells[1]

    def rows_json(self, start: int, n: int) -> bytes:
        low, high = self._counts(start, n)
        key = (low, high)
        if key not in self._split:
            self._split[key] = b",".join(
                [self.row_bytes[0]] * low + [self.row_bytes[1]] * high
            )
        return self._split[key]


def _warm_peak(
    google: PagedGoogle, params: dict[str, Any], *, warm_bytes: int | None = None
) -> tuple[int, int]:
    """(peak bytes, rows) of a pull, every envelope kept, after the same pull
    unmeasured (imports, the token, every page's bytes made). ``warm_bytes``:
    the unmeasured pull stops at that ``max_result_bytes``, past the pages
    whose shape changes (a page it didn't make is made in the measured one,
    and counted there: the peak can only come out higher)."""
    from tests.fakes import blob

    config = blob()
    warm = params if warm_bytes is None else {**params, "max_result_bytes": warm_bytes}
    list(BigQueryQuerySource(session=google).pull(config, PullParams(warm)))
    gc.collect()
    tracemalloc.start()
    try:
        base = tracemalloc.get_traced_memory()[0]
        kept = list(
            BigQueryQuerySource(session=google).pull(config, PullParams(params))
        )
        peak = tracemalloc.get_traced_memory()[1] - base
    finally:
        tracemalloc.stop()
    return peak, sum(e.metadata["row_count"] for e in kept)


def _elements(n: int, text=lambda j: str(1_000_000 + j)) -> list[dict[str, str]]:
    return [{"v": text(j)} for j in range(n)]


_SKEWS = {
    # A REPEATED column added later (older rows hold []), or ORDER BY array
    # length: the first page's rows hold far fewer cells than the later ones.
    "[]-then-100": ([[], "1"], [_elements(100), "1"]),
    "1-then-80": ([_elements(1), "1"], [_elements(80), "1"]),
    "[]-then-180-one-char": (
        [[], "1"],
        [_elements(180, lambda j: str(j % 10)), "1"],
    ),
}
_CAPS = {
    "max_rows": MAX_ROWS,
    "max_result_bytes": MAX_RESULT_BYTES,
    "batch_size": MAX_BATCH_SIZE,
}


@pytest.mark.parametrize(
    ("params", "total", "bound"),
    [
        # Measured 278, 224 and 378 MiB at 0b9e6e3 (#46 reopen seq 13987, FAIL
        # 2): the first page's [] sized the second at 9,951 rows (1M cells, a
        # 17 MiB page, under BigQuery's 20 MB page limit). Now 21, 20 and
        # 14 MiB: pages grow PAGE_GROWTH times a step.
        pytest.param({}, DEFAULT_MAX_ROWS, POD_SHARE / 4, id="defaults"),
        # Two full pages after the skew, which made the second one 10,000
        # rows. With 200,000 rows the caps stop them at 82,051
        # (max_result_bytes), 100,000 and 100,000 rows: 81, 80 and 51 MiB (the
        # README's numbers; 23-45 s each, so not run here).
        pytest.param(_CAPS, 2 * PAGE_ROWS + 50, POD_SHARE / 2, id="caps"),
    ],
)
@pytest.mark.parametrize("skew", list(_SKEWS))
def test_rows_that_grow_after_the_first_page_keep_each_page_bounded(
    skew, params, total, bound
):
    """A query whose first page's arrays are far shorter than the rest's."""

    before, after = _SKEWS[skew]
    fields = [field_("tags", "INT64", "REPEATED"), field_("id", "INT64")]
    google = SkewedGoogle(fields, total, skew_at=50, before=before, after=after)
    peak, rows = _warm_peak(google, {"sql": SQL, **params})
    assert rows > 1_000
    _assert_read_within_the_cap(google)
    assert peak < bound, peak / MIB


def test_a_table_read_keeps_each_page_bounded_when_rows_grow():
    """The same on the table path (tabledata.list; its 10 MB page limit kept
    it under the bar before, at 157 MiB)."""
    from aisquare_pipe_bigquery import query
    from tests.fakes import blob

    before, after = _SKEWS["[]-then-100"]
    fields = [field_("tags", "INT64", "REPEATED"), field_("id", "INT64")]
    google = SkewedGoogle(
        fields, DEFAULT_MAX_ROWS, skew_at=50, before=before, after=after
    )
    list(
        BigQueryQuerySource(session=google).pull(
            blob(), PullParams({"table": "sales.big"})
        )
    )
    _assert_read_within_the_cap(google)
    assert max(cells for _, cells in google.served) <= query.READ_CELLS


def _assert_read_within_the_cap(google: PagedGoogle) -> int:
    """Every row page was read whole within READ_BYTES and READ_CELLS, or
    dropped after at most READ_BYTES (and a chunk) of it was read. The pages
    dropped."""
    from aisquare_pipe_bigquery import client, query

    dropped = 0
    for page in google.pages:
        if page["read"] == page["bytes"]:
            assert page["bytes"] <= query.READ_BYTES, page
            assert page["cells"] <= query.READ_CELLS, page
        else:
            assert page["read"] <= query.READ_BYTES + client.READ_CHUNK, page
            assert page["bytes"] > query.READ_BYTES or page["cells"] > query.READ_CELLS
            dropped += 1
    return dropped


def test_a_page_over_the_read_cap_is_asked_again_with_the_rows_that_fit():
    """Rows of 20 KB: BigQuery's 10 MB page holds about 500 of the 10,000
    asked for, over READ_BYTES. It is dropped once its first READ_BYTES are
    read, and asked for again with the rows that fit PAGE_BYTES at what was
    read (not halved again and again against BigQuery's page limit); the
    next pages are sized by their bytes."""
    from aisquare_pipe_bigquery import query
    from tests.fakes import blob

    wide = ["x" * 20_000]
    google = SkewedGoogle(
        [field_("s", "STRING")],
        DEFAULT_MAX_ROWS,
        skew_at=0,
        before=wide,
        after=wide,
        limit=10_000_000,
    )
    envelopes = list(
        BigQueryQuerySource(session=google).pull(
            blob(), PullParams({"table": "sales.big"})
        )
    )
    asked = _asked(google)
    first_rows = google.served[0][0]
    assert asked[0] == query.PAGE_ROWS and first_rows < 600
    assert _assert_read_within_the_cap(google) == 1
    assert google.pages[0]["read"] < google.pages[0]["bytes"]
    assert 0 < asked[1] * len(google.row_bytes[1]) <= query.PAGE_BYTES
    assert all(
        n * len(google.row_bytes[1]) <= query.PAGE_BYTES + len(google.row_bytes[1])
        for n in asked[1:]
    )
    # Nothing skipped or repeated: the dropped page's rows came again.
    firsts = [e.metadata["first_row"] for e in envelopes]
    counts = [e.metadata["row_count"] for e in envelopes]
    assert firsts == [sum(counts[:i]) for i in range(len(counts))]


def test_max_result_bytes_counts_every_envelope_s_columns_header():
    """Each envelope repeats the columns header: with 200 columns and 1 KiB
    envelopes the headers were most of the output, uncounted (256 MiB of
    envelopes under a 64 MiB cap; #46 reopen seq 13961). Now the envelopes'
    bytes in all, headers included, stay within max_result_bytes."""
    from tests.fakes import blob

    fields = [field_(f"c{i:03d}", "STRING") for i in range(200)]
    google = PagedGoogle(fields, 5000, lambda f, i: "v")
    cap = 256 * 1024
    envelopes = list(
        BigQueryQuerySource(session=google).pull(
            blob(),
            PullParams(
                {"table": "sales.big", "max_batch_bytes": 1024, "max_result_bytes": cap}
            ),
        )
    )
    assert sum(len(e.data) for e in envelopes) <= cap
    assert {e.metadata["truncated"] for e in envelopes} == {"max_result_bytes"}
    rows = sum(e.metadata["row_count"] for e in envelopes)
    assert envelopes[-1].metadata["next_start_index"] == rows


# -- long arrays: every page read within a cap (#46 reopen seq 14031) -------------

#: BigQuery's own page limits (the most a page's JSON holds): jobs.query and
#: jobs.getQueryResults about 20 MB, tabledata.list about 10 MB.
QUERY_PAGE_LIMIT = 20_000_000
TABLE_PAGE_LIMIT = 10_000_000
_LONG_FIELDS = [field_("tags", "INT64", "REPEATED"), field_("id", "INT64")]

_LONG = {
    # SELECT customer_id, ARRAY_AGG(order_id) ... GROUP BY 1, ORDER BY
    # ARRAY_LENGTH DESC: the first page's rows hold the longest arrays, far
    # over the schema's guess (REPEATED_CELLS elements).
    "20k": [_elements(20_000), "1"],
    "22k": [_elements(22_000), "1"],
    "34k-one-char": [_elements(34_000, lambda j: str(j % 10)), "1"],
}
#: Each well under the bar: e3d068b measured 266-344 MiB at both.
_BOUND = {"defaults": POD_SHARE / 3, "caps": POD_SHARE / 2}


def _long_peak(google: SkewedGoogle, params: dict[str, Any], which: str) -> None:
    """The pull's peak under the bar for ``which`` (defaults or caps); then
    every page parsed was within the cap, and at least one was dropped."""
    extra = _CAPS if which == "caps" else {}
    peak, rows = _warm_peak(google, {**params, **extra}, warm_bytes=4 * MIB)
    assert rows > 50
    assert peak < _BOUND[which], peak / MIB
    assert _assert_read_within_the_cap(google) >= 1


@pytest.mark.parametrize(
    ("shape", "which", "total"),
    [
        # 292.9 MiB at e3d068b, at the defaults (148 rows, max_result_bytes) and
        # at the caps (381) alike: the first page asked for 49 rows (the guess),
        # 1.08M cells. Now 37.9 and 77.1 MiB (each in a process of its own;
        # a little more after other tests).
        ("22k", "defaults", 1_000),
        ("22k", "caps", 1_000),
        # 343.5 MiB at e3d068b (1.67M cells in the first page); now 38.2 at the
        # defaults, 73.9 at the caps (986 rows, a page a row: 60 s, so not run
        # here). 100 rows keep it quick: the first page is the same.
        ("34k-one-char", "defaults", 100),
    ],
)
def test_long_arrays_on_the_first_page_keep_a_query_under_the_pod_share(
    shape, which, total
):
    """The first page (jobs.query's answer) held 49 rows of 20,000+
    elements. Now its body is dropped once it is over READ_CELLS, and the
    job gives the first page again with the rows that fit."""
    from aisquare_pipe_bigquery import query

    row = _LONG[shape]
    google = SkewedGoogle(
        _LONG_FIELDS, total, skew_at=0, before=row, after=row, limit=QUERY_PAGE_LIMIT
    )
    _long_peak(google, {"sql": SQL}, which)
    assert google.pages[0]["cells"] > query.READ_CELLS  # jobs.query's page
    assert google.pages[0]["read"] < google.pages[0]["bytes"]


@pytest.mark.parametrize("which", ["defaults", "caps"])
def test_long_arrays_after_short_ones_keep_a_query_under_the_pod_share(which):
    """[] for the first 50 rows, then 22,000 elements: the page that reaches
    the long rows held 49 of them at e3d068b (the floor), 286.9 MiB at the
    defaults and at the caps. Now 37.9 and 77.2 MiB."""
    google = SkewedGoogle(
        _LONG_FIELDS,
        1_000,
        skew_at=50,
        before=[[], "1"],
        after=_LONG["22k"],
        limit=QUERY_PAGE_LIMIT,
    )
    _long_peak(google, {"sql": SQL}, which)


def test_long_arrays_keep_a_table_read_under_the_pod_share():
    """tabledata.list's 10 MB page limit held it to 146.6 MiB at e3d068b, at
    the defaults and at the caps; now 36.7 MiB (76.0 at the caps)."""
    row = _LONG["20k"]
    google = SkewedGoogle(
        _LONG_FIELDS, 1_000, skew_at=0, before=row, after=row, limit=TABLE_PAGE_LIMIT
    )
    _long_peak(google, {"table": "sales.big"}, "defaults")


# -- the worst page the read cap admits (#46 review seq 14088 n1) --------------------


class Utf8SkewedGoogle(SkewedGoogle):
    """SkewedGoogle sending non-ASCII as raw UTF-8, as Google does (not
    ``\\uXXXX`` escapes): a page's text then decodes to what Python holds."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        rows = [
            {"f": [{"v": v} for v in cells]}
            for cells in (kwargs["before"], kwargs["after"])
        ]
        self.row_bytes = [json.dumps(r, ensure_ascii=False).encode() for r in rows]


def test_the_worst_page_the_read_cap_admits_stays_under_the_bar_at_the_caps():
    """Rows of 99,002 cells in 7.7 MB of JSON (right under READ_CELLS and
    READ_BYTES), an emoji in every cell, so Python holds the page's text at
    4 bytes a character: one row a page. The worst shape measured: 160.4 MiB
    at the caps (120.7 at the defaults), the README's number."""
    from aisquare_pipe_bigquery import query

    row = [_elements(99_000, lambda j: "\U0001f600" + "x" * 58 + f"{j:05d}"), "1"]
    google = Utf8SkewedGoogle(
        [field_("tags", "STRING", "REPEATED"), field_("id", "INT64")],
        40,
        skew_at=0,
        before=row,
        after=row,
        limit=QUERY_PAGE_LIMIT,
    )
    assert len(google.row_bytes[1]) <= query.READ_BYTES
    assert google.row_cells[1] <= query.READ_CELLS
    peak, rows = _warm_peak(google, {"sql": SQL, **_CAPS}, warm_bytes=4 * MIB)
    assert rows >= 8  # max_result_bytes: 64 MiB of 7.7 MB rows
    assert peak < POD_SHARE, peak / MIB
