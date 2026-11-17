"""The Google Drive toolkit's catalog: one :class:`ToolSpec` plus one handler per tool.

Slugs and argument names are Composio's Google Drive toolkit's, verbatim —
including its mixed casing (``fileId`` on some tools, ``file_id`` on others)
— so callers written against Composio (the BE's Content Vault import, saved
prompts) keep working. The other spelling of an argument is accepted too
(the BE sends ``file_id`` to tools whose schema says ``fileId``); see
:func:`apply_aliases`.

Result shapes (``ToolResult.data``):

* object endpoints return Drive's resource (``kind`` keys dropped);
* list endpoints return ``{"has_more", "next_page_token", "items": [...]}``
  — one page per call; pass ``next_page_token`` back as the tool's page-token
  argument (``pageToken``, or ``page_token`` for FIND_FOLDER) to continue.
  File searches add ``incomplete_search`` before ``items``.
* downloads put the bytes in ``ToolResult.files`` (one DataEnvelope:
  ``content_type`` = MIME, ``metadata["filename"]``) and describe the file in
  ``data`` (``id``, ``name``, ``mimetype``, ``size``, plus ``content`` for
  small UTF-8 text).

Every listing and search covers shared drives (``supportsAllDrives`` and
``includeItemsFromAllDrives`` default to true).
"""

from __future__ import annotations

import base64
import binascii
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

from aisquare_pipe_googledrive.formats import (
    DOCUMENT,
    EXPORT_MIME_TYPES,
    SPREADSHEET,
    TOOL_EXPORTS,
    exported_name,
    guess_mimetype,
    is_native,
)
from aisquare_pipe_googledrive.rest import (
    FOLDER_MIME,
    SHORTCUT_MIME,
    DownloadLimitError,
    GoogleDriveAPIError,
    GoogleDriveRestClient,
    escape_query,
    group_query,
    json_body,
    quote_id,
)

Handler = Callable[[GoogleDriveRestClient, dict[str, Any]], ToolResult]

_SCOPE_BASE = "https://www.googleapis.com/auth/"
#: Full Drive access: every write tool needs it (and it covers every read).
#: Google classes it as *restricted*: production apps need OAuth
#: verification plus a security assessment (see the README for the narrower
#: alternatives).
DRIVE_SCOPE = _SCOPE_BASE + "drive"
#: Read every file and its metadata: what the read tools need.
DRIVE_READONLY_SCOPE = _SCOPE_BASE + "drive.readonly"
#: Google's Drive scope hierarchy: the broader scopes that cover each one.
COVERING_SCOPES: dict[str, frozenset[str]] = {
    DRIVE_READONLY_SCOPE: frozenset({DRIVE_SCOPE}),
    _SCOPE_BASE + "drive.metadata.readonly": frozenset({DRIVE_SCOPE, DRIVE_READONLY_SCOPE, _SCOPE_BASE + "drive.metadata"}),
    _SCOPE_BASE + "drive.metadata": frozenset({DRIVE_SCOPE}),
    _SCOPE_BASE + "drive.file": frozenset({DRIVE_SCOPE}),
}

#: UTF-8 downloads up to this size also come back as text in ``data["content"]``.
INLINE_TEXT_MAX = 100_000
#: Largest file a download tool loads into memory.
DOWNLOAD_MAX_BYTES = 100 * 1024 * 1024

DEFAULT_PAGE_SIZE = 100
MAX_PAGE_SIZE = 1000
#: permissions.list, drives.list and comments.list cap pageSize at 100.
MAX_SMALL_PAGE_SIZE = 100

#: File fields every file-returning tool asks for (Composio's FIND_FILE set).
FILE_FIELDS = (
    "id,name,mimeType,size,modifiedTime,createdTime,parents,webViewLink,trashed,starred,driveId"
)
METADATA_FIELDS = (
    f"{FILE_FIELDS},description,owners(displayName,emailAddress),"
    "lastModifyingUser(displayName,emailAddress),shared,shortcutDetails(targetId,targetMimeType)"
)
_DOWNLOAD_FIELDS = "id,name,mimeType,size,modifiedTime,webViewLink,shortcutDetails(targetId,targetMimeType)"
FOLDER_FIELDS = "id,name,parents,modifiedTime,webViewLink,starred,driveId"
PERMISSION_FIELDS = "id,type,role,emailAddress,domain,displayName,expirationTime,deleted,pendingOwner"
DRIVE_FIELDS = "id,name,createdTime,hidden"

#: Text content becomes these Google types on upload (Drive converts on import).
_TEXT_IMPORTS = {DOCUMENT: "text/plain", SPREADSHEET: "text/csv"}

# A Drive query (vs. bare search text): a comparison or an operator in its
# Drive form (contains '…', has { … }, '…' in parents) — the bare words
# "has" / "contains" in search text do not count.
_QUERY_SYNTAX = re.compile(
    r"[=<>]|\bcontains\s*'|\bhas\s*\{|'\s*in\s+(parents|owners|writers|readers)\b",
    re.IGNORECASE,
)
_EMAIL = re.compile(r"^[^@\s'\"\\]+@[^@\s'\"\\]+\.[^@\s'\"\\]+$")
# Ids inside Drive/Docs URLs: /d/<id>, /folders/<id>, ?id=<id>.
_URL_ID = re.compile(r"/(?:d|folders)/([A-Za-z0-9_-]{10,})|[?&]id=([A-Za-z0-9_-]{10,})")
#: Arguments that hold one Drive id (a URL to the item is accepted too).
_ID_ARGUMENTS = frozenset(
    {
        "fileId",
        "file_id",
        "folder_id",
        "folderId",
        "parent_folder_id",
        "folder_to_upload_to",
        "driveId",
    }
)


@dataclass(frozen=True)
class GoogleDriveTool:
    spec: ToolSpec
    handler: Handler
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


def _integer(description: str, *, default: int | None = None, maximum: int = MAX_PAGE_SIZE) -> dict[str, Any]:
    schema: dict[str, Any] = {"type": "integer", "minimum": 1, "maximum": maximum, "description": description}
    if default is not None:
        schema["default"] = default
    return schema


def _boolean(description: str, *, default: bool | None = None) -> dict[str, Any]:
    schema: dict[str, Any] = {"type": "boolean", "description": description}
    if default is not None:
        schema["default"] = default
    return schema


FILE_ID = _id("The file's Drive id (the long id in its URL); a Drive/Docs URL is accepted too.")
FIELDS = _string(
    "Partial-response field selector, e.g. 'id,name,mimeType'; '*' returns every field. "
    "Leave unset for a compact default."
)
PAGE_TOKEN = _string("The previous result's next_page_token, to fetch the next page.")
SUPPORTS_ALL_DRIVES = _boolean("Include items in shared drives (default true).", default=True)
INCLUDE_LABELS = _string("Comma-separated label ids to include in the response.")
PERMISSIONS_FOR_VIEW = _string("Only 'published' is supported.", enum=("published",))
CORPORA = _string(
    "Which collections to search: allDrives (default: My Drive, shared-with-me and every "
    "shared drive), user, domain, or drive (with driveId).",
    enum=("user", "drive", "domain", "allDrives"),
    default="allDrives",
)
ROLE = _string(
    "Access to grant: reader, commenter, writer, fileOrganizer, organizer or owner.",
    enum=("owner", "organizer", "fileOrganizer", "writer", "commenter", "reader"),
)
GRANTEE_TYPE = _string(
    "Who gets access: user or group (with email_address), domain (with domain) or anyone "
    "(anyone with the link).",
    enum=("user", "group", "domain", "anyone"),
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
    (``file_id`` <-> ``fileId``). A key already present wins over its alias."""
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


def drive_id(value: str) -> str:
    """A Drive id from an id or a Drive/Docs URL (``.../d/<id>/edit``)."""
    text = value.strip()
    if text.startswith(("http://", "https://")):
        match = _URL_ID.search(text)
        if match:
            return match.group(1) or match.group(2)
    return text


def normalize_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    """Forgive the common slips: whitespace around ids, a URL where an id goes."""
    args = dict(arguments)
    for key in _ID_ARGUMENTS & args.keys():
        if isinstance(args[key], str):
            args[key] = drive_id(args[key])
    return args


def _invalid(message: str) -> ToolError:
    return ToolError(message, code=ERROR_INVALID_ARGUMENTS)


def _path_id(args: dict[str, Any], key: str) -> str:
    """``args[key]`` as one URL path segment; '.'/'..' (which the HTTP stack
    would resolve, rerouting the call) and empty ids are refused."""
    value = args.get(key)
    if not isinstance(value, str) or not value.strip():
        raise _invalid(f"{key} is required")
    try:
        return quote_id(value)
    except ValueError:
        raise _invalid(f"{key} must be a Drive id, not empty, '.' or '..'") from None


def _flag(value: Any) -> str:
    return "true" if value else "false"


def _params(args: dict[str, Any], names: Mapping[str, str]) -> dict[str, Any]:
    """Query parameters from arguments (``arg name -> Drive parameter``);
    booleans become ``true``/``false``."""
    params: dict[str, Any] = {}
    for arg, param in names.items():
        if arg in args and args[arg] is not None:
            value = args[arg]
            params[param] = _flag(value) if isinstance(value, bool) else value
    return params


def _page_size(value: Any, default: int | None, maximum: int) -> int | None:
    if value is None:
        return default
    return min(max(1, int(value)), maximum)


def _list_fields(fields: Any, collection: str, default: str, *, extra: str = "") -> str:
    """A list call's ``fields``: always carries ``nextPageToken``; bare field
    names are wrapped as ``<collection>(...)``."""
    prefix = "nextPageToken" + (f",{extra}" if extra else "")
    if not isinstance(fields, str) or not fields.strip():
        return f"{prefix},{collection}({default})"
    text = fields.strip()
    if text == "*" or "nextPageToken" in text:
        return text
    if f"{collection}(" in text:
        return f"nextPageToken,{text}"
    return f"{prefix},{collection}({text})"


def _and(*clauses: str) -> str:
    """Our own clauses joined; a caller's query goes through :func:`_scoped` first."""
    return " and ".join(c for c in clauses if c)


def _scoped(q: str) -> str:
    """A caller's ``q`` as one group, so ANDing a folder scope binds to all of it."""
    try:
        return group_query(q)
    except ValueError as e:
        raise _invalid(f"q cannot be limited to the folder: {e}") from None


def search_query(q: Any) -> str:
    """A Drive ``q``: a real query passes through; bare text becomes a
    full-text search and a bare email an owner search (both skip trash)."""
    text = q.strip() if isinstance(q, str) else ""
    if not text or _QUERY_SYNTAX.search(text):
        return text
    if _EMAIL.match(text):
        return f"'{escape_query(text)}' in owners and trashed = false"
    return f"fullText contains '{escape_query(text)}' and trashed = false"


def _drive_scope(args: dict[str, Any]) -> dict[str, Any]:
    """corpora / driveId / supportsAllDrives / includeItemsFromAllDrives,
    kept consistent (Drive rejects some combinations)."""
    drive = args.get("driveId")
    corpora = "drive" if drive else (args.get("corpora") or "allDrives")
    include_all = bool(args.get("includeItemsFromAllDrives", True)) or corpora in ("drive", "allDrives")
    params: dict[str, Any] = {
        "corpora": corpora,
        "supportsAllDrives": _flag(include_all or args.get("supportsAllDrives", True)),
        "includeItemsFromAllDrives": _flag(include_all),
    }
    if drive:
        params["driveId"] = drive
    return params


# --------------------------------------------------------------------------
# Response helpers
# --------------------------------------------------------------------------


def slim(value: Any) -> Any:
    """Drop Drive's ``kind`` markers (``drive#file``): noise for an LLM."""
    if isinstance(value, dict):
        return {key: slim(item) for key, item in value.items() if key != "kind"}
    if isinstance(value, list):
        return [slim(item) for item in value]
    return value


def _body(response: Any) -> dict[str, Any]:
    data = json_body(response)
    return slim(data) if isinstance(data, dict) else {}


def listing(data: dict[str, Any], collection: str) -> dict[str, Any]:
    """One page: pagination hints first (they survive a truncated result), then ``items``."""
    token = data.get("nextPageToken")
    out: dict[str, Any] = {"has_more": bool(token), "next_page_token": token or None}
    if "incompleteSearch" in data:
        out["incomplete_search"] = bool(data["incompleteSearch"])
    items = data.get(collection)
    out["items"] = items if isinstance(items, list) else []
    return out


def _utf8(content: bytes) -> str | None:
    try:
        return content.decode("utf-8")
    except UnicodeDecodeError:
        return None


# --------------------------------------------------------------------------
# Handlers: find and read
# --------------------------------------------------------------------------


def _list_call(client: GoogleDriveRestClient, args: dict[str, Any], q: str, *, folder_key: str) -> ToolResult:
    folder = args.get(folder_key)
    clauses = [q or "trashed = false"]
    if folder:
        if q:
            clauses = [_scoped(q)]
        clauses.append(f"'{escape_query(folder)}' in parents")
    params: dict[str, Any] = {
        "q": _and(*clauses),
        "fields": _list_fields(args.get("fields"), "files", FILE_FIELDS, extra="incompleteSearch"),
        "pageSize": _page_size(args.get("pageSize"), DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE),
        **_params(
            args,
            {
                "pageToken": "pageToken",
                "orderBy": "orderBy",
                "spaces": "spaces",
                "include_labels": "includeLabels",
                "includeLabels": "includeLabels",
                "include_permissions_for_view": "includePermissionsForView",
                "includePermissionsForView": "includePermissionsForView",
            },
        ),
        **_drive_scope(args),
    }
    if not params["q"]:
        del params["q"]
    data = _body(client.get("/files", params))
    return ToolResult.ok(listing(data, "files"))


def _find_file(client: GoogleDriveRestClient, args: dict[str, Any]) -> ToolResult:
    return _list_call(client, args, search_query(args.get("q")), folder_key="folder_id")


def _list_files(client: GoogleDriveRestClient, args: dict[str, Any]) -> ToolResult:
    q = args.get("q")
    return _list_call(client, args, q.strip() if isinstance(q, str) else "", folder_key="folderId")


def _find_folder(client: GoogleDriveRestClient, args: dict[str, Any]) -> ToolResult:
    clauses = [f"mimeType = '{FOLDER_MIME}'", "trashed = false"]
    text_filters = {
        "name_exact": "name = '{}'",
        "name_contains": "name contains '{}'",
        "name_not_contains": "not name contains '{}'",
        "full_text_contains": "fullText contains '{}'",
        "full_text_not_contains": "not fullText contains '{}'",
        "parent_folder_id": "'{}' in parents",
        "modified_after": "modifiedTime > '{}'",
    }
    for arg, template in text_filters.items():
        if args.get(arg):
            clauses.append(template.format(escape_query(str(args[arg]))))
    if "starred" in args:
        clauses.append(f"starred = {_flag(args['starred'])}")
    params = {
        "q": _and(*clauses),
        "fields": f"nextPageToken,incompleteSearch,files({FOLDER_FIELDS})",
        "pageSize": _page_size(args.get("page_size"), DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE),
        **_params(args, {"page_token": "pageToken"}),
        **_drive_scope({}),
    }
    data = _body(client.get("/files", params))
    return ToolResult.ok(listing(data, "files"))


def _get_file_metadata(client: GoogleDriveRestClient, args: dict[str, Any]) -> ToolResult:
    params = {
        "fields": args.get("fields") or METADATA_FIELDS,
        "supportsAllDrives": _flag(args.get("supportsAllDrives", True)),
        **_params(args, {"includeLabels": "includeLabels", "includePermissionsForView": "includePermissionsForView"}),
    }
    return ToolResult.ok(_body(client.get(f"/files/{_path_id(args, 'fileId')}", params)))


def _metadata(client: GoogleDriveRestClient, file_id: str, fields: str = _DOWNLOAD_FIELDS) -> dict[str, Any]:
    return _body(client.get(f"/files/{file_id}", {"fields": fields, "supportsAllDrives": "true"}))


def _download_target(client: GoogleDriveRestClient, args: dict[str, Any]) -> dict[str, Any]:
    """The file to download: shortcuts are followed once; folders refused."""
    meta = _metadata(client, _path_id(args, "fileId"))
    if meta.get("mimeType") == SHORTCUT_MIME:
        target = (meta.get("shortcutDetails") or {}).get("targetId")
        if not isinstance(target, str) or not target:
            raise _invalid("the file is a shortcut without a target")
        meta = _metadata(client, _path_id({"targetId": target}, "targetId"))
    if meta.get("mimeType") == FOLDER_MIME:
        raise _invalid("the file is a folder; list its contents with GOOGLEDRIVE_FIND_FILE (folder_id)")
    return meta


def _file_result(
    meta: dict[str, Any], content: bytes, *, export_mime: str | None
) -> ToolResult:
    source_mime = str(meta.get("mimeType") or "")
    name = str(meta.get("name") or meta.get("id") or "download")
    if export_mime:
        name = exported_name(name, export_mime)
        mimetype = export_mime
    else:
        mimetype = source_mime or guess_mimetype(name)
    data: dict[str, Any] = {
        "id": meta.get("id"),
        "name": name,
        "mimetype": mimetype,
        "size": len(content),
        "exported": bool(export_mime),
    }
    if export_mime:
        data["original_mimetype"] = source_mime
    for key in ("modifiedTime", "webViewLink"):
        if meta.get(key):
            data[key] = meta[key]
    text = _utf8(content) if len(content) <= INLINE_TEXT_MAX and _textual(mimetype) else None
    if text is not None:
        data["content"] = text
    envelope = DataEnvelope(
        content_type=mimetype,
        data=content,
        source_id=f"googledrive:{meta.get('id')}",
        metadata={
            "filename": name,
            "file_id": meta.get("id"),
            "mime_type": mimetype,
            "source_mime_type": source_mime,
            "modified_time": meta.get("modifiedTime"),
            "web_view_link": meta.get("webViewLink"),
        },
    )
    return ToolResult.ok(data, files=[envelope])


def _textual(mimetype: str) -> bool:
    return mimetype.startswith("text/") or mimetype in ("application/json", "application/xml") or mimetype.endswith(
        ("+json", "+xml")
    )


def _fetch(client: GoogleDriveRestClient, meta: dict[str, Any], export_mime: str | None) -> bytes:
    size = meta.get("size")
    if not export_mime and str(size or "").isdigit() and int(str(size)) > DOWNLOAD_MAX_BYTES:
        raise _invalid(
            f"the file is {int(str(size)):,} bytes, over the {DOWNLOAD_MAX_BYTES:,}-byte download limit"
        )
    file_id = str(meta.get("id") or "")
    try:
        return client.download_bytes(file_id, export_mime=export_mime, max_bytes=DOWNLOAD_MAX_BYTES)
    except DownloadLimitError as e:
        raise _invalid(e.message) from None


def _download_file(client: GoogleDriveRestClient, args: dict[str, Any]) -> ToolResult:
    meta = _download_target(client, args)
    source_mime = str(meta.get("mimeType") or "")
    export_mime: str | None = None
    if is_native(source_mime):
        export_mime = args.get("mime_type") or TOOL_EXPORTS.get(source_mime)
        if not export_mime:
            raise _invalid(f"Google Drive cannot export {source_mime} files")
    return _file_result(meta, _fetch(client, meta, export_mime), export_mime=export_mime)


def _export_google_workspace_file(client: GoogleDriveRestClient, args: dict[str, Any]) -> ToolResult:
    meta = _download_target(client, args)
    source_mime = str(meta.get("mimeType") or "")
    if not is_native(source_mime):
        raise _invalid(
            f"the file is a stored {source_mime or 'file'}, not a Google Docs/Sheets/Slides file — "
            "download it with GOOGLEDRIVE_DOWNLOAD_FILE"
        )
    return _file_result(meta, _fetch(client, meta, args["mimeType"]), export_mime=args["mimeType"])


def _get_about(client: GoogleDriveRestClient, args: dict[str, Any]) -> ToolResult:
    fields = args.get("fields") or "user,storageQuota,maxUploadSize,canCreateDrives,appInstalled"
    return ToolResult.ok(_body(client.get("/about", {"fields": fields})))


def _list_shared_drives(client: GoogleDriveRestClient, args: dict[str, Any]) -> ToolResult:
    params = {
        "fields": _list_fields(args.get("fields"), "drives", DRIVE_FIELDS),
        "pageSize": _page_size(args.get("pageSize"), MAX_SMALL_PAGE_SIZE, MAX_SMALL_PAGE_SIZE),
        **_params(args, {"q": "q", "pageToken": "pageToken", "useDomainAdminAccess": "useDomainAdminAccess"}),
    }
    return ToolResult.ok(listing(_body(client.get("/drives", params)), "drives"))


def _list_permissions(client: GoogleDriveRestClient, args: dict[str, Any]) -> ToolResult:
    params: dict[str, Any] = {
        "fields": _list_fields(args.get("fields"), "permissions", PERMISSION_FIELDS),
        "supportsAllDrives": _flag(args.get("supportsAllDrives", True)),
        **_params(
            args,
            {
                "pageToken": "pageToken",
                "useDomainAdminAccess": "useDomainAdminAccess",
                "includePermissionsForView": "includePermissionsForView",
            },
        ),
    }
    size = _page_size(args.get("pageSize"), None, MAX_SMALL_PAGE_SIZE)
    if size is not None:
        params["pageSize"] = size
    response = client.get(f"/files/{_path_id(args, 'fileId')}/permissions", params)
    return ToolResult.ok(listing(_body(response), "permissions"))


def _list_comments(client: GoogleDriveRestClient, args: dict[str, Any]) -> ToolResult:
    fields = args.get("fields") or "*"
    if fields != "*" and "nextPageToken" not in fields:
        fields = _list_fields(fields, "comments", "")
    params = {
        "fields": fields,
        "pageSize": _page_size(args.get("pageSize"), 20, MAX_SMALL_PAGE_SIZE),
        "includeDeleted": _flag(args.get("includeDeleted", False)),
        **_params(args, {"pageToken": "pageToken", "startModifiedTime": "startModifiedTime"}),
    }
    response = client.get(f"/files/{_path_id(args, 'fileId')}/comments", params)
    return ToolResult.ok(listing(_body(response), "comments"))


# --------------------------------------------------------------------------
# Handlers: create, change, share
# --------------------------------------------------------------------------


def _resolve_folder(client: GoogleDriveRestClient, value: str) -> str:
    """A folder id from an id or the exact name of an existing folder."""
    if value == "root":
        return value
    try:
        meta = _metadata(client, _path_id({"id": value}, "id"), "id,mimeType")
    except GoogleDriveAPIError as e:
        if e.status not in (400, 404):
            raise
        meta = {}
    if meta:
        if meta.get("mimeType") != FOLDER_MIME:
            raise _invalid("parent_id names a file, not a folder")
        return str(meta["id"])
    query = f"mimeType = '{FOLDER_MIME}' and trashed = false and name = '{escape_query(value)}'"
    params = {"q": query, "fields": "files(id,name)", "pageSize": 10, **_drive_scope({})}
    found = _body(client.get("/files", params)).get("files") or []
    if not found:
        raise _invalid("parent_id matches no folder id or folder name")
    if len(found) > 1:
        ids = ", ".join(str(f.get("id")) for f in found[:5])
        raise _invalid(f"several folders are named like parent_id; pass one id: {ids}")
    return str(found[0]["id"])


def _create_folder(client: GoogleDriveRestClient, args: dict[str, Any]) -> ToolResult:
    body: dict[str, Any] = {"name": args["name"], "mimeType": FOLDER_MIME}
    parent = args.get("parent_id")
    if isinstance(parent, str) and parent.strip():
        body["parents"] = [_resolve_folder(client, drive_id(parent))]
    response = client.request(
        "POST", "/files", params={"supportsAllDrives": "true", "fields": FILE_FIELDS}, json=body
    )
    return ToolResult.ok(_body(response))


def _upload(
    client: GoogleDriveRestClient,
    content: bytes,
    *,
    metadata: dict[str, Any],
    content_type: str,
    file_id: str | None = None,
) -> ToolResult:
    created = client.upload(
        content,
        metadata=metadata,
        content_type=content_type,
        file_id=file_id,
        params={"fields": FILE_FIELDS},
    )
    return ToolResult.ok(slim(created))


def _parents(args: dict[str, Any], key: str) -> list[str]:
    value = args.get(key)
    if not isinstance(value, str) or not value.strip():
        return []
    _path_id({key: value}, key)  # refuses '.' / '..'
    return [value.strip()]


def _upload_file(client: GoogleDriveRestClient, args: dict[str, Any]) -> ToolResult:
    spec = args["file_to_upload"]
    try:
        content = base64.b64decode(str(spec["file_content_bytes"]), validate=True)
    except (binascii.Error, ValueError):
        raise _invalid("file_to_upload.file_content_bytes is not valid base64") from None
    name = str(spec["name"])
    metadata: dict[str, Any] = {"name": name}
    parents = _parents(args, "folder_to_upload_to")
    if parents:
        metadata["parents"] = parents
    content_type = spec.get("mimetype") or guess_mimetype(name)
    return _upload(client, content, metadata=metadata, content_type=content_type)


def _create_file_from_text(client: GoogleDriveRestClient, args: dict[str, Any]) -> ToolResult:
    mime_type = args.get("mime_type") or "text/plain"
    metadata: dict[str, Any] = {"name": args["file_name"]}
    content_type = mime_type
    if is_native(mime_type):
        content_type = _TEXT_IMPORTS.get(mime_type, "")
        if not content_type:
            raise _invalid(
                "text can become a Google Doc (application/vnd.google-apps.document) or Sheet "
                "(application/vnd.google-apps.spreadsheet, from CSV), not " + mime_type
            )
        metadata["mimeType"] = mime_type
    parents = _parents(args, "parent_id")
    if parents:
        metadata["parents"] = [drive_id(parents[0])]
    return _upload(
        client, str(args["text_content"]).encode("utf-8"), metadata=metadata, content_type=content_type
    )


def _edit_file(client: GoogleDriveRestClient, args: dict[str, Any]) -> ToolResult:
    mime_type = args.get("mime_type") or "text/plain"
    if is_native(mime_type):
        raise _invalid("mime_type must be a stored file type; Google Docs/Sheets/Slides cannot be overwritten")
    return _upload(
        client,
        str(args["content"]).encode("utf-8"),
        metadata={},
        content_type=mime_type,
        file_id=_path_id(args, "file_id"),
    )


def _copy_file(client: GoogleDriveRestClient, args: dict[str, Any]) -> ToolResult:
    body = {"name": args["new_title"]} if args.get("new_title") else {}
    response = client.request(
        "POST",
        f"/files/{_path_id(args, 'file_id')}/copy",
        params={"supportsAllDrives": "true", "fields": FILE_FIELDS},
        json=body,
    )
    return ToolResult.ok(_body(response))


_MOVE_PARAMS = {
    "add_parents": "addParents",
    "remove_parents": "removeParents",
    "ocr_language": "ocrLanguage",
    "include_labels": "includeLabels",
    "keep_revision_forever": "keepRevisionForever",
    "include_permissions_for_view": "includePermissionsForView",
    "use_content_as_indexable_text": "useContentAsIndexableText",
}


def _move_file(client: GoogleDriveRestClient, args: dict[str, Any]) -> ToolResult:
    file_id = _path_id(args, "file_id")
    if not args.get("add_parents") and not args.get("remove_parents"):
        raise _invalid("pass add_parents (destination folder id) and/or remove_parents")
    params = {
        **_params(args, _MOVE_PARAMS),
        "supportsAllDrives": _flag(args.get("supports_all_drives", True)),
        "fields": FILE_FIELDS,
    }
    if args.get("add_parents") and not args.get("remove_parents"):
        # Drive items have one parent: a move must drop the current one(s).
        current = _metadata(client, file_id, "parents").get("parents") or []
        target = {p.strip() for p in str(args["add_parents"]).split(",")}
        stale = [p for p in current if p not in target]
        if stale:
            params["removeParents"] = ",".join(stale)
    response = client.request("PATCH", f"/files/{file_id}", params=params, json={})
    return ToolResult.ok(_body(response))


def _update_file_put(client: GoogleDriveRestClient, args: dict[str, Any]) -> ToolResult:
    body: dict[str, Any] = {}
    for arg, key in (
        ("name", "name"),
        ("description", "description"),
        ("starred", "starred"),
        ("mime_type", "mimeType"),
        ("writers_can_share", "writersCanShare"),
    ):
        if arg in args:
            body[key] = args[arg]
    if "viewers_can_copy_content" in args:
        body["copyRequiresWriterPermission"] = not args["viewers_can_copy_content"]
    params = {
        **_params(
            args,
            {
                "add_parents": "addParents",
                "remove_parents": "removeParents",
                "ocr_language": "ocrLanguage",
                "keep_revision_forever": "keepRevisionForever",
            },
        ),
        "supportsAllDrives": _flag(args.get("supports_all_drives", True)),
        "fields": METADATA_FIELDS,
    }
    if not body and "addParents" not in params and "removeParents" not in params:
        raise _invalid("nothing to update: pass name, description, starred or another field")
    response = client.request("PATCH", f"/files/{_path_id(args, 'fileId')}", params=params, json=body)
    return ToolResult.ok(_body(response))


def _set_trashed(trashed: bool, id_key: str) -> Handler:
    def handler(client: GoogleDriveRestClient, args: dict[str, Any]) -> ToolResult:
        params = {
            "supportsAllDrives": _flag(args.get("supportsAllDrives", True)),
            "fields": args.get("fields") or "id,name,mimeType,trashed,parents,webViewLink",
        }
        response = client.request(
            "PATCH", f"/files/{_path_id(args, id_key)}", params=params, json={"trashed": trashed}
        )
        return ToolResult.ok(_body(response))

    return handler


_PERMISSION_PARAMS = {
    "transfer_ownership": "transferOwnership",
    "send_notification_email": "sendNotificationEmail",
    "email_message": "emailMessage",
    "move_to_new_owners_root": "moveToNewOwnersRoot",
    "use_domain_admin_access": "useDomainAdminAccess",
}


def _create_permission(client: GoogleDriveRestClient, args: dict[str, Any]) -> ToolResult:
    grantee = args["type"]
    if grantee in ("user", "group") and not args.get("email_address"):
        raise _invalid(f"email_address is required when type is {grantee}")
    if grantee == "domain" and not args.get("domain"):
        raise _invalid("domain is required when type is domain")
    if args["role"] == "owner" and not args.get("transfer_ownership"):
        raise _invalid("role 'owner' transfers ownership: set transfer_ownership to true")
    body: dict[str, Any] = {"role": args["role"], "type": grantee}
    for arg, key in (
        ("email_address", "emailAddress"),
        ("domain", "domain"),
        ("expiration_time", "expirationTime"),
        ("allow_file_discovery", "allowFileDiscovery"),
    ):
        if args.get(arg) is not None:
            body[key] = args[arg]
    params = {
        **_params(args, _PERMISSION_PARAMS),
        "supportsAllDrives": _flag(args.get("supports_all_drives", True)),
        "fields": PERMISSION_FIELDS,
    }
    response = client.request(
        "POST", f"/files/{_path_id(args, 'file_id')}/permissions", params=params, json=body
    )
    return ToolResult.ok(_body(response))


def _create_comment(client: GoogleDriveRestClient, args: dict[str, Any]) -> ToolResult:
    body: dict[str, Any] = {"content": args["content"]}
    if args.get("anchor"):
        body["anchor"] = args["anchor"]
    if args.get("quoted_file_content_value"):
        quoted = {"value": args["quoted_file_content_value"]}
        if args.get("quoted_file_content_mime_type"):
            quoted["mimeType"] = args["quoted_file_content_mime_type"]
        body["quotedFileContent"] = quoted
    response = client.request(
        "POST", f"/files/{_path_id(args, 'file_id')}/comments", params={"fields": "*"}, json=body
    )
    return ToolResult.ok(_body(response))


# --------------------------------------------------------------------------
# Catalog
# --------------------------------------------------------------------------


def _read(
    slug: str,
    description: str,
    schema: dict[str, Any],
    handler: Handler,
    *tags: str,
    aliases: Mapping[str, str] | None = None,
) -> GoogleDriveTool:
    spec = ToolSpec(slug, description, schema, read_only=True, tags=tags, required_scopes=(DRIVE_READONLY_SCOPE,))
    return GoogleDriveTool(spec, handler, aliases or {})


def _write(
    slug: str,
    description: str,
    schema: dict[str, Any],
    handler: Handler,
    *tags: str,
    destructive: bool = False,
    aliases: Mapping[str, str] | None = None,
) -> GoogleDriveTool:
    return GoogleDriveTool(
        ToolSpec(
            slug,
            description,
            schema,
            read_only=False,
            destructive=destructive,
            tags=tags,
            required_scopes=(DRIVE_SCOPE,),
        ),
        handler,
        aliases or {},
    )


_LISTING_ARGS: dict[str, Any] = {
    "fields": _string(
        "Fields per file, e.g. 'id,name,mimeType,size' (wrapped as files(...)), or '*'. "
        "Defaults to id, name, mimeType, size, modifiedTime, createdTime, parents, "
        "webViewLink, trashed, starred, driveId."
    ),
    "spaces": _string("drive (default) or appDataFolder.", enum=("drive", "appDataFolder", "photos")),
    "corpora": CORPORA,
    "driveId": _id("Search only this shared drive (sets corpora to drive)."),
    "orderBy": _string(
        "Comma-separated sort keys, e.g. 'modifiedTime desc' or 'folder,name'. Not allowed with "
        "fullText searches."
    ),
    "pageSize": _integer("Files per page (1-1000).", default=DEFAULT_PAGE_SIZE),
    "pageToken": PAGE_TOKEN,
    "supportsAllDrives": SUPPORTS_ALL_DRIVES,
    "includeItemsFromAllDrives": _boolean("Include shared-drive items in the results (default true).", default=True),
}

_DOWNLOAD_MIME_TYPES = (*EXPORT_MIME_TYPES, "text/html")

CATALOG: tuple[GoogleDriveTool, ...] = (
    # ------------------------------------------------------------ find / list
    _read(
        "GOOGLEDRIVE_FIND_FILE",
        "Search Google Drive (My Drive, shared with me and shared drives) for files and folders. "
        "q takes Drive query syntax, e.g. \"name contains 'roadmap' and trashed = false\", "
        "\"mimeType = 'application/pdf'\", \"modifiedTime > '2026-01-01T00:00:00'\", "
        "\"fullText contains 'budget'\"; plain words become a full-text search and a bare email "
        "an owner search. folder_id limits to one folder's direct children. Returns one page: "
        "has_more, next_page_token, items[] with id, name, mimeType, size, modifiedTime, "
        "webViewLink and parents. Pass next_page_token as pageToken for more.",
        _schema(
            {
                "q": _string("Drive search query, or plain words for a full-text search."),
                "folder_id": _id("Only direct children of this folder ('root' = top of My Drive)."),
                **_LISTING_ARGS,
                "include_labels": INCLUDE_LABELS,
                "include_permissions_for_view": PERMISSIONS_FOR_VIEW,
            }
        ),
        _find_file,
        "search", "find", "files", "documents", "query", "lookup", "locate",
    ),
    _read(
        "GOOGLEDRIVE_LIST_FILES",
        "List files and folders in Google Drive, optionally inside one folder (folderId) or "
        "filtered by a Drive query q, one page at a time (trashed items are hidden unless q says "
        "otherwise). Prefer GOOGLEDRIVE_FIND_FILE for searches by name or content. Returns "
        "has_more, next_page_token and items[]; pass next_page_token as pageToken for more.",
        _schema(
            {
                "q": _string("Drive query, e.g. \"mimeType = 'application/pdf'\"."),
                "folderId": _id("List this folder's direct children ('root' = top of My Drive)."),
                **_LISTING_ARGS,
                "includeLabels": INCLUDE_LABELS,
                "includePermissionsForView": PERMISSIONS_FOR_VIEW,
            }
        ),
        _list_files,
        "list", "files", "folder", "contents", "browse", "children",
    ),
    _read(
        "GOOGLEDRIVE_FIND_FOLDER",
        "Find Google Drive folders by name (exact or contains), parent folder, star or "
        "modification date. Returns one page: has_more, next_page_token, items[] with id, name, "
        "parents, modifiedTime and webViewLink. Pass next_page_token as page_token for more. "
        "Use the id as folder_id / parent_id in other tools.",
        _schema(
            {
                "name_exact": _string("Exact folder name (case-sensitive)."),
                "name_contains": _string("Text the folder name contains (case-insensitive)."),
                "name_not_contains": _string("Text the folder name must not contain."),
                "full_text_contains": _string("Text in the folder's name or description."),
                "full_text_not_contains": _string("Text not in the folder's name or description."),
                "parent_folder_id": _id("Only folders directly inside this folder."),
                "modified_after": _string("Only folders modified after this RFC 3339 time."),
                "starred": _boolean("Only starred (true) or unstarred (false) folders."),
                "page_size": _integer("Folders per page (1-1000).", default=DEFAULT_PAGE_SIZE),
                "page_token": PAGE_TOKEN,
            }
        ),
        _find_folder,
        "folder", "directory", "find", "search", "locate",
    ),
    _read(
        "GOOGLEDRIVE_GET_FILE_METADATA",
        "Get one Google Drive file's or folder's metadata by id — name, mimeType, size, "
        "created/modified times, parents, webViewLink, owners, description, sharing state, "
        "starred, trashed — without downloading its content. fields selects other fields ('*' = all).",
        _schema(
            {
                "fileId": FILE_ID,
                "fields": FIELDS,
                "supportsAllDrives": SUPPORTS_ALL_DRIVES,
                "includeLabels": INCLUDE_LABELS,
                "includePermissionsForView": PERMISSIONS_FOR_VIEW,
            },
            ["fileId"],
        ),
        _get_file_metadata,
        "metadata", "details", "info", "properties", "file",
    ),
    _read(
        "GOOGLEDRIVE_DOWNLOAD_FILE",
        "Download a Google Drive file's content (returned as a file, with name, mimetype and "
        "size; small text files also inline as content). Stored files come back as-is. Google "
        "Docs/Slides/Drawings are exported as PDF and Sheets as XLSX unless mime_type picks "
        "another format (e.g. text/plain, text/markdown, docx, text/csv). Shortcuts are followed; "
        "folders are refused. Limit 100 MB (exports 10 MB).",
        _schema(
            {
                "fileId": FILE_ID,
                "mime_type": _string(
                    "Export format for Google Docs/Sheets/Slides; ignored for stored files.",
                    enum=_DOWNLOAD_MIME_TYPES,
                ),
            },
            ["fileId"],
        ),
        _download_file,
        "download", "read", "content", "fetch", "open", "get", "file", "pdf",
    ),
    _read(
        "GOOGLEDRIVE_EXPORT_GOOGLE_WORKSPACE_FILE",
        "Export a Google Docs, Sheets, Slides, Drawings or Apps Script file to mimeType — e.g. "
        "application/pdf, text/plain, text/markdown, docx, xlsx, text/csv (first sheet), pptx, "
        "image/png — and return it as a file (small text inline as content). 10 MB export limit. "
        "For stored files (PDFs, images, uploads) use GOOGLEDRIVE_DOWNLOAD_FILE.",
        _schema(
            {
                "fileId": FILE_ID,
                "mimeType": _string("Target format.", enum=EXPORT_MIME_TYPES),
            },
            ["fileId", "mimeType"],
        ),
        _export_google_workspace_file,
        "export", "convert", "docs", "sheets", "slides", "pdf", "markdown", "csv",
    ),
    _read(
        "GOOGLEDRIVE_GET_ABOUT",
        "Get the connected Google account and its Drive: user (name, email), storage quota "
        "(limit, usage, usageInDrive, usageInDriveTrash — byte counts as strings), max upload "
        "size and whether shared drives can be created. fields selects others, e.g. "
        "'exportFormats' or '*'.",
        _schema({"fields": FIELDS}),
        _get_about,
        "me", "account", "user", "storage", "quota", "space", "whoami",
    ),
    _read(
        "GOOGLEDRIVE_LIST_SHARED_DRIVES",
        "List the shared drives (team drives) the user is a member of: id, name, createdTime, "
        "hidden. One page: has_more, next_page_token, items[]. Use an id as driveId in "
        "GOOGLEDRIVE_FIND_FILE to search inside one shared drive.",
        _schema(
            {
                "q": _string("Shared-drive query, e.g. \"name contains 'Marketing'\"."),
                "fields": FIELDS,
                "pageSize": _integer("Drives per page (1-100).", maximum=MAX_SMALL_PAGE_SIZE),
                "pageToken": PAGE_TOKEN,
                "useDomainAdminAccess": _boolean("As a domain admin, list every shared drive in the domain."),
            }
        ),
        _list_shared_drives,
        "shared drives", "team drives", "drives", "list",
    ),
    _read(
        "GOOGLEDRIVE_LIST_PERMISSIONS",
        "List who can access a Google Drive file, folder or shared drive: each permission's "
        "id, type (user/group/domain/anyone), role (owner/writer/commenter/reader...), "
        "emailAddress, domain and expirationTime. One page: has_more, next_page_token, items[].",
        _schema(
            {
                "fileId": FILE_ID,
                "fields": FIELDS,
                "pageSize": _integer("Permissions per page (1-100).", maximum=MAX_SMALL_PAGE_SIZE),
                "pageToken": PAGE_TOKEN,
                "supportsAllDrives": SUPPORTS_ALL_DRIVES,
                "useDomainAdminAccess": _boolean("Act as a domain administrator."),
                "includePermissionsForView": PERMISSIONS_FOR_VIEW,
            },
            ["fileId"],
        ),
        _list_permissions,
        "permissions", "sharing", "access", "who", "shared with", "collaborators",
    ),
    _read(
        "GOOGLEDRIVE_LIST_COMMENTS",
        "List the comments on a Google Drive file (Docs, Sheets, Slides, PDFs...): content, "
        "author, created/modified times, resolved, quoted text and replies. One page (20 by "
        "default): has_more, next_page_token, items[].",
        _schema(
            {
                "fileId": FILE_ID,
                "fields": _string("Fields to return; '*' (default) returns every comment field."),
                "pageSize": _integer("Comments per page (1-100).", default=20, maximum=MAX_SMALL_PAGE_SIZE),
                "pageToken": PAGE_TOKEN,
                "includeDeleted": _boolean("Include deleted comments.", default=False),
                "startModifiedTime": _string("Only comments modified after this RFC 3339 time."),
            },
            ["fileId"],
        ),
        _list_comments,
        "comments", "feedback", "discussion", "notes", "review",
    ),
    # --------------------------------------------------------------- create
    _write(
        "GOOGLEDRIVE_CREATE_FOLDER",
        "Create a folder in Google Drive, at the top of My Drive or inside parent_id (a folder "
        "id, or the exact name of an existing folder). Returns the new folder's id, name and "
        "webViewLink.",
        _schema(
            {
                "name": _string("Name of the new folder."),
                "parent_id": _string("Parent folder id, or the exact name of an existing folder."),
            },
            ["name"],
        ),
        _create_folder,
        "create", "new", "folder", "directory", "mkdir",
    ),
    _write(
        "GOOGLEDRIVE_UPLOAD_FILE",
        "Upload a file to Google Drive as a new file (in folder_to_upload_to, else the top of "
        "My Drive). Pass the bytes base64-encoded in file_to_upload.file_content_bytes with "
        "its name (and mimetype, guessed from the name when omitted). Returns the new file's "
        "id, name and webViewLink.",
        _schema(
            {
                "file_to_upload": {
                    "type": "object",
                    "description": "The file: name, mimetype and base64 content.",
                    "properties": {
                        "name": _string("File name, e.g. 'report.pdf'."),
                        "mimetype": _string("MIME type, e.g. 'application/pdf'."),
                        "file_content_bytes": _string("The file's bytes, base64-encoded."),
                    },
                    "required": ["name", "file_content_bytes"],
                },
                "folder_to_upload_to": _id("Destination folder id; the top of My Drive if omitted."),
            },
            ["file_to_upload"],
        ),
        _upload_file,
        "upload", "add", "put", "save", "store", "file",
    ),
    _write(
        "GOOGLEDRIVE_CREATE_FILE_FROM_TEXT",
        "Create a new Google Drive file from text — a .txt/.md/.csv/.html file, or a Google "
        "Doc (mime_type application/vnd.google-apps.document) or Google Sheet "
        "(application/vnd.google-apps.spreadsheet, from CSV text). Optionally inside parent_id "
        "(a folder id). Returns id, name and mimeType at the top level.",
        _schema(
            {
                "file_name": _string("Name of the new file, e.g. 'notes.md' or 'Meeting notes'."),
                "text_content": _string("The file's text."),
                "mime_type": _string("MIME type of the new file.", default="text/plain"),
                "parent_id": _id("Folder id to create the file in."),
            },
            ["file_name", "text_content"],
        ),
        _create_file_from_text,
        "create", "new", "write", "text", "document", "doc", "note",
        aliases={
            "title": "file_name",
            "name": "file_name",
            "content": "text_content",
            "body": "text_content",
            "text": "text_content",
            "folder_id": "parent_id",
            "parent_folder_id": "parent_id",
        },
    ),
    # --------------------------------------------------------------- change
    _write(
        "GOOGLEDRIVE_EDIT_FILE",
        "Replace a stored Google Drive file's entire content with new text (a new revision; the "
        "file keeps its id, name and sharing). Not for Google Docs/Sheets/Slides. Returns the "
        "updated file.",
        _schema(
            {
                "file_id": FILE_ID,
                "content": _string("The complete new content."),
                "mime_type": _string("MIME type of the new content.", default="text/plain"),
            },
            ["file_id", "content"],
        ),
        _edit_file,
        "edit", "update", "overwrite", "replace", "write", "change", "content",
        destructive=True,
    ),
    _write(
        "GOOGLEDRIVE_COPY_FILE",
        "Copy a Google Drive file (not a folder) into the same folder; new_title names the "
        "copy (default 'Copy of <name>'). Returns the copy's id, name and webViewLink.",
        _schema(
            {
                "file_id": FILE_ID,
                "new_title": _string("Name for the copy."),
            },
            ["file_id"],
        ),
        _copy_file,
        "copy", "duplicate", "clone",
    ),
    _write(
        "GOOGLEDRIVE_MOVE_FILE",
        "Move a Google Drive file or folder to another folder: add_parents is the destination "
        "folder id; its current folder is removed automatically unless remove_parents is given. "
        "Returns the file with its new parents.",
        _schema(
            {
                "file_id": FILE_ID,
                "add_parents": _string("Destination folder id."),
                "remove_parents": _string("Comma-separated folder ids to take the file out of."),
                "supports_all_drives": _boolean("Allow shared-drive items (default true).", default=True),
                "ocr_language": _string("ISO 639-1 language hint for OCR of uploaded images."),
                "include_labels": INCLUDE_LABELS,
                "keep_revision_forever": _boolean("Keep the head revision forever."),
                "include_permissions_for_view": PERMISSIONS_FOR_VIEW,
                "use_content_as_indexable_text": _boolean("Use the content as indexable text."),
            },
            ["file_id"],
        ),
        _move_file,
        "move", "relocate", "folder", "organize", "file",
    ),
    _write(
        "GOOGLEDRIVE_UPDATE_FILE_PUT",
        "Update a Google Drive file's or folder's metadata (partial update): rename it (name), "
        "set its description, star/unstar it, or change parents (add_parents / "
        "remove_parents) and sharing restrictions. Content is not changed — use "
        "GOOGLEDRIVE_EDIT_FILE for that. Returns the updated metadata.",
        _schema(
            {
                "fileId": FILE_ID,
                "name": _string("New name."),
                "description": _string("New description."),
                "starred": _boolean("Star (true) or unstar (false)."),
                "mime_type": _string("New MIME type (rarely needed)."),
                "add_parents": _string("Comma-separated folder ids to add."),
                "remove_parents": _string("Comma-separated folder ids to remove."),
                "writers_can_share": _boolean("Whether editors may change sharing."),
                "viewers_can_copy_content": _boolean(
                    "Whether viewers and commenters may download, print or copy."
                ),
                "ocr_language": _string("ISO 639-1 language hint for OCR."),
                "keep_revision_forever": _boolean("Keep the head revision forever."),
                "supports_all_drives": _boolean("Allow shared-drive items (default true).", default=True),
            },
            ["fileId"],
        ),
        _update_file_put,
        "rename", "update", "metadata", "describe", "star", "favorite", "edit",
    ),
    _write(
        "GOOGLEDRIVE_DELETE_FILE",
        "Delete a Google Drive file or folder by moving it to the trash (a folder takes its "
        "contents with it). Recoverable for 30 days with GOOGLEDRIVE_UNTRASH_FILE; nothing is "
        "deleted permanently.",
        _schema(
            {"fileId": FILE_ID, "supportsAllDrives": SUPPORTS_ALL_DRIVES},
            ["fileId"],
        ),
        _set_trashed(True, "fileId"),
        "delete", "remove", "trash", "erase", "discard",
        destructive=True,
    ),
    _write(
        "GOOGLEDRIVE_TRASH_FILE",
        "Move a Google Drive file or folder to the trash (recoverable for 30 days with "
        "GOOGLEDRIVE_UNTRASH_FILE).",
        _schema(
            {"file_id": FILE_ID, "fields": FIELDS, "supportsAllDrives": SUPPORTS_ALL_DRIVES},
            ["file_id"],
        ),
        _set_trashed(True, "file_id"),
        "trash", "delete", "remove", "bin",
        destructive=True,
    ),
    _write(
        "GOOGLEDRIVE_UNTRASH_FILE",
        "Restore a Google Drive file or folder from the trash to where it was.",
        _schema(
            {"file_id": FILE_ID, "supportsAllDrives": SUPPORTS_ALL_DRIVES},
            ["file_id"],
        ),
        _set_trashed(False, "file_id"),
        "restore", "untrash", "undelete", "recover",
    ),
    # ---------------------------------------------------------------- share
    _write(
        "GOOGLEDRIVE_ADD_FILE_SHARING_PREFERENCE",
        "Share a Google Drive file or folder: grant role (reader, commenter, writer, ...) to a "
        "user or group (email_address), a whole domain (domain), or anyone with the link "
        "(type 'anyone' — makes it public to the link). Returns the new permission.",
        _schema(
            {
                "file_id": FILE_ID,
                "role": ROLE,
                "type": GRANTEE_TYPE,
                "email_address": _string("Email of the user or group (type user/group)."),
                "domain": _string("Domain to share with (type domain), e.g. 'example.com'."),
                "transfer_ownership": _boolean("Required (true) when role is owner."),
            },
            ["file_id", "role", "type"],
        ),
        _create_permission,
        "share", "sharing", "permission", "access", "invite", "grant", "collaborator",
    ),
    _write(
        "GOOGLEDRIVE_CREATE_PERMISSION",
        "Grant access to a Google Drive file, folder or shared drive (like "
        "GOOGLEDRIVE_ADD_FILE_SHARING_PREFERENCE, plus notification email text, expiration time "
        "and discovery options). Returns the new permission.",
        _schema(
            {
                "file_id": FILE_ID,
                "role": ROLE,
                "type": GRANTEE_TYPE,
                "email_address": _string("Email of the user or group (type user/group)."),
                "domain": _string("Domain to share with (type domain)."),
                "email_message": _string("Text to include in the notification email."),
                "send_notification_email": _boolean("Email the grantee (Google's default: true for users/groups)."),
                "expiration_time": _string("RFC 3339 time the access ends (user/group readers, commenters, writers)."),
                "allow_file_discovery": _boolean("For domain/anyone: findable in search."),
                "transfer_ownership": _boolean("Required (true) when role is owner."),
                "move_to_new_owners_root": _boolean("On ownership transfer, move to the new owner's My Drive."),
                "supports_all_drives": _boolean("Allow shared-drive items (default true).", default=True),
                "use_domain_admin_access": _boolean("Act as a domain administrator."),
            },
            ["file_id", "role", "type"],
        ),
        _create_permission,
        "share", "permission", "access", "grant", "invite",
    ),
    _write(
        "GOOGLEDRIVE_CREATE_COMMENT",
        "Add a comment to a Google Drive file. Omit anchor and quoted_file_content_* for a "
        "file-level comment. Returns the comment (its id is data.id).",
        _schema(
            {
                "file_id": FILE_ID,
                "content": _string("The comment text."),
                "anchor": _string("JSON region the comment refers to (Drive anchor syntax)."),
                "quoted_file_content_value": _string("The quoted text the comment is about."),
                "quoted_file_content_mime_type": _string("MIME type of the quoted text, e.g. 'text/plain'."),
            },
            ["file_id", "content"],
        ),
        _create_comment,
        "comment", "feedback", "note", "annotate", "review",
    ),
)

TOOLS: dict[str, GoogleDriveTool] = {tool.spec.slug: tool for tool in CATALOG}
TOOL_SPECS: tuple[ToolSpec, ...] = tuple(tool.spec for tool in CATALOG)
HANDLERS: dict[str, Handler] = {tool.spec.slug: tool.handler for tool in CATALOG}
