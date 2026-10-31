# aisquare-pipe-googlecalendar

Google Calendar for [aisquare.pipe](../../README.md), over the Calendar v3
REST API (plain `requests`, no Google SDK). The package ships one thing:

| What | Registered as | Use it for |
|---|---|---|
| `GoogleCalendarToolkit` | toolkit `googlecalendar` (`aisquare_pipe.toolkits`) | 14 LLM-callable tools — calendars, events, recurring instances, events across calendars, free/busy and free slots, the current time; create, update, quick-add and delete events — plus Google's OAuth spec |

## Install

```bash
pip install aisquare-pipe-googlecalendar             # or: pip install "aisquare-pipe[full]"
cd connectors/googlecalendar && pip install -e ".[dev]"   # development
```

## Connecting (OAuth)

`GOOGLECALENDAR_OAUTH` (also `GoogleCalendarToolkit.oauth`) describes a
Google OAuth 2.0 *web application* client:

| | |
|---|---|
| authorize | `https://accounts.google.com/o/oauth2/v2/auth` with `access_type=offline`, `prompt=consent` |
| token | `https://oauth2.googleapis.com/token` |
| revoke | `https://oauth2.googleapis.com/revoke` |
| PKCE | required (S256) |
| tokens | access tokens expire after one hour; the refresh token does not rotate |
| default scope | `https://www.googleapis.com/auth/calendar.readonly` |

**Least privilege.** The default consent covers the read tools only. A host
that turns on writing requests `calendar.events` together with
`calendar.readonly` (`scopes=[...]` to `build_authorize_url`). Every tool
lists its scopes in `ToolSpec.required_scopes` (also `REQUIRED_SCOPES[slug]`)
and at the end of its description. With the connection's granted scopes in
`config["granted_scopes"]`, `available_tools` lists only the tools they
cover, and `call_tool` refuses the others as `insufficient_scope` before any
request. Google's full `calendar` scope covers both scopes below.

| Scope | Google class | Tools |
|---|---|---|
| `calendar.readonly` | **sensitive** | every read tool (calendars, events, free/busy); also `GOOGLECALENDAR_CREATE_EVENT` and `GOOGLECALENDAR_UPDATE_EVENT`, which look up the calendar's time zone (`calendars.get`, which `calendar.events` does not allow) and, for an update, read the event |
| `calendar.events` | **sensitive** | `GOOGLECALENDAR_CREATE_EVENT`, `GOOGLECALENDAR_UPDATE_EVENT`, `GOOGLECALENDAR_QUICK_ADD`, `GOOGLECALENDAR_DELETE_EVENT` |

No tool needs `https://www.googleapis.com/auth/calendar` (full access,
including creating and deleting calendars and sharing them).

**Google verification.** Calendar's scopes are Google **sensitive** scopes. A
production app that requests them must pass Google's OAuth app verification
(no third-party security assessment, unlike Gmail's restricted scopes).
Until then the consent screen shows an "unverified app" warning, and the app
is limited to 100 test users.

**One OAuth client for Drive, Gmail and Calendar.** Each Google toolkit gets
its own token and consent, and one OAuth client may serve all three. That is
why this spec — unlike Google Drive's — does not set
`include_granted_scopes=true`: with it, a Calendar token would also carry
every Drive or Gmail scope the user granted the same client. Revoking is
grant-wide, though: Google's revoke endpoint ends the user's whole grant to
the OAuth client, so `revoke_token` on a Calendar token also ends that user's
Drive and Gmail connections. On disconnecting one Google connector, delete
its stored tokens; revoke only when it is the user's last Google connection.

Set up: Google Cloud console → APIs & Services → enable the **Google Calendar
API** → OAuth consent screen (add the Calendar scopes) → Credentials → *OAuth
client ID* (Web application) with your callback as an authorized redirect URI.

## Toolkit

```python
from aisquare_pipe_googlecalendar import GoogleCalendarToolkit

calendar = GoogleCalendarToolkit()
config = {"access_token": "ya29...."}

calendar.search_tools("when am I free on thursday", limit=3)      # -> [ToolSpec, ...]
result = calendar.call_tool(
    "GOOGLECALENDAR_EVENTS_LIST", {"timeMin": "2026-11-02", "timeMax": "2026-11-09", "singleEvents": True}, config
)
result.successful, result.data, result.error, result.error_code
```

`call_tool` never raises. Slugs and argument names are Composio's Google
Calendar toolkit's, verbatim, including Composio's mixed casing
(`GOOGLECALENDAR_EVENTS_LIST` takes `calendarId` / `timeMin`;
`GOOGLECALENDAR_FIND_EVENT` takes `calendar_id` / `time_min`). The other
casing is accepted too. `calendar_id` defaults to `primary`; ids such as
`en.usa#holiday@group.v.calendar.google.com` are encoded as one path segment.

**Times and time zones.** Calendar wants RFC 3339 with an offset; callers
and models often write less. The toolkit reads a time as follows:

- with an offset or `Z` (`2026-11-02T09:00:00-05:00`) — an instant, kept as given;
- without one (`2026-11-02T09:00`, `2026-11-02 09:00:00`) — wall-clock time in
  the tool's `timezone` / `timeZone` argument, else in the **calendar's own
  zone** (one extra `GET /calendars/{id}` request);
- a bare date (`2026-11-02`) — midnight in that zone for a query bound, or
  the day of an all-day event.

Query bounds become offsets with the zone's rules for that date, so
`2026-03-08` in New York is `-05:00` and `2026-03-09` is `-04:00`. Event
times stay wall-clock with an IANA zone
(`{"dateTime": "2026-11-02T09:00:00", "timeZone": "America/New_York"}`), so
Google applies the zone to every instance of a recurring event across
daylight-saving changes. Durations are added in wall-clock time. Zones must
be IANA names; anything else is `invalid_arguments`.
`GOOGLECALENDAR_GET_CURRENT_DATE_TIME` gives "now" in a zone (default: the
primary calendar's), for turning "tomorrow" into a date.

**Recurring events.** `recurrence` takes RFC 5545 lines (`RRULE:`, `EXRULE:`,
`RDATE`, `EXDATE`; a bare `FREQ=…` becomes an `RRULE:`); an empty list makes
an event one-off. A recurring event always gets a `timeZone` (the calendar's
when none is given), as Google requires. `singleEvents=true` (and
`orderBy=startTime`, which turns it on) expands series into instances;
`GOOGLECALENDAR_EVENTS_INSTANCES` lists one series' instances. An instance
has its own id (`<series>_20261109T140000Z`): update or delete it to change
one occurrence, or use the series id to change them all.

**Results.** Events and calendars are Google's resources minus `kind` and
`etag`. List tools return one page per call, hints first:

```python
{"has_more": True, "next_page_token": "…", "time_zone": "America/New_York", "items": [...]}
```

Pass `next_page_token` back as `pageToken` / `page_token`. `next_sync_token`
appears when Google sends one (pass it as `syncToken` / `sync_token`; a sync
token cannot be combined with filters, and an expired one is `not_found`).
Event pages default to 50 (Composio's default is 250; up to 2500).

**Across calendars.** `GOOGLECALENDAR_EVENTS_LIST_ALL_CALENDARS` reads the
calendar list (up to 50 calendars) or `calendar_ids`, lists each calendar's
events in the window, and merges them sorted by start, each tagged with
`calendarId` and `calendarSummary`. A calendar that answers 403/404 is
reported in `errors` without failing the call; `has_more` and
`truncated_calendars` name calendars with more events than
`max_results_per_calendar`.

**Free/busy.** `GOOGLECALENDAR_FREE_BUSY_QUERY` is Google's `freeBusy` call
(calendar ids or people's emails, up to 50). `GOOGLECALENDAR_FIND_FREE_SLOTS`
merges every calendar's busy blocks and returns the gaps in the window
(default: now to a day later) as `free_slots` with `duration_minutes`.

**Writes.** `GOOGLECALENDAR_CREATE_EVENT` follows Composio's defaults: 30
minutes when there is no end or duration, invitations emailed to every guest
(`send_updates: all`), and a Google Meet link (`create_meeting_room: true`;
not for focus time, out-of-office, working-location or birthday events).
`GOOGLECALENDAR_UPDATE_EVENT` is a patch: it reads the event, changes only
the fields given and keeps the rest — a new start keeps the event's length.
`start_datetime` turns an all-day event into a timed one and `start_date` /
`end_date` the reverse; because Google merges nested objects on PATCH, the
old form's fields are sent as `null` (`date`, or `dateTime` and `timeZone`)
so they do not survive next to the new ones.
(Composio's update replaces the whole event; this one never clears fields
you did not mention.) Other writes email nobody unless `send_updates` says
so (Google's default).

**Errors.** A failed call carries core's canonical `error_code`, a
human-readable `error`, `retry_after` (seconds) when rate limited, and
`data={"status", "reason"}` when Google answered:

| Failure | `error_code` | Notes |
|---|---|---|
| 401 | `unauthorized` | refresh the token, or reconnect |
| 403 for a missing scope (`insufficientPermissions`, `ACCESS_TOKEN_SCOPE_INSUFFICIENT`, or `WWW-Authenticate: … insufficient_scope`) | `insufficient_scope` | `data` adds `insufficient_scope: true` and the tool's `required_scopes` |
| 429, or a 403 `rateLimitExceeded` / `userRateLimitExceeded`, once retries or the budget run out | `rate_limited` (+ `retry_after`) | |
| other 403 (`requiredAccessLevel`: no write access to that calendar; quotas) | `forbidden` | |
| 404, 410 (deleted event, expired sync token) | `not_found` | |
| 400 (`timeRangeEmpty`, …), and arguments the toolkit refuses | `invalid_arguments` | |
| 409, 412 | `conflict` | |
| request timeout, 504, spent `timeout_seconds` | `timeout` | |
| 5xx, network failure | `upstream_error` | |

**Retries.** Rate limits are retried up to 3 times (1 s, 2 s, 4 s, or a finite
`Retry-After`) on any method. A 500/502/503/504 is retried on GETs only: an
event created half-way is never created twice. Total sleep per call is
capped at 60 s.

**Time budget.** `config["timeout_seconds"]` bounds the whole call: each
request's timeout is the smaller of 30 s and the time left, a retry that
would outlast it fails at once, and a budget spent between the calendars of
`GOOGLECALENDAR_EVENTS_LIST_ALL_CALENDARS` fails as `timeout`.

**Concurrency and secrets.** A `GoogleCalendarToolkit` holds no mutable
state; each call opens and closes its own HTTP session (`session=` opts into
a shared one). The token rides the `Authorization` header only — never a URL,
log line, error or repr. Logs carry method, status and timing, never paths,
calendar ids or queries.

### Tools

Read-only (`calendar.readonly`):

| Slug | Calendar endpoint | Use for |
|---|---|---|
| `GOOGLECALENDAR_LIST_CALENDARS` | `GET users/me/calendarList` | the user's calendars and their ids |
| `GOOGLECALENDAR_GET_CALENDAR` | `GET calendars/{id}` | one calendar, including its time zone |
| `GOOGLECALENDAR_GET_CURRENT_DATE_TIME` | (none, or `GET calendars/primary` for the zone) | "now" in a zone |
| `GOOGLECALENDAR_EVENTS_LIST` | `GET calendars/{id}/events` | events in a range, by text, sync |
| `GOOGLECALENDAR_FIND_EVENT` | `GET calendars/{id}/events` | find events by text/range, expanded and ordered by start |
| `GOOGLECALENDAR_EVENTS_GET` | `GET calendars/{id}/events/{id}` | one event |
| `GOOGLECALENDAR_EVENTS_INSTANCES` | `GET calendars/{id}/events/{id}/instances` | a recurring event's occurrences |
| `GOOGLECALENDAR_EVENTS_LIST_ALL_CALENDARS` | `GET users/me/calendarList` + `GET calendars/{id}/events` each | everything in a window, merged |
| `GOOGLECALENDAR_FREE_BUSY_QUERY` | `POST freeBusy` | busy blocks of calendars or people |
| `GOOGLECALENDAR_FIND_FREE_SLOTS` | `POST freeBusy` | free gaps across calendars or people |

Writes (`calendar.events`; create and update also `calendar.readonly`):

| Slug | Calendar endpoint | Notes |
|---|---|---|
| `GOOGLECALENDAR_CREATE_EVENT` | `POST calendars/{id}/events` | timed or all-day; guests, recurrence, Meet link |
| `GOOGLECALENDAR_UPDATE_EVENT` | `GET` + `PATCH calendars/{id}/events/{id}` | only the given fields change; `start_date`/`end_date` make it all-day |
| `GOOGLECALENDAR_QUICK_ADD` | `POST calendars/{id}/events/quickAdd` | one line of text, parsed by Google |
| `GOOGLECALENDAR_DELETE_EVENT` | `DELETE calendars/{id}/events/{id}` | **destructive**; a series id deletes every instance |

## Tests

```bash
cd connectors/googlecalendar && pytest   # hermetic: faked HTTP, socket guard on
pytest -m live tests/test_live.py        # opt-in; needs GOOGLECALENDAR_ACCESS_TOKEN
```
