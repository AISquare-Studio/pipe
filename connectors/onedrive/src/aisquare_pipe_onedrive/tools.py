"""The OneDrive toolkit's catalog: one :class:`ToolSpec` plus one handler per tool.

Slugs and argument names are Composio's ``one_drive`` toolkit's, verbatim
(including its camelCase ``ONE_DRIVE_MOVE_ITEM``), so callers written against
Composio (the BE's Content Vault import, saved prompts) keep working.
Arguments map onto Microsoft Graph v1.0 ``driveItem`` endpoints; the
connected user's own OneDrive (``/me/drive``) unless ``drive_id`` (or
``user_id`` / ``site_id`` / ``group_id``) names another drive.

Result shapes (``ToolResult.data``):

* object endpoints return Graph's object;
* list endpoints return ``{"has_more", "next_page_token", "items": [...]}`` —
  one page per call; pass ``next_page_token`` back as ``page_token`` (with the
  same item/folder arguments) to continue.

The pagination hints come first so they survive a caller truncating a long
result. Graph's ``@``-annotations are dropped everywhere: ``@odata.*`` is
noise, and ``@microsoft.graph.downloadUrl`` is a pre-authenticated link that
grants the file's content to whoever holds it — it never leaves the toolkit.
Downloads put the bytes in ``ToolResult.files``.
"""

from __future__ import annotations

import base64
import binascii
import mimetypes
import posixpath
import re
import urllib.parse
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from aisquare.pipe import (
    ERROR_CONFLICT,
    ERROR_INVALID_ARGUMENTS,
    ERROR_UPSTREAM_ERROR,
    DataEnvelope,
    ToolError,
    ToolResult,
    ToolSpec,
)

from aisquare_pipe_onedrive.constants import CHUNK_SIZE, SIMPLE_UPLOAD_LIMIT
from aisquare_pipe_onedrive.rest import (
    DownloadLimitError,
    OneDriveAPIError,
    OneDriveRestClient,
    json_body,
)

Handler = Callable[[OneDriveRestClient, dict[str, Any]], ToolResult]

#: UTF-8 files up to this size also come back as text in ``data["content"]``.
TEXT_INLINE_MAX = 100_000
#: Largest file ONE_DRIVE_DOWNLOAD_FILE reads into memory.
DOWNLOAD_MAX_BYTES = 100 * 1024 * 1024
#: How long ONE_DRIVE_COPY_ITEM watches Graph's async copy before answering.
COPY_MONITOR_WAIT = 10.0
_COPY_POLL_WAITS = (0.5, 1.0, 2.0)

_DEFAULT_TOP = 50
_MAX_TOP = 200
#: The drive root a page token (a nextLink minus the API base) must start at.
_PAGE_DRIVE_ROOT = re.compile(r"/(?:me/drive|drives/[^/?#]+)/")
#: A decoded path that still encodes a separator or dot (double encoding).
_STILL_ENCODED = re.compile(r"%(?:2e|2f|5c|3f|23)", re.IGNORECASE)
#: How each paged listing's path ends, so a token only continues its own
#: kind. Functions may come back namespace-qualified ('microsoft.graph.recent()').
_PAGE_KINDS = {
    "children": re.compile(r"/children$"),
    "search": re.compile(r"/(?:microsoft\.graph\.)?search\(q='[^?#]*'\)$"),
    "recent": re.compile(r"/(?:microsoft\.graph\.)?recent(?:\(\))?$"),
    "sharedWithMe": re.compile(r"/(?:microsoft\.graph\.)?sharedWithMe(?:\(\))?$"),
    "permissions": re.compile(r"/permissions$"),
}
#: OneDrive for Business / SharePoint item IDs: '01' + 32 base32 characters.
_BUSINESS_ITEM_ID = re.compile(r"01[A-Z0-9]{32}")
#: A ``parentReference.path`` prefix ('/drive/root:', '/drives/b!x/root:').
_ROOT_PREFIX = re.compile(r"^/?(?:drive|drives/[^/]+)/root:")
_SEGMENT_SAFE = "!,"
_FORMAT_MIME = {"pdf": "application/pdf", "html": "text/html"}
_MIME = mimetypes.MimeTypes()  # stdlib defaults only: same answer on every host
_MIME_OVERRIDES = {".md": "text/markdown", ".markdown": "text/markdown"}


@dataclass(frozen=True)
class OneDriveTool:
    spec: ToolSpec
    handler: Handler


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


def _boolean(description: str) -> dict[str, Any]:
    return {"type": "boolean", "description": description}


def _strings(description: str) -> dict[str, Any]:
    return {"type": "array", "items": {"type": "string"}, "description": description}


def _object(description: str, properties: dict[str, Any] | None = None) -> dict[str, Any]:
    schema: dict[str, Any] = {"type": "object", "description": description}
    if properties:
        schema["properties"] = properties
    return schema


ITEM_ID = {
    **_string(
        "The DriveItem ID (file or folder) exactly as returned by ONE_DRIVE_SEARCH_ITEMS or "
        "ONE_DRIVE_LIST_FOLDER_CHILDREN. Not a name, path or web URL; IDs go stale after a "
        "move or delete."
    ),
    "minLength": 1,
}
DRIVE_ID = _string(
    "ID of the drive that holds the item. Omit for the connected user's own OneDrive; needed "
    "for shared libraries and other people's drives."
)
USER_ID = _string(
    "User ID or principal name whose OneDrive to use; omit (or 'me') for the connected user. "
    "Ignored when drive_id is given."
)
SITE_ID = _string("SharePoint site ID whose document library to use. Ignored when drive_id is given.")
GROUP_ID = _string("Microsoft 365 group ID whose document library to use. Ignored when drive_id is given.")
DRIVE_ARGS = {"drive_id": DRIVE_ID, "user_id": USER_ID}
LOCATION_ARGS = {**DRIVE_ARGS, "site_id": SITE_ID, "group_id": GROUP_ID}
TOP = {
    "type": "integer",
    "minimum": 1,
    "maximum": _MAX_TOP,
    "default": _DEFAULT_TOP,
    "description": f"Items per page (max {_MAX_TOP}).",
}
PAGE_TOKEN = _string(
    "Continuation token: the previous result's next_page_token, passed back unmodified with "
    "the same other arguments. Omit for the first page."
)
IF_MATCH = _string(
    "Only act if the item's current eTag (or cTag) still equals this value; otherwise the call "
    "fails with 412 and nothing changes."
)


def _conflict(default: str, choices: Iterable[str] = ("fail", "rename", "replace")) -> dict[str, Any]:
    return _string(
        "What to do when an item with the same name already exists at the destination "
        f"(default '{default}'): 'fail' errors, 'rename' adds a number to the new name"
        + (", 'replace' overwrites the existing item." if "replace" in choices else "."),
        enum=choices,
    )


# --------------------------------------------------------------------------
# Argument and response helpers
# --------------------------------------------------------------------------


def _invalid(message: str) -> ToolError:
    return ToolError(message, code=ERROR_INVALID_ARGUMENTS)


def _arg(args: Mapping[str, Any], key: str) -> str | None:
    """A string argument, stripped; None when absent or blank."""
    value = args.get(key)
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _segment(name: str, value: Any) -> str:
    """One URL path segment (an ID or a name), percent-encoded.

    A ``.`` / ``..`` segment or an embedded ``/`` is refused: the HTTP stack
    resolves dot segments, so ``item_id='../../..'`` would turn a delete into
    a call on a different endpoint.
    """
    text = str(value).strip()
    if not text:
        raise _invalid(f"{name} must not be empty")
    if text in (".", "..") or "/" in text or "\\" in text:
        raise _invalid(f"{name} must be a single ID or name, without '/' or '.' / '..' segments")
    return urllib.parse.quote(text, safe=_SEGMENT_SAFE)


def _path(name: str, value: Any) -> str:
    """A drive-relative path, percent-encoded per segment ('' for the root).
    Accepts '/Documents/Plans', 'Documents/Plans/' and a
    ``parentReference.path`` such as '/drive/root:/Documents'."""
    text = _ROOT_PREFIX.sub("", str(value or "").strip())
    parts = [part for part in text.split("/") if part]
    if any(part.strip() in (".", "..") for part in parts):
        raise _invalid(f"{name} must not contain '.' or '..' path segments")
    return "/".join(urllib.parse.quote(part, safe=_SEGMENT_SAFE) for part in parts)


def _drive(
    args: Mapping[str, Any],
    *,
    drive: str = "drive_id",
    user: str = "user_id",
    site: str = "site_id",
    group: str = "group_id",
) -> str:
    """The Graph path of the drive the call targets (``/me/drive`` by default)."""
    if value := _arg(args, drive):
        return f"/drives/{_segment(drive, value)}"
    if value := _arg(args, site):
        return f"/sites/{_segment(site, value)}/drive"
    if value := _arg(args, group):
        return f"/groups/{_segment(group, value)}/drive"
    value = _arg(args, user)
    if value and value.lower() != "me":
        return f"/users/{_segment(user, value)}/drive"
    return "/me/drive"


def _item(args: Mapping[str, Any], key: str = "item_id") -> str:
    return f"{_drive(args)}/items/{_segment(key, args.get(key, ''))}"


def _by_path(drive: str, path: str) -> str:
    return f"{drive}/root:/{path}:" if path else f"{drive}/root"


def _looks_like_item_id(text: str) -> bool:
    return "/" not in text and ("!" in text or _BUSINESS_ITEM_ID.fullmatch(text) is not None)


def _folder(args: Mapping[str, Any], key: str, drive: str) -> str:
    """A folder given as an item ID or a drive-relative path (a leading '/'
    always means a path); the root when absent."""
    value = _arg(args, key)
    if not value or value == "/":
        return f"{drive}/root"
    if not value.startswith("/") and _looks_like_item_id(value):
        return f"{drive}/items/{_segment(key, value)}"
    return _by_path(drive, _path(key, value))


def _child(args: Mapping[str, Any], key: str, drive: str, name: str) -> str:
    """Path-addressed ``<folder>/<name>`` (``.../root:/a/b.txt:``) for uploads."""
    value = _arg(args, key)
    if value and value != "/" and not value.startswith("/") and _looks_like_item_id(value):
        return f"{drive}/items/{_segment(key, value)}:/{name}:"
    folder = _path(key, value) if value else ""
    return f"{drive}/root:/{folder}/{name}:" if folder else f"{drive}/root:/{name}:"


def _query_value(value: Any) -> Any:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (list, tuple)):
        return ",".join(str(v) for v in value)
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return value


def _odata(args: Mapping[str, Any], names: Mapping[str, str]) -> dict[str, Any]:
    """OData query options (``$top``, ``$select``, ...) from the named arguments."""
    params: dict[str, Any] = {}
    for arg_name, option in names.items():
        value = args.get(arg_name)
        if value is None or value == "" or value == []:
            continue
        if option == "$top":  # the validator does not enforce minimum/maximum
            value = min(max(1, int(value)), _MAX_TOP)
        params[option] = _query_value(value)
    return params


def clean(value: Any) -> Any:
    """Drop Graph's ``@``-annotations (``@odata.*``, and the pre-authenticated
    ``@microsoft.graph.downloadUrl``) at every depth."""
    if isinstance(value, dict):
        return {
            key: clean(item)
            for key, item in value.items()
            if not (isinstance(key, str) and key.startswith("@"))
        }
    if isinstance(value, list):
        return [clean(item) for item in value]
    return value


def _is_page_token(token: str, kind: str) -> bool:
    """A Graph path under a drive root (``/me/drive/...`` or
    ``/drives/{id}/...``) whose listing is ``kind``, optionally with a query.

    Every check runs on the percent-DECODED path, because that is what the
    server acts on: requests turns ``%2e`` back into ``.`` and drops a
    ``#fragment``, so ``/me/drive/%2e%2e/%2e%2e/me/messages#/children`` would
    otherwise pass as a folder listing and fetch mail. Refused: a fragment
    (any ``#``), a scheme or host, an encoded ``/`` (the decoded path has
    more segments than the raw one), empty, ``.`` or ``..`` segments,
    backslashes, decoded ``?`` / ``#`` / control characters, and a second
    layer of encoding (``%252e``)."""
    if "#" in token or "\\" in token or any(ch.isspace() or ord(ch) < 32 or ord(ch) == 127 for ch in token):
        return False
    parts = urllib.parse.urlsplit(token)
    if parts.scheme or parts.netloc or not parts.path.startswith("/"):
        return False
    path = urllib.parse.unquote(parts.path)
    segments = path.split("/")[1:]
    return (
        len(segments) == len(parts.path.split("/")[1:])
        and all(seg and seg not in (".", "..") for seg in segments)
        and not any(ch in "\\?#" or ord(ch) < 32 or ord(ch) == 127 for ch in path)
        and _STILL_ENCODED.search(path) is None
        and _PAGE_DRIVE_ROOT.match(path) is not None
        and _PAGE_KINDS[kind].search(path) is not None
    )


def page_token_for(next_link: Any, base_url: str, kind: str) -> str | None:
    """The page token for a Graph ``@odata.nextLink``: its path and query
    under the API base. None when there is no next page, or when it is not a
    token the listing would accept back (see :func:`page_target`)."""
    prefix = base_url.rstrip("/")
    if not isinstance(next_link, str) or not next_link.startswith(prefix + "/"):
        return None
    token = next_link[len(prefix):]
    return token if _is_page_token(token, kind) else None


def page_target(token: str, kind: str) -> str:
    """Check a caller's ``page_token`` before anything is sent: it must be a
    nextLink for a drive root (``/me/drive/...``, ``/drives/{id}/...``) of
    this ``kind`` of listing. Anything else (``/me/messages``, a
    ``/users/...`` path, another listing's token) is refused, so a crafted
    token cannot point a read-only tool's bearer GET at other Graph data. It
    is always sent to the Graph base URL, never to another host."""
    if not _is_page_token(token, kind):
        raise _invalid(
            "page_token is not a token this tool returned — pass next_page_token back unmodified"
        )
    return token


def _page(client: OneDriveRestClient, response: Any, kind: str) -> dict[str, Any]:
    body = json_body(response)
    body = body if isinstance(body, dict) else {}
    items = body.get("value")
    next_link = body.get("@odata.nextLink")
    return {
        "has_more": bool(next_link),
        "next_page_token": page_token_for(next_link, client.base_url, kind),
        "items": clean(items) if isinstance(items, list) else [],
    }


def _paged(
    client: OneDriveRestClient,
    args: Mapping[str, Any],
    path: str,
    options: Mapping[str, str],
    kind: str,
    extra: Mapping[str, Any] | None = None,
) -> ToolResult:
    """One page of a Graph collection: the first page from ``path`` and the
    query options, or the page ``page_token`` names (a ``kind`` listing)."""
    token = _arg(args, "page_token")
    if token:
        response = client.get(page_target(token, kind))
    else:
        response = client.get(path, {**_odata(args, options), **(extra or {})})
    return ToolResult.ok(_page(client, response, kind))


def _object_result(response: Any) -> ToolResult:
    data = json_body(response)
    return ToolResult.ok(clean(data) if data is not None else {})


def _utf8(content: bytes) -> str | None:
    try:
        return content.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _guess_mimetype(name: str, content: bytes) -> str:
    ext = posixpath.splitext(name)[1].lower()
    guessed = _MIME_OVERRIDES.get(ext) or _MIME.guess_type(name)[0]
    if guessed:
        return guessed
    return "text/plain" if _utf8(content) is not None else "application/octet-stream"


def _if_match(args: Mapping[str, Any]) -> dict[str, str] | None:
    value = _arg(args, "if_match")
    return {"If-Match": value} if value else None


# --------------------------------------------------------------------------
# Handlers
# --------------------------------------------------------------------------

_SEARCH_OPTIONS = {"top": "$top", "select": "$select", "orderby": "$orderby", "expand": "$expand"}


def _search_items(client: OneDriveRestClient, args: dict[str, Any]) -> ToolResult:
    # Graph's search takes plain keywords: wildcards only add noise.
    q = " ".join(str(args["q"]).replace("*", " ").replace("?", " ").split())
    if not q:
        raise _invalid("q needs at least one keyword (wildcards are not supported)")
    drive = _drive(args)
    base = drive if args.get("search_scope") == "drive" else f"{drive}/root"
    literal = urllib.parse.quote(q.replace("'", "''"), safe="")
    return _paged(client, args, f"{base}/search(q='{literal}')", _SEARCH_OPTIONS, "search")


def _download_file(client: OneDriveRestClient, args: dict[str, Any]) -> ToolResult:
    target = _item(args)
    meta = json_body(client.get(target))
    meta = meta if isinstance(meta, dict) else {}
    remote = meta.get("remoteItem")
    if "file" not in meta and "folder" not in meta and isinstance(remote, dict) and remote.get("id"):
        # A shared item added to this drive: its bytes live in the owner's drive.
        remote_drive = (remote.get("parentReference") or {}).get("driveId")
        if remote_drive:
            target = f"/drives/{_segment('driveId', remote_drive)}/items/{_segment('id', remote['id'])}"
        meta = {**remote, "name": meta.get("name") or remote.get("name")}
    if "folder" in meta or "package" in meta:
        raise _invalid("item_id is a folder, not a file — list it with ONE_DRIVE_LIST_FOLDER_CHILDREN")
    limit_mb = DOWNLOAD_MAX_BYTES // (1024 * 1024)
    size = meta.get("size")
    if isinstance(size, int) and size > DOWNLOAD_MAX_BYTES:
        raise _invalid(f"the file is {size} bytes; downloads are limited to {limit_mb} MiB")
    fmt = args.get("format")
    try:
        # Streamed: the cap holds while reading (metadata may omit the size,
        # and a converted file's size is unknown) and the budget is checked
        # between chunks.
        content = client.download_bytes(
            f"{target}/content", {"format": fmt} if fmt else None, max_bytes=DOWNLOAD_MAX_BYTES
        )
    except DownloadLimitError:
        raise _invalid(f"the file is larger than the {limit_mb} MiB download limit") from None

    item_id = str(meta.get("id") or str(args["item_id"]).strip())
    source_name = str(meta.get("name") or item_id)
    if fmt:
        source_name = f"{posixpath.splitext(source_name)[0]}.{fmt}"
    name = posixpath.basename(_arg(args, "file_name") or "") or source_name
    mimetype = (
        _FORMAT_MIME.get(str(fmt))
        or (meta.get("file") or {}).get("mimeType")
        or _guess_mimetype(name, content)
    )
    data: dict[str, Any] = {"id": item_id, "name": name, "mimetype": mimetype, "size": len(content)}
    for key in ("webUrl", "lastModifiedDateTime"):
        if meta.get(key):
            data[key] = meta[key]
    text = _utf8(content) if len(content) <= TEXT_INLINE_MAX else None
    if text is not None:
        data["content"] = text
    drive_id = (meta.get("parentReference") or {}).get("driveId")
    envelope = DataEnvelope(
        content_type=mimetype,
        data=content,
        source_id=f"onedrive:{item_id}",
        metadata={
            "filename": name,
            "item_id": item_id,
            "drive_id": drive_id,
            "web_url": meta.get("webUrl"),
        },
    )
    return ToolResult.ok(data, files=[envelope])


_ITEM_OPTIONS = {"select_fields": "$select", "expand_relations": "$expand"}


def _get_item(client: OneDriveRestClient, args: dict[str, Any]) -> ToolResult:
    return _object_result(client.get(_item(args), _odata(args, _ITEM_OPTIONS)))


def _get_item_by_path(client: OneDriveRestClient, args: dict[str, Any]) -> ToolResult:
    path = _path("item_path", args["item_path"])
    return _object_result(client.get(_by_path(_drive(args), path), _odata(args, _ITEM_OPTIONS)))


_CHILDREN_OPTIONS = {"top": "$top", "select": "$select", "expand": "$expand", "orderby": "$orderby"}


def _list_folder_children(client: OneDriveRestClient, args: dict[str, Any]) -> ToolResult:
    drive = _drive(args)
    folder_id = _arg(args, "folder_item_id")
    if folder_id:
        folder = f"{drive}/items/{_segment('folder_item_id', folder_id)}"
    else:
        folder = _by_path(drive, _path("folder_path", args.get("folder_path")))
    return _paged(client, args, f"{folder}/children", _CHILDREN_OPTIONS, "children")


def _get_drive(client: OneDriveRestClient, args: dict[str, Any]) -> ToolResult:
    options = {"select_fields": "$select", "expand_fields": "$expand"}
    return _object_result(client.get(_drive(args), _odata(args, options)))


def _get_recent_items(client: OneDriveRestClient, args: dict[str, Any]) -> ToolResult:
    return _paged(client, args, "/me/drive/recent", {"top": "$top", "select": "$select"}, "recent")


def _get_shared_items(client: OneDriveRestClient, args: dict[str, Any]) -> ToolResult:
    extra = {"allowexternal": "true"} if args.get("allow_external") else None
    return _paged(client, args, "/me/drive/sharedWithMe", {"select": "$select"}, "sharedWithMe", extra)


def _get_item_permissions(client: OneDriveRestClient, args: dict[str, Any]) -> ToolResult:
    return _paged(client, args, f"{_item(args)}/permissions", {"select": "$select"}, "permissions")


def _decode_content(args: Mapping[str, Any]) -> bytes:
    content = args["content"]
    if args.get("encoding") == "base64":
        try:
            return base64.b64decode(str(content), validate=True)
        except (binascii.Error, ValueError):
            raise _invalid("content is not valid base64 (encoding is 'base64')") from None
    return str(content).encode("utf-8")


def _upload_file(client: OneDriveRestClient, args: dict[str, Any]) -> ToolResult:
    name = _segment("file_name", args["file_name"])
    raw = _decode_content(args)
    drive = _drive(args)
    target = _child(args, "folder", drive, name)
    conflict = args.get("conflict_behavior") or "rename"
    description = _arg(args, "description")
    if len(raw) <= SIMPLE_UPLOAD_LIMIT:
        response = client.request(
            "PUT",
            f"{target}/content",
            params={"@microsoft.graph.conflictBehavior": conflict},
            data=raw,
            headers={"Content-Type": "application/octet-stream"},
        )
        item = json_body(response)
        if description and isinstance(item, dict) and item.get("id"):
            response = client.request(
                "PATCH",
                f"{drive}/items/{_segment('id', item['id'])}",
                json={"description": description},
            )
            item = json_body(response)
        return ToolResult.ok(clean(item) if item is not None else {})

    session_item: dict[str, Any] = {"@microsoft.graph.conflictBehavior": conflict}
    if description:
        session_item["description"] = description
    session = json_body(
        client.request("POST", f"{target}/createUploadSession", json={"item": session_item})
    )
    upload_url = session.get("uploadUrl") if isinstance(session, dict) else None
    if not isinstance(upload_url, str) or not upload_url.startswith("https://"):
        raise OneDriveAPIError(0, "Microsoft Graph returned an upload session without an uploadUrl")
    try:
        item = _upload_chunks(client, upload_url, raw)
    except OneDriveAPIError:
        _cancel_upload(client, upload_url)
        raise
    return ToolResult.ok(clean(item))


def _upload_chunks(client: OneDriveRestClient, upload_url: str, raw: bytes) -> Any:
    """PUT ``raw`` to an upload session in ``CHUNK_SIZE`` pieces. The upload
    URL is pre-authenticated: Graph must not see the bearer token there."""
    size = len(raw)
    offset = 0
    while offset < size:
        chunk = raw[offset : offset + CHUNK_SIZE]
        response = client.request(
            "PUT",
            upload_url,
            data=chunk,
            headers={
                "Content-Type": "application/octet-stream",
                "Content-Range": f"bytes {offset}-{offset + len(chunk) - 1}/{size}",
            },
            authenticated=False,
        )
        offset += len(chunk)
        if response.status_code in (200, 201):
            return json_body(response) or {}
    raise OneDriveAPIError(0, "the upload session ended without returning the uploaded file")


def _cancel_upload(client: OneDriveRestClient, upload_url: str) -> None:
    try:
        client.request("DELETE", upload_url, authenticated=False)
    except OneDriveAPIError:
        pass  # best effort: an abandoned session expires on its own


def _create_folder(client: OneDriveRestClient, args: dict[str, Any]) -> ToolResult:
    name = str(args["name"]).strip()
    _segment("name", name)
    drive = _drive(args)
    parent = _folder(args, "parent_folder", drive)
    body: dict[str, Any] = {
        "name": name,
        "folder": {},
        "@microsoft.graph.conflictBehavior": args.get("conflict_behavior") or "rename",
    }
    if description := _arg(args, "description"):
        body["description"] = description
    return _object_result(client.request("POST", f"{parent}/children", json=body))


def _parent_reference(value: Any, name: str) -> dict[str, str]:
    """``{"id": ..., ["driveId": ...]}`` from a caller's reference object."""
    if not isinstance(value, Mapping) or not isinstance(value.get("id"), str) or not value["id"].strip():
        raise _invalid(f"{name} must be an object with the destination folder's 'id'")
    reference = {"id": value["id"].strip()}
    drive_id = value.get("driveId")
    if isinstance(drive_id, str) and drive_id.strip():
        reference["driveId"] = drive_id.strip()
    return reference


def _move_item(client: OneDriveRestClient, args: dict[str, Any]) -> ToolResult:
    drive = _drive(args, drive="driveId", user="userId", site="siteId", group="groupId")
    item = f"{drive}/items/{_segment('itemId', args['itemId'])}"
    body: dict[str, Any] = {"parentReference": _parent_reference(args["parentReference"], "parentReference")}
    for key in ("name", "description"):
        if value := _arg(args, key):
            body[key] = value
    return _object_result(client.request("PATCH", item, json=body))


def _copy_item(client: OneDriveRestClient, args: dict[str, Any]) -> ToolResult:
    item = _item(args)
    name = _arg(args, "name")
    if args.get("children_only") and name:
        raise _invalid("name cannot be combined with children_only")
    body: dict[str, Any] = {}
    if args.get("parent_reference") is not None:
        body["parentReference"] = _parent_reference(args["parent_reference"], "parent_reference")
    if name:
        body["name"] = name
    if args.get("children_only"):
        body["childrenOnly"] = True
    if args.get("include_all_version_history"):
        body["includeAllVersionHistory"] = True
    conflict = args.get("conflict_behavior")
    params = {"@microsoft.graph.conflictBehavior": conflict} if conflict else None
    response = client.request("POST", f"{item}/copy", params=params, json=body)
    return _watch_copy(client, response.headers.get("Location"))


def _watch_copy(client: OneDriveRestClient, monitor: Any) -> ToolResult:
    """Follow Graph's async copy for up to ``COPY_MONITOR_WAIT`` seconds (and
    never past the call's budget). The monitor URL is pre-authenticated, is
    polled without the bearer token, and is not returned."""
    note = "Copy accepted and still running; check the destination with ONE_DRIVE_LIST_FOLDER_CHILDREN."
    result: dict[str, Any] = {"status": "inProgress", "message": note}
    if not isinstance(monitor, str) or not monitor.startswith("https://"):
        return ToolResult.ok(result)
    waits = iter(_COPY_POLL_WAITS)
    waited = 0.0
    while True:
        try:
            response = client.request("GET", monitor, authenticated=False, allow_redirects=False)
        except OneDriveAPIError:
            return ToolResult.ok(result)
        if response.status_code == 303:  # older monitors redirect to the new item
            return ToolResult.ok({"status": "completed", "percentageComplete": 100.0})
        try:
            body = response.json()
        except Exception:  # noqa: BLE001 — not JSON, or too deeply nested to parse
            body = None
        body = body if isinstance(body, dict) else {}
        status = body.get("status")
        if status == "completed":
            done: dict[str, Any] = {"status": "completed", "percentageComplete": 100.0}
            if body.get("resourceId"):
                done["resourceId"] = body["resourceId"]
            return ToolResult.ok(done)
        if status == "failed":
            error = body.get("error")
            error = error if isinstance(error, dict) else {}
            code = str(error.get("code") or "")
            message = str(error.get("message") or "Microsoft Graph reported the copy as failed")
            return ToolResult.fail(
                f"copy failed: {code + ': ' if code else ''}{message}"[:500],
                code=ERROR_CONFLICT if code == "nameAlreadyExists" else ERROR_UPSTREAM_ERROR,
            )
        if isinstance(body.get("percentageComplete"), (int, float)):
            result["percentageComplete"] = float(body["percentageComplete"])
        wait = next(waits, _COPY_POLL_WAITS[-1])
        remaining = client.remaining()
        if waited + wait > COPY_MONITOR_WAIT or (remaining is not None and wait >= remaining):
            return ToolResult.ok(result)
        client.sleep(wait)
        waited += wait


def _update_item(client: OneDriveRestClient, args: dict[str, Any]) -> ToolResult:
    body: dict[str, Any] = {}
    for key in ("name", "description"):
        if value := _arg(args, key):
            body[key] = value
    parent_id = _arg(args, "parent_reference_id")
    parent_drive = _arg(args, "parent_reference_drive_id")
    if parent_drive and not parent_id:
        raise _invalid("parent_reference_drive_id needs parent_reference_id")
    if parent_id:
        body["parentReference"] = {"id": parent_id, **({"driveId": parent_drive} if parent_drive else {})}
    if isinstance(args.get("file_system_info"), Mapping) and args["file_system_info"]:
        body["fileSystemInfo"] = dict(args["file_system_info"])
    if not body:
        raise _invalid(
            "nothing to update: pass name, description, parent_reference_id or file_system_info"
        )
    return _object_result(client.request("PATCH", _item(args), json=body, headers=_if_match(args)))


def _delete_item(client: OneDriveRestClient, args: dict[str, Any]) -> ToolResult:
    client.request("DELETE", _item(args), headers=_if_match(args))
    return ToolResult.ok({"deleted": True, "item_id": str(args["item_id"]).strip()})


def _create_link(client: OneDriveRestClient, args: dict[str, Any]) -> ToolResult:
    scope = args.get("scope")
    recipients = args.get("recipients") or []
    if recipients and scope in ("anonymous", "organization"):
        raise _invalid(f"recipients cannot be used with scope '{scope}'")
    if scope == "users" and not recipients:
        raise _invalid("scope 'users' needs a non-empty recipients list")
    if recipients and not scope:
        scope = "users"
    body: dict[str, Any] = {"type": args["type"]}
    if scope:
        body["scope"] = scope
    if recipients:
        body["recipients"] = recipients
    if password := args.get("password"):
        body["password"] = password
    if expires := _arg(args, "expiration_date_time"):
        body["expirationDateTime"] = expires
    if isinstance(args.get("retain_inherited_permissions"), bool):
        body["retainInheritedPermissions"] = args["retain_inherited_permissions"]
    return _object_result(client.request("POST", f"{_item(args)}/createLink", json=body))


# --------------------------------------------------------------------------
# Catalog
# --------------------------------------------------------------------------


#: Microsoft Graph delegated permissions. The read tools reach files shared
#: with the user and other drives too, so they need the ``.All`` forms.
FILES_READ_ALL = "Files.Read.All"
FILES_READWRITE_ALL = "Files.ReadWrite.All"
_GRAPH = "https://graph.microsoft.com/"
#: Graph's file permission hierarchy, by lower-cased short name: the broader
#: permissions that cover each one.
COVERING_SCOPES: dict[str, frozenset[str]] = {
    "files.read": frozenset({"files.read.all", "files.readwrite", "files.readwrite.all"}),
    "files.read.all": frozenset({"files.readwrite.all"}),
    "files.readwrite": frozenset({"files.readwrite.all"}),
}


def graph_scope(scope: str) -> str:
    """A Graph scope's short, lower-cased name: Microsoft's token responses
    may say ``https://graph.microsoft.com/Files.Read.All`` or
    ``files.read.all`` for ``Files.Read.All``."""
    text = scope.strip()
    if text.lower().startswith(_GRAPH):
        text = text[len(_GRAPH):]
    return text.lower()


def _read(slug: str, description: str, schema: dict[str, Any], handler: Handler, *tags: str) -> OneDriveTool:
    return OneDriveTool(
        ToolSpec(slug, description, schema, read_only=True, tags=tags, required_scopes=(FILES_READ_ALL,)),
        handler,
    )


def _write(
    slug: str,
    description: str,
    schema: dict[str, Any],
    handler: Handler,
    *tags: str,
    destructive: bool = False,
) -> OneDriveTool:
    return OneDriveTool(
        ToolSpec(
            slug,
            description,
            schema,
            read_only=False,
            destructive=destructive,
            tags=tags,
            required_scopes=(FILES_READWRITE_ALL,),
        ),
        handler,
    )


CATALOG: tuple[OneDriveTool, ...] = (
    # ---------------------------------------------------------------- find
    _read(
        "ONE_DRIVE_SEARCH_ITEMS",
        "Search the connected OneDrive for files and folders by keyword, matched against names, "
        "metadata and file content. q is plain keywords (no wildcards, no 'filetype:' syntax). "
        "Results mix files (with a file facet and mimeType) and folders (folder facet); each item "
        "has id, name, size, webUrl, lastModifiedDateTime and parentReference.path. One page per "
        "call. Use the returned id with ONE_DRIVE_DOWNLOAD_FILE or ONE_DRIVE_GET_ITEM. Newly "
        "added files can take a while to appear; browse with ONE_DRIVE_LIST_FOLDER_CHILDREN then.",
        _schema(
            {
                "q": {**_string("Keywords to search for, e.g. 'quarterly report'."), "minLength": 1},
                "top": TOP,
                "select": _string("Comma-separated driveItem properties to return, e.g. 'id,name,size,webUrl'."),
                "orderby": _string("Sort order, e.g. 'lastModifiedDateTime desc'."),
                "expand": _string("Comma-separated relationships to include, e.g. 'thumbnails'."),
                "search_scope": _string(
                    "'root' (default) searches the drive's own folders; 'drive' also includes "
                    "items shared with the user.",
                    enum=("root", "drive"),
                ),
                "drive_id": DRIVE_ID,
                "page_token": PAGE_TOKEN,
            },
            ["q"],
        ),
        _search_items,
        "search", "find", "file", "files", "document", "lookup", "keyword",
    ),
    _read(
        "ONE_DRIVE_DOWNLOAD_FILE",
        "Download a file from OneDrive by its item ID (a file, not a folder). The bytes are "
        "returned as an attached file; data has id, name, mimetype and size, plus the text as "
        "content for small UTF-8 files. format='pdf' converts Office and other documents to PDF "
        "during the download, format='html' converts Loop/Fluid files. Files over 100 MiB are "
        "refused. Get the item ID from ONE_DRIVE_SEARCH_ITEMS or ONE_DRIVE_LIST_FOLDER_CHILDREN.",
        _schema(
            {
                "item_id": ITEM_ID,
                "file_name": _string(
                    "Name (with extension) to give the downloaded file; defaults to the item's own name."
                ),
                "format": _string(
                    "Convert while downloading: 'pdf' or 'html'. Omit for the original bytes.",
                    enum=("pdf", "html"),
                ),
                **DRIVE_ARGS,
            },
            ["item_id"],
        ),
        _download_file,
        "download", "read", "open", "content", "file", "fetch", "get", "pdf",
    ),
    _read(
        "ONE_DRIVE_GET_ITEM",
        "Get a file's or folder's metadata by item ID: name, size, mimeType (files), childCount "
        "(folders), webUrl, created/modified time and user, parentReference (driveId, path). "
        "expand_relations=['children'] also lists a folder's first children. To read the file's "
        "content use ONE_DRIVE_DOWNLOAD_FILE.",
        _schema(
            {
                "item_id": ITEM_ID,
                "select_fields": _strings("DriveItem properties to return, e.g. ['id', 'name', 'size']."),
                "expand_relations": _strings(
                    "Relationships to include: children, thumbnails, versions, permissions, listItem."
                ),
                **LOCATION_ARGS,
            },
            ["item_id"],
        ),
        _get_item,
        "metadata", "details", "info", "file", "folder", "item", "properties",
    ),
    _read(
        "ONE_DRIVE_GET_ITEM_BY_PATH",
        "Get a file's or folder's metadata by its path in the drive, e.g. 'Documents/Plans/q3.pdf' "
        "— useful when the user names a location rather than an ID. Returns the same fields as "
        "ONE_DRIVE_GET_ITEM, including the item's id.",
        _schema(
            {
                "item_path": {
                    **_string("Path from the drive root, e.g. 'Documents/Plans/q3.pdf'."),
                    "minLength": 1,
                },
                "select_fields": _strings("DriveItem properties to return."),
                "expand_relations": _strings("Relationships to include, such as children or permissions."),
                "drive_id": DRIVE_ID,
            },
            ["item_path"],
        ),
        _get_item_by_path,
        "path", "metadata", "file", "folder", "item", "location",
    ),
    _read(
        "ONE_DRIVE_LIST_FOLDER_CHILDREN",
        "List the files and folders directly inside a folder, one page at a time — the drive's "
        "root when no folder is given. Name the folder by folder_item_id or by folder_path "
        "('/Documents/Plans'). Each item has id, name, size, webUrl, lastModifiedDateTime and "
        "a file (with mimeType) or folder (with childCount) facet. Not recursive: list a "
        "subfolder by its id to go deeper.",
        _schema(
            {
                "folder_item_id": _string("ID of the folder to list."),
                "folder_path": _string("Path of the folder from the drive root, e.g. '/Documents'; '/' is the root."),
                "top": TOP,
                "select": _strings("DriveItem properties to return, e.g. ['id', 'name', 'size']."),
                "expand": _strings("Relationships to include, e.g. ['thumbnails']."),
                "orderby": _string("Sort order, e.g. 'name' or 'lastModifiedDateTime desc'."),
                "drive_id": DRIVE_ID,
                "page_token": PAGE_TOKEN,
            },
        ),
        _list_folder_children,
        "list", "browse", "folder", "files", "contents", "children", "directory",
    ),
    _read(
        "ONE_DRIVE_GET_DRIVE",
        "Get a drive's details: owner, driveType (personal, business, documentLibrary), webUrl "
        "and quota (total, used, remaining, deleted bytes and state). Defaults to the connected "
        "user's own OneDrive. Use for 'how much space do I have'.",
        _schema(
            {
                "drive_id": _string("ID of the drive; omit for the connected user's own OneDrive."),
                "select_fields": _strings("Drive properties to return, e.g. ['id', 'quota']."),
                "expand_fields": _strings("Relationships to include, e.g. ['root']."),
            },
        ),
        _get_drive,
        "drive", "quota", "storage", "space", "usage", "account",
    ),
    _read(
        "ONE_DRIVE_GET_RECENT_ITEMS",
        "List files the user recently opened or edited, most recent first (activity, not "
        "modification time). Microsoft is retiring the underlying endpoint; when it returns "
        "nothing, fall back to ONE_DRIVE_SEARCH_ITEMS with orderby='lastModifiedDateTime desc'.",
        _schema(
            {
                "top": {"type": "integer", "minimum": 1, "maximum": _MAX_TOP, "description": "Maximum items to return."},
                "select": _string("Comma-separated driveItem properties to return."),
                "page_token": PAGE_TOKEN,
            },
        ),
        _get_recent_items,
        "recent", "recently", "latest", "opened", "edited", "worked",
    ),
    _read(
        "ONE_DRIVE_GET_SHARED_ITEMS",
        "List files and folders other people have shared with the connected user. Items are "
        "references (remoteItem) into the owner's drive: pass remoteItem.parentReference.driveId "
        "as drive_id and remoteItem.id as item_id to other tools. Microsoft is retiring this "
        "endpoint; ONE_DRIVE_SEARCH_ITEMS with search_scope='drive' also finds shared files.",
        _schema(
            {
                "allow_external": _boolean("Also include items shared from other organizations."),
                "select": _string("Comma-separated driveItem properties to return."),
                "page_token": PAGE_TOKEN,
            },
        ),
        _get_shared_items,
        "shared", "share", "others", "received", "collaboration",
    ),
    _read(
        "ONE_DRIVE_GET_ITEM_PERMISSIONS",
        "List who can access a file or folder: each permission has roles (read, write, owner), "
        "the user or group it is granted to, sharing links (link.type, link.scope, webUrl) and "
        "whether it is inherited from a parent folder.",
        _schema(
            {
                "item_id": ITEM_ID,
                "select": _string("Comma-separated permission properties, e.g. 'id,roles,link'."),
                **LOCATION_ARGS,
                "page_token": PAGE_TOKEN,
            },
            ["item_id"],
        ),
        _get_item_permissions,
        "permissions", "access", "sharing", "shared", "who",
    ),
    # --------------------------------------------------------------- write
    _write(
        "ONE_DRIVE_ONEDRIVE_UPLOAD_FILE",
        "Upload a file to OneDrive: file_name plus content (text, or base64 with "
        "encoding='base64'), into folder (a path such as '/Documents/Notes' or a folder ID; "
        "the root by default). Files over 4 MB go through an upload session automatically. On a "
        "name clash the new file gets a numbered name unless conflict_behavior says otherwise; "
        "'replace' overwrites the existing file. Returns the new item (id, name, webUrl).",
        _schema(
            {
                "file_name": {**_string("Name of the new file with its extension, e.g. 'notes.txt'."), "minLength": 1},
                "content": _string("The complete file contents."),
                "encoding": _string(
                    "How content is encoded: utf-8 text (default) or base64 bytes.",
                    enum=("utf-8", "base64"),
                ),
                "folder": _string("Destination folder path from the root (e.g. '/Documents/Notes') or folder ID."),
                "conflict_behavior": _conflict("rename"),
                "description": _string("A description for the file (OneDrive personal only)."),
                **LOCATION_ARGS,
            },
            ["file_name", "content"],
        ),
        _upload_file,
        "upload", "save", "write", "create", "file", "put", "store",
        destructive=True,
    ),
    _write(
        "ONE_DRIVE_ONEDRIVE_CREATE_FOLDER",
        "Create a folder in OneDrive, inside parent_folder (a path such as '/Projects' or a folder "
        "ID; the root by default). If the name is taken the new folder gets a numbered name "
        "unless conflict_behavior='fail'. Returns the new folder (id, name, webUrl).",
        _schema(
            {
                "name": {**_string("Name of the new folder."), "minLength": 1},
                "parent_folder": _string("Parent folder path from the root (e.g. '/Projects/Alpha') or folder ID."),
                "conflict_behavior": _conflict("rename", ("fail", "rename")),
                "description": _string("A description for the folder."),
                "user_id": USER_ID,
                "drive_id": DRIVE_ID,
            },
            ["name"],
        ),
        _create_folder,
        "create", "new", "folder", "directory", "mkdir", "organize",
    ),
    _write(
        "ONE_DRIVE_MOVE_ITEM",
        "Move a file or folder into another folder, optionally renaming it. parentReference "
        "names the destination folder by ID ({'id': '<folder id>'}); folder names and paths are "
        "not accepted, so look the ID up first. Fails with nameAlreadyExists if the destination "
        "already has an item with that name.",
        _schema(
            {
                "itemId": {**_string("ID of the file or folder to move."), "minLength": 1},
                "parentReference": _object(
                    "Destination folder: {'id': '<folder id>'} plus 'driveId' when moving to another drive.",
                    {"id": _string("Destination folder ID."), "driveId": _string("Destination drive ID.")},
                ),
                "name": _string("New name for the item; keeps its name when omitted."),
                "description": _string("New description for the item."),
                "driveId": _string("ID of the drive holding the item; omit for the user's own OneDrive."),
                "userId": _string("User whose drive holds the item; omit for the connected user."),
                "siteId": _string("SharePoint site whose library holds the item."),
                "groupId": _string("Group whose library holds the item."),
            },
            ["itemId", "parentReference"],
        ),
        _move_item,
        "move", "relocate", "organize", "folder", "file",
    ),
    _write(
        "ONE_DRIVE_COPY_ITEM",
        "Copy a file or folder, optionally to another folder (parent_reference {'id': ...}) "
        "and/or under a new name. Graph copies asynchronously: the result's status is "
        "'completed' (with resourceId, the new item's ID) or 'inProgress' for large copies — "
        "check the destination later. conflict_behavior='replace' overwrites an existing item.",
        _schema(
            {
                "item_id": ITEM_ID,
                "parent_reference": _object(
                    "Destination folder: {'id': '<folder id>'} plus 'driveId' for another drive. "
                    "Omit to copy next to the original.",
                    {"id": _string("Destination folder ID."), "driveId": _string("Destination drive ID.")},
                ),
                "name": _string("Name for the copy; defaults to the original's name."),
                "conflict_behavior": _conflict("fail"),
                "children_only": _boolean("Copy only a folder's contents, not the folder itself (not with name)."),
                "include_all_version_history": _boolean("Also copy the file's version history."),
                **LOCATION_ARGS,
            },
            ["item_id"],
        ),
        _copy_item,
        "copy", "duplicate", "clone", "file", "folder",
        destructive=True,
    ),
    _write(
        "ONE_DRIVE_UPDATE_DRIVE_ITEM_METADATA",
        "Rename a file or folder, change its description, or move it (parent_reference_id). "
        "Pass only what changes. Names must not contain \\ / : * ? \" < > |. Fails with "
        "nameAlreadyExists if the folder already has an item with the new name.",
        _schema(
            {
                "item_id": ITEM_ID,
                "name": _string("New name for the item, with its extension for files."),
                "description": _string("New description."),
                "parent_reference_id": _string("ID of a new parent folder, to move the item."),
                "parent_reference_drive_id": _string("Drive of the new parent folder, for a move across drives."),
                "file_system_info": _object("Client file system timestamps (createdDateTime, lastModifiedDateTime)."),
                "if_match": IF_MATCH,
                **LOCATION_ARGS,
            },
            ["item_id"],
        ),
        _update_item,
        "rename", "update", "edit", "describe", "metadata", "name", "move",
    ),
    _write(
        "ONE_DRIVE_DELETE_ITEM",
        "Delete a file or folder (with everything in it) by item ID. It goes to the OneDrive "
        "recycle bin, where it can be restored. Returns deleted: true.",
        _schema(
            {
                "item_id": ITEM_ID,
                "if_match": IF_MATCH,
                **DRIVE_ARGS,
            },
            ["item_id"],
        ),
        _delete_item,
        "delete", "remove", "trash", "erase", "file", "folder",
        destructive=True,
    ),
    _write(
        "ONE_DRIVE_CREATE_LINK",
        "Create a sharing link for a file or folder. type: 'view' (read-only), 'edit' or 'embed'. "
        "scope: 'organization' (people in the user's organization), 'users' (only the given "
        "recipients) or 'anonymous' (anyone with the link) — choose 'anonymous' only when the "
        "user explicitly asks for it. Returns the permission with link.webUrl to share.",
        _schema(
            {
                "item_id": ITEM_ID,
                "type": _string("Kind of link.", enum=("view", "edit", "embed")),
                "scope": _string("Who the link works for.", enum=("anonymous", "organization", "users")),
                "recipients": {
                    "type": "array",
                    "items": {"type": "object"},
                    "description": "People for scope 'users', e.g. [{'email': 'ana@example.com'}].",
                },
                "password": _string("Password for the link (OneDrive personal only)."),
                "expiration_date_time": _string("When the link stops working, ISO 8601 UTC (YYYY-MM-DDTHH:MM:SSZ)."),
                "retain_inherited_permissions": _boolean(
                    "Keep inherited permissions (default true) when sharing for the first time."
                ),
                **LOCATION_ARGS,
            },
            ["item_id", "type"],
        ),
        _create_link,
        "share", "sharing", "link", "url", "invite", "access",
    ),
)

TOOL_SPECS: tuple[ToolSpec, ...] = tuple(tool.spec for tool in CATALOG)
HANDLERS: dict[str, Handler] = {tool.spec.slug: tool.handler for tool in CATALOG}
