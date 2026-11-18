"""DATABRICKS_RUN_SELECT's deadline, against a warehouse that holds the submit.

The fake warehouse runs each statement on a fake clock (tests/fakes.py): it
holds the POST for ``min(wait_timeout, run_seconds)`` and raises
``requests.ReadTimeout`` when that is longer than the request's ``timeout``,
leaving the statement running (``on_wait_timeout`` CONTINUE) or cancelling
it at ``wait_timeout`` (CANCEL). So a submit whose read timeout is shorter
than its ``wait_timeout`` fails here as it would against Databricks (seq
13947 FAIL 5: ``wait_timeout`` 50s on a 30 s read timeout).
"""

from __future__ import annotations

import math

import pytest

from aisquare_pipe_databricks import DatabricksToolkit
from aisquare_pipe_databricks.client import ANSWER_SLACK, API_MAX_WAIT, API_MIN_WAIT
from tests.fakes import Clock, FakeDatabricks, Statement, pat_blob

ROWS = {"rows": [[1]], "columns": [{"name": "x", "type_name": "INT"}]}


def _run(
    fake: FakeDatabricks,
    clock: Clock,
    timeout_seconds: int | None = None,
    budget: float | None = None,
):
    arguments: dict = {"statement": "SELECT 1 AS x"}
    if timeout_seconds is not None:
        arguments["timeout_seconds"] = timeout_seconds
    config = pat_blob() if budget is None else pat_blob(timeout_seconds=budget)
    kit = DatabricksToolkit(session=fake, clock=clock, sleep=clock.sleep)
    return kit.call_tool("DATABRICKS_RUN_SELECT", arguments, config)


def _wait(submit: dict) -> int:
    return int(str(submit["json"]["wait_timeout"]).removesuffix("s"))


def _every_request_ends_by(fake: FakeDatabricks, started: float, deadline: float):
    """Each request was sent with a timeout that runs out by the deadline."""
    for request in fake.requests:
        assert isinstance(request["timeout"], float | int), request
        ends = request["at"] - started + request["timeout"]
        assert ends <= deadline + 1e-9, (request["method"], request["url"], ends)


@pytest.mark.parametrize("timeout_seconds", [5, 6, 10, 30, 50, 55, 56, 60, 90, 120])
def test_the_submit_read_timeout_outlasts_its_wait_within_the_deadline(timeout_seconds):
    fake = FakeDatabricks(clock := Clock())
    fake.next_statement = Statement([], run_seconds=0.1, **ROWS)
    started = clock.now
    result = _run(fake, clock, timeout_seconds)
    assert result.successful, result.error
    _every_request_ends_by(fake, started, timeout_seconds)
    (submit,) = fake.sql_submissions()
    wait = _wait(submit)
    assert wait == 0 or API_MIN_WAIT <= wait <= API_MAX_WAIT
    assert submit["timeout"] <= timeout_seconds
    if wait:
        # The warehouse answers at wait_timeout: the read timeout outlasts it.
        assert submit["timeout"] > wait
        assert submit["json"]["on_wait_timeout"] == "CANCEL"
    else:
        assert submit["json"]["on_wait_timeout"] == "CONTINUE"


@pytest.mark.parametrize(
    ("timeout_seconds", "run_seconds"),
    [(10, 4.0), (40, 33.0), (50, 44.0), (55, 49.0), (60, 45.0), (120, 100.0)],
)
def test_a_statement_slower_than_30s_finishes_within_timeout_seconds(
    timeout_seconds, run_seconds
):
    """timeout_seconds above 30 is honoured: a 45 s statement on a 60 s
    deadline returns its rows (at b98842f the 30 s read timeout cut it off)."""
    fake = FakeDatabricks(clock := Clock())
    fake.next_statement = Statement([], run_seconds=run_seconds, **ROWS)
    started = clock.now
    result = _run(fake, clock, timeout_seconds)
    assert result.successful, result.error
    assert result.data["rows"] == [[1]]
    assert fake.cancelled == [] and fake.running() == []
    assert clock.now - started <= timeout_seconds
    _every_request_ends_by(fake, started, timeout_seconds)


@pytest.mark.parametrize("timeout_seconds", [5, 10, 30, 50, 55, 60, 120])
def test_a_statement_past_its_deadline_is_cancelled_and_nothing_keeps_running(
    timeout_seconds,
):
    fake = FakeDatabricks(clock := Clock())
    fake.next_statement = Statement([], run_seconds=math.inf)
    started = clock.now
    result = _run(fake, clock, timeout_seconds)
    assert not result.successful and result.error_code == "timeout"
    assert "cancel" in (result.error or "")
    assert clock.now - started <= timeout_seconds
    assert fake.statements["stmt-1"].cancelled_by in ("cancel", "warehouse")
    _every_request_ends_by(fake, started, timeout_seconds)
    clock.now = started + timeout_seconds
    assert fake.running() == []


@pytest.mark.parametrize("timeout_seconds", [10, 30, 50, 55])
def test_a_submit_whose_answer_never_comes_is_still_cancelled(timeout_seconds):
    """A client timeout on the submit: no statement_id comes back, so the
    warehouse must cancel it itself (on_wait_timeout CANCEL) by the deadline."""
    fake = FakeDatabricks(clock := Clock())
    fake.next_statement = Statement([], run_seconds=math.inf, submit_hold=math.inf)
    started = clock.now
    result = _run(fake, clock, timeout_seconds)
    assert not result.successful and result.error_code == "timeout"
    assert clock.now - started <= timeout_seconds
    _every_request_ends_by(fake, started, timeout_seconds)
    clock.now = started + timeout_seconds
    assert fake.running() == []
    assert fake.statements["stmt-1"].cancelled_by == "warehouse"


def test_a_poll_that_times_out_cancels_the_statement():
    fake = FakeDatabricks(clock := Clock())
    fake.next_statement = Statement([], run_seconds=math.inf)
    fake.poll_seconds = 1e9  # every poll hangs
    started = clock.now
    result = _run(fake, clock, 120)
    assert not result.successful and result.error_code == "timeout"
    assert fake.cancelled == ["stmt-1"] and fake.running() == []
    assert clock.now - started <= 120
    _every_request_ends_by(fake, started, 120)


@pytest.mark.parametrize("budget", [3.0, 5.5, 20.0, 45.0])
def test_a_host_budget_bounds_every_request_and_the_whole_call(budget):
    fake = FakeDatabricks(clock := Clock())
    fake.next_statement = Statement([], run_seconds=math.inf)
    started = clock.now
    result = _run(fake, clock, 120, budget=budget)
    assert not result.successful and result.error_code == "timeout"
    assert clock.now - started <= budget
    _every_request_ends_by(fake, started, budget)
    clock.now = started + budget
    assert fake.running() == []


def test_the_slack_and_wait_limits_are_the_apis():
    assert (API_MIN_WAIT, API_MAX_WAIT, ANSWER_SLACK) == (5, 50, 5.0)
