"""Compatibility with the BE's Content Vault import, which calls ``/fetch``
with Composio slugs (AISquare-Studio-BE
``aisquare_studio/content_vault/connector_import.py``):

* search: ``ONE_DRIVE_SEARCH_ITEMS`` with ``{"q": <query>, "top": 10}``; it
  walks every dict under the fetch payload's ``items`` and keeps those with
  ``id``, ``name`` and a ``file`` facet (``mimeType``, ``size``,
  ``lastModifiedDateTime`` when present).
* download: ``ONE_DRIVE_DOWNLOAD_FILE`` with ``{"item_id", "file_name"}``
  (``file_name`` always sent); it needs the file bytes.

The BE helpers are mirrored here verbatim (``_be_*``), as of the BE's
connector_import.py on 2026-10-16. This file catches drift on the toolkit
side only: refresh the mirror whenever connector_import.py changes. The bytes travel in ``ToolResult.files``; the pipeAAS
``/fetch`` route serializes them (``s3url`` / ``file_content_bytes`` are what
the BE's ``_extract_bytes`` reads) — that mapping is pipeAAS's side of the
contract, simulated below with the inline ``file_content_bytes`` form.
"""

from __future__ import annotations

import base64
from typing import Any

from .fakes import file_item, folder_item, harness, listing, response

BE_SEARCH_TOP = 10

SEARCH_REPLY = listing(
    [
        file_item("Roadmap.pdf", "01ROADMAPAAAAAAAAAAAAAAAAAAAAAAAAA", size=5120),
        folder_item("Roadmap assets"),
        file_item(
            "roadmap-notes.txt",
            "01NOTESBBBBBBBBBBBBBBBBBBBBBBBBBBB",
            file={"mimeType": "text/plain"},
            lastModifiedDateTime="2026-10-01T09:30:00Z",
        ),
    ]
)


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


def _be_search_one_drive(payload: dict[str, Any]) -> list[dict[str, Any]]:
    out = []
    for node in _be_iter_dicts(payload.get("items")):
        # Files carry a `file` facet (with mimeType); folders carry `folder`.
        if "id" in node and "name" in node and isinstance(node.get("file"), dict):
            out.append(
                _be_candidate(
                    str(node["id"]),
                    str(node["name"]),
                    node["file"].get("mimeType"),
                    node.get("size") if isinstance(node.get("size"), int) else None,
                    node.get("lastModifiedDateTime"),
                )
            )
    return out


def _be_one_drive_download_args(meta: dict[str, Any]) -> dict[str, Any]:
    return {
        "item_id": meta["source_id"],
        "file_name": meta.get("source_name") or meta.get("final_name") or "download",
    }


def _be_extract_inline_bytes(payload: dict[str, Any]) -> bytes:
    for node in _be_iter_dicts(payload.get("items")):
        raw = node.get("file_content_bytes")
        if isinstance(raw, str) and raw:
            return base64.b64decode(raw, validate=False)
    raise ValueError("no s3url or file_content_bytes in download response")


# ------------------------------------------------------------------------------


def _fetch_payload(data: Any) -> dict[str, Any]:
    """What /fetch hands the BE: the tool's data as the result item."""
    return {"items": [data]}


class TestSearchItems:
    def test_accepts_the_arguments_be_sends(self):
        h = harness(response(200, SEARCH_REPLY))
        result = h.call("ONE_DRIVE_SEARCH_ITEMS", {"q": "roadmap", "top": BE_SEARCH_TOP})
        assert result.successful, result.error
        call = h.session.last
        assert (call.method, call.path) == ("GET", "/me/drive/root/search(q='roadmap')")
        assert call.params == {"$top": BE_SEARCH_TOP}

    def test_result_has_the_fields_be_reads(self):
        h = harness(response(200, SEARCH_REPLY))
        data = h.call("ONE_DRIVE_SEARCH_ITEMS", {"q": "roadmap", "top": BE_SEARCH_TOP}).data
        assert _be_search_one_drive(_fetch_payload(data)) == [
            {
                "file_id": "01ROADMAPAAAAAAAAAAAAAAAAAAAAAAAAA",
                "name": "Roadmap.pdf",
                "mime_type": "application/pdf",
                "size": 5120,
                "modified": "2026-09-30T12:00:00Z",
            },
            {
                "file_id": "01NOTESBBBBBBBBBBBBBBBBBBBBBBBBBBB",
                "name": "roadmap-notes.txt",
                "mime_type": "text/plain",
                "size": 2048,
                "modified": "2026-10-01T09:30:00Z",
            },
        ]


class TestDownloadFile:
    def test_accepts_the_arguments_be_sends(self):
        args = _be_one_drive_download_args({"source_id": "01ROADMAPAAAAAAAAAAAAAAAAAAAAAAAAA", "source_name": "Roadmap.pdf"})
        h = harness(
            response(200, file_item("Roadmap.pdf", "01ROADMAPAAAAAAAAAAAAAAAAAAAAAAAAA")),
            response(200, content=b"%PDF-1.7 roadmap", content_type="application/pdf"),
        )
        result = h.call("ONE_DRIVE_DOWNLOAD_FILE", args)
        assert result.successful, result.error
        assert [(c.method, c.path) for c in h.session.calls] == [
            ("GET", "/me/drive/items/01ROADMAPAAAAAAAAAAAAAAAAAAAAAAAAA"),
            ("GET", "/me/drive/items/01ROADMAPAAAAAAAAAAAAAAAAAAAAAAAAA/content"),
        ]

    def test_bytes_and_name_reach_the_caller(self):
        payload = bytes(range(256)) * 600  # 153,600 bytes, not UTF-8
        h = harness(
            response(200, file_item("Roadmap.pdf", "01ROADMAPAAAAAAAAAAAAAAAAAAAAAAAAA", size=len(payload))),
            response(200, content=payload, content_type="application/pdf"),
        )
        result = h.call("ONE_DRIVE_DOWNLOAD_FILE", {"item_id": "01ROADMAPAAAAAAAAAAAAAAAAAAAAAAAAA", "file_name": "Roadmap.pdf"})
        (envelope,) = result.files
        assert envelope.data == payload
        assert envelope.content_type == "application/pdf"
        assert envelope.metadata["filename"] == "Roadmap.pdf"
        assert (result.data["name"], result.data["mimetype"], result.data["size"]) == ("Roadmap.pdf", "application/pdf", len(payload))
        assert "content" not in result.data  # binary: never inlined as text
        # pipeAAS-style staging: the file rides as file_content_bytes in the item.
        staged = {**result.data, "file_content_bytes": base64.b64encode(envelope.data).decode()}
        assert _be_extract_inline_bytes(_fetch_payload(staged)) == payload

    def test_search_hit_round_trips_into_a_download(self):
        h = harness(
            response(200, SEARCH_REPLY),
            response(200, file_item("Roadmap.pdf", "01ROADMAPAAAAAAAAAAAAAAAAAAAAAAAAA")),
            response(200, content=b"%PDF-1.7", content_type="application/pdf"),
        )
        found = h.call("ONE_DRIVE_SEARCH_ITEMS", {"q": "roadmap", "top": BE_SEARCH_TOP})
        candidate = _be_search_one_drive(_fetch_payload(found.data))[0]
        meta = {"source_id": candidate["file_id"], "source_name": candidate["name"]}
        download = h.call("ONE_DRIVE_DOWNLOAD_FILE", _be_one_drive_download_args(meta))
        assert download.files[0].data == b"%PDF-1.7"
        assert h.session.last.path == "/me/drive/items/01ROADMAPAAAAAAAAAAAAAAAAAAAAAAAAA/content"
