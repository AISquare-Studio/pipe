"""gcs-source's peak memory (tracemalloc), so a pull fits a 512 MiB pod (#46,
seq 13867 (a) / 13869), and the truncation marker it no longer needs to
hold an envelope back for.

The consumer drops each envelope, but its loop variable still holds the
previous one while the next object downloads, as pipe-as-service's pull loop
does. Before #46 a pull also held one envelope back, and a download held its
chunks and then their join: about four objects (400 MiB at 100 MiB objects).
Now: the one being read and the one the consumer holds.
"""

from __future__ import annotations

import gc
import tracemalloc
from dataclasses import dataclass
from typing import Any

import pytest
from aisquare.pipe.core.envelope import PullParams
from aisquare.pipe.testing.host import host_answer

from aisquare_pipe_gcs import GcsSource
from aisquare_pipe_gcs.connector import MAX_SOURCE_BYTES
from tests.fakes import FakeGoogle, StoredObject, blob

MIB = 1024 * 1024
POD_SHARE = 256 * MIB


def _objects(count: int, size: int) -> dict[str, StoredObject]:
    return {
        f"policies/f{i}.bin": StoredObject(bytes([i]) * size, generation=str(100 + i))
        for i in range(count)
    }


def _pull(google: FakeGoogle, **params: Any) -> list:
    return list(GcsSource(session=google).pull(blob(), PullParams(params=params)))


def _peak(google: FakeGoogle, **params: Any) -> int:
    source = GcsSource(session=google)
    google.objects["policies/warm-up.txt"] = StoredObject(b"hi", "text/plain")
    list(
        source.pull(
            blob(), PullParams(params={"object_names": ["policies/warm-up.txt"]})
        )
    )
    gc.collect()
    tracemalloc.start()
    try:
        base = tracemalloc.get_traced_memory()[0]
        pulled = PullParams(params={"prefix": "policies/f", **params})
        for envelope in source.pull(blob(), pulled):
            assert envelope.data  # the consumer reads it, then drops it
        return tracemalloc.get_traced_memory()[1] - base
    finally:
        tracemalloc.stop()


@pytest.mark.parametrize("truncated", [False, True])
def test_objects_at_the_cap_peak_at_about_two_objects(truncated):
    google = FakeGoogle(objects=_objects(3, MAX_SOURCE_BYTES))
    params: dict[str, Any] = {"max_bytes": MAX_SOURCE_BYTES}
    if truncated:
        params["max_objects"] = 2
    peak = _peak(google, **params)
    # Measured 129 MiB for 64 MiB objects either way (192 MiB truncated, at
    # 64 MiB, and 400 MiB at the old 100 MiB cap, before).
    assert peak < 2.5 * MAX_SOURCE_BYTES, peak / MIB
    assert peak < POD_SHARE, peak / MIB


def test_max_bytes_above_the_source_cap_is_a_422():
    with pytest.raises(ValueError) as raised:
        _pull(FakeGoogle(objects=_objects(1, 10)), max_bytes=MAX_SOURCE_BYTES + 1)
    assert host_answer(raised.value).status == 422


def test_a_truncated_pull_marks_only_its_last_envelope():
    google = FakeGoogle(objects=_objects(4, 10))
    envelopes = _pull(google, max_objects=3)
    assert [e.metadata["object_name"] for e in envelopes] == [
        f"policies/f{i}.bin" for i in range(3)
    ]
    for envelope in envelopes[:-1]:
        assert "truncated" not in envelope.metadata
    assert envelopes[-1].metadata["truncated"] == "max_objects"
    assert envelopes[-1].metadata["next_start_offset"] == "policies/f3.bin"


@dataclass
class _Inflates(StoredObject):
    """Stored gzip and small (its listed size), read inflated and larger."""

    stored_size: int = 1

    def resource(self, bucket: str, name: str) -> dict[str, Any]:
        resource = super().resource(bucket, name)
        resource["size"] = str(self.stored_size)
        return resource


def test_the_marker_survives_a_last_object_skipped_at_download():
    """When every object after one could still be skipped at download (only
    an object of unknown size or stored gzip can be), that one is held back,
    so the marker still lands on the last envelope that comes out."""
    objects = _objects(2, 10)
    objects["policies/f2.bin"] = _Inflates(
        b"x" * 100, content_encoding="gzip", generation="102", stored_size=5
    )
    objects["policies/f3.bin"] = StoredObject(b"next", generation="103")
    google = FakeGoogle(objects=objects)
    envelopes = _pull(google, max_objects=3, max_bytes=50)
    assert [e.metadata["object_name"] for e in envelopes] == [
        "policies/f0.bin",
        "policies/f1.bin",
    ]
    assert "truncated" not in envelopes[0].metadata
    assert envelopes[1].metadata["truncated"] == "max_objects"
    assert envelopes[1].metadata["next_start_offset"] == "policies/f3.bin"


def test_a_truncated_pull_with_nothing_readable_still_raises_with_the_offset():
    objects = {
        "policies/f0.bin": _Inflates(
            b"x" * 100, content_encoding="gzip", stored_size=5
        ),
        "policies/f1.bin": StoredObject(b"next", generation="101"),
    }
    from aisquare_pipe_gcs.connector import GcsPullTruncated

    with pytest.raises(GcsPullTruncated) as raised:
        _pull(FakeGoogle(objects=objects), max_objects=1, max_bytes=50)
    assert raised.value.next_start_offset == "policies/f1.bin"
    assert host_answer(raised.value).status == 422
