"""Where a row source cuts its result into envelopes: at ``max_rows`` rows or
``max_bytes`` bytes of envelope data, whichever comes first.

A host can refuse a big envelope: pipe-as-service returns at most
:data:`INLINE_BATCH_BYTES` per envelope inline in a synchronous pull (its
``inline_max_bytes``) and answers 400 for a larger one when there is no blob
store. A 500-row batch of 1 KiB rows is twice that, so the row sources
(BigQuery, Snowflake, Redshift) cut batches by bytes too, by default at that
size.
"""

from __future__ import annotations

from collections.abc import Sequence

#: pipe-as-service's default ``inline_max_bytes``: the most of one envelope a
#: synchronous pull returns inline.
INLINE_BATCH_BYTES = 256 * 1024


def batch_bounds(
    row_sizes: Sequence[int],
    *,
    max_rows: int,
    max_bytes: int,
    header: int,
    separator: int,
    closing: int,
) -> list[tuple[int, int]]:
    """``(start, end)`` row ranges, one per envelope: each at most
    ``max_rows`` rows and, with its ``header`` (bytes before the first row),
    ``separator`` bytes between rows and ``closing`` bytes after the last,
    at most ``max_bytes`` bytes, except a batch of one row that alone is
    larger (it still gets an envelope of its own). No rows: one empty
    batch, ``[(0, 0)]``."""
    bounds: list[tuple[int, int]] = []
    start = 0
    size = header + closing
    for index, row in enumerate(row_sizes):
        count = index - start
        grown = size + row + (separator if count else 0)
        if count and (count >= max_rows or grown > max_bytes):
            bounds.append((start, index))
            start, size = index, header + closing + row
        else:
            size = grown
    bounds.append((start, len(row_sizes)))
    return bounds


__all__ = ["INLINE_BATCH_BYTES", "batch_bounds"]
