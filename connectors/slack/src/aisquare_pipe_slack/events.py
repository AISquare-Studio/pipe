"""Slack Events API helpers for a host's inbound endpoint.

The host (pipe-service) owns the HTTP route, the app's Signing Secret, the
dedupe ledger and the 3-second ack. These are pure functions over the request
it received: no network, no state, no clock other than ``time.time`` (and
``now`` overrides that).

A typical endpoint::

    try:
        event = parse_request(request.headers, request.body, signing_secret)
    except SlackSignatureError:  # not from Slack, or replayed
        return 401
    except SlackEventError:      # signed, but not an Events API body
        return 400
    if isinstance(event, UrlVerification):
        return 200, event.response()          # {"challenge": ...}
    if isinstance(event, EventCallback) and not ledger.seen(event.event_id):
        queue(event_to_envelope(event))       # do the work after the ack
    return 200

:func:`parse_request` checks the signature *and* the timestamp window before
it parses anything, so a host can't do one and forget the other. It works on
the raw body bytes, exactly as received: Slack signs those, and a re-encoded
JSON body no longer matches. Slack's scheme is
https://docs.slack.dev/authentication/verifying-requests-from-slack.

:func:`parse_event` returns a typed object for the events pipe handles —
:class:`AppMention`, :class:`Message` (``message.im`` and the channel
variants), :class:`Reaction` (``reaction_added`` / ``reaction_removed``),
:class:`TokensRevoked`, :class:`AppUninstalled` — and a plain
:class:`EventCallback` for any other event, so a new Slack event type or
message subtype is acknowledged rather than refused.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from aisquare.pipe import DataEnvelope

SIGNATURE_HEADER = "X-Slack-Signature"
TIMESTAMP_HEADER = "X-Slack-Request-Timestamp"
RETRY_NUM_HEADER = "X-Slack-Retry-Num"
RETRY_REASON_HEADER = "X-Slack-Retry-Reason"
#: How far a request's timestamp may be from now, either way (replay window).
TIMESTAMP_WINDOW_SECONDS = 300
SIGNATURE_VERSION = "v0"

#: ``trigger_slug`` per event, in Composio's current trigger vocabulary (its
#: ``slack`` toolkit, 2026-10), so subscriptions keep their names. Composio
#: has no app-mention trigger; ``SLACK_APP_MENTION_RECEIVED`` is pipe's.
TRIGGER_CHANNEL_MESSAGE = "SLACK_CHANNEL_MESSAGE_RECEIVED"
TRIGGER_DIRECT_MESSAGE = "SLACK_DIRECT_MESSAGE_RECEIVED"
TRIGGER_REACTION_ADDED = "SLACK_MESSAGE_REACTION_ADDED"
TRIGGER_REACTION_REMOVED = "SLACK_MESSAGE_REACTION_REMOVED"
TRIGGER_CHANNEL_CREATED = "SLACK_CHANNEL_CREATED"
TRIGGER_APP_MENTION = "SLACK_APP_MENTION_RECEIVED"

#: The bot events the app manifest subscribes to, and the bot scope each
#: needs (none for the two lifecycle events).
BOT_EVENTS: dict[str, tuple[str, ...]] = {
    "app_mention": ("app_mentions:read",),
    "message.im": ("im:history",),
    "reaction_added": ("reactions:read",),
    "reaction_removed": ("reactions:read",),
    "app_uninstalled": (),
    "tokens_revoked": (),
}
#: Scopes only an event subscription needs: no tool calls a method that uses
#: them, so the catalog doesn't declare them.
EVENTS_ONLY_BOT_SCOPES: tuple[str, ...] = ("app_mentions:read",)

#: Message subtypes that are a new message (others — edits, deletions,
#: joins — are events about a message, and carry no trigger).
NEW_MESSAGE_SUBTYPES = frozenset({None, "bot_message", "file_share", "thread_broadcast", "me_message"})

#: A timestamp header: whole Unix seconds, digits only (no sign, point or
#: spaces). 15 digits bounds it far beyond any real clock, so arithmetic on it
#: can't overflow.
_TIMESTAMP = re.compile(r"[0-9]{1,15}")
_MAX_TIMESTAMP = 10**15
_RETRY_NUM = re.compile(r"[0-9]{1,3}")
_RETRY_REASON = re.compile(r"[a-z_]{1,64}")


class SlackRequestError(ValueError):
    """An inbound request the host must not act on. ``reason`` is a short
    code for logs and metrics; the message never carries the secret, the
    signature or the body."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


class SlackSignatureError(SlackRequestError):
    """Not provably from Slack, or too old: answer 401 and drop it."""


class SlackEventError(SlackRequestError):
    """Signed by Slack but not an Events API body pipe can read: answer 400."""


# --------------------------------------------------------------------------
# Signature and replay window
# --------------------------------------------------------------------------


def verify_slack_signature(
    raw_body: bytes | str, timestamp: str | int, signature: str, signing_secret: str
) -> bool:
    """Whether ``signature`` (``X-Slack-Signature``) is Slack's v0 signature
    of ``raw_body`` at ``timestamp`` (``X-Slack-Request-Timestamp``):
    ``"v0=" + hex(HMAC-SHA256(secret, b"v0:" + ts + b":" + raw_body))``,
    compared in constant time, with ``ts`` the header exactly as sent (as
    Slack's spec builds it: ``"01531420618"`` is not ``"1531420618"``).
    False — never an exception — for any missing or malformed part. Does not
    check the timestamp's age: use :func:`verify_request`, which does both."""
    body = _body_bytes(raw_body)
    ts = _timestamp_text(timestamp)
    if body is None or ts is None or not isinstance(signature, str) or not isinstance(signing_secret, str):
        return False
    given = signature.strip()
    if not signing_secret or not given.isascii() or not given.startswith(f"{SIGNATURE_VERSION}="):
        return False
    try:
        key = signing_secret.encode("utf-8")
    except UnicodeEncodeError:
        return False
    base = f"{SIGNATURE_VERSION}:{ts}:".encode() + body
    expected = f"{SIGNATURE_VERSION}=" + hmac.new(key, base, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected.encode(), given.encode())


def timestamp_in_window(
    timestamp: str | int, *, now: float | None = None, window: float = TIMESTAMP_WINDOW_SECONDS
) -> bool:
    """Whether ``timestamp`` (whole Unix seconds) is within ``window``
    seconds of ``now``, either way. False for anything but an integer."""
    ts = _timestamp(timestamp)
    if ts is None:
        return False
    current = time.time() if now is None else now
    return abs(current - ts) <= window


def verify_request(
    headers: Mapping[str, Any] | Iterable[tuple[Any, Any]],
    raw_body: bytes | str,
    signing_secret: str,
    *,
    now: float | None = None,
) -> None:
    """Check that a request came from Slack, recently: the timestamp header
    is an integer within :data:`TIMESTAMP_WINDOW_SECONDS` of ``now``, and the
    signature matches the raw body. Headers are looked up case-insensitively
    (a mapping, or ``(name, value)`` pairs such as ASGI's). Raises
    :class:`SlackSignatureError` with a ``reason``; returns None when the
    request is good."""
    if not isinstance(signing_secret, str) or not signing_secret:
        raise SlackSignatureError("no_signing_secret", "slack: the host passed no Signing Secret")
    timestamp = header(headers, TIMESTAMP_HEADER)
    signature = header(headers, SIGNATURE_HEADER)
    if timestamp is None:
        raise SlackSignatureError("missing_timestamp", f"slack: no {TIMESTAMP_HEADER} header")
    if signature is None:
        raise SlackSignatureError("missing_signature", f"slack: no {SIGNATURE_HEADER} header")
    if _timestamp(timestamp) is None:
        raise SlackSignatureError("bad_timestamp", f"slack: {TIMESTAMP_HEADER} is not whole Unix seconds")
    if not timestamp_in_window(timestamp, now=now):
        raise SlackSignatureError(
            "stale_timestamp",
            f"slack: {TIMESTAMP_HEADER} is more than {TIMESTAMP_WINDOW_SECONDS}s from now (replay?)",
        )
    if _body_bytes(raw_body) is None:
        raise SlackSignatureError("bad_body", "slack: the raw body must be bytes, exactly as received")
    if not verify_slack_signature(raw_body, timestamp, signature, signing_secret):
        raise SlackSignatureError("bad_signature", f"slack: {SIGNATURE_HEADER} does not match the body")


def header(headers: Mapping[str, Any] | Iterable[tuple[Any, Any]], name: str) -> str | None:
    """One header's value, by case-insensitive name; None when absent, and
    when it appears more than once with different values (fail closed).
    Byte names and values (ASGI) are read as Latin-1."""
    pairs = headers.items() if isinstance(headers, Mapping) else headers
    wanted = name.lower()
    found: set[str] = set()
    try:
        for key, value in pairs:
            if _latin1(key).lower() == wanted:
                if isinstance(value, (list, tuple)):  # multi-dict style
                    found.update(_latin1(v) for v in value)
                else:
                    found.add(_latin1(value))
    except (TypeError, ValueError):
        return None
    return found.pop().strip() if len(found) == 1 else None


def _latin1(value: Any) -> str:
    return value.decode("latin-1") if isinstance(value, (bytes, bytearray)) else str(value)


def _body_bytes(raw_body: Any) -> bytes | None:
    if isinstance(raw_body, (bytes, bytearray, memoryview)):
        return bytes(raw_body)
    if isinstance(raw_body, str):
        try:
            return raw_body.encode("utf-8")
        except UnicodeEncodeError:  # lone surrogates: not a body Slack sent
            return None
    return None


def _timestamp_text(value: Any) -> str | None:
    """The timestamp as Slack signed it: a header value of 1–15 digits as it
    is, or an int in range as its decimal; None for anything else (floats,
    signs, exponents, spaces, booleans, huge numbers)."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return str(value) if 0 <= value < _MAX_TIMESTAMP else None
    return value if isinstance(value, str) and _TIMESTAMP.fullmatch(value) else None


def _timestamp(value: Any) -> int | None:
    """Whole Unix seconds from a header value (see :func:`_timestamp_text`)."""
    text = _timestamp_text(value)
    return int(text) if text is not None else None


# --------------------------------------------------------------------------
# Parsed requests
# --------------------------------------------------------------------------


@dataclass(frozen=True, kw_only=True)
class SlackEvent:
    """Any Events API request body. ``type`` is the outer type
    (``event_callback``, ``url_verification``, ``app_rate_limited``, or one
    pipe doesn't know). ``retry_num`` / ``retry_reason`` come from Slack's
    retry headers: Slack resends an event it got no 2xx for within 3 s, up to
    3 times, with the same ``event_id``."""

    type: str
    body: Mapping[str, Any] = field(repr=False)
    retry_num: int | None = None
    retry_reason: str | None = None

    @property
    def is_retry(self) -> bool:
        return self.retry_num is not None


@dataclass(frozen=True, kw_only=True)
class UrlVerification(SlackEvent):
    """Slack checking the Request URL: echo :meth:`response` with HTTP 200."""

    challenge: str

    def response(self) -> dict[str, str]:
        return {"challenge": self.challenge}


@dataclass(frozen=True, kw_only=True)
class AppRateLimited(SlackEvent):
    """Slack is dropping this workspace's events (over 30,000 an hour for
    the app); ``minute_rate_limited`` is the minute it started."""

    team_id: str | None
    api_app_id: str | None
    minute_rate_limited: int | None


@dataclass(frozen=True, kw_only=True)
class EventCallback(SlackEvent):
    """One event. ``event_id`` is the dedupe key: unique per event, the same
    on every retry. ``event`` is Slack's inner event object, as sent."""

    event_id: str
    event_time: int | None
    team_id: str | None
    api_app_id: str | None
    event_type: str
    event: Mapping[str, Any] = field(repr=False)


@dataclass(frozen=True, kw_only=True)
class PostedMessage(EventCallback):
    """What a mention and a message have in common: where, who, what, when."""

    channel: str
    user: str | None
    text: str
    ts: str
    thread_ts: str | None
    bot_id: str | None

    @property
    def is_thread_reply(self) -> bool:
        return self.thread_ts is not None and self.thread_ts != self.ts

    @property
    def is_from_bot(self) -> bool:
        """Posted by a bot — possibly this app's own: skip it, or the bot
        answers itself in a loop."""
        return self.bot_id is not None


@dataclass(frozen=True, kw_only=True)
class AppMention(PostedMessage):
    """``app_mention``: someone wrote ``@app`` in a channel the bot is in."""


@dataclass(frozen=True, kw_only=True)
class Message(PostedMessage):
    """A ``message`` event: ``message.im`` (``channel_type == "im"``, a DM
    with the bot), or ``message.channels`` / ``.groups`` / ``.mpim`` when
    subscribed. ``subtype`` is None for an ordinary new message; edits
    (``message_changed``) and deletions (``message_deleted``) arrive as
    subtypes too. Ignore :attr:`is_from_bot` messages to avoid answering the
    bot's own posts in a loop."""

    channel_type: str | None
    subtype: str | None

    @property
    def is_direct_message(self) -> bool:
        return self.channel_type == "im"

    @property
    def is_from_bot(self) -> bool:
        return super().is_from_bot or self.subtype == "bot_message"

    @property
    def is_new(self) -> bool:
        """A new message, not an event about one (edit, delete, join...)."""
        return self.subtype in NEW_MESSAGE_SUBTYPES


@dataclass(frozen=True, kw_only=True)
class Reaction(EventCallback):
    """``reaction_added`` (``added``) or ``reaction_removed``. The item is a
    message (``item_channel`` + ``item_ts``) or a file (``item_file``);
    ``item_user`` owns it, when Slack says."""

    added: bool
    reaction: str
    user: str
    item_type: str
    item_channel: str | None
    item_ts: str | None
    item_file: str | None
    item_user: str | None


@dataclass(frozen=True, kw_only=True)
class TokensRevoked(EventCallback):
    """Tokens were revoked: delete the stored tokens of these Slack users
    (``oauth``: user tokens; ``bot``: the bot token)."""

    oauth_user_ids: tuple[str, ...]
    bot_user_ids: tuple[str, ...]


@dataclass(frozen=True, kw_only=True)
class AppUninstalled(EventCallback):
    """The app was uninstalled from ``team_id``: delete every token stored
    for that workspace."""


# --------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------


def parse_request(
    headers: Mapping[str, Any] | Iterable[tuple[Any, Any]],
    raw_body: bytes | str,
    signing_secret: str,
    *,
    now: float | None = None,
) -> SlackEvent:
    """:func:`verify_request`, then :func:`parse_event` — the one call an
    endpoint needs. Raises :class:`SlackSignatureError` before reading the
    body when the request isn't Slack's."""
    verify_request(headers, raw_body, signing_secret, now=now)
    return parse_event(raw_body, headers)


def parse_event(
    body: bytes | str | Mapping[str, Any],
    headers: Mapping[str, Any] | Iterable[tuple[Any, Any]] | None = None,
) -> SlackEvent:
    """The typed form of an Events API body (raw JSON, or already decoded).
    It does **not** authenticate: call :func:`verify_request` first, or use
    :func:`parse_request`. ``headers`` supply the retry number and reason.
    Raises :class:`SlackEventError` for a body that isn't a JSON object with
    a ``type``, a ``url_verification`` without a challenge, or an
    ``event_callback`` without ``event_id`` or an ``event`` with a type. An
    event whose fields don't match its type comes back as a plain
    :class:`EventCallback` rather than failing."""
    payload = _decode(body)
    kind = payload.get("type")
    if not isinstance(kind, str) or not kind:
        raise SlackEventError("missing_type", "slack: the body has no 'type'")
    retry_num, retry_reason = _retry(headers)
    common: dict[str, Any] = {"type": kind, "body": payload, "retry_num": retry_num, "retry_reason": retry_reason}
    if kind == "url_verification":
        challenge = payload.get("challenge")
        if not isinstance(challenge, str) or not challenge:
            raise SlackEventError("missing_challenge", "slack: url_verification without a challenge")
        return UrlVerification(**common, challenge=challenge)
    if kind == "app_rate_limited":
        minute = payload.get("minute_rate_limited")
        return AppRateLimited(
            **common,
            team_id=_str(payload, "team_id"),
            api_app_id=_str(payload, "api_app_id"),
            minute_rate_limited=minute if isinstance(minute, int) and not isinstance(minute, bool) else None,
        )
    if kind != "event_callback":
        return SlackEvent(**common)
    event = payload.get("event")
    event_id = _str(payload, "event_id")
    if not isinstance(event, Mapping) or not _str(event, "type"):
        raise SlackEventError("missing_event", "slack: event_callback without an event type")
    if not event_id:
        raise SlackEventError("missing_event_id", "slack: event_callback without an event_id")
    event_time = payload.get("event_time")
    callback: dict[str, Any] = {
        **common,
        "event_id": event_id,
        "event_time": event_time if isinstance(event_time, int) and not isinstance(event_time, bool) else None,
        "team_id": _str(payload, "team_id"),
        "api_app_id": _str(payload, "api_app_id"),
        "event_type": event["type"],
        "event": event,
    }
    return _typed(callback, event) or EventCallback(**callback)


def _decode(body: Any) -> Mapping[str, Any]:
    if isinstance(body, Mapping):
        return body
    raw = _body_bytes(body)
    if raw is None:
        raise SlackEventError("not_json", "slack: the body must be bytes, str or a decoded object")
    try:
        payload = json.loads(raw)
    except (ValueError, RecursionError):  # UnicodeDecodeError is a ValueError
        raise SlackEventError("not_json", "slack: the body is not JSON") from None
    if not isinstance(payload, dict):
        raise SlackEventError("not_an_object", "slack: the body is not a JSON object")
    return payload


def _retry(headers: Mapping[str, Any] | Iterable[tuple[Any, Any]] | None) -> tuple[int | None, str | None]:
    if headers is None:
        return None, None
    num = header(headers, RETRY_NUM_HEADER)
    reason = header(headers, RETRY_REASON_HEADER)
    return (
        int(num) if num is not None and _RETRY_NUM.fullmatch(num) else None,
        reason if reason is not None and _RETRY_REASON.fullmatch(reason) else None,
    )


def _str(mapping: Mapping[str, Any], key: str) -> str | None:
    value = mapping.get(key)
    return value if isinstance(value, str) and value else None


def _typed(callback: dict[str, Any], event: Mapping[str, Any]) -> EventCallback | None:
    """The typed event, or None when its fields don't fit the type."""
    kind = event["type"]
    if kind in ("app_mention", "message"):
        channel, ts = _str(event, "channel"), _str(event, "ts")
        if channel is None or ts is None:
            return None
        text = event.get("text")
        posted = {
            **callback,
            "channel": channel,
            "user": _str(event, "user"),
            "text": text if isinstance(text, str) else "",
            "ts": ts,
            "thread_ts": _str(event, "thread_ts"),
            "bot_id": _str(event, "bot_id"),
        }
        if kind == "app_mention":
            return AppMention(**posted)
        return Message(**posted, channel_type=_str(event, "channel_type"), subtype=_str(event, "subtype"))
    if kind in ("reaction_added", "reaction_removed"):
        item = event.get("item")
        reaction, user = _str(event, "reaction"), _str(event, "user")
        if not isinstance(item, Mapping) or not _str(item, "type") or reaction is None or user is None:
            return None
        return Reaction(
            **callback,
            added=kind == "reaction_added",
            reaction=reaction,
            user=user,
            item_type=item["type"],
            item_channel=_str(item, "channel"),
            item_ts=_str(item, "ts"),
            item_file=_str(item, "file"),
            item_user=_str(event, "item_user"),
        )
    if kind == "tokens_revoked":
        tokens = event.get("tokens")
        if not isinstance(tokens, Mapping):
            return None
        return TokensRevoked(
            **callback, oauth_user_ids=_ids(tokens.get("oauth")), bot_user_ids=_ids(tokens.get("bot"))
        )
    if kind == "app_uninstalled":
        return AppUninstalled(**callback)
    return None


def _ids(value: Any) -> tuple[str, ...]:
    return tuple(v for v in value if isinstance(v, str) and v) if isinstance(value, list) else ()


# --------------------------------------------------------------------------
# Envelopes
# --------------------------------------------------------------------------

#: The message fields a message trigger carries (Composio's payload names).
_MESSAGE_FIELDS = (
    "channel", "channel_type", "user", "text", "ts", "thread_ts", "parent_user_id", "subtype",
    "bot_id", "blocks", "attachments", "files",
)


def trigger_slug(event: EventCallback) -> str | None:
    """The Composio-style trigger name for an event, or None when it is not
    one pipe triggers on (an edit, a reaction to a file, an unknown type)."""
    if isinstance(event, AppMention):
        return TRIGGER_APP_MENTION
    if isinstance(event, Message):
        if not event.is_new or event.channel_type is None:
            return None
        return TRIGGER_DIRECT_MESSAGE if event.is_direct_message else TRIGGER_CHANNEL_MESSAGE
    if isinstance(event, Reaction):
        if event.item_type != "message":
            return None
        return TRIGGER_REACTION_ADDED if event.added else TRIGGER_REACTION_REMOVED
    if event.event_type == "channel_created" and isinstance(event.event.get("channel"), Mapping):
        return TRIGGER_CHANNEL_CREATED
    return None


def event_to_envelope(event: EventCallback) -> DataEnvelope:
    """A ``DataEnvelope`` for a pipeline. ``data`` is the trigger payload in
    Composio's field names (``channel``/``ts``/``text``... for messages and
    mentions; ``message_channel``/``message_ts``/``reaction``... for
    reactions; the channel for ``channel_created``), or the inner event as
    Slack sent it when there is no trigger. ``metadata`` carries
    ``trigger_slug`` (None without one), ``event_id`` (the dedupe key),
    ``event_type``, ``team_id``, ``api_app_id`` and ``event_time``, and for
    messages and mentions ``is_from_bot`` (route those nowhere that answers,
    or the bot replies to itself)."""
    slug = trigger_slug(event)
    data: dict[str, Any]
    if isinstance(event, PostedMessage) and slug is not None:
        data = {k: event.event[k] for k in _MESSAGE_FIELDS if event.event.get(k) is not None}
        data["team_id"] = _str(event.event, "team") or event.team_id
    elif isinstance(event, Reaction) and slug is not None:
        data = {
            "user": event.user,
            "reaction": event.reaction,
            "message_channel": event.item_channel,
            "message_ts": event.item_ts,
            "message_user": event.item_user,
            "event_ts": _str(event.event, "event_ts"),
            "team_id": event.team_id,
        }
    elif slug == TRIGGER_CHANNEL_CREATED:
        channel = event.event["channel"]
        data = {k: channel[k] for k in ("id", "name", "created", "creator") if k in channel}
    else:
        data = dict(event.event)
    metadata: dict[str, Any] = {
        "trigger_slug": slug,
        "event_id": event.event_id,
        "event_type": event.event_type,
        "team_id": event.team_id,
        "api_app_id": event.api_app_id,
        "event_time": event.event_time,
    }
    if isinstance(event, PostedMessage):
        metadata["is_from_bot"] = event.is_from_bot
    return DataEnvelope(content_type="application/json", data=data, source_id="slack", metadata=metadata)


__all__ = [
    "BOT_EVENTS",
    "EVENTS_ONLY_BOT_SCOPES",
    "RETRY_NUM_HEADER",
    "RETRY_REASON_HEADER",
    "SIGNATURE_HEADER",
    "TIMESTAMP_HEADER",
    "TIMESTAMP_WINDOW_SECONDS",
    "AppMention",
    "AppRateLimited",
    "AppUninstalled",
    "EventCallback",
    "Message",
    "PostedMessage",
    "Reaction",
    "SlackEvent",
    "SlackEventError",
    "SlackRequestError",
    "SlackSignatureError",
    "TokensRevoked",
    "UrlVerification",
    "event_to_envelope",
    "header",
    "parse_event",
    "parse_request",
    "timestamp_in_window",
    "trigger_slug",
    "verify_request",
    "verify_slack_signature",
]
