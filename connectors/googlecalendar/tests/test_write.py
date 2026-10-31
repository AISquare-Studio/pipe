"""Write tools: create (guests, Meet links, special event types), update as
a patch that keeps what it is not told to change, quick add and delete."""

from __future__ import annotations

from .fakes import event, google_error, harness, response, zone_reply

AT_NINE = "2026-11-02T09:00:00-05:00"


class TestCreate:
    def test_full_event(self):
        h = harness(response(200, event(hangoutLink="https://meet.google.com/abc-defg-hij")))
        result = h.call(
            "GOOGLECALENDAR_CREATE_EVENT",
            {
                "summary": "Launch review",
                "description": "<b>Agenda</b>",
                "location": "Room 4",
                "start_datetime": "2026-11-02T09:00",
                "event_duration_hour": 1,
                "event_duration_minutes": 15,
                "timezone": "America/New_York",
                "attendees": ["ann@example.com", {"email": "bob@example.com", "optional": True, "displayName": "Bob", "self": True}],
                "visibility": "private",
                "transparency": "opaque",
                "guestsCanModify": True,
                "guestsCanSeeOtherGuests": False,
                "extended_properties": {"private": {"source": "pipe"}, "other": {"x": "y"}},
            },
        )
        assert result.successful, result.error
        assert result.data["hangoutLink"] == "https://meet.google.com/abc-defg-hij"
        call = h.session.last
        assert (call.method, call.path) == ("POST", "/calendars/primary/events")
        assert call.params == {"sendUpdates": "all", "conferenceDataVersion": 1}
        body = call.json
        request_id = body["conferenceData"]["createRequest"].pop("requestId")
        assert len(request_id) == 32
        assert body == {
            "summary": "Launch review",
            "description": "<b>Agenda</b>",
            "location": "Room 4",
            "visibility": "private",
            "transparency": "opaque",
            "attendees": [{"email": "ann@example.com"}, {"email": "bob@example.com", "optional": True, "displayName": "Bob"}],
            "guestsCanModify": True,
            "guestsCanSeeOtherGuests": False,
            "extendedProperties": {"private": {"source": "pipe"}},
            "start": {"dateTime": "2026-11-02T09:00:00", "timeZone": "America/New_York"},
            "end": {"dateTime": "2026-11-02T10:15:00", "timeZone": "America/New_York"},
            "conferenceData": {"createRequest": {"conferenceSolutionKey": {"type": "hangoutsMeet"}}},
        }

    def test_meet_link_requests_are_unique(self):
        h = harness(response(200, event()), response(200, event()))
        for _ in range(2):
            h.call("GOOGLECALENDAR_CREATE_EVENT", {"start_datetime": AT_NINE})
        ids = {c.json["conferenceData"]["createRequest"]["requestId"] for c in h.session.calls}
        assert len(ids) == 2

    def test_no_meet_link_and_quiet(self):
        h = harness(response(200, event()))
        h.call("GOOGLECALENDAR_CREATE_EVENT", {"start_datetime": AT_NINE, "create_meeting_room": False, "send_updates": "none"})
        assert "conferenceData" not in h.session.last.json
        assert h.session.last.params == {"sendUpdates": "none"}

    def test_focus_time_gets_its_properties_and_no_meet(self):
        h = harness(response(200, event()))
        h.call(
            "GOOGLECALENDAR_CREATE_EVENT",
            {
                "start_datetime": AT_NINE,
                "eventType": "focusTime",
                "focusTimeProperties": {"chatStatus": "doNotDisturb"},
                "outOfOfficeProperties": {"ignored": True},
            },
        )
        body = h.session.last.json
        assert body["eventType"] == "focusTime"
        assert body["focusTimeProperties"] == {"chatStatus": "doNotDisturb"}
        assert "outOfOfficeProperties" not in body and "conferenceData" not in body

    def test_other_calendar(self):
        h = harness(response(200, event()))
        h.call("GOOGLECALENDAR_CREATE_EVENT", {"start_datetime": AT_NINE, "calendar_id": "team@group.calendar.google.com"})
        assert h.session.last.path == "/calendars/team%40group.calendar.google.com/events"

    def test_aliases(self):
        h = harness(response(200, event()))
        h.call("GOOGLECALENDAR_CREATE_EVENT", {"title": "Sync", "start_time": AT_NINE, "end_time": "2026-11-02T10:00:00-05:00"})
        body = h.session.last.json
        assert (body["summary"], body["end"]["dateTime"]) == ("Sync", "2026-11-02T10:00:00-05:00")

    def test_create_is_not_retried_on_a_5xx(self):
        h = harness(google_error(503, "Backend Error", "backendError"))
        assert h.call("GOOGLECALENDAR_CREATE_EVENT", {"start_datetime": AT_NINE}).error_code == "upstream_error"
        assert len(h.session.calls) == 1  # an event created half-way must not be created twice

    def test_no_write_access_to_the_calendar_is_forbidden(self):
        h = harness(google_error(403, "You need to have writer access to this calendar.", "requiredAccessLevel"))
        result = h.call("GOOGLECALENDAR_CREATE_EVENT", {"start_datetime": AT_NINE, "calendar_id": "holidays@g.co"})
        assert result.error_code == "forbidden"
        assert result.data == {"status": 403, "reason": "requiredAccessLevel"}


class TestUpdate:
    CURRENT = event("evt1", summary="Standup", start="2026-11-02T09:00:00-05:00", end="2026-11-02T09:45:00-05:00",
                    attendees=[{"email": "ann@example.com"}], description="keep me")

    def test_only_given_fields_change(self):
        h = harness(response(200, self.CURRENT), response(200, {**self.CURRENT, "summary": "Daily standup"}))
        result = h.call("GOOGLECALENDAR_UPDATE_EVENT", {"event_id": "evt1", "summary": "Daily standup", "send_updates": "none"})
        assert result.data["summary"] == "Daily standup"
        call = h.session.last
        assert (call.method, call.path, call.json, call.params) == (
            "PATCH",
            "/calendars/primary/events/evt1",
            {"summary": "Daily standup"},
            {"sendUpdates": "none"},
        )

    def test_reschedule_keeps_the_length(self):
        h = harness(response(200, self.CURRENT), response(200, self.CURRENT))
        h.call("GOOGLECALENDAR_UPDATE_EVENT", {"event_id": "evt1", "start_datetime": "2026-11-03T14:00"})
        assert h.session.last.json == {
            "start": {"dateTime": "2026-11-03T14:00:00", "timeZone": "America/New_York"},
            "end": {"dateTime": "2026-11-03T14:45:00", "timeZone": "America/New_York"},
        }

    def test_new_end_only(self):
        h = harness(response(200, self.CURRENT), response(200, self.CURRENT))
        h.call("GOOGLECALENDAR_UPDATE_EVENT", {"event_id": "evt1", "end_datetime": "2026-11-02T10:00:00-05:00"})
        patch = h.session.last.json
        assert patch["start"]["dateTime"] == "2026-11-02T09:00:00-05:00"
        assert patch["end"]["dateTime"] == "2026-11-02T10:00:00-05:00"

    def test_new_duration_only(self):
        h = harness(response(200, self.CURRENT), response(200, self.CURRENT))
        h.call("GOOGLECALENDAR_UPDATE_EVENT", {"event_id": "evt1", "event_duration_hour": 2})
        assert h.session.last.json["end"]["dateTime"] == "2026-11-02T11:00:00-05:00"

    def test_end_before_start_is_refused_without_patching(self):
        h = harness(response(200, self.CURRENT))
        result = h.call("GOOGLECALENDAR_UPDATE_EVENT", {"event_id": "evt1", "end_datetime": "2026-11-02T08:00:00-05:00"})
        assert result.error_code == "invalid_arguments"
        assert [c.method for c in h.session.calls] == ["GET"]

    def test_attendees_replace_the_list(self):
        h = harness(response(200, self.CURRENT), response(200, self.CURRENT))
        h.call("GOOGLECALENDAR_UPDATE_EVENT", {"event_id": "evt1", "attendees": ["bob@example.com"]})
        assert h.session.last.json == {"attendees": [{"email": "bob@example.com"}]}

    def test_zone_alone_relabels_the_times(self):
        h = harness(response(200, self.CURRENT), response(200, self.CURRENT))
        h.call("GOOGLECALENDAR_UPDATE_EVENT", {"event_id": "evt1", "timezone": "Europe/Dublin"})
        assert h.session.last.json == {
            "start": {"dateTime": "2026-11-02T09:00:00-05:00", "timeZone": "Europe/Dublin"},
            "end": {"dateTime": "2026-11-02T09:45:00-05:00", "timeZone": "Europe/Dublin"},
        }

    def test_meet_link_only_when_missing(self):
        h = harness(response(200, self.CURRENT), response(200, self.CURRENT))
        h.call("GOOGLECALENDAR_UPDATE_EVENT", {"event_id": "evt1", "create_meeting_room": True})
        assert "createRequest" in h.session.last.json["conferenceData"]
        assert h.session.last.params == {"conferenceDataVersion": 1}  # no emails unless send_updates says so
        has_meet = {**self.CURRENT, "conferenceData": {"entryPoints": []}}
        h = harness(response(200, has_meet))
        result = h.call("GOOGLECALENDAR_UPDATE_EVENT", {"event_id": "evt1", "create_meeting_room": True})
        assert result.error_code == "invalid_arguments"  # nothing left to change

    def test_all_day_event_becomes_timed(self):
        """Google merges nested objects on PATCH: the stored date is nulled."""
        all_day = event("evt2", start="2026-11-02", end="2026-11-03")
        h = harness(response(200, all_day), zone_reply("Europe/Rome"), response(200, all_day))
        h.call("GOOGLECALENDAR_UPDATE_EVENT", {"event_id": "evt2", "start_datetime": "2026-11-02T10:00"})
        assert h.session.last.json == {
            "start": {"dateTime": "2026-11-02T10:00:00", "timeZone": "Europe/Rome", "date": None},
            "end": {"dateTime": "2026-11-02T10:30:00", "timeZone": "Europe/Rome", "date": None},
        }

    def test_timed_event_becomes_all_day(self):
        """...and the stored dateTime and timeZone are nulled the other way."""
        h = harness(response(200, self.CURRENT), response(200, self.CURRENT))
        h.call("GOOGLECALENDAR_UPDATE_EVENT", {"event_id": "evt1", "start_date": "2026-11-05"})
        assert h.session.last.json == {
            "start": {"date": "2026-11-05", "dateTime": None, "timeZone": None},
            "end": {"date": "2026-11-06", "dateTime": None, "timeZone": None},
        }

    def test_timed_event_becomes_a_multi_day_all_day_event_in_a_zone(self):
        h = harness(response(200, self.CURRENT), response(200, self.CURRENT))
        h.call("GOOGLECALENDAR_UPDATE_EVENT", {"event_id": "evt1", "start_date": "2026-11-05", "end_date": "2026-11-08", "timezone": "Europe/Oslo"})
        assert h.session.last.json == {
            "start": {"date": "2026-11-05", "timeZone": "Europe/Oslo", "dateTime": None},
            "end": {"date": "2026-11-08", "timeZone": "Europe/Oslo", "dateTime": None},
        }

    def test_all_day_event_moves_and_keeps_its_length(self):
        trip = event("evt3", start="2026-11-02", end="2026-11-05")
        h = harness(response(200, trip), response(200, trip))
        h.call("GOOGLECALENDAR_UPDATE_EVENT", {"event_id": "evt3", "start_date": "2026-12-01"})
        assert h.session.last.json == {"start": {"date": "2026-12-01"}, "end": {"date": "2026-12-04"}}  # nothing to clear

    def test_all_day_event_gets_a_new_end(self):
        trip = event("evt3", start="2026-11-02", end="2026-11-05")
        h = harness(response(200, trip), response(200, trip))
        h.call("GOOGLECALENDAR_UPDATE_EVENT", {"event_id": "evt3", "end_date": "2026-11-03"})
        assert h.session.last.json == {"start": {"date": "2026-11-02"}, "end": {"date": "2026-11-03"}}

    def test_timed_event_needs_a_start_date_to_become_all_day(self):
        h = harness(response(200, self.CURRENT))
        result = h.call("GOOGLECALENDAR_UPDATE_EVENT", {"event_id": "evt1", "end_date": "2026-11-09"})
        assert result.error_code == "invalid_arguments"
        assert [c.method for c in h.session.calls] == ["GET"]

    def test_timed_to_timed_sends_no_nulls(self):
        h = harness(response(200, self.CURRENT), response(200, self.CURRENT))
        h.call("GOOGLECALENDAR_UPDATE_EVENT", {"event_id": "evt1", "start_datetime": "2026-11-03T14:00:00-05:00"})
        assert all(None not in moment.values() for moment in (h.session.last.json["start"], h.session.last.json["end"]))

    def test_all_day_event_needs_a_start_to_get_an_end(self):
        h = harness(response(200, event("evt2", start="2026-11-02", end="2026-11-03")))
        assert h.call("GOOGLECALENDAR_UPDATE_EVENT", {"event_id": "evt2", "event_duration_hour": 1}).error_code == "invalid_arguments"

    def test_missing_event(self):
        h = harness(google_error(404, "Not Found", "notFound"))
        assert h.call("GOOGLECALENDAR_UPDATE_EVENT", {"event_id": "gone", "summary": "x"}).error_code == "not_found"


class TestQuickAdd:
    def test_quick_add(self):
        h = harness(response(200, event(summary="Lunch with Ann")))
        result = h.call("GOOGLECALENDAR_QUICK_ADD", {"text": " Lunch with Ann tomorrow 1pm ", "send_updates": "none", "calendar_id": "me@example.com"})
        call = h.session.last
        assert (call.method, call.path, call.json) == ("POST", "/calendars/me%40example.com/events/quickAdd", None)
        assert call.params == {"text": "Lunch with Ann tomorrow 1pm", "sendUpdates": "none"}
        assert result.data["summary"] == "Lunch with Ann"


class TestDelete:
    def test_delete(self):
        h = harness(response(204))
        result = h.call("GOOGLECALENDAR_DELETE_EVENT", {"event_id": "evt1", "calendar_id": "team@g.co", "send_updates": "externalOnly"})
        call = h.session.last
        assert (call.method, call.path, call.params) == ("DELETE", "/calendars/team%40g.co/events/evt1", {"sendUpdates": "externalOnly"})
        assert result.data == {"deleted": True, "event_id": "evt1", "calendar_id": "team@g.co"}

    def test_deprecated_send_notifications(self):
        for flag, expected in ((True, "all"), (False, "none")):
            h = harness(response(204))
            h.call("GOOGLECALENDAR_DELETE_EVENT", {"event_id": "evt1", "send_notifications": flag})
            assert h.session.last.params == {"sendUpdates": expected}

    def test_already_deleted_is_not_found(self):
        h = harness(google_error(410, "Resource has been deleted", "deleted"))
        assert h.call("GOOGLECALENDAR_DELETE_EVENT", {"event_id": "evt1"}).error_code == "not_found"

    def test_delete_is_destructive(self):
        from aisquare_pipe_googlecalendar import GoogleCalendarToolkit

        spec = GoogleCalendarToolkit().get_tool("GOOGLECALENDAR_DELETE_EVENT")
        assert spec is not None and spec.destructive and not spec.read_only

    def test_update_is_destructive(self):
        # attendees/recurrence replace the whole list: an update can cancel
        # meetings, like GITHUB_UPDATE_AN_ISSUE or SLACK_UPDATES_A_SLACK_MESSAGE.
        from aisquare_pipe_googlecalendar import GoogleCalendarToolkit

        spec = GoogleCalendarToolkit().get_tool("GOOGLECALENDAR_UPDATE_EVENT")
        assert spec is not None and spec.risk == "destructive" and not spec.read_only


class TestFreeBusy:
    def test_query(self):
        reply = {"kind": "calendar#freeBusy", "timeMin": "2026-11-02T00:00:00Z", "timeMax": "2026-11-03T00:00:00Z",
                 "calendars": {"ann@example.com": {"busy": [{"start": "2026-11-02T14:00:00Z", "end": "2026-11-02T15:00:00Z"}]},
                               "nobody@example.com": {"errors": [{"domain": "global", "reason": "notFound"}], "busy": []}}}
        h = harness(response(200, reply))
        data = h.call(
            "GOOGLECALENDAR_FREE_BUSY_QUERY",
            {"items": ["ann@example.com", {"id": "nobody@example.com"}], "timeMin": "2026-11-02", "timeMax": "2026-11-03", "timeZone": "UTC", "calendarExpansionMax": 5},
        ).data
        call = h.session.last
        assert (call.method, call.path) == ("POST", "/freeBusy")
        assert call.json == {
            "timeMin": "2026-11-02T00:00:00Z",
            "timeMax": "2026-11-03T00:00:00Z",
            "timeZone": "UTC",
            "items": [{"id": "ann@example.com"}, {"id": "nobody@example.com"}],
            "calendarExpansionMax": 5,
        }
        assert "kind" not in data
        assert data["calendars"]["nobody@example.com"]["errors"][0]["reason"] == "notFound"

    def test_query_zone_defaults_to_the_primary_calendar(self):
        h = harness(zone_reply("Asia/Singapore"), response(200, {"calendars": {}}))
        h.call("GOOGLECALENDAR_FREE_BUSY_QUERY", {"items": ["primary"], "timeMin": "2026-11-02T09:00", "timeMax": "2026-11-02T18:00"})
        body = h.session.last.json
        assert (body["timeMin"], body["timeMax"], body["timeZone"]) == ("2026-11-02T09:00:00+08:00", "2026-11-02T18:00:00+08:00", "Asia/Singapore")

    def test_free_slots(self):
        busy = {
            "calendars": {
                "primary": {"busy": [
                    {"start": "2026-11-02T10:00:00Z", "end": "2026-11-02T11:00:00Z"},
                    {"start": "2026-11-02T10:30:00Z", "end": "2026-11-02T12:00:00Z"},
                ]},
                "bob@example.com": {"busy": [
                    {"start": "2026-11-02T07:00:00Z", "end": "2026-11-02T09:30:00Z"},  # starts before the window
                    {"start": "2026-11-02T15:00:00Z", "end": "2026-11-02T16:00:00Z"},
                ]},
            }
        }
        h = harness(response(200, busy))
        data = h.call(
            "GOOGLECALENDAR_FIND_FREE_SLOTS",
            {"items": ["primary", "bob@example.com"], "time_min": "2026-11-02T09:00:00Z", "time_max": "2026-11-02T17:00:00Z", "timezone": "Europe/London"},
        ).data
        assert data["timezone"] == "Europe/London"
        assert data["busy"] == [
            {"start": "2026-11-02T09:00:00Z", "end": "2026-11-02T09:30:00Z"},
            {"start": "2026-11-02T10:00:00Z", "end": "2026-11-02T12:00:00Z"},
            {"start": "2026-11-02T15:00:00Z", "end": "2026-11-02T16:00:00Z"},
        ]
        assert data["free_slots"] == [
            {"start": "2026-11-02T09:30:00Z", "end": "2026-11-02T10:00:00Z", "duration_minutes": 30},
            {"start": "2026-11-02T12:00:00Z", "end": "2026-11-02T15:00:00Z", "duration_minutes": 180},
            {"start": "2026-11-02T16:00:00Z", "end": "2026-11-02T17:00:00Z", "duration_minutes": 60},
        ]

    def test_free_slots_are_shown_in_the_zone(self):
        h = harness(response(200, {"calendars": {"primary": {"busy": [{"start": "2026-11-02T15:00:00Z", "end": "2026-11-02T16:00:00Z"}]}}}))
        data = h.call(
            "GOOGLECALENDAR_FIND_FREE_SLOTS", {"time_min": "2026-11-02T09:00", "time_max": "2026-11-02T17:00", "timezone": "America/New_York"}
        ).data
        assert h.session.last.json["timeMin"] == "2026-11-02T09:00:00-05:00"
        assert data["free_slots"] == [
            {"start": "2026-11-02T09:00:00-05:00", "end": "2026-11-02T10:00:00-05:00", "duration_minutes": 60},
            {"start": "2026-11-02T11:00:00-05:00", "end": "2026-11-02T17:00:00-05:00", "duration_minutes": 360},
        ]

    def test_free_slots_defaults(self, monkeypatch):
        import datetime

        from aisquare_pipe_googlecalendar import times

        monkeypatch.setattr(times, "now", lambda tz: datetime.datetime(2026, 11, 2, 8, 0, tzinfo=datetime.UTC).astimezone(tz))
        h = harness(zone_reply("UTC"), response(200, {"calendars": {"primary": {"busy": []}}}))
        data = h.call("GOOGLECALENDAR_FIND_FREE_SLOTS", {}).data
        body = h.session.last.json
        assert (body["items"], body["timeMin"], body["timeMax"]) == ([{"id": "primary"}], "2026-11-02T08:00:00Z", "2026-11-03T08:00:00Z")
        assert data["free_slots"] == [{"start": "2026-11-02T08:00:00Z", "end": "2026-11-03T08:00:00Z", "duration_minutes": 1440}]

    def test_fully_booked(self):
        h = harness(response(200, {"calendars": {"primary": {"busy": [{"start": "2026-11-02T00:00:00Z", "end": "2026-11-03T00:00:00Z"}]}}}))
        data = h.call("GOOGLECALENDAR_FIND_FREE_SLOTS", {"time_min": "2026-11-02T09:00:00Z", "time_max": "2026-11-02T10:00:00Z", "timezone": "UTC"}).data
        assert data["free_slots"] == []
