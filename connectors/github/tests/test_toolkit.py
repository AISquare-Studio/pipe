"""GitHubToolkit behaviour beyond the per-tool table: catalog contract, result
shaping, file content handling, write-tool guard rails, argument
normalization, secrecy, and tool ranking for real requests."""

from __future__ import annotations

import base64
import logging

import pytest
import requests
from aisquare.pipe import AuthType, get_toolkit

from aisquare_pipe_github import GITHUB_OAUTH, GitHubToolkit
from aisquare_pipe_github.rest import GitHubAPIError
from aisquare_pipe_github.toolkit import describe_error
from aisquare_pipe_github.tools import INLINE_CONTENT_MAX, slim

from .fakes import CONFIG, RAW_CONTENT_TYPE, TOKEN, harness, response

REPO = {"owner": "octocat", "repo": "Hello-World"}

READ_SLUGS = {
    "GITHUB_GET_THE_AUTHENTICATED_USER",
    "GITHUB_LIST_REPOSITORIES_FOR_THE_AUTHENTICATED_USER",
    "GITHUB_LIST_ORGANIZATIONS_FOR_THE_AUTHENTICATED_USER",
    "GITHUB_GET_A_REPOSITORY",
    "GITHUB_SEARCH_REPOSITORIES",
    "GITHUB_LIST_BRANCHES",
    "GITHUB_LIST_COMMITS",
    "GITHUB_GET_A_COMMIT",
    "GITHUB_GET_REPOSITORY_CONTENT",
    "GITHUB_GET_RAW_REPOSITORY_CONTENT",
    "GITHUB_SEARCH_CODE",
    "GITHUB_LIST_REPOSITORY_ISSUES",
    "GITHUB_LIST_ISSUES_ASSIGNED_TO_THE_AUTHENTICATED_USER",
    "GITHUB_GET_AN_ISSUE",
    "GITHUB_LIST_ISSUE_COMMENTS",
    "GITHUB_SEARCH_ISSUES_AND_PULL_REQUESTS",
    "GITHUB_LIST_PULL_REQUESTS",
    "GITHUB_GET_A_PULL_REQUEST",
    "GITHUB_LIST_PULL_REQUESTS_FILES",
    "GITHUB_LIST_RELEASES",
    "GITHUB_LIST_WORKFLOW_RUNS_FOR_A_REPOSITORY",
    "GITHUB_LIST_REPOSITORY_CONTRIBUTORS",
    "GITHUB_LIST_NOTIFICATIONS_FOR_THE_AUTHENTICATED_USER",
}
WRITE_SLUGS = {
    "GITHUB_CREATE_AN_ISSUE",
    "GITHUB_UPDATE_AN_ISSUE",
    "GITHUB_CREATE_AN_ISSUE_COMMENT",
    "GITHUB_CREATE_A_PULL_REQUEST",
    "GITHUB_CREATE_A_REVIEW_FOR_A_PULL_REQUEST",
    "GITHUB_CREATE_OR_UPDATE_FILE_CONTENTS",
    "GITHUB_CREATE_A_REFERENCE",
    "GITHUB_STAR_A_REPOSITORY_FOR_THE_AUTHENTICATED_USER",
}


class TestCatalog:
    def test_registered_as_the_github_toolkit(self):
        assert get_toolkit("github") is GitHubToolkit

    def test_identity(self):
        toolkit = GitHubToolkit()
        assert (toolkit.name, toolkit.version, toolkit.slug_prefix) == ("github", "0.2.0", "GITHUB_")
        assert toolkit.auth_type is AuthType.OAUTH2
        assert toolkit.oauth is GITHUB_OAUTH

    def test_composio_slugs_exactly(self):
        assert {t.slug for t in GitHubToolkit().tools()} == READ_SLUGS | WRITE_SLUGS

    def test_read_only_and_destructive_flags(self):
        tools = {t.slug: t for t in GitHubToolkit().tools()}
        assert {s for s, t in tools.items() if t.read_only} == READ_SLUGS
        assert {s for s, t in tools.items() if t.destructive} == {
            "GITHUB_UPDATE_AN_ISSUE",
            "GITHUB_CREATE_OR_UPDATE_FILE_CONTENTS",
        }

    def test_owner_repo_tools_require_both(self):
        for tool in GitHubToolkit().tools():
            properties = tool.input_schema["properties"]
            if "owner" in properties and "repo" in properties:
                assert {"owner", "repo"} <= set(tool.input_schema["required"]), tool.slug

    def test_validate_config(self):
        toolkit = GitHubToolkit()
        assert toolkit.validate_config({}) is False
        assert toolkit.validate_config({"access_token": "  "}) is False
        assert toolkit.validate_config(CONFIG) is True

    def test_no_credentials_means_no_request(self):
        h = harness()
        result = h.call("GITHUB_GET_THE_AUTHENTICATED_USER", {}, config={})
        assert result.successful is False
        assert "not connected" in result.error
        assert h.session.calls == []


class TestResultShape:
    def test_list_items_come_after_the_hints(self):
        h = harness(response(200, [{"name": "main"}]))
        data = h.call("GITHUB_LIST_BRANCHES", REPO).data
        assert list(data) == ["page", "per_page", "has_more", "next_page", "items"]

    def test_object_list_endpoints_keep_github_keys(self):
        h = harness(response(200, {"total_count": 3, "workflow_runs": [{"id": 1}]}))
        data = h.call("GITHUB_LIST_WORKFLOW_RUNS_FOR_A_REPOSITORY", REPO).data
        assert data["total_count"] == 3
        assert data["workflow_runs"] == [{"id": 1}]
        assert list(data)[:4] == ["page", "per_page", "has_more", "next_page"]

    def test_empty_204_list_is_empty_items(self):
        h = harness(response(204))
        data = h.call("GITHUB_LIST_REPOSITORY_CONTRIBUTORS", REPO).data
        assert data["items"] == []

    def test_api_hypermedia_is_dropped(self):
        repo = {
            "full_name": "octocat/Hello-World",
            "url": "https://api.github.com/repos/octocat/Hello-World",
            "html_url": "https://github.com/octocat/Hello-World",
            "clone_url": "https://github.com/octocat/Hello-World.git",
            "issues_url": "https://api.github.com/repos/octocat/Hello-World/issues{/number}",
            "hooks_url": "https://api.github.com/repos/octocat/Hello-World/hooks",
            "mirror_url": None,
            "owner": {
                "login": "octocat",
                "avatar_url": "https://avatars.githubusercontent.com/u/583231",
                "followers_url": "https://api.github.com/users/octocat/followers",
            },
            "_links": {"self": {"href": "https://api.github.com/x"}},
        }
        h = harness(response(200, repo))
        data = h.call("GITHUB_GET_A_REPOSITORY", REPO).data
        assert data == {
            "full_name": "octocat/Hello-World",
            "url": "https://api.github.com/repos/octocat/Hello-World",
            "html_url": "https://github.com/octocat/Hello-World",
            "clone_url": "https://github.com/octocat/Hello-World.git",
            "mirror_url": None,
            "owner": {"login": "octocat", "avatar_url": "https://avatars.githubusercontent.com/u/583231"},
        }

    def test_slim_leaves_scalars_and_lists_alone(self):
        assert slim([1, "x", None, {"a_url": "https://api.github.com/a"}], "https://api.github.com/") == [
            1,
            "x",
            None,
            {},
        ]


class TestArguments:
    @pytest.mark.parametrize(
        ("owner", "repo"),
        [
            ("octocat", "octocat/Hello-World"),
            ("octocat/Hello-World", "Hello-World"),
            (" octocat ", "Hello-World.git"),
        ],
    )
    def test_owner_repo_slips_are_forgiven(self, owner, repo):
        h = harness(response(200, {}))
        h.call("GITHUB_GET_A_REPOSITORY", {"owner": owner, "repo": repo})
        assert h.session.last.path == "/repos/octocat/Hello-World"

    @pytest.mark.parametrize(
        ("owner", "repo", "path"),
        [
            ("", "acme/widgets", "/repos/acme/widgets"),  # the owner inside repo wins
            ("ACME", "acme/widgets", "/repos/acme/widgets"),  # same owner, any case
            ("acme/widgets", "", "/repos/acme/widgets"),
            ("acme/widgets.git", "WIDGETS", "/repos/acme/WIDGETS"),
        ],
    )
    def test_the_owner_inside_repo_wins(self, owner, repo, path):
        h = harness(response(200, {}))
        result = h.call("GITHUB_GET_A_REPOSITORY", {"owner": owner, "repo": repo})
        assert result.successful, result.error
        assert h.session.last.path == path

    @pytest.mark.parametrize(
        ("owner", "repo"),
        [
            ("octocat", "acme/widgets"),  # a fork owner next to the upstream full name
            ("acme/widgets", "gadgets"),
            ("acme", "a/b/c"),
            ("a/b/c", "widgets"),
        ],
    )
    def test_conflicting_owner_is_refused_before_any_write(self, owner, repo):
        h = harness()
        result = h.call("GITHUB_CREATE_AN_ISSUE", {"owner": owner, "repo": repo, "title": "t"})
        assert result.successful is False
        assert result.error_code == "invalid_arguments"
        assert h.session.calls == []

    def test_path_segments_are_encoded_but_slashes_kept(self):
        h = harness(response(200, {"type": "file", "encoding": "none", "content": ""}))
        h.call("GITHUB_GET_REPOSITORY_CONTENT", {**REPO, "path": "/docs/Q3 plan#1.md"})
        assert h.session.last.url == "https://api.github.com/repos/octocat/Hello-World/contents/docs/Q3%20plan%231.md"

    def test_names_cannot_inject_url_syntax(self):
        h = harness(response(200, {}))
        h.call("GITHUB_GET_AN_ISSUE", {"owner": "octo cat", "repo": "?x=1#y", "issue_number": 1})
        assert h.session.last.path == "/repos/octo%20cat/%3Fx%3D1%23y/issues/1"

    @pytest.mark.parametrize(
        ("slug", "args"),
        [
            ("GITHUB_GET_A_REPOSITORY", {"owner": "..", "repo": "user"}),
            ("GITHUB_GET_A_REPOSITORY", {"owner": "octocat", "repo": "."}),
            ("GITHUB_GET_A_COMMIT", {**REPO, "ref": "../../../user"}),
            (
                "GITHUB_CREATE_OR_UPDATE_FILE_CONTENTS",
                {**REPO, "path": "docs/../../collaborators/mallory", "message": "m", "content": "c", "sha": "s"},
            ),
        ],
    )
    def test_dot_segments_cannot_reroute_a_call(self, slug, args):
        h = harness()
        result = h.call(slug, args)
        assert result.successful is False
        assert "path segments" in result.error
        assert h.session.calls == []

    def test_ref_keeps_its_slashes(self):
        h = harness(response(200, {"sha": "abc", "files": []}))
        h.call("GITHUB_GET_A_COMMIT", {**REPO, "ref": "heads/feature/x"})
        assert h.session.last.path == "/repos/octocat/Hello-World/commits/heads/feature/x"

    def test_per_page_is_clamped(self):
        h = harness(response(200, []), response(200, []))
        h.call("GITHUB_LIST_COMMITS", {**REPO, "per_page": 500})
        assert h.session.last.params["per_page"] == 100
        h.call("GITHUB_LIST_NOTIFICATIONS_FOR_THE_AUTHENTICATED_USER", {"per_page": 100})
        assert h.session.last.params["per_page"] == 50

    def test_integral_floats_become_integers(self):
        h = harness(response(200, {}))
        h.call("GITHUB_GET_AN_ISSUE", {**REPO, "issue_number": 7.0, "page": 2.0})
        assert h.session.last.path.endswith("/issues/7")

    def test_explicit_nulls_on_optional_args_are_dropped(self):
        h = harness(response(200, []))
        h.call("GITHUB_LIST_REPOSITORY_ISSUES", {**REPO, "state": None, "labels": None})
        assert h.session.last.params == {"page": 1, "per_page": 30}

    def test_nullable_update_fields_send_null(self):
        h = harness(response(200, {"number": 8}))
        result = h.call("GITHUB_UPDATE_AN_ISSUE", {**REPO, "issue_number": 8, "body": None, "milestone": None})
        assert result.successful, result.error
        assert h.session.last.json == {"body": None, "milestone": None}

    def test_bad_arguments_fail_before_any_request(self):
        h = harness()
        result = h.call("GITHUB_LIST_REPOSITORY_ISSUES", {**REPO, "state": "sideways"})
        assert result.successful is False
        assert "state" in result.error
        assert h.session.calls == []


class TestRepositoryContent:
    def test_text_file_is_decoded(self):
        encoded = base64.encodebytes("héllo\nworld\n".encode()).decode()  # GitHub's line-wrapped base64
        h = harness(response(200, {"type": "file", "name": "a.md", "encoding": "base64", "content": encoded}))
        data = h.call("GITHUB_GET_REPOSITORY_CONTENT", {**REPO, "path": "a.md"}).data
        assert data["content"] == "héllo\nworld\n"
        assert data["encoding"] == "utf-8"

    def test_binary_file_stays_base64(self):
        encoded = base64.b64encode(b"\x89PNG\r\n\x1a\n\xff\xfe").decode()
        h = harness(response(200, {"type": "file", "name": "logo.png", "encoding": "base64", "content": encoded}))
        data = h.call("GITHUB_GET_REPOSITORY_CONTENT", {**REPO, "path": "logo.png"}).data
        assert data["content"] == encoded
        assert data["encoding"] == "base64"

    def test_large_text_is_cut_and_flagged(self):
        text = "a" + "é" * 60_000  # 120,001 bytes; the cap falls inside a 2-byte character
        encoded = base64.b64encode(text.encode()).decode()
        h = harness(response(200, {"type": "file", "name": "big.md", "size": 120_001, "encoding": "base64", "content": encoded}))
        data = h.call("GITHUB_GET_REPOSITORY_CONTENT", {**REPO, "path": "big.md"}).data
        assert data["truncated"] is True
        assert data["encoding"] == "utf-8"
        assert data["content"] == "a" + "é" * (INLINE_CONTENT_MAX // 2 - 1)  # split char dropped
        assert len(data["content"].encode()) == INLINE_CONTENT_MAX - 1
        assert data["size"] == 120_001

    def test_large_binary_is_left_out_and_flagged(self):
        encoded = base64.b64encode(b"\xff" * (INLINE_CONTENT_MAX + 1)).decode()
        h = harness(response(200, {"type": "file", "name": "blob.bin", "encoding": "base64", "content": encoded}))
        data = h.call("GITHUB_GET_REPOSITORY_CONTENT", {**REPO, "path": "blob.bin"}).data
        assert (data["content"], data["encoding"], data["truncated"]) == ("", "none", True)

    def test_content_at_the_cap_is_whole(self):
        encoded = base64.b64encode(b"x" * INLINE_CONTENT_MAX).decode()
        h = harness(response(200, {"type": "file", "name": "a.txt", "encoding": "base64", "content": encoded}))
        data = h.call("GITHUB_GET_REPOSITORY_CONTENT", {**REPO, "path": "a.txt"}).data
        assert len(data["content"]) == INLINE_CONTENT_MAX
        assert "truncated" not in data

    def test_directory_lists_items(self):
        listing = [{"name": "README", "path": "README", "type": "file", "sha": "a", "size": 13}]
        h = harness(response(200, listing))
        data = h.call("GITHUB_GET_REPOSITORY_CONTENT", {**REPO, "path": ""}).data
        assert h.session.last.path == "/repos/octocat/Hello-World/contents"
        assert data == {"type": "dir", "path": "", "items": listing}


class TestRawRepositoryContent:
    def _raw(self, content: bytes, path: str = "docs/roadmap.md", **args):
        h = harness(response(200, content=content, content_type=RAW_CONTENT_TYPE))
        return h, h.call("GITHUB_GET_RAW_REPOSITORY_CONTENT", {**REPO, "path": path, **args})

    def test_bytes_come_back_as_a_file(self):
        h, result = self._raw(b"# Roadmap\n", ref="v1.0")
        assert result.successful, result.error
        assert h.session.last.params == {"ref": "v1.0"}
        (envelope,) = result.files
        assert envelope.data == b"# Roadmap\n"
        assert envelope.content_type == "text/markdown"
        assert envelope.metadata["filename"] == "roadmap.md"
        assert envelope.metadata["repository"] == "octocat/Hello-World"
        assert result.data == {
            "name": "roadmap.md",
            "path": "docs/roadmap.md",
            "repository": "octocat/Hello-World",
            "size": 10,
            "mimetype": "text/markdown",
            "ref": "v1.0",
            "content": "# Roadmap\n",
        }
        assert "files" not in result.to_dict()

    def test_binary_file_has_no_inline_text(self):
        pdf = b"%PDF-1.7\n\xe2\xe3\xcf\xd3 binary"
        _, result = self._raw(pdf, path="spec.pdf")
        assert result.data["mimetype"] == "application/pdf"
        assert "content" not in result.data
        assert result.files[0].data == pdf

    def test_large_text_is_not_inlined(self):
        _, result = self._raw(b"x" * (INLINE_CONTENT_MAX + 1), path="big.txt")
        assert "content" not in result.data
        assert result.data["size"] == INLINE_CONTENT_MAX + 1

    def test_unknown_extension_is_sniffed(self):
        _, text = self._raw(b"plain words", path="LICENSE")
        _, blob = self._raw(b"\xff\xfe\x00", path="blob")
        assert text.data["mimetype"] == "text/plain"
        assert blob.data["mimetype"] == "application/octet-stream"

    def test_directory_is_refused(self):
        listing = [{"name": "a.md", "path": "docs/a.md", "type": "file", "sha": "a"}]
        h = harness(response(200, listing))
        result = h.call("GITHUB_GET_RAW_REPOSITORY_CONTENT", {**REPO, "path": "docs"})
        assert result.successful is False
        assert "directory" in result.error

    def test_json_file_is_not_mistaken_for_a_directory(self):
        _, result = self._raw(b'[{"type": "x"}]', path="data.json")
        assert result.successful
        assert result.data["mimetype"] == "application/json"


FILE_ARGS = {**REPO, "path": "docs/a.md", "message": "Update a", "content": "new text"}


class TestCreateOrUpdateFile:
    ARGS = FILE_ARGS

    def test_existing_file_sha_is_looked_up(self):
        h = harness(
            response(200, {"type": "file", "sha": "cur-sha"}),
            response(200, {"commit": {"sha": "c1"}}),
        )
        result = h.call("GITHUB_CREATE_OR_UPDATE_FILE_CONTENTS", {**self.ARGS, "branch": "dev"})
        assert result.successful, result.error
        lookup, put = h.session.calls
        assert (lookup.method, lookup.path, lookup.params) == ("GET", "/repos/octocat/Hello-World/contents/docs/a.md", {"ref": "dev"})
        assert put.method == "PUT"
        assert put.json["sha"] == "cur-sha"
        assert put.json["branch"] == "dev"
        assert base64.b64decode(put.json["content"]) == b"new text"

    def test_new_file_has_no_sha(self):
        h = harness(response(404, {"message": "Not Found"}), response(201, {"commit": {"sha": "c1"}}))
        result = h.call("GITHUB_CREATE_OR_UPDATE_FILE_CONTENTS", self.ARGS)
        assert result.successful, result.error
        assert "sha" not in h.session.last.json

    def test_lookup_failure_other_than_404_is_reported(self):
        h = harness(response(403, {"message": "Resource not accessible by integration"}))
        result = h.call("GITHUB_CREATE_OR_UPDATE_FILE_CONTENTS", self.ARGS)
        assert result.error.startswith("403: Resource not accessible by integration")
        assert result.error_code == "forbidden"
        assert len(h.session.calls) == 1

    def test_base64_content_is_passed_through(self):
        raw = b"\x00\x01binary"
        h = harness(response(201, {}))
        h.call(
            "GITHUB_CREATE_OR_UPDATE_FILE_CONTENTS",
            {**self.ARGS, "content": base64.b64encode(raw).decode(), "encoding": "base64", "sha": "s"},
        )
        assert base64.b64decode(h.session.last.json["content"]) == raw

    def test_invalid_base64_fails_without_a_request(self):
        h = harness()
        result = h.call("GITHUB_CREATE_OR_UPDATE_FILE_CONTENTS", {**self.ARGS, "content": "not base64!", "encoding": "base64"})
        assert result.successful is False
        assert "base64" in result.error
        assert h.session.calls == []

    def test_flattened_author_and_committer(self):
        h = harness(response(201, {}))
        h.call(
            "GITHUB_CREATE_OR_UPDATE_FILE_CONTENTS",
            {
                **self.ARGS,
                "sha": "s",
                "author__name": "Mona",
                "author__email": "mona@example.com",
                "committer__name": "Bot",
                "committer__email": "bot@example.com",
                "committer__date": "2026-10-16T00:00:00Z",
            },
        )
        body = h.session.last.json
        assert body["author"] == {"name": "Mona", "email": "mona@example.com"}
        assert body["committer"] == {"name": "Bot", "email": "bot@example.com", "date": "2026-10-16T00:00:00Z"}


class TestWriteGuards:
    def test_pull_request_needs_title_or_issue(self):
        h = harness()
        result = h.call("GITHUB_CREATE_A_PULL_REQUEST", {**REPO, "head": "docs", "base": "main"})
        assert result.successful is False
        assert "title" in result.error
        assert h.session.calls == []

    def test_pull_request_from_an_issue(self):
        h = harness(response(201, {"number": 9}))
        result = h.call("GITHUB_CREATE_A_PULL_REQUEST", {**REPO, "head": "docs", "base": "main", "issue": 5})
        assert result.successful, result.error
        assert h.session.last.json == {"head": "docs", "base": "main", "issue": 5}

    @pytest.mark.parametrize("event", ["REQUEST_CHANGES", "COMMENT"])
    def test_review_needs_a_body_for_comment_events(self, event):
        h = harness()
        result = h.call("GITHUB_CREATE_A_REVIEW_FOR_A_PULL_REQUEST", {**REPO, "pull_number": 1, "event": event})
        assert result.successful is False
        assert "body is required" in result.error
        assert h.session.calls == []

    def test_review_with_inline_comments(self):
        comments = [{"path": "README", "body": "typo", "line": 3}]
        h = harness(response(200, {"id": 1}))
        h.call(
            "GITHUB_CREATE_A_REVIEW_FOR_A_PULL_REQUEST",
            {**REPO, "pull_number": 1, "event": "COMMENT", "body": "notes", "comments": comments},
        )
        assert h.session.last.json == {"event": "COMMENT", "body": "notes", "comments": comments}

    def test_star_returns_starred(self):
        h = harness(response(204))
        result = h.call("GITHUB_STAR_A_REPOSITORY_FOR_THE_AUTHENTICATED_USER", REPO)
        assert result.data == {"starred": True, "repository": "octocat/Hello-World"}


class TestSecrecy:
    def test_token_echoed_in_an_error_is_scrubbed(self):
        h = harness(response(500, {"message": f"internal error for {TOKEN}"}))
        result = h.call("GITHUB_CREATE_AN_ISSUE", {**REPO, "title": "t"})
        assert TOKEN not in result.error
        assert "***" in result.error

    def test_network_error_carries_no_url_or_arguments(self):
        h = harness(requests.ConnectionError(f"https://api.github.com/search/code?q=project-x-secret token={TOKEN}"))
        result = h.call("GITHUB_SEARCH_CODE", {"q": "project-x-secret"})
        assert result.error == "network error talking to GitHub (ConnectionError)"
        assert result.error_code == "upstream_error"
        assert result.data is None

    def test_logs_carry_no_token_or_arguments(self, caplog):
        caplog.set_level(logging.DEBUG)
        h = harness(response(404, {"message": "Not Found"}))
        h.call("GITHUB_SEARCH_CODE", {"q": "project-x-secret"})
        assert TOKEN not in caplog.text
        assert "project-x-secret" not in caplog.text


class TestDescribeError:
    @pytest.mark.parametrize(
        ("error", "expected"),
        [
            (GitHubAPIError(0, "network error talking to GitHub (Timeout)"), "network error talking to GitHub (Timeout)"),
            (GitHubAPIError(422, "Validation Failed"), "422: Validation Failed"),
            (GitHubAPIError(403, "API rate limit exceeded", retry_after=12.2), "403: API rate limit exceeded — GitHub rate limit; retry in about 13s"),
        ],
    )
    def test_messages(self, error, expected):
        assert describe_error(error) == expected


class TestToolSelection:
    """The keyword ranking (used by pipeAAS's ranked agent mode) must surface
    the right tool for the requests real /ask traffic sends."""

    @pytest.mark.parametrize(
        ("query", "slug"),
        [
            ("list my repos", "GITHUB_LIST_REPOSITORIES_FOR_THE_AUTHENTICATED_USER"),
            ("issues assigned to me", "GITHUB_LIST_ISSUES_ASSIGNED_TO_THE_AUTHENTICATED_USER"),
            ("search code for TODO", "GITHUB_SEARCH_CODE"),
            ("who am I", "GITHUB_GET_THE_AUTHENTICATED_USER"),
            ("read the README file", "GITHUB_GET_REPOSITORY_CONTENT"),
            ("failing CI builds", "GITHUB_LIST_WORKFLOW_RUNS_FOR_A_REPOSITORY"),
            ("latest release", "GITHUB_LIST_RELEASES"),
            ("my notifications", "GITHUB_LIST_NOTIFICATIONS_FOR_THE_AUTHENTICATED_USER"),
            ("close an issue", "GITHUB_UPDATE_AN_ISSUE"),
            ("create a branch", "GITHUB_CREATE_A_REFERENCE"),
            ("approve the pull request", "GITHUB_CREATE_A_REVIEW_FOR_A_PULL_REQUEST"),
            ("show the files changed in PR 12", "GITHUB_LIST_PULL_REQUESTS_FILES"),
        ],
    )
    def test_top_result(self, query, slug):
        assert GitHubToolkit().search_tools(query, limit=1)[0].slug == slug

    @pytest.mark.parametrize(
        ("query", "slug"),
        [
            ("open pull requests in acme/api", "GITHUB_LIST_PULL_REQUESTS"),
            ("create a pull request", "GITHUB_CREATE_A_PULL_REQUEST"),
            ("write a file to the repo", "GITHUB_CREATE_OR_UPDATE_FILE_CONTENTS"),
        ],
    )
    def test_in_top_three(self, query, slug):
        assert slug in [t.slug for t in GitHubToolkit().search_tools(query, limit=3)]
