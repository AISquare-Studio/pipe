"""The host's time budget and the statement deadline, on a fake clock."""

from __future__ import annotations

import time

import pytest
from aisquare.pipe.core.envelope import PullParams
from aisquare.pipe.errors import PipelineError
from aisquare.pipe.testing.host import host_answer

from aisquare_pipe_snowflake import (
    SnowflakeQuerySource,
    SnowflakeToolkit,
)
from aisquare_pipe_snowflake import client as client_module
from tests.fakes import (
    TOKEN_URL,
    Canned,
    FakeResponse,
    FakeSnowflake,
    blob,
    col,
    oauth_blob,
)


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


@pytest.fixture
def clock(monkeypatch) -> Clock:
    clock = Clock()
    monkeypatch.setattr(client_module, "_monotonic", clock)
    monkeypatch.setattr(time, "sleep", clock.sleep)
    return clock


def _fake(clock: Clock, step: float, **canned) -> FakeSnowflake:
    """Every request takes ``step`` seconds of the fake clock."""
    fake = FakeSnowflake()
    fake.add("SELECT 1", Canned([col("X", "fixed")], [["1"]], **canned))

    def tick(url: str) -> None:
        clock.now += step

    fake.on_request = tick
    return fake


def _select(fake: FakeSnowflake, budget: float | None, config=None, **arguments):
    config = dict(config or blob())
    if budget is not None:
        config["timeout_seconds"] = budget
    return SnowflakeToolkit(session=fake).call_tool(
        "SNOWFLAKE_RUN_SELECT", {"statement": "SELECT 1", **arguments}, config
    )


def test_each_request_gets_the_time_remaining_token_post_included(clock):
    fake = _fake(clock, 2.0, polls=1)
    result = _select(fake, 10.0, config=oauth_blob())
    assert result.successful, result.error
    timeouts = [r["timeout"] for r in fake.requests]
    assert fake.requests[0]["url"] == TOKEN_URL
    # token at t=0 (10 s left), submit at 2, poll at 4, poll at 6 (+0.25 s pause)
    assert timeouts == [10.0, 8.0, 6.0, pytest.approx(3.75)]


def test_the_statement_timeout_is_the_budget_left_when_it_is_submitted(clock):
    fake = _fake(clock, 0.0)
    assert _select(fake, 25.0).successful
    assert fake.submits()[0]["timeout"] == 25
    fake2 = _fake(clock, 0.0)
    assert _select(fake2, 25.0, timeout=10).successful
    assert fake2.submits()[0]["timeout"] == 10  # the caller may ask for less


def test_without_a_budget_the_statement_gets_the_default_deadline(clock):
    fake = _fake(clock, 0.0)
    assert _select(fake, None).successful
    assert fake.submits()[0]["timeout"] == 60
    fake2 = _fake(clock, 0.0)
    assert _select(fake2, None, timeout=600).successful
    assert fake2.submits()[0]["timeout"] == 600


def test_the_call_fails_timed_out_when_no_time_is_left(clock):
    fake = _fake(clock, 0.0)
    result = _select(fake, 0.5)
    assert not result.successful and result.error_code == "timeout", result.error
    assert fake.requests == []  # under a second: not even submitted


def test_polling_stops_at_the_deadline_and_cancels_the_statement(clock):
    fake = _fake(clock, 0.1, endless=True)
    result = _select(fake, 30.0, timeout=5)
    assert not result.successful and result.error_code == "timeout", result.error
    assert "cancelled" in result.error
    assert fake.cancelled == ["01bc0000-0000-0000-0000-000000000001"]
    cancel = fake.requests[-1]
    assert cancel["method"] == "POST" and cancel["url"].endswith("/cancel")
    assert clock.now - 1000.0 < 6.5  # stopped at the 5 s deadline, not the budget
    assert max(clock.sleeps) <= 2.0


def test_polling_stops_at_the_budget_when_it_is_shorter(clock):
    fake = _fake(clock, 0.1, endless=True)
    result = _select(fake, 4.0)
    assert not result.successful and result.error_code == "timeout"
    assert clock.now - 1000.0 <= 4.0 + 0.2


def test_a_poll_answered_429_waits_and_polls_again(clock):
    fake = _fake(clock, 0.1)
    fake.override("poll", FakeResponse(429, headers={"Retry-After": "3"}))
    result = _select(fake, 30.0)
    assert result.successful, result.error
    assert 3.0 in clock.sleeps


def test_snowflake_answering_429_until_the_deadline_is_rate_limited(clock):
    fake = _fake(clock, 0.1)
    fake.override(
        "poll", *[FakeResponse(429, headers={"Retry-After": "2"}) for _ in range(50)]
    )
    result = _select(fake, 30.0, timeout=5)
    assert not result.successful and result.error_code == "rate_limited", result.error
    assert result.retry_after == 2


def test_the_source_statement_deadline_bounds_its_polling(clock):
    fake = _fake(clock, 0.1, endless=True)
    with pytest.raises(PipelineError) as raised:
        list(
            SnowflakeQuerySource(session=fake).pull(
                blob(), PullParams({"sql": "SELECT 1", "timeout_seconds": 7})
            )
        )
    # A timeout is upstream (502 in pipe-as-service), not the caller's fault.
    assert host_answer(raised.value).status == 502
    assert "did not finish within its 7s deadline" in str(raised.value)
    submitted = [b for b in fake.submits() if b["statement"] == "SELECT 1"]
    assert submitted[0]["timeout"] == 7
    assert clock.now - 1000.0 < 8.5
