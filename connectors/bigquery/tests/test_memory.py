"""The source's peak memory (tracemalloc) at the defaults and at the caps'
maxima, so a pull fits a 512 MiB pod (review #37 m1).

The fake serves each REST page as fresh JSON bytes, as the network would:
what is measured is the source's own work (each page parsed, its rows
converted and kept, the envelopes built) while the consumer keeps every
envelope, as pipe-as-service does for a pull's response. The README's
"Memory" table has the numbers.
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

    def _serve(self, head: dict[str, Any], params: dict[str, Any]) -> FakeResponse:
        size = int(params.get("maxResults") or 100)
        start = int(params.get("pageToken") or params.get("startIndex") or 0)
        n = max(0, min(size, self.total - start))
        parts = [json.dumps(head).encode()[:-1]]
        if n:
            parts.append(b',"rows":[' + bytes(self.page(n)) + b"]")
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
