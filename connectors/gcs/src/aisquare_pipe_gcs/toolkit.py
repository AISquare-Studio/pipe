"""GcsToolkit: read-only object listing and reads in one Cloud Storage bucket.

``config`` is the tenant's credential blob (``service_account_json``,
``bucket``, optional ``prefix``; see :mod:`.credentials`). Hosts build it
server-side; no tool argument can name another bucket or step outside the
prefix. There is no OAuth: the key belongs to a service account the customer
creates, so a host classifies this toolkit as a per-tenant credential (first
party).
"""

from __future__ import annotations

import mimetypes
import re
from collections.abc import Mapping
from typing import Any

from aisquare.pipe import (
    ERROR_FORBIDDEN,
    ERROR_INVALID_ARGUMENTS,
    ERROR_NOT_CONNECTED,
    ERROR_RATE_LIMITED,
    ERROR_TIMEOUT,
    ERROR_UPSTREAM_ERROR,
    PRIMARY_TAG,
    AuthType,
    ToolError,
    Toolkit,
    ToolResult,
    ToolSpec,
    error_code_for_status,
    time_budget,
)
from aisquare.pipe.core.envelope import DataEnvelope

from aisquare_pipe_gcs.client import (
    GcsAPIError,
    GcsClient,
    ObjectTooLarge,
    object_record,
)
from aisquare_pipe_gcs.credentials import (
    GcsCredentials,
    GcsNotConfigured,
    load_credentials,
)

MAX_PAGE_SIZE = 1000
DEFAULT_PAGE_SIZE = 100
#: Largest object GCS_GET_OBJECT loads (bytes); max_bytes may lower it.
MAX_OBJECT_BYTES = 100 * 1024 * 1024
DEFAULT_MAX_BYTES = 25 * 1024 * 1024
#: Text up to this size is also returned inline as ``content``.
INLINE_TEXT_MAX = 1024 * 1024
MAX_NAME_CHARS = 1024
_GENERATION = re.compile(r"^[0-9]{1,20}$")

LIST_OBJECTS = ToolSpec(
    slug="GCS_LIST_OBJECTS",
    description=(
        "List the files (objects) in the company's Google Cloud Storage bucket, optionally "
        "under a folder-like prefix such as 'policies/2026/'. Returns each object's name, "
        "size, content_type and its version markers: generation and md5_hash (both change "
        "when the file's content changes) and updated. Set folders=true to list one level, "
        "with sub-prefixes in folders. Pass next_page_token back as page_token for more. "
        "Read a file with GCS_GET_OBJECT."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "prefix": {
                "type": "string",
                "maxLength": MAX_NAME_CHARS,
                "description": "Only objects whose names start with this, e.g. 'policies/'.",
            },
            "folders": {
                "type": "boolean",
                "default": False,
                "description": "List one level only: objects directly under prefix, and the "
                "sub-prefixes below it in folders.",
            },
            "page_size": {
                "type": "integer",
                "minimum": 1,
                "maximum": MAX_PAGE_SIZE,
                "default": DEFAULT_PAGE_SIZE,
                "description": f"Objects per page, 1-{MAX_PAGE_SIZE}.",
            },
            "page_token": {
                "type": "string",
                "description": "next_page_token from a previous GCS_LIST_OBJECTS call.",
            },
        },
        "additionalProperties": False,
    },
    read_only=True,
    tags=("list", "files", "objects", "bucket", "folder", "storage", "version"),
)

GET_OBJECT = ToolSpec(
    slug="GCS_GET_OBJECT",
    description=(
        "Read one file (object) from the company's Google Cloud Storage bucket by its name: "
        "its bytes (returned as a file; text up to 1 MiB also inline as content) plus "
        "content_type, size and the version markers generation, md5_hash and updated, which "
        "change when the file is edited. Set include_content=false to read only the "
        "metadata, e.g. to check whether a policy document changed. generation reads an "
        "older version when the bucket keeps them."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "minLength": 1,
                "maxLength": MAX_NAME_CHARS,
                "description": "The object's full name, e.g. 'policies/travel.pdf'.",
            },
            "generation": {
                "type": "string",
                "pattern": "^[0-9]{1,20}$",
                "description": "A specific generation (version) of the object.",
            },
            "include_content": {
                "type": "boolean",
                "default": True,
                "description": "false: metadata and version markers only, no download.",
            },
            "max_bytes": {
                "type": "integer",
                "minimum": 1,
                "maximum": MAX_OBJECT_BYTES,
                "default": DEFAULT_MAX_BYTES,
                "description": f"Largest object to download, at most {MAX_OBJECT_BYTES} bytes.",
            },
        },
        "required": ["name"],
        "additionalProperties": False,
    },
    read_only=True,
    tags=(
        PRIMARY_TAG,
        "read",
        "download",
        "file",
        "document",
        "content",
        "policy document",
    ),
)

TOOL_SPECS = (LIST_OBJECTS, GET_OBJECT)


def _describe(e: GcsAPIError) -> str:
    if e.status == 401:
        return (
            f"{e.message} — Google rejected the service account key (deleted, disabled or "
            "expired); the tenant's GCS credential must be fixed"
        )
    if e.status == 403:
        return f"{e.message} — the service account lacks roles/storage.objectViewer on the bucket"
    return e.message


def failure(e: GcsAPIError) -> ToolResult:
    """A GCS failure as a canonical ``ToolResult`` error."""
    if e.timed_out:
        code = ERROR_TIMEOUT
    elif e.status == 429:
        code = ERROR_RATE_LIMITED
    elif e.status is None or 300 <= e.status < 400:
        code = ERROR_UPSTREAM_ERROR
    else:
        code = error_code_for_status(e.status)
    return ToolResult.fail(_describe(e), code=code, retry_after=e.retry_after)


def _bounded_int(arguments: Mapping[str, Any], name: str, low: int, high: int) -> int:
    value = arguments[name]
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not low <= value <= high
    ):
        raise ToolError(
            f"{name} must be an integer from {low} to {high}",
            code=ERROR_INVALID_ARGUMENTS,
        )
    return value


def _inside(creds: GcsCredentials, name: str, what: str) -> str:
    """``name`` if it is inside the configured prefix, else a forbidden error."""
    if not name.isprintable():
        raise ToolError(
            f"{what} holds control characters", code=ERROR_INVALID_ARGUMENTS
        )
    if not creds.contains(name):
        raise ToolError(
            f"{what} is outside the configured prefix {creds.prefix!r}",
            code=ERROR_FORBIDDEN,
        )
    return name


def _textual(content_type: str) -> bool:
    return (
        content_type.startswith("text/")
        or content_type in ("application/json", "application/xml")
        or content_type.endswith(("+json", "+xml"))
    )


def file_envelope(
    record: dict[str, Any], content: bytes, *, bucket: str
) -> DataEnvelope:
    """An object's bytes as an envelope whose metadata carries its version markers."""
    name = str(record.get("name") or "")
    content_type = str(record.get("content_type") or "") or (
        mimetypes.guess_type(name)[0] or "application/octet-stream"
    )
    metadata = {
        "filename": name.rsplit("/", 1)[-1] or name,
        "bucket": bucket,
        "object_name": name,
        "size": len(content),
        "content_type": content_type,
        **{
            key: record[key]
            for key in (
                "generation",
                "md5_hash",
                "crc32c",
                "etag",
                "updated",
                "time_created",
            )
            if record.get(key) is not None
        },
    }
    return DataEnvelope(
        content_type=content_type,
        data=content,
        source_id=f"gcs:{bucket}/{name}",
        metadata=metadata,
    )


class GcsToolkit(Toolkit):
    """Google Cloud Storage, read-only: ``GCS_GET_OBJECT`` (primary) and
    ``GCS_LIST_OBJECTS``. ``session`` (tests, or a host that pools connections)
    is the ``requests.Session`` every call goes through, token minting included."""

    name = "gcs"
    version = "0.1.0"
    display_name = "Google Cloud Storage"
    description = (
        "Google Cloud Storage: list the files in the company's bucket and read one, with the "
        "version markers (generation, md5 hash, updated) that tell when a file changed."
    )
    auth_type = AuthType.CUSTOM
    oauth = None
    docs_url = "https://cloud.google.com/storage/docs/json_api"
    category = "files"
    keywords = (
        "google cloud storage",
        "gcs",
        "gcp",
        "bucket",
        "object storage",
        "cloud storage",
        "files",
        "policy documents",
        "download",
    )
    examples = (
        "list the policy documents in our GCS bucket",
        "read policies/travel.pdf from cloud storage",
        "has the retention policy file in the bucket changed",
    )

    def __init__(self, *, session: Any = None) -> None:
        self._session = session

    def tools(self) -> list[ToolSpec]:
        return list(TOOL_SPECS)

    def validate_config(self, config: dict[str, Any]) -> bool:
        try:
            load_credentials(config)
        except GcsNotConfigured:
            return False
        return True

    def call_tool(
        self,
        slug: str,
        arguments: Mapping[str, Any] | None,
        config: Mapping[str, Any] | None,
    ) -> ToolResult:
        """Core's ``call_tool``, except that a known tool on an unusable
        credential fails with why GCS is not configured (no request is made)."""
        if isinstance(slug, str) and self.get_tool(slug) is not None:
            try:
                load_credentials(config)
            except GcsNotConfigured as e:
                return ToolResult.fail(
                    f"Google Cloud Storage is not configured: {e}",
                    code=ERROR_NOT_CONNECTED,
                )
            except Exception:  # noqa: BLE001 — call_tool never raises
                return ToolResult.fail(
                    "Google Cloud Storage is not configured", code=ERROR_NOT_CONNECTED
                )
        return super().call_tool(slug, arguments, config)

    def run_tool(
        self, tool: ToolSpec, arguments: dict[str, Any], config: dict[str, Any]
    ) -> ToolResult:
        creds = load_credentials(config)
        with GcsClient(
            creds, session=self._session, budget=time_budget(config)
        ) as client:
            try:
                if tool.slug == LIST_OBJECTS.slug:
                    return self._list(client, creds, arguments)
                if tool.slug == GET_OBJECT.slug:
                    return self._get(client, creds, arguments)
            except GcsNotConfigured as e:
                return ToolResult.fail(
                    f"Google Cloud Storage is not configured: {e}",
                    code=ERROR_NOT_CONNECTED,
                )
            except GcsAPIError as e:
                return failure(e)
        raise ToolError(f"{tool.slug} is not implemented")

    def _list(
        self, client: GcsClient, creds: GcsCredentials, arguments: dict[str, Any]
    ) -> ToolResult:
        prefix = arguments.get("prefix")
        prefix = (
            creds.prefix
            if prefix in (None, "")
            else _inside(creds, str(prefix), "prefix")
        )
        page_size = _bounded_int(arguments, "page_size", 1, MAX_PAGE_SIZE)
        response = client.list_objects(
            prefix=prefix,
            page_size=page_size,
            page_token=arguments.get("page_token") or None,
            delimiter=arguments.get("folders") is True,
        )
        items = response.get("items")
        prefixes = response.get("prefixes")
        token = response.get("nextPageToken")
        token = token if isinstance(token, str) and token else None
        return ToolResult.ok(
            {
                "bucket": creds.bucket,
                "prefix": prefix,
                "objects": [object_record(i) for i in items]
                if isinstance(items, list)
                else [],
                "folders": [p for p in prefixes if isinstance(p, str)]
                if isinstance(prefixes, list)
                else [],
                "has_more": token is not None,
                "next_page_token": token,
            }
        )

    def _get(
        self, client: GcsClient, creds: GcsCredentials, arguments: dict[str, Any]
    ) -> ToolResult:
        name = _inside(creds, str(arguments["name"]), "name")
        generation = arguments.get("generation") or None
        if generation is not None and not _GENERATION.match(str(generation)):
            raise ToolError("generation must be digits", code=ERROR_INVALID_ARGUMENTS)
        max_bytes = _bounded_int(arguments, "max_bytes", 1, MAX_OBJECT_BYTES)
        record = object_record(client.get_metadata(name, generation=generation))
        data: dict[str, Any] = {"bucket": creds.bucket, **record}
        if arguments.get("include_content") is False:
            return ToolResult.ok(data)
        if record.get("size") is not None and record["size"] > max_bytes:
            raise ToolError(
                f"{name} is {record['size']} bytes, over max_bytes ({max_bytes}); read its "
                "metadata with include_content=false",
                code=ERROR_INVALID_ARGUMENTS,
            )
        try:
            content = client.download(
                name,
                generation=record.get("generation") or generation,
                max_bytes=max_bytes,
            )
        except ObjectTooLarge as e:
            raise ToolError(
                f"{e.message}; raise max_bytes or read the metadata only",
                code=ERROR_INVALID_ARGUMENTS,
            ) from None
        envelope = file_envelope(record, content, bucket=creds.bucket)
        content_type = str(envelope.content_type)
        if len(content) <= INLINE_TEXT_MAX and _textual(content_type):
            try:
                data["content"] = content.decode("utf-8")
            except UnicodeDecodeError:
                pass
        return ToolResult.ok(data, files=[envelope])


__all__ = [
    "GET_OBJECT",
    "LIST_OBJECTS",
    "TOOL_SPECS",
    "GcsToolkit",
    "failure",
    "file_envelope",
]
