"""GleanToolkit: search, read a document, failures, and what callers cannot set."""

from __future__ import annotations

import pytest
import requests
from aisquare.pipe import PRIMARY_TAG
from aisquare.pipe.core.registry import index_for, search_connectors, search_scored

from aisquare_pipe_glean import GleanToolkit
from tests.fakes import (
    BASE,
    GLOBAL_BLOB,
    HOST,
    TOKEN,
    USER_BLOB,
    FakeSession,
    document,
    documents,
    response,
    search_page,
)


def _toolkit(*responses):
    session = FakeSession().queue(*responses)
    return GleanToolkit(session=session), session


# --- catalogue and search ---------------------------------------------------


def test_catalogue_is_read_only_and_search_is_primary():
    tools = {t.slug: t for t in GleanToolkit().tools()}
    assert set(tools) == {"GLEAN_SEARCH", "GLEAN_GET_DOCUMENT"}
    assert all(
        t.read_only and not t.destructive and t.risk == "read" for t in tools.values()
    )
    assert PRIMARY_TAG in tools["GLEAN_SEARCH"].tags
    assert PRIMARY_TAG not in tools["GLEAN_GET_DOCUMENT"].tags
    for tool in tools.values():
        assert tool.input_schema["additionalProperties"] is False
    card = GleanToolkit.card()
    assert (card["name"], card["category"], card["auth_type"]) == (
        "glean",
        "docs",
        "api_key",
    )
    assert GleanToolkit.oauth is None  # a host classifies it as a per-tenant credential


def test_toolkit_is_found_by_search_scored_and_search_connectors():
    assert (
        search_connectors("search glean for the travel reimbursement policy", limit=3)[
            0
        ][0]
        == "glean"
    )
    hits = search_scored("find the company data retention policy document", limit=5)
    assert (hits[0].connector, hits[0].tool.slug) == ("glean", "GLEAN_SEARCH")
    scoped = search_scored(
        "read the full text of a policy document", connectors=["glean"], limit=2
    )
    assert {h.tool.slug for h in scoped} == {"GLEAN_SEARCH", "GLEAN_GET_DOCUMENT"}
    assert {t.slug for t in index_for("glean").tools} == {
        "GLEAN_SEARCH",
        "GLEAN_GET_DOCUMENT",
    }


# --- GLEAN_SEARCH -------------------------------------------------------------


def test_search_returns_hits_with_the_update_time_and_pages():
    kit, session = _toolkit(
        response(200, search_page("d1", "d2", cursor="c-2")),
        response(200, search_page("d3")),
    )
    first = kit.call_tool(
        "GLEAN_SEARCH", {"query": "travel policy", "page_size": 2}, USER_BLOB
    )
    assert first.successful, first.error
    assert [r["document_id"] for r in first.data["results"]] == ["d1", "d2"]
    hit = first.data["results"][0]
    assert hit["update_time"] == "2026-11-01T10:00:00Z"
    assert (hit["title"], hit["datasource"], hit["snippets"]) == (
        "Doc d1",
        "confluence",
        ["snippet of d1"],
    )
    assert (first.data["has_more"], first.data["next_cursor"]) == (True, "c-2")

    last = kit.call_tool(
        "GLEAN_SEARCH",
        {"query": "travel policy", "page_size": 2, "cursor": "c-2"},
        USER_BLOB,
    )
    assert [r["document_id"] for r in last.data["results"]] == ["d3"]
    assert (last.data["has_more"], last.data["next_cursor"]) == (False, None)

    assert [c.endpoint for c in session.calls] == ["search", "search"]
    assert session.calls[0].json == {"query": "travel policy", "pageSize": 2}
    assert session.calls[1].json == {
        "query": "travel policy",
        "pageSize": 2,
        "cursor": "c-2",
    }
    assert all(c.method == "POST" and c.url.startswith(BASE) for c in session.calls)


def test_search_with_datasources_sends_the_required_facet_bucket_size():
    kit, session = _toolkit(response(200, search_page("d1")))
    result = kit.call_tool(
        "GLEAN_SEARCH",
        {"query": "pto", "datasources": ["confluence", "gdrive"]},
        USER_BLOB,
    )
    assert result.successful, result.error
    assert session.calls[0].json == {
        "query": "pto",
        "pageSize": 10,
        "requestOptions": {
            "facetBucketSize": 10,
            "datasourcesFilter": ["confluence", "gdrive"],
        },
    }


@pytest.mark.parametrize(
    "arguments",
    [
        {"query": "   "},
        {"query": "x" * 1001},
        {"query": "q", "page_size": 0},
        {"query": "q", "page_size": 51},
        {"query": "q", "datasources": [""]},
        {"query": "q", "datasources": ["d"] * 21},
    ],
)
def test_search_refuses_out_of_range_arguments_without_a_request(arguments):
    kit, session = _toolkit()
    result = kit.call_tool("GLEAN_SEARCH", arguments, USER_BLOB)
    assert (result.successful, result.error_code) == (False, "invalid_arguments")
    assert session.calls == []


# --- GLEAN_GET_DOCUMENT ---------------------------------------------------------


def test_get_document_returns_text_and_the_version_marker():
    kit, session = _toolkit(response(200, documents(document("d1"))))
    result = kit.call_tool("GLEAN_GET_DOCUMENT", {"document_id": "d1"}, USER_BLOB)
    assert result.successful, result.error
    assert result.data["text"] == "line one of d1\nline two"
    assert result.data["update_time"] == "2026-11-01T10:00:00Z"
    assert (result.data["mime_type"], result.data["truncated"]) == ("text/html", False)
    assert session.calls[0].endpoint == "getdocuments"
    assert session.calls[0].json == {
        "documentSpecs": [{"id": "d1"}],
        "includeFields": ["DOCUMENT_CONTENT"],
    }


def test_get_document_by_url_and_truncation():
    kit, session = _toolkit(response(200, documents(document("d1", text=["a" * 50]))))
    result = kit.call_tool(
        "GLEAN_GET_DOCUMENT",
        {"url": "https://docs.acme.com/d1", "max_chars": 10},
        USER_BLOB,
    )
    assert result.data["text"] == "a" * 10
    assert (result.data["truncated"], result.data["text_chars"]) == (True, 50)
    assert session.calls[0].json["documentSpecs"] == [
        {"url": "https://docs.acme.com/d1"}
    ]


@pytest.mark.parametrize(
    "arguments", [{}, {"document_id": "d1", "url": "https://x"}, {"document_id": " "}]
)
def test_get_document_needs_exactly_one_of_id_and_url(arguments):
    kit, session = _toolkit()
    result = kit.call_tool("GLEAN_GET_DOCUMENT", arguments, USER_BLOB)
    assert (result.successful, result.error_code) == (False, "invalid_arguments")
    assert session.calls == []


def test_get_document_permission_error_entry_is_forbidden():
    kit, _ = _toolkit(
        response(200, documents(errors={"d9": "Permission denied for this user"}))
    )
    result = kit.call_tool("GLEAN_GET_DOCUMENT", {"document_id": "d9"}, USER_BLOB)
    assert (result.successful, result.error_code) == (False, "forbidden")


def test_get_document_error_entry_is_not_found():
    kit, _ = _toolkit(response(200, documents(errors={"d9": "Document not found"})))
    result = kit.call_tool("GLEAN_GET_DOCUMENT", {"document_id": "d9"}, USER_BLOB)
    assert (result.successful, result.error_code) == (False, "not_found")
    assert "Document not found" in result.error


# --- credentials: fail closed, never caller-set -----------------------------------


@pytest.mark.parametrize(
    "config",
    [
        {},
        None,
        {"host": HOST},
        {"token": TOKEN},
        {**USER_BLOB, "host": "evil.com"},
        {**GLOBAL_BLOB, "act_as": None},
    ],
)
def test_without_usable_credentials_every_tool_fails_closed_with_no_request(config):
    for slug, arguments in (
        ("GLEAN_SEARCH", {"query": "q"}),
        ("GLEAN_GET_DOCUMENT", {"document_id": "d"}),
    ):
        kit, session = _toolkit()
        result = kit.call_tool(slug, arguments, config)
        assert (result.successful, result.error_code) == (False, "not_connected"), slug
        assert result.error.startswith("Glean is not configured")
        assert TOKEN not in result.error
        assert session.calls == []
    assert GleanToolkit().validate_config(config or {}) is False


@pytest.mark.parametrize(
    "config",
    [
        {**USER_BLOB, "token": "glean_tok\u20acn"},
        {**GLOBAL_BLOB, "act_as": "r\u00ebader@acme.com"},
        {**USER_BLOB, "host": "169.254.169.254", "allowed_hosts": ["169.254.169.254"]},
    ],
)
def test_a_credential_that_cannot_be_sent_is_not_connected_not_an_internal_error(
    config,
):
    kit, session = _toolkit()
    result = kit.call_tool("GLEAN_SEARCH", {"query": "q"}, config)
    assert (result.successful, result.error_code) == (False, "not_connected")
    assert result.error.startswith("Glean is not configured: ")
    assert session.calls == []


@pytest.mark.parametrize(
    "key", ["host", "token", "token_kind", "act_as", "allowed_hosts", "base_url"]
)
def test_tool_arguments_cannot_set_the_host_or_the_credential(key):
    kit, session = _toolkit()
    result = kit.call_tool("GLEAN_SEARCH", {"query": "q", key: "evil.com"}, USER_BLOB)
    assert (result.successful, result.error_code) == (False, "invalid_arguments")
    assert session.calls == []


def test_global_token_sends_act_as_on_every_call_and_user_token_never_does():
    kit, session = _toolkit(
        response(200, search_page("d1")), response(200, documents(document("d1")))
    )
    kit.call_tool("GLEAN_SEARCH", {"query": "q"}, GLOBAL_BLOB)
    kit.call_tool("GLEAN_GET_DOCUMENT", {"document_id": "d1"}, GLOBAL_BLOB)
    assert [c.headers.get("X-Glean-ActAs") for c in session.calls] == [
        "reader@acme.com"
    ] * 2
    assert all(c.headers["Authorization"] == f"Bearer {TOKEN}" for c in session.calls)

    kit, session = _toolkit(
        response(200, search_page("d1")), response(200, documents(document("d1")))
    )
    kit.call_tool("GLEAN_SEARCH", {"query": "q"}, USER_BLOB)
    kit.call_tool("GLEAN_GET_DOCUMENT", {"document_id": "d1"}, USER_BLOB)
    assert all("X-Glean-ActAs" not in c.headers for c in session.calls)


def test_requests_go_only_to_the_blob_host_and_never_follow_redirects():
    kit, session = _toolkit(response(302, {}, {"Location": "https://evil.com/steal"}))
    result = kit.call_tool("GLEAN_SEARCH", {"query": "q"}, USER_BLOB)
    assert (result.successful, result.error_code) == (False, "upstream_error")
    assert "redirect" in result.error
    assert [c.url for c in session.calls] == [f"https://{HOST}/rest/api/v1/search"]
    assert session.calls[0].allow_redirects is False


# --- failures -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("item", "code", "hint"),
    [
        (
            response(401, {"message": "Invalid Secret"}),
            "unauthorized",
            "rejected the token",
        ),
        (response(403, {"message": "Insufficient scopes"}), "forbidden", "SEARCH"),
        (
            response(400, {"message": "Required header missing: X-Glean-ActAs"}),
            "invalid_arguments",
            "token_kind 'global'",
        ),
        (response(408, {}), "timeout", ""),
        (response(504, {}), "timeout", ""),
        (response(502, {}), "upstream_error", ""),
        (
            response(422, {"message": "Invalid Query"}),
            "invalid_arguments",
            "Invalid Query",
        ),
        (response(500, f"oops {TOKEN}"), "upstream_error", ""),
        (response(200, "not json"), "upstream_error", "not JSON"),
        (requests.Timeout("slow"), "timeout", "did not answer"),
        (requests.ConnectionError("down"), "upstream_error", "could not reach Glean"),
    ],
)
def test_http_failures_map_to_canonical_codes_without_the_token(item, code, hint):
    kit, _ = _toolkit(item)
    result = kit.call_tool("GLEAN_SEARCH", {"query": "q"}, USER_BLOB)
    assert (result.successful, result.error_code) == (False, code)
    assert hint in result.error
    assert TOKEN not in result.error


def test_rate_limit_carries_retry_after():
    kit, _ = _toolkit(
        response(429, {"message": "Too Many Requests"}, {"Retry-After": "7"})
    )
    result = kit.call_tool("GLEAN_SEARCH", {"query": "q"}, USER_BLOB)
    assert (result.error_code, result.retry_after) == ("rate_limited", 7.0)
