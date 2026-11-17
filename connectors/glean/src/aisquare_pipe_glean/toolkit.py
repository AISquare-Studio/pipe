"""GleanToolkit: read-only search and document reads over Glean's Client API.

``config`` is the tenant's Glean credential blob (``host``, ``token``,
``token_kind``, ``act_as``, ``allowed_hosts``; see :mod:`.credentials`). Hosts
build it server-side; no tool argument can set the host, the token or the user
Glean reads as. There is no OAuth: Glean tokens are issued by a Glean admin,
so a host classifies this toolkit as a per-tenant credential (first party).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from aisquare.pipe import (
    ERROR_FORBIDDEN,
    ERROR_INVALID_ARGUMENTS,
    ERROR_NOT_CONNECTED,
    ERROR_NOT_FOUND,
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

from aisquare_pipe_glean.client import GleanAPIError, GleanClient
from aisquare_pipe_glean.credentials import GleanNotConfigured, load_credentials
from aisquare_pipe_glean.documents import document_record, search_hit, split_documents

MAX_PAGE_SIZE = 50
MAX_QUERY_CHARS = 1000
MAX_DATASOURCES = 20
DEFAULT_MAX_CHARS = 20_000
MAX_CHARS = 200_000

SEARCH = ToolSpec(
    slug="GLEAN_SEARCH",
    description=(
        "Search the company's Glean index for documents matching a natural-language query: "
        "policies, handbooks, wiki pages, specs and files from every app Glean connects "
        "(Google Drive, Confluence, SharePoint, Slack, ...), as the configured Glean user. "
        "Returns each hit's title, URL, datasource, snippets, document_id and update_time "
        "(the version marker: it changes when the document is edited). Read a hit's full "
        "text with GLEAN_GET_DOCUMENT. Pass next_cursor back as cursor for more results."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "What to look for, e.g. 'travel reimbursement policy'.",
            },
            "page_size": {
                "type": "integer",
                "minimum": 1,
                "maximum": MAX_PAGE_SIZE,
                "default": 10,
                "description": f"Results per page, 1-{MAX_PAGE_SIZE}.",
            },
            "cursor": {
                "type": "string",
                "description": "next_cursor from a previous GLEAN_SEARCH with the same query.",
            },
            "datasources": {
                "type": "array",
                "items": {"type": "string"},
                "maxItems": MAX_DATASOURCES,
                "description": "Only these Glean datasources, e.g. ['confluence', 'gdrive'].",
            },
        },
        "required": ["query"],
        "additionalProperties": False,
    },
    read_only=True,
    tags=(
        PRIMARY_TAG,
        "search",
        "enterprise search",
        "documents",
        "policies",
        "knowledge base",
    ),
)

GET_DOCUMENT = ToolSpec(
    slug="GLEAN_GET_DOCUMENT",
    description=(
        "Read one document from Glean by its document_id (from GLEAN_SEARCH) or its URL: "
        "the full plain text plus title, URL, datasource, mime type, author, create_time and "
        "update_time (the version marker). Give exactly one of document_id and url. Long "
        "documents are cut at max_chars; truncated says so."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "document_id": {"type": "string", "description": "The Glean document id."},
            "url": {
                "type": "string",
                "description": "The document's URL as Glean indexes it.",
            },
            "max_chars": {
                "type": "integer",
                "minimum": 1,
                "maximum": MAX_CHARS,
                "default": DEFAULT_MAX_CHARS,
                "description": f"Longest text returned, 1-{MAX_CHARS} characters.",
            },
        },
        "additionalProperties": False,
    },
    read_only=True,
    tags=("read", "document", "full text", "content", "policy document"),
)

TOOL_SPECS = (SEARCH, GET_DOCUMENT)


def _describe(e: GleanAPIError) -> str:
    if e.status == 401:
        return (
            f"{e.message} — Glean rejected the token (invalid, expired or revoked) or the "
            "act_as email; the tenant's Glean credential must be fixed"
        )
    if e.status == 403:
        return (
            f"{e.message} — the Glean token lacks the scope this needs (SEARCH to search, "
            "DOCUMENTS to read documents), or the Glean user cannot see it"
        )
    if e.status == 400 and "actas" in e.message.lower().replace("-", ""):
        return (
            f"{e.message} — this is a global token: set token_kind 'global' and act_as"
        )
    return e.message


def failure(e: GleanAPIError) -> ToolResult:
    """A Glean failure as a canonical ``ToolResult`` error."""
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


class GleanToolkit(Toolkit):
    """Glean enterprise search, read-only: ``GLEAN_SEARCH`` (primary) and
    ``GLEAN_GET_DOCUMENT``. ``session`` (tests, or a host that pools
    connections) is the ``requests.Session`` every call goes through."""

    name = "glean"
    version = "0.1.0"
    display_name = "Glean"
    description = (
        "Glean enterprise search: find company documents (policies, handbooks, wiki pages, "
        "specs, files) across every app Glean indexes, and read a document's full text."
    )
    auth_type = AuthType.API_KEY
    oauth = None
    docs_url = "https://developers.glean.com/api/client-api/search/search"
    category = "docs"
    keywords = (
        "glean",
        "enterprise search",
        "company search",
        "search documents",
        "policy",
        "policies",
        "handbook",
        "wiki",
        "knowledge base",
        "intranet",
        "documentation",
    )
    examples = (
        "search glean for the travel reimbursement policy",
        "find our data retention policy document",
        "read the employee handbook page about PTO",
    )

    def __init__(self, *, session: Any = None) -> None:
        self._session = session

    def tools(self) -> list[ToolSpec]:
        return list(TOOL_SPECS)

    def validate_config(self, config: dict[str, Any]) -> bool:
        try:
            load_credentials(config)
        except GleanNotConfigured:
            return False
        return True

    def call_tool(
        self,
        slug: str,
        arguments: Mapping[str, Any] | None,
        config: Mapping[str, Any] | None,
    ) -> ToolResult:
        """Core's ``call_tool``, except that a known tool on an unusable
        credential fails with why Glean is not configured (no request is made)."""
        if isinstance(slug, str) and self.get_tool(slug) is not None:
            try:
                load_credentials(config)
            except GleanNotConfigured as e:
                return ToolResult.fail(
                    f"Glean is not configured: {e}", code=ERROR_NOT_CONNECTED
                )
            except Exception:  # noqa: BLE001 — call_tool never raises
                return ToolResult.fail(
                    "Glean is not configured", code=ERROR_NOT_CONNECTED
                )
        return super().call_tool(slug, arguments, config)

    def run_tool(
        self, tool: ToolSpec, arguments: dict[str, Any], config: dict[str, Any]
    ) -> ToolResult:
        creds = load_credentials(config)
        with GleanClient(
            creds, session=self._session, budget=time_budget(config)
        ) as client:
            try:
                if tool.slug == SEARCH.slug:
                    return self._search(client, arguments)
                if tool.slug == GET_DOCUMENT.slug:
                    return self._get_document(client, arguments)
            except GleanAPIError as e:
                return failure(e)
        raise ToolError(f"{tool.slug} is not implemented")

    def _search(self, client: GleanClient, arguments: dict[str, Any]) -> ToolResult:
        query = arguments["query"].strip()
        if not query or len(query) > MAX_QUERY_CHARS:
            raise ToolError(
                f"query must be 1-{MAX_QUERY_CHARS} characters",
                code=ERROR_INVALID_ARGUMENTS,
            )
        page_size = _bounded_int(arguments, "page_size", 1, MAX_PAGE_SIZE)
        datasources = [d.strip() for d in arguments.get("datasources") or []]
        if len(datasources) > MAX_DATASOURCES or not all(datasources):
            raise ToolError(
                f"datasources must be up to {MAX_DATASOURCES} non-empty names",
                code=ERROR_INVALID_ARGUMENTS,
            )
        response = client.search(
            query,
            page_size=page_size,
            cursor=arguments.get("cursor") or None,
            datasources=datasources,
        )
        results = response.get("results")
        hits = [search_hit(r) for r in results] if isinstance(results, list) else []
        more = response.get("hasMoreResults") is True
        cursor = (
            response.get("cursor") if isinstance(response.get("cursor"), str) else None
        )
        return ToolResult.ok(
            {
                "results": hits,
                "has_more": more and bool(cursor),
                "next_cursor": cursor if more else None,
            }
        )

    def _get_document(
        self, client: GleanClient, arguments: dict[str, Any]
    ) -> ToolResult:
        doc_id = (arguments.get("document_id") or "").strip()
        url = (arguments.get("url") or "").strip()
        if bool(doc_id) == bool(url):
            raise ToolError(
                "give exactly one of document_id and url", code=ERROR_INVALID_ARGUMENTS
            )
        max_chars = _bounded_int(arguments, "max_chars", 1, MAX_CHARS)
        response = client.get_documents(
            ids=[doc_id] if doc_id else [], urls=[url] if url else []
        )
        readable, failed = split_documents(response)
        if not readable:
            reason = failed[0][1] if failed else "Glean returned no document"
            code = (
                ERROR_FORBIDDEN if "permission" in reason.lower() else ERROR_NOT_FOUND
            )
            raise ToolError(
                f"GLEAN_GET_DOCUMENT: the document was not found, or the Glean user cannot see it "
                f"({reason[:200]})",
                code=code,
            )
        record = document_record(readable[0])
        text = record.pop("text")
        record.update(
            text=text[:max_chars],
            truncated=len(text) > max_chars,
            text_chars=len(text),
        )
        return ToolResult.ok(record)


__all__ = ["GET_DOCUMENT", "SEARCH", "TOOL_SPECS", "GleanToolkit", "failure"]
