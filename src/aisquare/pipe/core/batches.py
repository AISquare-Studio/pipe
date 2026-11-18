"""Where a row source cuts its result into envelopes: at ``max_rows`` rows or
``max_bytes`` bytes of envelope data, whichever comes first, and what the
envelopes add up to.

A host can refuse a big envelope: pipe-as-service returns at most
:data:`INLINE_BATCH_BYTES` per envelope inline in a synchronous pull (its
``inline_max_bytes``) and answers 400 for a larger one when there is no blob
store. A 500-row batch of 1 KiB rows is twice that, so the row sources
(BigQuery, Snowflake, Redshift) cut batches by bytes too, by default at that
size.

Every envelope repeats the columns header, so a result's size is its
envelopes' bytes, headers included: :class:`BatchPlan` plans the cuts while
the rows are read, and its :attr:`~BatchPlan.total` is what a source checks
its ``max_result_bytes`` against.
"""

from __future__ import annotations

from collections.abc import Sequence

#: pipe-as-service's default ``inline_max_bytes``: the most of one envelope a
#: synchronous pull returns inline.
INLINE_BATCH_BYTES = 256 * 1024


class BatchPlan:
    """The envelopes a result is cut into, planned one row at a time.

    Each envelope's data is ``header`` bytes, the rows with ``separator``
    bytes between them, and ``closing`` bytes. An envelope holds at most
    ``max_rows`` rows and ``max_bytes`` bytes, except one row that alone is
    larger (it gets an envelope of its own). No rows is one empty envelope.
    """

    def __init__(
        self,
        *,
        max_rows: int,
        max_bytes: int,
        header: int,
        separator: int,
        closing: int,
    ) -> None:
        self.max_rows = max_rows
        self.max_bytes = max_bytes
        self.header = header
        self.separator = separator
        self.closing = closing
        self._starts: list[int] = [0]
        self._count = 0
        self._rows_in_last = 0
        self._last_bytes = header + closing
        #: Every planned envelope's bytes together (one empty envelope's at
        #: first).
        self.total = header + closing

    def _fits_last(self, row: int) -> bool:
        if not self._rows_in_last:
            return True
        return (
            self._rows_in_last < self.max_rows
            and self._last_bytes + self.separator + row <= self.max_bytes
        )

    def cost(self, row: int) -> int:
        """The bytes a row of ``row`` bytes would add to :attr:`total`: the
        row (and a separator), or a new envelope's header and closing too."""
        if self._fits_last(row):
            return row + (self.separator if self._rows_in_last else 0)
        return self.header + self.closing + row

    def add(self, row: int) -> None:
        """Plan the next row (of ``row`` bytes)."""
        added = self.cost(row)
        if self._fits_last(row):
            self._last_bytes += added
            self._rows_in_last += 1
        else:
            self._starts.append(self._count)
            self._last_bytes = added
            self._rows_in_last = 1
        self._count += 1
        self.total += added

    def bounds(self) -> list[tuple[int, int]]:
        """``(start, end)`` row ranges, one per envelope."""
        ends = [*self._starts[1:], self._count]
        return list(zip(self._starts, ends, strict=True))


def batch_bounds(
    row_sizes: Sequence[int],
    *,
    max_rows: int,
    max_bytes: int,
    header: int,
    separator: int,
    closing: int,
) -> list[tuple[int, int]]:
    """:class:`BatchPlan`'s ``(start, end)`` row ranges for rows of
    ``row_sizes`` bytes. No rows: one empty batch, ``[(0, 0)]``."""
    plan = BatchPlan(
        max_rows=max_rows,
        max_bytes=max_bytes,
        header=header,
        separator=separator,
        closing=closing,
    )
    for row in row_sizes:
        plan.add(row)
    return plan.bounds()


__all__ = ["INLINE_BATCH_BYTES", "BatchPlan", "batch_bounds"]
