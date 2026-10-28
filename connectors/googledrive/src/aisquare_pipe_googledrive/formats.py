"""MIME types Google Drive stores and exports, shared by the toolkit and the
source connector.

Google Docs, Sheets, Slides, Drawings and Apps Script projects
(``application/vnd.google-apps.*``) have no bytes of their own: they are
exported to a real format on download. Folders, shortcuts, Forms, Sites and
Maps cannot be exported at all.
"""

from __future__ import annotations

import mimetypes
import posixpath

DOCUMENT = "application/vnd.google-apps.document"
SPREADSHEET = "application/vnd.google-apps.spreadsheet"
PRESENTATION = "application/vnd.google-apps.presentation"
DRAWING = "application/vnd.google-apps.drawing"
SCRIPT = "application/vnd.google-apps.script"

PDF = "application/pdf"
DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
PPTX = "application/vnd.openxmlformats-officedocument.presentationml.presentation"
SCRIPT_JSON = "application/vnd.google-apps.script+json"

#: Every export target Drive offers (the Composio ``mimeType`` enum).
EXPORT_MIME_TYPES: tuple[str, ...] = (
    DOCX,
    "application/vnd.oasis.opendocument.text",
    "application/rtf",
    PDF,
    "text/plain",
    "application/zip",
    "application/epub+zip",
    "text/markdown",
    XLSX,
    "application/vnd.oasis.opendocument.spreadsheet",
    "text/csv",
    "text/tab-separated-values",
    PPTX,
    "application/vnd.oasis.opendocument.presentation",
    "image/jpeg",
    "image/png",
    "image/svg+xml",
    SCRIPT_JSON,
)

#: Download-tool default per native type (Composio's: PDF, except Sheets,
#: which keep every tab and their cells as XLSX).
TOOL_EXPORTS: dict[str, str] = {
    DOCUMENT: PDF,
    SPREADSHEET: XLSX,
    PRESENTATION: PDF,
    DRAWING: PDF,
    SCRIPT: SCRIPT_JSON,
}

#: Source-connector default per native type: Docs as Word, Sheets as CSV
#: (first tab only — override with XLSX for every tab), Slides as PDF.
SOURCE_EXPORTS: dict[str, str] = {
    DOCUMENT: DOCX,
    SPREADSHEET: "text/csv",
    PRESENTATION: PDF,
    DRAWING: "image/png",
    SCRIPT: SCRIPT_JSON,
}

_EXTENSIONS: dict[str, str] = {
    PDF: ".pdf",
    DOCX: ".docx",
    XLSX: ".xlsx",
    PPTX: ".pptx",
    "application/vnd.oasis.opendocument.text": ".odt",
    "application/vnd.oasis.opendocument.spreadsheet": ".ods",
    "application/vnd.oasis.opendocument.presentation": ".odp",
    "application/rtf": ".rtf",
    "text/plain": ".txt",
    "text/markdown": ".md",
    "text/csv": ".csv",
    "text/tab-separated-values": ".tsv",
    "text/html": ".html",
    "application/zip": ".zip",
    "application/epub+zip": ".epub",
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/svg+xml": ".svg",
    SCRIPT_JSON: ".json",
}

_MIME = mimetypes.MimeTypes()  # stdlib defaults only: same answer on every host
_MIME_OVERRIDES = {".md": "text/markdown", ".markdown": "text/markdown"}


def is_native(mime_type: str | None) -> bool:
    """A Google Workspace type (no bytes of its own; folders included)."""
    return bool(mime_type) and str(mime_type).startswith("application/vnd.google-apps.")


def exported_name(name: str, export_mime: str) -> str:
    """``name`` with the export format's extension (``Roadmap`` -> ``Roadmap.pdf``)."""
    ext = _EXTENSIONS.get(export_mime, "")
    if not ext or name.lower().endswith(ext):
        return name
    return name + ext


def guess_mimetype(name: str, default: str = "application/octet-stream") -> str:
    ext = posixpath.splitext(name)[1].lower()
    return _MIME_OVERRIDES.get(ext) or _MIME.guess_type(name)[0] or default
