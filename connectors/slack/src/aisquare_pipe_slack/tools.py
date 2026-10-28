"""The Slack toolkit's catalog: one :class:`SlackTool` row per tool.

Each row carries the LLM-facing :class:`ToolSpec`, its handler, its risk
tier, its area (the scope group it belongs to), the Slack methods it calls,
the scopes it needs for each identity, and so which identities can run it.

Slugs and argument names are Composio's Slack toolkit's, verbatim (frozen in
``tests/fixtures/composio_slack_args.json``), except where noted there.

Result shapes (``ToolResult.data``): Slack's payload minus ``ok``,
``response_metadata`` and ``warning``. Paged reads put their hints first so
they survive a caller truncating a long result:

* cursor methods: ``{"has_more", "next_cursor", ...}`` — pass ``next_cursor``
  back as ``cursor`` to continue;
* page methods (``search.*``): ``{"has_more", "next_page", ...}``.

Message lists are slimmed (the ``rich_text`` mirror of ``text`` in
``blocks``, ``user_profile`` and bot icons are dropped) and cut at
:data:`~aisquare_pipe_slack.constants.RESULT_CHAR_CAP` characters, with
``truncated: true`` and a resume hint (``resume_latest`` / ``resume_oldest``).

Channel arguments take an ID or a name. A name is looked up with
``conversations.list``; an ambiguous name is refused, never guessed — on
Enterprise Grid two workspaces can each have ``#general``.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from aisquare.pipe import (
    ERROR_INVALID_ARGUMENTS,
    ERROR_NOT_FOUND,
    ERROR_UPSTREAM_ERROR,
    ToolError,
    ToolResult,
    ToolSpec,
)

from aisquare_pipe_slack.config import SlackSettings
from aisquare_pipe_slack.constants import (
    ATTRIBUTION_TEMPLATE,
    HISTORY_DEFAULT_LIMIT,
    HISTORY_MAX_LIMIT,
    IDENTITY_BOT,
    IDENTITY_USER,
    LIST_DEFAULT_LIMIT,
    LIST_MAX_LIMIT,
    MAX_BLOCKS,
    MAX_MARKDOWN_TEXT,
    RESULT_CHAR_CAP,
    SCAN_MAX_PAGES,
    SCAN_PAGE_SIZE,
    SEARCH_DEFAULT_COUNT,
    SEARCH_MAX_COUNT,
    TIER_DESTRUCTIVE,
    TIER_READ,
    TIER_SEND,
    TIER_WRITE,
)
from aisquare_pipe_slack.rest import SlackAPIError, SlackWebClient, next_cursor


@dataclass(frozen=True)
class SlackCall:
    """What a handler works with: the client, the call's settings, whether
    the tool is a read (reads are retried on Slack server errors), and the
    attribution name for messages it posts (None: no marker)."""

    client: SlackWebClient
    settings: SlackSettings
    idempotent: bool = False
    attribution: str | None = None

    def call(self, method: str, args: Mapping[str, Any] | None = None, *, json_body: bool = False) -> dict[str, Any]:
        return self.client.call(method, args, json_body=json_body, idempotent=self.idempotent)


Handler = Callable[[SlackCall, dict[str, Any]], ToolResult]


@dataclass(frozen=True)
class SlackTool:
    """One catalog row. ``bot_scopes`` is None for a tool only a user token
    can run (search, profile writes, ...)."""

    spec: ToolSpec
    handler: Handler
    tier: str
    area: str
    methods: tuple[str, ...]
    user_scopes: tuple[str, ...]
    bot_scopes: tuple[str, ...] | None

    @property
    def identities(self) -> tuple[str, ...]:
        return (IDENTITY_USER,) if self.bot_scopes is None else (IDENTITY_USER, IDENTITY_BOT)


# --------------------------------------------------------------------------
# Scopes (per conversation type; DM/group-DM topics are not supported)
# --------------------------------------------------------------------------

HISTORY = ("channels:history", "groups:history", "im:history", "mpim:history")
READ = ("channels:read", "groups:read", "im:read", "mpim:read")
#: Channel-name lookup (``conversations.list`` over public + private channels).
LOOKUP = ("channels:read", "groups:read")


# --------------------------------------------------------------------------
# Schema helpers
# --------------------------------------------------------------------------


def _schema(properties: dict[str, Any], required: Iterable[str] = ()) -> dict[str, Any]:
    return {"type": "object", "properties": properties, "required": list(required)}


def _string(description: str, *, enum: Iterable[str] | None = None) -> dict[str, Any]:
    schema: dict[str, Any] = {"type": "string", "description": description}
    if enum is not None:
        schema["enum"] = list(enum)
    return schema


def _integer(description: str, *, default: int | None = None) -> dict[str, Any]:
    schema: dict[str, Any] = {"type": "integer", "description": description}
    if default is not None:
        schema["default"] = default
    return schema


def _boolean(description: str) -> dict[str, Any]:
    return {"type": "boolean", "description": description}


def _strings_schema(description: str) -> dict[str, Any]:
    return {"type": "array", "items": {"type": "string"}, "description": description}


CHANNEL = _string(
    "Conversation ID (C…, G… or D…), or a channel name such as 'general' or '#general'. "
    "IDs are faster: a name costs a channel lookup."
)
TS = _string(
    "Message timestamp, exactly as Slack returned it (a string such as '1761312000.004100'; "
    "never a number — that loses digits)."
)
CURSOR = _string("Pagination cursor: the previous result's next_cursor. Cursors expire; don't store them.")
USER = _string("User ID, e.g. 'U07ABC123'.")
LATEST = _string("Only messages before this timestamp (a ts string). Default: now.")
OLDEST = _string("Only messages after this timestamp (a ts string). Default: the beginning.")
INCLUSIVE = _boolean("Include messages exactly at latest/oldest.")
METADATA = _boolean("Include message metadata.")
TEAM_ID = _string("Workspace ID; only for org-wide (Enterprise Grid) tokens.")
INCLUDE_LOCALE = _boolean("Include the locale.")
BLOCKS = {
    "type": ["array", "string"],
    "items": {"type": "object"},
    "description": "Block Kit blocks (an array, or the same as a JSON string). Not with markdown_text.",
}
MARKDOWN_TEXT = _string(
    "Message in standard markdown (**bold**, _italic_, [link](url), lists, code). Up to 12,000 "
    "characters. Use this OR blocks, not both."
)
USERS_LIST = {
    "type": ["string", "array"],
    "items": {"type": "string"},
    "description": "User IDs: a comma-separated string or an array.",
}


def _limit(default: int, maximum: int) -> dict[str, Any]:
    return _integer(f"Items per page (default {default}, max {maximum}).", default=default)


def _search_props(sort_enum: bool = True) -> dict[str, Any]:
    return {
        "query": _string(
            "Search query with Slack modifiers, e.g. 'launch date in:#product from:@sam after:2026-10-01'."
        ),
        "count": _integer(f"Results per page (default {SEARCH_DEFAULT_COUNT}, max {SEARCH_MAX_COUNT}).",
                          default=SEARCH_DEFAULT_COUNT),
        "page": _integer("Page number, 1-based: the previous result's next_page."),
        "sort": _string("Sort by relevance score or by time.", enum=("score", "timestamp"))
        if sort_enum
        else _string("Sort by 'score' or 'timestamp'."),
        "sort_dir": _string("Sort direction.", enum=("desc", "asc")),
        "highlight": _boolean("Mark matching terms in the results."),
        "team_id": TEAM_ID,
    }


# --------------------------------------------------------------------------
# Argument helpers
# --------------------------------------------------------------------------

#: The shape of a conversation (C/G/D) or user (U/W) ID. Slack doesn't say
#: IDs carry a digit (Slackbot is USLACKBOT), and a person may type a channel
#: name in capitals ('GENERAL'). So a value of this shape *with* a digit is
#: used as an ID at once. One without a digit is tried as a name first, and
#: used as an ID when no such name exists: channel names are never uppercase.
_ID = re.compile(r"^[CGDUW][A-Z0-9]{6,}$")
_DIGIT = re.compile(r"[0-9]")


def _surely_an_id(text: str) -> bool:
    return bool(_ID.match(text) and _DIGIT.search(text))


def _invalid(message: str) -> ToolError:
    return ToolError(message, code=ERROR_INVALID_ARGUMENTS)


def _clamp(value: Any, default: int, maximum: int) -> int:
    try:
        number = int(value) if value is not None else default
    except (TypeError, ValueError):
        number = default
    return min(max(1, number), maximum)


def _id_list(value: Any, name: str) -> str:
    """A comma-separated ID list from a string or an array."""
    if isinstance(value, (list, tuple)):
        items = [str(v).strip() for v in value]
    else:
        items = [v.strip() for v in str(value).split(",")]
    items = [v for v in items if v]
    if not items:
        raise _invalid(f"{name} must name at least one user ID")
    return ",".join(items)


def _scan(call: SlackCall, method: str, key: str, args: Mapping[str, Any], cursor: str | None = None):
    """Walk a cursor list up to :data:`SCAN_MAX_PAGES` pages; yield
    ``(items, next_cursor)`` per page."""
    for _ in range(SCAN_MAX_PAGES):
        payload = call.client.call(
            method, {**args, "limit": SCAN_PAGE_SIZE, "cursor": cursor}, idempotent=True
        )
        items = payload.get(key)
        cursor = next_cursor(payload)
        yield [i for i in items if isinstance(i, dict)] if isinstance(items, list) else [], cursor
        if cursor is None:
            return


def resolve_channel(call: SlackCall, value: Any, *, archived: bool = False) -> str:
    """A conversation ID for ``value``: an ID as it is, a name (with or
    without '#') looked up among the channels this connection can see.
    Ambiguity is refused, never guessed. An all-caps value that names no
    channel is taken as an ID (see :data:`_ID`)."""
    text = str(value).strip()
    if _surely_an_id(text):
        return text
    name = text.removeprefix("#").strip().lower()
    if not name:
        raise _invalid("channel is empty")
    matches: list[str] = []
    cursor: str | None = None
    for page, cursor in _scan(
        call,
        "conversations.list",
        "channels",
        {"types": "public_channel,private_channel", "exclude_archived": not archived},
    ):
        matches += [str(c["id"]) for c in page if c.get("name") == name and c.get("id")]
    if not matches and _ID.match(text):
        # An all-caps value with no channel of that name is an ID — even when
        # the scan was capped, since channel names are never uppercase.
        return text
    if cursor is not None:
        # The scan stopped at its cap: a later page could hold another
        # '#name' (Enterprise Grid), so even one match is not an answer.
        raise _invalid(
            f"could not check every channel for '#{name}' (more than {SCAN_MAX_PAGES * SCAN_PAGE_SIZE} "
            "are visible); pass the channel ID instead (find it with SLACK_FIND_CHANNELS)"
        )
    if len(matches) > 1:
        raise _invalid(
            f"channel name '#{name}' matches {len(matches)} conversations; pass the channel ID "
            "(find it with SLACK_FIND_CHANNELS)"
        )
    if not matches:
        raise ToolError(
            f"no channel named '#{name}' among the channels this Slack connection can see — check "
            "the name or pass the channel ID (SLACK_FIND_CHANNELS)",
            code=ERROR_NOT_FOUND,
        )
    return matches[0]


def _resolve(call: SlackCall, args: dict[str, Any], names: Iterable[str], archived: bool = False) -> dict[str, Any]:
    resolved = dict(args)
    for name in names:
        if resolved.get(name) is not None:
            resolved[name] = resolve_channel(call, resolved[name], archived=archived)
    return resolved


# --------------------------------------------------------------------------
# Message content and attribution
# --------------------------------------------------------------------------


def _blocks(value: Any) -> list[Any] | None:
    if value is None:
        return None
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            raise _invalid("blocks is not valid JSON (pass an array of Block Kit blocks)") from None
    if not isinstance(value, list) or not all(isinstance(b, dict) for b in value):
        raise _invalid("blocks must be an array of Block Kit block objects")
    return list(value)


#: Characters markdown_text can read as markup: emphasis and strikethrough
#: (* _ ~), code (`), links ([ ]), Slack's <...> mentions and links, and the
#: escape character itself. Only these get a backslash. A typical name such as
#: 'pipe.ai (R&D)' gets none, so it reads right even if Slack ignored
#: CommonMark escapes (undocumented; tests/test_live.py checks it).
_MARKDOWN_MARKUP = re.compile(r"([\\`*_~\[\]<>])")


def mrkdwn_marker(name: str) -> str:
    """The marker for Slack mrkdwn (``text``, context blocks): ``&``, ``<``
    and ``>`` as entities, which is mrkdwn's only escaping."""
    escaped = name.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return ATTRIBUTION_TEMPLATE.format(name=escaped)


def markdown_marker(name: str) -> str:
    """The marker for ``markdown_text`` (standard markdown): only markup
    characters in the name are backslash-escaped (see
    :data:`_MARKDOWN_MARKUP`). Entities would show literally."""
    return ATTRIBUTION_TEMPLATE.format(name=_MARKDOWN_MARKUP.sub(r"\\\1", name))


def attribution_block(name: str) -> dict[str, Any]:
    """The context block (small grey text under the message) that carries
    the marker."""
    return {"type": "context", "elements": [{"type": "mrkdwn", "text": mrkdwn_marker(name)}]}


def compose_message(args: Mapping[str, Any], attribution: str | None, *, text_arg: str) -> dict[str, Any]:
    """Slack's content fields for a message tool: exactly one of
    ``markdown_text``, or ``blocks`` (with ``text_arg`` as the notification
    fallback), or plain ``text_arg`` mrkdwn — with the "Sent by
    <attribution>" marker appended visibly, escaped for its format: a
    context block after ``blocks``, an italic last line otherwise."""
    markdown = args.get("markdown_text")
    blocks = _blocks(args.get("blocks"))
    text = args.get(text_arg)
    if markdown is not None and (blocks is not None or text is not None):
        raise _invalid(f"markdown_text cannot be combined with blocks or {text_arg}; pass one of them")
    if markdown is None and blocks is None and not (isinstance(text, str) and text.strip()):
        raise _invalid(f"message content is missing: pass markdown_text, or blocks with {text_arg}")
    if markdown is not None:
        if not str(markdown).strip():
            raise _invalid("markdown_text is empty")
        content = f"{markdown}\n\n_{markdown_marker(attribution)}_" if attribution else str(markdown)
        if len(content) > MAX_MARKDOWN_TEXT:
            room = MAX_MARKDOWN_TEXT - (len(content) - len(str(markdown)))
            raise _invalid(
                f"markdown_text is too long: at most {room} characters "
                + ("(Slack's 12,000 minus the attribution line)" if attribution else "(Slack's limit)")
            )
        return {"markdown_text": content}
    if blocks is not None:
        if attribution:
            if len(blocks) >= MAX_BLOCKS:
                raise _invalid(
                    f"at most {MAX_BLOCKS - 1} blocks: one of Slack's {MAX_BLOCKS} is the attribution"
                )
            blocks.append(attribution_block(attribution))
        content_fields: dict[str, Any] = {"blocks": blocks}
        if text is not None:
            content_fields["text"] = text
        return content_fields
    return {"text": f"{text}\n_{mrkdwn_marker(attribution)}_" if attribution else text}


# --------------------------------------------------------------------------
# Result shaping
# --------------------------------------------------------------------------

_DROPPED = frozenset({"ok", "response_metadata", "warning"})


def _payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in payload.items() if k not in _DROPPED}


def _cursor_page(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Hints first, derived from the cursor (Slack's own ``has_more`` loses)."""
    cursor = next_cursor(payload)
    hints = {"has_more": cursor is not None, "next_cursor": cursor}
    return {**hints, **{k: v for k, v in _payload(payload).items() if k not in hints}}


def slim_message(message: Any) -> Any:
    """Drop what costs tokens without informing: ``user_profile``, bot
    icons, and ``blocks`` that only mirror ``text`` as ``rich_text``."""
    if not isinstance(message, dict):
        return message
    out = {k: v for k, v in message.items() if k != "user_profile"}
    blocks = out.get("blocks")
    if (
        out.get("text")
        and isinstance(blocks, list)
        and blocks
        and all(isinstance(b, dict) and b.get("type") == "rich_text" for b in blocks)
    ):
        del out["blocks"]
    bot_profile = out.get("bot_profile")
    if isinstance(bot_profile, dict) and "icons" in bot_profile:
        out["bot_profile"] = {k: v for k, v in bot_profile.items() if k != "icons"}
    return out


def _size(value: Any) -> int:
    return len(json.dumps(value, default=str, ensure_ascii=False))


def cap_messages(data: dict[str, Any], key: str, resume_key: str) -> dict[str, Any]:
    """Slim ``data[key]`` and cut it to fit :data:`RESULT_CHAR_CAP`; when cut,
    set ``truncated`` and ``resume_key`` (the last kept message's ``ts``)
    right after the paging hints."""
    items = data.get(key)
    if not isinstance(items, list):
        return data
    slimmed = [slim_message(m) for m in items]
    size = _size({**data, key: []})
    kept: list[Any] = []
    for message in slimmed:
        size += _size(message) + 2
        if size > RESULT_CHAR_CAP:
            break
        kept.append(message)
    if len(kept) == len(slimmed):
        return {**data, key: kept}
    last = kept[-1] if kept else None
    hints: dict[str, Any] = {k: data[k] for k in ("has_more", "next_cursor") if k in data}
    rest = {k: v for k, v in data.items() if k not in hints and k != key}
    return {
        **hints,
        "truncated": True,
        resume_key: last.get("ts") if isinstance(last, dict) else None,
        key: kept,
        **rest,
    }


def _search_hints(payload: Mapping[str, Any], sections: Iterable[str]) -> dict[str, Any]:
    """``has_more`` / ``next_page`` (and ``next_cursor`` for cursormark) for
    search results, from each section's ``paging``."""
    has_more = False
    page = None
    for section in sections:
        body = payload.get(section)
        paging = body.get("paging") if isinstance(body, Mapping) else None
        if isinstance(paging, Mapping):
            current, pages = paging.get("page"), paging.get("pages")
            if isinstance(current, int) and isinstance(pages, int):
                page = current
                has_more = has_more or current < pages
    hints: dict[str, Any] = {"has_more": has_more, "next_page": page + 1 if has_more and page else None}
    cursor = next_cursor(payload)
    for section in sections:
        body = payload.get(section)
        meta = body.get("pagination") if isinstance(body, Mapping) else None
        if isinstance(meta, Mapping) and isinstance(meta.get("next_cursor"), str) and meta["next_cursor"]:
            cursor = meta["next_cursor"]
    if cursor:
        hints["has_more"] = True
        hints["next_cursor"] = cursor
    return hints


# --------------------------------------------------------------------------
# Handlers
# --------------------------------------------------------------------------


def _rpc(
    method: str,
    args: Iterable[str] = (),
    *,
    channels: Iterable[str] = ("channel",),
    paged: bool = False,
    limits: tuple[int, int] | None = None,
    messages: tuple[str, str] | None = None,
    archived: bool = False,
    id_lists: Iterable[str] = (),
) -> Handler:
    """Handler passing ``args`` 1:1 to one form-encoded method call.
    ``channels`` are resolved from names; ``paged`` adds the cursor hints;
    ``limits`` clamps ``limit``; ``messages`` (key, resume key) caps a
    message list; ``id_lists`` join arrays of IDs into Slack's comma form."""
    names = tuple(args)
    channel_names = tuple(channels)
    list_names = tuple(id_lists)

    def handler(call: SlackCall, arguments: dict[str, Any]) -> ToolResult:
        values = _resolve(call, arguments, [n for n in channel_names if n in names], archived)
        sent = {n: values[n] for n in names if n in values}
        for name in list_names:
            if name in sent:
                sent[name] = _id_list(sent[name], name)
        if limits is not None and "limit" in names:
            sent["limit"] = _clamp(sent.get("limit"), *limits)
        payload = call.call(method, sent)
        data = _cursor_page(payload) if paged else _payload(payload)
        if messages is not None:
            data = cap_messages(data, *messages)
        return ToolResult.ok(data)

    return handler


def _message_tool(method: str, args: Iterable[str], *, text_arg: str, channel_arg: str = "channel") -> Handler:
    """A tool that posts (or rewrites) message content, as JSON, with the
    connection's attribution."""
    names = tuple(args)

    def handler(call: SlackCall, arguments: dict[str, Any]) -> ToolResult:
        content = compose_message(arguments, call.attribution, text_arg=text_arg)
        values = _resolve(call, arguments, [channel_arg])
        body = {n: values[n] for n in names if n in values}
        if "post_at" in body:
            body["post_at"] = _post_at(body["post_at"])
        payload = call.call(method, {**body, **content}, json_body=True)
        return ToolResult.ok(_payload(payload))

    return handler


def _post_at(value: Any) -> int:
    text = str(value).strip()
    if isinstance(value, bool) or not re.fullmatch(r"\d+(\.0+)?", text):
        raise _invalid("post_at must be a Unix timestamp in seconds, e.g. '1767225600'")
    return int(float(text))


def _search(method: str, sections: tuple[str, ...]) -> Handler:
    names = ("query", "count", "page", "cursor", "sort", "sort_dir", "highlight", "team_id")

    def handler(call: SlackCall, arguments: dict[str, Any]) -> ToolResult:
        sent = {n: arguments[n] for n in names if n in arguments}
        sent["count"] = _clamp(sent.get("count"), SEARCH_DEFAULT_COUNT, SEARCH_MAX_COUNT)
        payload = call.call(method, sent)
        data = _payload(payload)
        for section in sections:
            body = data.get(section)
            if isinstance(body, dict) and isinstance(body.get("matches"), list):
                data[section] = {**body, "matches": [slim_message(m) for m in body["matches"]]}
        return ToolResult.ok({**_search_hints(payload, sections), **data})

    return handler


def _list_unread(call: SlackCall, arguments: dict[str, Any]) -> ToolResult:
    channel = resolve_channel(call, arguments["channel"])
    info = call.call("conversations.info", {"channel": channel})
    conversation = info.get("channel")
    last_read = conversation.get("last_read") if isinstance(conversation, dict) else None
    if not isinstance(last_read, str) or not last_read:
        # Slack documents last_read for DMs; channels may not carry it.
        raise _invalid(
            "Slack reports no read position (last_read) for this conversation, so unread messages "
            "can't be told apart — this works reliably for DMs; for a channel, read recent "
            "messages with SLACK_FETCH_CONVERSATION_HISTORY (narrow it with oldest)"
        )
    payload = call.call(
        "conversations.history",
        {
            "channel": channel,
            "oldest": last_read,
            "limit": _clamp(arguments.get("limit"), HISTORY_DEFAULT_LIMIT, HISTORY_MAX_LIMIT),
            "cursor": arguments.get("cursor"),
            "include_all_metadata": arguments.get("include_all_metadata"),
        },
    )
    data = cap_messages(_cursor_page(payload), "messages", "resume_latest")
    data["last_read"] = last_read
    for key in ("unread_count", "unread_count_display"):
        if isinstance(conversation, dict) and key in conversation:
            data[key] = conversation[key]
    return ToolResult.ok(data)


def _channel_summary(channel: Mapping[str, Any]) -> dict[str, Any]:
    out = {
        k: channel[k]
        for k in ("id", "name", "is_private", "is_archived", "is_member", "num_members", "is_channel", "is_group")
        if k in channel
    }
    for key in ("topic", "purpose"):
        value = channel.get(key)
        if isinstance(value, Mapping) and value.get("value"):
            out[key] = value["value"]
    return out


def _find_channels(call: SlackCall, arguments: dict[str, Any]) -> ToolResult:
    query = str(arguments["query"]).strip().removeprefix("#").lower()
    if not query:
        raise _invalid("query is empty")
    exact = bool(arguments.get("exact_match"))
    member_only = bool(arguments.get("member_only"))
    wanted = _clamp(arguments.get("limit"), 50, LIST_MAX_LIMIT)
    list_args = {
        "types": arguments.get("types") or "public_channel,private_channel",
        "exclude_archived": arguments.get("exclude_archived", True),
        "team_id": arguments.get("team_id"),
    }

    def matches(channel: Mapping[str, Any]) -> bool:
        if member_only and not channel.get("is_member"):
            return False
        name = str(channel.get("name") or "").lower()
        if exact:
            return name == query
        texts = [name] + [
            str(channel[k].get("value") or "").lower()
            for k in ("topic", "purpose")
            if isinstance(channel.get(k), Mapping)
        ]
        return any(query in text for text in texts)

    found: list[dict[str, Any]] = []
    cursor: str | None = None
    pages = 0
    for page, cursor in _scan(call, "conversations.list", "channels", list_args, arguments.get("cursor")):
        pages += 1
        found += [_channel_summary(c) for c in page if matches(c)]
        if len(found) >= wanted:
            break
    data: dict[str, Any] = {"has_more": cursor is not None, "next_cursor": cursor}
    if cursor is not None and len(found) < wanted and pages >= SCAN_MAX_PAGES:
        data["truncated"] = True
    return ToolResult.ok({**data, "channels": found})


def _user_summary(user: Mapping[str, Any]) -> dict[str, Any]:
    out = {
        k: user[k]
        for k in ("id", "name", "real_name", "deleted", "is_bot", "is_admin", "is_restricted", "tz")
        if k in user
    }
    profile = user.get("profile")
    if isinstance(profile, Mapping):
        out["profile"] = {
            k: profile[k]
            for k in ("display_name", "real_name", "email", "title", "status_text", "status_emoji")
            if profile.get(k)
        }
    return out


def _find_users(call: SlackCall, arguments: dict[str, Any]) -> ToolResult:
    email = arguments.get("email")
    query = arguments.get("search_query")
    locale = arguments.get("include_locale")
    if email:
        # Nobody with that email is "found nobody" (members []), the same
        # shape as an unknown ID or an unmatched name.
        try:
            user = call.call("users.lookupByEmail", {"email": email}).get("user")
        except SlackAPIError as e:
            if e.error not in ("users_not_found", "user_not_found"):
                raise
            user = None
        return ToolResult.ok({"has_more": False, "next_cursor": None, "members": [user] if user else []})
    if not isinstance(query, str) or not query.strip():
        raise _invalid("pass search_query (a name, display name or user ID) or email")
    query = query.strip().removeprefix("@")
    if _surely_an_id(query) and query[0] in "UW":
        return _user_by_id(call, query, locale)
    needle = query.lower()
    exact = bool(arguments.get("exact_match"))
    wanted = _clamp(arguments.get("limit"), 50, LIST_MAX_LIMIT)

    def matches(user: Mapping[str, Any]) -> bool:
        if user.get("deleted") and not arguments.get("include_deleted"):
            return False
        if (user.get("is_bot") or user.get("id") == "USLACKBOT") and not arguments.get("include_bots"):
            return False
        if (user.get("is_restricted") or user.get("is_ultra_restricted")) and not arguments.get(
            "include_restricted"
        ):
            return False
        raw_profile = user.get("profile")
        profile: Mapping[str, Any] = raw_profile if isinstance(raw_profile, Mapping) else {}
        names = [
            str(v).lower()
            for v in (
                user.get("name"),
                user.get("real_name"),
                profile.get("display_name"),
                profile.get("real_name"),
                profile.get("email"),
            )
            if v
        ]
        return needle in names if exact else any(needle in n for n in names)

    found: list[dict[str, Any]] = []
    cursor: str | None = None
    pages = 0
    list_args = {"include_locale": locale, "team_id": arguments.get("team_id")}
    for page, cursor in _scan(call, "users.list", "members", list_args, arguments.get("cursor")):
        pages += 1
        found += [_user_summary(u) for u in page if matches(u)]
        if len(found) >= wanted:
            break
    if not found and _ID.match(query) and query[0] in "UW":
        # A digitless all-caps ID such as USLACKBOT: no name matched it.
        return _user_by_id(call, query, locale)
    data: dict[str, Any] = {"has_more": cursor is not None, "next_cursor": cursor}
    if cursor is not None and len(found) < wanted and pages >= SCAN_MAX_PAGES:
        data["truncated"] = True
    return ToolResult.ok({**data, "members": found})


def _user_by_id(call: SlackCall, user_id: str, locale: Any) -> ToolResult:
    """FIND_USERS by ID: ``users.info``; an unknown ID finds nobody."""
    try:
        user = call.call("users.info", {"user": user_id, "include_locale": locale}).get("user")
    except SlackAPIError as e:
        if e.error not in ("user_not_found", "users_not_found"):
            raise
        user = None
    return ToolResult.ok({"has_more": False, "next_cursor": None, "members": [user] if user else []})


def _reaction_name(call: SlackCall, arguments: dict[str, Any], method: str, names: tuple[str, ...]) -> ToolResult:
    values = _resolve(call, arguments, ["channel"])
    sent = {n: values[n] for n in names if n in values}
    if "name" in sent:
        sent["name"] = str(sent["name"]).strip().strip(":")
    return ToolResult.ok(_payload(call.call(method, sent)))


def _add_reaction(call: SlackCall, arguments: dict[str, Any]) -> ToolResult:
    return _reaction_name(call, arguments, "reactions.add", ("channel", "timestamp", "name"))


def _remove_reaction(call: SlackCall, arguments: dict[str, Any]) -> ToolResult:
    if not arguments.get("file") and not arguments.get("file_comment") and not (
        arguments.get("channel") and arguments.get("timestamp")
    ):
        raise _invalid("pass channel and timestamp (or file / file_comment)")
    return _reaction_name(
        call, arguments, "reactions.remove", ("channel", "timestamp", "name", "file", "file_comment")
    )


def _get_reactions(call: SlackCall, arguments: dict[str, Any]) -> ToolResult:
    if not arguments.get("file") and not arguments.get("file_comment") and not (
        arguments.get("channel") and arguments.get("timestamp")
    ):
        raise _invalid("pass channel and timestamp (or file / file_comment)")
    values = _resolve(call, arguments, ["channel"])
    names = ("channel", "timestamp", "file", "file_comment", "full")
    payload = call.call("reactions.get", {n: values[n] for n in names if n in values})
    return ToolResult.ok(_payload(payload))


def _set_user_profile(call: SlackCall, arguments: dict[str, Any]) -> ToolResult:
    profile = arguments.get("profile")
    if isinstance(profile, str):
        try:
            profile = json.loads(profile)
        except ValueError:
            raise _invalid("profile is not valid JSON (pass an object of profile fields)") from None
    if profile is not None and not isinstance(profile, dict):
        raise _invalid("profile must be an object of profile fields")
    has_pair = arguments.get("name") is not None and arguments.get("value") is not None
    if profile is None and not has_pair:
        raise _invalid("pass profile (an object of fields), or name and value for one field")
    if profile is not None and has_pair:
        raise _invalid("pass profile or name/value, not both")
    body: dict[str, Any] = {"user": arguments.get("user")}
    if profile is not None:
        body["profile"] = profile
    else:
        body["name"], body["value"] = arguments["name"], arguments["value"]
    return ToolResult.ok(_payload(call.call("users.profile.set", body, json_body=True)))


#: What OPEN_DM returns: only what sending needs. The full conversation
#: object (Slack's return_im) would carry the DM's latest message and read
#: state, which is reading, and OPEN_DM is a send-tier tool (manager ruling).
_DM_CHANNEL_FIELDS = ("id", "is_im", "is_mpim", "user")
_DM_RESULT_FIELDS = ("already_open", "no_op")


def _open_dm(call: SlackCall, arguments: dict[str, Any]) -> ToolResult:
    """``conversations.open`` without ``return_im``, returning the channel
    ID (plus ``is_im`` / ``user`` and ``already_open`` / ``no_op``) and
    never message content, for every identity and tier."""
    if not arguments.get("users") and not arguments.get("channel"):
        raise _invalid("pass users (1 user for a DM, 2–8 for a group DM) or the channel to resume")
    sent = {
        "users": _id_list(arguments["users"], "users") if arguments.get("users") else None,
        "channel": arguments.get("channel"),
        "prevent_creation": arguments.get("prevent_creation"),
    }
    payload = call.call("conversations.open", sent)
    channel = payload.get("channel")
    data: dict[str, Any] = {
        "channel": {k: channel[k] for k in _DM_CHANNEL_FIELDS if k in channel}
        if isinstance(channel, Mapping)
        else None
    }
    data.update({k: payload[k] for k in _DM_RESULT_FIELDS if k in payload})
    return ToolResult.ok(data)


def _api_test(call: SlackCall, arguments: dict[str, Any]) -> ToolResult:
    """``api.test`` echoes ``error`` back as a failure on purpose. An echo
    is reported as ``upstream_error``, never through the error table, so a
    model can't make a healthy connection look revoked (``invalid_auth``)
    or rate limited. It runs without retries, so an echoed ``ratelimited``
    can't make the call sleep."""
    sent: dict[str, Any] = {n: arguments[n] for n in ("foo", "error") if n in arguments}
    try:
        payload = call.client.call("api.test", sent, idempotent=True, retry=False)
    except SlackAPIError as e:
        if not (sent.get("error") and e.error == sent["error"]):
            raise
        return ToolResult.fail(
            f"api.test: returned the requested test error ({e.error})",
            data={"slack_error": e.error, "status": e.status, "echoed": True},
            code=ERROR_UPSTREAM_ERROR,
        )
    return ToolResult.ok(_payload(payload))


def _who_am_i(call: SlackCall, arguments: dict[str, Any]) -> ToolResult:
    auth = _payload(call.call("auth.test"))
    data: dict[str, Any] = {"identity": call.settings.identity, **auth}
    try:
        team = call.call("team.info").get("team")
    except SlackAPIError as e:
        if e.error not in ("missing_scope", "no_permission", "not_allowed_token_type"):
            raise
        team = None
    if isinstance(team, Mapping):
        data["team_domain"] = team.get("domain")
        data["team_name"] = team.get("name")
    who = auth.get("user") or auth.get("user_id") or "?"
    workspace = data.get("team_name") or auth.get("team") or auth.get("team_id") or "?"
    data["label"] = f"{who} @ {workspace}"
    return ToolResult.ok(data)


# --------------------------------------------------------------------------
# Catalog
# --------------------------------------------------------------------------


def _tool(
    slug: str,
    tier: str,
    area: str,
    methods: str | tuple[str, ...],
    description: str,
    schema: dict[str, Any],
    handler: Handler,
    *,
    user: Iterable[str] = (),
    bot: Iterable[str] | None = (),
    tags: Iterable[str] = (),
    overwrites: bool = False,
) -> SlackTool:
    """A row. ``read_only`` follows the read tier; ``destructive`` follows
    the destructive tier, or ``overwrites`` for a write that replaces content
    (an edit) — the owner put edits in the opt-in write tier."""
    spec = ToolSpec(
        slug,
        description,
        schema,
        read_only=tier == TIER_READ,
        destructive=tier == TIER_DESTRUCTIVE or overwrites,
        tags=tuple(tags),
    )
    return SlackTool(
        spec=spec,
        handler=handler,
        tier=tier,
        area=area,
        methods=(methods,) if isinstance(methods, str) else methods,
        user_scopes=tuple(sorted(set(user))),
        bot_scopes=None if bot is None else tuple(sorted(set(bot))),
    )


_SEND_ARGS = ("channel", "thread_ts", "reply_broadcast", "unfurl_links", "unfurl_media")

CATALOG: tuple[SlackTool, ...] = (
    # -- messages ------------------------------------------------------------
    _tool(
        "SLACK_SEND_MESSAGE", TIER_SEND, "messages", "chat.postMessage",
        "Post a message to a channel, DM or group DM, or reply in a thread (thread_ts; "
        "reply_broadcast also shows the reply in the channel). Content is markdown_text "
        "(standard markdown) OR blocks (Block Kit) with fallback_text for notifications. "
        "Messages sent as the user carry a visible 'Sent by <name>' line. To DM someone, get the "
        "DM channel with SLACK_OPEN_DM first. Returns channel and ts of the new message.",
        _schema(
            {
                "channel": CHANNEL,
                "markdown_text": MARKDOWN_TEXT,
                "blocks": BLOCKS,
                "fallback_text": _string("Plain-text fallback for notifications when sending blocks."),
                "thread_ts": _string("Reply in this thread: the parent message's ts."),
                "reply_broadcast": _boolean("With thread_ts: also post the reply to the channel."),
                "unfurl_links": _boolean("Expand link previews."),
                "unfurl_media": _boolean("Expand media previews."),
            },
            ["channel"],
        ),
        _message_tool("chat.postMessage", _SEND_ARGS, text_arg="fallback_text"),
        user=("chat:write", *LOOKUP),
        bot=("chat:write", "chat:write.public", *LOOKUP),
        tags=("post", "send", "write", "reply", "thread", "dm", "notify", "message", "chat"),
    ),
    _tool(
        "SLACK_UPDATES_A_SLACK_MESSAGE", TIER_WRITE, "messages", "chat.update",
        "Edit a message this connection posted (channel + ts): replace its content with "
        "markdown_text, or blocks with fallback_text. The new content replaces the old entirely.",
        _schema(
            {
                "channel": CHANNEL,
                "ts": TS,
                "markdown_text": MARKDOWN_TEXT,
                "blocks": BLOCKS,
                "fallback_text": _string("Plain-text fallback for notifications when sending blocks."),
                "reply_broadcast": _boolean("Broadcast an edited thread reply to the channel."),
                "file_ids": _strings_schema("IDs of files to attach to the message."),
                "metadata": {"type": "object", "description": "Message metadata (event_type and event_payload)."},
            },
            ["channel", "ts"],
        ),
        _message_tool(
            "chat.update", ("channel", "ts", "reply_broadcast", "file_ids", "metadata"), text_arg="fallback_text"
        ),
        user=("chat:write", *LOOKUP),
        bot=("chat:write", *LOOKUP),
        tags=("edit", "update", "change", "fix", "message"),
        overwrites=True,
    ),
    _tool(
        "SLACK_DELETES_A_MESSAGE_FROM_A_CHAT", TIER_DESTRUCTIVE, "messages", "chat.delete",
        "Delete a message (channel + ts). A bot can delete only its own messages; a user what "
        "they could delete in Slack. Cannot be undone.",
        _schema({"channel": CHANNEL, "ts": TS}, ["channel", "ts"]),
        _rpc("chat.delete", ("channel", "ts")),
        user=("chat:write", *LOOKUP),
        bot=("chat:write", *LOOKUP),
        tags=("delete", "remove", "message"),
    ),
    _tool(
        "SLACK_SEND_EPHEMERAL_MESSAGE", TIER_SEND, "messages", "chat.postEphemeral",
        "Post a message only one member of a channel sees ('only visible to you'). Delivery is "
        "not guaranteed and it does not persist. Content: markdown_text, text (mrkdwn), or blocks "
        "with text as the fallback.",
        _schema(
            {
                "channel": CHANNEL,
                "user": _string("ID of the channel member who will see the message."),
                "markdown_text": MARKDOWN_TEXT,
                "text": _string("Message in Slack mrkdwn (*bold*, _italic_, <url|label>)."),
                "blocks": BLOCKS,
                "thread_ts": _string("Show it in this thread: the parent message's ts."),
                "parse": _string("'full' or 'none': how Slack parses the text."),
                "link_names": _boolean("Link @names and #channels in text."),
            },
            ["channel", "user"],
        ),
        _message_tool(
            "chat.postEphemeral", ("channel", "user", "thread_ts", "parse", "link_names"), text_arg="text"
        ),
        user=("chat:write", *LOOKUP),
        bot=("chat:write", *LOOKUP),
        tags=("ephemeral", "private", "only you", "whisper", "send", "message"),
    ),
    _tool(
        "SLACK_SCHEDULE_MESSAGE", TIER_SEND, "messages", "chat.scheduleMessage",
        "Schedule a message for later: post_at is a Unix timestamp in seconds, in the future and "
        "at most 120 days ahead (Slack allows 30 per 5 minutes per channel). Content: "
        "markdown_text, text (mrkdwn), or blocks with text as the fallback. Returns "
        "scheduled_message_id.",
        _schema(
            {
                "channel": CHANNEL,
                "post_at": {
                    "type": ["string", "integer"],
                    "description": "When to post: Unix timestamp in seconds, e.g. '1767225600'.",
                },
                "markdown_text": MARKDOWN_TEXT,
                "text": _string("Message in Slack mrkdwn (*bold*, _italic_, <url|label>)."),
                "blocks": BLOCKS,
                "thread_ts": _string("Reply in this thread: the parent message's ts."),
                "reply_broadcast": _boolean("With thread_ts: also post the reply to the channel."),
                "unfurl_links": _boolean("Expand link previews."),
                "unfurl_media": _boolean("Expand media previews."),
                "parse": _string("'full' or 'none': how Slack parses the text."),
                "link_names": _boolean("Link @names and #channels in text."),
            },
            ["channel", "post_at"],
        ),
        _message_tool(
            "chat.scheduleMessage",
            (*_SEND_ARGS, "post_at", "parse", "link_names"),
            text_arg="text",
        ),
        user=("chat:write", *LOOKUP),
        bot=("chat:write", *LOOKUP),
        tags=("schedule", "later", "send", "remind", "message"),
    ),
    _tool(
        "SLACK_LIST_SCHEDULED_MESSAGES", TIER_READ, "messages", "chat.scheduledMessages.list",
        "Pending messages scheduled through this connection (not ones scheduled in the Slack "
        "app), optionally for one channel or a time window.",
        _schema(
            {
                "channel": CHANNEL,
                "latest": _string("Only messages scheduled before this Unix time."),
                "oldest": _string("Only messages scheduled after this Unix time."),
                "limit": _limit(LIST_DEFAULT_LIMIT, LIST_MAX_LIMIT),
                "cursor": CURSOR,
                "team_id": TEAM_ID,
            }
        ),
        _rpc(
            "chat.scheduledMessages.list",
            ("channel", "latest", "oldest", "limit", "cursor", "team_id"),
            paged=True,
            limits=(LIST_DEFAULT_LIMIT, LIST_MAX_LIMIT),
        ),
        user=LOOKUP,
        bot=LOOKUP,
        tags=("scheduled", "pending", "list", "message"),
    ),
    _tool(
        "SLACK_DELETE_SCHEDULED_MESSAGE", TIER_DESTRUCTIVE, "messages", "chat.deleteScheduledMessage",
        "Cancel a scheduled message (channel + scheduled_message_id from SLACK_SCHEDULE_MESSAGE or "
        "SLACK_LIST_SCHEDULED_MESSAGES). Not possible within 60 seconds of its post time.",
        _schema(
            {"channel": CHANNEL, "scheduled_message_id": _string("The scheduled message's ID (Q…).")},
            ["channel", "scheduled_message_id"],
        ),
        _rpc("chat.deleteScheduledMessage", ("channel", "scheduled_message_id")),
        user=("chat:write", *LOOKUP),
        bot=("chat:write", *LOOKUP),
        tags=("cancel", "delete", "scheduled", "message"),
    ),
    _tool(
        "SLACK_RETRIEVE_MESSAGE_PERMALINK_URL", TIER_READ, "messages", "chat.getPermalink",
        "The permanent link to a message (channel + message_ts), to share or cite it.",
        _schema({"channel": CHANNEL, "message_ts": TS}, ["channel", "message_ts"]),
        _rpc("chat.getPermalink", ("channel", "message_ts")),
        user=LOOKUP,
        bot=LOOKUP,
        tags=("permalink", "link", "url", "share", "message"),
    ),
    # -- history and threads ----------------------------------------------------
    _tool(
        "SLACK_FETCH_CONVERSATION_HISTORY", TIER_READ, "history", "conversations.history",
        "Messages in a channel or DM, newest first (thread replies are not included; read them "
        "with SLACK_FETCH_MESSAGE_THREAD_FROM_A_CONVERSATION). Narrow with oldest/latest. Costly: "
        "apps outside the Slack Marketplace may get 1 call per minute and 15 messages per page, "
        "so prefer SLACK_SEARCH_MESSAGES or one narrow page, and don't loop.",
        _schema(
            {
                "channel": CHANNEL,
                "limit": _limit(HISTORY_DEFAULT_LIMIT, HISTORY_MAX_LIMIT),
                "cursor": CURSOR,
                "latest": LATEST,
                "oldest": OLDEST,
                "inclusive": INCLUSIVE,
                "include_all_metadata": METADATA,
            },
            ["channel"],
        ),
        _rpc(
            "conversations.history",
            ("channel", "limit", "cursor", "latest", "oldest", "inclusive", "include_all_metadata"),
            paged=True,
            limits=(HISTORY_DEFAULT_LIMIT, HISTORY_MAX_LIMIT),
            messages=("messages", "resume_latest"),
        ),
        user=(*HISTORY, *LOOKUP),
        bot=(*HISTORY, *LOOKUP),
        tags=("history", "read", "messages", "recent", "channel", "conversation"),
    ),
    _tool(
        "SLACK_FETCH_MESSAGE_THREAD_FROM_A_CONVERSATION", TIER_READ, "history", "conversations.replies",
        "A thread: its parent message (channel + ts of the parent) and the replies, oldest first. "
        "Same rate limit caveat as SLACK_FETCH_CONVERSATION_HISTORY.",
        _schema(
            {
                "channel": CHANNEL,
                "ts": _string("The thread's parent message ts, exactly as Slack returned it."),
                "limit": _limit(HISTORY_DEFAULT_LIMIT, HISTORY_MAX_LIMIT),
                "cursor": CURSOR,
                "latest": LATEST,
                "oldest": OLDEST,
                "inclusive": INCLUSIVE,
                "include_all_metadata": METADATA,
            },
            ["channel", "ts"],
        ),
        _rpc(
            "conversations.replies",
            ("channel", "ts", "limit", "cursor", "latest", "oldest", "inclusive", "include_all_metadata"),
            paged=True,
            limits=(HISTORY_DEFAULT_LIMIT, HISTORY_MAX_LIMIT),
            messages=("messages", "resume_oldest"),
        ),
        user=(*HISTORY, *LOOKUP),
        bot=(*HISTORY, *LOOKUP),
        tags=("thread", "replies", "read", "conversation", "messages"),
    ),
    _tool(
        "SLACK_LIST_UNREAD_CHANNEL_MESSAGES", TIER_READ, "history",
        ("conversations.info", "conversations.history"),
        "Messages the connected user has not read yet in a DM or group DM (after their read "
        "position, last_read). Reliable for DMs; Slack may not report a read position for "
        "channels, and then this fails with invalid_arguments. User identity only. Costs one "
        "history call (see the rate limit caveat on SLACK_FETCH_CONVERSATION_HISTORY).",
        _schema(
            {
                "channel": CHANNEL,
                "limit": _limit(HISTORY_DEFAULT_LIMIT, HISTORY_MAX_LIMIT),
                "cursor": CURSOR,
                "include_all_metadata": METADATA,
            },
            ["channel"],
        ),
        _list_unread,
        user=(*READ, *HISTORY),
        bot=None,
        tags=("unread", "new", "missed", "catch up", "messages", "channel"),
    ),
    # -- search ------------------------------------------------------------------
    _tool(
        "SLACK_SEARCH_MESSAGES", TIER_READ, "search", "search.messages",
        "Full-text search over messages the connected user can see, with Slack modifiers "
        "(in:#channel, from:@user, has:link, before:/after:/on: YYYY-MM-DD, \"exact phrase\"). "
        "Returns matches with channel, ts, text and permalink; page with page/next_page. User "
        "identity only.",
        _schema(
            {
                **_search_props(),
                "cursor": _string(
                    "Cursor paging instead of page: '*' for the first page, then the previous "
                    "result's next_cursor."
                ),
            },
            ["query"],
        ),
        _search("search.messages", ("messages",)),
        user=("search:read",),
        bot=None,
        tags=("search", "find", "messages", "query", "lookup", "discussion"),
    ),
    _tool(
        "SLACK_SEARCH_FILES", TIER_READ, "search", "search.files",
        "Search files the connected user can see (names, titles, contents of text files), with "
        "Slack modifiers. User identity only.",
        _schema(_search_props(), ["query"]),
        _search("search.files", ("files",)),
        user=("search:read",),
        bot=None,
        tags=("search", "find", "files", "documents", "attachments"),
    ),
    _tool(
        "SLACK_SEARCH_ALL", TIER_READ, "search", "search.all",
        "Search messages and files in one call, with Slack modifiers. User identity only.",
        _schema(_search_props(), ["query"]),
        _search("search.all", ("messages", "files")),
        user=("search:read",),
        bot=None,
        tags=("search", "find", "everything", "messages", "files"),
    ),
    _tool(
        "SLACK_FIND_CHANNELS", TIER_READ, "search", "conversations.list",
        "Find channels whose name, topic or purpose contains query (exact_match: the name equals "
        "it). Scans up to 2,000 channels per call; continue with next_cursor. Returns id, name, "
        "privacy, membership, member count, topic and purpose.",
        _schema(
            {
                "query": _string("Text to look for, e.g. 'product' or '#product-launch'."),
                "exact_match": _boolean("Only channels whose name is exactly query."),
                "member_only": _boolean("Only channels the connection is a member of."),
                "exclude_archived": _boolean("Skip archived channels (default true)."),
                "types": _string(
                    "Comma-separated: public_channel, private_channel, mpim, im "
                    "(default public_channel,private_channel)."
                ),
                "limit": _integer("Stop after this many matches (default 50)."),
                "cursor": CURSOR,
                "team_id": TEAM_ID,
            },
            ["query"],
        ),
        _find_channels,
        user=READ,
        bot=READ,
        tags=("find", "search", "channel", "lookup", "which channel", "name"),
    ),
    _tool(
        "SLACK_FIND_USERS", TIER_READ, "search", ("users.info", "users.lookupByEmail", "users.list"),
        "Find people: by email (exact), by user ID, or by search_query matched against username, "
        "real name, display name and email. Deleted users, bots and guests are skipped unless "
        "asked for. Returns id, name, real name, title, status and email (when visible).",
        _schema(
            {
                "search_query": _string("Name, display name, '@handle' or user ID to look for."),
                "email": _string("Exact email address."),
                "exact_match": _boolean("Only users whose name or email equals search_query."),
                "include_bots": _boolean("Include bots and apps."),
                "include_deleted": _boolean("Include deactivated users."),
                "include_restricted": _boolean("Include guests."),
                "include_locale": INCLUDE_LOCALE,
                "limit": _integer("Stop after this many matches (default 50)."),
                "cursor": CURSOR,
                "team_id": TEAM_ID,
            }
        ),
        _find_users,
        user=("users:read", "users:read.email"),
        bot=("users:read", "users:read.email"),
        tags=("find", "search", "people", "person", "user", "who", "email", "colleague"),
    ),
    # -- conversations -----------------------------------------------------------
    _tool(
        "SLACK_LIST_ALL_CHANNELS", TIER_READ, "conversations", "conversations.list",
        "List conversations in the workspace by type (public_channel, private_channel, mpim, im; "
        "default public_channel), one page per call.",
        _schema(
            {
                "types": _string("Comma-separated: public_channel, private_channel, mpim, im."),
                "exclude_archived": _boolean("Skip archived channels."),
                "limit": _limit(LIST_DEFAULT_LIMIT, LIST_MAX_LIMIT),
                "cursor": CURSOR,
                "team_id": TEAM_ID,
            }
        ),
        _rpc(
            "conversations.list",
            ("types", "exclude_archived", "limit", "cursor", "team_id"),
            paged=True,
            limits=(LIST_DEFAULT_LIMIT, LIST_MAX_LIMIT),
        ),
        user=READ,
        bot=READ,
        tags=("list", "channels", "conversations", "all"),
    ),
    _tool(
        "SLACK_LIST_CONVERSATIONS", TIER_READ, "conversations", "users.conversations",
        "Conversations a user is a member of (default: the connected user or bot), by type.",
        _schema(
            {
                "user": _string("User ID; default the connection itself."),
                "types": _string("Comma-separated: public_channel, private_channel, mpim, im."),
                "exclude_archived": _boolean("Skip archived channels."),
                "limit": _limit(LIST_DEFAULT_LIMIT, LIST_MAX_LIMIT),
                "cursor": CURSOR,
                "team_id": TEAM_ID,
            }
        ),
        _rpc(
            "users.conversations",
            ("user", "types", "exclude_archived", "limit", "cursor", "team_id"),
            paged=True,
            limits=(LIST_DEFAULT_LIMIT, LIST_MAX_LIMIT),
        ),
        user=READ,
        bot=READ,
        tags=("my channels", "member", "joined", "list", "conversations"),
    ),
    _tool(
        "SLACK_RETRIEVE_CONVERSATION_INFORMATION", TIER_READ, "conversations", "conversations.info",
        "A conversation's details: name, topic, purpose, privacy, archive state, creator, and "
        "(with include_num_members) the member count.",
        _schema(
            {
                "channel": CHANNEL,
                "include_num_members": _boolean("Include the member count."),
                "include_locale": INCLUDE_LOCALE,
            },
            ["channel"],
        ),
        _rpc("conversations.info", ("channel", "include_num_members", "include_locale")),
        user=READ,
        bot=READ,
        tags=("channel", "info", "details", "topic", "purpose", "about"),
    ),
    _tool(
        "SLACK_RETRIEVE_CONVERSATION_MEMBERS_LIST", TIER_READ, "conversations", "conversations.members",
        "User IDs of a conversation's members, one page per call.",
        _schema(
            {"channel": CHANNEL, "limit": _limit(LIST_DEFAULT_LIMIT, LIST_MAX_LIMIT), "cursor": CURSOR},
            ["channel"],
        ),
        _rpc(
            "conversations.members",
            ("channel", "limit", "cursor"),
            paged=True,
            limits=(LIST_DEFAULT_LIMIT, LIST_MAX_LIMIT),
        ),
        user=READ,
        bot=READ,
        tags=("members", "people", "who is in", "channel", "participants"),
    ),
    _tool(
        "SLACK_CREATE_CHANNEL", TIER_WRITE, "conversations", "conversations.create",
        "Create a public or private (is_private) channel. Names are lowercase, without spaces or "
        "periods, at most 80 characters.",
        _schema(
            {
                "name": _string("Channel name, e.g. 'project-apollo'."),
                "is_private": _boolean("Create a private channel."),
                "team_id": TEAM_ID,
            },
            ["name"],
        ),
        _rpc("conversations.create", ("name", "is_private", "team_id"), channels=()),
        user=("channels:write", "groups:write"),
        bot=("channels:manage", "groups:write"),
        tags=("create", "new", "channel"),
    ),
    _tool(
        "SLACK_JOIN_AN_EXISTING_CONVERSATION", TIER_WRITE, "conversations", "conversations.join",
        "Join a public channel.",
        _schema({"channel": CHANNEL}, ["channel"]),
        _rpc("conversations.join", ("channel",)),
        user=("channels:write", *LOOKUP),
        bot=("channels:join", *LOOKUP),
        tags=("join", "enter", "channel"),
    ),
    _tool(
        "SLACK_LEAVE_CONVERSATION", TIER_DESTRUCTIVE, "conversations", "conversations.leave",
        "Leave a conversation. Leaving a private channel can't be undone by this connection: "
        "only a member can invite it back. (As the bot: channels and DMs, not group DMs.)",
        _schema({"channel": CHANNEL}, ["channel"]),
        _rpc("conversations.leave", ("channel",)),
        user=("channels:write", "groups:write", "im:write", "mpim:write", *LOOKUP),
        bot=("channels:manage", "groups:write", "im:write", *LOOKUP),
        tags=("leave", "exit", "channel"),
    ),
    _tool(
        "SLACK_INVITE_USERS_TO_A_SLACK_CHANNEL", TIER_WRITE, "conversations", "conversations.invite",
        "Invite 1–1000 users to a channel the connection is in. All or nothing unless force is "
        "set (then valid users are added and the rest reported).",
        _schema(
            {
                "channel": CHANNEL,
                "users": USERS_LIST,
                "force": _boolean("Add the valid users even if some IDs are invalid."),
            },
            ["channel", "users"],
        ),
        _rpc("conversations.invite", ("channel", "users", "force"), id_lists=("users",)),
        user=("channels:write.invites", "groups:write.invites", *LOOKUP),
        bot=("channels:write.invites", "groups:write.invites", *LOOKUP),
        tags=("invite", "add", "people", "channel", "members"),
    ),
    _tool(
        "SLACK_REMOVE_USER_FROM_CONVERSATION", TIER_DESTRUCTIVE, "conversations", "conversations.kick",
        "Remove a user from a channel.",
        _schema({"channel": CHANNEL, "user": USER}, ["channel", "user"]),
        _rpc("conversations.kick", ("channel", "user")),
        user=("channels:write", "groups:write", *LOOKUP),
        bot=("channels:manage", "groups:write", *LOOKUP),
        tags=("remove", "kick", "member", "channel"),
    ),
    _tool(
        "SLACK_ARCHIVE_CONVERSATION", TIER_DESTRUCTIVE, "conversations", "conversations.archive",
        "Archive a channel: it becomes read-only and leaves everyone's sidebar.",
        _schema({"channel": CHANNEL}, ["channel"]),
        _rpc("conversations.archive", ("channel",)),
        user=("channels:write", "groups:write", *LOOKUP),
        bot=("channels:manage", "groups:write", *LOOKUP),
        tags=("archive", "close", "channel"),
    ),
    _tool(
        "SLACK_UNARCHIVE_CHANNEL", TIER_WRITE, "conversations", "conversations.unarchive",
        "Unarchive a channel. User identity only (Slack does not support this for bots).",
        _schema({"channel": CHANNEL}, ["channel"]),
        _rpc("conversations.unarchive", ("channel",), archived=True),
        user=("channels:write", "groups:write", *LOOKUP),
        bot=None,
        tags=("unarchive", "restore", "reopen", "channel"),
    ),
    _tool(
        "SLACK_RENAME_CONVERSATION", TIER_WRITE, "conversations", "conversations.rename",
        "Rename a channel. Only its creator, a workspace admin or a channel manager may.",
        _schema({"channel": CHANNEL, "name": _string("New channel name (lowercase, no spaces).")},
                ["channel", "name"]),
        _rpc("conversations.rename", ("channel", "name")),
        user=("channels:write", "groups:write", *LOOKUP),
        bot=("channels:manage", "groups:write", *LOOKUP),
        tags=("rename", "name", "channel"),
    ),
    _tool(
        "SLACK_SET_THE_TOPIC_OF_A_CONVERSATION", TIER_WRITE, "conversations", "conversations.setTopic",
        "Set a channel's topic (shown in the channel header).",
        _schema({"channel": CHANNEL, "topic": _string("New topic; plain text.")}, ["channel", "topic"]),
        _rpc("conversations.setTopic", ("channel", "topic")),
        user=("channels:write.topic", "groups:write.topic", *LOOKUP),
        bot=("channels:write.topic", "groups:write.topic", *LOOKUP),
        tags=("topic", "header", "channel", "set"),
    ),
    _tool(
        "SLACK_SET_CONVERSATION_PURPOSE", TIER_WRITE, "conversations", "conversations.setPurpose",
        "Set a channel's purpose (its description).",
        _schema({"channel": CHANNEL, "purpose": _string("New purpose; plain text.")}, ["channel", "purpose"]),
        _rpc("conversations.setPurpose", ("channel", "purpose")),
        user=("channels:write.topic", "groups:write.topic", *LOOKUP),
        bot=("channels:write.topic", "groups:write.topic", *LOOKUP),
        tags=("purpose", "description", "channel", "set"),
    ),
    # -- DMs ---------------------------------------------------------------------
    _tool(
        "SLACK_OPEN_DM", TIER_SEND, "dms", "conversations.open",
        "Open (or find) a DM with one user, or a group DM with 2–8 users, and return its channel "
        "ID (D…) for SLACK_SEND_MESSAGE. Returns the existing conversation when there is one "
        "(already_open). Returns the channel ID only, never the DM's messages (read them with "
        "SLACK_FETCH_CONVERSATION_HISTORY). As the bot: DMs only, not group DMs.",
        _schema(
            {
                "users": USERS_LIST,
                "channel": _string("Resume this existing DM or group DM instead (its ID)."),
                "prevent_creation": _boolean("Only find an existing DM; don't create one."),
            }
        ),
        _open_dm,
        user=("im:write", "mpim:write"),
        bot=("im:write",),
        tags=("dm", "direct message", "private message", "open", "conversation", "message someone"),
    ),
    _tool(
        "SLACK_CLOSE_DM", TIER_WRITE, "dms", "conversations.close",
        "Close a DM or group DM (hide it from the sidebar; history is kept).",
        _schema({"channel": _string("The DM or group DM's ID (D… or G…).")}, ["channel"]),
        _rpc("conversations.close", ("channel",), channels=()),
        user=("im:write", "mpim:write"),
        bot=("im:write",),
        tags=("close", "hide", "dm", "direct message"),
    ),
    # -- reactions -----------------------------------------------------------
    _tool(
        "SLACK_ADD_REACTION_TO_AN_ITEM", TIER_WRITE, "reactions", "reactions.add",
        "Add an emoji reaction to a message (channel + timestamp of the message; name without "
        "colons, e.g. 'thumbsup' or '+1').",
        _schema(
            {"channel": CHANNEL, "timestamp": TS, "name": _string("Emoji name, e.g. 'thumbsup', 'eyes'.")},
            ["channel", "timestamp", "name"],
        ),
        _add_reaction,
        user=("reactions:write", *LOOKUP),
        bot=("reactions:write", *LOOKUP),
        tags=("react", "reaction", "emoji", "thumbs up", "like", "acknowledge"),
    ),
    _tool(
        "SLACK_REMOVE_REACTION_FROM_ITEM", TIER_WRITE, "reactions", "reactions.remove",
        "Remove one of this connection's emoji reactions from a message (channel + timestamp), a "
        "file or a file comment.",
        _schema(
            {
                "name": _string("Emoji name, e.g. 'thumbsup'."),
                "channel": CHANNEL,
                "timestamp": TS,
                "file": _string("File ID, instead of channel + timestamp."),
                "file_comment": _string("File comment ID, instead of channel + timestamp."),
            },
            ["name"],
        ),
        _remove_reaction,
        user=("reactions:write", *LOOKUP),
        bot=("reactions:write", *LOOKUP),
        tags=("unreact", "remove", "reaction", "emoji"),
    ),
    _tool(
        "SLACK_FETCH_ITEM_REACTIONS", TIER_READ, "reactions", "reactions.get",
        "Reactions on a message (channel + timestamp), a file or a file comment: each emoji, its "
        "count and who reacted.",
        _schema(
            {
                "channel": CHANNEL,
                "timestamp": TS,
                "file": _string("File ID, instead of channel + timestamp."),
                "file_comment": _string("File comment ID, instead of channel + timestamp."),
                "full": _boolean("List every reacting user (not just a sample)."),
            }
        ),
        _get_reactions,
        user=("reactions:read", *LOOKUP),
        bot=("reactions:read", *LOOKUP),
        tags=("reactions", "emoji", "who reacted", "votes"),
    ),
    _tool(
        "SLACK_LIST_USER_REACTIONS", TIER_READ, "reactions", "reactions.list",
        "Items a user (default: the connection) reacted to, newest first.",
        _schema(
            {
                "user": _string("User ID; default the connection itself."),
                "full": _boolean("List every reacting user per item."),
                "limit": _limit(LIST_DEFAULT_LIMIT, LIST_MAX_LIMIT),
                "cursor": CURSOR,
                "count": _integer("Legacy page size (use limit + cursor)."),
                "page": _integer("Legacy page number (use limit + cursor)."),
                "team_id": TEAM_ID,
            }
        ),
        _rpc(
            "reactions.list",
            ("user", "full", "limit", "cursor", "count", "page", "team_id"),
            channels=(),
            paged=True,
            limits=(LIST_DEFAULT_LIMIT, LIST_MAX_LIMIT),
        ),
        user=("reactions:read",),
        bot=("reactions:read",),
        tags=("reactions", "reacted", "emoji", "list"),
    ),
    # -- pins --------------------------------------------------------------------
    _tool(
        "SLACK_PIN_ITEM", TIER_WRITE, "pins", "pins.add",
        "Pin a message to its channel (channel + timestamp).",
        _schema({"channel": CHANNEL, "timestamp": TS}, ["channel", "timestamp"]),
        _rpc("pins.add", ("channel", "timestamp")),
        user=("pins:write", *LOOKUP),
        bot=("pins:write", *LOOKUP),
        tags=("pin", "highlight", "save", "message"),
    ),
    _tool(
        "SLACK_UNPIN_ITEM", TIER_WRITE, "pins", "pins.remove",
        "Unpin a message (channel + timestamp).",
        _schema({"channel": CHANNEL, "timestamp": TS}, ["channel", "timestamp"]),
        _rpc("pins.remove", ("channel", "timestamp")),
        user=("pins:write", *LOOKUP),
        bot=("pins:write", *LOOKUP),
        tags=("unpin", "remove", "pin"),
    ),
    _tool(
        "SLACK_LIST_PINNED_ITEMS", TIER_READ, "pins", "pins.list",
        "Everything pinned in a channel.",
        _schema({"channel": CHANNEL}, ["channel"]),
        _rpc("pins.list", ("channel",)),
        user=("pins:read", *LOOKUP),
        bot=("pins:read", *LOOKUP),
        tags=("pins", "pinned", "important", "list"),
    ),
    # -- users, profiles, presence -----------------------------------------------
    _tool(
        "SLACK_LIST_ALL_USERS", TIER_READ, "users", "users.list",
        "Every member of the workspace, one page per call (includes deactivated users and bots; "
        "email only with the users:read.email scope).",
        _schema(
            {
                "limit": _limit(LIST_DEFAULT_LIMIT, LIST_MAX_LIMIT),
                "cursor": CURSOR,
                "include_locale": INCLUDE_LOCALE,
                "team_id": TEAM_ID,
            }
        ),
        _rpc(
            "users.list",
            ("limit", "cursor", "include_locale", "team_id"),
            channels=(),
            paged=True,
            limits=(LIST_DEFAULT_LIMIT, LIST_MAX_LIMIT),
        ),
        user=("users:read", "users:read.email"),
        bot=("users:read", "users:read.email"),
        tags=("users", "people", "members", "directory", "list", "everyone"),
    ),
    _tool(
        "SLACK_RETRIEVE_DETAILED_USER_INFORMATION", TIER_READ, "users", "users.info",
        "One user's full record: names, title, time zone, admin/guest/bot flags, profile.",
        _schema({"user": USER, "include_locale": INCLUDE_LOCALE}, ["user"]),
        _rpc("users.info", ("user", "include_locale"), channels=()),
        user=("users:read",),
        bot=("users:read",),
        tags=("user", "person", "info", "details", "who"),
    ),
    _tool(
        "SLACK_FIND_USER_BY_EMAIL_ADDRESS", TIER_READ, "users", "users.lookupByEmail",
        "The user with exactly this email address (deactivated users are not found).",
        _schema({"email": _string("Email address.")}, ["email"]),
        _rpc("users.lookupByEmail", ("email",), channels=()),
        user=("users:read.email",),
        bot=("users:read.email",),
        tags=("email", "find", "user", "lookup", "person"),
    ),
    _tool(
        "SLACK_RETRIEVE_USER_PROFILE_INFORMATION", TIER_READ, "users", "users.profile.get",
        "A user's profile (default: the connection's own): names, title, phone, status text and "
        "emoji, custom fields (with include_labels, their names).",
        _schema(
            {"user": _string("User ID; default the connection itself."),
             "include_labels": _boolean("Name the custom profile fields.")}
        ),
        _rpc("users.profile.get", ("user", "include_labels"), channels=()),
        user=("users.profile:read",),
        bot=("users.profile:read",),
        tags=("profile", "status", "title", "user", "about"),
    ),
    _tool(
        "SLACK_SET_USER_PROFILE", TIER_WRITE, "users", "users.profile.set",
        "Set the connected user's profile fields, including status: profile is an object such "
        "as {\"status_text\": \"In a meeting\", \"status_emoji\": \":calendar:\", "
        "\"status_expiration\": 0}; or set one field with name + value. Slack allows 10 updates "
        "a minute per user. User identity only.",
        _schema(
            {
                "profile": {
                    "type": ["object", "string"],
                    "description": "Profile fields to set (an object, or the same as a JSON string).",
                },
                "name": _string("One field to set, e.g. 'title' (with value)."),
                "value": _string("The value for name."),
                "user": _string("Another user's ID; admins on paid plans only."),
            }
        ),
        _set_user_profile,
        user=("users.profile:write",),
        bot=None,
        tags=("status", "profile", "set", "away message", "title", "update"),
    ),
    _tool(
        "SLACK_GET_USER_PRESENCE", TIER_READ, "users", "users.getPresence",
        "Whether a user (default: the connection) is active or away.",
        _schema({"user": _string("User ID; default the connection itself.")}),
        _rpc("users.getPresence", ("user",), channels=()),
        user=("users:read",),
        bot=("users:read",),
        tags=("presence", "online", "away", "active", "available"),
    ),
    _tool(
        "SLACK_SET_USER_PRESENCE", TIER_WRITE, "users", "users.setPresence",
        "Set the connection's own presence: 'auto' (Slack decides) or 'away'.",
        _schema({"presence": _string("auto or away.", enum=("auto", "away"))}, ["presence"]),
        _rpc("users.setPresence", ("presence",), channels=()),
        user=("users:write",),
        bot=("users:write",),
        tags=("presence", "away", "available", "set"),
    ),
    # -- workspace ---------------------------------------------------------------
    _tool(
        "SLACK_FETCH_TEAM_INFO", TIER_READ, "workspace", "team.info",
        "The workspace's name, domain and icon (or another workspace's, by team or domain).",
        _schema({"team": _string("Workspace ID; default this one."), "domain": _string("Workspace domain.")}),
        _rpc("team.info", ("team", "domain"), channels=()),
        user=("team:read",),
        bot=("team:read",),
        tags=("workspace", "team", "organization", "info"),
    ),
    _tool(
        "SLACK_LIST_CUSTOM_EMOJIS", TIER_READ, "workspace", "emoji.list",
        "The workspace's custom emoji (name to image URL or alias).",
        _schema({"include_categories": _boolean("Also list the standard emoji categories.")}),
        _rpc("emoji.list", ("include_categories",), channels=()),
        user=("emoji:read",),
        bot=("emoji:read",),
        tags=("emoji", "custom", "reactions", "list"),
    ),
    _tool(
        "SLACK_TEST_AUTH", TIER_READ, "workspace", "auth.test",
        "Who this connection is: user (or bot) ID and name, workspace ID, name and URL. Also a "
        "health check after connecting.",
        _schema({}),
        _rpc("auth.test", (), channels=()),
        user=(),
        bot=(),
        tags=("whoami", "auth", "identity", "health", "check", "connection"),
    ),
    _tool(
        "SLACK_WHO_AM_I", TIER_READ, "workspace", ("auth.test", "team.info"),
        "Who this connection acts as (identity user or bot), in which workspace, with a readable "
        "label such as 'sam @ Acme'.",
        _schema({}),
        _who_am_i,
        user=("team:read",),
        bot=("team:read",),
        tags=("whoami", "me", "identity", "account", "workspace"),
    ),
    _tool(
        "SLACK_API_TEST", TIER_READ, "workspace", "api.test",
        "Check that Slack's API is reachable; echoes its arguments (error makes it fail on "
        "purpose, for testing error handling).",
        _schema({"foo": _string("Any value to echo back."), "error": _string("An error code Slack should return.")}),
        _api_test,
        user=(),
        bot=(),
        tags=("ping", "test", "connectivity", "health"),
    ),
)


TOOLS: dict[str, SlackTool] = {tool.spec.slug: tool for tool in CATALOG}
TOOL_SPECS: tuple[ToolSpec, ...] = tuple(tool.spec for tool in CATALOG)
