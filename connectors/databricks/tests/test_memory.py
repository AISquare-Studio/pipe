"""databricks-files-source's peak memory (tracemalloc), so a pull fits a
512 MiB pod (#46, seq 13861).

The consumer drops each envelope, but its loop variable still holds the
previous one while the next file downloads, as pipe-as-service's pull loop
does. At b98842f a volume download held its chunks and then their join, a
DBFS read a bytearray and then its copy, and the source's own variable held
the previous file: about three files. Now: the one being read and the one
the consumer holds.
"""

from __future__ import annotations

import gc
import tracemalloc
from typing import Any

import pytest
from aisquare.pipe.core.envelope import PullParams
from aisquare.pipe.testing.host import host_answer

from aisquare_pipe_databricks import DatabricksFilesSource
from aisquare_pipe_databricks.connector import MAX_SOURCE_BYTES
from aisquare_pipe_databricks.toolkit import DEFAULT_MAX_BYTES, MAX_FILE_BYTES
from tests.fakes import FakeDatabricks, StoredFile, pat_blob

MIB = 1024 * 1024
POD_SHARE = 256 * MIB
VOL = "/Volumes/main/policies/docs"


def _fake(size: int, *, dbfs: bool) -> FakeDatabricks:
    fake = FakeDatabricks()
    fake.dbfs_read_size = 1024 * 1024  # DBFS's own 1 MB reads
    for i in range(3):
        stored = StoredFile(bytes([i]) * size)
        if dbfs:
            fake.dbfs[f"/mnt/policies/f{i}.bin"] = stored
        else:
            fake.volume[f"{VOL}/f{i}.bin"] = stored
    return fake


def _peak(fake: FakeDatabricks, path: str, *, holds: bool = True, **params: Any) -> int:
    """The pull's peak. ``holds``: the consumer's loop variable still holds
    the previous envelope while the next file downloads (pipe-as-service's
    loop); else it lets go of each one before asking for the next."""
    source = DatabricksFilesSource(session=fake)
    gc.collect()
    tracemalloc.start()
    try:
        base = tracemalloc.get_traced_memory()[0]
        pulled = 0
        for envelope in source.pull(
            pat_blob(dbfs=True), PullParams(params={"path": path, **params})
        ):
            assert envelope.data  # the consumer reads it, then drops it
            pulled += 1
            if not holds:
                del envelope
        assert pulled == 3
        return tracemalloc.get_traced_memory()[1] - base
    finally:
        tracemalloc.stop()


@pytest.mark.parametrize("dbfs", [False, True], ids=["volume", "dbfs"])
@pytest.mark.parametrize(
    "size", [DEFAULT_MAX_BYTES, MAX_SOURCE_BYTES], ids=["default", "cap"]
)
def test_a_pull_peaks_at_about_two_files(size, dbfs):
    path = "dbfs:/mnt/policies" if dbfs else VOL
    peak = _peak(_fake(size, dbfs=dbfs), path, max_bytes=size)
    # Measured: about 2.0 files (50 MiB at 25 MiB, 128 MiB at the 64 MiB cap),
    # for volumes and DBFS alike.
    assert peak < 2.5 * size, peak / MIB
    assert peak < POD_SHARE, peak / MIB


@pytest.mark.parametrize("dbfs", [False, True], ids=["volume", "dbfs"])
def test_the_source_lets_go_of_each_file_before_the_next(dbfs):
    """A consumer that drops each envelope: the pull then holds one file."""
    path = "dbfs:/mnt/policies" if dbfs else VOL
    size = MAX_SOURCE_BYTES
    peak = _peak(_fake(size, dbfs=dbfs), path, holds=False, max_bytes=size)
    assert peak < 1.5 * size, peak / MIB


def test_max_bytes_above_the_source_cap_is_a_422():
    assert MAX_SOURCE_BYTES < MAX_FILE_BYTES  # tools still read up to 100 MiB
    with pytest.raises(ValueError) as raised:
        list(
            DatabricksFilesSource(session=_fake(10, dbfs=False)).pull(
                pat_blob(),
                PullParams(params={"path": VOL, "max_bytes": MAX_SOURCE_BYTES + 1}),
            )
        )
    assert host_answer(raised.value).status == 422


def test_a_run_select_at_its_caps_fits_too():
    """The worst case for memory is many short values: each becomes a str
    object of about 60 bytes. 10,000 rows (the row cap) of 168 two-character
    columns is about the byte cap of JSON."""
    import json

    from aisquare_pipe_databricks import DatabricksToolkit
    from aisquare_pipe_databricks.toolkit import MAX_ROWS, SQL_BYTE_LIMIT
    from tests.fakes import Statement

    rows = [["ab"] * 168 for _ in range(MAX_ROWS)]
    assert len(json.dumps(rows)) >= SQL_BYTE_LIMIT
    fake = FakeDatabricks()
    fake.next_statement = Statement(
        ["SUCCEEDED"],
        rows=rows,
        columns=[{"name": f"c{i}", "type_name": "STRING"} for i in range(168)],
    )
    kit = DatabricksToolkit(session=fake)
    gc.collect()
    tracemalloc.start()
    try:
        base = tracemalloc.get_traced_memory()[0]
        result = kit.call_tool(
            "DATABRICKS_RUN_SELECT",
            {"statement": "SELECT * FROM t", "row_limit": MAX_ROWS},
            pat_blob(),
        )
        peak = tracemalloc.get_traced_memory()[1] - base
    finally:
        tracemalloc.stop()
    assert result.successful and result.data["row_count"] == MAX_ROWS
    # Measured 117 MiB (232 MiB at the 16 MiB cap before).
    assert peak < POD_SHARE * 0.6, peak / MIB
