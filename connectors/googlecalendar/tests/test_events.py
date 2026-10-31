"""Read tools: calendars, event listings and their pagination, recurring
events and their instances, one event, and events across every calendar."""

from __future__ import annotations

from .fakes import (
    calendar_entry,
    event,
    events_page,
    google_error,
    harness,
    response,
    zone_reply,
)

ZDAY = {"timeMin": "2026-11-02T00:00:00Z", "timeMax": "2026-11-03T00:00:00Z"}


class TestCalendars:
    def test_list_calendars_page(self):
        page = {
            "kind": "calendar#calendarList",
            "etag": '"p1"',
            "nextPageToken": "CAL2",
            "nextSyncToken": None,
            "items": [calendar_entry("me@example.com", "Me", primary=True), calendar_entry("team@group.calendar.google.com", "Team", role="reader")],
        }
        h = harness(response(200, page))
        data = h.call("GOOGLECALENDAR_LIST_CALENDARS", {"max_results": 2, "show_hidden": True, "min_access_role": "reader"}).data
        assert h.session.calls[0].params == {"maxResults": 2, "showHidden": "true", "minAccessRole": "reader"}
        assert list(data)[:2] == ["has_more", "next_page_token"]
        assert (data["has_more"], data["next_page_token"]) == (True, "CAL2")
        assert [c["id"] for c in data["items"]] == ["me@example.com", "team@group.calendar.google.com"]
        assert all("kind" not in c for c in data["items"])

    def test_list_calendars_next_page_and_sync(self):
        h = harness(response(200, {"items": [], "nextSyncToken": "SYNC"}))
        data = h.call("GOOGLECALENDAR_LIST_CALENDARS", {"page_token": "CAL2"}).data
        assert h.session.calls[0].params == {"maxResults": 100, "pageToken": "CAL2"}
        assert (data["has_more"], data["next_sync_token"]) == (False, "SYNC")

    def test_page_size_is_clamped(self):
        h = harness(response(200, {"items": []}))
        h.call("GOOGLECALENDAR_LIST_CALENDARS", {"max_results": 9999})
        assert h.session.calls[0].params["maxResults"] == 250

    def test_get_calendar_by_email_id(self):
        h = harness(response(200, {"kind": "calendar#calendar", "id": "en.usa#holiday@group.v.calendar.google.com", "timeZone": "UTC"}))
        data = h.call("GOOGLECALENDAR_GET_CALENDAR", {"calendarId": "en.usa#holiday@group.v.calendar.google.com"}).data
        assert h.session.calls[0].path == "/calendars/en.usa%23holiday%40group.v.calendar.google.com"
        assert data == {"id": "en.usa#holiday@group.v.calendar.google.com", "timeZone": "UTC"}


class TestEventsList:
    def test_listing_with_pagination_hints(self):
        h = harness(response(200, events_page(event("a"), event("b"), token="EV2")))
        data = h.call("GOOGLECALENDAR_EVENTS_LIST", {**ZDAY, "maxResults": 2}).data
        assert h.session.calls[0].params == {"maxResults": 2, **ZDAY}
        assert list(data)[:2] == ["has_more", "next_page_token"]
        assert (data["has_more"], data["next_page_token"], data["time_zone"]) == (True, "EV2", "America/New_York")
        assert [e["id"] for e in data["items"]] == ["a", "b"]
        assert all("etag" not in e and "kind" not in e for e in data["items"])

    def test_next_page(self):
        h = harness(response(200, events_page(event("c"))))
        data = h.call("GOOGLECALENDAR_EVENTS_LIST", {**ZDAY, "pageToken": "EV2"}).data
        assert h.session.calls[0].params["pageToken"] == "EV2"
        assert (data["has_more"], data["next_page_token"]) == (False, None)

    def test_default_page_is_50(self):
        h = harness(response(200, events_page()))
        h.call("GOOGLECALENDAR_EVENTS_LIST", ZDAY)
        assert h.session.calls[0].params["maxResults"] == 50

    def test_query_and_filters(self):
        h = harness(response(200, events_page()))
        h.call(
            "GOOGLECALENDAR_EVENTS_LIST",
            {
                **ZDAY,
                "calendarId": "team@group.calendar.google.com",
                "query": " retro ",
                "showDeleted": True,
                "eventTypes": ["default", "focusTime"],
                "maxAttendees": 5,
                "privateExtendedProperty": "project=apollo",
            },
        )
        call = h.session.calls[0]
        assert call.path == "/calendars/team%40group.calendar.google.com/events"
        assert call.params == {
            "maxResults": 50,
            **ZDAY,
            "q": "retro",
            "showDeleted": "true",
            "eventTypes": ["default", "focusTime"],
            "maxAttendees": 5,
            "privateExtendedProperty": "project=apollo",
        }

    def test_order_by_start_time_expands_recurring_events(self):
        h = harness(response(200, events_page()))
        h.call("GOOGLECALENDAR_EVENTS_LIST", {**ZDAY, "orderBy": "startTime"})
        assert h.session.calls[0].params["singleEvents"] == "true"
        assert h.session.calls[0].params["orderBy"] == "startTime"

    def test_sync_token_alone(self):
        h = harness(response(200, events_page(sync="NEXTSYNC")))
        data = h.call("GOOGLECALENDAR_EVENTS_LIST", {"syncToken": "SYNC1"}).data
        assert h.session.calls[0].params == {"maxResults": 50, "syncToken": "SYNC1"}
        assert data["next_sync_token"] == "NEXTSYNC"

    def test_expired_sync_token_is_not_found(self):
        h = harness(google_error(410, "Sync token is no longer valid, a full sync is required.", "fullSyncRequired"))
        assert h.call("GOOGLECALENDAR_EVENTS_LIST", {"syncToken": "OLD"}).error_code == "not_found"

    def test_snake_case_arguments_reach_the_camel_case_schema(self):
        h = harness(response(200, events_page()))
        h.call("GOOGLECALENDAR_EVENTS_LIST", {"calendar_id": "x@example.com", "time_min": ZDAY["timeMin"], "max_results": 3})
        assert h.session.calls[0].path == "/calendars/x%40example.com/events"
        assert h.session.calls[0].params == {"maxResults": 3, "timeMin": ZDAY["timeMin"]}


class TestFindEvent:
    def test_defaults_expand_and_order_by_start(self):
        h = harness(response(200, events_page(event())))
        data = h.call("GOOGLECALENDAR_FIND_EVENT", {"query": "standup", "time_min": ZDAY["timeMin"]}).data
        assert h.session.calls[0].params == {
            "maxResults": 50,
            "timeMin": ZDAY["timeMin"],
            "q": "standup",
            "singleEvents": "true",
            "orderBy": "startTime",
        }
        assert data["items"][0]["id"] == "evt123"

    def test_unexpanded(self):
        h = harness(response(200, events_page()))
        h.call("GOOGLECALENDAR_FIND_EVENT", {"single_events": False, "page_token": "P2"})
        params = h.session.calls[0].params
        assert params["singleEvents"] == "false" and "orderBy" not in params and params["pageToken"] == "P2"


class TestOneEvent:
    def test_get(self):
        h = harness(response(200, event(attendees=[{"email": "ann@example.com", "responseStatus": "accepted"}])))
        data = h.call("GOOGLECALENDAR_EVENTS_GET", {"event_id": "evt123", "time_zone": "Asia/Kolkata", "max_attendees": 3}).data
        assert h.session.calls[0].params == {"timeZone": "Asia/Kolkata", "maxAttendees": 3}
        assert data["attendees"][0]["responseStatus"] == "accepted"

    def test_missing_event(self):
        h = harness(google_error(404, "Not Found", "notFound"))
        result = h.call("GOOGLECALENDAR_EVENTS_GET", {"event_id": "nope"})
        assert result.error_code == "not_found"
        assert "cannot see it" in result.error

    def test_instance_id_is_one_segment(self):
        h = harness(response(200, event("evt_20261102T140000Z")))
        h.call("GOOGLECALENDAR_EVENTS_GET", {"event_id": "evt_20261102T140000Z", "calendar_id": "a/b"})
        assert h.session.calls[0].path == "/calendars/a%2Fb/events/evt_20261102T140000Z"


class TestRecurring:
    SERIES = event("wk1", summary="Weekly 1:1", recurrence=["RRULE:FREQ=WEEKLY;BYDAY=MO;COUNT=4"])

    def test_series_is_returned_with_its_rule(self):
        h = harness(response(200, events_page(self.SERIES)))
        (item,) = h.call("GOOGLECALENDAR_EVENTS_LIST", ZDAY).data["items"]
        assert item["recurrence"] == ["RRULE:FREQ=WEEKLY;BYDAY=MO;COUNT=4"]

    def test_instances_page(self):
        instances = [
            event(f"wk1_2026110{d}T140000Z", summary="Weekly 1:1", recurringEventId="wk1", originalStartTime={"dateTime": f"2026-11-0{d}T09:00:00-05:00"})
            for d in (2, 9)
        ]
        h = harness(response(200, events_page(*instances, token="I2")))
        data = h.call("GOOGLECALENDAR_EVENTS_INSTANCES", {"eventId": "wk1", "timeMin": "2026-11-01T00:00:00Z", "maxResults": 2}).data
        assert h.session.calls[0].path == "/calendars/primary/events/wk1/instances"
        assert h.session.calls[0].params == {"maxResults": 2, "timeMin": "2026-11-01T00:00:00Z"}
        assert (data["has_more"], data["next_page_token"]) == (True, "I2")
        assert [i["recurringEventId"] for i in data["items"]] == ["wk1", "wk1"]

    def test_instances_next_page_and_original_start(self):
        h = harness(response(200, events_page()))
        h.call("GOOGLECALENDAR_EVENTS_INSTANCES", {"event_id": "wk1", "pageToken": "I2", "originalStart": "2026-11-09T09:00:00-05:00"})
        assert h.session.calls[0].params == {"maxResults": 50, "pageToken": "I2", "originalStart": "2026-11-09T09:00:00-05:00"}

    def test_instances_naive_bounds_use_the_calendar_zone(self):
        h = harness(zone_reply("Europe/London"), response(200, events_page()))
        h.call("GOOGLECALENDAR_EVENTS_INSTANCES", {"eventId": "wk1", "timeMin": "2026-07-01", "timeMax": "2026-07-02"})
        assert h.session.calls[0].params == {"fields": "timeZone"}
        assert h.session.calls[1].params["timeMin"] == "2026-07-01T00:00:00+01:00"


class TestAllCalendars:
    def _calendars(self, *extra, token=None):
        page = {"items": [calendar_entry("me@example.com", "Me", primary=True, zone="Europe/Berlin"), *extra]}
        if token:
            page["nextPageToken"] = token
        return response(200, page)

    def test_merges_tags_and_sorts(self):
        h = harness(
            self._calendars(calendar_entry("team@g.co", "Team")),
            response(200, events_page(event("late", start="2026-11-02T15:00:00+01:00", end="2026-11-02T16:00:00+01:00"))),
            response(200, events_page(
                event("early", start="2026-11-02T08:00:00Z", end="2026-11-02T09:00:00Z"),
                event("allday", start="2026-11-02", end="2026-11-03"),
            )),
        )
        data = h.call("GOOGLECALENDAR_EVENTS_LIST_ALL_CALENDARS", {"time_min": "2026-11-02", "time_max": "2026-11-03"}).data
        assert h.session.calls[1].params == {
            "timeMin": "2026-11-02T00:00:00+01:00",
            "timeMax": "2026-11-03T00:00:00+01:00",
            "maxResults": 50,
            "timeZone": "Europe/Berlin",
            "singleEvents": "true",
            "orderBy": "startTime",
            "showDeleted": "false",
        }
        assert [e["id"] for e in data["items"]] == ["allday", "early", "late"]
        assert [e["calendarSummary"] for e in data["items"]] == ["Team", "Team", "Me"]
        assert set(data["items"][0]) == {"id", "summary", "start", "end", "status", "htmlLink", "calendarId", "calendarSummary"}
        assert (data["time_zone"], data["calendar_count"], data["has_more"], data["errors"]) == ("Europe/Berlin", 2, False, [])

    def test_full_detail(self):
        h = harness(self._calendars(), response(200, events_page(event(description="Agenda"))))
        (item,) = h.call("GOOGLECALENDAR_EVENTS_LIST_ALL_CALENDARS", {"time_min": "2026-11-02T00:00:00Z", "time_max": "2026-11-03T00:00:00Z", "response_detail": "full"}).data["items"]
        assert item["description"] == "Agenda" and "etag" not in item

    def test_unreadable_calendars_are_reported_not_fatal(self):
        h = harness(
            self._calendars(calendar_entry("gone@g.co", "Gone"), calendar_entry("blocked@g.co", "Blocked")),
            response(200, events_page(event())),
            google_error(404, "Not Found", "notFound"),
            google_error(403, "Forbidden", "forbidden"),
        )
        data = h.call("GOOGLECALENDAR_EVENTS_LIST_ALL_CALENDARS", {"time_min": "2026-11-02T00:00:00Z", "time_max": "2026-11-03T00:00:00Z"}).data
        assert [(e["calendarId"], e["error_code"]) for e in data["errors"]] == [("gone@g.co", "not_found"), ("blocked@g.co", "forbidden")]
        assert len(data["items"]) == 1

    def test_a_401_mid_way_fails_the_call(self):
        h = harness(self._calendars(calendar_entry("team@g.co", "Team")), response(200, events_page()), google_error(401, "Invalid Credentials", "authError"))
        assert h.call("GOOGLECALENDAR_EVENTS_LIST_ALL_CALENDARS", {"time_min": "2026-11-02T00:00:00Z", "time_max": "2026-11-03T00:00:00Z"}).error_code == "unauthorized"

    def test_truncated_calendars_set_has_more(self):
        h = harness(self._calendars(), response(200, events_page(event(), token="MORE")))
        data = h.call("GOOGLECALENDAR_EVENTS_LIST_ALL_CALENDARS", {"time_min": "2026-11-02T00:00:00Z", "time_max": "2026-11-03T00:00:00Z", "max_results_per_calendar": 1}).data
        assert (data["has_more"], data["truncated_calendars"]) == (True, ["me@example.com"])
        assert h.session.calls[1].params["maxResults"] == 1

    def test_calendar_list_pages_are_followed(self):
        h = harness(
            self._calendars(token="CL2"),
            response(200, {"items": [calendar_entry("team@g.co", "Team")]}),
            response(200, events_page()),
            response(200, events_page()),
        )
        data = h.call("GOOGLECALENDAR_EVENTS_LIST_ALL_CALENDARS", {"time_min": "2026-11-02T00:00:00Z", "time_max": "2026-11-03T00:00:00Z"}).data
        assert h.session.calls[1].params == {"maxResults": 250, "pageToken": "CL2"}
        assert data["calendar_count"] == 2

    def test_explicit_calendar_ids_skip_the_list(self):
        h = harness(zone_reply("Asia/Tokyo"), response(200, events_page()))
        data = h.call(
            "GOOGLECALENDAR_EVENTS_LIST_ALL_CALENDARS",
            {"time_min": "2026-11-02", "time_max": "2026-11-03", "calendar_ids": ["team@g.co", "team@g.co"], "q": "launch", "single_events": False},
        ).data
        assert [c.path for c in h.session.calls] == ["/calendars/primary", "/calendars/team%40g.co/events"]
        params = h.session.calls[1].params
        assert params["timeMin"] == "2026-11-02T00:00:00+09:00"
        assert params["singleEvents"] == "false" and "orderBy" not in params and params["q"] == "launch"
        assert data["calendar_count"] == 1
