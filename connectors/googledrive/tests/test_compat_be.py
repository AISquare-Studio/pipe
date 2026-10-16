"""Compatibility with the BE's Content Vault import, which calls ``/fetch``
with Composio slugs (AISquare-Studio-BE
``aisquare_studio/content_vault/connector_import.py``):

* search: ``GOOGLEDRIVE_FIND_FILE`` with ``{"q": "name contains '<query>'
  and trashed = false", "pageSize": 10}``; it walks every dict under the
  fetch payload's ``items`` and keeps those with ``id``, ``name`` and
  ``mimeType`` (folders skipped), reading ``size`` (a digit string) and
  ``modifiedTime``.
* metadata: ``GOOGLEDRIVE_GET_FILE_METADATA`` with ``{"file_id": <id>}`` —
  snake_case, although Composio's schema says ``fileId`` — reading the dict
  whose ``id`` is that id for ``mimeType`` and ``name``.
* download: ``GOOGLEDRIVE_DOWNLOAD_FILE`` with ``{"file_id": <id>}`` plus
  ``"mime_type": <export mime>`` for Google-native files; it needs the bytes.

The BE helpers are copied here verbatim (``_be_*``), as of the BE revision
read for this PR. This file fails when the toolkit stops accepting those
arguments or stops returning those fields. It cannot see a later BE change,
so update the copies when ``connector_import.py`` changes. The bytes travel in ``ToolResult.files``; the pipeAAS
``/fetch`` route serializes them (``s3url`` / ``file_content_bytes`` are what
the BE's ``_extract_bytes`` reads) — that mapping is pipeAAS's side of the
contract.
"""

from __future__ import annotations

from typing import Any

from aisquare_pipe_googledrive.tools import FILE_FIELDS

from .fakes import file_meta, harness, response

DOC = "application/vnd.google-apps.document"
SHEET = "application/vnd.google-apps.spreadsheet"
FOLDER = "application/vnd.google-apps.folder"

FIND_REPLY = {
    "kind": "drive#fileList",
    "incompleteSearch": False,
    "files": [
        file_meta("1RoadmapPdf", "Roadmap 2026.pdf", "application/pdf", 48213, modifiedTime="2026-09-30T10:00:00.000Z"),
        file_meta("1RoadmapDoc", "Roadmap", DOC, None, modifiedTime="2026-10-01T08:30:00.000Z"),
        file_meta("1RoadmapDir", "Roadmap assets", FOLDER, None),
    ],
}

# ---- mirrored from connector_import.py --------------------------------------

_GOOGLE_NATIVE_EXPORTS = {
    "application/vnd.google-apps.document": ("application/pdf", ".pdf"),
    "application/vnd.google-apps.presentation": ("application/pdf", ".pdf"),
    "application/vnd.google-apps.spreadsheet": (
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        ".xlsx",
    ),
}


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
    escaped = query.replace("\\", "\\\\").replace("'", "\\'")
    return {"q": f"name contains '{escaped}' and trashed = false", "pageSize": 10}


def _be_search_googledrive(payload: dict[str, Any]) -> list[dict[str, Any]]:
    out = []
    for node in _be_iter_dicts(payload.get("items")):
        if "id" in node and "name" in node and "mimeType" in node:
            if node.get("mimeType") == "application/vnd.google-apps.folder":
                continue
            size = node.get("size")
            out.append(
                _be_candidate(
                    str(node["id"]),
                    str(node["name"]),
                    node.get("mimeType"),
                    int(size) if str(size or "").isdigit() else None,
                    node.get("modifiedTime"),
                )
            )
    return out


def _be_metadata(payload: dict[str, Any], file_id: str, chosen: dict[str, Any]) -> dict[str, Any]:
    for node in _be_iter_dicts(payload.get("items")):
        if node.get("id") == file_id and "mimeType" in node:
            chosen["mime_type"] = node.get("mimeType") or ""
            chosen["name"] = node.get("name") or chosen["name"]
            break
    return chosen


def _be_download_args(meta: dict[str, Any]) -> dict[str, Any]:
    return {
        "file_id": meta["source_id"],
        **({"mime_type": meta["export_mime"]} if meta.get("export_mime") else {}),
    }


# ------------------------------------------------------------------------------


def _fetch_payload(data: Any) -> dict[str, Any]:
    """What /fetch hands the BE: the tool's data as the result item."""
    return {"items": [data]}


class TestFindFile:
    def test_accepts_the_arguments_be_sends(self):
        h = harness(response(200, FIND_REPLY))
        result = h.call("GOOGLEDRIVE_FIND_FILE", _be_search_args("Ada's roadmap"))
        assert result.successful, result.error
        call = h.session.last
        assert (call.method, call.path) == ("GET", "/files")
        assert call.params["q"] == "name contains 'Ada\\'s roadmap' and trashed = false"
        assert call.params["pageSize"] == 10
        assert call.params["fields"] == f"nextPageToken,incompleteSearch,files({FILE_FIELDS})"

    def test_result_has_the_fields_be_reads(self):
        h = harness(response(200, FIND_REPLY))
        data = h.call("GOOGLEDRIVE_FIND_FILE", _be_search_args("roadmap")).data
        assert _be_search_googledrive(_fetch_payload(data)) == [
            {"file_id": "1RoadmapPdf", "name": "Roadmap 2026.pdf", "mime_type": "application/pdf", "size": 48213, "modified": "2026-09-30T10:00:00.000Z"},
            {"file_id": "1RoadmapDoc", "name": "Roadmap", "mime_type": DOC, "size": None, "modified": "2026-10-01T08:30:00.000Z"},
        ]

    def test_default_fields_include_what_be_reads(self):
        for field in ("id", "name", "mimeType", "size", "modifiedTime"):
            assert field in FILE_FIELDS.split(",")


class TestGetFileMetadata:
    def test_snake_case_file_id_is_accepted(self):
        h = harness(response(200, file_meta("1RoadmapDoc", "Roadmap", DOC, None)))
        result = h.call("GOOGLEDRIVE_GET_FILE_METADATA", {"file_id": "1RoadmapDoc"})
        assert result.successful, result.error
        assert (h.session.last.method, h.session.last.path) == ("GET", "/files/1RoadmapDoc")

    def test_result_has_the_fields_be_reads(self):
        h = harness(response(200, file_meta("1RoadmapDoc", "Roadmap", DOC, None)))
        data = h.call("GOOGLEDRIVE_GET_FILE_METADATA", {"file_id": "1RoadmapDoc"}).data
        chosen = _be_metadata(_fetch_payload(data), "1RoadmapDoc", {"name": "roadmap", "mime_type": ""})
        assert chosen == {"name": "Roadmap", "mime_type": DOC}


class TestDownloadFile:
    def test_stored_file(self):
        args = _be_download_args({"source_id": "1RoadmapPdf"})
        assert args == {"file_id": "1RoadmapPdf"}
        h = harness(
            response(200, file_meta("1RoadmapPdf", "Roadmap 2026.pdf", "application/pdf", 9)),
            response(200, content=b"%PDF-1.7\n", content_type="application/pdf"),
        )
        result = h.call("GOOGLEDRIVE_DOWNLOAD_FILE", args)
        assert result.successful, result.error
        assert (h.session.last.path, h.session.last.params) == ("/files/1RoadmapPdf", {"alt": "media", "supportsAllDrives": "true"})
        (envelope,) = result.files
        assert envelope.data == b"%PDF-1.7\n"
        assert envelope.content_type == "application/pdf"
        assert envelope.metadata["filename"] == "Roadmap 2026.pdf"
        assert (result.data["name"], result.data["mimetype"], result.data["size"]) == ("Roadmap 2026.pdf", "application/pdf", 9)

    def test_google_native_file_is_exported_to_the_mime_be_asks_for(self):
        for native, (export_mime, ext) in _GOOGLE_NATIVE_EXPORTS.items():
            args = _be_download_args({"source_id": "1Native", "export_mime": export_mime})
            assert args == {"file_id": "1Native", "mime_type": export_mime}
            h = harness(response(200, file_meta("1Native", "Roadmap", native, None)), response(200, content=b"exported"))
            result = h.call("GOOGLEDRIVE_DOWNLOAD_FILE", args)
            assert result.successful, result.error
            assert (h.session.last.path, h.session.last.params) == ("/files/1Native/export", {"mimeType": export_mime})
            assert result.files[0].data == b"exported"
            assert result.files[0].content_type == export_mime
            assert result.data["name"] == "Roadmap" + ext

    def test_search_hit_round_trips_into_a_download(self):
        h = harness(
            response(200, FIND_REPLY),
            response(200, file_meta("1RoadmapDoc", "Roadmap", DOC, None)),
            response(200, file_meta("1RoadmapDoc", "Roadmap", DOC, None)),
            response(200, content=b"%PDF doc"),
        )
        found = h.call("GOOGLEDRIVE_FIND_FILE", _be_search_args("roadmap"))
        candidate = _be_search_googledrive(_fetch_payload(found.data))[1]
        chosen = _be_metadata(
            _fetch_payload(h.call("GOOGLEDRIVE_GET_FILE_METADATA", {"file_id": candidate["file_id"]}).data),
            candidate["file_id"],
            {"name": candidate["name"], "mime_type": ""},
        )
        export_mime = _GOOGLE_NATIVE_EXPORTS[chosen["mime_type"]][0]
        download = h.call("GOOGLEDRIVE_DOWNLOAD_FILE", _be_download_args({"source_id": candidate["file_id"], "export_mime": export_mime}))
        assert download.files[0].data == b"%PDF doc"
        assert h.session.last.path == "/files/1RoadmapDoc/export"
