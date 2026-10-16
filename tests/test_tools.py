"""Tests for the toolkit abstractions (core/tools.py): ToolSpec/ToolResult,
keyword ranking, argument validation, and Toolkit.call_tool semantics."""

from __future__ import annotations

import dataclasses

import pytest

from aisquare.pipe import (
    ERROR_CODES,
    ERROR_CONFLICT,
    ERROR_FORBIDDEN,
    ERROR_INVALID_ARGUMENTS,
    ERROR_NOT_FOUND,
    ERROR_RATE_LIMITED,
    ERROR_TIMEOUT,
    ERROR_UNAUTHORIZED,
    ERROR_UNKNOWN_TOOL,
    ERROR_UPSTREAM_ERROR,
    AuthType,
    DataEnvelope,
    OAuthError,
    Toolkit,
    ToolResult,
    ToolSpec,
    error_code_for_status,
    rank_tools,
    validate_arguments,
)
from aisquare.pipe.core.tools import default_slug_prefix, prepare_arguments, query_terms
from aisquare.pipe.errors import ToolError
from aisquare.pipe.testing.mock_connectors import (
    MOCK_EXPIRED_TOKEN,
    MOCK_OAUTH,
    MOCK_RATE_LIMITED_TOKEN,
    MockToolkit,
)

EMPTY_SCHEMA = {"type": "object", "properties": {}}
TOKEN = {"access_token": "tok-secret-123456"}


def spec(slug: str, description: str = "does a thing", **kwargs) -> ToolSpec:
    kwargs.setdefault("input_schema", EMPTY_SCHEMA)
    kwargs.setdefault("read_only", True)
    return ToolSpec(slug=slug, description=description, **kwargs)


# --------------------------------------------------------------------------
# ToolSpec / ToolResult
# --------------------------------------------------------------------------


class TestToolSpec:
    def test_to_llm_tool_shape(self):
        schema = {"type": "object", "properties": {"q": {"type": "string"}}, "required": ["q"]}
        tool = spec("X_SEARCH", "Search things", input_schema=schema)
        assert tool.to_llm_tool() == {
            "name": "X_SEARCH",
            "description": "Search things",
            "input_schema": schema,
        }

    def test_to_llm_tool_is_a_copy(self):
        schema = {"type": "object", "properties": {"q": {"type": "string"}}}
        tool = spec("X_SEARCH", input_schema=schema)
        tool.to_llm_tool()["input_schema"]["properties"]["q"]["type"] = "integer"
        assert schema["properties"]["q"]["type"] == "string"

    def test_to_dict_carries_flags(self):
        tool = spec("X_DELETE", read_only=False, destructive=True, tags=["files"])
        assert tool.tags == ("files",)
        assert tool.to_dict() == {
            "slug": "X_DELETE",
            "description": "does a thing",
            "input_schema": EMPTY_SCHEMA,
            "read_only": False,
            "destructive": True,
            "tags": ["files"],
        }

    def test_frozen_and_hashable(self):
        tool = spec("X_GET")
        with pytest.raises(dataclasses.FrozenInstanceError):
            tool.slug = "X_OTHER"  # type: ignore[misc]
        assert hash(tool) == hash(spec("X_GET"))


class TestToolResult:
    def test_to_dict_excludes_files(self):
        envelope = DataEnvelope(content_type="text/plain", data=b"hi", source_id="x")
        result = ToolResult.ok({"size": 2}, files=[envelope])
        assert result.to_dict() == {
            "successful": True,
            "data": {"size": 2},
            "error": None,
            "error_code": None,
            "retry_after": None,
        }
        assert result.files == [envelope]

    def test_fail_defaults_to_upstream_error(self):
        assert ToolResult.fail("boom").to_dict() == {
            "successful": False,
            "data": None,
            "error": "boom",
            "error_code": "upstream_error",
            "retry_after": None,
        }

    def test_fail_with_code(self):
        result = ToolResult.fail("slow down", code=ERROR_RATE_LIMITED, retry_after=12.5)
        assert (result.error_code, result.retry_after) == ("rate_limited", 12.5)


class TestErrorCodes:
    def test_canonical_set(self):
        assert ERROR_CODES == {
            "unauthorized",
            "forbidden",
            "not_found",
            "rate_limited",
            "invalid_arguments",
            "unknown_tool",
            "conflict",
            "upstream_error",
            "timeout",
        }

    @pytest.mark.parametrize(
        ("status", "code"),
        [
            (400, ERROR_INVALID_ARGUMENTS),
            (401, ERROR_UNAUTHORIZED),
            (403, ERROR_FORBIDDEN),
            (404, ERROR_NOT_FOUND),
            (408, ERROR_TIMEOUT),
            (409, ERROR_CONFLICT),
            (410, ERROR_NOT_FOUND),
            (412, ERROR_CONFLICT),
            (422, ERROR_INVALID_ARGUMENTS),
            (429, ERROR_RATE_LIMITED),
            (500, ERROR_UPSTREAM_ERROR),
            (502, ERROR_UPSTREAM_ERROR),
            (504, ERROR_TIMEOUT),
            (418, ERROR_UPSTREAM_ERROR),
        ],
    )
    def test_error_code_for_status(self, status, code):
        assert error_code_for_status(status) == code

    def test_tool_error_carries_code(self):
        err = ToolError("gone", code=ERROR_NOT_FOUND)
        assert (str(err), err.code, err.retry_after) == ("gone", "not_found", None)
        assert ToolError("x").code == ERROR_UPSTREAM_ERROR

    def test_tool_error_rejects_non_canonical_code(self):
        with pytest.raises(ValueError, match="not one of"):
            ToolError("x", code="auth_failed")


# --------------------------------------------------------------------------
# Ranking
# --------------------------------------------------------------------------

GITHUB_LIKE = [
    spec("GITHUB_CREATE_ISSUE", "Create a new issue in a repository."),
    spec("GITHUB_LIST_REPOSITORIES_FOR_USER", "List public repositories for the specified user."),
    spec(
        "GITHUB_LIST_REPOSITORIES_FOR_THE_AUTHENTICATED_USER",
        "List repositories that the authenticated user has explicit permission to access.",
    ),
    spec("GITHUB_SEARCH_CODE", "Search for code across all repositories."),
    spec("GITHUB_GET_RAW_REPOSITORY_CONTENT", "Get the raw content of a file in a repository."),
    spec("GITHUB_LIST_PULL_REQUESTS", "List pull requests in a repository.", tags=("pulls",)),
]


class TestQueryTerms:
    def test_drops_stopwords_and_short_words(self):
        assert query_terms("what are the issues in it") == {"issues", "issue"}

    def test_plural_strip(self):
        assert {"repos", "repo"} <= query_terms("repos")

    def test_first_person_adds_authenticated(self):
        assert "authenticated" in query_terms("list my repos")
        assert "authenticated" in query_terms("issues assigned to me")
        assert "authenticated" not in query_terms("list repos for octocat")


class TestRankTools:
    def test_slug_match_outranks_description_match(self):
        tools = [
            spec("X_ALPHA", "search inside things"),  # description only: +1
            spec("X_SEARCH_THINGS", "unrelated"),  # slug: +2
        ]
        assert [t.slug for t in rank_tools(tools, "search")] == ["X_SEARCH_THINGS", "X_ALPHA"]

    def test_search_code(self):
        top = rank_tools(GITHUB_LIKE, "search code for a function")[0]
        assert top.slug == "GITHUB_SEARCH_CODE"

    def test_first_person_prefers_authenticated_variant(self):
        top = rank_tools(GITHUB_LIKE, "list my repositories")[0]
        assert top.slug == "GITHUB_LIST_REPOSITORIES_FOR_THE_AUTHENTICATED_USER"

    def test_third_person_prefers_for_user_variant(self):
        top = rank_tools(GITHUB_LIKE, "list repositories for user octocat")[0]
        assert top.slug == "GITHUB_LIST_REPOSITORIES_FOR_USER"

    def test_tags_count_like_description(self):
        tools = [spec("X_ONE", "nothing"), spec("X_TWO", "nothing", tags=("pulls",))]
        assert rank_tools(tools, "pulls")[0].slug == "X_TWO"

    def test_ties_keep_catalog_order(self):
        tools = [spec("X_LIST_A"), spec("X_LIST_B"), spec("X_LIST_C")]
        assert [t.slug for t in rank_tools(tools, "list")] == ["X_LIST_A", "X_LIST_B", "X_LIST_C"]

    def test_only_matches_returned_when_any_match(self):
        result = rank_tools(GITHUB_LIKE, "pull requests", limit=10)
        assert result[0].slug == "GITHUB_LIST_PULL_REQUESTS"
        assert all("PULL" in t.slug or "pull" in t.description.lower() for t in result)

    def test_no_match_falls_back_to_catalog_head(self):
        result = rank_tools(GITHUB_LIKE, "zzzz qqqq", limit=2)
        assert [t.slug for t in result] == [t.slug for t in GITHUB_LIKE[:2]]

    @pytest.mark.parametrize("query", [None, "", "   "])
    def test_empty_query_returns_head(self, query):
        assert rank_tools(GITHUB_LIKE, query, limit=3) == GITHUB_LIKE[:3]

    def test_limit(self):
        assert len(rank_tools(GITHUB_LIKE, "repository", limit=2)) == 2
        assert len(rank_tools(GITHUB_LIKE, "repository", limit=None)) == 3  # matches only
        assert len(rank_tools(GITHUB_LIKE, "zzzz", limit=None)) == len(GITHUB_LIKE)
        assert rank_tools(GITHUB_LIKE, "repository", limit=0) == []

    def test_deterministic(self):
        first = rank_tools(GITHUB_LIKE, "get raw file content from my repo")
        assert first == rank_tools(GITHUB_LIKE, "get raw file content from my repo")
        assert first[0].slug == "GITHUB_GET_RAW_REPOSITORY_CONTENT"


# --------------------------------------------------------------------------
# Argument validation
# --------------------------------------------------------------------------

SCHEMA = {
    "type": "object",
    "properties": {
        "q": {"type": "string"},
        "per_page": {"type": "integer"},
        "ratio": {"type": "number"},
        "draft": {"type": "boolean"},
        "labels": {"type": "array", "items": {"type": "string"}},
        "state": {"type": "string", "enum": ["open", "closed"]},
        "cursor": {"type": ["string", "null"]},
        "filter": {
            "type": "object",
            "properties": {"owner": {"type": "string"}},
            "required": ["owner"],
        },
    },
    "required": ["q"],
}


class TestValidateArguments:
    def test_valid(self):
        args = {
            "q": "x",
            "per_page": 5,
            "ratio": 0.5,
            "draft": False,
            "labels": ["a"],
            "state": "open",
            "cursor": None,
            "filter": {"owner": "me"},
        }
        assert validate_arguments(SCHEMA, args) == []

    def test_missing_required(self):
        assert validate_arguments(SCHEMA, {}) == ["missing required argument 'q'"]

    @pytest.mark.parametrize(
        ("key", "value", "expected"),
        [
            ("q", 5, "'q' must be string, got integer"),
            ("per_page", "10", "'per_page' must be integer, got string"),
            ("per_page", True, "'per_page' must be integer, got boolean"),
            ("per_page", 1.5, "'per_page' must be integer, got number"),
            ("ratio", "0.5", "'ratio' must be number, got string"),
            ("draft", "yes", "'draft' must be boolean, got string"),
            ("labels", "a", "'labels' must be array, got string"),
            ("filter", [], "'filter' must be object, got array"),
            ("cursor", 3, "'cursor' must be string or null, got integer"),
        ],
    )
    def test_primitive_types(self, key, value, expected):
        assert validate_arguments(SCHEMA, {"q": "x", key: value}) == [expected]

    def test_integral_float_is_integer_and_int_is_number(self):
        assert validate_arguments(SCHEMA, {"q": "x", "per_page": 10.0, "ratio": 2}) == []

    def test_enum(self):
        assert validate_arguments(SCHEMA, {"q": "x", "state": "merged"}) == [
            "'state' must be one of: 'open', 'closed'"
        ]

    def test_enum_does_not_confuse_bool_and_int(self):
        schema = {"type": "object", "properties": {"n": {"enum": [1, 2]}}}
        assert validate_arguments(schema, {"n": True}) != []
        assert validate_arguments(schema, {"n": 1}) == []

    def test_nested_required_and_items(self):
        problems = validate_arguments(SCHEMA, {"q": "x", "filter": {}, "labels": ["a", 3]})
        assert "missing required argument 'filter.owner'" in problems
        assert "'labels[1]' must be string, got integer" in problems

    def test_additional_properties_false(self):
        schema = {**SCHEMA, "additionalProperties": False}
        assert validate_arguments(schema, {"q": "x", "bogus": 1}) == ["unknown argument(s) 'bogus'"]
        # Extra keys pass when the schema does not forbid them.
        assert validate_arguments(SCHEMA, {"q": "x", "bogus": 1}) == []

    def test_non_object_arguments(self):
        assert validate_arguments(SCHEMA, ["q"]) == ["arguments must be an object, got array"]

    def test_messages_never_echo_values(self):
        problems = validate_arguments(SCHEMA, {"q": 12345678, "state": "s3cr3t-value"})
        assert problems
        assert not any("12345678" in p or "s3cr3t-value" in p for p in problems)


class TestPrepareArguments:
    def test_none_for_optional_is_dropped(self):
        args, problems = prepare_arguments(SCHEMA, {"q": "x", "per_page": None})
        assert args == {"q": "x"} and problems == []

    def test_none_kept_when_nullable(self):
        args, problems = prepare_arguments(SCHEMA, {"q": "x", "cursor": None})
        assert args == {"q": "x", "cursor": None} and problems == []

    def test_none_for_required_is_an_error(self):
        _, problems = prepare_arguments(SCHEMA, {"q": None})
        assert problems == ["'q' must be string, got null"]

    def test_defaults_applied(self):
        schema = {"type": "object", "properties": {"limit": {"type": "integer", "default": 10}}}
        assert prepare_arguments(schema, {}) == ({"limit": 10}, [])
        assert prepare_arguments(schema, {"limit": 3}) == ({"limit": 3}, [])

    def test_none_arguments_mean_empty(self):
        assert prepare_arguments(EMPTY_SCHEMA, None) == ({}, [])


# --------------------------------------------------------------------------
# Toolkit
# --------------------------------------------------------------------------


class _Base(Toolkit):
    name = "acme"
    version = "1.0.0"

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict, dict]] = []

    def tools(self) -> list[ToolSpec]:
        return [
            spec(
                "ACME_GET",
                input_schema={
                    "type": "object",
                    "properties": {"id": {"type": "string"}, "limit": {"type": "integer", "default": 5}},
                    "required": ["id"],
                },
            ),
            spec("ACME_BOOM"),
            spec("ACME_TOOL_ERROR"),
            spec("ACME_LEAK"),
            spec("ACME_WRONG_RETURN"),
            spec("ACME_FAIL_NO_MESSAGE"),
            spec("ACME_RATE_LIMITED"),
            spec("ACME_TIMEOUT"),
            spec("ACME_URL_ERROR"),
            spec("ACME_ODD_CODE"),
            spec("ACME_EMPTY_EXCEPTION"),
        ]

    def run_tool(self, tool, arguments, config):
        self.calls.append((tool.slug, arguments, config))
        if tool.slug == "ACME_GET":
            return ToolResult.ok({"id": arguments["id"], "limit": arguments["limit"]})
        if tool.slug == "ACME_BOOM":
            raise RuntimeError("upstream exploded")
        if tool.slug == "ACME_TOOL_ERROR":
            raise ToolError("resource not found", code=ERROR_NOT_FOUND)
        if tool.slug == "ACME_LEAK":
            raise RuntimeError(f"401 for token {config['access_token']}")
        if tool.slug == "ACME_WRONG_RETURN":
            return {"successful": True}
        if tool.slug == "ACME_RATE_LIMITED":
            raise ToolError("slow down", code=ERROR_RATE_LIMITED, retry_after=42.0)
        if tool.slug == "ACME_TIMEOUT":
            raise TimeoutError("read timed out after 30s")
        if tool.slug == "ACME_URL_ERROR":
            raise RuntimeError(
                "404 Client Error for url: https://api.acme.example/search?q=private+memo&page=2"
            )
        if tool.slug == "ACME_ODD_CODE":
            return ToolResult(successful=False, error="odd", error_code="weird")
        if tool.slug == "ACME_EMPTY_EXCEPTION":
            raise RuntimeError()
        return ToolResult(successful=False)


class TestSlugPrefix:
    def test_default_from_name(self):
        assert _Base.slug_prefix == "ACME_"
        assert default_slug_prefix("one_drive") == "ONE_DRIVE_"
        assert default_slug_prefix("microsoft-teams") == "MICROSOFT_TEAMS_"

    def test_explicit_prefix_kept_and_inherited(self):
        class Drive(_Base):
            name = "googledrive"
            slug_prefix = "GDRIVE_"

        class Child(Drive):
            name = "googledrive-v2"

        assert Drive.slug_prefix == "GDRIVE_"
        assert Child.slug_prefix == "GDRIVE_"

    def test_auto_prefix_follows_subclass_name(self):
        class Other(_Base):
            name = "other"

        assert Other.slug_prefix == "OTHER_"
        assert _Base.slug_prefix == "ACME_"


class TestGetAndSearch:
    def test_get_tool_exact_and_case_insensitive(self):
        kit = _Base()
        assert kit.get_tool("ACME_GET").slug == "ACME_GET"
        assert kit.get_tool("acme_get").slug == "ACME_GET"
        assert kit.get_tool("NOPE") is None
        assert kit.get_tool(None) is None  # type: ignore[arg-type]

    def test_search_tools_default_limit(self):
        kit = MockToolkit()
        assert len(kit.search_tools("zzz")) == 6
        assert len(kit.search_tools("item", limit=2)) == 2
        assert kit.search_tools("delete an item")[0].slug == "MOCK_DELETE_ITEM"


class TestCallTool:
    def test_success_applies_defaults(self):
        kit = _Base()
        result = kit.call_tool("ACME_GET", {"id": "7"}, TOKEN)
        assert result.successful and result.data == {"id": "7", "limit": 5}

    def test_success_has_no_error_code(self):
        result = _Base().call_tool("ACME_GET", {"id": "7"}, TOKEN)
        assert result.error_code is None and result.retry_after is None

    def test_unknown_slug(self):
        result = _Base().call_tool("ACME_NOPE", {}, TOKEN)
        assert not result.successful
        assert "unknown tool 'ACME_NOPE'" in (result.error or "")
        assert result.error_code == ERROR_UNKNOWN_TOOL

    def test_invalid_arguments_do_not_run(self):
        kit = _Base()
        result = kit.call_tool("ACME_GET", {"id": 7}, TOKEN)
        assert not result.successful
        assert result.error == "invalid arguments for ACME_GET: 'id' must be string, got integer"
        assert result.error_code == ERROR_INVALID_ARGUMENTS
        assert kit.calls == []

    def test_integral_float_coerced_for_integer_argument(self):
        result = _Base().call_tool("ACME_GET", {"id": "7", "limit": 3.0}, TOKEN)
        assert result.successful and result.data["limit"] == 3
        assert type(result.data["limit"]) is int

    def test_non_object_arguments(self):
        result = _Base().call_tool("ACME_GET", "id=7", TOKEN)  # type: ignore[arg-type]
        assert not result.successful and "must be an object" in (result.error or "")

    @pytest.mark.parametrize("config", [{}, None, {"access_token": ""}, {"access_token": 5}])
    def test_missing_credentials(self, config):
        kit = _Base()
        result = kit.call_tool("ACME_GET", {"id": "7"}, config)
        assert not result.successful and "not connected" in (result.error or "")
        assert result.error_code == ERROR_UNAUTHORIZED
        assert kit.calls == []

    def test_validate_config_crash_means_not_connected(self):
        class Crashy(_Base):
            def validate_config(self, config):
                raise KeyError("x")

        result = Crashy().call_tool("ACME_GET", {"id": "7"}, TOKEN)
        assert not result.successful and "not connected" in (result.error or "")

    def test_exception_is_caught(self):
        result = _Base().call_tool("ACME_BOOM", {}, TOKEN)
        assert not result.successful
        assert result.error == "ACME_BOOM failed (RuntimeError): upstream exploded"
        assert result.error_code == ERROR_UPSTREAM_ERROR

    def test_exception_text_drops_url_query(self):
        result = _Base().call_tool("ACME_URL_ERROR", {}, TOKEN)
        assert "private" not in (result.error or "") and "page=2" not in (result.error or "")
        assert "https://api.acme.example/search?…" in (result.error or "")

    def test_exception_without_text(self):
        result = _Base().call_tool("ACME_EMPTY_EXCEPTION", {}, TOKEN)
        assert result.error == "ACME_EMPTY_EXCEPTION failed (RuntimeError)"

    def test_exception_text_truncated(self):
        class Verbose(_Base):
            def run_tool(self, tool, arguments, config):
                raise RuntimeError("x" * 5000)

        result = Verbose().call_tool("ACME_GET", {"id": "7"}, TOKEN)
        assert len(result.error or "") < 400

    def test_timeout_maps_to_timeout_code(self):
        result = _Base().call_tool("ACME_TIMEOUT", {}, TOKEN)
        assert (result.error, result.error_code) == ("ACME_TIMEOUT timed out", ERROR_TIMEOUT)

    def test_tool_error_message_and_code_pass_through(self):
        result = _Base().call_tool("ACME_TOOL_ERROR", {}, TOKEN)
        assert result.to_dict() == {
            "successful": False,
            "data": None,
            "error": "resource not found",
            "error_code": "not_found",
            "retry_after": None,
        }

    def test_tool_error_retry_after(self):
        result = _Base().call_tool("ACME_RATE_LIMITED", {}, TOKEN)
        assert (result.error_code, result.retry_after) == (ERROR_RATE_LIMITED, 42.0)

    def test_non_canonical_result_code_normalized(self):
        result = _Base().call_tool("ACME_ODD_CODE", {}, TOKEN)
        assert result.error_code == ERROR_UPSTREAM_ERROR

    def test_secrets_scrubbed_from_errors(self):
        result = _Base().call_tool("ACME_LEAK", {}, TOKEN)
        assert TOKEN["access_token"] not in (result.error or "")
        assert "***" in (result.error or "")

    def test_wrong_return_type(self):
        result = _Base().call_tool("ACME_WRONG_RETURN", {}, TOKEN)
        assert not result.successful and "expected ToolResult" in (result.error or "")
        assert result.error_code == ERROR_UPSTREAM_ERROR

    def test_failure_without_message_gets_one(self):
        result = _Base().call_tool("ACME_FAIL_NO_MESSAGE", {}, TOKEN)
        assert result.error == "ACME_FAIL_NO_MESSAGE failed"
        assert result.error_code == ERROR_UPSTREAM_ERROR

    def test_get_tool_crash_never_raises(self):
        class Broken(_Base):
            def tools(self):
                raise RuntimeError("catalog down")

        result = Broken().call_tool("ACME_GET", {"id": "7"}, TOKEN)
        assert not result.successful and "internal error" in (result.error or "")
        assert result.error_code == ERROR_UPSTREAM_ERROR

    def test_run_tool_gets_copies(self):
        kit = _Base()
        config = dict(TOKEN)
        kit.call_tool("ACME_GET", {"id": "7"}, config)
        _, _, seen = kit.calls[0]
        assert seen == config and seen is not config

    def test_auth_none_toolkit_needs_no_token(self):
        class Open(_Base):
            auth_type = AuthType.NONE

        assert Open().call_tool("ACME_GET", {"id": "7"}, {}).successful


class TestMockToolkit:
    def test_crud_round_trip(self):
        kit = MockToolkit()
        created = kit.call_tool("MOCK_CREATE_ITEM", {"title": "Third"}, TOKEN)
        assert created.successful and created.data["status"] == "open"
        listed = kit.call_tool("MOCK_LIST_ITEMS", {"query": "third"}, TOKEN)
        assert [i["title"] for i in listed.data["items"]] == ["Third"]
        assert kit.call_tool("MOCK_DELETE_ITEM", {"item_id": created.data["id"]}, TOKEN).successful
        missing = kit.call_tool("MOCK_GET_ITEM", {"item_id": created.data["id"]}, TOKEN)
        assert (missing.error, missing.error_code) == ("item not found", ERROR_NOT_FOUND)

    def test_download_returns_file_envelope(self):
        result = MockToolkit().call_tool("MOCK_DOWNLOAD_FILE", {"item_id": "1"}, TOKEN)
        assert result.successful
        (envelope,) = result.files
        assert envelope.data == b"First item\n"
        assert envelope.metadata["filename"] == result.data["name"] == "item-1.txt"
        assert result.data["mimetype"] == envelope.content_type == "text/plain"

    def test_expired_token_trigger(self):
        result = MockToolkit().call_tool(
            "MOCK_GET_AUTHENTICATED_USER", {}, {"access_token": MOCK_EXPIRED_TOKEN}
        )
        assert (result.successful, result.error_code) == (False, ERROR_UNAUTHORIZED)

    def test_rate_limited_token_trigger(self):
        result = MockToolkit().call_tool(
            "MOCK_LIST_ITEMS", {}, {"access_token": MOCK_RATE_LIMITED_TOKEN}
        )
        assert (result.error_code, result.retry_after) == (ERROR_RATE_LIMITED, 30.0)

    def test_instances_do_not_share_state(self):
        first = MockToolkit()
        first.call_tool("MOCK_DELETE_ITEM", {"item_id": "1"}, TOKEN)
        assert MockToolkit().call_tool("MOCK_GET_ITEM", {"item_id": "1"}, TOKEN).successful


class TestToolkitOAuthMethods:
    def test_without_spec_raises(self):
        with pytest.raises(OAuthError) as info:
            _Base().build_authorize_url(client_id="c", redirect_uri="https://r", state="s")
        assert info.value.error == "oauth_not_supported"

    def test_revoke_without_spec_is_false(self):
        assert _Base().revoke_token(client_id="c", client_secret="s", token="t") is False

    def test_authorize_url_uses_spec(self):
        url = MockToolkit().build_authorize_url(
            client_id="c", redirect_uri="https://r", state="s", code_challenge="ch"
        )
        assert url.startswith(MOCK_OAUTH.authorize_url + "?")
        assert "code_challenge=ch" in url
