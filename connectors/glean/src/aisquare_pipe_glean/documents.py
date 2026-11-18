"""Glean's search results and documents as plain records.

Glean's shapes (Client API models): a search result carries ``title``, ``url``,
``snippets[].snippet`` and ``document`` (``id``, ``datasource``, ``docType``,
``metadata``). A document from ``getdocuments`` carries ``id``, ``title``,
``url``, ``datasource``, ``content.fullTextList`` and ``metadata``
(``updateTime``, ``createTime``, ``mimeType``, ``author.name``). An entry of
``getdocuments``'s ``documents`` map that could not be read is
``{"error": "<reason>"}``.

``update_time`` (Glean's ``metadata.updateTime``) is the version marker: when
it changes, the document changed.
"""

from __future__ import annotations

from typing import Any


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _str(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _common(doc: dict[str, Any], meta: dict[str, Any]) -> dict[str, Any]:
    return {
        "document_id": _str(doc.get("id")) or _str(meta.get("documentId")),
        "title": _str(doc.get("title")),
        "url": _str(doc.get("url")),
        "datasource": _str(doc.get("datasource")) or _str(meta.get("datasource")),
        "doc_type": _str(doc.get("docType")),
        "mime_type": _str(meta.get("mimeType")),
        "update_time": _str(meta.get("updateTime")),
        "create_time": _str(meta.get("createTime")),
        "author": _str(_dict(meta.get("author")).get("name")),
    }


def search_hit(result: Any) -> dict[str, Any]:
    """One search result as ``{document_id, title, url, datasource, doc_type,
    mime_type, update_time, create_time, author, snippets}``."""
    result = _dict(result)
    doc = _dict(result.get("document"))
    record = _common(doc, _dict(doc.get("metadata")))
    record["title"] = _str(result.get("title")) or record["title"]
    record["url"] = _str(result.get("url")) or record["url"]
    record["snippets"] = [
        s["snippet"]
        for s in result.get("snippets") or []
        if isinstance(s, dict) and isinstance(s.get("snippet"), str) and s["snippet"]
    ]
    return record


def document_record(doc: Any) -> dict[str, Any]:
    """One ``getdocuments`` document as the common fields plus ``text`` (the
    ``content.fullTextList`` lines joined by newlines; empty when Glean sent
    no content)."""
    doc = _dict(doc)
    record = _common(doc, _dict(doc.get("metadata")))
    lines = _dict(doc.get("content")).get("fullTextList")
    record["text"] = (
        "\n".join(x for x in lines if isinstance(x, str))
        if isinstance(lines, list)
        else ""
    )
    return record


def split_documents(
    response: Any,
) -> tuple[list[dict[str, Any]], list[tuple[str, str]]]:
    """``getdocuments``'s ``documents`` map as (readable documents, failures).
    A failure is ``(key, reason)`` for an entry Glean answered with
    ``{"error": ...}`` or with something that is not a document."""
    documents = _dict(_dict(response).get("documents"))
    readable: list[dict[str, Any]] = []
    failed: list[tuple[str, str]] = []
    for key, entry in documents.items():
        entry = _dict(entry)
        if "error" in entry and not entry.get("id"):
            failed.append((str(key), str(entry.get("error") or "unreadable")))
        elif entry.get("id") or entry.get("url"):
            readable.append(entry)
        else:
            failed.append((str(key), "not a document"))
    return readable, failed
