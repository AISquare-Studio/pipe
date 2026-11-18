"""The host's time budget (``config["timeout_seconds"]``) bounds the WHOLE
call: every request, the token POST included, gets the time remaining as its
timeout, and a call with none left fails ``timed_out`` without sending more.
Time is a fake clock that the fake Google moves on each request."""

from __future__ import annotations

import pytest
from aisquare.pipe.core.envelope import PullParams

from aisquare_pipe_gcs import GcsSource, GcsToolkit
from aisquare_pipe_gcs import client as client_module
from aisquare_pipe_gcs.client import DEFAULT_TIMEOUT, GcsAPIError, GcsClient
from aisquare_pipe_gcs.credentials import load_credentials
from tests.fakes import TOKEN_URL, FakeGoogle, StoredObject, blob

TEXT = b"Travel policy: economy class.\n"


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> Clock:
    fake = Clock()
    monkeypatch.setattr(client_module, "_monotonic", fake)
    return fake


def _google(clock: Clock, per_request: float) -> FakeGoogle:
    google_ = FakeGoogle(
        objects={"policies/travel.txt": StoredObject(TEXT, "text/plain")}
    )
    google_.on_request = lambda _url: clock.advance(per_request)
    return google_


def _get(google_: FakeGoogle, budget: float, **arguments):
    return GcsToolkit(session=google_).call_tool(
        "GCS_GET_OBJECT",
        {"name": "policies/travel.txt", **arguments},
        {**blob(), "timeout_seconds": budget},
    )


def _timeouts(google_: FakeGoogle) -> list[tuple[str, float]]:
    return [
        (
            "token" if r["url"] == TOKEN_URL else r["params"].get("alt", "metadata"),
            r["timeout"],
        )
        for r in google_.requests
    ]


def test_each_request_gets_the_time_remaining_token_post_included(clock):
    """GCS_GET_OBJECT is three requests (token POST, metadata, download);
    each one's timeout is what the earlier ones left of the 10 s budget."""
    google_ = _google(clock, per_request=3)
    result = _get(google_, 10)
    assert result.successful, result.error
    assert _timeouts(google_) == [("token", 10), ("metadata", 7), ("media", 4)]


def test_the_call_fails_timed_out_when_no_time_is_left(clock):
    """The token POST and the metadata read use up the 5 s budget: the
    download is never sent, and the tool answers timeout."""
    google_ = _google(clock, per_request=3)
    result = _get(google_, 5)
    assert not result.successful and result.error_code == "timeout"
    assert "5s time budget" in (result.error or "")
    assert [kind for kind, _ in _timeouts(google_)] == ["token", "metadata"]


def test_a_budget_spent_on_the_token_post_sends_nothing_else(clock):
    google_ = _google(clock, per_request=4)
    result = _get(google_, 4)
    assert not result.successful and result.error_code == "timeout"
    assert _timeouts(google_) == [("token", 4)]
    assert google_.storage_calls() == []


def test_the_retry_after_a_rejected_cached_token_shares_the_budget(clock):
    """A cached token's 401 adds a token POST and a retry (5 requests): they
    get what is left, not a fresh budget each."""
    google_ = _google(clock, per_request=0)
    assert _get(google_, 30).successful  # caches a token
    google_.requests.clear()
    google_.revoke_tokens()
    google_.on_request = lambda _url: clock.advance(2)
    result = _get(google_, 9)
    assert result.successful, result.error
    assert _timeouts(google_) == [
        ("metadata", 9),  # the cached token: 401
        ("token", 7),
        ("metadata", 5),
        ("media", 3),
    ]


def test_the_retry_is_not_sent_when_the_budget_is_spent(clock):
    google_ = _google(clock, per_request=0)
    assert _get(google_, 30).successful
    google_.requests.clear()
    google_.revoke_tokens()
    google_.on_request = lambda _url: clock.advance(3)
    result = _get(google_, 6)
    assert not result.successful and result.error_code == "timeout"
    assert [kind for kind, _ in _timeouts(google_)] == ["metadata", "token"]


def test_a_slow_download_stops_at_the_deadline(clock):
    """requests' timeout bounds each socket read, not the body: the client
    checks the deadline between chunks."""
    google_ = FakeGoogle(
        objects={"policies/big.bin": StoredObject(b"x" * (3 * 64 * 1024 + 1))}
    )
    google_.on_chunk = lambda: clock.advance(4)
    result = GcsToolkit(session=google_).call_tool(
        "GCS_GET_OBJECT",
        {"name": "policies/big.bin"},
        {**blob(), "timeout_seconds": 10},
    )
    assert not result.successful and result.error_code == "timeout"


def test_the_request_timeout_caps_each_request_inside_a_long_budget(clock):
    google_ = _google(clock, per_request=1)
    assert _get(google_, 600).successful
    assert {t for _, t in _timeouts(google_)} == {DEFAULT_TIMEOUT}


@pytest.mark.parametrize("budget", [None, 0, -1])
def test_no_budget_means_the_per_request_timeout_only(clock, budget):
    google_ = _google(clock, per_request=1000)
    with GcsClient(load_credentials(blob()), session=google_, budget=budget) as client:
        client.get_metadata("policies/travel.txt")
    assert {t for _, t in _timeouts(google_)} == {DEFAULT_TIMEOUT}


def test_the_budget_runs_from_the_clients_creation(clock):
    """A client whose budget passed before its first request sends nothing."""
    google_ = _google(clock, per_request=0)
    with GcsClient(load_credentials(blob()), session=google_, budget=2) as client:
        clock.advance(2)
        with pytest.raises(GcsAPIError) as caught:
            client.list_objects(prefix="policies/", page_size=10)
    assert caught.value.timed_out
    assert google_.requests == []


def test_the_source_has_no_budget(clock):
    """gcs-source runs without a host budget: each request gets the
    per-request timeout."""
    google_ = _google(clock, per_request=50)
    envelopes = list(GcsSource(session=google_).pull(blob(), PullParams(params={})))
    assert len(envelopes) == 1
    assert {t for _, t in _timeouts(google_)} == {DEFAULT_TIMEOUT}
