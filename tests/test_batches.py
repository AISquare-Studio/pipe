"""aisquare.pipe.core.batches: where a row source cuts envelopes."""

from __future__ import annotations

import itertools

import pytest

from aisquare.pipe.core.batches import INLINE_BATCH_BYTES, batch_bounds


def _sizes(bounds, rows, header=10, separator=1, closing=2):
    return [
        header + closing + sum(rows[a:b]) + separator * max(0, b - a - 1)
        for a, b in bounds
    ]


def test_no_rows_is_one_empty_batch():
    assert batch_bounds(
        [], max_rows=5, max_bytes=100, header=10, separator=1, closing=2
    ) == [(0, 0)]


def test_the_row_cap_cuts():
    assert batch_bounds(
        [1] * 5, max_rows=2, max_bytes=10**6, header=10, separator=1, closing=2
    ) == [
        (0, 2),
        (2, 4),
        (4, 5),
    ]


@pytest.mark.parametrize("separator", [1, 2])
def test_the_byte_cap_cuts_and_every_batch_fits(separator):
    rows = [7, 30, 30, 30, 5, 5, 5, 60, 1]
    bounds = batch_bounds(
        rows, max_rows=100, max_bytes=80, header=10, separator=separator, closing=2
    )
    assert bounds[0][0] == 0 and bounds[-1][1] == len(rows)
    assert all(a < b for a, b in bounds)
    assert all(b == c for (_, b), (c, _) in itertools.pairwise(bounds))
    assert all(s <= 80 for s in _sizes(bounds, rows, separator=separator))
    # Each cut is needed: the next row would not have fit.
    for (a, b), (_, _) in itertools.pairwise(bounds):
        assert _sizes([(a, b + 1)], rows, separator=separator)[0] > 80


def test_an_exact_fit_is_one_batch():
    # header 10 + closing 2 + 3 rows of 20 + 2 separators = 74
    assert batch_bounds(
        [20, 20, 20], max_rows=9, max_bytes=74, header=10, separator=1, closing=2
    ) == [(0, 3)]
    assert batch_bounds(
        [20, 20, 20], max_rows=9, max_bytes=73, header=10, separator=1, closing=2
    ) == [
        (0, 2),
        (2, 3),
    ]


def test_a_row_larger_than_the_cap_gets_its_own_batch():
    assert batch_bounds(
        [5, 500, 5], max_rows=9, max_bytes=100, header=10, separator=1, closing=2
    ) == [
        (0, 1),
        (1, 2),
        (2, 3),
    ]


def test_the_default_is_pipe_as_services_inline_size():
    assert INLINE_BATCH_BYTES == 262_144


def test_a_plan_counts_every_envelope_s_header_in_its_total():
    from aisquare.pipe.core.batches import BatchPlan

    plan = BatchPlan(max_rows=2, max_bytes=10**6, header=100, separator=1, closing=2)
    assert plan.total == 102  # no rows: one empty envelope
    assert plan.cost(10) == 10
    plan.add(10)
    assert plan.cost(10) == 11  # a separator
    plan.add(10)
    assert plan.cost(10) == 112  # a new envelope: its header and closing too
    plan.add(10)
    assert plan.total == 102 + 10 + 11 + 112
    assert plan.bounds() == [(0, 2), (2, 3)]
    # The total is exactly what the envelopes hold.
    sizes = [100 + 2 + 10 + 1 + 10, 100 + 2 + 10]
    assert plan.total == sum(sizes)


def test_batch_bounds_and_a_plan_agree():
    from aisquare.pipe.core.batches import BatchPlan

    rows = [7, 30, 30, 30, 5, 5, 5, 60, 1, 200, 3]
    plan = BatchPlan(max_rows=3, max_bytes=80, header=10, separator=2, closing=2)
    for row in rows:
        plan.add(row)
    assert plan.bounds() == batch_bounds(
        rows, max_rows=3, max_bytes=80, header=10, separator=2, closing=2
    )
    assert plan.total == sum(
        10 + 2 + sum(rows[a:b]) + 2 * max(0, b - a - 1) for a, b in plan.bounds()
    )
