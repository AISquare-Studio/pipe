"""The Gmail toolkit's catalog: one :class:`ToolSpec` plus one handler per tool.

Slugs and argument names are Composio's Gmail toolkit's, verbatim
(``GMAIL_FETCH_EMAILS`` with ``query`` / ``max_results`` / ``page_token``,
``GMAIL_GET_ATTACHMENT`` with ``message_id`` / ``attachment_id`` /
``file_name``, ...), so callers written against Composio — the BE's Content
Vault import, saved prompts — keep working. The other casing of an argument
(``messageId``) and Composio's documented aliases (``to`` for
``recipient_email``, ``message_body`` for ``body``) are accepted too; see
:func:`apply_aliases`.

Result shapes (``ToolResult.data``):

* a message is summarised with Composio's keys — ``messageId``,
  ``threadId``, ``labelIds``, ``messageTimestamp`` (RFC 3339, UTC),
  ``subject``, ``sender``, ``to``, ``preview`` (``subject``, ``body``:
  Gmail's snippet), ``messageText`` (the readable body, plain text preferred,
  HTML converted) and ``attachmentList`` (``filename``, ``mimeType``,
  ``size``, ``attachmentId``) — plus Gmail's MIME ``payload`` when asked for;
* list endpoints return ``{"has_more", "next_page_token", "items": [...]}``
  — one page per call; pass ``next_page_token`` back as ``page_token``;
* ``GMAIL_GET_ATTACHMENT`` puts the bytes in ``ToolResult.files`` (one
  DataEnvelope: ``content_type`` = MIME, ``metadata["filename"]``) and
  describes the file in ``data`` (``name``, ``mimetype``, ``size``, plus
  ``content`` for small UTF-8 text) — the shape Google Drive downloads use;
* writes return Gmail's resource (``id``, ``threadId``, ``labelIds``).

Scopes: every tool lists the OAuth scopes it needs in
``ToolSpec.required_scopes`` (also :attr:`GmailTool.scopes`) and its
description. The default connection is read-only
(``gmail.readonly``); drafts need ``gmail.compose``, sending ``gmail.send``,
labels and trash ``gmail.modify``.
"""

from __future__ import annotations

import base64
import binascii
import datetime
import html
import json
import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from aisquare.pipe import (
    ERROR_INVALID_ARGUMENTS,
    DataEnvelope,
    ToolError,
    ToolResult,
    ToolSpec,
)

from aisquare_pipe_gmail.mime import (
    Attachment,
    attachments_of,
    body_text,
    build_message,
    guess_mimetype,
    headers_of,
    parse_addresses,
    reply_subject,
    safe_filename,
)
from aisquare_pipe_gmail.rest import (
    MEDIA_TIMEOUT,
    MESSAGE_MAX,
    GmailAPIError,
    GmailRestClient,
    decode_base64url,
    json_object,
    quote_segment,
)

Handler = Callable[[GmailRestClient, dict[str, Any]], ToolResult]

SCOPE_BASE = "https://www.googleapis.com/auth/"
#: Read mail, threads, labels, attachments and the profile. Restricted.
READONLY_SCOPE = SCOPE_BASE + "gmail.readonly"
#: Create, read, update and delete drafts; send them. Restricted.
COMPOSE_SCOPE = SCOPE_BASE + "gmail.compose"
#: Send mail only (no read). Sensitive.
SEND_SCOPE = SCOPE_BASE + "gmail.send"
#: Read and change mail (labels, trash) — not permanent delete. Restricted.
MODIFY_SCOPE = SCOPE_BASE + "gmail.modify"
#: Full mailbox access, permanent delete included. No tool asks for it.
FULL_SCOPE = "https://mail.google.com/"
#: The broader scopes Google accepts in place of each scope a tool lists.
COVERING_SCOPES: dict[str, frozenset[str]] = {
    READONLY_SCOPE: frozenset({FULL_SCOPE, MODIFY_SCOPE}),
    COMPOSE_SCOPE: frozenset({FULL_SCOPE, MODIFY_SCOPE}),
    SEND_SCOPE: frozenset({FULL_SCOPE, MODIFY_SCOPE, COMPOSE_SCOPE}),
    MODIFY_SCOPE: frozenset({FULL_SCOPE}),
}

DEFAULT_PAGE_SIZE = 10
#: Messages/threads per page when each one is fetched in detail (one request each).
MAX_DETAIL_PAGE = 100
#: messages.list / threads.list cap.
MAX_LIST_PAGE = 500
#: UTF-8 attachments up to this size also come back as text in ``data["content"]``.
INLINE_TEXT_MAX = 100_000
#: ``raw`` messages up to this many base64url characters are also returned inline.
INLINE_RAW_MAX = 100_000
#: Total attachment bytes per message (Gmail's own limit).
ATTACHMENTS_MAX = 25 * 1024 * 1024

#: Headers asked for when a message is read without its body.
SUMMARY_HEADERS = ("From", "To", "Cc", "Subject", "Date")
#: Headers a reply needs from the message it answers.
REPLY_HEADERS = ("From", "To", "Cc", "Reply-To", "Subject", "Message-ID", "References")

SYSTEM_LABELS = frozenset(
    {
        "INBOX", "SPAM", "TRASH", "UNREAD", "STARRED", "IMPORTANT", "SENT", "DRAFT", "CHAT",
        "CATEGORY_PERSONAL", "CATEGORY_SOCIAL", "CATEGORY_PROMOTIONS", "CATEGORY_UPDATES",
        "CATEGORY_FORUMS",
    }
)
_USER_LABEL_ID = re.compile(r"^Label_\d+$")
_USER_ID = re.compile(r"^(me|[^@\s/]+@[^@\s/]+)$")


@dataclass(frozen=True)
class GmailTool:
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


def _integer(description: str, *, default: int | None = None, maximum: int = MAX_LIST_PAGE) -> dict[str, Any]:
    schema: dict[str, Any] = {"type": "integer", "minimum": 1, "maximum": maximum, "description": description}
    if default is not None:
        schema["default"] = default
    return schema


def _boolean(description: str, *, default: bool | None = None) -> dict[str, Any]:
    schema: dict[str, Any] = {"type": "boolean", "description": description}
    if default is not None:
        schema["default"] = default
    return schema


def _strings(description: str) -> dict[str, Any]:
    """An array of strings; a single (comma-separated) string is accepted too."""
    return {"type": ["array", "string"], "items": {"type": "string"}, "description": description}


USER_ID = _string(
    "Mailbox: 'me' (default, the connected account) or an address it may act for.", default="me"
)
MESSAGE_ID = _id("The message's Gmail id (messageId in GMAIL_FETCH_EMAILS results).")
THREAD_ID = _id("The thread's Gmail id (threadId in message results).")
PAGE_TOKEN = _string("The previous result's next_page_token, to fetch the next page.")
QUERY = _string(
    "Gmail search, as in the Gmail search box: e.g. 'from:ann@x.com is:unread', "
    "'subject:invoice after:2026/10/01', 'has:attachment filename:pdf', 'label:work'."
)
ATTACHMENT_FILE = {
    "type": "object",
    "properties": {
        "name": _string("File name, e.g. 'report.pdf'."),
        "mimetype": _string("MIME type; guessed from the name when omitted."),
        "file_content_bytes": _string("The file's bytes, base64-encoded."),
    },
    "required": ["name", "file_content_bytes"],
}
ATTACHMENT = {
    "type": ["object", "array"],
    "description": "File(s) to attach: {name, mimetype, file_content_bytes (base64)}, or a list of them. "
    "25 MB in total.",
    "properties": ATTACHMENT_FILE["properties"],
    "required": ATTACHMENT_FILE["required"],
    "items": ATTACHMENT_FILE,
}
LABEL_IDS = _strings(
    "Label ids — system ones (INBOX, UNREAD, STARRED, IMPORTANT, SPAM, TRASH, CATEGORY_*) or "
    "user ones (Label_123, from GMAIL_LIST_LABELS); a user label's exact name works too."
)


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
    (``messageId`` <-> ``message_id``). A key already present wins over its alias."""
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


def _user(args: dict[str, Any]) -> str:
    """``user_id`` as a path segment: ``me`` or an email address."""
    value = str(args.get("user_id") or "me").strip()
    if not _USER_ID.match(value):
        raise _invalid("user_id must be 'me' or an email address")
    return quote_segment(value, "user_id")


def _path_id(args: dict[str, Any], key: str) -> str:
    """``args[key]`` as one URL path segment; '.'/'..' (which the HTTP stack
    would resolve, rerouting the call) and empty ids are refused."""
    value = args.get(key)
    if not isinstance(value, str) or not value.strip():
        raise _invalid(f"{key} is required")
    try:
        return quote_segment(value)
    except ValueError:
        raise _invalid(f"{key} must be a Gmail id, not empty, '.' or '..'") from None


def _page_size(value: Any, default: int, maximum: int) -> int:
    if value is None:
        return min(default, maximum)
    return min(max(1, int(value)), maximum)


def _string_list(value: Any, what: str) -> list[str]:
    """A list of strings from an array, a JSON-encoded array or a
    comma-separated string."""
    if value is None:
        return []
    if isinstance(value, str):
        text = value.strip()
        if text.startswith("["):
            try:
                value = json.loads(text)
            except ValueError:
                raise _invalid(f"{what} is not a valid JSON array") from None
        else:
            return [part.strip() for part in text.split(",") if part.strip()]
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise _invalid(f"{what} must be a list of strings")
    return [v.strip() for v in value if v.strip()]


def _resolve_labels(
    client: GmailRestClient,
    user: str,
    values: list[str],
    what: str,
    known: dict[str, str] | None = None,
) -> list[str]:
    """Label ids for ``values``: system labels (any case) and ``Label_<n>``
    ids pass through; anything else is looked up by exact name
    (case-insensitive). ``known`` caches the mailbox's labels (lower-cased
    name -> id) across calls in one tool call."""
    ids: list[str] = []
    pending: list[tuple[int, str]] = []
    for index, value in enumerate(values):
        if value.upper() in SYSTEM_LABELS:
            ids.append(value.upper())
        elif _USER_LABEL_ID.match(value):
            ids.append(value)
        else:
            ids.append("")
            pending.append((index, value))
    if pending:
        by_name = known if known is not None else {}
        if not by_name:
            labels = json_object(client.get(f"/{user}/labels")).get("labels") or []
            by_name.update(
                (str(label.get("name", "")).lower(), str(label.get("id"))) for label in labels if isinstance(label, dict)
            )
        known_ids = set(by_name.values())
        for index, value in pending:
            label_id = value if value in known_ids else by_name.get(value.lower())
            if not label_id:
                raise _invalid(
                    f"{what}[{index}] matches no label id or name in this mailbox (see GMAIL_LIST_LABELS)"
                )
            ids[index] = label_id
    return list(dict.fromkeys(ids))


# --------------------------------------------------------------------------
# Response helpers
# --------------------------------------------------------------------------


def listing(items: list[Any], token: Any, **extra: Any) -> dict[str, Any]:
    """One page: pagination hints first (they survive a truncated result), then ``items``."""
    out: dict[str, Any] = {"has_more": bool(token), "next_page_token": token or None, **extra}
    out["items"] = items
    return out


def timestamp(internal_date: Any) -> str | None:
    """Gmail's ``internalDate`` (epoch milliseconds) as RFC 3339 UTC."""
    try:
        millis = int(str(internal_date))
    except ValueError:
        return None
    when = datetime.datetime.fromtimestamp(millis / 1000, tz=datetime.UTC)
    return when.isoformat(timespec="seconds").replace("+00:00", "Z")


def summarize(message: Mapping[str, Any], *, text: bool = True, payload: bool = False) -> dict[str, Any]:
    """A message in Composio's shape (see the module docstring)."""
    found = message.get("payload")
    part: Mapping[str, Any] = found if isinstance(found, Mapping) else {}
    headers = headers_of(part)
    subject = headers.get("subject", "")
    out: dict[str, Any] = {
        "messageId": message.get("id"),
        "threadId": message.get("threadId"),
        "labelIds": list(message.get("labelIds") or []),
        "messageTimestamp": timestamp(message.get("internalDate")),
        "subject": subject,
        "sender": headers.get("from", ""),
        "to": headers.get("to", ""),
    }
    for name in ("cc", "date"):
        if headers.get(name):
            out[name] = headers[name]
    out["preview"] = {"subject": subject, "body": html.unescape(str(message.get("snippet") or ""))}
    if text and part:
        body, truncated = body_text(part)
        out["messageText"] = body
        if truncated:
            out["messageTextTruncated"] = True
        out["attachmentList"] = attachments_of(part)
    if payload and part:
        out["payload"] = part
    return out


def _textual(mimetype: str) -> bool:
    return mimetype.startswith("text/") or mimetype in ("application/json", "application/xml") or mimetype.endswith(
        ("+json", "+xml")
    )


# --------------------------------------------------------------------------
# Handlers: read
# --------------------------------------------------------------------------


def _get_message(client: GmailRestClient, user: str, message_id: str, fmt: str) -> dict[str, Any]:
    params: dict[str, Any] = {"format": fmt}
    if fmt == "metadata":
        params["metadataHeaders"] = list(SUMMARY_HEADERS)
    return json_object(client.get(f"/{user}/messages/{quote_segment(message_id)}", params))


def _fetch_emails(client: GmailRestClient, args: dict[str, Any]) -> ToolResult:
    user = _user(args)
    ids_only = bool(args.get("ids_only"))
    verbose = args.get("verbose", True) is not False
    params: dict[str, Any] = {
        "maxResults": _page_size(
            args.get("max_results"), DEFAULT_PAGE_SIZE, MAX_LIST_PAGE if ids_only else MAX_DETAIL_PAGE
        )
    }
    if isinstance(args.get("query"), str) and args["query"].strip():
        params["q"] = args["query"].strip()
    labels = _string_list(args.get("label_ids"), "label_ids")
    if labels:
        params["labelIds"] = _resolve_labels(client, user, labels, "label_ids")
    if args.get("page_token"):
        params["pageToken"] = args["page_token"]
    if args.get("include_spam_trash"):
        params["includeSpamTrash"] = "true"
    data = json_object(client.get(f"/{user}/messages", params))
    refs = [r for r in data.get("messages") or [] if isinstance(r, dict) and r.get("id")]
    items: list[dict[str, Any]] = []
    for ref in refs:
        if ids_only:
            items.append({"messageId": ref["id"], "threadId": ref.get("threadId")})
            continue
        try:
            message = _get_message(client, user, str(ref["id"]), "full" if verbose else "metadata")
        except GmailAPIError as e:
            if e.status == 404:  # deleted between the list and the read
                continue
            raise
        items.append(summarize(message, text=verbose, payload=verbose and bool(args.get("include_payload"))))
    return ToolResult.ok(
        listing(items, data.get("nextPageToken"), result_size_estimate=data.get("resultSizeEstimate"))
    )


def _fetch_message(client: GmailRestClient, args: dict[str, Any]) -> ToolResult:
    user = _user(args)
    fmt = args.get("format") or "full"
    path = f"/{user}/messages/{_path_id(args, 'message_id')}"
    params: dict[str, Any] = {"format": fmt}
    if fmt == "metadata":
        params["metadataHeaders"] = list(SUMMARY_HEADERS)
    message = json_object(client.get(path, params))
    if fmt != "raw":
        return ToolResult.ok(summarize(message, text=fmt == "full", payload=fmt == "full"))
    raw = str(message.get("raw") or "")
    try:
        content = decode_base64url(raw)
    except ValueError:
        raise GmailAPIError(0, "Gmail returned a raw message that is not base64url") from None
    data: dict[str, Any] = {
        "messageId": message.get("id"),
        "threadId": message.get("threadId"),
        "labelIds": list(message.get("labelIds") or []),
        "messageTimestamp": timestamp(message.get("internalDate")),
        "size": len(content),
    }
    if len(raw) <= INLINE_RAW_MAX:
        data["raw"] = raw
    envelope = DataEnvelope(
        content_type="message/rfc822",
        data=content,
        source_id=f"gmail:{message.get('id')}",
        metadata={"filename": f"{message.get('id')}.eml", "message_id": message.get("id")},
    )
    return ToolResult.ok(data, files=[envelope])


def _fetch_thread(client: GmailRestClient, args: dict[str, Any]) -> ToolResult:
    user = _user(args)
    thread = json_object(client.get(f"/{user}/threads/{_path_id(args, 'thread_id')}", {"format": "full"}))
    messages = [summarize(m) for m in thread.get("messages") or [] if isinstance(m, dict)]
    return ToolResult.ok(
        {
            "threadId": thread.get("id"),
            "historyId": thread.get("historyId"),
            "messageCount": len(messages),
            "messages": messages,
        }
    )


def _thread_summary(thread: Mapping[str, Any]) -> dict[str, Any]:
    messages = [m for m in thread.get("messages") or [] if isinstance(m, dict)]
    first = headers_of(messages[0].get("payload") or {}) if messages else {}
    senders: list[str] = []
    labels: list[str] = []
    for message in messages:
        sender = headers_of(message.get("payload") or {}).get("from")
        if sender and sender not in senders:
            senders.append(sender)
        labels.extend(label for label in message.get("labelIds") or [] if label not in labels)
    last = messages[-1] if messages else {}
    return {
        "id": thread.get("id"),
        "snippet": html.unescape(str(last.get("snippet") or thread.get("snippet") or "")),
        "historyId": thread.get("historyId"),
        "subject": first.get("subject", ""),
        "participants": senders,
        "messageCount": len(messages),
        "labelIds": labels,
        "lastMessageTimestamp": timestamp(last.get("internalDate")) if last else None,
    }


def _list_threads(client: GmailRestClient, args: dict[str, Any]) -> ToolResult:
    user = _user(args)
    verbose = bool(args.get("verbose"))
    params: dict[str, Any] = {
        "maxResults": _page_size(
            args.get("max_results"), DEFAULT_PAGE_SIZE, MAX_DETAIL_PAGE if verbose else MAX_LIST_PAGE
        )
    }
    if isinstance(args.get("query"), str) and args["query"].strip():
        params["q"] = args["query"].strip()
    if args.get("page_token"):
        params["pageToken"] = args["page_token"]
    data = json_object(client.get(f"/{user}/threads", params))
    threads = [t for t in data.get("threads") or [] if isinstance(t, dict) and t.get("id")]
    items: list[dict[str, Any]] = []
    for thread in threads:
        if not verbose:
            items.append(
                {
                    "id": thread["id"],
                    "snippet": html.unescape(str(thread.get("snippet") or "")),
                    "historyId": thread.get("historyId"),
                }
            )
            continue
        try:
            detail = json_object(
                client.get(
                    f"/{user}/threads/{quote_segment(str(thread['id']))}",
                    {"format": "metadata", "metadataHeaders": list(SUMMARY_HEADERS)},
                )
            )
        except GmailAPIError as e:
            if e.status == 404:
                continue
            raise
        items.append(_thread_summary(detail))
    return ToolResult.ok(
        listing(items, data.get("nextPageToken"), result_size_estimate=data.get("resultSizeEstimate"))
    )


def _get_attachment(client: GmailRestClient, args: dict[str, Any]) -> ToolResult:
    user = _user(args)
    message_id = _path_id(args, "message_id")
    attachment_id = _path_id(args, "attachment_id")
    name = safe_filename(str(args.get("file_name") or ""))
    body = json_object(
        client.get(f"/{user}/messages/{message_id}/attachments/{attachment_id}", timeout=MEDIA_TIMEOUT)
    )
    try:
        content = decode_base64url(str(body.get("data") or ""))
    except ValueError:
        raise GmailAPIError(0, "Gmail returned attachment data that is not base64url") from None
    mimetype = guess_mimetype(name)
    data: dict[str, Any] = {
        "name": name,
        "mimetype": mimetype,
        "size": len(content),
        "message_id": args["message_id"].strip(),
        "attachment_id": args["attachment_id"].strip(),
    }
    if len(content) <= INLINE_TEXT_MAX and _textual(mimetype):
        try:
            data["content"] = content.decode("utf-8")
        except UnicodeDecodeError:
            pass
    envelope = DataEnvelope(
        content_type=mimetype,
        data=content,
        source_id=f"gmail:{data['message_id']}",
        metadata={
            "filename": name,
            "mime_type": mimetype,
            "message_id": data["message_id"],
            "attachment_id": data["attachment_id"],
        },
    )
    return ToolResult.ok(data, files=[envelope])


def _list_labels(client: GmailRestClient, args: dict[str, Any]) -> ToolResult:
    user = _user(args)
    labels = [label for label in json_object(client.get(f"/{user}/labels")).get("labels") or [] if isinstance(label, dict)]
    if args.get("include_details"):
        labels = [
            json_object(client.get(f"/{user}/labels/{quote_segment(str(label.get('id')))}"))
            for label in labels
            if label.get("id")
        ]
    return ToolResult.ok(listing(labels, None))


def _get_profile(client: GmailRestClient, args: dict[str, Any]) -> ToolResult:
    return ToolResult.ok(json_object(client.get(f"/{_user(args)}/profile")))


# --------------------------------------------------------------------------
# Handlers: compose
# --------------------------------------------------------------------------


def _decode_file(spec: Any, index: int) -> Attachment:
    if not isinstance(spec, Mapping):
        raise _invalid(f"attachment[{index}] must be an object with name and file_content_bytes")
    content = spec.get("file_content_bytes")
    if not isinstance(content, str):
        raise _invalid(
            f"attachment[{index}] needs file_content_bytes (base64); "
            "Composio s3key uploads are not supported here"
        )
    text = "".join(content.split())
    try:
        data = base64.b64decode(text, validate=True)
    except (binascii.Error, ValueError):
        try:
            data = decode_base64url(text)
        except ValueError:
            raise _invalid(f"attachment[{index}].file_content_bytes is not valid base64") from None
    name = safe_filename(str(spec.get("name") or ""), f"attachment-{index + 1}")
    mimetype = spec.get("mimetype") if isinstance(spec.get("mimetype"), str) and "/" in spec["mimetype"] else None
    return Attachment(name, mimetype or guess_mimetype(name), data)


def _attachments(value: Any) -> list[Attachment]:
    if value is None or value == [] or value == {}:
        return []
    specs = value if isinstance(value, list) else [value]
    files = [_decode_file(spec, i) for i, spec in enumerate(specs)]
    total = sum(len(f.content) for f in files)
    if total > ATTACHMENTS_MAX:
        raise _invalid(f"attachments total {total:,} bytes, over Gmail's {ATTACHMENTS_MAX:,}-byte limit")
    return files


@dataclass
class _Draft:
    to: list[Any]
    cc: list[Any]
    bcc: list[Any]
    subject: str
    body: str
    attachments: list[Attachment]
    in_reply_to: str | None = None
    references: str | None = None


def _one_line(value: str) -> str:
    return " ".join(value.split())


def _thread_context(client: GmailRestClient, user: str, thread_id: str) -> dict[str, str]:
    """What a reply in ``thread_id`` needs: its subject, the Message-ID and
    References of the last message, and who to answer (that message's
    Reply-To/From — or its To when the user sent it)."""
    thread = json_object(
        client.get(f"/{user}/threads/{thread_id}", {"format": "metadata", "metadataHeaders": list(REPLY_HEADERS)})
    )
    messages = [
        m for m in thread.get("messages") or [] if isinstance(m, dict) and "DRAFT" not in (m.get("labelIds") or [])
    ]
    if not messages:
        raise _invalid("the thread has no messages to reply to")
    last = messages[-1]
    headers = {k: _one_line(v) for k, v in headers_of(last.get("payload") or {}).items()}
    first = headers_of(messages[0].get("payload") or {})
    message_id = headers.get("message-id", "")
    sent_by_user = "SENT" in (last.get("labelIds") or [])
    return {
        "subject": _one_line(first.get("subject") or headers.get("subject", "")),
        "in_reply_to": message_id,
        "references": " ".join(x for x in (headers.get("references", ""), message_id) if x),
        "reply_to": headers.get("to", "") if sent_by_user else headers.get("reply-to") or headers.get("from", ""),
    }


def _compose(args: dict[str, Any], body_key: str) -> _Draft:
    try:
        to = parse_addresses(
            [args.get("recipient_email") or "", *_string_list(args.get("extra_recipients"), "extra_recipients")],
            "recipient_email",
        )
        cc = parse_addresses(_string_list(args.get("cc"), "cc"), "cc")
        bcc = parse_addresses(_string_list(args.get("bcc"), "bcc"), "bcc")
    except ValueError as e:
        raise _invalid(str(e)) from None
    return _Draft(
        to=to,
        cc=cc,
        bcc=bcc,
        subject=str(args.get("subject") or ""),
        body=str(args.get(body_key) or ""),
        attachments=_attachments(args.get("attachment")),
    )


def _apply_thread(client: GmailRestClient, user: str, args: dict[str, Any], draft: _Draft) -> str:
    """Thread the draft into ``thread_id``: Re: subject, In-Reply-To and
    References, and the thread's last sender when no recipient was given."""
    thread_id = _path_id(args, "thread_id")
    context = _thread_context(client, user, thread_id)
    if not draft.subject:
        draft.subject = reply_subject(context["subject"])
    draft.in_reply_to = context["in_reply_to"] or None
    draft.references = context["references"] or None
    if not (draft.to or draft.cc or draft.bcc) and context["reply_to"]:
        try:
            draft.to = parse_addresses([context["reply_to"]], "the thread's sender")
        except ValueError:
            draft.to = []
    return str(args["thread_id"]).strip()


def _raw(args: dict[str, Any], draft: _Draft) -> bytes:
    sender = None
    if args.get("from_email"):
        try:
            senders = parse_addresses([str(args["from_email"])], "from_email")
        except ValueError as e:
            raise _invalid(str(e)) from None
        if len(senders) != 1:
            raise _invalid("from_email must be one address")
        sender = senders[0]
    try:
        raw = build_message(
            to=draft.to,
            cc=draft.cc,
            bcc=draft.bcc,
            subject=draft.subject,
            body=draft.body,
            is_html=bool(args.get("is_html")),
            sender=sender,
            in_reply_to=draft.in_reply_to,
            references=draft.references,
            attachments=draft.attachments,
        )
    except ValueError as e:
        raise _invalid(str(e)) from None
    if len(raw) > MESSAGE_MAX:
        raise _invalid(f"the message is {len(raw):,} bytes, over Gmail's {MESSAGE_MAX:,}-byte limit")
    return raw


def _require_content(draft: _Draft, body_key: str) -> None:
    if not (draft.subject.strip() or draft.body.strip() or draft.attachments):
        raise _invalid(f"give a subject, a {body_key} or an attachment")


def _create_draft(client: GmailRestClient, args: dict[str, Any]) -> ToolResult:
    user = _user(args)
    draft = _compose(args, "body")
    _require_content(draft, "body")
    thread_id = _apply_thread(client, user, args, draft) if args.get("thread_id") else None
    created = client.post_message(f"/{user}/drafts", _raw(args, draft), thread_id=thread_id, draft=True)
    return ToolResult.ok(created)


def _send_email(client: GmailRestClient, args: dict[str, Any]) -> ToolResult:
    user = _user(args)
    draft = _compose(args, "body")
    if not (draft.to or draft.cc or draft.bcc):
        raise _invalid("give recipient_email (or extra_recipients, cc or bcc)")
    _require_content(draft, "body")
    sent = client.post_message(f"/{user}/messages/send", _raw(args, draft))
    return ToolResult.ok(sent)


def _reply_to_thread(client: GmailRestClient, args: dict[str, Any]) -> ToolResult:
    user = _user(args)
    draft = _compose(args, "message_body")
    if not (draft.body.strip() or draft.attachments):
        raise _invalid("give a message_body or an attachment")
    thread_id = _apply_thread(client, user, args, draft)
    if not (draft.to or draft.cc or draft.bcc):
        raise _invalid("the thread has no sender to reply to; give recipient_email")
    sent = client.post_message(f"/{user}/messages/send", _raw(args, draft), thread_id=thread_id)
    return ToolResult.ok(sent)


# --------------------------------------------------------------------------
# Handlers: labels and trash
# --------------------------------------------------------------------------


def _label_change(client: GmailRestClient, user: str, args: dict[str, Any]) -> dict[str, Any]:
    add = _string_list(args.get("add_label_ids"), "add_label_ids")
    remove = _string_list(args.get("remove_label_ids"), "remove_label_ids")
    if not add and not remove:
        raise _invalid("give add_label_ids or remove_label_ids")
    body: dict[str, Any] = {}
    known: dict[str, str] = {}
    if add:
        body["addLabelIds"] = _resolve_labels(client, user, add, "add_label_ids", known)
    if remove:
        body["removeLabelIds"] = _resolve_labels(client, user, remove, "remove_label_ids", known)
    return body


def _modify_message_labels(client: GmailRestClient, args: dict[str, Any]) -> ToolResult:
    user = _user(args)
    path = f"/{user}/messages/{_path_id(args, 'message_id')}/modify"
    return ToolResult.ok(json_object(client.post(path, json=_label_change(client, user, args))))


def _modify_thread_labels(client: GmailRestClient, args: dict[str, Any]) -> ToolResult:
    user = _user(args)
    path = f"/{user}/threads/{_path_id(args, 'thread_id')}/modify"
    thread = json_object(client.post(path, json=_label_change(client, user, args)))
    messages = [
        {"id": m.get("id"), "labelIds": m.get("labelIds") or []}
        for m in thread.get("messages") or []
        if isinstance(m, dict)
    ]
    return ToolResult.ok({"id": thread.get("id"), "messages": messages})


def _move_to_trash(client: GmailRestClient, args: dict[str, Any]) -> ToolResult:
    user = _user(args)
    return ToolResult.ok(json_object(client.post(f"/{user}/messages/{_path_id(args, 'message_id')}/trash")))


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
    scopes: tuple[str, ...],
    read_only: bool,
    destructive: bool = False,
    aliases: Mapping[str, str] | None = None,
) -> GmailTool:
    text = f"{description} Needs scope: {_short(scopes)}."
    return GmailTool(
        ToolSpec(
            slug, text, schema, read_only=read_only, destructive=destructive, tags=tags, required_scopes=scopes
        ),
        handler,
        scopes,
        aliases or {},
    )


def _read(slug: str, description: str, schema: dict[str, Any], handler: Handler, *tags: str) -> GmailTool:
    return _tool(slug, description, schema, handler, *tags, scopes=(READONLY_SCOPE,), read_only=True)


def _write(
    slug: str,
    description: str,
    schema: dict[str, Any],
    handler: Handler,
    *tags: str,
    scopes: tuple[str, ...],
    destructive: bool = False,
    aliases: Mapping[str, str] | None = None,
) -> GmailTool:
    return _tool(
        slug, description, schema, handler, *tags,
        scopes=scopes, read_only=False, destructive=destructive, aliases=aliases,
    )


_RECIPIENTS: dict[str, Any] = {
    "recipient_email": _string("Main recipient's address ('Ann <ann@x.com>' works; several comma-separated)."),
    "extra_recipients": _strings("More To: addresses."),
    "cc": _strings("Cc addresses."),
    "bcc": _strings("Bcc addresses."),
}
_SEND_ALIASES = {"to": "recipient_email", "message_body": "body", "attachments": "attachment"}

CATALOG: tuple[GmailTool, ...] = (
    # ------------------------------------------------------------------ read
    _read(
        "GMAIL_FETCH_EMAILS",
        "Search or list Gmail messages, newest first: query takes Gmail search syntax "
        "(from:, to:, subject:, is:unread, has:attachment, after:2026/10/01, label:...), "
        "label_ids filters by label (all must match). Each item has messageId, threadId, "
        "labelIds, messageTimestamp, subject, sender, to, preview, messageText and attachmentList "
        "(attachmentId + filename for GMAIL_GET_ATTACHMENT); verbose=false skips bodies, ids_only "
        "returns ids, include_payload adds the MIME payload. One page (10 by default, 100 max with "
        "details): has_more, next_page_token, items[]; pass next_page_token as page_token.",
        _schema(
            {
                "query": QUERY,
                "label_ids": LABEL_IDS,
                "max_results": _integer(
                    "Messages per page (1-100; up to 500 with ids_only).", default=DEFAULT_PAGE_SIZE
                ),
                "page_token": PAGE_TOKEN,
                "include_payload": _boolean("Add each message's raw MIME payload (headers and parts).", default=False),
                "include_spam_trash": _boolean("Include Spam and Trash.", default=False),
                "verbose": _boolean("Read bodies and attachments (false: headers only, faster).", default=True),
                "ids_only": _boolean("Return only messageId/threadId (fastest).", default=False),
                "user_id": USER_ID,
            }
        ),
        _fetch_emails,
        "email", "emails", "mail", "inbox", "messages", "search", "list", "unread", "recent", "fetch", "read",
    ),
    _read(
        "GMAIL_FETCH_MESSAGE_BY_MESSAGE_ID",
        "Get one Gmail message by messageId: headers (subject, sender, to, cc, date), labelIds, "
        "messageText (the body as text), attachmentList and the MIME payload. format: full "
        "(default), metadata (headers only), minimal (ids and labels) or raw (the whole RFC 822 "
        "message, returned as a .eml file).",
        _schema(
            {
                "message_id": MESSAGE_ID,
                "format": _string(
                    "How much to return.", enum=("minimal", "metadata", "full", "raw"), default="full"
                ),
                "user_id": USER_ID,
            },
            ["message_id"],
        ),
        _fetch_message,
        "email", "message", "read", "open", "get", "body", "content", "details",
    ),
    _read(
        "GMAIL_LIST_THREADS",
        "List Gmail conversations (threads), newest first, optionally filtered by query (Gmail "
        "search syntax). Each item has id and snippet; verbose=true adds subject, participants, "
        "messageCount, labelIds and lastMessageTimestamp. One page: has_more, next_page_token, "
        "items[]; read one with GMAIL_FETCH_MESSAGE_BY_THREAD_ID.",
        _schema(
            {
                "query": QUERY,
                "max_results": _integer(
                    "Threads per page (1-500; up to 100 with verbose).", default=DEFAULT_PAGE_SIZE
                ),
                "page_token": PAGE_TOKEN,
                "verbose": _boolean("Add subject, participants and counts (one request per thread).", default=False),
                "user_id": USER_ID,
            }
        ),
        _list_threads,
        "threads", "conversations", "list", "search", "inbox", "email",
    ),
    _read(
        "GMAIL_FETCH_MESSAGE_BY_THREAD_ID",
        "Get a whole Gmail conversation by threadId: every message in order, each with subject, "
        "sender, to, messageTimestamp, labelIds, messageText and attachmentList.",
        _schema({"thread_id": THREAD_ID, "user_id": USER_ID}, ["thread_id"]),
        _fetch_thread,
        "thread", "conversation", "messages", "read", "replies", "history", "get",
    ),
    _read(
        "GMAIL_GET_ATTACHMENT",
        "Download one attachment of a Gmail message (returned as a file with name, mimetype and "
        "size; small text files also inline as content). message_id and attachment_id come from "
        "a message's attachmentList; file_name names the file (and sets its type).",
        _schema(
            {
                "message_id": MESSAGE_ID,
                "attachment_id": _id("The attachment's id (attachmentId in attachmentList)."),
                "file_name": _string("File name for the download, e.g. 'invoice.pdf' (its filename)."),
                "user_id": USER_ID,
            },
            ["message_id", "attachment_id"],
        ),
        _get_attachment,
        "attachment", "download", "file", "pdf", "document", "save",
    ),
    _read(
        "GMAIL_LIST_LABELS",
        "List the mailbox's labels — system ones (INBOX, UNREAD, STARRED, SPAM, TRASH, "
        "CATEGORY_*) and the user's own — with id, name and type. include_details adds message "
        "and thread counts (total and unread) per label.",
        _schema(
            {
                "include_details": _boolean("Add counts (one request per label).", default=False),
                "user_id": USER_ID,
            }
        ),
        _list_labels,
        "labels", "folders", "categories", "list", "tags",
    ),
    _read(
        "GMAIL_GET_PROFILE",
        "Get the connected Gmail account: emailAddress, messagesTotal, threadsTotal and historyId.",
        _schema({"user_id": USER_ID}),
        _get_profile,
        "profile", "account", "me", "whoami", "address", "email address",
    ),
    # --------------------------------------------------------------- compose
    _write(
        "GMAIL_CREATE_EMAIL_DRAFT",
        "Save an email as a Gmail draft (not sent): recipients, subject, body (is_html for HTML), "
        "attachments. With thread_id the draft is a reply in that conversation (Re: subject and "
        "threading headers set; the last sender is the default recipient). Returns the draft id "
        "and its message.",
        _schema(
            {
                **_RECIPIENTS,
                "subject": _string("Subject line."),
                "body": _string("Message body, plain text (or HTML with is_html)."),
                "is_html": _boolean("The body is HTML.", default=False),
                "thread_id": _id("Make the draft a reply in this thread."),
                "attachment": ATTACHMENT,
                "user_id": USER_ID,
            }
        ),
        _create_draft,
        "draft", "compose", "write", "email", "prepare",
        scopes=(COMPOSE_SCOPE,),
        aliases=_SEND_ALIASES,
    ),
    _write(
        "GMAIL_SEND_EMAIL",
        "Send an email now from the connected Gmail account: recipient_email (plus "
        "extra_recipients, cc, bcc), subject, body (is_html for HTML) and optional attachments; "
        "from_email picks a send-as alias. To answer an existing conversation use "
        "GMAIL_REPLY_TO_THREAD. Returns the sent message's id, threadId and labelIds.",
        _schema(
            {
                **_RECIPIENTS,
                "subject": _string("Subject line."),
                "body": _string("Message body, plain text (or HTML with is_html)."),
                "is_html": _boolean("The body is HTML.", default=False),
                "attachment": ATTACHMENT,
                "from_email": _string("Send as this verified alias of the account."),
                "user_id": USER_ID,
            }
        ),
        _send_email,
        "send", "email", "mail", "message", "compose", "write",
        scopes=(SEND_SCOPE,),
        aliases=_SEND_ALIASES,
    ),
    _write(
        "GMAIL_REPLY_TO_THREAD",
        "Reply in a Gmail conversation (thread_id): message_body (is_html for HTML) goes to the "
        "last message's sender unless recipient_email/cc/bcc are given, with Re: subject and "
        "threading headers so it lands in the same thread. Returns the sent message's id and "
        "threadId.",
        _schema(
            {
                "thread_id": THREAD_ID,
                "message_body": _string("The reply, plain text (or HTML with is_html)."),
                **_RECIPIENTS,
                "is_html": _boolean("The body is HTML.", default=False),
                "attachment": ATTACHMENT,
                "user_id": USER_ID,
            },
            ["thread_id"],
        ),
        _reply_to_thread,
        "reply", "respond", "answer", "thread", "email", "send",
        scopes=(READONLY_SCOPE, SEND_SCOPE),
        aliases={"body": "message_body", "to": "recipient_email", "attachments": "attachment"},
    ),
    # ---------------------------------------------------------------- labels
    _write(
        "GMAIL_ADD_LABEL_TO_EMAIL",
        "Change a Gmail message's labels: add_label_ids and/or remove_label_ids. Mark read = "
        "remove UNREAD; mark unread = add UNREAD; star = add STARRED; archive = remove INBOX. "
        "User labels by id (Label_123) or exact name. Returns the message's labelIds.",
        _schema(
            {
                "message_id": MESSAGE_ID,
                "add_label_ids": LABEL_IDS,
                "remove_label_ids": LABEL_IDS,
                "user_id": USER_ID,
            },
            ["message_id"],
        ),
        _modify_message_labels,
        "email", "label", "mark", "read", "unread", "star", "archive", "important", "tag", "move", "modify",
        scopes=(MODIFY_SCOPE,),
    ),
    _write(
        "GMAIL_MODIFY_THREAD_LABELS",
        "Change the labels of every message in a Gmail thread: add_label_ids and/or "
        "remove_label_ids (remove UNREAD = mark the conversation read; remove INBOX = archive it).",
        _schema(
            {
                "thread_id": THREAD_ID,
                "add_label_ids": LABEL_IDS,
                "remove_label_ids": LABEL_IDS,
                "user_id": USER_ID,
            },
            ["thread_id"],
        ),
        _modify_thread_labels,
        "label", "thread", "conversation", "mark", "read", "archive", "modify",
        scopes=(MODIFY_SCOPE,),
    ),
    _write(
        "GMAIL_MOVE_TO_TRASH",
        "Move a Gmail message to the Trash (Gmail deletes it for good after 30 days; until then "
        "it can be restored from Trash). Returns the message's id and labelIds.",
        _schema({"message_id": MESSAGE_ID, "user_id": USER_ID}, ["message_id"]),
        _move_to_trash,
        "email", "trash", "delete", "remove", "bin", "discard",
        scopes=(MODIFY_SCOPE,),
        destructive=True,
    ),
)

TOOLS: dict[str, GmailTool] = {tool.spec.slug: tool for tool in CATALOG}
TOOL_SPECS: tuple[ToolSpec, ...] = tuple(tool.spec for tool in CATALOG)
#: Each tool's OAuth scopes, by slug.
REQUIRED_SCOPES: dict[str, tuple[str, ...]] = {tool.spec.slug: tool.scopes for tool in CATALOG}
