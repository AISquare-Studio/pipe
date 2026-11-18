"""The deadlines: DATABRICKS_RUN_SELECT's, against a warehouse that holds the
submit, and the host's time budget over file reads.

The fake warehouse runs each statement on a fake clock (tests/fakes.py): it
holds the POST for ``min(wait_timeout, run_seconds)`` and raises
``requests.ReadTimeout`` when that is longer than the request's ``timeout``,
leaving the statement running (``on_wait_timeout`` CONTINUE) or cancelling
it at ``wait_timeout`` (CANCEL). So a submit whose read timeout is shorter
than its ``wait_timeout`` fails here as it would against Databricks (seq
13947 FAIL 5: ``wait_timeout`` 50s on a 30 s read timeout).

File reads run on the same fake clock: each DBFS read, and each read of a
volume download's body, takes time, and a read slower than its timeout
raises as a socket does (seq 13990 m3).
"""

from __future__ import annotations

import math

import pytest

from aisquare_pipe_databricks import DatabricksToolkit
from aisquare_pipe_databricks.client import (
    ANSWER_SLACK,
    API_MAX_WAIT,
    API_MIN_WAIT,
    MIN_REQUEST_TIMEOUT,
)
from tests.fakes import Clock, FakeDatabricks, Statement, StoredFile, pat_blob

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
    assert submit["timeout"] <= timeout_seconds
    if timeout_seconds <= API_MAX_WAIT + ANSWER_SLACK:
        assert API_MIN_WAIT <= wait <= API_MAX_WAIT
        assert submit["json"]["on_wait_timeout"] == "CANCEL"
        if timeout_seconds >= API_MIN_WAIT + ANSWER_SLACK:
            # The warehouse answers at wait_timeout: the read timeout outlasts it.
            assert submit["timeout"] > wait
        else:
            # Less than that left: the client may give up first, and the
            # warehouse still cancels at wait_timeout (5 s).
            assert (wait, submit["timeout"]) == (API_MIN_WAIT, timeout_seconds)
    else:
        assert wait == 0 and submit["json"]["on_wait_timeout"] == "CONTINUE"


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
    """Every request ends by the budget, and the statement by the budget or,
    under 5 s, by the 5 s the warehouse itself cancels it at."""
    fake = FakeDatabricks(clock := Clock())
    fake.next_statement = Statement([], run_seconds=math.inf)
    started = clock.now
    result = _run(fake, clock, 120, budget=budget)
    assert not result.successful and result.error_code == "timeout"
    assert clock.now - started <= budget
    _every_request_ends_by(fake, started, budget)
    clock.now = started + max(budget, API_MIN_WAIT)
    assert fake.running() == []


@pytest.mark.parametrize("budget", [0.5, 1.0, 3.0, 5.5])
def test_under_a_short_budget_even_a_lost_submit_answer_ends_by_5s(budget):
    """A host budget under 6 s submits synchronously (wait_timeout 5s,
    on_wait_timeout CANCEL), so a submit whose answer never comes still ends
    5 s after it was sent. At 810abb2 it went asynchronous (CONTINUE), and the
    statement ran on until the warehouse's STATEMENT_TIMEOUT (seq 13990 n3)."""
    fake = FakeDatabricks(clock := Clock())
    fake.next_statement = Statement([], run_seconds=math.inf, submit_hold=math.inf)
    started = clock.now
    result = _run(fake, clock, 120, budget=budget)
    assert not result.successful and result.error_code == "timeout"
    assert clock.now - started <= budget
    _every_request_ends_by(fake, started, budget)
    (submit,) = fake.sql_submissions()
    assert (_wait(submit), submit["json"]["on_wait_timeout"]) == (
        API_MIN_WAIT,
        "CANCEL",
    )
    clock.now = started + API_MIN_WAIT
    assert fake.running() == []
    assert fake.statements["stmt-1"].cancelled_by == "warehouse"


def test_the_slack_and_wait_limits_are_the_apis():
    assert (API_MIN_WAIT, API_MAX_WAIT, ANSWER_SLACK) == (5, 50, 5.0)


# -- the host's budget over file reads (seq 13990 m3) --------------------------------

VOL = "/Volumes/main/policies/docs"


def _read(fake: FakeDatabricks, clock: Clock, path: str, budget: float, **config):
    kit = DatabricksToolkit(session=fake, clock=clock, sleep=clock.sleep)
    return kit.call_tool(
        "DATABRICKS_READ_FILE",
        {"path": path, "max_bytes": 100 * 1024 * 1024},
        pat_blob(timeout_seconds=budget, **config),
    )


@pytest.mark.parametrize(("budget", "per_read"), [(5.0, 1.0), (5.0, 1.5), (2.5, 0.4)])
def test_a_dbfs_read_stops_at_the_host_budget(budget, per_read):
    """A DBFS file comes 1 MB a request (here 4 bytes): each request fits
    what is left of the budget, and none is sent once it is spent. At
    810abb2 each read got the full per-request timeout: a 20-read file on a
    5 s budget took 20 s."""
    fake = FakeDatabricks(clock := Clock())
    fake.dbfs["/mnt/policies/big.bin"] = StoredFile(b"x" * 80)  # 20 reads
    fake.dbfs_read_seconds = per_read
    started = clock.now
    result = _read(fake, clock, "dbfs:/mnt/policies/big.bin", budget, dbfs=True)
    assert not result.successful and result.error_code == "timeout"
    assert clock.now - started <= budget
    _every_request_ends_by(fake, started, budget)
    reads = [r for r in fake.requests if r["url"].endswith("/dbfs/read")]
    assert 0 < len(reads) < 20


@pytest.mark.parametrize(
    ("per_read", "seconds"), [(1024, 0.5), (16, 0.01), (1024, 1e9)]
)
def test_a_volume_download_that_trickles_stops_at_the_host_budget(per_read, seconds):
    """urllib3 2: the body is read one socket read at a time (read1), each
    with what is left of the budget as its socket timeout, so a body that
    drips in (or stalls) ends by the deadline. At 810abb2 the download read
    on for as long as each read kept within its own timeout."""
    fake = FakeDatabricks(clock := Clock())
    fake.volume[f"{VOL}/big.pdf"] = StoredFile(b"x" * 100 * 1024)
    fake.trickle = (per_read, seconds, True)
    started, budget = clock.now, 5.0
    kit_result = _read(fake, clock, f"{VOL}/big.pdf", budget)
    assert not kit_result.successful and kit_result.error_code == "timeout"
    assert clock.now - started <= budget
    _every_request_ends_by(fake, started, budget)


def test_each_socket_read_is_sent_with_what_is_left_of_the_budget(monkeypatch):
    import aisquare_pipe_databricks.client as client_mod

    bodies = []
    real = client_mod.DatabricksClient._body

    def keep(self, resp):
        bodies.append(resp)
        return real(self, resp)

    monkeypatch.setattr(client_mod.DatabricksClient, "_body", keep)
    fake = FakeDatabricks(clock := Clock())
    fake.volume[f"{VOL}/big.pdf"] = StoredFile(b"x" * 100 * 1024)
    fake.trickle = (1024, 0.5, True)
    started = clock.now
    _read(fake, clock, f"{VOL}/big.pdf", 5.0)
    (body,) = bodies
    assert len(body.sock.timeouts) >= 9
    for at, timeout in body.sock.timeouts:
        assert MIN_REQUEST_TIMEOUT <= timeout and at - started + timeout <= 5.0 + 1e-9


def test_without_read1_the_deadline_is_checked_between_chunks():
    """urllib3 1.26 has no read1: the download checks the deadline between
    requests' chunks instead (each chunk here one read of 0.5 s)."""
    fake = FakeDatabricks(clock := Clock())
    fake.volume[f"{VOL}/big.pdf"] = StoredFile(b"x" * 100 * 1024)
    fake.trickle = (1024, 0.5, False)
    started = clock.now
    result = _read(fake, clock, f"{VOL}/big.pdf", 5.0)
    assert not result.successful and result.error_code == "timeout"
    assert clock.now - started <= 5.0


@pytest.mark.parametrize("read1", [True, False])
def test_a_download_within_the_budget_returns_the_whole_file(read1):
    fake = FakeDatabricks(clock := Clock())
    data = bytes(range(256)) * 40
    fake.volume[f"{VOL}/ok.bin"] = StoredFile(data)
    fake.trickle = (1024, 0.1, read1)
    result = _read(fake, clock, f"{VOL}/ok.bin", 5.0)
    assert result.successful, result.error
    assert result.files[0].data == data


def test_no_budget_leaves_the_per_request_timeout_alone():
    fake = FakeDatabricks(clock := Clock())
    fake.dbfs["/mnt/policies/big.bin"] = StoredFile(b"x" * 80)
    fake.dbfs_read_seconds = 1.0
    kit = DatabricksToolkit(session=fake, clock=clock, sleep=clock.sleep)
    result = kit.call_tool(
        "DATABRICKS_READ_FILE",
        {"path": "dbfs:/mnt/policies/big.bin"},
        pat_blob(dbfs=True),
    )
    assert result.successful, result.error
    assert {r["timeout"] for r in fake.requests} == {30.0}
