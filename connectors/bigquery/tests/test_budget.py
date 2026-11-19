"""The host's time budget and the query job's deadline, on a fake clock."""

from __future__ import annotations

import time

import pytest
from aisquare.pipe.core.envelope import PullParams
from aisquare.pipe.errors import PipelineError
from aisquare.pipe.testing.host import host_answer

from aisquare_pipe_bigquery import (
    BigQueryQuerySource,
    BigQueryToolkit,
)
from aisquare_pipe_bigquery import client as client_module
from tests.fakes import TOKEN_URL, CannedQuery, FakeGoogle, blob, field_


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


def _google(clock: Clock, step: float, *, polls: int = 0) -> FakeGoogle:
    """Every request takes ``step`` seconds of the fake clock."""
    google = FakeGoogle()
    google.add_dataset("acme-analytics.policies")
    google.add_query(
        "SELECT 1", CannedQuery([field_("x", "INT64")], [[1]], polls=polls)
    )

    def tick(url: str) -> None:
        clock.now += step

    google.on_request = tick
    return google


def _select(google: FakeGoogle, budget: float | None):
    config = blob() if budget is None else {**blob(), "timeout_seconds": budget}
    return BigQueryToolkit(session=google).call_tool(
        "BIGQUERY_RUN_SELECT", {"sql": "SELECT 1"}, config
    )


def test_each_request_gets_the_time_remaining_token_post_included(clock):
    google = _google(clock, step=2.0)
    result = _select(google, budget=20)
    assert result.successful, result.error
    timeouts = [r["timeout"] for r in google.requests]
    # token, dry run, run: 20 - 0, 20 - 2, 20 - 4 seconds left
    assert [r["url"] == TOKEN_URL for r in google.requests] == [True, False, False]
    assert timeouts == [20.0, 18.0, 16.0]


def test_the_job_deadline_is_the_budget_left_when_the_job_starts(clock):
    google = _google(clock, step=2.0)
    assert _select(google, budget=20).successful
    (run,) = google.runs()
    assert run["jobTimeoutMs"] == "16000"
    assert run["timeoutMs"] <= 10_000  # the server-side wait fits inside it


def test_without_a_budget_the_job_gets_the_default_deadline(clock):
    google = _google(clock, step=0.0)
    assert _select(google, budget=None).successful
    assert google.runs()[0]["jobTimeoutMs"] == "60000"
    assert all(r["timeout"] == 30.0 for r in google.requests)


def test_the_call_fails_timed_out_when_no_time_is_left(clock):
    google = _google(clock, step=11.0)
    result = _select(google, budget=20)
    assert not result.successful and result.error_code == "timeout"
    assert "time budget" in (result.error or "")
    # token (11 s) and dry run (22 s): no time left to start the job
    assert len(google.requests) == 2 and google.runs() == []


def test_polling_stops_at_the_job_deadline(clock):
    google = _google(clock, step=3.0, polls=1000)
    result = _select(google, budget=30)
    assert not result.successful and result.error_code == "timeout", result.error
    polls = [r for r in google.api_calls() if "/queries/job_1" in r["url"]]
    assert 1 <= len(polls) <= 8
    assert clock.now - 1000.0 <= 30 + 3.0


def test_an_answer_that_comes_back_fast_and_incomplete_is_not_polled_in_a_spin(clock):
    google = _google(clock, step=0.0, polls=3)
    result = _select(google, budget=None)
    assert result.successful, result.error
    assert clock.sleeps and all(0 < s <= 1.0 for s in clock.sleeps)


def test_the_source_job_deadline_bounds_its_polling(clock):
    google = _google(clock, step=4.0, polls=1000)
    with pytest.raises(PipelineError) as raised:
        list(
            BigQueryQuerySource(session=google).pull(
                blob(), PullParams({"sql": "SELECT 1", "job_timeout_seconds": 20})
            )
        )
    # A timeout is upstream (502 in pipe-as-service), not the caller's fault.
    assert host_answer(raised.value).status == 502
    assert "did not finish within 20s" in str(raised.value)
    assert google.runs()[0]["jobTimeoutMs"] == "20000"
    # the source has no host budget: each request gets the full 30 s
    assert all(r["timeout"] == 30.0 for r in google.requests)
