"""redshift-query-source's peak memory (tracemalloc) at the defaults and at
the caps' maxima, so a pull fits a 512 MiB pod (#46; review seq 13894 m6).

The fake Data API client parses each GetStatementResult page from fresh JSON
bytes, as botocore does (a Stubber would hand back the same objects every
time, and their parse would not count). Pages of 1,000 rows, as the review
measured; every envelope is kept, as pipe-as-service keeps a pull's
envelopes for its response. The README's "Memory" table has the numbers.
"""

from __future__ import annotations

import gc
import json
import tracemalloc
from typing import Any

import pytest
from aisquare.pipe import PullParams

from aisquare_pipe_aws import connector as aws_connector
from aisquare_pipe_aws.redshift import dataapi, rows
from aisquare_pipe_aws.redshift.connector import (
    DEFAULT_MAX_RESULT_BYTES,
    DEFAULT_MAX_ROWS,
    MAX_BATCH_SIZE,
    MAX_RESULT_BYTES,
    MAX_ROWS,
    RedshiftQuerySource,
)
from tests.redshift_fakes import STATEMENT_ID, blob

MIB = 1024 * 1024
#: The bar from the review: half of a 512 MiB pod.
POD_SHARE = 256 * MIB
PAGE_ROWS = 1000


class PagedDataApi:
    """A redshift-data client: one statement, FINISHED at once, whose rows
    come in pages parsed from fresh JSON bytes."""

    def __init__(self, columns: int, width: int, total: int) -> None:
        self.total = total
        self.columns = [
            {"name": f"c{i}", "label": f"c{i}", "typeName": "varchar", "nullable": 1}
            for i in range(columns)
        ]
        row = json.dumps([{"stringValue": "v" * width} for _ in range(columns)])
        self._pages = {PAGE_ROWS: ("[" + ",".join([row] * PAGE_ROWS) + "]").encode()}
        self._row = row

    def _page(self, n: int) -> bytes:
        if n not in self._pages:
            self._pages[n] = ("[" + ",".join([self._row] * n) + "]").encode()
        return self._pages[n]

    def execute_statement(self, **_: Any) -> dict[str, Any]:
        return {"Id": STATEMENT_ID}

    def describe_statement(self, **_: Any) -> dict[str, Any]:
        return {
            "Id": STATEMENT_ID,
            "Status": "FINISHED",
            "HasResultSet": True,
            "ResultRows": self.total,
        }

    def cancel_statement(self, **_: Any) -> dict[str, Any]:
        return {"Status": True}

    def get_statement_result(self, Id: str, NextToken: str | None = None) -> Any:
        start = int(NextToken or 0)
        n = min(PAGE_ROWS, self.total - start)
        head = json.dumps({"ColumnMetadata": self.columns, "TotalNumRows": self.total})
        body = head[:-1].encode()
        if start + n < self.total:
            body += b',"NextToken":"' + str(start + n).encode() + b'"'
        return json.loads(body + b',"Records":' + bytes(self._page(n)) + b"}")


@pytest.fixture
def paged(monkeypatch):
    def make(columns: int, width: int, total: int) -> PagedDataApi:
        fake = PagedDataApi(columns, width, total)
        monkeypatch.setattr(dataapi, "build_client", lambda *a, **k: fake)
        monkeypatch.setattr(
            aws_connector._AwsSource, "_session", lambda self, cfg: None
        )
        fake._page(total % PAGE_ROWS or PAGE_ROWS)
        return fake

    return make


def _peak(params: dict[str, Any]) -> tuple[int, int, int]:
    """(peak bytes, rows, data bytes) of one pull, every envelope kept."""
    source = RedshiftQuerySource()
    list(source.pull(blob(), PullParams({"sql": "SELECT 1", "max_rows": 1})))
    gc.collect()
    tracemalloc.start()
    try:
        base = tracemalloc.get_traced_memory()[0]
        kept = list(
            source.pull(blob(), PullParams({"sql": "SELECT * FROM t", **params}))
        )
        peak = tracemalloc.get_traced_memory()[1] - base
    finally:
        tracemalloc.stop()
    return (
        peak,
        sum(e.metadata["row_count"] for e in kept),
        sum(len(e.data) for e in kept),
    )


def test_the_defaults_peak_well_under_the_pod_share(paged):
    paged(columns=10, width=98, total=DEFAULT_MAX_ROWS)
    peak, count, data = _peak({})
    assert count == DEFAULT_MAX_ROWS and data < DEFAULT_MAX_RESULT_BYTES
    # Measured 16 MiB for 10 MiB of JSON; 29 MiB before (seq 13894).
    assert peak < 32 * MIB, peak / MIB


def test_the_byte_cap_at_its_maximum_peaks_under_the_pod_share(paged):
    """About 1 KiB rows reach MAX_RESULT_BYTES first: the most bytes held."""
    paged(columns=10, width=98, total=2 * MAX_ROWS)
    peak, _count, data = _peak(
        {
            "max_rows": MAX_ROWS,
            "max_result_bytes": MAX_RESULT_BYTES,
            "batch_size": MAX_BATCH_SIZE,
        }
    )
    assert data > MAX_RESULT_BYTES * 0.95, data / MIB
    # Measured 96 MiB for 64 MiB of rows; 277 MiB at the old 100 MiB (13894).
    assert peak < POD_SHARE / 2, peak / MIB


def test_the_row_cap_at_its_maximum_peaks_under_the_pod_share(paged):
    """Many narrow rows: the most objects per byte."""
    paged(columns=4, width=8, total=2 * MAX_ROWS)
    peak, count, _data = _peak(
        {
            "max_rows": MAX_ROWS,
            "max_result_bytes": MAX_RESULT_BYTES,
            "batch_size": MAX_BATCH_SIZE,
        }
    )
    assert count == MAX_ROWS
    assert peak < POD_SHARE / 4, peak / MIB


def test_envelopes_built_from_encoded_rows_are_byte_for_byte_the_same():
    """The source builds each batch from the rows' JSON; it must be exactly
    what json.dumps({"columns", "rows"}) gave before (and so the hashes)."""
    columns = [
        {"name": "id", "type": "int8", "nullable": True},
        {"name": "név", "type": "varchar", "nullable": True, "label": "név"},
    ]
    batch = [
        {"id": 1, "név": 'Ünïcödé   "q" \\ ' + "\x00"},
        {"id": None, "név": None},
    ]
    want = json.dumps(
        {"columns": columns, "rows": batch}, ensure_ascii=False, default=str
    )
    encoded = [rows.envelope_json(r) for r in batch]
    assert rows.envelope_batch(columns, encoded) == want.encode("utf-8")
    assert rows.envelope_batch(columns, []) == json.dumps(
        {"columns": columns, "rows": []}, ensure_ascii=False
    ).encode("utf-8")
    digests = [rows.row_digest(r) for r in batch]
    assert rows.digests_sha256(digests) == rows.rows_sha256(batch)
    assert rows.result_sha256_of_digests(columns, digests) == rows.result_sha256(
        columns, batch
    )
