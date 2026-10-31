"""The catalog: identity, Composio's slugs and argument names, read/write
and destructive flags, least-privilege scopes, argument aliases and which
tools a model finds for common asks."""

from __future__ import annotations

from importlib.metadata import entry_points

import pytest

from aisquare_pipe_googlecalendar import (
    GOOGLECALENDAR_OAUTH,
    REQUIRED_SCOPES,
    GoogleCalendarToolkit,
)
from aisquare_pipe_googlecalendar.tools import (
    EVENTS_SCOPE,
    READONLY_SCOPE,
    TOOLS,
    apply_aliases,
)

from .fakes import event, events_page, harness, response

#: Composio's Google Calendar slugs this toolkit mirrors, with the arguments
#: Composio documents for each that are implemented here
#: (docs.composio.dev/toolkits/googlecalendar, read 2026-10-30), plus the
#: few added here (marked).
COMPOSIO_ARGUMENTS = {
    "GOOGLECALENDAR_LIST_CALENDARS": {"max_results", "page_token", "show_hidden", "show_deleted", "min_access_role", "sync_token"},
    "GOOGLECALENDAR_GET_CALENDAR": {"calendar_id"},
    "GOOGLECALENDAR_GET_CURRENT_DATE_TIME": {"timezone"},
    "GOOGLECALENDAR_EVENTS_LIST": {
        "calendarId", "timeMin", "timeMax", "timeZone", "query", "maxResults", "pageToken", "singleEvents", "orderBy",
        "showDeleted", "updatedMin", "eventTypes", "iCalUID", "syncToken", "maxAttendees", "showHiddenInvitations",
        "privateExtendedProperty", "sharedExtendedProperty",
    },
    "GOOGLECALENDAR_FIND_EVENT": {
        "query", "time_min", "time_max", "calendar_id", "max_results", "page_token", "order_by", "single_events",
        "show_deleted", "event_types", "updated_min",
        "timezone",  # added
    },
    "GOOGLECALENDAR_EVENTS_GET": {"event_id", "calendar_id", "time_zone", "max_attendees"},
    "GOOGLECALENDAR_EVENTS_INSTANCES": {
        "eventId", "calendarId", "timeMin", "timeMax", "timeZone", "pageToken", "maxResults", "showDeleted", "maxAttendees", "originalStart",
    },
    "GOOGLECALENDAR_EVENTS_LIST_ALL_CALENDARS": {
        "time_min", "time_max", "q", "calendar_ids", "single_events", "show_deleted", "event_types", "response_detail",
        "max_results_per_calendar",
        "timezone",  # added
    },
    "GOOGLECALENDAR_FREE_BUSY_QUERY": {"items", "timeMin", "timeMax", "timeZone", "groupExpansionMax", "calendarExpansionMax"},
    "GOOGLECALENDAR_FIND_FREE_SLOTS": {"items", "time_min", "time_max", "timezone", "group_expansion_max", "calendar_expansion_max"},
    "GOOGLECALENDAR_CREATE_EVENT": {
        "summary", "description", "location", "start_datetime", "end_datetime", "event_duration_hour", "event_duration_minutes",
        "timezone", "attendees", "recurrence", "visibility", "transparency", "send_updates", "guestsCanModify",
        "guestsCanInviteOthers", "guestsCanSeeOtherGuests", "extended_properties", "calendar_id", "start_date", "end_date",
        "eventType", "create_meeting_room", "focusTimeProperties", "outOfOfficeProperties", "workingLocationProperties",
        "birthdayProperties",
    },
    "GOOGLECALENDAR_UPDATE_EVENT": {
        "event_id", "summary", "description", "location", "start_datetime", "end_datetime", "event_duration_hour",
        "event_duration_minutes", "timezone", "attendees", "recurrence", "visibility", "transparency", "send_updates",
        "guestsCanModify", "guestsCanInviteOthers", "guestsCanSeeOtherGuests", "extended_properties", "calendar_id",
        "create_meeting_room",
        "start_date", "end_date",  # added: all-day <-> timed
    },
    "GOOGLECALENDAR_QUICK_ADD": {"text", "calendar_id", "send_updates"},
    "GOOGLECALENDAR_DELETE_EVENT": {"event_id", "calendar_id", "send_updates", "send_notifications"},
}
#: Required arguments. Composio also requires calendarId on EVENTS_INSTANCES
#: and start_datetime on UPDATE_EVENT; both are optional here (primary /
#: the event's own start), which accepts every call Composio accepts.
REQUIRED = {
    "GOOGLECALENDAR_EVENTS_GET": ["event_id"],
    "GOOGLECALENDAR_EVENTS_INSTANCES": ["eventId"],
    "GOOGLECALENDAR_EVENTS_LIST_ALL_CALENDARS": ["time_min", "time_max"],
    "GOOGLECALENDAR_FREE_BUSY_QUERY": ["items", "timeMin", "timeMax"],
    "GOOGLECALENDAR_UPDATE_EVENT": ["event_id"],
    "GOOGLECALENDAR_QUICK_ADD": ["text"],
    "GOOGLECALENDAR_DELETE_EVENT": ["event_id"],
}


class TestCatalog:
    def test_registered_as_the_googlecalendar_toolkit(self):
        (ep,) = [ep for ep in entry_points(group="aisquare_pipe.toolkits") if ep.name == "googlecalendar"]
        assert ep.load() is GoogleCalendarToolkit

    def test_identity(self):
        toolkit = GoogleCalendarToolkit()
        assert (toolkit.name, toolkit.slug_prefix, toolkit.display_name) == ("googlecalendar", "GOOGLECALENDAR_", "Google Calendar")
        assert toolkit.oauth is GOOGLECALENDAR_OAUTH

    def test_composio_slugs_and_arguments(self):
        tools = {t.slug: t for t in GoogleCalendarToolkit().tools()}
        assert set(tools) == set(COMPOSIO_ARGUMENTS)
        for slug, names in COMPOSIO_ARGUMENTS.items():
            assert set(tools[slug].input_schema["properties"]) == names, slug
            assert tools[slug].input_schema["required"] == REQUIRED.get(slug, []), slug

    def test_read_only_and_destructive_flags(self):
        flags = {t.slug: (t.read_only, t.destructive) for t in GoogleCalendarToolkit().tools()}
        writes = {s for s, (ro, _) in flags.items() if not ro}
        assert writes == {
            "GOOGLECALENDAR_CREATE_EVENT",
            "GOOGLECALENDAR_UPDATE_EVENT",
            "GOOGLECALENDAR_QUICK_ADD",
            "GOOGLECALENDAR_DELETE_EVENT",
        }
        assert {s for s, (_, d) in flags.items() if d} == {"GOOGLECALENDAR_DELETE_EVENT"}

    def test_descriptions_fit_and_name_their_scope(self):
        for tool in GoogleCalendarToolkit().tools():
            assert len(tool.description) <= 1024
            for scope in REQUIRED_SCOPES[tool.slug]:
                assert scope.rsplit("/", 1)[-1] in tool.description, tool.slug


class TestScopes:
    def test_default_consent_is_read_only(self):
        assert GOOGLECALENDAR_OAUTH.default_scopes == (READONLY_SCOPE,)

    def test_reads_need_the_default_writes_need_events(self):
        both = {"GOOGLECALENDAR_CREATE_EVENT", "GOOGLECALENDAR_UPDATE_EVENT"}  # also read the calendar's zone
        for tool in GoogleCalendarToolkit().tools():
            expected = (READONLY_SCOPE,) if tool.read_only else (READONLY_SCOPE, EVENTS_SCOPE) if tool.slug in both else (EVENTS_SCOPE,)
            assert REQUIRED_SCOPES[tool.slug] == expected, tool.slug

    def test_no_tool_needs_full_calendar_access(self):
        assert all(not s.endswith("/calendar") for scopes in REQUIRED_SCOPES.values() for s in scopes)

    def test_tool_specs_carry_their_scopes_and_risk(self):
        for tool in GoogleCalendarToolkit().tools():
            assert tool.required_scopes == REQUIRED_SCOPES[tool.slug]
            assert tool.risk == ("destructive" if tool.destructive else "read" if tool.read_only else "write")

    @pytest.mark.parametrize(
        ("granted", "count"),
        [([READONLY_SCOPE], 10), ([READONLY_SCOPE, EVENTS_SCOPE], 14), (["https://www.googleapis.com/auth/calendar"], 14), ([EVENTS_SCOPE], 2)],
    )
    def test_granted_scopes_decide_what_is_listed(self, granted, count):
        listed = GoogleCalendarToolkit().available_tools({"access_token": "t", "granted_scopes": granted})
        assert len(listed) == count
        if count == 10:
            assert all(t.read_only for t in listed)

    def test_a_write_without_its_scope_is_refused_before_any_request(self):
        h = harness()
        result = h.call(
            "GOOGLECALENDAR_DELETE_EVENT", {"event_id": "e"}, config={"access_token": "t", "granted_scopes": f"{READONLY_SCOPE}"}
        )
        assert result.error_code == "insufficient_scope"
        assert h.session.calls == []

    def test_full_calendar_scope_covers_writes(self):
        h = harness(response(204))
        result = h.call(
            "GOOGLECALENDAR_DELETE_EVENT",
            {"event_id": "e"},
            config={"access_token": "t", "granted_scopes": "https://www.googleapis.com/auth/calendar"},
        )
        assert result.successful, result.error


class TestAliases:
    def test_other_casing(self):
        tool = TOOLS["GOOGLECALENDAR_EVENTS_LIST"]
        assert apply_aliases(tool.spec, {"calendar_id": "c", "time_min": "t", "max_results": 3}) == {"calendarId": "c", "timeMin": "t", "maxResults": 3}
        tool = TOOLS["GOOGLECALENDAR_FIND_EVENT"]
        assert apply_aliases(tool.spec, {"calendarId": "c", "timeMax": "t"}) == {"calendar_id": "c", "time_max": "t"}

    def test_the_schema_spelling_wins(self):
        tool = TOOLS["GOOGLECALENDAR_EVENTS_GET"]
        assert apply_aliases(tool.spec, {"event_id": "a", "eventId": "b"}) == {"event_id": "a", "eventId": "b"}

    def test_lowercase_slug_still_gets_aliases(self):
        h = harness(response(200, event()))
        assert h.call("googlecalendar_events_get", {"eventId": "evt123"}).successful

    def test_unknown_slug(self):
        assert harness().call("GOOGLECALENDAR_DELETE_ALL_CALENDARS", {}).error_code == "unknown_tool"


class TestToolSelection:
    @pytest.mark.parametrize(
        ("query", "slug"),
        [
            ("what is on my calendar this week", "GOOGLECALENDAR_EVENTS_LIST"),
            ("find the meeting about the budget", "GOOGLECALENDAR_FIND_EVENT"),
            ("schedule a meeting with ann tomorrow at 3", "GOOGLECALENDAR_CREATE_EVENT"),
            ("reschedule my standup to 10am", "GOOGLECALENDAR_UPDATE_EVENT"),
            ("cancel the event on friday", "GOOGLECALENDAR_DELETE_EVENT"),
            ("when am I free on thursday", "GOOGLECALENDAR_FIND_FREE_SLOTS"),
            ("is bob busy at 2pm", "GOOGLECALENDAR_FREE_BUSY_QUERY"),
            ("list my calendars", "GOOGLECALENDAR_LIST_CALENDARS"),
            ("what is today's date", "GOOGLECALENDAR_GET_CURRENT_DATE_TIME"),
            ("all occurrences of the recurring 1:1", "GOOGLECALENDAR_EVENTS_INSTANCES"),
        ],
    )
    def test_in_top_three(self, query, slug):
        assert slug in [t.slug for t in GoogleCalendarToolkit().search_tools(query, limit=3)]


def test_find_then_get_round_trip():
    h = harness(response(200, events_page(event("evt9"))), response(200, event("evt9")))
    found = h.call("GOOGLECALENDAR_FIND_EVENT", {"query": "standup", "time_min": "2026-11-02T00:00:00Z"}).data["items"][0]
    again = h.call("GOOGLECALENDAR_EVENTS_GET", {"event_id": found["id"]}).data
    assert again["id"] == "evt9"
