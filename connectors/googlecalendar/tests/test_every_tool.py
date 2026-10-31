"""The failure contract, tool by tool: every tool in the catalog is run
against a successful answer, a 401, a missing-scope 403, a 429 and a host
time budget, and is refused before any request when its arguments are bad.

``CASES`` holds one valid call per tool with the answers Calendar would
give; ``test_cases_cover_the_catalog`` keeps it in step with the catalog."""

from __future__ import annotations

import pytest
from aisquare.pipe import ERROR_CODES

from aisquare_pipe_googlecalendar import REQUIRED_SCOPES
from aisquare_pipe_googlecalendar.errors import ERROR_INSUFFICIENT_SCOPE
from aisquare_pipe_googlecalendar.tools import CATALOG

from .fakes import (
    CONFIG,
    TOKEN,
    calendar_entry,
    event,
    events_page,
    harness,
    rate_limited,
    response,
    scope_denied,
    unauthorized,
    zone_reply,
)

DAY = {"min": "2026-11-02T00:00:00Z", "max": "2026-11-03T00:00:00Z"}
FREEBUSY = {"timeMin": DAY["min"], "timeMax": DAY["max"], "calendars": {"primary": {"busy": []}}}
AT_NINE = "2026-11-02T09:00:00-05:00"

# slug -> (arguments, answers for a successful call, (method, path) of the first request)
CASES: dict[str, tuple[dict, list, tuple[str, str]]] = {
    "GOOGLECALENDAR_LIST_CALENDARS": (
        {},
        [response(200, {"items": [calendar_entry("me@example.com", "Me", primary=True)]})],
        ("GET", "/users/me/calendarList"),
    ),
    "GOOGLECALENDAR_GET_CALENDAR": (
        {},
        [response(200, {"id": "me@example.com", "summary": "Me", "timeZone": "America/New_York"})],
        ("GET", "/calendars/primary"),
    ),
    "GOOGLECALENDAR_GET_CURRENT_DATE_TIME": ({}, [zone_reply()], ("GET", "/calendars/primary")),
    "GOOGLECALENDAR_EVENTS_LIST": (
        {"timeMin": DAY["min"], "timeMax": DAY["max"]},
        [response(200, events_page(event()))],
        ("GET", "/calendars/primary/events"),
    ),
    "GOOGLECALENDAR_FIND_EVENT": (
        {"query": "standup", "time_min": DAY["min"]},
        [response(200, events_page(event()))],
        ("GET", "/calendars/primary/events"),
    ),
    "GOOGLECALENDAR_EVENTS_GET": ({"event_id": "evt123"}, [response(200, event())], ("GET", "/calendars/primary/events/evt123")),
    "GOOGLECALENDAR_EVENTS_INSTANCES": (
        {"eventId": "evt123"},
        [response(200, events_page(event("evt123_20261102T140000Z", recurringEventId="evt123")))],
        ("GET", "/calendars/primary/events/evt123/instances"),
    ),
    "GOOGLECALENDAR_EVENTS_LIST_ALL_CALENDARS": (
        {"time_min": DAY["min"], "time_max": DAY["max"]},
        [
            response(200, {"items": [calendar_entry("me@example.com", "Me", primary=True)]}),
            response(200, events_page(event())),
        ],
        ("GET", "/users/me/calendarList"),
    ),
    "GOOGLECALENDAR_FREE_BUSY_QUERY": (
        {"items": ["primary"], "timeMin": DAY["min"], "timeMax": DAY["max"], "timeZone": "UTC"},
        [response(200, FREEBUSY)],
        ("POST", "/freeBusy"),
    ),
    "GOOGLECALENDAR_FIND_FREE_SLOTS": (
        {"time_min": DAY["min"], "time_max": DAY["max"], "timezone": "UTC"},
        [response(200, FREEBUSY)],
        ("POST", "/freeBusy"),
    ),
    "GOOGLECALENDAR_CREATE_EVENT": (
        {"summary": "Standup", "start_datetime": AT_NINE, "timezone": "America/New_York"},
        [response(200, event())],
        ("POST", "/calendars/primary/events"),
    ),
    "GOOGLECALENDAR_UPDATE_EVENT": (
        {"event_id": "evt123", "summary": "Daily standup"},
        [response(200, event()), response(200, event(summary="Daily standup"))],
        ("GET", "/calendars/primary/events/evt123"),
    ),
    "GOOGLECALENDAR_QUICK_ADD": (
        {"text": "Lunch with Ann tomorrow 1pm"},
        [response(200, event(summary="Lunch with Ann"))],
        ("POST", "/calendars/primary/events/quickAdd"),
    ),
    "GOOGLECALENDAR_DELETE_EVENT": ({"event_id": "evt123"}, [response(204)], ("DELETE", "/calendars/primary/events/evt123")),
}

SLUGS = sorted(CASES)
RANGE = {"time_min": DAY["min"], "time_max": DAY["max"]}

# slug -> bad argument sets (each refused as invalid_arguments, no request sent)
BAD_ARGUMENTS: dict[str, list[dict]] = {
    "GOOGLECALENDAR_LIST_CALENDARS": [{"max_results": "many"}, {"min_access_role": "admin"}, {"show_hidden": "yes"}],
    "GOOGLECALENDAR_GET_CALENDAR": [{"calendar_id": ".."}, {"calendar_id": 5}],
    "GOOGLECALENDAR_GET_CURRENT_DATE_TIME": [{"timezone": "Mars/Olympus"}, {"timezone": 20}, {"timezone": "../../etc/passwd"}, {"timezone": True}],
    "GOOGLECALENDAR_EVENTS_LIST": [
        {"timeMin": "tomorrow"},
        {"orderBy": "startTime", "singleEvents": False},
        {"syncToken": "s", "timeMin": DAY["min"]},
        {"timeMin": DAY["max"], "timeMax": DAY["min"]},
        {"timeZone": "Nowhere/City"},
        {"orderBy": "random"},
        {"calendarId": "."},
    ],
    "GOOGLECALENDAR_FIND_EVENT": [{"time_min": "2026-13-45"}, {"order_by": "startTime", "single_events": False}, {"max_results": 0.5}],
    "GOOGLECALENDAR_EVENTS_GET": [{}, {"event_id": ".."}, {"event_id": "e", "time_zone": "Bad/Zone"}],
    "GOOGLECALENDAR_EVENTS_INSTANCES": [{}, {"eventId": "e", "timeMin": "noon"}, {"eventId": "e", "timeMin": DAY["max"], "timeMax": DAY["min"]}],
    "GOOGLECALENDAR_EVENTS_LIST_ALL_CALENDARS": [
        {"time_min": DAY["min"]},
        {**RANGE, "response_detail": "verbose"},
        {**RANGE, "calendar_ids": [f"c{i}@example.com" for i in range(51)]},
        {**RANGE, "timezone": "Atlantis/Capital"},
    ],
    "GOOGLECALENDAR_FREE_BUSY_QUERY": [
        {"items": [], "timeMin": DAY["min"], "timeMax": DAY["max"], "timeZone": "UTC"},
        {"items": [""], "timeMin": DAY["min"], "timeMax": DAY["max"], "timeZone": "UTC"},
        {"items": ["primary"], "timeMin": DAY["min"]},
        {"items": ["primary"] * 51, "timeMin": DAY["min"], "timeMax": DAY["max"], "timeZone": "UTC"},
        {"items": ["primary"], "timeMin": DAY["max"], "timeMax": DAY["min"], "timeZone": "UTC"},
    ],
    "GOOGLECALENDAR_FIND_FREE_SLOTS": [
        {"timezone": "UTC", "time_min": DAY["max"], "time_max": DAY["min"]},
        {"items": [5], "timezone": "UTC"},
        {"timezone": "Not/AZone"},
    ],
    "GOOGLECALENDAR_CREATE_EVENT": [
        {"summary": "no start"},
        {"start_datetime": "2026-11-02T10:00:00-05:00", "end_datetime": AT_NINE},
        {"start_datetime": AT_NINE, "event_duration_minutes": 75},
        {"start_datetime": AT_NINE, "event_duration_hour": 0, "event_duration_minutes": 0},
        {"start_datetime": AT_NINE, "timezone": "Bad/Zone"},
        {"start_datetime": AT_NINE, "recurrence": ["WEEKLY"]},
        {"start_datetime": AT_NINE, "recurrence": ["RRULE:FREQ=DAILY\nEXDATE:x"]},
        {"start_datetime": AT_NINE, "attendees": ["not-an-email"]},
        {"start_date": "2026-11-05", "end_date": "2026-11-01"},
        {"start_date": "Nov 5"},
        {"start_datetime": AT_NINE, "send_updates": "everyone"},
        {"start_datetime": "next tuesday"},
    ],
    "GOOGLECALENDAR_UPDATE_EVENT": [
        {},
        {"event_id": "e"},
        {"event_id": "e", "start_datetime": "noon"},
        {"event_id": "e", "attendees": [{"name": "no email"}]},
        {"event_id": "..", "summary": "x"},
    ],
    "GOOGLECALENDAR_QUICK_ADD": [{}, {"text": "   "}, {"text": "x", "send_updates": "loud"}],
    "GOOGLECALENDAR_DELETE_EVENT": [{}, {"event_id": "."}, {"event_id": "e", "send_updates": "loud"}],
}


def test_cases_cover_the_catalog():
    slugs = {tool.spec.slug for tool in CATALOG}
    assert set(CASES) == slugs
    assert set(BAD_ARGUMENTS) == slugs


@pytest.mark.parametrize("slug", SLUGS)
def test_succeeds(slug):
    args, answers, (method, path) = CASES[slug]
    h = harness(*answers)
    result = h.call(slug, args)
    assert result.successful, result.error
    assert (h.session.calls[0].method, h.session.calls[0].path) == (method, path)
    assert h.session.responses == []
    for call in h.session.calls:
        assert call.headers["Authorization"] == f"Bearer {TOKEN}"
        assert TOKEN not in call.url


@pytest.mark.parametrize("slug", SLUGS)
def test_401_is_unauthorized(slug):
    args, _, _ = CASES[slug]
    h = harness(unauthorized())
    result = h.call(slug, args)
    assert (result.successful, result.error_code, result.retry_after) == (False, "unauthorized", None)
    assert "reconnect" in result.error
    assert len(h.session.calls) == 1


@pytest.mark.parametrize("slug", SLUGS)
def test_scope_403_is_insufficient_scope(slug):
    args, _, _ = CASES[slug]
    h = harness(scope_denied())
    result = h.call(slug, args)
    assert result.error_code == ERROR_INSUFFICIENT_SCOPE
    assert result.error_code in ERROR_CODES
    assert result.data == {
        "status": 403,
        "reason": "insufficientPermissions",
        "insufficient_scope": True,
        "required_scopes": list(REQUIRED_SCOPES[slug]),
    }
    assert REQUIRED_SCOPES[slug][-1].rsplit("/", 1)[-1] in result.error
    assert len(h.session.calls) == 1


@pytest.mark.parametrize("slug", SLUGS)
def test_429_is_rate_limited_with_retry_after(slug):
    args, _, _ = CASES[slug]
    h = harness(*rate_limited(1, retry_after="120"))
    result = h.call(slug, args)
    assert (result.error_code, result.retry_after) == ("rate_limited", 120.0)
    assert h.sleeps == []


@pytest.mark.parametrize("slug", SLUGS)
def test_429_is_retried_then_succeeds(slug):
    args, answers, _ = CASES[slug]
    h = harness(*rate_limited(1), *answers)
    result = h.call(slug, args)
    assert result.successful, result.error
    assert h.sleeps == [1.0]


@pytest.mark.parametrize("slug", SLUGS)
def test_budget_caps_every_request(slug):
    args, answers, _ = CASES[slug]
    h = harness(*answers)
    result = h.call(slug, args, config={**CONFIG, "timeout_seconds": 5})
    assert result.successful, result.error
    assert all(0 < call.timeout <= 5 for call in h.session.calls)


@pytest.mark.parametrize("slug", SLUGS)
def test_a_retry_that_would_outlast_the_budget_fails_now(slug):
    args, _, _ = CASES[slug]
    h = harness(*rate_limited(1, retry_after="3"))
    result = h.call(slug, args, config={**CONFIG, "timeout_seconds": 4})
    assert (result.error_code, result.retry_after) == ("rate_limited", 3.0)
    assert h.sleeps == []
    assert len(h.session.calls) == 1


@pytest.mark.parametrize(
    ("slug", "args"),
    [(slug, args) for slug in SLUGS for args in BAD_ARGUMENTS[slug]],
    ids=[f"{slug.removeprefix('GOOGLECALENDAR_')}-{i}" for slug in SLUGS for i, _ in enumerate(BAD_ARGUMENTS[slug])],
)
def test_bad_arguments_are_refused_before_any_request(slug, args):
    h = harness()
    result = h.call(slug, args)
    assert result.successful is False
    assert result.error_code == "invalid_arguments", result.error
    assert h.session.calls == []


@pytest.mark.parametrize("slug", SLUGS)
def test_no_credentials_means_no_request(slug):
    args, _, _ = CASES[slug]
    h = harness()
    assert h.call(slug, args, config={}).error_code == "unauthorized"
    assert h.session.calls == []
