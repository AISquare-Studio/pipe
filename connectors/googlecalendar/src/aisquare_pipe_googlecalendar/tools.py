"""The Google Calendar toolkit's catalog: one :class:`ToolSpec` plus one handler per tool.

Slugs and argument names are Composio's Google Calendar toolkit's, verbatim
— including its mixed casing (``GOOGLECALENDAR_EVENTS_LIST`` takes
``calendarId`` / ``timeMin``; ``GOOGLECALENDAR_FIND_EVENT`` takes
``calendar_id`` / ``time_min``) — so callers written against Composio keep
working. The other casing of any argument is accepted too; see
:func:`apply_aliases`.

Times (see :mod:`aisquare_pipe_googlecalendar.times`): a time with an offset
is an instant; a time without one is wall-clock time in ``timezone`` (or
``timeZone``) when given, else in the calendar's own zone, looked up with
one extra request. A bare date is midnight, or an all-day event's day.

Result shapes (``ToolResult.data``):

* events and calendars are Google's resources (``kind`` / ``etag`` dropped);
* list endpoints return ``{"has_more", "next_page_token", ..., "items": [...]}``
  — one page per call; pass ``next_page_token`` back as the page-token
  argument (``pageToken`` or ``page_token``) to continue;
* writes return the event; a delete returns ``{"deleted": true, ...}``.

Scopes: every tool lists the OAuth scopes it needs in
``ToolSpec.required_scopes`` (also :attr:`GoogleCalendarTool.scopes`) and
its description. The default
connection is read-only (``calendar.readonly``); creating, changing and
deleting events needs ``calendar.events``.
"""

from __future__ import annotations

import datetime
import re
import uuid
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any
from zoneinfo import ZoneInfo

from aisquare.pipe import ERROR_INVALID_ARGUMENTS, ToolError, ToolResult, ToolSpec

from aisquare_pipe_googlecalendar import times
from aisquare_pipe_googlecalendar.errors import describe_error, error_code
from aisquare_pipe_googlecalendar.rest import (
    GoogleCalendarAPIError,
    GoogleCalendarRestClient,
    json_object,
    quote_segment,
)

Handler = Callable[[GoogleCalendarRestClient, dict[str, Any]], ToolResult]

SCOPE_BASE = "https://www.googleapis.com/auth/"
#: See every calendar the user can access, its events and free/busy. Sensitive.
READONLY_SCOPE = SCOPE_BASE + "calendar.readonly"
#: View and edit events on every calendar the user can access. Sensitive.
EVENTS_SCOPE = SCOPE_BASE + "calendar.events"
#: Full access, calendars and sharing included. No tool asks for it.
FULL_SCOPE = SCOPE_BASE + "calendar"
#: The broader scopes Google accepts in place of each scope a tool lists.
COVERING_SCOPES: dict[str, frozenset[str]] = {
    READONLY_SCOPE: frozenset({FULL_SCOPE}),
    EVENTS_SCOPE: frozenset({FULL_SCOPE}),
}

DEFAULT_EVENTS_PAGE = 50
MAX_EVENTS_PAGE = 2500
DEFAULT_CALENDARS_PAGE = 100
MAX_CALENDARS_PAGE = 250
#: Events per calendar for GOOGLECALENDAR_EVENTS_LIST_ALL_CALENDARS.
DEFAULT_PER_CALENDAR = 50
#: Calendars GOOGLECALENDAR_EVENTS_LIST_ALL_CALENDARS reads at most.
MAX_CALENDARS = 50
#: Calendars (or groups) one free/busy query may name (Google's limit).
MAX_FREEBUSY_ITEMS = 50
#: Event length when neither an end nor a duration is given (Composio's default).
DEFAULT_DURATION = datetime.timedelta(minutes=30)

_RECURRENCE_PREFIX = re.compile(r"^(RRULE|EXRULE|RDATE|EXDATE)[:;]", re.IGNORECASE)
_EMAIL = re.compile(r"^[^@\s<>,;\"]+@[^@\s<>,;\"]+$")
_SPECIAL_EVENT_TYPES = frozenset({"focusTime", "outOfOffice", "workingLocation", "birthday"})
_TYPE_PROPERTIES = {
    "focusTime": "focusTimeProperties",
    "outOfOffice": "outOfOfficeProperties",
    "workingLocation": "workingLocationProperties",
    "birthday": "birthdayProperties",
}


@dataclass(frozen=True)
class GoogleCalendarTool:
    spec: ToolSpec
    handler: Handler
    #: OAuth scopes the tool needs (full URLs).
    scopes: tuple[str, ...]
    #: Extra argument spellings accepted for this tool (alias -> schema name).
    aliases: Mapping[str, str] = field(default_factory=dict)


# --------------------------------------------------------------------------
# Schema helpers
# --------------------------------------------------------------------------


def _schema(properties: dict[str, Any], required: Iterable[str] = ()) -> dict[str, Any]:
    return {"type": "object", "properties": properties, "required": list(required)}


def _string(description: str, *, enum: Iterable[str] | None = None, default: str | None = None) -> dict[str, Any]:
    schema: dict[str, Any] = {"type": "string", "description": description}
    if enum is not None:
        schema["enum"] = list(enum)
    if default is not None:
        schema["default"] = default
    return schema


def _id(description: str) -> dict[str, Any]:
    return {**_string(description), "minLength": 1}


def _integer(description: str, *, default: int | None = None, maximum: int | None = None, minimum: int = 1) -> dict[str, Any]:
    schema: dict[str, Any] = {"type": "integer", "minimum": minimum, "description": description}
    if maximum is not None:
        schema["maximum"] = maximum
    if default is not None:
        schema["default"] = default
    return schema


def _boolean(description: str, *, default: bool | None = None) -> dict[str, Any]:
    schema: dict[str, Any] = {"type": "boolean", "description": description}
    if default is not None:
        schema["default"] = default
    return schema


def _strings(description: str) -> dict[str, Any]:
    return {"type": "array", "items": {"type": "string"}, "description": description}


CALENDAR_ID = _string(
    "Calendar id: 'primary' (default, the user's own) or an id from GOOGLECALENDAR_LIST_CALENDARS "
    "(often an email address).",
    default="primary",
)
EVENT_ID = _id("The event's id (an instance of a recurring event has its own id, like abc123_20261102T090000Z).")
PAGE_TOKEN = _string("The previous result's next_page_token, to fetch the next page.")
TIME_BOUND = (
    "RFC 3339 time, e.g. 2026-11-02T09:00:00-05:00 or ...Z; a time without an offset or a bare "
    "date (2026-11-02) is read in timeZone/timezone, else the calendar's own zone."
)
ZONE = "IANA time zone, e.g. 'Europe/London'"
SEND_UPDATES = _string(
    "Who Google emails about the change: all, externalOnly or none (Google's default: none).",
    enum=("all", "externalOnly", "none"),
)
EVENT_TYPES = _strings("Only these event types: default, focusTime, outOfOffice, workingLocation, birthday, fromGmail.")
VISIBILITY = _string("Who sees the details.", enum=("default", "public", "private", "confidential"))
TRANSPARENCY = _string("opaque = shows as busy; transparent = shows as free.", enum=("opaque", "transparent"))
EVENT_TYPE = _string(
    "Kind of event (fixed once created).", enum=("default", "birthday", "focusTime", "outOfOffice", "workingLocation")
)
ATTENDEES = {
    "type": "array",
    "items": {"type": ["string", "object"]},
    "description": "Guests: email addresses, or objects {email, displayName?, optional?}.",
}
RECURRENCE = {
    "type": ["array", "string"],
    "items": {"type": "string"},
    "description": "RFC 5545 rules, e.g. ['RRULE:FREQ=WEEKLY;BYDAY=MO,WE;COUNT=10'] (EXDATE/RDATE lines too); "
    "an empty list makes the event one-off.",
}
EXTENDED_PROPERTIES = {
    "type": "object",
    "description": "Hidden key/value data: {private: {...}, shared: {...}} (string values).",
}


# --------------------------------------------------------------------------
# Argument helpers
# --------------------------------------------------------------------------


def _camel(name: str) -> str:
    head, *rest = name.split("_")
    return head + "".join(part[:1].upper() + part[1:] for part in rest)


def _snake(name: str) -> str:
    return re.sub(r"(?<=[a-z0-9])([A-Z])", r"_\1", name).lower()


def apply_aliases(
    spec: ToolSpec, arguments: Mapping[str, Any], aliases: Mapping[str, str] | None = None
) -> dict[str, Any]:
    """Rename argument spellings the schema does not use onto the ones it
    does: the tool's explicit ``aliases``, then the other casing of any key
    (``calendar_id`` <-> ``calendarId``). A key already present wins over its alias."""
    properties = spec.input_schema.get("properties")
    names = set(properties) if isinstance(properties, Mapping) else set()
    args = dict(arguments)
    for key in list(args):
        if key in names:
            continue
        target = (aliases or {}).get(key)
        if target is None:
            alternate = _camel(key) if "_" in key else _snake(key)
            target = alternate if alternate in names else None
        if target is not None and target not in args:
            args[target] = args.pop(key)
    return args


def _invalid(message: str) -> ToolError:
    return ToolError(message, code=ERROR_INVALID_ARGUMENTS)


def _segment(value: Any, key: str, default: str | None = None) -> str:
    """``value`` (or ``default``) as one URL path segment; '.'/'..' and empty
    ids are refused."""
    text = value.strip() if isinstance(value, str) else ""
    if not text:
        if default is None:
            raise _invalid(f"{key} is required")
        text = default
    try:
        return quote_segment(text)
    except ValueError:
        raise _invalid(f"{key} must be a calendar or event id, not empty, '.' or '..'") from None


def _calendar(args: dict[str, Any], key: str) -> str:
    return _segment(args.get(key), key, "primary")


def _zone_arg(args: dict[str, Any], key: str) -> str | None:
    value = args.get(key)
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    try:
        return times.zone(str(value)).key
    except ValueError as e:
        raise _invalid(f"{key}: {e}") from None


def _calendar_zone(client: GoogleCalendarRestClient, calendar: str) -> str:
    """The calendar's own IANA zone (UTC if Google names none)."""
    data = json_object(client.get(f"/calendars/{calendar}", {"fields": "timeZone"}))
    name = data.get("timeZone")
    try:
        return times.zone(str(name)).key if name else "UTC"
    except ValueError:
        return "UTC"


def _zone_for(
    client: GoogleCalendarRestClient, args: dict[str, Any], key: str, calendar: str, *values: str | None
) -> str | None:
    """The zone to read ``values`` in: the caller's ``key`` argument, else —
    when one of them is a date or has no offset — the calendar's zone."""
    given = _zone_arg(args, key)
    if given or not times.needs_zone(*values):
        return given
    return _calendar_zone(client, calendar)


def _bound(args: dict[str, Any], key: str, tz_name: str | None) -> str | None:
    value = args.get(key)
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return times.instant(value, ZoneInfo(tz_name) if tz_name else None)
    except ValueError as e:
        raise _invalid(f"{key}: {e}") from None


def _check_range(lower: str | None, upper: str | None, names: tuple[str, str]) -> None:
    if lower and upper and times.parse_datetime(lower) >= times.parse_datetime(upper):
        raise _invalid(f"{names[0]} must be before {names[1]}")


def _page_size(value: Any, default: int, maximum: int) -> int:
    if value is None:
        return default
    return min(max(1, int(value)), maximum)


def _flag(value: Any) -> str:
    return "true" if value else "false"


def _put(params: dict[str, Any], key: str, value: Any) -> None:
    if value is None or value == "" or value == []:
        return
    params[key] = _flag(value) if isinstance(value, bool) else value


# --------------------------------------------------------------------------
# Response helpers
# --------------------------------------------------------------------------


def slim(value: Any) -> Any:
    """Drop Google's ``kind`` / ``etag`` markers: noise for an LLM."""
    if isinstance(value, dict):
        return {k: slim(v) for k, v in value.items() if k not in ("kind", "etag")}
    if isinstance(value, list):
        return [slim(v) for v in value]
    return value


def listing(data: Mapping[str, Any], **extra: Any) -> dict[str, Any]:
    """One page: pagination hints first (they survive a truncated result), then ``items``."""
    token = data.get("nextPageToken")
    out: dict[str, Any] = {"has_more": bool(token), "next_page_token": token or None}
    out.update({k: v for k, v in extra.items() if v is not None})
    if data.get("nextSyncToken"):
        out["next_sync_token"] = data["nextSyncToken"]
    items = data.get("items")
    out["items"] = slim(items) if isinstance(items, list) else []
    return out


# --------------------------------------------------------------------------
# Handlers: calendars
# --------------------------------------------------------------------------


def _list_calendars(client: GoogleCalendarRestClient, args: dict[str, Any]) -> ToolResult:
    params: dict[str, Any] = {
        "maxResults": _page_size(args.get("max_results"), DEFAULT_CALENDARS_PAGE, MAX_CALENDARS_PAGE)
    }
    _put(params, "pageToken", args.get("page_token"))
    _put(params, "syncToken", args.get("sync_token"))
    _put(params, "showHidden", args.get("show_hidden"))
    _put(params, "showDeleted", args.get("show_deleted"))
    _put(params, "minAccessRole", args.get("min_access_role"))
    return ToolResult.ok(listing(json_object(client.get("/users/me/calendarList", params))))


def _get_calendar(client: GoogleCalendarRestClient, args: dict[str, Any]) -> ToolResult:
    return ToolResult.ok(slim(json_object(client.get(f"/calendars/{_calendar(args, 'calendar_id')}"))))


def _current_date_time(client: GoogleCalendarRestClient, args: dict[str, Any]) -> ToolResult:
    value = args.get("timezone")
    tz: datetime.tzinfo
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if not -14 <= value <= 14:
            raise _invalid("timezone offset must be between -14 and 14 hours")
        offset = datetime.timedelta(hours=float(value))
        tz = datetime.timezone(offset)
        minutes = abs(int(offset.total_seconds() // 60))
        sign = "-" if offset < datetime.timedelta(0) else "+"
        name = f"UTC{sign}{minutes // 60:02d}:{minutes % 60:02d}" if minutes else "UTC"
    else:
        name = _zone_arg(args, "timezone") or _calendar_zone(client, "primary")
        tz = ZoneInfo(name)
    current = times.now(tz)
    return ToolResult.ok(
        {
            "current_datetime": times.rfc3339(current),
            "timezone": name,
            "date": current.date().isoformat(),
            "time": current.time().isoformat(timespec="seconds"),
            "weekday": current.strftime("%A"),
            "utc": times.rfc3339(current.astimezone(datetime.UTC)),
        }
    )


# --------------------------------------------------------------------------
# Handlers: events (read)
# --------------------------------------------------------------------------


@dataclass
class _EventQuery:
    """The options of an events.list call, whatever the tool's argument casing."""

    calendar: str
    time_min: str | None = None
    time_max: str | None = None
    time_zone: str | None = None
    query: str | None = None
    max_results: Any = None
    page_token: str | None = None
    single_events: bool | None = None
    order_by: str | None = None
    show_deleted: bool | None = None
    updated_min: str | None = None
    event_types: list[str] | None = None
    sync_token: str | None = None
    ical_uid: str | None = None
    max_attendees: int | None = None
    extra: dict[str, Any] = field(default_factory=dict)


def _list_events(client: GoogleCalendarRestClient, q: _EventQuery, names: Mapping[str, str]) -> ToolResult:
    """events.list with Calendar's rules applied up front: ``orderBy=startTime``
    needs expanded recurring events, a sync token excludes filters, bounds are
    RFC 3339 (resolved in the right zone) and ordered."""
    if q.sync_token and any((q.time_min, q.time_max, q.query, q.order_by, q.updated_min, q.ical_uid)):
        raise _invalid(f"{names['sync_token']} cannot be combined with time bounds, a query, an order or {names['ical_uid']}")
    if q.order_by == "startTime":
        if q.single_events is False:
            raise _invalid(
                f"{names['order_by']}=startTime needs {names['single_events']}=true (recurring events expanded)"
            )
        q.single_events = True
    zone_name = q.time_zone
    if not zone_name and times.needs_zone(q.time_min, q.time_max, q.updated_min):
        zone_name = _calendar_zone(client, q.calendar)
    bounds = {}
    for key in ("time_min", "time_max", "updated_min"):
        bounds[key] = _bound({key: getattr(q, key)}, key, zone_name)
    _check_range(bounds["time_min"], bounds["time_max"], (names["time_min"], names["time_max"]))
    params: dict[str, Any] = {"maxResults": _page_size(q.max_results, DEFAULT_EVENTS_PAGE, MAX_EVENTS_PAGE)}
    _put(params, "timeMin", bounds["time_min"])
    _put(params, "timeMax", bounds["time_max"])
    _put(params, "updatedMin", bounds["updated_min"])
    _put(params, "timeZone", q.time_zone)
    _put(params, "q", q.query.strip() if isinstance(q.query, str) else None)
    _put(params, "pageToken", q.page_token)
    _put(params, "singleEvents", q.single_events)
    _put(params, "orderBy", q.order_by)
    _put(params, "showDeleted", q.show_deleted)
    _put(params, "eventTypes", q.event_types)
    _put(params, "syncToken", q.sync_token)
    _put(params, "iCalUID", q.ical_uid)
    _put(params, "maxAttendees", q.max_attendees)
    for key, value in q.extra.items():
        _put(params, key, value)
    data = json_object(client.get(f"/calendars/{q.calendar}/events", params))
    return ToolResult.ok(listing(data, time_zone=data.get("timeZone"), summary=data.get("summary")))


_CAMEL_NAMES = {
    "sync_token": "syncToken",
    "ical_uid": "iCalUID",
    "order_by": "orderBy",
    "single_events": "singleEvents",
    "time_min": "timeMin",
    "time_max": "timeMax",
}
_SNAKE_NAMES = {key: key for key in _CAMEL_NAMES}


def _events_list(client: GoogleCalendarRestClient, args: dict[str, Any]) -> ToolResult:
    query = _EventQuery(
        calendar=_calendar(args, "calendarId"),
        time_min=args.get("timeMin"),
        time_max=args.get("timeMax"),
        time_zone=_zone_arg(args, "timeZone"),
        query=args.get("query"),
        max_results=args.get("maxResults"),
        page_token=args.get("pageToken"),
        single_events=args.get("singleEvents"),
        order_by=args.get("orderBy"),
        show_deleted=args.get("showDeleted"),
        updated_min=args.get("updatedMin"),
        event_types=args.get("eventTypes"),
        sync_token=args.get("syncToken"),
        ical_uid=args.get("iCalUID"),
        max_attendees=args.get("maxAttendees"),
        extra={
            "showHiddenInvitations": args.get("showHiddenInvitations"),
            "privateExtendedProperty": args.get("privateExtendedProperty"),
            "sharedExtendedProperty": args.get("sharedExtendedProperty"),
        },
    )
    return _list_events(client, query, _CAMEL_NAMES)


def _find_event(client: GoogleCalendarRestClient, args: dict[str, Any]) -> ToolResult:
    query = _EventQuery(
        calendar=_calendar(args, "calendar_id"),
        time_min=args.get("time_min"),
        time_max=args.get("time_max"),
        time_zone=_zone_arg(args, "timezone"),
        query=args.get("query"),
        max_results=args.get("max_results"),
        page_token=args.get("page_token"),
        single_events=args.get("single_events", True),
        order_by=args.get("order_by") or ("startTime" if args.get("single_events", True) else None),
        show_deleted=args.get("show_deleted"),
        updated_min=args.get("updated_min"),
        event_types=args.get("event_types"),
    )
    return _list_events(client, query, _SNAKE_NAMES)


def _events_get(client: GoogleCalendarRestClient, args: dict[str, Any]) -> ToolResult:
    params: dict[str, Any] = {}
    _put(params, "timeZone", _zone_arg(args, "time_zone"))
    _put(params, "maxAttendees", args.get("max_attendees"))
    path = f"/calendars/{_calendar(args, 'calendar_id')}/events/{_segment(args.get('event_id'), 'event_id')}"
    return ToolResult.ok(slim(json_object(client.get(path, params))))


def _events_instances(client: GoogleCalendarRestClient, args: dict[str, Any]) -> ToolResult:
    calendar = _calendar(args, "calendarId")
    event = _segment(args.get("eventId"), "eventId")
    time_zone = _zone_arg(args, "timeZone")
    zone_name = time_zone
    if not zone_name and times.needs_zone(args.get("timeMin"), args.get("timeMax")):
        zone_name = _calendar_zone(client, calendar)
    time_min = _bound(args, "timeMin", zone_name)
    time_max = _bound(args, "timeMax", zone_name)
    _check_range(time_min, time_max, ("timeMin", "timeMax"))
    params: dict[str, Any] = {"maxResults": _page_size(args.get("maxResults"), DEFAULT_EVENTS_PAGE, MAX_EVENTS_PAGE)}
    _put(params, "timeMin", time_min)
    _put(params, "timeMax", time_max)
    _put(params, "timeZone", time_zone)
    _put(params, "pageToken", args.get("pageToken"))
    _put(params, "showDeleted", args.get("showDeleted"))
    _put(params, "maxAttendees", args.get("maxAttendees"))
    _put(params, "originalStart", args.get("originalStart"))
    data = json_object(client.get(f"/calendars/{calendar}/events/{event}/instances", params))
    return ToolResult.ok(listing(data, time_zone=data.get("timeZone")))


def _start_key(event: Mapping[str, Any], tz: ZoneInfo) -> datetime.datetime:
    start = event.get("start") or {}
    try:
        if start.get("dateTime"):
            moment = times.parse_datetime(str(start["dateTime"]))
            return moment if moment.tzinfo else moment.replace(tzinfo=tz)
        if start.get("date"):
            return datetime.datetime.combine(times.parse_date(str(start["date"])), datetime.time(), tz)
    except ValueError:
        pass
    return datetime.datetime.max.replace(tzinfo=datetime.UTC)


_MINIMAL_EVENT_KEYS = ("id", "summary", "start", "end", "status", "location", "htmlLink", "recurringEventId", "eventType")


def _list_all_calendars(client: GoogleCalendarRestClient, args: dict[str, Any]) -> ToolResult:
    zone_name = _zone_arg(args, "timezone")
    for key in ("time_min", "time_max"):
        _bound(args, key, None)  # refuse a malformed time before any request
    requested = [c.strip() for c in args.get("calendar_ids") or [] if isinstance(c, str) and c.strip()]
    calendars: list[dict[str, Any]]
    primary_zone: str | None = None
    if requested:
        calendars = [{"id": c} for c in dict.fromkeys(requested)]
    else:
        calendars = []
        token: str | None = None
        while len(calendars) < MAX_CALENDARS:
            params: dict[str, Any] = {"maxResults": MAX_CALENDARS_PAGE}
            _put(params, "pageToken", token)
            page = json_object(client.get("/users/me/calendarList", params))
            calendars.extend(c for c in page.get("items") or [] if isinstance(c, dict) and c.get("id"))
            token = page.get("nextPageToken")
            if not token:
                break
        for entry in calendars:
            if entry.get("primary") and entry.get("timeZone"):
                primary_zone = str(entry["timeZone"])
    if len(calendars) > MAX_CALENDARS:
        if requested:
            raise _invalid(f"calendar_ids may name at most {MAX_CALENDARS} calendars")
        calendars = calendars[:MAX_CALENDARS]
    if not zone_name:
        try:
            zone_name = times.zone(primary_zone).key if primary_zone else _calendar_zone(client, "primary")
        except ValueError:
            zone_name = "UTC"
    tz = ZoneInfo(zone_name)
    time_min = _bound(args, "time_min", zone_name)
    time_max = _bound(args, "time_max", zone_name)
    _check_range(time_min, time_max, ("time_min", "time_max"))
    single = args.get("single_events", True) is not False
    full = args.get("response_detail") == "full"
    per_calendar = _page_size(args.get("max_results_per_calendar"), DEFAULT_PER_CALENDAR, MAX_EVENTS_PAGE)
    items: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    truncated: list[str] = []
    for entry in calendars:
        calendar_id = str(entry["id"])
        params = {"timeMin": time_min, "timeMax": time_max, "maxResults": per_calendar, "timeZone": zone_name}
        _put(params, "singleEvents", single)
        _put(params, "orderBy", "startTime" if single else None)
        _put(params, "q", args.get("q"))
        _put(params, "showDeleted", args.get("show_deleted"))
        _put(params, "eventTypes", args.get("event_types"))
        try:
            data = json_object(client.get(f"/calendars/{_segment(calendar_id, 'calendar_ids')}/events", params))
        except GoogleCalendarAPIError as e:
            if e.status not in (403, 404):
                raise
            errors.append({"calendarId": calendar_id, "error_code": error_code(e), "error": describe_error(e)})
            continue
        if data.get("nextPageToken"):
            truncated.append(calendar_id)
        name = entry.get("summaryOverride") or entry.get("summary") or data.get("summary")
        for event in data.get("items") or []:
            if not isinstance(event, dict):
                continue
            shown = slim(event) if full else {k: event[k] for k in _MINIMAL_EVENT_KEYS if k in event}
            shown["calendarId"] = calendar_id
            if name:
                shown["calendarSummary"] = name
            items.append(shown)
    items.sort(key=lambda e: _start_key(e, tz))
    return ToolResult.ok(
        {
            "has_more": bool(truncated),
            "next_page_token": None,
            "time_zone": zone_name,
            "time_min": time_min,
            "time_max": time_max,
            "calendar_count": len(calendars),
            "truncated_calendars": truncated,
            "errors": errors,
            "items": items,
        }
    )


# --------------------------------------------------------------------------
# Handlers: free/busy
# --------------------------------------------------------------------------


def _freebusy_items(value: Any, key: str) -> list[dict[str, str]]:
    items: list[dict[str, str]] = []
    for index, item in enumerate(value or []):
        if isinstance(item, Mapping):
            item = item.get("id")
        if not isinstance(item, str) or not item.strip():
            raise _invalid(f"{key}[{index}] must be a calendar id (or {{'id': ...}})")
        items.append({"id": item.strip()})
    if len(items) > MAX_FREEBUSY_ITEMS:
        raise _invalid(f"{key} may name at most {MAX_FREEBUSY_ITEMS} calendars")
    return items


def _freebusy(
    client: GoogleCalendarRestClient,
    items: list[dict[str, str]],
    time_min: str,
    time_max: str,
    zone_name: str,
    groups: Any,
    calendars: Any,
) -> dict[str, Any]:
    body: dict[str, Any] = {"timeMin": time_min, "timeMax": time_max, "timeZone": zone_name, "items": items}
    if groups is not None:
        body["groupExpansionMax"] = groups
    if calendars is not None:
        body["calendarExpansionMax"] = calendars
    return slim(json_object(client.send("POST", "/freeBusy", json=body)))


def _free_busy_query(client: GoogleCalendarRestClient, args: dict[str, Any]) -> ToolResult:
    items = _freebusy_items(args.get("items"), "items")
    if not items:
        raise _invalid("items must name at least one calendar")
    zone_name = _zone_for(client, args, "timeZone", "primary", args.get("timeMin"), args.get("timeMax")) or "UTC"
    time_min = _bound(args, "timeMin", zone_name)
    time_max = _bound(args, "timeMax", zone_name)
    if not time_min or not time_max:
        raise _invalid("timeMin and timeMax are required")
    _check_range(time_min, time_max, ("timeMin", "timeMax"))
    data = _freebusy(
        client, items, time_min, time_max, zone_name, args.get("groupExpansionMax"), args.get("calendarExpansionMax")
    )
    return ToolResult.ok(data)


def _merge(intervals: list[tuple[datetime.datetime, datetime.datetime]]) -> list[tuple[datetime.datetime, datetime.datetime]]:
    merged: list[tuple[datetime.datetime, datetime.datetime]] = []
    for start, end in sorted(intervals):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def _find_free_slots(client: GoogleCalendarRestClient, args: dict[str, Any]) -> ToolResult:
    items = _freebusy_items(args.get("items") or ["primary"], "items")
    zone_name = _zone_arg(args, "timezone") or _calendar_zone(client, "primary")
    tz = ZoneInfo(zone_name)
    lower = _bound(args, "time_min", zone_name) or times.rfc3339(times.now(tz))
    upper = _bound(args, "time_max", zone_name) or times.rfc3339(times.parse_datetime(lower) + datetime.timedelta(days=1))
    _check_range(lower, upper, ("time_min", "time_max"))
    data = _freebusy(
        client, items, lower, upper, zone_name, args.get("group_expansion_max"), args.get("calendar_expansion_max")
    )
    window = (times.parse_datetime(lower).astimezone(tz), times.parse_datetime(upper).astimezone(tz))
    busy: list[tuple[datetime.datetime, datetime.datetime]] = []
    found = data.get("calendars")
    calendars: dict[str, Any] = found if isinstance(found, dict) else {}
    for info in calendars.values():
        for block in (info or {}).get("busy") or []:
            try:
                start = times.parse_datetime(str(block["start"])).astimezone(tz)
                end = times.parse_datetime(str(block["end"])).astimezone(tz)
            except (KeyError, ValueError):
                continue
            busy.append((max(start, window[0]), min(end, window[1])))
    merged = [(s, e) for s, e in _merge(busy) if s < e]
    free: list[dict[str, Any]] = []
    cursor = window[0]
    for start, end in [*merged, (window[1], window[1])]:
        if start > cursor:
            free.append(
                {
                    "start": times.rfc3339(cursor),
                    "end": times.rfc3339(start),
                    "duration_minutes": int((start - cursor).total_seconds() // 60),
                }
            )
        cursor = max(cursor, end)
    return ToolResult.ok(
        {
            "timezone": zone_name,
            "time_min": times.rfc3339(window[0]),
            "time_max": times.rfc3339(window[1]),
            "calendars": calendars,
            "busy": [{"start": times.rfc3339(s), "end": times.rfc3339(e)} for s, e in merged],
            "free_slots": free,
        }
    )


# --------------------------------------------------------------------------
# Handlers: events (write)
# --------------------------------------------------------------------------


def _attendees(value: Any) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for index, item in enumerate(value or []):
        entry = {"email": item} if isinstance(item, str) else dict(item) if isinstance(item, Mapping) else {}
        email = str(entry.get("email") or "").strip()
        if not _EMAIL.match(email):
            raise _invalid(f"attendees[{index}] needs an email address")
        entry["email"] = email
        out.append({k: v for k, v in entry.items() if k in ("email", "displayName", "optional", "responseStatus", "comment")})
    return out


def _recurrence(value: Any) -> list[str]:
    lines = [value] if isinstance(value, str) else list(value or [])
    out: list[str] = []
    for index, line in enumerate(lines):
        text = str(line).strip()
        if "\n" in text or "\r" in text:
            raise _invalid(f"recurrence[{index}] must be one line")
        if not text:
            continue
        if text.upper().startswith("FREQ="):
            text = "RRULE:" + text
        if not _RECURRENCE_PREFIX.match(text):
            raise _invalid(f"recurrence[{index}] must be an RRULE, EXRULE, RDATE or EXDATE line")
        out.append(text)
    return out


def _parse(args: dict[str, Any], key: str) -> datetime.datetime | None:
    value = args.get(key)
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return times.parse_datetime(value)
    except ValueError as e:
        raise _invalid(f"{key}: {e}") from None


def _date(args: dict[str, Any], key: str) -> datetime.date | None:
    value = args.get(key)
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return times.parse_date(value)
    except ValueError as e:
        raise _invalid(f"{key}: {e}") from None


def _duration(args: dict[str, Any]) -> datetime.timedelta | None:
    hours = args.get("event_duration_hour")
    minutes = args.get("event_duration_minutes")
    if hours is None and minutes is None:
        return None
    if minutes is not None and not 0 <= minutes <= 59:
        raise _invalid("event_duration_minutes must be 0-59 (use event_duration_hour for hours)")
    span = datetime.timedelta(hours=hours or 0, minutes=minutes or 0)
    if span <= datetime.timedelta(0):
        raise _invalid("the event's duration must be more than 0")
    return span


def _aware(value: datetime.datetime, tz_name: str | None) -> datetime.datetime:
    return value if value.tzinfo else value.replace(tzinfo=ZoneInfo(tz_name or "UTC"))


def _timed(
    start: datetime.datetime,
    end: datetime.datetime | None,
    span: datetime.timedelta | None,
    tz_name: str | None,
) -> tuple[dict[str, str], dict[str, str]]:
    """``start`` / ``end`` bodies for a timed event. Durations are added in
    wall-clock time (a 9:00 one-hour event ends at 10:00 local, across a
    daylight-saving change too)."""
    if end is None:
        end = start + (span or DEFAULT_DURATION)
    # Compared as instants (a naive time read in the event's zone), sent as given.
    if _aware(end, tz_name) <= _aware(start, tz_name):
        raise _invalid("the event must end after it starts")
    return times.event_time(start, tz_name), times.event_time(end, tz_name)


def _all_day(start: datetime.date, end: datetime.date | None) -> tuple[dict[str, str], dict[str, str]]:
    """All-day ``start`` / ``end``: ``end`` is exclusive (Google's rule); an
    end on the start day means that one day."""
    if end is None or end == start:
        end = start + datetime.timedelta(days=1)
    if end < start:
        raise _invalid("end_date must not be before start_date")
    return {"date": start.isoformat()}, {"date": end.isoformat()}


def _event_fields(args: dict[str, Any], body: dict[str, Any]) -> None:
    """The event fields a create or update sets from its arguments."""
    for key in ("summary", "description", "location", "visibility", "transparency"):
        if args.get(key) is not None:
            body[key] = args[key]
    if args.get("attendees") is not None:
        body["attendees"] = _attendees(args["attendees"])
    if args.get("recurrence") is not None:
        body["recurrence"] = _recurrence(args["recurrence"])
    for key in ("guestsCanModify", "guestsCanInviteOthers", "guestsCanSeeOtherGuests"):
        if args.get(key) is not None:
            body[key] = bool(args[key])
    props = args.get("extended_properties")
    if isinstance(props, Mapping) and props:
        body["extendedProperties"] = {k: dict(v) for k, v in props.items() if k in ("private", "shared") and isinstance(v, Mapping)}


def _meet(body: dict[str, Any]) -> None:
    body["conferenceData"] = {
        "createRequest": {"requestId": uuid.uuid4().hex, "conferenceSolutionKey": {"type": "hangoutsMeet"}}
    }


def _write_params(args: dict[str, Any], body: Mapping[str, Any]) -> dict[str, Any]:
    params: dict[str, Any] = {}
    updates = args.get("send_updates")
    if updates is None and args.get("send_notifications") is not None:
        updates = "all" if args["send_notifications"] else "none"
    _put(params, "sendUpdates", updates)
    if "conferenceData" in body:
        params["conferenceDataVersion"] = 1
    return params


def _create_event(client: GoogleCalendarRestClient, args: dict[str, Any]) -> ToolResult:
    calendar = _calendar(args, "calendar_id")
    body: dict[str, Any] = {}
    _event_fields(args, body)
    event_type = args.get("eventType")
    if event_type:
        body["eventType"] = event_type
        prop = _TYPE_PROPERTIES.get(event_type)
        if prop and isinstance(args.get(prop), Mapping):
            body[prop] = dict(args[prop])
    start_date = _date(args, "start_date")
    start = _parse(args, "start_datetime")
    if start is None and start_date is None:
        raise _invalid("give start_datetime (or start_date for an all-day event)")
    if start is not None:
        end = _parse(args, "end_datetime")
        naive = start.tzinfo is None or (end is not None and end.tzinfo is None)
        tz_name = _zone_arg(args, "timezone")
        if not tz_name and (naive or body.get("recurrence")):
            tz_name = _calendar_zone(client, calendar)
        body["start"], body["end"] = _timed(start, end, None if end else _duration(args), tz_name)
    else:
        assert start_date is not None
        body["start"], body["end"] = _all_day(start_date, _date(args, "end_date"))
        tz_name = _zone_arg(args, "timezone")
        if tz_name:
            body["start"]["timeZone"] = body["end"]["timeZone"] = tz_name
    if args.get("create_meeting_room", True) and (event_type or "default") not in _SPECIAL_EVENT_TYPES:
        _meet(body)
    created = client.send("POST", f"/calendars/{calendar}/events", json=body, params=_write_params(args, body))
    return ToolResult.ok(slim(json_object(created)))


def _existing_start(event: Mapping[str, Any]) -> tuple[datetime.datetime | None, datetime.timedelta | None, str | None]:
    """An event's start, its length and its zone (all None for all-day events)."""
    start = event.get("start") or {}
    end = event.get("end") or {}
    if not start.get("dateTime"):
        return None, None, start.get("timeZone")
    try:
        begin = times.parse_datetime(str(start["dateTime"]))
        span = times.parse_datetime(str(end["dateTime"])) - begin if end.get("dateTime") else None
    except (ValueError, TypeError):
        return None, None, start.get("timeZone")
    return begin, span, start.get("timeZone")


def _existing_days(event: Mapping[str, Any]) -> tuple[datetime.date | None, int | None]:
    """An all-day event's first day and its length in days (None, None for a timed one)."""
    start = (event.get("start") or {}).get("date")
    end = (event.get("end") or {}).get("date")
    try:
        first = times.parse_date(str(start)) if start else None
        last = times.parse_date(str(end)) if end else None
    except ValueError:
        return None, None
    return first, (last - first).days if first and last else None


def _clear(moments: tuple[dict[str, Any], dict[str, Any]], *keys: str) -> None:
    """Null ``keys`` in a patch's start/end: Google merges nested objects on
    PATCH, so a field of the event's old form (``date`` when it becomes
    timed, ``dateTime`` / ``timeZone`` when it becomes all-day) would
    otherwise survive next to the new one."""
    for moment in moments:
        for key in keys:
            moment.setdefault(key, None)


def _update_event(client: GoogleCalendarRestClient, args: dict[str, Any]) -> ToolResult:
    calendar = _calendar(args, "calendar_id")
    path = f"/calendars/{calendar}/events/{_segment(args.get('event_id'), 'event_id')}"
    patch: dict[str, Any] = {}
    _event_fields(args, patch)
    new_start = _parse(args, "start_datetime")
    new_end = _parse(args, "end_datetime")
    span = _duration(args)
    new_day = _date(args, "start_date")
    new_last_day = _date(args, "end_date")
    tz_given = _zone_arg(args, "timezone")
    timed = bool(new_start or new_end or span)
    all_day = bool(new_day or new_last_day)
    if timed and all_day:
        raise _invalid(
            "give start_date/end_date (all-day) or start_datetime/end_datetime/a duration (timed), not both"
        )
    if not (patch or timed or all_day or tz_given or args.get("create_meeting_room")):
        raise _invalid("nothing to update: give the fields to change")
    current = json_object(client.get(path))
    old_start, old_span, old_zone = _existing_start(current)
    old_day, old_days = _existing_days(current)
    if timed:
        start = new_start or old_start
        if start is None:
            raise _invalid("the event is all-day; give start_datetime to make it a timed event")
        if new_start and not new_end and not span:
            span = old_span
        tz_name = tz_given or old_zone
        naive = start.tzinfo is None or (new_end is not None and new_end.tzinfo is None)
        if not tz_name and (naive or patch.get("recurrence") or current.get("recurrence")):
            tz_name = _calendar_zone(client, calendar)
        patch["start"], patch["end"] = _timed(start, new_end, span, tz_name)
        if old_day is not None:
            _clear((patch["start"], patch["end"]), "date")
    elif all_day:
        first = new_day or old_day
        if first is None:
            raise _invalid("the event is timed; give start_date to make it an all-day event")
        last = new_last_day
        if last is None and new_day and old_day and old_days:
            last = new_day + datetime.timedelta(days=old_days)  # keep its length in days
        patch["start"], patch["end"] = _all_day(first, last)
        if tz_given:
            patch["start"]["timeZone"] = patch["end"]["timeZone"] = tz_given
        if old_day is None:
            _clear((patch["start"], patch["end"]), "dateTime", "timeZone")
    elif tz_given:
        for key in ("start", "end"):
            moment = dict(current.get(key) or {})
            if moment.get("dateTime"):
                moment["timeZone"] = tz_given
                patch[key] = {k: v for k, v in moment.items() if k in ("dateTime", "timeZone")}
    if patch.get("recurrence") and "start" not in patch and old_start is not None and not old_zone:
        zone_name = tz_given or _calendar_zone(client, calendar)
        for key in ("start", "end"):
            moment = current.get(key) or {}
            patch[key] = {"dateTime": moment.get("dateTime"), "timeZone": zone_name}
    if args.get("create_meeting_room") and not current.get("conferenceData"):
        _meet(patch)
    if not patch:
        raise _invalid("nothing to update: give the fields to change")
    updated = client.send("PATCH", path, json=patch, params=_write_params(args, patch))
    return ToolResult.ok(slim(json_object(updated)))


def _quick_add(client: GoogleCalendarRestClient, args: dict[str, Any]) -> ToolResult:
    text = str(args.get("text") or "").strip()
    if not text:
        raise _invalid("text is required, e.g. 'Lunch with Ann tomorrow 1pm at Cafe Rio'")
    params: dict[str, Any] = {"text": text}
    _put(params, "sendUpdates", args.get("send_updates"))
    created = client.send("POST", f"/calendars/{_calendar(args, 'calendar_id')}/events/quickAdd", params=params)
    return ToolResult.ok(slim(json_object(created)))


def _delete_event(client: GoogleCalendarRestClient, args: dict[str, Any]) -> ToolResult:
    calendar = _calendar(args, "calendar_id")
    event = _segment(args.get("event_id"), "event_id")
    client.send("DELETE", f"/calendars/{calendar}/events/{event}", params=_write_params(args, {}))
    return ToolResult.ok(
        {"deleted": True, "event_id": str(args["event_id"]).strip(), "calendar_id": str(args.get("calendar_id") or "primary").strip()}
    )


# --------------------------------------------------------------------------
# Catalog
# --------------------------------------------------------------------------


def _short(scopes: Iterable[str]) -> str:
    return ", ".join(s.removeprefix(SCOPE_BASE) for s in scopes)


def _tool(
    slug: str,
    description: str,
    schema: dict[str, Any],
    handler: Handler,
    *tags: str,
    scopes: tuple[str, ...] = (READONLY_SCOPE,),
    read_only: bool = True,
    destructive: bool = False,
    aliases: Mapping[str, str] | None = None,
) -> GoogleCalendarTool:
    text = f"{description} Needs scope: {_short(scopes)}."
    return GoogleCalendarTool(
        ToolSpec(
            slug, text, schema, read_only=read_only, destructive=destructive, tags=tags, required_scopes=scopes
        ),
        handler,
        scopes,
        aliases or {},
    )


def _write(slug: str, description: str, schema: dict[str, Any], handler: Handler, *tags: str, **kw: Any) -> GoogleCalendarTool:
    scopes = kw.pop("scopes", (EVENTS_SCOPE,))
    return _tool(slug, description, schema, handler, *tags, scopes=scopes, read_only=False, **kw)


#: Scopes of a write that also reads: the calendar's zone (``calendars.get``,
#: which ``calendar.events`` does not allow) and, for an update, the event.
_READ_WRITE = (READONLY_SCOPE, EVENTS_SCOPE)


_EVENT_WRITE_FIELDS: dict[str, Any] = {
    "summary": _string("Title."),
    "description": _string("Notes (HTML allowed)."),
    "location": _string("Where: an address or a room."),
    "start_datetime": _string(
        "Start, e.g. 2026-11-02T09:30:00 (wall-clock time in timezone, else the calendar's zone) or with "
        "an offset (2026-11-02T09:30:00-05:00)."
    ),
    "end_datetime": _string("End, same forms as start_datetime; overrides the duration fields."),
    "event_duration_hour": _integer("Length in hours (with event_duration_minutes).", minimum=0),
    "event_duration_minutes": _integer("Extra minutes of length, 0-59.", minimum=0, maximum=59),
    "timezone": _string(f"{ZONE}; times without an offset are read in it (default: the calendar's zone)."),
    "attendees": ATTENDEES,
    "recurrence": RECURRENCE,
    "visibility": VISIBILITY,
    "transparency": TRANSPARENCY,
    "send_updates": SEND_UPDATES,
    "guestsCanModify": _boolean("Guests may edit the event."),
    "guestsCanInviteOthers": _boolean("Guests may invite others."),
    "guestsCanSeeOtherGuests": _boolean("Guests see the guest list."),
    "extended_properties": EXTENDED_PROPERTIES,
    "calendar_id": CALENDAR_ID,
}

CATALOG: tuple[GoogleCalendarTool, ...] = (
    # -------------------------------------------------------------- calendars
    _tool(
        "GOOGLECALENDAR_LIST_CALENDARS",
        "List the calendars the user has on their list (their own, shared and subscribed): id, "
        "summary, timeZone, accessRole, primary. One page: has_more, next_page_token, items[]; "
        "pass next_page_token as page_token. Use an id as calendar_id elsewhere.",
        _schema(
            {
                "max_results": _integer("Calendars per page (1-250).", default=DEFAULT_CALENDARS_PAGE, maximum=MAX_CALENDARS_PAGE),
                "page_token": PAGE_TOKEN,
                "show_hidden": _boolean("Include calendars hidden from the list."),
                "show_deleted": _boolean("Include deleted calendars."),
                "min_access_role": _string(
                    "Only calendars where the user has at least this role.",
                    enum=("freeBusyReader", "reader", "writerWithoutPrivateAccess", "writer", "owner"),
                ),
                "sync_token": _string("A previous result's next_sync_token: only what changed since."),
            }
        ),
        _list_calendars,
        "calendars", "list", "my calendars", "shared", "subscribed",
    ),
    _tool(
        "GOOGLECALENDAR_GET_CALENDAR",
        "Get one calendar's details: id, summary, description, timeZone (the zone its times use) "
        "and location. calendar_id defaults to the user's primary calendar.",
        _schema({"calendar_id": CALENDAR_ID}),
        _get_calendar,
        "calendar", "timezone", "details", "settings",
    ),
    _tool(
        "GOOGLECALENDAR_GET_CURRENT_DATE_TIME",
        "Get the current date, time and weekday in a time zone (IANA name, or an hour offset such "
        "as -5); without one, in the user's primary-calendar zone. Use it to turn 'today', "
        "'tomorrow' or 'next Monday' into dates.",
        _schema({"timezone": {"type": ["string", "number"], "description": f"{ZONE}, or an offset in hours."}}),
        _current_date_time,
        "now", "today", "date", "time", "current", "clock", "timezone",
    ),
    # ----------------------------------------------------------------- events
    _tool(
        "GOOGLECALENDAR_EVENTS_LIST",
        "List events on one calendar (calendarId, default primary) between timeMin and timeMax, "
        "optionally matching query (free text over title, description, location, attendees). "
        "singleEvents=true expands recurring events into instances (needed for orderBy=startTime, "
        "which turns it on). Returns has_more, next_page_token, time_zone and items[] (id, summary, "
        "start, end, attendees, recurrence/recurringEventId, htmlLink); pass next_page_token as "
        "pageToken.",
        _schema(
            {
                "calendarId": CALENDAR_ID,
                "timeMin": _string(f"Events ending after this. {TIME_BOUND}"),
                "timeMax": _string(f"Events starting before this. {TIME_BOUND}"),
                "timeZone": _string(f"{ZONE} for the times in the result and for timeMin/timeMax without an offset."),
                "query": _string("Free-text search."),
                "maxResults": _integer("Events per page (1-2500).", default=DEFAULT_EVENTS_PAGE, maximum=MAX_EVENTS_PAGE),
                "pageToken": PAGE_TOKEN,
                "singleEvents": _boolean("Expand recurring events into their instances."),
                "orderBy": _string("startTime (expands recurring events) or updated.", enum=("startTime", "updated")),
                "showDeleted": _boolean("Include cancelled events."),
                "updatedMin": _string(f"Only events changed after this. {TIME_BOUND}"),
                "eventTypes": EVENT_TYPES,
                "iCalUID": _string("Only the event with this iCalendar UID."),
                "syncToken": _string("A previous result's next_sync_token: only what changed since (no other filters)."),
                "maxAttendees": _integer("Cap on attendees returned per event."),
                "showHiddenInvitations": _boolean("Include hidden invitations."),
                "privateExtendedProperty": _string("Only events with this private property, 'key=value'."),
                "sharedExtendedProperty": _string("Only events with this shared property, 'key=value'."),
            }
        ),
        _events_list,
        "events", "list", "agenda", "meetings", "upcoming", "calendar", "week", "today",
    ),
    _tool(
        "GOOGLECALENDAR_FIND_EVENT",
        "Find events on one calendar by text (query) and/or time range (time_min, time_max), "
        "recurring events expanded and ordered by start time unless single_events=false. Returns "
        "has_more, next_page_token and items[] with each event's id (for EVENTS_GET, UPDATE_EVENT, "
        "DELETE_EVENT), summary, start, end and attendees; pass next_page_token as page_token.",
        _schema(
            {
                "query": _string("Free-text search over title, description, location and attendees."),
                "time_min": _string(f"Events ending after this. {TIME_BOUND}"),
                "time_max": _string(f"Events starting before this. {TIME_BOUND}"),
                "timezone": _string(f"{ZONE} for the result and for times without an offset."),
                "calendar_id": CALENDAR_ID,
                "max_results": _integer("Events per page (1-2500).", default=DEFAULT_EVENTS_PAGE, maximum=MAX_EVENTS_PAGE),
                "page_token": PAGE_TOKEN,
                "order_by": _string("startTime (default) or updated.", enum=("startTime", "updated")),
                "single_events": _boolean("Expand recurring events (default true).", default=True),
                "show_deleted": _boolean("Include cancelled events."),
                "event_types": EVENT_TYPES,
                "updated_min": _string(f"Only events changed after this. {TIME_BOUND}"),
            }
        ),
        _find_event,
        "find", "search", "event", "meeting", "lookup", "when",
    ),
    _tool(
        "GOOGLECALENDAR_EVENTS_GET",
        "Get one event by id: summary, description, location, start/end (with timeZone), "
        "attendees and their responses, organizer, recurrence, conference link, reminders.",
        _schema(
            {
                "event_id": EVENT_ID,
                "calendar_id": CALENDAR_ID,
                "time_zone": _string(f"{ZONE} for the times in the result."),
                "max_attendees": _integer("Cap on attendees returned."),
            },
            ["event_id"],
        ),
        _events_get,
        "event", "details", "get", "meeting", "attendees",
    ),
    _tool(
        "GOOGLECALENDAR_EVENTS_INSTANCES",
        "List the occurrences of a recurring event (eventId of the series), optionally between "
        "timeMin and timeMax. Each instance has its own id (to change or cancel just that one) and "
        "originalStartTime. One page: has_more, next_page_token, items[].",
        _schema(
            {
                "eventId": EVENT_ID,
                "calendarId": CALENDAR_ID,
                "timeMin": _string(f"Instances ending after this. {TIME_BOUND}"),
                "timeMax": _string(f"Instances starting before this. {TIME_BOUND}"),
                "timeZone": _string(f"{ZONE} for the result and for times without an offset."),
                "maxResults": _integer("Instances per page (1-2500).", default=DEFAULT_EVENTS_PAGE, maximum=MAX_EVENTS_PAGE),
                "pageToken": PAGE_TOKEN,
                "showDeleted": _boolean("Include cancelled instances."),
                "maxAttendees": _integer("Cap on attendees returned per instance."),
                "originalStart": _string("Only the instance originally at this RFC 3339 time."),
            },
            ["eventId"],
        ),
        _events_instances,
        "recurring", "instances", "occurrences", "series", "repeat",
    ),
    _tool(
        "GOOGLECALENDAR_EVENTS_LIST_ALL_CALENDARS",
        "List events across all the user's calendars (or calendar_ids) between time_min and "
        "time_max, merged and sorted by start, each tagged with calendarId and calendarSummary; "
        "recurring events expanded. response_detail minimal (default: id, summary, start, end, "
        "status, location, htmlLink) or full. Calendars that cannot be read are listed in errors; "
        "has_more names calendars with more than max_results_per_calendar events.",
        _schema(
            {
                "time_min": _string(f"Events ending after this. {TIME_BOUND}"),
                "time_max": _string(f"Events starting before this. {TIME_BOUND}"),
                "q": _string("Free-text search."),
                "calendar_ids": _strings("Only these calendars (default: every calendar on the list, up to 50)."),
                "timezone": _string(f"{ZONE} for the result and for times without an offset (default: primary's)."),
                "single_events": _boolean("Expand recurring events (default true).", default=True),
                "show_deleted": _boolean("Include cancelled events.", default=False),
                "event_types": EVENT_TYPES,
                "response_detail": _string("minimal or full events.", enum=("minimal", "full"), default="minimal"),
                "max_results_per_calendar": _integer("Events per calendar (1-2500).", default=DEFAULT_PER_CALENDAR, maximum=MAX_EVENTS_PAGE),
            },
            ["time_min", "time_max"],
        ),
        _list_all_calendars,
        "events", "all calendars", "agenda", "everything", "busy", "across",
    ),
    _tool(
        "GOOGLECALENDAR_FREE_BUSY_QUERY",
        "Get busy time blocks for one or more calendars or people (items: calendar ids or email "
        "addresses) between timeMin and timeMax. Returns calendars: {id: {busy: [{start, end}], "
        "errors}}; event details are not shown.",
        _schema(
            {
                "items": {
                    "type": "array",
                    "items": {"type": ["string", "object"]},
                    "description": "Calendar ids or emails ('primary' for the user), or {id} objects; up to 50.",
                },
                "timeMin": _string(f"Window start. {TIME_BOUND}"),
                "timeMax": _string(f"Window end. {TIME_BOUND}"),
                "timeZone": _string(f"{ZONE} for the result and for times without an offset."),
                "groupExpansionMax": _integer("Max members to expand per group (1-100).", maximum=100),
                "calendarExpansionMax": _integer("Max calendars to expand (1-50).", maximum=50),
            },
            ["items", "timeMin", "timeMax"],
        ),
        _free_busy_query,
        "free", "busy", "availability", "freebusy", "available",
    ),
    _tool(
        "GOOGLECALENDAR_FIND_FREE_SLOTS",
        "Find free time across calendars or people (items; default the user's primary calendar) "
        "between time_min (default now) and time_max (default a day later): returns busy (merged) "
        "and free_slots [{start, end, duration_minutes}] in timezone (default the primary "
        "calendar's zone). Use it to suggest meeting times.",
        _schema(
            {
                "items": {
                    "type": "array",
                    "items": {"type": ["string", "object"]},
                    "description": "Calendar ids or emails to check (default ['primary']); up to 50.",
                },
                "time_min": _string(f"Window start (default now). {TIME_BOUND}"),
                "time_max": _string(f"Window end (default time_min + 1 day). {TIME_BOUND}"),
                "timezone": _string(f"{ZONE} for the result and for times without an offset."),
                "group_expansion_max": _integer("Max members to expand per group (1-100).", maximum=100),
                "calendar_expansion_max": _integer("Max calendars to expand (1-50).", maximum=50),
            }
        ),
        _find_free_slots,
        "free", "slots", "availability", "available", "open", "when",
    ),
    # ----------------------------------------------------------------- writes
    _write(
        "GOOGLECALENDAR_CREATE_EVENT",
        "Create an event: summary, start_datetime plus end_datetime or a duration "
        "(event_duration_hour/minutes; default 30 min), or start_date/end_date for all-day. Times "
        "without an offset are in timezone (default the calendar's). attendees get invites "
        "(send_updates, default all); recurrence takes RRULE lines; a Google Meet link is added "
        "unless create_meeting_room=false. Returns the event (id, htmlLink, hangoutLink).",
        _schema(
            {
                **_EVENT_WRITE_FIELDS,
                "send_updates": _string(
                    "Who Google emails the invitation: all (default), externalOnly or none.",
                    enum=("all", "externalOnly", "none"),
                    default="all",
                ),
                "start_date": _string("All-day start date, YYYY-MM-DD."),
                "end_date": _string("All-day end date, exclusive (the day after the last day); default one day."),
                "eventType": EVENT_TYPE,
                "create_meeting_room": _boolean("Add a Google Meet link (default true).", default=True),
                "focusTimeProperties": {"type": "object", "description": "For eventType focusTime."},
                "outOfOfficeProperties": {"type": "object", "description": "For eventType outOfOffice."},
                "workingLocationProperties": {"type": "object", "description": "For eventType workingLocation."},
                "birthdayProperties": {"type": "object", "description": "For eventType birthday."},
            }
        ),
        _create_event,
        "create", "schedule", "add", "book", "new", "event", "meeting", "invite", "appointment",
        scopes=_READ_WRITE,
        aliases={"title": "summary", "start_time": "start_datetime", "end_time": "end_datetime", "time_zone": "timezone"},
    ),
    _write(
        "GOOGLECALENDAR_UPDATE_EVENT",
        "Change an event (event_id): only the fields given change, the rest is kept. New "
        "start_datetime keeps the event's length unless end_datetime or a duration is given; "
        "start_date/end_date make it all-day. attendees and recurrence replace the whole list. On a recurring series every instance "
        "changes; pass an instance id (from EVENTS_INSTANCES) to change one. Returns the event.",
        _schema(
            {
                "event_id": EVENT_ID,
                **_EVENT_WRITE_FIELDS,
                "start_date": _string("Make it all-day from this date, YYYY-MM-DD (or move an all-day event)."),
                "end_date": _string("All-day end date, exclusive; default: the same length (one day for a timed event)."),
                "create_meeting_room": _boolean("Add a Google Meet link if the event has none."),
            },
            ["event_id"],
        ),
        _update_event,
        "update", "change", "reschedule", "move", "edit", "rename", "event", "meeting",
        scopes=_READ_WRITE,
        aliases={"title": "summary", "start_time": "start_datetime", "end_time": "end_datetime", "time_zone": "timezone"},
    ),
    _write(
        "GOOGLECALENDAR_QUICK_ADD",
        "Create an event from one line of text, parsed by Google: e.g. 'Lunch with Ann tomorrow "
        "1pm-2pm at Cafe Rio' or 'Dentist Friday 9am'. Returns the event.",
        _schema(
            {
                "text": _string("The event in plain words."),
                "calendar_id": CALENDAR_ID,
                "send_updates": SEND_UPDATES,
            },
            ["text"],
        ),
        _quick_add,
        "quick", "add", "create", "natural language", "event", "remind",
    ),
    _write(
        "GOOGLECALENDAR_DELETE_EVENT",
        "Delete an event (event_id) from a calendar; guests are told per send_updates. Deleting "
        "a recurring series removes every instance; pass an instance id to cancel just one.",
        _schema(
            {
                "event_id": EVENT_ID,
                "calendar_id": CALENDAR_ID,
                "send_updates": SEND_UPDATES,
                "send_notifications": _boolean("Deprecated: send_updates all (true) or none (false)."),
            },
            ["event_id"],
        ),
        _delete_event,
        "delete", "remove", "cancel", "event", "meeting",
        destructive=True,
    ),
)

TOOLS: dict[str, GoogleCalendarTool] = {tool.spec.slug: tool for tool in CATALOG}
TOOL_SPECS: tuple[ToolSpec, ...] = tuple(tool.spec for tool in CATALOG)
#: Each tool's OAuth scopes, by slug.
REQUIRED_SCOPES: dict[str, tuple[str, ...]] = {tool.spec.slug: tool.scopes for tool in CATALOG}
