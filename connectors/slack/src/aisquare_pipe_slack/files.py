"""Files through ``DataEnvelope`` (plan §4.8): Slack's v2 upload and downloads.

:func:`upload_file` runs the three-step v2 upload (``files.upload`` (v1) was
sunset in 2025): ask for a pre-signed URL, POST the bytes there (no token),
then complete the upload exactly once — into conversations, or privately when
none is given. It adds no attribution; callers append it to
``initial_comment``. The toolkit's upload tool and the message sink share it.

:func:`download_file` reads ``files.info``, refuses external files (their
``url_private`` is the third party's, and must never get the Slack token),
pre-checks the size, streams the bytes from ``files.slack.com`` and returns
them as a :class:`~aisquare.pipe.DataEnvelope` in ``ToolResult.files`` — real
bytes a host can import. ``data`` describes the file with Composio-style keys.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from aisquare.pipe import (
    ERROR_FORBIDDEN,
    ERROR_INVALID_ARGUMENTS,
    DataEnvelope,
    ToolError,
    ToolResult,
)

from aisquare_pipe_slack.rest import SlackAPIError, SlackWebClient

#: Largest file a download returns (the same cap as Google Drive's).
DOWNLOAD_MAX_BYTES = 100 * 1024 * 1024
#: Largest text a download also inlines in ``data["content"]``.
INLINE_CONTENT_MAX = 100_000
#: Metadata a download describes the file with.
_FILE_KEYS = ("id", "name", "title", "mimetype", "filetype", "size", "mode", "permalink", "created", "user")


def upload_file(
    client: SlackWebClient,
    data: bytes,
    *,
    filename: str,
    title: str | None = None,
    channels: str | None = None,
    initial_comment: str | None = None,
    thread_ts: str | None = None,
    snippet_type: str | None = None,
    blocks: list[Any] | None = None,
) -> dict[str, Any]:
    """Upload ``data`` with Slack's v2 flow and return the file object.
    ``channels`` is a comma-separated list of conversation IDs to share it
    in (none: the file stays private to the uploader)."""
    ticket = client.call(
        "files.getUploadURLExternal",
        {"filename": filename, "length": len(data), "snippet_type": snippet_type},
    )
    upload_url, file_id = ticket.get("upload_url"), ticket.get("file_id")
    if not isinstance(upload_url, str) or not isinstance(file_id, str):
        raise SlackAPIError("files.getUploadURLExternal", "invalid_response", status=200)
    client.upload_external(upload_url, data)
    entry: dict[str, Any] = {"id": file_id}
    if title:
        entry["title"] = title
    done = client.call(
        "files.completeUploadExternal",
        {
            "files": [entry],
            "channels": channels,
            "initial_comment": initial_comment,
            "thread_ts": thread_ts,
            "blocks": blocks,
        },
        json_body=True,
    )
    uploaded = done.get("files")
    if isinstance(uploaded, list) and uploaded and isinstance(uploaded[0], dict):
        return uploaded[0]
    return {"id": file_id}


def download_file(client: SlackWebClient, file_id: str) -> ToolResult:
    """``files.info`` then the bytes, as a ToolResult with one file envelope."""
    info = client.call("files.info", {"file": file_id}, idempotent=True).get("file")
    if not isinstance(info, Mapping):
        raise SlackAPIError("files.info", "invalid_response", status=200)
    if info.get("is_external") or info.get("mode") == "external":
        raise ToolError(
            "this is an external (remote) file: its bytes live on the third party's service, "
            "not in Slack — use its permalink",
            code=ERROR_INVALID_ARGUMENTS,
        )
    size = info.get("size")
    if isinstance(size, int) and size > DOWNLOAD_MAX_BYTES:
        raise ToolError(
            f"the file is {size} bytes, over the {DOWNLOAD_MAX_BYTES // (1024 * 1024)} MiB download cap",
            code=ERROR_INVALID_ARGUMENTS,
        )
    name = str(info.get("name") or file_id)
    mimetype = str(info.get("mimetype") or "")
    inline = info.get("content")
    if info.get("mode") == "snippet" and isinstance(inline, str):
        content, content_type = inline.encode("utf-8"), mimetype or "text/plain"
    else:
        url = info.get("url_private_download") or info.get("url_private")
        content, content_type = client.download(str(url or ""), max_bytes=DOWNLOAD_MAX_BYTES)
        if content_type.lower().startswith("text/html") and "html" not in mimetype.lower():
            raise ToolError(
                "Slack returned a sign-in page instead of the file — this connection lacks "
                "files:read (reconnect Slack to grant it)",
                code=ERROR_FORBIDDEN,
            )
    data: dict[str, Any] = {k: info[k] for k in _FILE_KEYS if k in info}
    data["size"] = len(content)
    data["mimetype"] = mimetype or content_type.split(";", 1)[0].strip() or "application/octet-stream"
    text_like = data["mimetype"].startswith("text/") or data["mimetype"] in ("application/json", "application/xml")
    if text_like and len(content) <= INLINE_CONTENT_MAX:
        try:
            data["content"] = content.decode("utf-8")
        except UnicodeDecodeError:
            pass
    envelope = DataEnvelope(
        content_type=data["mimetype"],
        data=content,
        source_id=f"slack:{file_id}",
        metadata={"filename": name, "file_id": file_id, "size": len(content)},
    )
    return ToolResult.ok(data, files=[envelope])
