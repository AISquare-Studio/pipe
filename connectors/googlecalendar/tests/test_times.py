"""Time zones and recurring events: loose times become RFC 3339 in the right
zone (daylight-saving changes included), event times stay wall-clock with
an IANA zone so Google expands recurring events correctly, and all-day
events use dates."""

from __future__ import annotations

import datetime
from zoneinfo import ZoneInfo

import pytest

from aisquare_pipe_googlecalendar import times

from .fakes import event, events_page, harness, response, zone_reply

NY = ZoneInfo("America/New_York")


class TestTimesModule:
    @pytest.mark.parametrize(
        ("text", "zone", "expected"),
        [
            ("2026-11-02T09:30:00Z", None, "2026-11-02T09:30:00Z"),
            ("2026-11-02T09:30:00+05:30", None, "2026-11-02T09:30:00+05:30"),
            ("2026-11-02T09:30:00.250-05:00", None, "2026-11-02T09:30:00-05:00"),
            ("2026-11-02 09:30", NY, "2026-11-02T09:30:00-05:00"),
            ("2026-07-02T09:30", NY, "2026-07-02T09:30:00-04:00"),
            ("2026-11-02", NY, "2026-11-02T00:00:00-05:00"),
            ("2026-11-02", None, "2026-11-02T00:00:00Z"),
            ("2026-03-08", NY, "2026-03-08T00:00:00-05:00"),  # DST starts at 02:00 that day
            ("2026-03-09", NY, "2026-03-09T00:00:00-04:00"),
            ("2026-11-02T09:30:00+00:00", None, "2026-11-02T09:30:00Z"),
        ],
    )
    def test_instant(self, text, zone, expected):
        assert times.instant(text, zone) == expected

    @pytest.mark.parametrize("text", ["tomorrow", "2026-13-01", "2026-11-02T25:00", "11/02/2026", ""])
    def test_bad_times(self, text):
        with pytest.raises(ValueError):
            times.instant(text, None)

    @pytest.mark.parametrize(
        ("values", "expected"),
        [
            (("2026-11-02T09:00:00Z", None), False),
            (("2026-11-02T09:00:00Z", "2026-11-02T10:00:00+01:00"), False),
            (("2026-11-02T09:00:00",), True),
            (("2026-11-02",), True),
            (("garbage",), False),
            ((None, ""), False),
        ],
    )
    def test_needs_zone(self, values, expected):
        assert times.needs_zone(*values) is expected

    @pytest.mark.parametrize("name", ["UTC", "Europe/London", "America/Argentina/Buenos_Aires", "Etc/GMT+5"])
    def test_zones(self, name):
        assert times.zone(name).key == name

    @pytest.mark.parametrize("name", ["Mars/Base", "../../etc/passwd", "Europe/London; DROP", "", "EST5EDT/x"])
    def test_bad_zones(self, name):
        with pytest.raises(ValueError):
            times.zone(name)

    def test_event_time(self):
        naive = datetime.datetime(2026, 11, 2, 9, 30)
        aware = datetime.datetime(2026, 11, 2, 9, 30, tzinfo=datetime.UTC)
        assert times.event_time(naive, "Europe/London") == {"dateTime": "2026-11-02T09:30:00", "timeZone": "Europe/London"}
        assert times.event_time(aware, None) == {"dateTime": "2026-11-02T09:30:00Z"}
        assert times.event_time(aware, "Asia/Tokyo") == {"dateTime": "2026-11-02T09:30:00Z", "timeZone": "Asia/Tokyo"}


class TestQueryBounds:
    def test_naive_bounds_use_the_calendars_zone(self):
        h = harness(zone_reply("America/New_York"), response(200, events_page()))
        h.call("GOOGLECALENDAR_EVENTS_LIST", {"timeMin": "2026-03-08", "timeMax": "2026-03-09"})
        lookup, listing = h.session.calls
        assert (lookup.path, lookup.params) == ("/calendars/primary", {"fields": "timeZone"})
        assert (listing.params["timeMin"], listing.params["timeMax"]) == ("2026-03-08T00:00:00-05:00", "2026-03-09T00:00:00-04:00")
        assert "timeZone" not in listing.params

    def test_zone_lookup_is_for_the_listed_calendar(self):
        h = harness(zone_reply("Asia/Kolkata"), response(200, events_page()))
        h.call("GOOGLECALENDAR_FIND_EVENT", {"calendar_id": "team@g.co", "time_min": "2026-11-02T09:00"})
        assert h.session.calls[0].path == "/calendars/team%40g.co"
        assert h.session.calls[1].params["timeMin"] == "2026-11-02T09:00:00+05:30"

    def test_given_zone_wins_and_shapes_the_result(self):
        h = harness(response(200, events_page(zone="Asia/Tokyo")))
        data = h.call("GOOGLECALENDAR_EVENTS_LIST", {"timeMin": "2026-11-02", "timeMax": "2026-11-03", "timeZone": "Asia/Tokyo"}).data
        (call,) = h.session.calls
        assert (call.params["timeMin"], call.params["timeZone"]) == ("2026-11-02T00:00:00+09:00", "Asia/Tokyo")
        assert data["time_zone"] == "Asia/Tokyo"

    def test_offsets_need_no_lookup(self):
        h = harness(response(200, events_page()))
        h.call("GOOGLECALENDAR_EVENTS_LIST", {"timeMin": "2026-11-02T00:00:00-08:00", "updatedMin": "2026-10-01T00:00:00Z"})
        (call,) = h.session.calls
        assert (call.params["timeMin"], call.params["updatedMin"]) == ("2026-11-02T00:00:00-08:00", "2026-10-01T00:00:00Z")

    def test_bounds_compare_as_instants(self):
        h = harness(response(200, events_page()))
        # 10:00+05:30 is 04:30Z — before 05:00Z, so the range is fine.
        assert h.call("GOOGLECALENDAR_EVENTS_LIST", {"timeMin": "2026-11-02T10:00:00+05:30", "timeMax": "2026-11-02T05:00:00Z"}).successful

    def test_unknown_calendar_zone_falls_back_to_utc(self):
        h = harness(response(200, {"timeZone": "Not/AZone"}), response(200, events_page()))
        h.call("GOOGLECALENDAR_EVENTS_LIST", {"timeMin": "2026-11-02"})
        assert h.session.calls[1].params["timeMin"] == "2026-11-02T00:00:00Z"


class TestEventTimes:
    def _create(self, args, *answers):
        h = harness(*answers, response(200, event()))
        result = h.call("GOOGLECALENDAR_CREATE_EVENT", {"summary": "x", "create_meeting_room": False, **args})
        assert result.successful, result.error
        return h, h.session.last.json

    def test_naive_start_in_the_given_zone(self):
        h, body = self._create({"start_datetime": "2026-11-02T09:30", "event_duration_hour": 1, "timezone": "Europe/London"})
        assert body["start"] == {"dateTime": "2026-11-02T09:30:00", "timeZone": "Europe/London"}
        assert body["end"] == {"dateTime": "2026-11-02T10:30:00", "timeZone": "Europe/London"}
        assert len(h.session.calls) == 1

    def test_naive_start_without_a_zone_uses_the_calendars(self):
        _, body = self._create({"start_datetime": "2026-11-02 14:00:00"}, zone_reply("Australia/Sydney"))
        assert body["start"] == {"dateTime": "2026-11-02T14:00:00", "timeZone": "Australia/Sydney"}
        assert body["end"] == {"dateTime": "2026-11-02T14:30:00", "timeZone": "Australia/Sydney"}  # 30 min default

    def test_offset_start_needs_no_zone(self):
        h, body = self._create({"start_datetime": "2026-11-02T09:00:00-05:00", "end_datetime": "2026-11-02T15:00:00Z"})
        assert body["start"] == {"dateTime": "2026-11-02T09:00:00-05:00"}
        assert body["end"] == {"dateTime": "2026-11-02T15:00:00Z"}
        assert len(h.session.calls) == 1

    def test_duration_is_wall_clock_across_a_dst_change(self):
        # 23:00 + 4h on the night clocks spring forward: 03:00 local (3 real hours).
        _, body = self._create({"start_datetime": "2026-03-07T23:00", "event_duration_hour": 4, "timezone": "America/New_York"})
        assert body["end"] == {"dateTime": "2026-03-08T03:00:00", "timeZone": "America/New_York"}

    def test_mixed_naive_and_offset_end(self):
        # 09:00 New York (-05:00) to 15:00Z (10:00 New York): one hour, valid.
        _, body = self._create({"start_datetime": "2026-11-02T09:00", "end_datetime": "2026-11-02T15:00:00Z", "timezone": "America/New_York"})
        assert body["start"] == {"dateTime": "2026-11-02T09:00:00", "timeZone": "America/New_York"}
        assert body["end"] == {"dateTime": "2026-11-02T15:00:00Z", "timeZone": "America/New_York"}

    def test_mixed_times_that_end_before_they_start(self):
        h = harness()
        result = h.call("GOOGLECALENDAR_CREATE_EVENT", {"start_datetime": "2026-11-02T11:00", "end_datetime": "2026-11-02T15:00:00Z", "timezone": "America/New_York"})
        assert result.error_code == "invalid_arguments"  # 11:00 NY is 16:00Z

    def test_all_day(self):
        _, body = self._create({"start_date": "2026-12-24"})
        assert (body["start"], body["end"]) == ({"date": "2026-12-24"}, {"date": "2026-12-25"})

    def test_all_day_same_end_is_one_day_and_multi_day_is_exclusive(self):
        _, one = self._create({"start_date": "2026-12-24", "end_date": "2026-12-24"})
        _, many = self._create({"start_date": "2026-12-24", "end_date": "2026-12-27"})
        assert one["end"] == {"date": "2026-12-25"}
        assert many["end"] == {"date": "2026-12-27"}

    def test_all_day_with_a_zone(self):
        _, body = self._create({"start_date": "2026-12-24", "timezone": "Europe/Paris"})
        assert body["start"] == {"date": "2026-12-24", "timeZone": "Europe/Paris"}


class TestRecurringEvents:
    def test_weekly_series_carries_the_rule_and_a_zone(self):
        h = harness(response(200, event()))
        h.call(
            "GOOGLECALENDAR_CREATE_EVENT",
            {"summary": "1:1", "start_datetime": "2026-11-02T09:00", "timezone": "America/New_York", "recurrence": ["RRULE:FREQ=WEEKLY;BYDAY=MO;COUNT=10"]},
        )
        body = h.session.last.json
        assert body["recurrence"] == ["RRULE:FREQ=WEEKLY;BYDAY=MO;COUNT=10"]
        # Wall-clock time + IANA zone: every Monday stays 09:00 New York, across DST.
        assert body["start"] == {"dateTime": "2026-11-02T09:00:00", "timeZone": "America/New_York"}

    def test_offset_start_of_a_series_gets_the_calendars_zone(self):
        """Google needs a time zone to expand a recurring event."""
        h = harness(zone_reply("Europe/Berlin"), response(200, event()))
        h.call("GOOGLECALENDAR_CREATE_EVENT", {"start_datetime": "2026-11-02T09:00:00+01:00", "recurrence": "FREQ=DAILY;COUNT=5", "create_meeting_room": False})
        body = h.session.last.json
        assert body["start"] == {"dateTime": "2026-11-02T09:00:00+01:00", "timeZone": "Europe/Berlin"}
        assert body["recurrence"] == ["RRULE:FREQ=DAILY;COUNT=5"]

    def test_exceptions_and_extra_dates(self):
        h = harness(response(200, event()))
        rules = ["RRULE:FREQ=WEEKLY;BYDAY=FR", "EXDATE;TZID=America/New_York:20261127T090000", "RDATE;VALUE=DATE:20261201", ""]
        h.call("GOOGLECALENDAR_CREATE_EVENT", {"start_datetime": "2026-11-06T09:00", "timezone": "America/New_York", "recurrence": rules})
        assert h.session.last.json["recurrence"] == rules[:3]

    def test_updating_the_rule_of_a_zoneless_series_adds_a_zone(self):
        current = event("wk1", start="2026-11-02T09:00:00-05:00", end="2026-11-02T09:30:00-05:00", zone=None)
        h = harness(response(200, current), zone_reply("America/Chicago"), response(200, current))
        h.call("GOOGLECALENDAR_UPDATE_EVENT", {"event_id": "wk1", "recurrence": ["RRULE:FREQ=WEEKLY;COUNT=3"]})
        patch = h.session.last.json
        assert patch["recurrence"] == ["RRULE:FREQ=WEEKLY;COUNT=3"]
        assert patch["start"] == {"dateTime": "2026-11-02T09:00:00-05:00", "timeZone": "America/Chicago"}
        assert patch["end"] == {"dateTime": "2026-11-02T09:30:00-05:00", "timeZone": "America/Chicago"}

    def test_empty_rule_makes_the_event_one_off(self):
        h = harness(response(200, event("wk1", recurrence=["RRULE:FREQ=DAILY"])), response(200, event("wk1")))
        h.call("GOOGLECALENDAR_UPDATE_EVENT", {"event_id": "wk1", "recurrence": []})
        assert h.session.last.json == {"recurrence": []}

    def test_one_instance_can_be_moved(self):
        instance = event("wk1_20261109T140000Z", recurringEventId="wk1")
        h = harness(response(200, instance), response(200, instance))
        h.call("GOOGLECALENDAR_UPDATE_EVENT", {"event_id": "wk1_20261109T140000Z", "start_datetime": "2026-11-09T11:00:00-05:00"})
        assert h.session.last.path == "/calendars/primary/events/wk1_20261109T140000Z"
        assert h.session.last.json["end"]["dateTime"] == "2026-11-09T11:30:00-05:00"  # length kept


class TestCurrentDateTime:
    @pytest.fixture(autouse=True)
    def frozen(self, monkeypatch):
        moment = datetime.datetime(2026, 11, 2, 14, 5, 9, tzinfo=datetime.UTC)
        monkeypatch.setattr(times, "now", lambda tz: moment.astimezone(tz))

    def test_named_zone(self):
        h = harness()
        data = h.call("GOOGLECALENDAR_GET_CURRENT_DATE_TIME", {"timezone": "Asia/Kolkata"}).data
        assert data == {
            "current_datetime": "2026-11-02T19:35:09+05:30",
            "timezone": "Asia/Kolkata",
            "date": "2026-11-02",
            "time": "19:35:09",
            "weekday": "Monday",
            "utc": "2026-11-02T14:05:09Z",
        }
        assert h.session.calls == []

    @pytest.mark.parametrize(("offset", "name", "local"), [(0, "UTC", "2026-11-02T14:05:09Z"), (-5, "UTC-05:00", "2026-11-02T09:05:09-05:00"), (5.5, "UTC+05:30", "2026-11-02T19:35:09+05:30")])
    def test_hour_offsets(self, offset, name, local):
        data = harness().call("GOOGLECALENDAR_GET_CURRENT_DATE_TIME", {"timezone": offset}).data
        assert (data["timezone"], data["current_datetime"]) == (name, local)

    def test_default_is_the_primary_calendars_zone(self):
        h = harness(zone_reply("Pacific/Kiritimati"))
        data = h.call("GOOGLECALENDAR_GET_CURRENT_DATE_TIME", {}).data
        assert (data["timezone"], data["date"], data["weekday"]) == ("Pacific/Kiritimati", "2026-11-03", "Tuesday")
