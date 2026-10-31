"""Live checks against the real Calendar API (opt-in).

Run with ``pipe validate googlecalendar --live`` or ``pytest -m live tests/test_live.py``.
Needs ``GOOGLECALENDAR_ACCESS_TOKEN`` (an OAuth access token with the
``calendar.readonly`` scope; e.g. from the OAuth Playground); self-skips
without it. Read-only by default. ``GOOGLECALENDAR_LIVE_WRITE=1`` (with a
``calendar.events`` token) also creates a recurring event on the primary
calendar, moves it, and deletes it.
"""

from __future__ import annotations

import datetime
import os

import pytest

from aisquare_pipe_googlecalendar import GoogleCalendarToolkit

TOKEN = os.environ.get("GOOGLECALENDAR_ACCESS_TOKEN", "")

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(not TOKEN, reason="GOOGLECALENDAR_ACCESS_TOKEN not set"),
]

TODAY = datetime.date.today()
WEEK = {"time_min": TODAY.isoformat(), "time_max": (TODAY + datetime.timedelta(days=7)).isoformat()}

READS = [
    ("GOOGLECALENDAR_LIST_CALENDARS", {"max_results": 5}),
    ("GOOGLECALENDAR_GET_CALENDAR", {}),
    ("GOOGLECALENDAR_GET_CURRENT_DATE_TIME", {}),
    ("GOOGLECALENDAR_EVENTS_LIST", {"timeMin": WEEK["time_min"], "maxResults": 3}),
    ("GOOGLECALENDAR_FIND_EVENT", {**WEEK, "max_results": 3}),
    ("GOOGLECALENDAR_EVENTS_LIST_ALL_CALENDARS", {**WEEK, "max_results_per_calendar": 3}),
    ("GOOGLECALENDAR_FREE_BUSY_QUERY", {"items": ["primary"], "timeMin": WEEK["time_min"], "timeMax": WEEK["time_max"]}),
    ("GOOGLECALENDAR_FIND_FREE_SLOTS", WEEK),
]


@pytest.fixture(scope="module")
def call():
    toolkit = GoogleCalendarToolkit()
    config = {"access_token": TOKEN, "timeout_seconds": 60}

    def run(slug, arguments):
        result = toolkit.call_tool(slug, arguments, config)
        assert result.successful, f"{slug}: {result.error}"
        return result

    return run


@pytest.mark.parametrize(("slug", "arguments"), READS, ids=[s.removeprefix("GOOGLECALENDAR_") for s, _ in READS])
def test_read_tool(call, slug, arguments):
    data = call(slug, arguments).data
    assert isinstance(data, dict)
    if "items" in data:
        assert list(data)[:2] == ["has_more", "next_page_token"]


def test_bad_token_is_unauthorized():
    result = GoogleCalendarToolkit().call_tool("GOOGLECALENDAR_GET_CALENDAR", {}, {"access_token": "ya29.definitelyNotAValidToken"})
    assert result.successful is False
    assert result.error_code == "unauthorized"


@pytest.mark.skipif(os.environ.get("GOOGLECALENDAR_LIVE_WRITE") != "1", reason="GOOGLECALENDAR_LIVE_WRITE != 1")
def test_write_round_trip(call):
    day = TODAY + datetime.timedelta(days=30)
    created = call(
        "GOOGLECALENDAR_CREATE_EVENT",
        {
            "summary": "aisquare-pipe live test",
            "start_datetime": f"{day.isoformat()}T09:00",
            "event_duration_hour": 1,
            "recurrence": ["RRULE:FREQ=DAILY;COUNT=2"],
            "create_meeting_room": False,
            "send_updates": "none",
        },
    ).data
    try:
        instances = call("GOOGLECALENDAR_EVENTS_INSTANCES", {"eventId": created["id"]}).data["items"]
        assert len(instances) == 2
        moved = call("GOOGLECALENDAR_UPDATE_EVENT", {"event_id": created["id"], "start_datetime": f"{day.isoformat()}T10:00", "send_updates": "none"}).data
        assert moved["start"]["dateTime"].startswith(f"{day.isoformat()}T10:00")
    finally:
        call("GOOGLECALENDAR_DELETE_EVENT", {"event_id": created["id"], "send_updates": "none"})
