"""Compatibility with the BE's Content Vault import, which calls ``/fetch``
with Composio slugs (AISquare-Studio-BE
``aisquare_studio/content_vault/connector_import.py``, origin/main fde165099):

* search: ``GMAIL_FETCH_EMAILS`` with ``{"query", "max_results": 5,
  "include_payload": True, "verbose": True}``; for each entry of the fetch
  payload's ``items`` it takes ``id`` or ``messageId``, walks every dict under
  the entry's ``payload`` and keeps parts with ``body.attachmentId`` and a
  ``filename`` (``mimeType`` and ``body.size`` too). The candidate id is
  ``<message_id>::<attachment_id>``.
* download: ``GMAIL_GET_ATTACHMENT`` with ``message_id``, ``attachment_id``
  and ``file_name``; it needs the bytes.

The BE reads ``items`` as one entry per message, so the pipeAAS ``/fetch``
route must fan this toolkit's ``data["items"]`` out into its response items
(Composio's own shape nests them under ``messages``; ``unwrap: "messages"``
in INTEGRATION.md). That mapping is pipeAAS's side of the contract; the test
below fans out the same way.

The BE helpers are copied verbatim (``_be_*``). This file fails when the
toolkit stops accepting those arguments or stops returning those fields; it
cannot see a later BE change, so update the copies when the BE changes.
"""

from __future__ import annotations

from typing import Any

from .fakes import attachment_part, b64, harness, listing, message, response, text_part

COMPOSITE_ID_SEPARATOR = "::"
MAX_CANDIDATES = 5

# ---- mirrored from connector_import.py --------------------------------------


def _be_iter_dicts(node: Any):
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from _be_iter_dicts(value)
    elif isinstance(node, list):
        for value in node:
            yield from _be_iter_dicts(value)


def _be_candidate(file_id, name, mime, size, modified):
    return {"file_id": file_id, "name": name, "mime_type": mime or "", "size": size, "modified": modified or ""}


def _be_search_args(query: str) -> dict[str, Any]:
    return {"query": query, "max_results": MAX_CANDIDATES, "include_payload": True, "verbose": True}


def _be_search_gmail(payload: dict[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for msg in payload.get("items") or []:
        if not isinstance(msg, dict):
            continue
        message_id = msg.get("id") or msg.get("messageId")
        if not message_id:
            continue
        for part in _be_iter_dicts(msg.get("payload")):
            body = part.get("body") or {}
            attachment_id = body.get("attachmentId")
            filename = part.get("filename")
            if not attachment_id or not filename:
                continue
            size = body.get("size")
            out.append(
                _be_candidate(
                    f"{message_id}{COMPOSITE_ID_SEPARATOR}{attachment_id}",
                    str(filename),
                    part.get("mimeType"),
                    size if isinstance(size, int) else None,
                    None,
                )
            )
    return out


def _be_download_args(meta: dict[str, Any]) -> dict[str, Any]:
    message_id, _, attachment_id = meta["source_id"].partition(COMPOSITE_ID_SEPARATOR)
    return {
        "message_id": message_id,
        "attachment_id": attachment_id,
        "file_name": meta.get("source_name") or meta.get("final_name") or "attachment",
    }


# ---- the round trip ---------------------------------------------------------


def _fetch_payload(data: dict[str, Any]) -> dict[str, Any]:
    """What pipeAAS ``/fetch`` returns for a list result: one item per entry."""
    return {"count": len(data["items"]), "items": data["items"]}


def test_search_then_download():
    with_pdf = message(
        "18c1aaa",
        parts=[
            text_part("See attached", part_id="0"),
            attachment_part("Q3 report.pdf", "application/pdf", "ANGjdJ-pdf", 48213, part_id="1"),
        ],
    )
    nested = message(
        "18c1bbb",
        parts=[
            {"partId": "0", "mimeType": "multipart/alternative", "filename": "", "headers": [], "body": {"size": 0},
             "parts": [text_part("hi", part_id="0.0"), text_part("<p>hi</p>", "text/html", part_id="0.1")]},
            attachment_part("data.csv", "text/csv", "ANGjdJ-csv", 12, part_id="1"),
        ],
    )
    no_attachments = message("18c1ccc")
    h = harness(
        response(200, listing(["18c1aaa", "18c1bbb", "18c1ccc"])),
        response(200, with_pdf),
        response(200, nested),
        response(200, no_attachments),
        response(200, {"size": 7, "data": b64("%PDF-1.")}),
    )
    result = h.call("GMAIL_FETCH_EMAILS", _be_search_args("Q3 report"))
    assert result.successful, result.error
    assert h.session.calls[0].params == {"maxResults": 5, "q": "Q3 report"}

    candidates = _be_search_gmail(_fetch_payload(result.data))
    assert candidates == [
        _be_candidate("18c1aaa::ANGjdJ-pdf", "Q3 report.pdf", "application/pdf", 48213, None),
        _be_candidate("18c1bbb::ANGjdJ-csv", "data.csv", "text/csv", 12, None),
    ]

    meta = {"source_id": candidates[0]["file_id"], "source_name": candidates[0]["name"]}
    download = h.call("GMAIL_GET_ATTACHMENT", _be_download_args(meta))
    assert download.successful, download.error
    assert h.session.last.path == "/me/messages/18c1aaa/attachments/ANGjdJ-pdf"
    (envelope,) = download.files
    assert (envelope.data, envelope.content_type, envelope.metadata["filename"]) == (b"%PDF-1.", "application/pdf", "Q3 report.pdf")
    assert (download.data["name"], download.data["mimetype"]) == ("Q3 report.pdf", "application/pdf")


def test_without_payload_the_be_finds_nothing_to_import():
    """``include_payload`` is what exposes the parts; the BE always sends it."""
    h = harness(response(200, listing(["m1"])), response(200, message("m1", parts=[attachment_part("a.pdf", "application/pdf", "A1")])))
    data = h.call("GMAIL_FETCH_EMAILS", {**_be_search_args("x"), "include_payload": False}).data
    assert _be_search_gmail(_fetch_payload(data)) == []
    assert data["items"][0]["attachmentList"][0]["attachmentId"] == "A1"
