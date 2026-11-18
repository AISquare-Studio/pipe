"""The source's peak memory (tracemalloc) at the defaults and at the caps'
maxima, so a pull fits a 512 MiB pod (the #37 review's m1, checked here too).

The fake serves each result partition as fresh JSON bytes, as the network
would: what is measured is the source's own work (each partition read a row
at a time, its rows converted and kept, the envelopes built). Snowflake, not
the connector, sizes the partitions: PartitionedSnowflake uses 10,000 rows
and builds each answer whole (its copy counts in the peak); SizedSnowflake
takes any sizes and hands an answer out a chunk at a time, as the network
does (#46 review seq 14088 M2). The README's "Memory" table has the numbers
(SizedSnowflake's, a process per pull).

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


def test_max_result_bytes_counts_every_envelope_s_columns_header():
    """Each envelope repeats the columns header: with 200 columns and 1 KiB
    envelopes the headers were most of the output, uncounted (250 MiB of
    envelopes under a 64 MiB cap; #46 reopen seq 13961). Now the envelopes'
    bytes in all, headers included, stay within max_result_bytes."""
    columns = [col(f"C{i:03d}", "text") for i in range(200)]
    fake = PartitionedSnowflake(columns, 5000, lambda c, i: "v")
    cap = 256 * 1024
    envelopes = list(
        SnowflakeQuerySource(session=fake).pull(
            blob(),
            PullParams({"sql": SQL, "max_batch_bytes": 1024, "max_result_bytes": cap}),
        )
    )
    assert sum(len(e.data) for e in envelopes) <= cap
    assert {e.metadata["truncated"] for e in envelopes} == {"max_result_bytes"}


# -- partitions Snowflake sizes: read a row at a time (#46 review seq 14088 M2) -------


class StreamedResponse(FakeResponse):
    """A 200 whose body is ``parts`` (bytes made outside the measurement),
    given out as the network does: ``iter_content`` makes a fresh copy of
    each chunk, never of the whole body. ``content`` (what a client that
    reads the whole body holds) joins them. ``consumed``: the bytes given
    out so far."""

    def __init__(self, parts: list[bytes]) -> None:
        super().__init__(200, None)
        self.parts = parts
        self.consumed = 0
        self.headers = {"Content-Type": "application/json"}

    @property  # type: ignore[override]
    def content(self) -> bytes:
        self.consumed = sum(len(p) for p in self.parts)
        return b"".join(self.parts)

    @content.setter
    def content(self, value: bytes) -> None:
        pass

    def json(self) -> Any:
        return json.loads(self.content)

    def iter_content(self, chunk_size: int = 1):
        for part in self.parts:
            view = memoryview(part)
            for start in range(0, len(part), chunk_size):
                chunk = bytes(view[start : start + chunk_size])
                self.consumed += len(chunk)
                yield chunk


class SizedSnowflake(PartitionedSnowflake):
    """PartitionedSnowflake whose partitions hold ``sizes`` rows (the last
    size repeats), and whose answers stream (:class:`StreamedResponse`).
    ``served`` has each answer with rows: (partition, its response)."""

    def __init__(self, columns, total, cell, sizes: list[int]) -> None:
        super().__init__(columns, total, cell)
        self.sizes = sizes
        self.served: list[tuple[int, StreamedResponse]] = []

    def _sizes(self, handle: str) -> list[int]:
        cap = int(self._statements[handle]["cap"])
        left, out = min(self.total, cap), []
        while left > 0:
            n = min(self.sizes[min(len(out), len(self.sizes) - 1)], left)
            out.append(n)
            left -= n
        return out

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
        response = StreamedResponse(
            [json.dumps(head).encode()[:-1], b',"data":', self.part(sizes[0]), b"}"]
        )
        self.served.append((0, response))
        return response

    def _partition(self, handle, index):
        response = StreamedResponse(
            [b'{"data":', self.part(self._sizes(handle)[index]), b"}"]
        )
        self.served.append((index, response))
        return response


def _sized_peak(fake: SizedSnowflake, params: dict[str, Any]) -> tuple[int, int, int]:
    """(peak bytes, rows, data bytes) of one pull, every envelope kept; every
    partition's bytes made first (outside the measurement)."""
    config = blob()
    list(
        SnowflakeQuerySource(session=fake).pull(
            config, PullParams({"sql": SQL, "max_rows": 1})
        )
    )
    cap = params.get("max_rows", DEFAULT_MAX_ROWS) + 1
    left, i = min(fake.total, cap), 0
    while left > 0:
        n = min(fake.sizes[min(i, len(fake.sizes) - 1)], left)
        fake.part(n)
        left, i = left - n, i + 1
    fake.served.clear()
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


_SF_CAPS = {
    "max_rows": MAX_ROWS,
    "max_result_bytes": MAX_RESULT_BYTES,
    "batch_size": MAX_BATCH_SIZE,
}
_NUMBERS = [col(f"C{i:02d}", "fixed") for i in range(50)]
_WIDE = [col(f"S{i}", "text") for i in range(4)]


@pytest.mark.parametrize(
    ("columns", "cell", "sizes", "params", "bound"),
    [
        # 417.9 MiB at 1a8b64e (review seq 14088 M2; 380.0 in this harness, now
        # 74.1): partition 0, in the
        # status answer, 100,000 rows of 50 NUMBER columns (52.5 MiB), was
        # never checked and parsed whole.
        pytest.param(
            _NUMBERS,
            _int_cell,
            [100_000],
            _SF_CAPS,
            POD_SHARE / 2,
            id="one-100k-partition",
        ),
        # 467.0 MiB (376.8 here, now 74.1): 1,000 rows, then a 99,000-row
        # partition (~52 MiB) that
        # was fetched because its uncompressedSize fit the bytes left.
        pytest.param(
            _NUMBERS,
            _int_cell,
            [1_000, 99_000],
            _SF_CAPS,
            POD_SHARE / 2,
            id="1k-then-99k",
        ),
        # 229.2 MiB (135.5 here, now 71.3): 4 wide TEXT columns, 60,000 rows
        # in one 58 MiB partition.
        pytest.param(
            _WIDE,
            _wide_cell,
            [60_000],
            _SF_CAPS,
            POD_SHARE / 2,
            id="wide-60k-partition",
        ),
        # 43.6 MiB (35.3 here, now 7.9) at the defaults, the 2-partition shape.
        pytest.param(
            _NUMBERS,
            _int_cell,
            [1_000, 99_000],
            {},
            POD_SHARE / 8,
            id="defaults-1k-then-99k",
        ),
    ],
)
def test_a_partition_snowflake_sized_large_is_read_a_row_at_a_time(
    columns, cell, sizes, params, bound
):
    fake = SizedSnowflake(columns, 2 * MAX_ROWS, cell, sizes)
    peak, rows, data = _sized_peak(fake, {"sql": SQL, **params})
    assert rows > 1_000
    assert data <= params.get("max_result_bytes", DEFAULT_MAX_RESULT_BYTES)
    assert peak < bound, peak / MIB


def test_reading_stops_at_the_cap_inside_a_partition():
    """max_result_bytes 1 MiB of a 100,000-row partition 0 (52.5 MiB): about
    that much is read, not the partition, and no later partition is asked
    for."""
    fake = SizedSnowflake(_NUMBERS, 2 * MAX_ROWS, _int_cell, [100_000])
    envelopes = list(
        SnowflakeQuerySource(session=fake).pull(
            blob(),
            PullParams({"sql": SQL, "max_rows": MAX_ROWS, "max_result_bytes": MIB}),
        )
    )
    assert {e.metadata["truncated"] for e in envelopes} == {"max_result_bytes"}
    assert sum(len(e.data) for e in envelopes) <= MIB
    assert [index for index, _ in fake.served] == [0]
    ((_, answer),) = fake.served
    assert answer.consumed <= MIB + 2 * 64 * 1024
    assert len(fake.part(100_000)) > 50 * MIB


class Utf8SizedSnowflake(SizedSnowflake):
    """SizedSnowflake sending non-ASCII as raw UTF-8 (not ``\\uXXXX``
    escapes): a row's text then decodes to what Python holds."""

    def part(self, n: int) -> bytes:
        if n not in self._parts:
            rows = (
                json.dumps([self.cell(c, i) for c in self.columns], ensure_ascii=False)
                for i in range(n)
            )
            self._parts[n] = ("[" + ",".join(rows) + "]").encode()
        return self._parts[n]


def test_the_largest_row_read_stays_under_the_bar_at_the_caps():
    """Rows of 4 TEXT cells of 2 MiB, an emoji in each (so Python holds each
    cell, and the row's text, at 4 bytes a character): just under ROW_BYTES
    of JSON, the largest row read. The worst shape measured: 177.1 MiB at
    the caps (136.6 at the defaults; 320.0 at 1a8b64e), the README's
    number."""
    from aisquare_pipe_snowflake.stream import ROW_BYTES

    def cell(column: dict[str, Any], i: int) -> str:
        return "\U0001f600" + "x" * (2 * MIB - 40) + f"{i:06d}"

    fake = Utf8SizedSnowflake(_WIDE, 12, cell, [5])
    assert ROW_BYTES - 1_000 < len(fake.part(1)) - 2 <= ROW_BYTES
    peak, rows, _data = _sized_peak(fake, {"sql": SQL, **_SF_CAPS})
    assert rows == 7  # max_result_bytes: 64 MiB of 8 MiB rows
    assert peak < POD_SHARE, peak / MIB
