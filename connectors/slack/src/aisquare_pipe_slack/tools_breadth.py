"""The breadth tools (plan PR-2): files, canvases, bookmarks, DnD, reminders,
user groups, profile photo, read cursor and a few more.

Rows are built with :mod:`aisquare_pipe_slack.tools`' helpers and use the
same tier, identity and attribution machinery as the core tools. Most of
them change state, so most are opt-in (write or destructive tiers). The PR
body justifies every row's tier.
"""

from __future__ import annotations

import base64
import binascii
import json
from collections.abc import Mapping
from typing import Any

from aisquare.pipe import ERROR_INVALID_ARGUMENTS, ToolError, ToolResult

from aisquare_pipe_slack.constants import (
    HISTORY_DEFAULT_LIMIT,
    HISTORY_MAX_LIMIT,
    LIST_DEFAULT_LIMIT,
    LIST_MAX_LIMIT,
    TIER_DESTRUCTIVE,
    TIER_READ,
    TIER_SEND,
    TIER_WRITE,
)
from aisquare_pipe_slack.files import download_file, upload_file
from aisquare_pipe_slack.tools import (
    CHANNEL,
    CURSOR,
    LOOKUP,
    TEAM_ID,
    TS,
    USERS_LIST,
    SlackCall,
    SlackTool,
    _boolean,
    _clamp,
    _cursor_page,
    _id_list,
    _integer,
    _invalid,
    _limit,
    _payload,
    _resolve,
    _rpc,
    _scan,
    _schema,
    _string,
    _strings_schema,
    _tool,
    cap_messages,
    mrkdwn_marker,
    resolve_channel,
)

#: Largest profile photo accepted (Slack wants 512-1024 px squares).
MAX_PHOTO_BYTES = 10 * 1024 * 1024
#: Largest text an upload accepts from the model.
MAX_UPLOAD_CONTENT = 50 * 1024 * 1024

FILE_ID = _string("File ID, e.g. 'F0123ABCD'.")
CANVAS_ID = _string("Canvas ID, e.g. 'F07ABCDEFGH' (a canvas is a file).")
USERGROUP = _string("User group ID, e.g. 'S0123ABCD'.")
REMINDER = _string("Reminder ID, e.g. 'Rm12345678'.")
BOOKMARK_CHANNEL = _string("Channel ID (C…/G…) or name ('general'/'#general') the bookmark belongs to.")
MARKDOWN_DOC = {
    "type": ["object", "string"],
    "description": (
        "Canvas content: {\"type\": \"markdown\", \"markdown\": \"# Title\\n...\"}, or just the markdown "
        "as a string."
    ),
}
INCLUDE_COUNT = _boolean("Include the member count.")
USERGROUP_FIELDS = {
    "name": _string("Group name, unique in the workspace."),
    "handle": _string("Mention handle (without @), unique in the workspace."),
    "description": _string("Short description."),
    "channels": _string("Comma-separated default channel IDs for the group."),
    "additional_channels": _string("Comma-separated extra channel IDs (sidebar section)."),
    "enable_section": _boolean("Show a sidebar section for the group's channels."),
    "include_count": INCLUDE_COUNT,
    "team_id": TEAM_ID,
}


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def _markdown_doc(value: Any, name: str) -> dict[str, Any] | None:
    """Canvas content as Slack wants it: ``{"type": "markdown", "markdown": …}``."""
    if value is None:
        return None
    if isinstance(value, str):
        return {"type": "markdown", "markdown": value}
    if isinstance(value, Mapping) and isinstance(value.get("markdown"), str):
        return {"type": str(value.get("type") or "markdown"), "markdown": value["markdown"]}
    raise _invalid(f"{name} must be markdown text, or {{\"type\": \"markdown\", \"markdown\": \"…\"}}")


def _json_call(method: str, names: tuple[str, ...], *, channels: tuple[str, ...] = ()) -> Any:
    """Handler for a JSON-bodied method: ``names`` 1:1; ``channels`` resolved."""

    def handler(call: SlackCall, arguments: dict[str, Any]) -> ToolResult:
        values = _resolve(call, arguments, [c for c in channels if c in arguments])
        body = {n: values[n] for n in names if n in values}
        return ToolResult.ok(_payload(call.call(method, body, json_body=True)))

    return handler


def _page_hints(payload: Mapping[str, Any]) -> dict[str, Any]:
    """``has_more`` / ``next_page`` from a ``paging`` object (files.list)."""
    paging = payload.get("paging")
    page = paging.get("page") if isinstance(paging, Mapping) else None
    pages = paging.get("pages") if isinstance(paging, Mapping) else None
    has_more = isinstance(page, int) and isinstance(pages, int) and page < pages
    return {"has_more": has_more, "next_page": page + 1 if has_more and isinstance(page, int) else None}


# --------------------------------------------------------------------------
# Handlers
# --------------------------------------------------------------------------


def _send_me_message(call: SlackCall, arguments: dict[str, Any]) -> ToolResult:
    text = str(arguments["text"])
    if not text.strip():
        raise _invalid("text is empty")
    if call.attribution:
        text = f"{text}\n{mrkdwn_marker(call.attribution)}"
    channel = resolve_channel(call, arguments["channel"])
    return ToolResult.ok(_payload(call.call("chat.meMessage", {"channel": channel, "text": text})))


def _unread_from_user(call: SlackCall, arguments: dict[str, Any]) -> ToolResult:
    """The DM with ``user_id`` is found among the connection's DMs
    (``users.conversations``) rather than opened with ``conversations.open``,
    which could create or reopen it — a read must not change anything."""
    user_id = str(arguments["user_id"]).strip()
    dm: str | None = None
    cursor: str | None = None
    for page, cursor in _scan(call, "users.conversations", "channels", {"types": "im", "exclude_archived": True}):
        match = next((c for c in page if c.get("user") == user_id and c.get("id")), None)
        if match is not None:
            dm = str(match["id"])
            break
    if dm is None:
        if cursor is not None:
            raise _invalid("could not check every DM for that user; read the DM by its ID instead")
        return ToolResult.ok({"has_more": False, "next_cursor": None, "channel": None, "messages": []})
    info = call.call("conversations.info", {"channel": dm}).get("channel")
    last_read = info.get("last_read") if isinstance(info, Mapping) else None
    if not isinstance(last_read, str) or not last_read:
        raise _invalid("Slack reports no read position (last_read) for this DM, so unread messages can't be told apart")
    payload = call.call(
        "conversations.history",
        {
            "channel": dm,
            "oldest": last_read,
            "limit": _clamp(arguments.get("limit"), HISTORY_DEFAULT_LIMIT, HISTORY_MAX_LIMIT),
        },
    )
    data = cap_messages(_cursor_page(payload), "messages", "resume_latest")
    data["channel"] = dm
    data["last_read"] = last_read
    return ToolResult.ok(data)


def _bookmark(method: str, names: tuple[str, ...], *, link: bool = False) -> Any:
    def handler(call: SlackCall, arguments: dict[str, Any]) -> ToolResult:
        values = _resolve(call, arguments, ["channel_id"])
        body = {n: values[n] for n in names if n in values}
        if link:
            body["type"] = "link"
        return ToolResult.ok(_payload(call.call(method, body)))

    return handler


def _photo_bytes(image: Any) -> tuple[str, bytes, str]:
    """``(filename, bytes, mime)`` from ``image = {"base64": …}``."""
    if not isinstance(image, Mapping) or not isinstance(image.get("base64"), str):
        raise _invalid(
            "image must be {\"base64\": \"<image bytes, base64>\", \"mimetype\": \"image/png\"}; "
            "host-staged files are not supported yet"
        )
    try:
        raw = base64.b64decode(image["base64"], validate=True)
    except (binascii.Error, ValueError):
        raise _invalid("image.base64 is not valid base64") from None
    if not raw:
        raise _invalid("image.base64 is empty")
    if len(raw) > MAX_PHOTO_BYTES:
        raise _invalid(f"the image is over {MAX_PHOTO_BYTES // (1024 * 1024)} MiB")
    mimetype = str(image.get("mimetype") or "image/png")
    if not mimetype.startswith("image/"):
        raise _invalid("image.mimetype must be an image type, e.g. image/png")
    return str(image.get("name") or "photo"), raw, mimetype


def _set_profile_photo(call: SlackCall, arguments: dict[str, Any]) -> ToolResult:
    name, raw, mimetype = _photo_bytes(arguments.get("image"))
    crop = {k: str(arguments[k]) for k in ("crop_w", "crop_x", "crop_y") if arguments.get(k) is not None}
    return ToolResult.ok(_payload(call.call("users.setPhoto", crop, files={"image": (name, raw, mimetype)})))


def _upload(call: SlackCall, arguments: dict[str, Any]) -> ToolResult:
    if arguments.get("file") is not None:
        raise ToolError(
            "file (a host-staged file) is not supported yet: pass the text as content",
            code=ERROR_INVALID_ARGUMENTS,
        )
    content = arguments.get("content")
    if not isinstance(content, str) or not content:
        raise _invalid("pass content: the text to upload as a file or snippet")
    data = content.encode("utf-8")
    if len(data) > MAX_UPLOAD_CONTENT:
        raise _invalid(f"content is over {MAX_UPLOAD_CONTENT // (1024 * 1024)} MiB")
    channels = None
    if arguments.get("channels"):
        ids = [resolve_channel(call, c) for c in str(arguments["channels"]).split(",") if c.strip()]
        channels = ",".join(ids) or None
    comment = arguments.get("initial_comment")
    if channels and call.attribution:
        # The share is a message under the connection's name: mark it.
        marker = mrkdwn_marker(call.attribution)
        comment = f"{comment}\n{marker}" if comment else marker
    blocks = arguments.get("blocks")
    if isinstance(blocks, str):
        try:
            blocks = json.loads(blocks)
        except ValueError:
            raise _invalid("blocks is not valid JSON") from None
    uploaded = upload_file(
        call.client,
        data,
        filename=str(arguments.get("filename") or "snippet.txt"),
        title=arguments.get("title"),
        channels=channels,
        initial_comment=comment,
        thread_ts=arguments.get("thread_ts"),
        snippet_type=arguments.get("filetype"),
        blocks=blocks if isinstance(blocks, list) else None,
    )
    return ToolResult.ok({"file": uploaded, "shared_to": channels.split(",") if channels else []})


def _download(call: SlackCall, arguments: dict[str, Any]) -> ToolResult:
    return download_file(call.client, str(arguments["file"]))


def _list_files(call: SlackCall, arguments: dict[str, Any]) -> ToolResult:
    values = _resolve(call, arguments, ["channel"])
    names = ("channel", "user", "types", "ts_from", "ts_to", "count", "page", "show_files_hidden_by_limit", "team_id")
    sent = {n: values[n] for n in names if n in values}
    if "count" in sent:
        sent["count"] = _clamp(sent["count"], LIST_DEFAULT_LIMIT, LIST_MAX_LIMIT)
    payload = call.call("files.list", sent)
    return ToolResult.ok({**_page_hints(payload), **_payload(payload)})


def _create_canvas(method: str, *, channel_required: bool) -> Any:
    def handler(call: SlackCall, arguments: dict[str, Any]) -> ToolResult:
        body: dict[str, Any] = {"title": arguments.get("title")}
        doc = _markdown_doc(arguments.get("document_content"), "document_content")
        if doc is not None:
            body["document_content"] = doc
        if arguments.get("channel_id"):
            body["channel_id"] = resolve_channel(call, arguments["channel_id"])
        elif channel_required:
            raise _invalid("channel_id is required")
        return ToolResult.ok(_payload(call.call(method, body, json_body=True)))

    return handler


_EDIT_OPERATIONS = ("replace", "insert_after", "insert_before", "insert_at_start", "insert_at_end", "delete", "rename")


def _edit_canvas(call: SlackCall, arguments: dict[str, Any]) -> ToolResult:
    """Composio's flat arguments -> Slack's ``changes`` (one operation per call)."""
    operation = arguments.get("operation") or "insert_at_end"
    change: dict[str, Any] = {"operation": operation}
    section_id = arguments.get("section_id")
    if operation in ("insert_after", "insert_before", "delete") and not section_id:
        raise _invalid(f"{operation} needs section_id (find it with SLACK_LOOKUP_CANVAS_SECTIONS)")
    if section_id:
        change["section_id"] = section_id
    if operation == "rename":
        title = _markdown_doc(arguments.get("title_content"), "title_content")
        if title is None:
            raise _invalid("rename needs title_content")
        change["title_content"] = title
    elif operation != "delete":
        doc = _markdown_doc(arguments.get("document_content"), "document_content")
        if doc is None:
            raise _invalid(f"{operation} needs document_content")
        change["document_content"] = doc
    payload = call.call("canvases.edit", {"canvas_id": arguments["canvas_id"], "changes": [change]}, json_body=True)
    return ToolResult.ok(_payload(payload))


def _lookup_sections(call: SlackCall, arguments: dict[str, Any]) -> ToolResult:
    criteria = arguments["criteria"]
    if isinstance(criteria, str):
        try:
            criteria = json.loads(criteria)
        except ValueError:
            raise _invalid("criteria is not valid JSON") from None
    if not isinstance(criteria, Mapping) or not criteria:
        raise _invalid("criteria needs section_types and/or contains_text")
    body = {"canvas_id": arguments["canvas_id"], "criteria": dict(criteria)}
    return ToolResult.ok(_payload(call.call("canvases.sections.lookup", body, json_body=True)))


def _canvas_access(method: str, *, needs_level: bool) -> Any:
    def handler(call: SlackCall, arguments: dict[str, Any]) -> ToolResult:
        channel_ids = arguments.get("channel_ids")
        user_ids = arguments.get("user_ids")
        if bool(channel_ids) == bool(user_ids):
            raise _invalid("pass channel_ids or user_ids (one of them)")
        body: dict[str, Any] = {"canvas_id": arguments["canvas_id"]}
        if channel_ids:
            body["channel_ids"] = [resolve_channel(call, c) for c in channel_ids]
        else:
            body["user_ids"] = list(user_ids or [])
        if needs_level:
            body["access_level"] = arguments["access_level"]
            if body["access_level"] == "owner" and channel_ids:
                raise _invalid("owner access can be given to users only")
        return ToolResult.ok(_payload(call.call(method, body, json_body=True)))

    return handler


def _num_minutes(call: SlackCall, arguments: dict[str, Any]) -> ToolResult:
    value = str(arguments["num_minutes"]).strip()
    if not value.isdigit() or int(value) < 1:
        raise _invalid("num_minutes must be a whole number of minutes, at least 1")
    return ToolResult.ok(_payload(call.call("dnd.setSnooze", {"num_minutes": int(value)})))


def _update_members(call: SlackCall, arguments: dict[str, Any]) -> ToolResult:
    body = {
        "usergroup": arguments["usergroup"],
        "users": _id_list(arguments["users"], "users"),
        "include_count": arguments.get("include_count"),
        "team_id": arguments.get("team_id"),
    }
    return ToolResult.ok(_payload(call.call("usergroups.users.update", body)))


# --------------------------------------------------------------------------
# Catalog
# --------------------------------------------------------------------------

_DM_WRITE_USER = ("channels:write", "groups:write", "im:write", "mpim:write")
_DM_WRITE_BOT = ("channels:manage", "groups:write", "im:write")

BREADTH_CATALOG: tuple[SlackTool, ...] = (
    # -- messages and history ------------------------------------------------
    _tool(
        "SLACK_SEND_ME_MESSAGE", TIER_SEND, "messages", "chat.meMessage",
        "Post a '/me' action message (shown in italics, e.g. '/me is reviewing the deck') to a "
        "channel. Messages sent as the user carry a 'Sent by <name>' line.",
        _schema({"channel": CHANNEL, "text": _string("The action text, without '/me'.")}, ["channel", "text"]),
        _send_me_message,
        user=("chat:write", *LOOKUP),
        bot=("chat:write", *LOOKUP),
        tags=("me", "action", "emote", "status message", "post"),
    ),
    _tool(
        "SLACK_GET_UNREAD_MESSAGES_FROM_USER", TIER_READ, "history",
        ("users.conversations", "conversations.info", "conversations.history"),
        "Unread messages in the connected user's DM with someone (by user ID): finds the "
        "existing DM — it never opens one — then returns what came after the read position. "
        "Empty when there is no DM. User identity only; costs one history call.",
        _schema(
            {
                "user_id": _string("The other person's user ID, e.g. 'U07SAM123'."),
                "limit": _limit(HISTORY_DEFAULT_LIMIT, HISTORY_MAX_LIMIT),
            },
            ["user_id"],
        ),
        _unread_from_user,
        user=("im:read", "im:history"),
        bot=None,
        tags=("unread", "dm", "direct message", "missed", "from someone"),
    ),
    _tool(
        "SLACK_SET_READ_CURSOR_IN_A_CONVERSATION", TIER_WRITE, "history", "conversations.mark",
        "Mark a conversation as read up to a message (ts). As the bot: channels and DMs, not "
        "group DMs.",
        _schema({"channel": CHANNEL, "ts": TS}, ["channel", "ts"]),
        _rpc("conversations.mark", ("channel", "ts")),
        user=(*_DM_WRITE_USER, *LOOKUP),
        bot=(*_DM_WRITE_BOT, *LOOKUP),
        tags=("mark read", "read cursor", "seen", "catch up"),
    ),
    # -- bookmarks -----------------------------------------------------------
    _tool(
        "SLACK_ADD_BOOKMARK", TIER_WRITE, "bookmarks", "bookmarks.add",
        "Add a link bookmark to a channel's header (at most 100 per channel).",
        _schema(
            {
                "channel_id": BOOKMARK_CHANNEL,
                "title": _string("Bookmark title."),
                "link": _string("The URL to bookmark."),
                "emoji": _string("Emoji shown with the bookmark, e.g. ':link:'."),
            },
            ["channel_id", "title", "link"],
        ),
        _bookmark("bookmarks.add", ("channel_id", "title", "link", "emoji"), link=True),
        user=("bookmarks:write", *LOOKUP),
        bot=("bookmarks:write", *LOOKUP),
        tags=("bookmark", "link", "pin a link", "channel header", "add"),
    ),
    _tool(
        "SLACK_EDIT_BOOKMARK", TIER_WRITE, "bookmarks", "bookmarks.edit",
        "Change a bookmark's title, link or emoji.",
        _schema(
            {
                "channel_id": BOOKMARK_CHANNEL,
                "bookmark_id": _string("Bookmark ID (from SLACK_LIST_BOOKMARKS)."),
                "title": _string("New title."),
                "link": _string("New URL."),
                "emoji": _string("New emoji, e.g. ':star:'."),
            },
            ["channel_id", "bookmark_id"],
        ),
        _bookmark("bookmarks.edit", ("channel_id", "bookmark_id", "title", "link", "emoji")),
        user=("bookmarks:write", *LOOKUP),
        bot=("bookmarks:write", *LOOKUP),
        tags=("bookmark", "edit", "rename", "update"),
    ),
    _tool(
        "SLACK_REMOVE_BOOKMARK", TIER_DESTRUCTIVE, "bookmarks", "bookmarks.remove",
        "Remove a bookmark from a channel. Cannot be undone.",
        _schema(
            {"channel_id": BOOKMARK_CHANNEL, "bookmark_id": _string("Bookmark ID (from SLACK_LIST_BOOKMARKS).")},
            ["channel_id", "bookmark_id"],
        ),
        _bookmark("bookmarks.remove", ("channel_id", "bookmark_id")),
        user=("bookmarks:write", *LOOKUP),
        bot=("bookmarks:write", *LOOKUP),
        tags=("bookmark", "remove", "delete"),
    ),
    _tool(
        "SLACK_LIST_BOOKMARKS", TIER_READ, "bookmarks", "bookmarks.list",
        "A channel's bookmarks: title, link, emoji, ID.",
        _schema({"channel_id": BOOKMARK_CHANNEL}, ["channel_id"]),
        _bookmark("bookmarks.list", ("channel_id",)),
        user=("bookmarks:read", *LOOKUP),
        bot=("bookmarks:read", *LOOKUP),
        tags=("bookmarks", "links", "channel header", "list"),
    ),
    # -- people: profile, photo, bots ------------------------------------------
    _tool(
        "SLACK_GET_TEAM_PROFILE", TIER_READ, "users", "team.profile.get",
        "The workspace's profile field definitions (custom fields such as 'Team' or 'Pronouns').",
        _schema(
            {
                "visibility": _string("Which fields: all (default), visible or hidden.", enum=("all", "visible", "hidden")),
                "team_id": TEAM_ID,
            }
        ),
        _rpc("team.profile.get", ("visibility", "team_id"), channels=()),
        user=("users.profile:read",),
        bot=("users.profile:read",),
        tags=("profile fields", "custom fields", "workspace", "team"),
    ),
    _tool(
        "SLACK_SET_PROFILE_PHOTO", TIER_WRITE, "users", "users.setPhoto",
        "Set the connected user's profile photo: image is {\"base64\": …, \"mimetype\": "
        "\"image/png\"} (512–1024 px square works best), optionally cropped with crop_x, crop_y and "
        "crop_w. User identity only.",
        _schema(
            {
                "image": {
                    "type": "object",
                    "description": "{\"base64\": \"<image bytes>\", \"mimetype\": \"image/png\", \"name\": \"photo.png\"}.",
                },
                "crop_x": _integer("Left edge of a square crop, in pixels."),
                "crop_y": _integer("Top edge of a square crop, in pixels."),
                "crop_w": _integer("Width (and height) of a square crop, in pixels."),
            },
            ["image"],
        ),
        _set_profile_photo,
        user=("users.profile:write",),
        bot=None,
        tags=("photo", "avatar", "picture", "profile"),
    ),
    _tool(
        "SLACK_DELETE_USER_PROFILE_PHOTO", TIER_DESTRUCTIVE, "users", "users.deletePhoto",
        "Delete the connected user's profile photo. Cannot be undone. User identity only.",
        _schema({}),
        _rpc("users.deletePhoto", (), channels=()),
        user=("users.profile:write",),
        bot=None,
        tags=("photo", "avatar", "delete", "remove"),
    ),
    _tool(
        "SLACK_GET_BOT_USER", TIER_READ, "users", "bots.info",
        "A bot's details by bot ID (B…): name, app, icons, user ID.",
        _schema({"bot": _string("Bot ID, e.g. 'B0123ABCD'."), "team_id": TEAM_ID}),
        _rpc("bots.info", ("bot", "team_id"), channels=()),
        user=("users:read",),
        bot=("users:read",),
        tags=("bot", "app", "integration", "info"),
    ),
    # -- Do Not Disturb ------------------------------------------------------------
    _tool(
        "SLACK_RETRIEVE_CURRENT_USER_DND_STATUS", TIER_READ, "dnd", "dnd.info",
        "One person's Do Not Disturb state (default: the connection's): on/off, snooze end, schedule.",
        _schema({"user": _string("User ID; default the connection itself."), "team_id": TEAM_ID}),
        _rpc("dnd.info", ("user", "team_id"), channels=()),
        user=("dnd:read",),
        bot=("dnd:read",),
        tags=("dnd", "do not disturb", "snooze", "available", "notifications"),
    ),
    _tool(
        "SLACK_GET_USER_DND_STATUS", TIER_READ, "dnd", "dnd.teamInfo",
        "Do Not Disturb state for up to 50 people at once.",
        _schema({"users": USERS_LIST, "team_id": TEAM_ID}, ["users"]),
        _rpc("dnd.teamInfo", ("users", "team_id"), channels=(), id_lists=("users",)),
        user=("dnd:read",),
        bot=("dnd:read",),
        tags=("dnd", "do not disturb", "team", "available"),
    ),
    _tool(
        "SLACK_SET_DND_DURATION", TIER_WRITE, "dnd", "dnd.setSnooze",
        "Snooze the connected user's notifications for num_minutes. User identity only.",
        _schema(
            {"num_minutes": {"type": ["string", "integer"], "description": "Minutes to snooze, e.g. 60."}},
            ["num_minutes"],
        ),
        _num_minutes,
        user=("dnd:write",),
        bot=None,
        tags=("snooze", "dnd", "do not disturb", "mute", "focus"),
    ),
    _tool(
        "SLACK_END_DND", TIER_WRITE, "dnd", "dnd.endDnd",
        "End the connected user's current Do Not Disturb session. User identity only.",
        _schema({}),
        _rpc("dnd.endDnd", (), channels=()),
        user=("dnd:write",),
        bot=None,
        tags=("dnd", "end", "resume notifications"),
    ),
    _tool(
        "SLACK_END_SNOOZE", TIER_WRITE, "dnd", "dnd.endSnooze",
        "End the connected user's snooze. User identity only.",
        _schema({}),
        _rpc("dnd.endSnooze", (), channels=()),
        user=("dnd:write",),
        bot=None,
        tags=("snooze", "end", "resume notifications"),
    ),
    # -- files -----------------------------------------------------------------------
    _tool(
        "SLACK_UPLOAD_OR_CREATE_A_FILE_IN_SLACK", TIER_SEND, "files",
        ("files.getUploadURLExternal", "files.completeUploadExternal"),
        "Upload text as a file or snippet (content, filename, optional title and filetype) and "
        "share it into channels (IDs or names, comma-separated; optionally in a thread) with an "
        "initial_comment. Without channels the file stays private to the uploader. Shares made "
        "as the user carry a 'Sent by <name>' line.",
        _schema(
            {
                "content": _string("The text to upload."),
                "filename": _string("File name, e.g. 'notes.md' (default 'snippet.txt')."),
                "title": _string("Title shown in Slack."),
                "filetype": _string("Snippet type, e.g. 'python', 'markdown' (optional)."),
                "channels": _string("Comma-separated channel IDs or names to share it in."),
                "initial_comment": _string("Message posted with the file."),
                "thread_ts": _string("Share it as a reply in this thread (parent ts)."),
                "blocks": {
                    "type": ["array", "string"],
                    "items": {"type": "object"},
                    "description": "Block Kit blocks for the share message (instead of initial_comment).",
                },
                "file": _string("Reserved: host-staged files are not supported yet; use content."),
            }
        ),
        _upload,
        user=("files:write", *LOOKUP),
        bot=("files:write", *LOOKUP),
        tags=("upload", "file", "snippet", "attach", "share", "send a file"),
    ),
    _tool(
        "SLACK_DOWNLOAD_SLACK_FILE", TIER_READ, "files", ("files.info",),
        "Download a file's bytes (up to 100 MiB) — they come back as a file for the host to "
        "import; data describes it (name, mimetype, size, permalink) and includes text content "
        "inline when it is small text. External (remote) files can't be downloaded.",
        _schema(
            {
                "file": FILE_ID,
                "count": _integer("Unused (kept for Composio compatibility)."),
                "page": _integer("Unused (kept for Composio compatibility)."),
                "limit": _integer("Unused (kept for Composio compatibility)."),
                "cursor": _string("Unused (kept for Composio compatibility)."),
            },
            ["file"],
        ),
        _download,
        user=("files:read",),
        bot=("files:read",),
        tags=("download", "file", "get file", "attachment", "import"),
    ),
    _tool(
        "SLACK_RETRIEVE_DETAILED_INFORMATION_ABOUT_A_FILE", TIER_READ, "files", "files.info",
        "A file's metadata — name, type, size, owner, where it is shared, permalink — plus its "
        "comments (paged).",
        _schema(
            {
                "file": FILE_ID,
                "limit": _limit(LIST_DEFAULT_LIMIT, LIST_MAX_LIMIT),
                "cursor": CURSOR,
                "count": _integer("Legacy page size for comments (use limit + cursor)."),
                "page": _integer("Legacy page number for comments (use limit + cursor)."),
            },
            ["file"],
        ),
        _rpc(
            "files.info",
            ("file", "limit", "cursor", "count", "page"),
            channels=(),
            paged=True,
            limits=(LIST_DEFAULT_LIMIT, LIST_MAX_LIMIT),
        ),
        user=("files:read",),
        bot=("files:read",),
        tags=("file", "info", "details", "metadata"),
    ),
    _tool(
        "SLACK_LIST_FILES_WITH_FILTERS_IN_SLACK", TIER_READ, "files", "files.list",
        "List files, optionally by channel, by user, by type (e.g. 'images', 'pdfs', 'canvas') "
        "and by date (ts_from/ts_to, Unix seconds). Paged: pass next_page back as page.",
        _schema(
            {
                "channel": _string("Only files shared in this channel (ID or name)."),
                "user": _string("Only files from this user ID."),
                "types": _string("Comma-separated: all, spaces, snippets, images, gdocs, zips, pdfs, canvas."),
                "ts_from": _integer("Only files created after this Unix time."),
                "ts_to": _integer("Only files created before this Unix time."),
                "count": {"type": ["string", "integer"], "description": "Files per page (default 100)."},
                "page": {"type": ["string", "integer"], "description": "Page number, 1-based."},
                "show_files_hidden_by_limit": _boolean("Include files hidden by the free plan's limit."),
                "team_id": TEAM_ID,
            }
        ),
        _list_files,
        user=("files:read", *LOOKUP),
        bot=("files:read", *LOOKUP),
        tags=("files", "list", "documents", "images", "search files"),
    ),
    _tool(
        "SLACK_DELETE_FILE", TIER_DESTRUCTIVE, "files", "files.delete",
        "Delete a file. Cannot be undone.",
        _schema({"file": FILE_ID}, ["file"]),
        _rpc("files.delete", ("file",), channels=()),
        user=("files:write",),
        bot=("files:write",),
        tags=("delete", "remove", "file"),
    ),
    _tool(
        "SLACK_DELETE_FILE_COMMENT", TIER_DESTRUCTIVE, "files", "files.comments.delete",
        "Delete a comment on a file. Cannot be undone.",
        _schema({"file": FILE_ID, "id": _string("The comment's ID.")}, ["file", "id"]),
        _rpc("files.comments.delete", ("file", "id"), channels=()),
        user=("files:write",),
        bot=("files:write",),
        tags=("delete", "comment", "file"),
    ),
    _tool(
        "SLACK_ENABLE_PUBLIC_SHARING_OF_A_FILE", TIER_DESTRUCTIVE, "files", "files.sharedPublicURL",
        "Create a public URL for a file — anyone with the link can see it, outside Slack. User "
        "identity only.",
        _schema({"file": FILE_ID}, ["file"]),
        _rpc("files.sharedPublicURL", ("file",), channels=()),
        user=("files:write",),
        bot=None,
        tags=("public link", "share publicly", "external", "file"),
    ),
    _tool(
        "SLACK_REVOKE_FILE_PUBLIC_SHARING", TIER_WRITE, "files", "files.revokePublicURL",
        "Turn off a file's public URL.",
        _schema({"file": FILE_ID}, ["file"]),
        _rpc("files.revokePublicURL", ("file",), channels=()),
        user=("files:write",),
        bot=("files:write",),
        tags=("revoke", "public link", "unshare", "file"),
    ),
    # -- canvases --------------------------------------------------------------------
    _tool(
        "SLACK_CREATE_CANVAS", TIER_WRITE, "canvases", "canvases.create",
        "Create a canvas from markdown (title, document_content); with channel_id it is also "
        "added as a tab in that channel. Returns canvas_id.",
        _schema(
            {
                "title": _string("Canvas title."),
                "document_content": MARKDOWN_DOC,
                "channel_id": _string("Also add it as a tab in this channel (ID or name)."),
            }
        ),
        _create_canvas("canvases.create", channel_required=False),
        user=("canvases:write", *LOOKUP),
        bot=("canvases:write", *LOOKUP),
        tags=("canvas", "create", "document", "doc", "write up", "notes"),
    ),
    _tool(
        "SLACK_EDIT_CANVAS", TIER_DESTRUCTIVE, "canvases", "canvases.edit",
        "Change a canvas with one operation: insert_at_start / insert_at_end (document_content), "
        "insert_after / insert_before (section_id + document_content), replace (document_content; "
        "with section_id one section, without it the WHOLE canvas), delete (section_id) or rename "
        "(title_content). Find section IDs with SLACK_LOOKUP_CANVAS_SECTIONS.",
        _schema(
            {
                "canvas_id": CANVAS_ID,
                "operation": _string("What to do (default insert_at_end).", enum=_EDIT_OPERATIONS),
                "section_id": _string("Section to act on (from SLACK_LOOKUP_CANVAS_SECTIONS)."),
                "document_content": MARKDOWN_DOC,
                "title_content": {
                    "type": ["object", "string"],
                    "description": "New title for rename: markdown text or {\"type\": \"markdown\", \"markdown\": …}.",
                },
            },
            ["canvas_id"],
        ),
        _edit_canvas,
        user=("canvases:write",),
        bot=("canvases:write",),
        tags=("canvas", "edit", "append", "update", "document"),
    ),
    _tool(
        "SLACK_DELETE_CANVAS", TIER_DESTRUCTIVE, "canvases", "canvases.delete",
        "Delete a canvas. Cannot be undone.",
        _schema({"canvas_id": CANVAS_ID}, ["canvas_id"]),
        _rpc("canvases.delete", ("canvas_id",), channels=()),
        user=("canvases:write",),
        bot=("canvases:write",),
        tags=("canvas", "delete", "remove"),
    ),
    _tool(
        "SLACK_LOOKUP_CANVAS_SECTIONS", TIER_READ, "canvases", "canvases.sections.lookup",
        "Find section IDs in a canvas, for targeted edits: criteria = {\"section_types\": "
        "[\"h1\", \"h2\", \"any_header\", …], \"contains_text\": \"…\"}.",
        _schema(
            {
                "canvas_id": CANVAS_ID,
                "criteria": {
                    "type": ["object", "string"],
                    "description": "{\"section_types\": [\"any_header\"], \"contains_text\": \"Agenda\"}.",
                },
            },
            ["canvas_id", "criteria"],
        ),
        _lookup_sections,
        user=("canvases:read",),
        bot=("canvases:read",),
        tags=("canvas", "sections", "find", "headings"),
    ),
    _tool(
        "SLACK_GET_CANVAS_CONTENT", TIER_READ, "canvases", "canvases.getContent",
        "Read a whole canvas as markdown (or HTML with content_type).",
        _schema(
            {"canvas_id": CANVAS_ID, "content_type": _string("markdown (default) or html.", enum=("markdown", "html"))},
            ["canvas_id"],
        ),
        _rpc("canvases.getContent", ("canvas_id", "content_type"), channels=()),
        user=("canvases:read",),
        bot=("canvases:read",),
        tags=("canvas", "read", "content", "document", "open"),
    ),
    _tool(
        "SLACK_CREATE_CHANNEL_CANVAS", TIER_WRITE, "canvases", "conversations.canvases.create",
        "Create a channel's own canvas (one per channel), optionally from markdown.",
        _schema(
            {"channel_id": _string("Channel ID or name."), "title": _string("Canvas title."), "document_content": MARKDOWN_DOC},
            ["channel_id"],
        ),
        _create_canvas("conversations.canvases.create", channel_required=True),
        user=("canvases:write", *LOOKUP),
        bot=("canvases:write", *LOOKUP),
        tags=("canvas", "channel canvas", "create", "tab"),
    ),
    _tool(
        "SLACK_SET_CANVAS_ACCESS", TIER_WRITE, "canvases", "canvases.access.set",
        "Give channels or people read/write access to a canvas (owner: people only). Paid plans.",
        _schema(
            {
                "canvas_id": CANVAS_ID,
                "access_level": _string("read, write or owner.", enum=("read", "write", "owner")),
                "channel_ids": _strings_schema("Channel IDs or names (not with user_ids)."),
                "user_ids": _strings_schema("User IDs (not with channel_ids)."),
            },
            ["canvas_id", "access_level"],
        ),
        _canvas_access("canvases.access.set", needs_level=True),
        user=("canvases:write", *LOOKUP),
        bot=("canvases:write", *LOOKUP),
        tags=("canvas", "share", "access", "permissions"),
    ),
    _tool(
        "SLACK_DELETE_CANVAS_ACCESS", TIER_DESTRUCTIVE, "canvases", "canvases.access.delete",
        "Remove a channel's or a person's access to a canvas. Paid plans.",
        _schema(
            {
                "canvas_id": CANVAS_ID,
                "channel_ids": _strings_schema("Channel IDs or names (not with user_ids)."),
                "user_ids": _strings_schema("User IDs (not with channel_ids)."),
            },
            ["canvas_id"],
        ),
        _canvas_access("canvases.access.delete", needs_level=False),
        user=("canvases:write", *LOOKUP),
        bot=("canvases:write", *LOOKUP),
        tags=("canvas", "unshare", "access", "revoke"),
    ),
    # -- reminders (Slack calls this API degraded; user tokens only) ------------------
    _tool(
        "SLACK_CREATE_A_REMINDER", TIER_WRITE, "reminders", "reminders.add",
        "Create a reminder for the connected user: text, and time as Unix seconds or natural "
        "language ('in 15 minutes', 'every Thursday'). Slack no longer supports reminders for "
        "others. User identity only.",
        _schema(
            {
                "text": _string("What to be reminded of."),
                "time": _string("When: Unix seconds, or e.g. 'in 2 hours', 'tomorrow at 9am'."),
                "user": _string("Deprecated by Slack; leave unset."),
                "team_id": TEAM_ID,
            },
            ["text", "time"],
        ),
        _rpc("reminders.add", ("text", "time", "user", "team_id"), channels=()),
        user=("reminders:write",),
        bot=None,
        tags=("remind", "reminder", "later", "follow up"),
    ),
    _tool(
        "SLACK_GET_REMINDER", TIER_READ, "reminders", "reminders.info",
        "One of the connected user's reminders by ID. User identity only.",
        _schema({"reminder": REMINDER, "team_id": TEAM_ID}, ["reminder"]),
        _rpc("reminders.info", ("reminder", "team_id"), channels=()),
        user=("reminders:read",),
        bot=None,
        tags=("reminder", "info"),
    ),
    _tool(
        "SLACK_LIST_REMINDERS", TIER_READ, "reminders", "reminders.list",
        "The connected user's reminders. User identity only.",
        _schema({"team_id": TEAM_ID}),
        _rpc("reminders.list", ("team_id",), channels=()),
        user=("reminders:read",),
        bot=None,
        tags=("reminders", "list", "upcoming"),
    ),
    _tool(
        "SLACK_DELETE_REMINDER", TIER_DESTRUCTIVE, "reminders", "reminders.delete",
        "Delete one of the connected user's reminders. Cannot be undone. User identity only.",
        _schema({"reminder": REMINDER, "team_id": TEAM_ID}, ["reminder"]),
        _rpc("reminders.delete", ("reminder", "team_id"), channels=()),
        user=("reminders:write",),
        bot=None,
        tags=("reminder", "delete", "cancel"),
    ),
    # -- user groups ---------------------------------------------------------------
    _tool(
        "SLACK_CREATE_USER_GROUP", TIER_WRITE, "usergroups", "usergroups.create",
        "Create a user group (an @handle that mentions all its members). Standard plan and up; "
        "workspace settings may restrict who can.",
        _schema(dict(USERGROUP_FIELDS), ["name"]),
        _rpc("usergroups.create", tuple(USERGROUP_FIELDS), channels=()),
        user=("usergroups:write",),
        bot=("usergroups:write",),
        tags=("user group", "team", "handle", "create", "mention group"),
    ),
    _tool(
        "SLACK_UPDATE_USER_GROUP", TIER_WRITE, "usergroups", "usergroups.update",
        "Change a user group's name, handle, description or default channels (not its members: "
        "SLACK_UPDATE_USER_GROUP_MEMBERS).",
        _schema({"usergroup": USERGROUP, **USERGROUP_FIELDS}, ["usergroup"]),
        _rpc("usergroups.update", ("usergroup", *USERGROUP_FIELDS), channels=()),
        user=("usergroups:write",),
        bot=("usergroups:write",),
        tags=("user group", "rename", "update", "handle"),
    ),
    _tool(
        "SLACK_DISABLE_USER_GROUP", TIER_DESTRUCTIVE, "usergroups", "usergroups.disable",
        "Disable a user group: its @handle stops notifying anyone, for the whole workspace "
        "(SLACK_ENABLE_USER_GROUP brings it back).",
        _schema({"usergroup": USERGROUP, "include_count": INCLUDE_COUNT, "team_id": TEAM_ID}, ["usergroup"]),
        _rpc("usergroups.disable", ("usergroup", "include_count", "team_id"), channels=()),
        user=("usergroups:write",),
        bot=("usergroups:write",),
        tags=("user group", "disable", "archive"),
    ),
    _tool(
        "SLACK_ENABLE_USER_GROUP", TIER_WRITE, "usergroups", "usergroups.enable",
        "Re-enable a disabled user group.",
        _schema({"usergroup": USERGROUP, "include_count": INCLUDE_COUNT, "team_id": TEAM_ID}, ["usergroup"]),
        _rpc("usergroups.enable", ("usergroup", "include_count", "team_id"), channels=()),
        user=("usergroups:write",),
        bot=("usergroups:write",),
        tags=("user group", "enable", "restore"),
    ),
    _tool(
        "SLACK_LIST_USER_GROUPS", TIER_READ, "usergroups", "usergroups.list",
        "The workspace's user groups (optionally with members, counts, and disabled ones).",
        _schema(
            {
                "include_users": _boolean("Include each group's member IDs."),
                "include_count": INCLUDE_COUNT,
                "include_disabled": _boolean("Include disabled groups."),
                "team_id": TEAM_ID,
            }
        ),
        _rpc("usergroups.list", ("include_users", "include_count", "include_disabled", "team_id"), channels=()),
        user=("usergroups:read",),
        bot=("usergroups:read",),
        tags=("user groups", "teams", "handles", "list"),
    ),
    _tool(
        "SLACK_LIST_USER_GROUP_MEMBERS", TIER_READ, "usergroups", "usergroups.users.list",
        "The member user IDs of a user group.",
        _schema({"usergroup": USERGROUP, "include_disabled": _boolean("Allow a disabled group."), "team_id": TEAM_ID},
                ["usergroup"]),
        _rpc("usergroups.users.list", ("usergroup", "include_disabled", "team_id"), channels=()),
        user=("usergroups:read",),
        bot=("usergroups:read",),
        tags=("user group", "members", "who is in"),
    ),
    _tool(
        "SLACK_UPDATE_USER_GROUP_MEMBERS", TIER_DESTRUCTIVE, "usergroups", "usergroups.users.update",
        "REPLACE a user group's whole member list with users (anyone not listed is removed).",
        _schema(
            {"usergroup": USERGROUP, "users": USERS_LIST, "include_count": INCLUDE_COUNT, "team_id": TEAM_ID},
            ["usergroup", "users"],
        ),
        _update_members,
        user=("usergroups:write",),
        bot=("usergroups:write",),
        tags=("user group", "members", "replace", "set members"),
    ),
    # -- workspace ---------------------------------------------------------------
    _tool(
        "SLACK_LIST_AUTH_TEAMS", TIER_READ, "workspace", "auth.teams.list",
        "Workspaces this connection is authorized for (more than one only for org-wide installs).",
        _schema(
            {
                "limit": _limit(LIST_DEFAULT_LIMIT, LIST_MAX_LIMIT),
                "cursor": CURSOR,
                "include_icon": _boolean("Include each workspace's icon."),
            }
        ),
        _rpc(
            "auth.teams.list",
            ("limit", "cursor", "include_icon"),
            channels=(),
            paged=True,
            limits=(LIST_DEFAULT_LIMIT, LIST_MAX_LIMIT),
        ),
        user=(),
        bot=(),
        tags=("workspaces", "teams", "org", "grid"),
    ),
)
