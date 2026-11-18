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
        return FakeResponse(200, None, content=b"".join(parts))

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
        # Measured 22 MiB (277 MiB before: 10,000-row pages of 100 elements).
        (100, {}, 48 * MIB),
        # Arrays ten times the first guess's. What's bounded is each page, so a
        # smaller byte cap keeps the test quick; at the full caps (64 MiB) it
        # measured 79 MiB, and 100-element rows 82 MiB (342 MiB before).
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


def test_the_schema_guess_is_a_floor_for_every_page():
    """Every page is sized from the schema's guess (a REPEATED column as
    REPEATED_CELLS elements), not only the first: rows that held fewer cells
    say nothing about the next ones (#46 reopen seq 13987, FAIL 2). Rows that
    held more make the next page shorter."""
    from aisquare_pipe_bigquery import query

    fields = [field_("tags", "INT64", "REPEATED"), field_("id", "INT64")]
    guess = query.PAGE_CELLS // ((1 * query.REPEATED_CELLS) + 1)
    # Rows of 3 elements: far under the guess, and the pages stay at it.
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
    assert len(asked) > 2 and set(asked[:-1]) == {guess}
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
        fields, 10_000, seen=1, last_rows=500, last_bytes=10_000_000
    ) == (query.PAGE_BYTES * 500 // 10_000_000)
    # 10 small rows came back: the next page asks for at most PAGE_GROWTH times
    # as many, not the 10,000 their bytes would fit.
    assert (
        query.page_rows(fields, 10_000, seen=1, last_rows=10, last_bytes=1_000)
        == query.PAGE_GROWTH * 10
    )
    # Neither bound grows a page past the cells', and an empty page says nothing.
    assert query.page_rows(
        fields, 10_000, seen=1, last_rows=10_000, last_bytes=1
    ) == min(query.PAGE_ROWS, query.PAGE_CELLS)
    assert query.page_rows(fields, 10_000, seen=1, last_rows=0, last_bytes=99) == (
        query.PAGE_ROWS
    )


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
        self.served: list[tuple[int, int]] = []
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

    def rows_json(self, start: int, n: int) -> bytes:
        low, high = self._counts(start, n)
        self.served.append((n, low * self.row_cells[0] + high * self.row_cells[1]))
        key = (low, high)
        if key not in self._split:
            self._split[key] = b",".join(
                [self.row_bytes[0]] * low + [self.row_bytes[1]] * high
            )
        return self._split[key]


def _warm_peak(google: PagedGoogle, params: dict[str, Any]) -> tuple[int, int]:
    """(peak bytes, rows) of a pull, every envelope kept, after the same pull
    unmeasured (imports, the token, every page's bytes made)."""
    from tests.fakes import blob

    config = blob()
    list(BigQueryQuerySource(session=google).pull(config, PullParams(params)))
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
        # Measured 278, 224 and 378 MiB before (#46 reopen seq 13987, FAIL 2):
        # the first page's [] sized the second at 9,951 rows (1M cells, a
        # 17 MiB page, under BigQuery's 20 MB page limit). Now 9.7, 7.9 and
        # 6.0 MiB.
        pytest.param({}, DEFAULT_MAX_ROWS, POD_SHARE / 4, id="defaults"),
        # Two full pages after the skew, which made the second one 10,000
        # rows; now 18, 15 and 10 MiB. With 200,000 rows the caps stop them at
        # 82,051 (max_result_bytes), 100,000 and 100,000 rows: 75, 76 and
        # 49 MiB (the README's numbers; 24-46 s each, so not run here).
        pytest.param(_CAPS, 2 * PAGE_ROWS + 50, POD_SHARE / 2, id="caps"),
    ],
)
@pytest.mark.parametrize("skew", list(_SKEWS))
def test_rows_that_grow_after_the_first_page_keep_each_page_bounded(
    skew, params, total, bound
):
    """A query whose first page's arrays are far shorter than the rest's."""
    from aisquare_pipe_bigquery import query

    before, after = _SKEWS[skew]
    fields = [field_("tags", "INT64", "REPEATED"), field_("id", "INT64")]
    google = SkewedGoogle(fields, total, skew_at=50, before=before, after=after)
    peak, rows = _warm_peak(google, {"sql": SQL, **params})
    assert rows > 1_000
    # Each page within PAGE_CELLS: the floor (REPEATED_CELLS elements a row)
    # is over every row's arrays here.
    assert max(cells for _, cells in google.served) <= query.PAGE_CELLS
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
    assert max(cells for _, cells in google.served) <= query.PAGE_CELLS


def test_a_page_cut_short_by_bigquerys_page_limit_sizes_the_next_by_its_bytes():
    """Rows of 20 KB: BigQuery's 10 MB page holds about 500 of the 10,000
    asked for, and the next page asks for what fits PAGE_BYTES."""
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
    list(
        BigQueryQuerySource(session=google).pull(
            blob(), PullParams({"table": "sales.big"})
        )
    )
    asked = _asked(google)
    first_rows = google.served[0][0]
    assert asked[0] == query.PAGE_ROWS and first_rows < 600
    assert 0 < asked[1] * len(google.row_bytes[1]) <= query.PAGE_BYTES
    assert asked[1] < first_rows


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
