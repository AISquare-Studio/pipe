"""The source's peak memory (tracemalloc) at the defaults and at the caps'
maxima, so a pull fits a 512 MiB pod (the #37 review's m1, checked here too).

The fake serves each result partition as fresh JSON bytes, as the network
would: what is measured is the source's own work (each partition parsed,
its rows converted and kept, the envelopes built) while the consumer keeps
every envelope, as pipe-as-service does for a pull's response. Snowflake,
not the connector, sizes the partitions; these use 10,000 rows. The README's
"Memory" table has the numbers.
"""

from __future__ import annotations

import gc
import json
import tracemalloc
from typing import Any

import pytest
from aisquare.pipe.core.envelope import PullParams

from aisquare_pipe_snowflake import SnowflakeQuerySource
from aisquare_pipe_snowflake.connector import (
    DEFAULT_MAX_RESULT_BYTES,
    DEFAULT_MAX_ROWS,
    MAX_BATCH_SIZE,
    MAX_RESULT_BYTES,
    MAX_ROWS,
)
from aisquare_pipe_snowflake.rows import canonical, encoded_batch
from tests.fakes import Canned, FakeResponse, FakeSnowflake, blob, col

MIB = 1024 * 1024
#: The bar from the review: half of a 512 MiB pod.
POD_SHARE = 256 * MIB
SQL = "SELECT * FROM BIG"
PARTITION_ROWS = 10_000


class PartitionedSnowflake(FakeSnowflake):
    """FakeSnowflake whose result partitions are JSON bytes made once per
    size (outside the measurement) and served as a fresh copy per request."""

    def __init__(self, columns: list[dict[str, Any]], total: int, cell) -> None:
        super().__init__()
        self.columns, self.total, self.cell = columns, total, cell
        self._parts: dict[int, bytes] = {}
        self.add(SQL, Canned(columns=columns))

    def part(self, n: int) -> bytes:
        if n not in self._parts:
            rows = (
                json.dumps([self.cell(c, i) for c in self.columns]) for i in range(n)
            )
            self._parts[n] = ("[" + ",".join(rows) + "]").encode()
        return self._parts[n]

    def _sizes(self, handle: str) -> list[int]:
        cap = int(self._statements[handle]["cap"])
        rows = min(self.total, cap)
        return [min(PARTITION_ROWS, rows - s) for s in range(0, rows, PARTITION_ROWS)]

    def _submit(self, body, params):
        answer = super()._submit(body, params)
        handle = answer.body["statementHandle"]
        parameters = body.get("parameters") or {}
        self._statements[handle]["cap"] = parameters.get("ROWS_PER_RESULTSET", "0")
        return answer

    def _poll(self, handle):
        if self._statements[handle]["canned"].columns is not self.columns:
            return super()._poll(handle)
        sizes = self._sizes(handle)
        head = {
            "code": "090001",
            "statementHandle": handle,
            "sqlState": "00000",
            "resultSetMetaData": {
                "numRows": sum(sizes),
                "format": "jsonv2",
                "rowType": self.columns,
                "partitionInfo": [
                    {"rowCount": n, "uncompressedSize": len(self.part(n))}
                    for n in sizes
                ],
            },
        }
        content = (
            json.dumps(head).encode()[:-1]
            + b',"data":'
            + bytes(self.part(sizes[0]))
            + b"}"
        )
        return FakeResponse(200, None, content=content)

    def _partition(self, handle, index):
        n = self._sizes(handle)[index]
        return FakeResponse(200, None, content=b'{"data":' + bytes(self.part(n)) + b"}")


def _int_cell(column: dict[str, Any], i: int) -> str:
    return str(1_000_000 + i)


def _wide_cell(column: dict[str, Any], i: int) -> str:
    return "x" * 240 + f"{i:010d}"


def _peak(fake: PartitionedSnowflake, params: dict[str, Any]) -> tuple[int, int, int]:
    """(peak bytes, rows, data bytes) of one pull, every envelope kept."""
    config = blob()
    list(
        SnowflakeQuerySource(session=fake).pull(
            config, PullParams({"sql": SQL, "max_rows": 1})
        )
    )
    for n in {PARTITION_ROWS, fake.total % PARTITION_ROWS or PARTITION_ROWS}:
        fake.part(min(n, fake.total))
    gc.collect()
    tracemalloc.start()
    try:
        base = tracemalloc.get_traced_memory()[0]
        kept = list(SnowflakeQuerySource(session=fake).pull(config, PullParams(params)))
        peak = tracemalloc.get_traced_memory()[1] - base
    finally:
        tracemalloc.stop()
    rows = sum(e.metadata["row_count"] for e in kept)
    return peak, rows, sum(len(e.data) for e in kept)


def test_the_defaults_peak_well_under_the_pod_share():
    columns = [col(f"C{i:02d}", "fixed") for i in range(50)]
    fake = PartitionedSnowflake(columns, DEFAULT_MAX_ROWS, _int_cell)
    peak, rows, data = _peak(fake, {"sql": SQL})
    assert rows == DEFAULT_MAX_ROWS and data < DEFAULT_MAX_RESULT_BYTES
    # Measured 47 MiB (6.7 MiB of JSON; one 10,000-row partition parsed is
    # most of it); 65 MiB before.
    assert peak < 64 * MIB, peak / MIB


def test_the_byte_cap_at_its_maximum_peaks_under_the_pod_share():
    """The widest rows reach MAX_RESULT_BYTES first: the most bytes held."""
    columns = [col(f"S{i}", "text") for i in range(4)]
    fake = PartitionedSnowflake(columns, 2 * MAX_ROWS, _wide_cell)
    peak, _rows, data = _peak(
        fake,
        {
            "sql": SQL,
            "max_rows": MAX_ROWS,
            "max_result_bytes": MAX_RESULT_BYTES,
            "batch_size": MAX_BATCH_SIZE,
        },
    )
    assert data > MAX_RESULT_BYTES * 0.9, data / MIB
    # Measured ~100 MiB for 64 MiB of JSON; 241 MiB at 100 MiB before.
    assert peak < POD_SHARE / 2, peak / MIB


def test_the_row_cap_at_its_maximum_peaks_under_the_pod_share():
    """Many narrow rows: the most objects per byte."""
    columns = [col(f"C{i}", "fixed") for i in range(10)]
    fake = PartitionedSnowflake(columns, 2 * MAX_ROWS, _int_cell)
    peak, rows, _data = _peak(
        fake,
        {
            "sql": SQL,
            "max_rows": MAX_ROWS,
            "max_result_bytes": MAX_RESULT_BYTES,
            "batch_size": MAX_BATCH_SIZE,
        },
    )
    assert rows == MAX_ROWS
    assert peak < POD_SHARE / 4, peak / MIB


def test_envelopes_built_from_encoded_rows_are_byte_for_byte_the_same():
    columns = [col("ID", "fixed"), col("NÉV", "text"), col("V", "variant")]
    rows = [
        {"ID": 1, "NÉV": 'Ünïcödé   "q" \\', "V": {"a": [1, 2]}},
        {"ID": None, "NÉV": None, "V": None},
    ]
    encoded = [canonical(r) for r in rows]
    assert encoded_batch(columns, encoded) == canonical(
        {"columns": columns, "rows": rows}
    )
    assert encoded_batch(columns, []) == canonical({"columns": columns, "rows": []})


# -- envelope size: what a host returns inline (#46, seq 13867 (b)) ----------------


def test_default_envelopes_fit_a_sync_pull_inline():
    """pipe-as-service returns at most 256 KiB per envelope inline in a sync
    pull and answers 400 for more; 500 rows of 1 KiB were twice that."""
    from aisquare.pipe.core.batches import INLINE_BATCH_BYTES

    columns = [col(f"S{i}", "text") for i in range(4)]
    fake = PartitionedSnowflake(columns, 2000, _wide_cell)
    envelopes = list(
        SnowflakeQuerySource(session=fake).pull(blob(), PullParams({"sql": SQL}))
    )
    assert sum(e.metadata["row_count"] for e in envelopes) == 2000
    assert all(len(e.data) <= INLINE_BATCH_BYTES for e in envelopes)
    assert all(e.metadata["row_count"] < 500 for e in envelopes)
    assert {e.metadata["batch_count"] for e in envelopes} == {len(envelopes)}
    assert all(json.loads(e.data)["rows"] for e in envelopes)


@pytest.mark.parametrize("limit", [1024, 64 * 1024, 1024 * 1024])
def test_max_batch_bytes_bounds_each_envelope(limit):
    columns = [col(f"S{i}", "text") for i in range(4)]
    fake = PartitionedSnowflake(columns, 3000, _wide_cell)
    envelopes = list(
        SnowflakeQuerySource(session=fake).pull(
            blob(),
            PullParams({"sql": SQL, "max_batch_bytes": limit, "batch_size": 10_000}),
        )
    )
    assert sum(e.metadata["row_count"] for e in envelopes) == 3000
    assert all(len(e.data) <= limit or e.metadata["row_count"] == 1 for e in envelopes)


@pytest.mark.parametrize("bad", [0, 1023, MAX_RESULT_BYTES + 1, "1", True])
def test_max_batch_bytes_is_checked(bad):
    fake = PartitionedSnowflake([col("S0", "text")], 1, _wide_cell)
    with pytest.raises(ValueError, match="max_batch_bytes"):
        list(
            SnowflakeQuerySource(session=fake).pull(
                blob(), PullParams({"sql": SQL, "max_batch_bytes": bad})
            )
        )
