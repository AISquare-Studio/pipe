"""Remote (external) file tools (``files.remote.*``): a Slack file entry that
points at a document stored elsewhere — Drive, Dropbox, a wiki — by the app's
own ``external_id`` and its ``external_url``.

Slack lets only the app's bot token add, update or remove remote files (they
belong to the app), so those three are bot-identity tools; reading and
sharing work with either token. Tiers: reads are ``read``; adding, updating
and sharing into conversations are ``write`` (a share posts the file with no
room for a "Sent by" line, so it is not a ``send``); removing is
``destructive``.

``preview_image`` is not offered: Slack takes it only as a multipart file
upload, and a tool argument can't carry the image bytes.
"""

from __future__ import annotations

from typing import Any

from aisquare.pipe import ToolResult

from aisquare_pipe_slack.constants import (
    LIST_DEFAULT_LIMIT,
    LIST_MAX_LIMIT,
    TIER_DESTRUCTIVE,
    TIER_READ,
    TIER_WRITE,
)
from aisquare_pipe_slack.tools import (
    CHANNEL,
    CURSOR,
    LOOKUP,
    Handler,
    SlackCall,
    SlackTool,
    _id_list,
    _invalid,
    _limit,
    _payload,
    _rpc,
    _schema,
    _string,
    _tool,
)

AREA = "remote_files"
READ_SCOPES = ("remote_files:read",)
WRITE_SCOPES = ("remote_files:write",)
SHARE_SCOPES = ("remote_files:share",)

FILE = _string("The remote file's Slack ID (F…). Pass this or external_id, not both.")
EXTERNAL_ID = _string("The app's own unique ID for the file (the one it was added with).")
EXTERNAL_URL = _string("The document's http(s) URL at its source.")
TIMESTAMP_FILTER = {"type": ["string", "number"], "description": "Unix time in seconds (inclusive)."}


def _by_file_or_external_id(method: str, names: tuple[str, ...]) -> Handler:
    """A call that names the file by ``file`` or ``external_id`` — exactly
    one. ``files.remote.info`` refuses both (``too_many_ids``); the other
    methods' docs allow both, but one is all they need, so every tool here
    asks for exactly one rather than guess which Slack would use."""

    def handler(call: SlackCall, arguments: dict[str, Any]) -> ToolResult:
        if bool(arguments.get("file")) == bool(arguments.get("external_id")):
            raise _invalid("pass either file (the Slack file ID) or external_id, not both")
        sent = {n: arguments[n] for n in names if arguments.get(n) is not None}
        if "channels" in sent:
            sent["channels"] = _id_list(sent["channels"], "channels", "conversation")
        return ToolResult.ok(_payload(call.call(method, sent)))

    return handler


def _require_url(arguments: dict[str, Any]) -> None:
    url = arguments.get("external_url")
    if url is not None and not str(url).strip().lower().startswith(("https://", "http://")):
        raise _invalid("external_url must be an http(s) URL")


def _add(call: SlackCall, arguments: dict[str, Any]) -> ToolResult:
    _require_url(arguments)
    names = ("external_id", "external_url", "title", "filetype", "indexable_file_contents")
    sent = {n: arguments[n] for n in names if arguments.get(n) is not None}
    return ToolResult.ok(_payload(call.call("files.remote.add", sent)))


_CHANGES = ("external_url", "title", "filetype", "indexable_file_contents")
_update_by_id = _by_file_or_external_id("files.remote.update", ("file", "external_id", *_CHANGES))


def _update(call: SlackCall, arguments: dict[str, Any]) -> ToolResult:
    _require_url(arguments)
    if not any(arguments.get(n) is not None for n in _CHANGES):
        raise _invalid("nothing to change: pass external_url, title, filetype or indexable_file_contents")
    return _update_by_id(call, arguments)


REMOTE_FILES_CATALOG: tuple[SlackTool, ...] = (
    _tool(
        "SLACK_ADD_REMOTE_FILE", TIER_WRITE, AREA, "files.remote.add",
        "Register a document stored outside Slack (Drive, Dropbox, a wiki...) as a Slack remote "
        "file, so it can be found and shared in Slack: a title, the app's own external_id and the "
        "external_url; indexable_file_contents makes its text searchable. Bot identity only. "
        "Returns the file (share it with SLACK_SHARE_REMOTE_FILE).",
        _schema(
            {
                "external_id": EXTERNAL_ID,
                "external_url": EXTERNAL_URL,
                "title": _string("The title Slack shows."),
                "filetype": _string("The file's type, e.g. 'pdf', 'gdoc', 'docx' (for its icon)."),
                "indexable_file_contents": _string("Plain text of the document, for Slack search."),
            },
            ["external_id", "external_url", "title"],
        ),
        _add,
        user=None,
        bot=WRITE_SCOPES,
        tags=("remote", "external", "file", "link", "register", "add", "document"),
    ),
    _tool(
        "SLACK_UPDATE_REMOTE_FILE", TIER_WRITE, AREA, "files.remote.update",
        "Change a remote file's title, URL, type or searchable text (by file or external_id); "
        "the new values replace the old. Bot identity only.",
        _schema(
            {
                "file": FILE,
                "external_id": EXTERNAL_ID,
                "external_url": EXTERNAL_URL,
                "title": _string("The new title."),
                "filetype": _string("The new type, e.g. 'pdf'."),
                "indexable_file_contents": _string("New plain text for Slack search."),
            }
        ),
        _update,
        user=None,
        bot=WRITE_SCOPES,
        tags=("remote", "external", "file", "update", "edit", "document"),
        overwrites=True,
    ),
    _tool(
        "SLACK_REMOVE_REMOTE_FILE", TIER_DESTRUCTIVE, AREA, "files.remote.remove",
        "Remove a remote file from Slack (by file or external_id); the document at its source is "
        "not touched. Bot identity only.",
        _schema({"file": FILE, "external_id": EXTERNAL_ID}),
        _by_file_or_external_id("files.remote.remove", ("file", "external_id")),
        user=None,
        bot=WRITE_SCOPES,
        tags=("remote", "external", "file", "remove", "delete", "unregister"),
    ),
    _tool(
        "SLACK_GET_REMOTE_FILE", TIER_READ, AREA, "files.remote.info",
        "A remote file's details (title, external URL, where it is shared) by file or external_id. "
        "Only for remote files, not files uploaded to Slack.",
        _schema({"file": FILE, "external_id": EXTERNAL_ID}),
        _by_file_or_external_id("files.remote.info", ("file", "external_id")),
        user=READ_SCOPES,
        bot=READ_SCOPES,
        tags=("remote", "external", "file", "info", "details", "get"),
    ),
    _tool(
        "SLACK_LIST_REMOTE_FILES", TIER_READ, AREA, "files.remote.list",
        "Remote files visible to this connection, optionally in one channel or a time window "
        "(ts_from / ts_to); pass next_cursor back as cursor for more.",
        _schema(
            {
                "channel": CHANNEL,
                "ts_from": TIMESTAMP_FILTER,
                "ts_to": TIMESTAMP_FILTER,
                "limit": _limit(LIST_DEFAULT_LIMIT, LIST_MAX_LIMIT),
                "cursor": CURSOR,
            }
        ),
        _rpc(
            "files.remote.list",
            ("channel", "ts_from", "ts_to", "limit", "cursor"),
            paged=True,
            limits=(LIST_DEFAULT_LIMIT, LIST_MAX_LIMIT),
        ),
        user=(*READ_SCOPES, *LOOKUP),
        bot=(*READ_SCOPES, *LOOKUP),
        tags=("remote", "external", "files", "list", "documents"),
    ),
    _tool(
        "SLACK_SHARE_REMOTE_FILE", TIER_WRITE, AREA, "files.remote.share",
        "Share a remote file (by file or external_id) into conversations: channels is a "
        "comma-separated list of channel or DM IDs. It shows as a file message from the app's "
        "bot. Bot identity only.",
        _schema(
            {
                "file": FILE,
                "external_id": EXTERNAL_ID,
                "channels": {
                    "type": ["string", "array"],
                    "items": {"type": "string"},
                    "description": "Conversation IDs (C…, G…, D…): a comma-separated string or an array.",
                },
            },
            ["channels"],
        ),
        _by_file_or_external_id("files.remote.share", ("file", "external_id", "channels")),
        # Bot-only: remote files are the app's, and a share as the user would
        # post under the user's name with no room for the "Sent by" line (Q7).
        user=None,
        bot=SHARE_SCOPES,
        tags=("remote", "external", "file", "share", "post", "channel"),
    ),
)

__all__ = ["REMOTE_FILES_CATALOG"]
