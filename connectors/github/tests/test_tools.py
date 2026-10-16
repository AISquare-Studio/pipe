"""Every GitHub tool, end to end through ``call_tool`` with HTTP mocked at the
client boundary: request mapping, happy path, pagination hint, 404, 401 ->
reconnect message, and rate-limit retry. One CASES row per tool."""

from __future__ import annotations

import base64
from dataclasses import dataclass, field
from typing import Any

import pytest

from aisquare_pipe_github import GitHubToolkit
from aisquare_pipe_github.rest import JSON_MEDIA, RAW_MEDIA

from .fakes import RAW_CONTENT_TYPE, harness, next_link, response

OWNER_REPO = {"owner": "octocat", "repo": "Hello-World"}
REPO_PATH = "/repos/octocat/Hello-World"
FIRST_PAGE = {"page": 1, "per_page": 30}
HINT_KEYS = ["page", "per_page", "has_more", "next_page"]


@dataclass(frozen=True)
class Case:
    slug: str
    args: dict[str, Any]
    method: str
    path: str
    params: dict[str, Any] | None = None
    json: Any = None
    reply: Any = field(default_factory=dict)
    status: int = 200
    paged: bool = False
    raw: bytes | None = None

    def ok_response(self, **kwargs: Any):
        if self.raw is not None:
            return response(self.status, content=self.raw, content_type=RAW_CONTENT_TYPE, **kwargs)
        return response(self.status, None if self.status == 204 else self.reply, **kwargs)

    @property
    def id(self) -> str:
        return self.slug.removeprefix("GITHUB_")


def _b64(text: str) -> str:
    return base64.b64encode(text.encode()).decode()


CASES = [
    # users
    Case("GITHUB_GET_THE_AUTHENTICATED_USER", {}, "GET", "/user", reply={"login": "octocat"}),
    Case(
        "GITHUB_LIST_REPOSITORIES_FOR_THE_AUTHENTICATED_USER",
        {"type": "owner", "sort": "pushed", "direction": "desc"},
        "GET",
        "/user/repos",
        params={"type": "owner", "sort": "pushed", "direction": "desc", **FIRST_PAGE},
        reply=[{"full_name": "octocat/Hello-World"}],
        paged=True,
    ),
    Case(
        "GITHUB_LIST_ORGANIZATIONS_FOR_THE_AUTHENTICATED_USER",
        {},
        "GET",
        "/user/orgs",
        params=FIRST_PAGE,
        reply=[{"login": "github"}],
        paged=True,
    ),
    # repositories
    Case("GITHUB_GET_A_REPOSITORY", OWNER_REPO, "GET", REPO_PATH, reply={"full_name": "octocat/Hello-World"}),
    Case(
        "GITHUB_SEARCH_REPOSITORIES",
        {"q": "pipe language:python", "sort": "stars", "order": "desc"},
        "GET",
        "/search/repositories",
        params={"q": "pipe language:python", "sort": "stars", "order": "desc", **FIRST_PAGE},
        reply={"total_count": 1, "incomplete_results": False, "items": [{"full_name": "a/b"}]},
        paged=True,
    ),
    Case(
        "GITHUB_LIST_BRANCHES",
        {**OWNER_REPO, "protected": True},
        "GET",
        f"{REPO_PATH}/branches",
        params={"protected": "true", **FIRST_PAGE},
        reply=[{"name": "main", "commit": {"sha": "abc"}, "protected": True}],
        paged=True,
    ),
    Case(
        "GITHUB_LIST_COMMITS",
        {**OWNER_REPO, "sha": "main", "path": "README", "author": "octocat", "since": "2026-01-01T00:00:00Z"},
        "GET",
        f"{REPO_PATH}/commits",
        params={"sha": "main", "path": "README", "author": "octocat", "since": "2026-01-01T00:00:00Z", **FIRST_PAGE},
        reply=[{"sha": "abc"}],
        paged=True,
    ),
    Case(
        "GITHUB_GET_A_COMMIT",
        {**OWNER_REPO, "ref": "7fd1a60b01f91b314f59955a4e4d4e80d8edf11d"},
        "GET",
        f"{REPO_PATH}/commits/7fd1a60b01f91b314f59955a4e4d4e80d8edf11d",
        params=FIRST_PAGE,
        reply={"sha": "7fd1a60b01f91b314f59955a4e4d4e80d8edf11d", "files": [{"filename": "README"}]},
        paged=True,
    ),
    # code
    Case(
        "GITHUB_GET_REPOSITORY_CONTENT",
        {**OWNER_REPO, "path": "README", "ref": "master"},
        "GET",
        f"{REPO_PATH}/contents/README",
        params={"ref": "master"},
        reply={"type": "file", "name": "README", "path": "README", "encoding": "base64", "content": _b64("Hello World!\n")},
    ),
    Case(
        "GITHUB_GET_RAW_REPOSITORY_CONTENT",
        {**OWNER_REPO, "path": "docs/roadmap.md"},
        "GET",
        f"{REPO_PATH}/contents/docs/roadmap.md",
        raw=b"# Roadmap\n",
    ),
    Case(
        "GITHUB_SEARCH_CODE",
        {"q": "roadmap filename:roadmap.md"},
        "GET",
        "/search/code",
        params={"q": "roadmap filename:roadmap.md", **FIRST_PAGE},
        reply={
            "total_count": 1,
            "incomplete_results": False,
            "items": [{"name": "roadmap.md", "path": "docs/roadmap.md", "repository": {"full_name": "octocat/Hello-World"}}],
        },
        paged=True,
    ),
    # issues
    Case(
        "GITHUB_LIST_REPOSITORY_ISSUES",
        {**OWNER_REPO, "state": "all", "labels": "bug,ui", "assignee": "*"},
        "GET",
        f"{REPO_PATH}/issues",
        params={"state": "all", "labels": "bug,ui", "assignee": "*", **FIRST_PAGE},
        reply=[{"number": 1, "title": "Found a bug"}],
        paged=True,
    ),
    Case(
        "GITHUB_LIST_ISSUES_ASSIGNED_TO_THE_AUTHENTICATED_USER",
        {"filter": "created", "state": "open"},
        "GET",
        "/issues",
        params={"filter": "created", "state": "open", **FIRST_PAGE},
        reply=[{"number": 2}],
        paged=True,
    ),
    Case(
        "GITHUB_GET_AN_ISSUE",
        {**OWNER_REPO, "issue_number": 7},
        "GET",
        f"{REPO_PATH}/issues/7",
        reply={"number": 7, "title": "Found a bug"},
    ),
    Case(
        "GITHUB_LIST_ISSUE_COMMENTS",
        {**OWNER_REPO, "issue_number": 7, "since": "2026-01-01T00:00:00Z"},
        "GET",
        f"{REPO_PATH}/issues/7/comments",
        params={"since": "2026-01-01T00:00:00Z", **FIRST_PAGE},
        reply=[{"id": 1, "body": "Me too"}],
        paged=True,
    ),
    Case(
        "GITHUB_SEARCH_ISSUES_AND_PULL_REQUESTS",
        {"q": "is:pr is:open author:@me", "sort": "updated", "order": "desc"},
        "GET",
        "/search/issues",
        params={"q": "is:pr is:open author:@me", "sort": "updated", "order": "desc", **FIRST_PAGE},
        reply={"total_count": 1, "incomplete_results": False, "items": [{"number": 3}]},
        paged=True,
    ),
    # pull requests
    Case(
        "GITHUB_LIST_PULL_REQUESTS",
        {**OWNER_REPO, "state": "open", "base": "main", "sort": "updated"},
        "GET",
        f"{REPO_PATH}/pulls",
        params={"state": "open", "base": "main", "sort": "updated", **FIRST_PAGE},
        reply=[{"number": 3}],
        paged=True,
    ),
    Case(
        "GITHUB_GET_A_PULL_REQUEST",
        {**OWNER_REPO, "pull_number": 3},
        "GET",
        f"{REPO_PATH}/pulls/3",
        reply={"number": 3, "merged": False},
    ),
    Case(
        "GITHUB_LIST_PULL_REQUESTS_FILES",
        {**OWNER_REPO, "pull_number": 3},
        "GET",
        f"{REPO_PATH}/pulls/3/files",
        params=FIRST_PAGE,
        reply=[{"filename": "README", "status": "modified"}],
        paged=True,
    ),
    # releases and CI
    Case(
        "GITHUB_LIST_RELEASES",
        OWNER_REPO,
        "GET",
        f"{REPO_PATH}/releases",
        params=FIRST_PAGE,
        reply=[{"tag_name": "v1.0.0"}],
        paged=True,
    ),
    Case(
        "GITHUB_LIST_WORKFLOW_RUNS_FOR_A_REPOSITORY",
        {**OWNER_REPO, "branch": "main", "status": "failure", "exclude_pull_requests": True},
        "GET",
        f"{REPO_PATH}/actions/runs",
        params={"branch": "main", "status": "failure", "exclude_pull_requests": "true", **FIRST_PAGE},
        reply={"total_count": 1, "workflow_runs": [{"id": 42, "conclusion": "failure"}]},
        paged=True,
    ),
    Case(
        "GITHUB_LIST_REPOSITORY_CONTRIBUTORS",
        {**OWNER_REPO, "anon": "1"},
        "GET",
        f"{REPO_PATH}/contributors",
        params={"anon": "1", **FIRST_PAGE},
        reply=[{"login": "octocat", "contributions": 32}],
        paged=True,
    ),
    Case(
        "GITHUB_LIST_NOTIFICATIONS_FOR_THE_AUTHENTICATED_USER",
        {"all": True, "participating": False},
        "GET",
        "/notifications",
        params={"all": "true", "participating": "false", **FIRST_PAGE},
        reply=[{"reason": "mention", "subject": {"title": "Hi"}}],
        paged=True,
    ),
    # writes
    Case(
        "GITHUB_CREATE_AN_ISSUE",
        {**OWNER_REPO, "title": "Found a bug", "body": "Steps", "labels": ["bug"], "assignees": ["octocat"]},
        "POST",
        f"{REPO_PATH}/issues",
        json={"title": "Found a bug", "body": "Steps", "labels": ["bug"], "assignees": ["octocat"]},
        reply={"number": 8, "html_url": "https://github.com/octocat/Hello-World/issues/8"},
        status=201,
    ),
    Case(
        "GITHUB_UPDATE_AN_ISSUE",
        {**OWNER_REPO, "issue_number": 8, "state": "closed", "state_reason": "completed"},
        "PATCH",
        f"{REPO_PATH}/issues/8",
        json={"state": "closed", "state_reason": "completed"},
        reply={"number": 8, "state": "closed"},
    ),
    Case(
        "GITHUB_CREATE_AN_ISSUE_COMMENT",
        {**OWNER_REPO, "issue_number": 8, "body": "Fixed in #9"},
        "POST",
        f"{REPO_PATH}/issues/8/comments",
        json={"body": "Fixed in #9"},
        reply={"id": 1, "html_url": "https://github.com/octocat/Hello-World/issues/8#issuecomment-1"},
        status=201,
    ),
    Case(
        "GITHUB_CREATE_A_PULL_REQUEST",
        {**OWNER_REPO, "title": "Add docs", "head": "docs", "base": "main", "draft": True},
        "POST",
        f"{REPO_PATH}/pulls",
        json={"title": "Add docs", "head": "docs", "base": "main", "draft": True},
        reply={"number": 9, "html_url": "https://github.com/octocat/Hello-World/pull/9"},
        status=201,
    ),
    Case(
        "GITHUB_CREATE_A_REVIEW_FOR_A_PULL_REQUEST",
        {**OWNER_REPO, "pull_number": 9, "event": "APPROVE"},
        "POST",
        f"{REPO_PATH}/pulls/9/reviews",
        json={"event": "APPROVE"},
        reply={"id": 80, "state": "APPROVED"},
    ),
    Case(
        "GITHUB_CREATE_A_REFERENCE",
        {**OWNER_REPO, "ref": "refs/heads/feature-x", "sha": "aa218f56b14c9653891f9e74264a383fa43fefbd"},
        "POST",
        f"{REPO_PATH}/git/refs",
        json={"ref": "refs/heads/feature-x", "sha": "aa218f56b14c9653891f9e74264a383fa43fefbd"},
        reply={"ref": "refs/heads/feature-x"},
        status=201,
    ),
    Case(
        "GITHUB_CREATE_OR_UPDATE_FILE_CONTENTS",
        {**OWNER_REPO, "path": "docs/a.md", "message": "Add a", "content": "hello", "branch": "main", "sha": "oldsha"},
        "PUT",
        f"{REPO_PATH}/contents/docs/a.md",
        json={"message": "Add a", "content": _b64("hello"), "branch": "main", "sha": "oldsha"},
        reply={"content": {"path": "docs/a.md", "sha": "newsha"}, "commit": {"sha": "c0ffee"}},
    ),
    Case(
        "GITHUB_STAR_A_REPOSITORY_FOR_THE_AUTHENTICATED_USER",
        OWNER_REPO,
        "PUT",
        "/user/starred/octocat/Hello-World",
        status=204,
    ),
]
PAGED = [c for c in CASES if c.paged]
IDS = [c.id for c in CASES]


def test_cases_cover_every_tool_exactly_once():
    assert sorted(c.slug for c in CASES) == sorted(t.slug for t in GitHubToolkit().tools())


@pytest.mark.parametrize("case", CASES, ids=IDS)
def test_happy_path(case: Case):
    h = harness(case.ok_response())
    result = h.call(case.slug, case.args)
    assert result.successful, result.error
    assert result.error is None
    call = h.session.last
    assert len(h.session.calls) == 1
    assert (call.method, call.path) == (case.method, case.path)
    assert call.params == case.params
    assert call.json == case.json
    assert call.headers["Accept"] == (RAW_MEDIA if case.raw is not None else JSON_MEDIA)
    assert isinstance(result.data, dict)
    if case.paged:
        assert list(result.data)[:4] == HINT_KEYS
        assert result.data["has_more"] is False
        assert result.data["next_page"] is None
        if isinstance(case.reply, list):
            assert result.data["items"] == case.reply


@pytest.mark.parametrize("case", PAGED, ids=[c.id for c in PAGED])
def test_pagination_hint(case: Case):
    h = harness(case.ok_response(headers=next_link(2)))
    result = h.call(case.slug, case.args)
    assert result.successful, result.error
    assert {k: result.data[k] for k in HINT_KEYS} == {
        "page": 1,
        "per_page": 30,
        "has_more": True,
        "next_page": 2,
    }


@pytest.mark.parametrize("case", PAGED, ids=[c.id for c in PAGED])
def test_next_page_is_requested_as_page(case: Case):
    h = harness(case.ok_response())
    h.call(case.slug, {**case.args, "page": 2, "per_page": 10})
    assert h.session.last.params is not None
    assert (h.session.last.params["page"], h.session.last.params["per_page"]) == (2, 10)


@pytest.mark.parametrize("case", CASES, ids=IDS)
def test_not_found(case: Case):
    h = harness(response(404, {"message": "Not Found", "documentation_url": "https://docs.github.com"}))
    result = h.call(case.slug, case.args)
    assert result.successful is False
    assert result.error.startswith("404: Not Found")
    assert "cannot access" in result.error
    assert result.data == {"status": 404}


@pytest.mark.parametrize("case", CASES, ids=IDS)
def test_unauthorized_says_reconnect(case: Case):
    h = harness(response(401, {"message": "Bad credentials"}))
    result = h.call(case.slug, case.args)
    assert result.successful is False
    assert result.error.startswith("401: Bad credentials")
    assert "must be reconnected" in result.error
    assert result.data == {"status": 401, "reconnect_required": True}


@pytest.mark.parametrize("case", CASES, ids=IDS)
def test_rate_limit_is_retried(case: Case):
    limited = response(429, {"message": "Too many requests"}, headers={"Retry-After": "2"})
    h = harness(limited, case.ok_response())
    result = h.call(case.slug, case.args)
    assert result.successful, result.error
    assert h.sleeps == [2.0]
    assert len(h.session.calls) == 2
    assert h.session.calls[0].path == h.session.calls[1].path == case.path


@pytest.mark.parametrize("case", CASES[:3], ids=IDS[:3])
def test_rate_limit_that_persists_reports_retry_after(case: Case):
    limited = [response(429, {"message": "Too many requests"}, headers={"Retry-After": "2"}) for _ in range(3)]
    h = harness(*limited)
    result = h.call(case.slug, case.args)
    assert result.successful is False
    assert result.error.startswith("429: Too many requests")
    assert "retry in about 2s" in result.error
    assert result.data == {"status": 429, "retry_after": 2}
    assert h.sleeps == [2.0, 2.0]
