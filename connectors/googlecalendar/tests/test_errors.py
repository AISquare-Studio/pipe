"""Failure contract: every failure class carries core's canonical
``error_code`` (and ``retry_after`` for rate limits); the host's
``config["timeout_seconds"]`` bounds a whole call; concurrent calls share no
HTTP session; no token or argument value leaks into an error or a log."""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from unittest import mock

import pytest
import requests
from aisquare.pipe import ERROR_CODES, time_budget

from aisquare_pipe_googlecalendar import GoogleCalendarToolkit, rest
from aisquare_pipe_googlecalendar.errors import (
    ERROR_INSUFFICIENT_SCOPE,
    describe_error,
    error_code,
)
from aisquare_pipe_googlecalendar.rest import (
    GoogleCalendarAPIError,
    GoogleCalendarRestClient,
)

from .fakes import (
    CONFIG,
    TOKEN,
    FakeSession,
    google_error,
    harness,
    rate_limited,
    response,
    scope_denied,
)

GET = ("GOOGLECALENDAR_GET_CALENDAR", {})
POST = ("GOOGLECALENDAR_QUICK_ADD", {"text": "Lunch at 1"})

# (id, tool call, scripted answers, error_code, retry_after)
FAILURES = [
    ("401", GET, [google_error(401, "Invalid Credentials", "authError")], "unauthorized", None),
    ("429, retries exhausted", GET, rate_limited(4), "rate_limited", 8.0),
    ("403 userRateLimitExceeded, retries exhausted", GET, [google_error(403, "User-rate limit exceeded", "userRateLimitExceeded")] * 4, "rate_limited", 8.0),
    ("429 on a POST, retries exhausted", POST, rate_limited(4), "rate_limited", 8.0),
    ("429 Retry-After beyond the cap", GET, rate_limited(1, retry_after="120"), "rate_limited", 120.0),
    ("403 Retry-After, retries exhausted", GET, rate_limited(4, status=403, retry_after="5"), "rate_limited", 5.0),
    ("403 scope (all signals)", POST, [scope_denied()], ERROR_INSUFFICIENT_SCOPE, None),
    (
        "403 scope (reason only)",
        POST,
        [google_error(403, "Insufficient Permission", "insufficientPermissions")],
        ERROR_INSUFFICIENT_SCOPE,
        None,
    ),
    (
        "403 scope (WWW-Authenticate only)",
        POST,
        [response(403, {"error": {"code": 403, "message": "Forbidden"}}, headers={"WWW-Authenticate": 'Bearer error="insufficient_scope"'})],
        ERROR_INSUFFICIENT_SCOPE,
        None,
    ),
    ("403 no write access", POST, [google_error(403, "You need to have writer access to this calendar.", "requiredAccessLevel")], "forbidden", None),
    ("403 daily limit", GET, [google_error(403, "Daily Limit Exceeded", "dailyLimitExceeded")], "forbidden", None),
    ("404", POST, [google_error(404, "Requested entity was not found.", "notFound")], "not_found", None),
    ("400", POST, [google_error(400, "Invalid id value", "invalidArgument")], "invalid_arguments", None),
    ("400 empty range", GET, [google_error(400, "The specified time range is empty.", "timeRangeEmpty")], "invalid_arguments", None),
    ("410 gone", POST, [google_error(410, "Resource has been deleted", "deleted")], "not_found", None),
    ("412", POST, [google_error(412, "Precondition Failed", "conditionNotMet")], "conflict", None),
    ("409", POST, [google_error(409, "Conflict", "conflict")], "conflict", None),
    ("500 on a POST", POST, [google_error(500, "Internal Error", "backendError")], "upstream_error", None),
    ("503 x4 on a GET", GET, [google_error(503, "Backend Error", "backendError")] * 4, "upstream_error", None),
    ("504", POST, [response(504, content=b"")], "timeout", None),
    ("non-JSON 502 page", POST, [response(502, content=b"<html>Bad Gateway</html>", content_type="text/html")], "upstream_error", None),
    ("ReadTimeout", GET, [requests.ReadTimeout("read timed out")], "timeout", None),
    ("ConnectTimeout", GET, [requests.ConnectTimeout("connect timed out")], "timeout", None),
    ("ConnectionError", GET, [requests.ConnectionError("refused")], "upstream_error", None),
    ("200 that is not JSON", GET, [response(200, content=b"<html>", content_type="text/html")], "upstream_error", None),
]


@pytest.mark.parametrize(
    ("call", "answers", "code", "retry_after"),
    [f[1:] for f in FAILURES],
    ids=[f[0] for f in FAILURES],
)
def test_error_code_per_failure_class(call, answers, code, retry_after):
    slug, args = call
    h = harness(*answers)
    result = h.call(slug, args)
    assert result.successful is False
    assert result.error_code == code
    assert result.error_code in ERROR_CODES
    assert result.retry_after == retry_after
    assert TOKEN not in result.error
    assert result.to_dict()["error_code"] == code


def test_scope_code_is_cores():
    assert ERROR_INSUFFICIENT_SCOPE == "insufficient_scope"


def test_get_5xx_is_retried_then_succeeds():
    h = harness(google_error(503, "Backend Error", "backendError"), response(200, {"id": "me@x.co"}))
    assert h.call(*GET).successful
    assert h.sleeps == [1.0]


def test_rate_limit_backoff_doubles():
    h = harness(*rate_limited(3), response(200, {}))
    assert h.call(*GET).successful
    assert h.sleeps == [1.0, 2.0, 4.0]


@pytest.mark.parametrize(
    ("error", "code"),
    [
        (GoogleCalendarAPIError(429, "x", retry_after=1), "rate_limited"),
        (GoogleCalendarAPIError(403, "x", reason="userRateLimitExceeded", retry_after=1), "rate_limited"),
        (GoogleCalendarAPIError(403, "x", reason="insufficientPermissions", insufficient_scope=True), ERROR_INSUFFICIENT_SCOPE),
        (GoogleCalendarAPIError(403, "x", reason="forbidden"), "forbidden"),
        (GoogleCalendarAPIError(0, "x", timed_out=True), "timeout"),
        (GoogleCalendarAPIError(0, "x"), "upstream_error"),
        (GoogleCalendarAPIError(410, "Gone"), "not_found"),
        (GoogleCalendarAPIError(400, "x"), "invalid_arguments"),
        (GoogleCalendarAPIError(401, "x"), "unauthorized"),
    ],
)
def test_error_code_mapping(error, code):
    assert error_code(error) == code


@pytest.mark.parametrize(
    ("error", "scopes", "expected"),
    [
        (GoogleCalendarAPIError(0, "network error talking to Google Calendar (ConnectionError)"), (), "network error talking to Google Calendar (ConnectionError)"),
        (GoogleCalendarAPIError(401, "Invalid Credentials"), (), "401: Invalid Credentials — Google rejected this connection's access token (expired or revoked); refresh it, or the Google Calendar connection must be reconnected"),
        (GoogleCalendarAPIError(429, "Slow down", retry_after=2.2), (), "429: Slow down — Google Calendar rate limit; retry in about 3s"),
        (
            GoogleCalendarAPIError(403, "Request had insufficient authentication scopes.", insufficient_scope=True),
            ("https://www.googleapis.com/auth/calendar.events",),
            "403: Request had insufficient authentication scopes. — this Google Calendar connection was not granted the access this tool needs (calendar.events); reconnect Google Calendar and approve it",
        ),
        (GoogleCalendarAPIError(404, "Not Found"), (), "404: Not Found — it does not exist, or this Google Calendar connection cannot see it"),
        (GoogleCalendarAPIError(400, "Bad"), (), "400: Bad"),
    ],
)
def test_describe_error(error, scopes, expected):
    assert describe_error(error, scopes) == expected


# --------------------------------------------------------------------------
# timeout_seconds: the whole call's budget
# --------------------------------------------------------------------------


class FakeClock:
    """Monotonic time that only moves when the client sleeps."""

    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def _budgeted(budget, *answers, **kwargs):
    clock = FakeClock()
    session = FakeSession(list(answers))
    client = GoogleCalendarRestClient(
        TOKEN, session=session, budget=budget, sleep=clock.sleep, monotonic=clock, **kwargs  # type: ignore[arg-type]
    )
    return client, session, clock


class TestTimeBudget:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [(5, 5.0), (2.5, 2.5), (None, None), (0, None), (-1, None), (True, None), ("10", None), (float("nan"), None)],
    )
    def test_budget_from_config(self, value, expected):
        assert time_budget({**CONFIG, "timeout_seconds": value}) == expected

    @pytest.mark.parametrize(("budget", "timeout"), [(5, 5.0), (100, rest.DEFAULT_TIMEOUT), (None, rest.DEFAULT_TIMEOUT)])
    def test_request_timeout_is_capped_by_the_budget(self, budget, timeout):
        h = harness(response(200, {}))
        config = {**CONFIG, "timeout_seconds": budget} if budget is not None else CONFIG
        h.call(*GET, config=config)
        assert h.session.last.timeout == pytest.approx(timeout, abs=0.5)

    def test_remaining_budget_shrinks_each_attempt(self):
        client, session, clock = _budgeted(5, *rate_limited(1, retry_after="3"), response(200, {}))
        client.request("GET", "/calendars/primary")
        assert clock.sleeps == [3.0]
        assert [c.timeout for c in session.calls] == [5.0, 2.0]

    def test_a_retry_that_would_outlast_the_budget_fails_now(self):
        client, session, clock = _budgeted(5, *rate_limited(2, retry_after="3"))
        with pytest.raises(GoogleCalendarAPIError) as exc:
            client.request("GET", "/calendars/primary")
        assert exc.value.retry_after == 3.0
        assert clock.sleeps == [3.0]
        assert len(session.calls) == 2

    def test_spent_budget_times_out_without_a_request(self):
        client, session, clock = _budgeted(5)
        clock.now = 5.0
        with pytest.raises(GoogleCalendarAPIError) as exc:
            client.request("GET", "/calendars/primary")
        assert exc.value.timed_out
        assert session.calls == []

    def test_budget_spent_between_calendars_times_out(self):
        """EVENTS_LIST_ALL_CALENDARS reads each calendar separately; a budget
        that runs out part-way fails the call as ``timeout`` instead of
        overrunning."""
        from aisquare_pipe_googlecalendar.errors import failure
        from aisquare_pipe_googlecalendar.tools import TOOLS

        clock = FakeClock()

        class SlowSession(FakeSession):
            def request(self, method, url, **kwargs):
                clock.now += 3  # every answer takes 3s
                return super().request(method, url, **kwargs)

        calendars = {"items": [{"id": "a@x.co", "primary": True, "timeZone": "UTC"}, {"id": "b@x.co"}]}
        session = SlowSession([response(200, calendars), response(200, {"items": []})])
        client = GoogleCalendarRestClient(TOKEN, session=session, budget=5, sleep=clock.sleep, monotonic=clock)  # type: ignore[arg-type]
        args = {"time_min": "2026-11-02T00:00:00Z", "time_max": "2026-11-03T00:00:00Z"}
        with pytest.raises(GoogleCalendarAPIError) as exc:
            TOOLS["GOOGLECALENDAR_EVENTS_LIST_ALL_CALENDARS"].handler(client, args)
        assert exc.value.timed_out
        assert len(session.calls) == 2  # the third request never went out
        assert failure(exc.value).error_code == "timeout"

    def test_request_timeout_names_the_seconds_not_the_url(self):
        h = harness(requests.ReadTimeout("HTTPSConnectionPool: /calendar/v3/calendars/primary/events?q=secret-project"))
        result = h.call("GOOGLECALENDAR_EVENTS_LIST", {"query": "secret-project"}, config={**CONFIG, "timeout_seconds": 7})
        assert result.error_code == "timeout"
        assert result.error == "Google Calendar did not answer within 7s (ReadTimeout)"


# --------------------------------------------------------------------------
# Concurrency and secrecy
# --------------------------------------------------------------------------


class TestConcurrency:
    def test_each_call_gets_its_own_session_and_closes_it(self):
        made: list[FakeSession] = []
        lock = threading.Lock()

        def new_session():
            session = FakeSession([response(200, {"id": "me@x.co"})])
            with lock:
                made.append(session)
            return session

        toolkit = GoogleCalendarToolkit()
        with (
            mock.patch.object(rest.requests, "Session", side_effect=new_session),
            ThreadPoolExecutor(max_workers=8) as pool,
        ):
            results = list(pool.map(lambda _: toolkit.call_tool(*GET, CONFIG), range(16)))
        assert all(r.successful for r in results)
        assert len(made) == 16
        assert all(s.closed and len(s.calls) == 1 for s in made)

    def test_a_passed_session_is_not_closed(self):
        h = harness(response(200, {}))
        h.call(*GET)
        assert h.session.closed is False


class TestSecrecy:
    def test_token_echoed_in_an_error_is_scrubbed(self):
        h = harness(google_error(400, f"bad token {TOKEN}", "invalid"))
        result = h.call(*GET)
        assert TOKEN not in result.error

    def test_network_error_carries_no_url_or_arguments(self):
        h = harness(requests.ConnectionError("HTTPSConnectionPool(host='www.googleapis.com'): /calendars/primary/events?q=board+meeting"))
        result = h.call("GOOGLECALENDAR_EVENTS_LIST", {"query": "board meeting"})
        assert result.error == "network error talking to Google Calendar (ConnectionError)"

    def test_logs_carry_no_token_or_arguments(self, caplog):
        caplog.set_level("DEBUG")
        h = harness(*rate_limited(1), response(200, {"items": []}))
        h.call("GOOGLECALENDAR_EVENTS_LIST", {"query": "secret.example offsite", "calendarId": "boss@secret.example"})
        assert TOKEN not in caplog.text
        assert "secret.example" not in caplog.text

    def test_client_repr_has_no_token(self):
        assert TOKEN not in repr(GoogleCalendarRestClient(TOKEN, session=FakeSession()))  # type: ignore[arg-type]

    def test_config_is_not_mutated(self):
        config = {"access_token": f"  {TOKEN}\n", "timeout_seconds": 5}
        before = dict(config)
        harness(response(200, {})).call(*GET, config=config)
        assert config == before


# --------------------------------------------------------------------------
# Hostile Retry-After values and error bodies
# --------------------------------------------------------------------------


@pytest.mark.parametrize("value", ["inf", "1e309", "Infinity", "-inf", "nan", "soon"])
def test_non_finite_retry_after_falls_back_to_backoff(value):
    h = harness(*rate_limited(4, retry_after=value))
    result = h.call(*GET)
    assert (result.error_code, result.retry_after) == ("rate_limited", 8.0)
    assert h.sleeps == [1.0, 2.0, 4.0]


DEEP = b"[" * 200_000 + b"]" * 200_000


@pytest.mark.parametrize(("status", "code"), [(401, "unauthorized"), (404, "not_found"), (403, "forbidden"), (500, "upstream_error")])
def test_deeply_nested_error_body_keeps_its_status(status, code):
    h = harness(response(status, content=DEEP, content_type="application/json"))
    result = h.call(*POST)
    assert result.error_code == code
    assert result.data == {"status": status}


def test_deeply_nested_success_body_is_a_clean_upstream_error():
    result = harness(response(200, content=DEEP, content_type="application/json")).call(*GET)
    assert result.error_code == "upstream_error"
    assert "not JSON" in result.error
