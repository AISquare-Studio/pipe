"""``REQUIRED_SCOPES`` against what each tool really sends: every request a
tool can make (all its paths, the calendar-zone lookups included) must be
allowed by the scopes it declares, as Google documents per method, and each
declared scope must be needed by one of them. Core's ``granted_scopes``
pre-flight trusts these lists, so an understated one fails at Google and an
overstated one hides a tool that would have worked."""

from __future__ import annotations

import functools
import re

import pytest

from aisquare_pipe_googlecalendar import REQUIRED_SCOPES, GoogleCalendarToolkit
from aisquare_pipe_googlecalendar.tools import EVENTS_SCOPE, FULL_SCOPE, READONLY_SCOPE

from .fakes import (
    CONFIG,
    calendar_entry,
    event,
    events_page,
    harness,
    response,
    zone_reply,
)
from .test_every_tool import CASES

READ = frozenset({FULL_SCOPE, READONLY_SCOPE})
EVENTS_READ = frozenset({FULL_SCOPE, READONLY_SCOPE, EVENTS_SCOPE})
EVENTS_WRITE = frozenset({FULL_SCOPE, EVENTS_SCOPE})

#: Scopes Google accepts per Calendar method (developers.google.com/calendar/api/v3/reference),
#: limited to the ones this toolkit deals in. ``(method, path regex, scopes)``.
ENDPOINTS: list[tuple[str, str, frozenset[str]]] = [
    ("GET", r"/users/me/calendarList", READ),
    ("GET", r"/calendars/[^/]+", READ),  # calendars.get: not calendar.events
    ("GET", r"/calendars/[^/]+/events(/[^/]+(/instances)?)?", EVENTS_READ),
    ("POST", r"/freeBusy", READ),
    ("POST", r"/calendars/[^/]+/events(/quickAdd)?", EVENTS_WRITE),
    ("PATCH", r"/calendars/[^/]+/events/[^/]+", EVENTS_WRITE),
    ("DELETE", r"/calendars/[^/]+/events/[^/]+", EVENTS_WRITE),
]

DAY = {"time_min": "2026-11-02", "time_max": "2026-11-03"}
FREEBUSY = {"calendars": {"primary": {"busy": []}}}
ALL_DAY = event("evt2", start="2026-11-02", end="2026-11-03")

#: Paths the matrix in test_every_tool.py does not take: times without an
#: offset (a zone lookup first), conversions, per-calendar listing.
EXTRA: list[tuple[str, dict, list]] = [
    ("GOOGLECALENDAR_EVENTS_LIST", {"timeMin": "2026-11-02"}, [zone_reply(), response(200, events_page())]),
    ("GOOGLECALENDAR_FIND_EVENT", {"time_min": "2026-11-02T09:00"}, [zone_reply(), response(200, events_page())]),
    ("GOOGLECALENDAR_EVENTS_INSTANCES", {"eventId": "e", "timeMin": "2026-11-02"}, [zone_reply(), response(200, events_page())]),
    ("GOOGLECALENDAR_EVENTS_LIST_ALL_CALENDARS", {**DAY, "calendar_ids": ["team@g.co"]}, [zone_reply(), response(200, events_page())]),
    (
        "GOOGLECALENDAR_EVENTS_LIST_ALL_CALENDARS",
        DAY,
        [response(200, {"items": [calendar_entry("me@x.co", "Me", primary=True)]}), response(200, events_page())],
    ),
    ("GOOGLECALENDAR_FREE_BUSY_QUERY", {"items": ["primary"], "timeMin": "2026-11-02", "timeMax": "2026-11-03"}, [zone_reply(), response(200, FREEBUSY)]),
    ("GOOGLECALENDAR_FIND_FREE_SLOTS", {}, [zone_reply(), response(200, FREEBUSY)]),
    ("GOOGLECALENDAR_CREATE_EVENT", {"start_datetime": "2026-11-02T09:00"}, [zone_reply(), response(200, event())]),
    ("GOOGLECALENDAR_UPDATE_EVENT", {"event_id": "evt2", "start_datetime": "2026-11-02T10:00"}, [response(200, ALL_DAY), zone_reply(), response(200, event())]),
    ("GOOGLECALENDAR_UPDATE_EVENT", {"event_id": "evt1", "start_date": "2026-11-05"}, [response(200, event("evt1")), response(200, ALL_DAY)]),
]

SCENARIOS = [(slug, args, answers) for slug, (args, answers, _) in CASES.items()] + EXTRA


def _endpoint(method: str, path: str) -> frozenset[str]:
    for verb, pattern, scopes in ENDPOINTS:
        if verb == method and re.fullmatch(pattern, path):
            return scopes
    raise AssertionError(f"no scope entry for {method} {path}: add the endpoint to ENDPOINTS")


@functools.cache
def _requests_by_tool() -> dict[str, list[frozenset[str]]]:
    """Each tool's requests (as the scopes their endpoints accept), over every
    scenario. Run once, inside the first test (so under the socket guard)."""
    seen: dict[str, list[frozenset[str]]] = {}
    for slug, args, answers in SCENARIOS:
        h = harness(*answers)
        result = h.call(slug, args, config={**CONFIG, "granted_scopes": list(REQUIRED_SCOPES[slug])})
        assert result.successful, f"{slug}: {result.error}"
        seen.setdefault(slug, []).extend(_endpoint(c.method, c.path) for c in h.session.calls)
    return seen


def _allowed(accepted: frozenset[str], granted: frozenset[str]) -> bool:
    return any(GoogleCalendarToolkit().scope_granted(scope, granted) for scope in accepted)


def test_every_tool_is_audited():
    assert set(_requests_by_tool()) == set(REQUIRED_SCOPES)


@pytest.mark.parametrize("slug", sorted(REQUIRED_SCOPES))
def test_declared_scopes_allow_every_request(slug):
    granted = frozenset(REQUIRED_SCOPES[slug])
    for accepted in _requests_by_tool()[slug]:
        assert _allowed(accepted, granted), f"{slug} sends a request its scopes do not allow ({sorted(accepted)})"


@pytest.mark.parametrize("slug", sorted(REQUIRED_SCOPES))
def test_every_declared_scope_is_needed(slug):
    for scope in REQUIRED_SCOPES[slug]:
        fewer = frozenset(REQUIRED_SCOPES[slug]) - {scope}
        assert any(not _allowed(accepted, fewer) for accepted in _requests_by_tool()[slug]), f"{slug} does not need {scope}"


#: The corrected tools (and FIND_FREE_SLOTS, whose zone lookup the review
#: flagged): each one's run that looks up the calendar's zone.
LOOKUPS = {
    "GOOGLECALENDAR_CREATE_EVENT": ({"start_datetime": "2026-11-02T09:00"}, [zone_reply(), response(200, event())]),
    "GOOGLECALENDAR_UPDATE_EVENT": (
        {"event_id": "evt2", "start_datetime": "2026-11-02T10:00"},
        [response(200, ALL_DAY), zone_reply(), response(200, event())],
    ),
    "GOOGLECALENDAR_FIND_FREE_SLOTS": ({}, [zone_reply(), response(200, FREEBUSY)]),
}


@pytest.mark.parametrize("slug", sorted(LOOKUPS))
def test_calendar_events_alone_does_not_list_or_run_it(slug):
    config = {**CONFIG, "granted_scopes": [EVENTS_SCOPE]}
    assert slug not in {t.slug for t in GoogleCalendarToolkit().available_tools(config)}
    args, _ = LOOKUPS[slug]
    h = harness()
    assert h.call(slug, args, config=config).error_code == "insufficient_scope"
    assert h.session.calls == []


#: Grants that cover each of those tools: its declared scopes, or full access.
COVERING = [(slug, list(REQUIRED_SCOPES[slug])) for slug in sorted(LOOKUPS)] + [(slug, [FULL_SCOPE]) for slug in sorted(LOOKUPS)]


@pytest.mark.parametrize(("slug", "granted"), COVERING)
def test_its_scopes_or_full_access_run_it_zone_lookup_included(slug, granted):
    args, answers = LOOKUPS[slug]
    h = harness(*answers)
    result = h.call(slug, args, config={**CONFIG, "granted_scopes": granted})
    assert result.successful, result.error
    assert ("GET", "/calendars/primary") in [(c.method, c.path) for c in h.session.calls]


def test_events_alone_still_lists_quick_add_and_delete():
    listed = {t.slug for t in GoogleCalendarToolkit().available_tools({**CONFIG, "granted_scopes": [EVENTS_SCOPE]})}
    assert listed == {"GOOGLECALENDAR_QUICK_ADD", "GOOGLECALENDAR_DELETE_EVENT"}
