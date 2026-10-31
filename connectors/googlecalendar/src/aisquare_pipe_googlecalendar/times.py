"""Dates, times and time zones as the Calendar API wants them.

Callers (and models) write times loosely: ``2026-11-02``,
``2026-11-02T09:30``, ``2026-11-02 09:30:00``, ``2026-11-02T09:30:00Z`` or
``…+05:30``. Calendar needs RFC 3339 with an offset for query bounds
(``timeMin``), and either an offset or an IANA ``timeZone`` for event times.

* A time with an offset (or ``Z``) is an instant and is kept as given.
* A time without one is wall-clock time in a zone: the caller's
  ``timezone``, else the calendar's own zone (the toolkit looks it up).
  Event times stay wall-clock (``{"dateTime": "2026-11-02T09:30:00",
  "timeZone": "Europe/London"}``) so Google applies the zone's rules —
  daylight-saving changes included — to every instance of a recurring event.
  Query bounds are resolved to an offset with :mod:`zoneinfo`.
* A bare date is midnight (a query bound) or an all-day event day.

Zones are IANA names (``America/New_York``, ``UTC``); anything else is
refused with a ValueError, as is a time that does not parse.
"""

from __future__ import annotations

import datetime
import re
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_ZONE_NAME = re.compile(r"^[A-Za-z0-9_+\-]+(/[A-Za-z0-9_+\-]+)*$")


def zone(name: str) -> ZoneInfo:
    """The IANA zone ``name``; ValueError when there is no such zone."""
    text = str(name).strip()
    if not _ZONE_NAME.match(text):
        raise ValueError("timezone must be an IANA name such as 'Europe/London' or 'UTC'")
    try:
        return ZoneInfo(text)
    except (ZoneInfoNotFoundError, ValueError):
        raise ValueError("timezone must be an IANA name such as 'Europe/London' or 'UTC'") from None


def is_date(text: str) -> bool:
    return bool(_DATE.match(text.strip()))


def parse_date(text: str) -> datetime.date:
    try:
        return datetime.date.fromisoformat(text.strip())
    except ValueError:
        raise ValueError("not a YYYY-MM-DD date") from None


def parse_datetime(text: str) -> datetime.datetime:
    """An ISO 8601 / RFC 3339 date-time, naive (wall clock) or aware;
    seconds and fractions optional, ``T`` or a space between date and time."""
    value = str(text).strip()
    if is_date(value):
        raise ValueError("a date-time needs a time (YYYY-MM-DDTHH:MM)")
    try:
        parsed = datetime.datetime.fromisoformat(value)
    except ValueError:
        raise ValueError("not an ISO 8601 date-time such as 2026-11-02T09:30:00") from None
    return parsed


def wall_clock(value: datetime.datetime) -> str:
    """A naive date-time as Calendar's ``dateTime`` without an offset."""
    return value.replace(tzinfo=None).isoformat(timespec="seconds")


def rfc3339(value: datetime.datetime) -> str:
    """An aware date-time as RFC 3339 (``Z`` for UTC)."""
    text = value.isoformat(timespec="seconds")
    return text.removesuffix("+00:00") + "Z" if text.endswith("+00:00") else text


def instant(text: str, tz: ZoneInfo | None) -> str:
    """A query bound (``timeMin`` / ``timeMax``) as RFC 3339: a date is
    midnight and a naive time wall-clock time in ``tz`` (UTC when None)."""
    value = str(text).strip()
    if is_date(value):
        moment = datetime.datetime.combine(parse_date(value), datetime.time())
    else:
        moment = parse_datetime(value)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=tz or datetime.UTC)
    return rfc3339(moment)


def needs_zone(*values: str | None) -> bool:
    """Whether any of ``values`` is a date or a naive date-time (so a zone
    is needed to place it)."""
    for value in values:
        if not isinstance(value, str) or not value.strip():
            continue
        text = value.strip()
        if is_date(text):
            try:
                parse_date(text)
            except ValueError:
                continue
            return True
        try:
            if parse_datetime(text).tzinfo is None:
                return True
        except ValueError:
            continue  # the caller's own parse reports it, before any request
    return False


def event_time(value: datetime.datetime, tz_name: str | None) -> dict[str, str]:
    """An event ``start`` / ``end``: wall-clock time plus ``timeZone`` when
    the zone is known, else the instant with its offset."""
    if tz_name and value.tzinfo is None:
        return {"dateTime": wall_clock(value), "timeZone": tz_name}
    out = {"dateTime": rfc3339(value) if value.tzinfo else wall_clock(value)}
    if tz_name:
        out["timeZone"] = tz_name
    return out


def now(tz: datetime.tzinfo) -> datetime.datetime:
    return datetime.datetime.now(tz).replace(microsecond=0)
