"""Live read-only checks against api.github.com (opt-in).

Run with ``pipe validate github --live`` or ``pytest -m live tests/test_live.py``.
Needs ``GITHUB_TOKEN`` (a classic PAT or OAuth token with ``repo``,
``read:user``, ``read:org``, ``notifications``); self-skips without it.
``GITHUB_LIVE_REPO`` (default ``octocat/Hello-World``) is the public
repository the per-repo tools read. No write tool is ever called.
"""

from __future__ import annotations

import os

import pytest

from aisquare_pipe_github import GitHubToolkit

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(not os.environ.get("GITHUB_TOKEN"), reason="GITHUB_TOKEN not set"),
]

OWNER, _, NAME = os.environ.get("GITHUB_LIVE_REPO", "octocat/Hello-World").partition("/")
REPO = {"owner": OWNER, "repo": NAME}

READS = [
    ("GITHUB_GET_THE_AUTHENTICATED_USER", {}),
    ("GITHUB_LIST_REPOSITORIES_FOR_THE_AUTHENTICATED_USER", {"per_page": 3, "sort": "pushed"}),
    ("GITHUB_LIST_ORGANIZATIONS_FOR_THE_AUTHENTICATED_USER", {"per_page": 3}),
    ("GITHUB_GET_A_REPOSITORY", REPO),
    ("GITHUB_SEARCH_REPOSITORIES", {"q": f"repo:{OWNER}/{NAME}", "per_page": 1}),
    ("GITHUB_LIST_BRANCHES", {**REPO, "per_page": 3}),
    ("GITHUB_LIST_COMMITS", {**REPO, "per_page": 3}),
    ("GITHUB_GET_REPOSITORY_CONTENT", {**REPO, "path": ""}),
    ("GITHUB_SEARCH_CODE", {"q": f"repo:{OWNER}/{NAME} hello", "per_page": 3}),
    ("GITHUB_LIST_REPOSITORY_ISSUES", {**REPO, "state": "all", "per_page": 3}),
    ("GITHUB_LIST_ISSUES_ASSIGNED_TO_THE_AUTHENTICATED_USER", {"per_page": 3}),
    ("GITHUB_SEARCH_ISSUES_AND_PULL_REQUESTS", {"q": f"repo:{OWNER}/{NAME} is:pr", "per_page": 3}),
    ("GITHUB_LIST_PULL_REQUESTS", {**REPO, "state": "all", "per_page": 3}),
    ("GITHUB_LIST_RELEASES", {**REPO, "per_page": 3}),
    ("GITHUB_LIST_WORKFLOW_RUNS_FOR_A_REPOSITORY", {**REPO, "per_page": 3}),
    ("GITHUB_LIST_REPOSITORY_CONTRIBUTORS", {**REPO, "per_page": 3}),
    ("GITHUB_LIST_NOTIFICATIONS_FOR_THE_AUTHENTICATED_USER", {"per_page": 3}),
]


@pytest.fixture(scope="module")
def call():
    toolkit = GitHubToolkit()
    config = {"access_token": os.environ.get("GITHUB_TOKEN", "")}

    def run(slug, arguments):
        result = toolkit.call_tool(slug, arguments, config)
        assert result.successful, f"{slug}: {result.error}"
        return result

    return run


@pytest.mark.parametrize(("slug", "arguments"), READS, ids=[s.removeprefix("GITHUB_") for s, _ in READS])
def test_read_tool(call, slug, arguments):
    data = call(slug, arguments).data
    assert isinstance(data, dict)
    if "per_page" in arguments:
        assert {"page", "per_page", "has_more", "next_page"} <= data.keys()


def test_file_reads(call):
    root = call("GITHUB_GET_REPOSITORY_CONTENT", {**REPO, "path": ""}).data
    first_file = next(item for item in root["items"] if item["type"] == "file")
    content = call("GITHUB_GET_REPOSITORY_CONTENT", {**REPO, "path": first_file["path"]}).data
    assert content["type"] == "file"
    raw = call("GITHUB_GET_RAW_REPOSITORY_CONTENT", {**REPO, "path": first_file["path"]})
    assert raw.files[0].data
    assert raw.data["size"] == len(raw.files[0].data) == first_file["size"]


def test_raw_read_of_a_directory_is_refused(call):
    root = call("GITHUB_GET_REPOSITORY_CONTENT", {**REPO, "path": ""}).data
    folder = next((item for item in root["items"] if item["type"] == "dir"), None)
    if folder is None:
        pytest.skip(f"{OWNER}/{NAME} has no top-level folder")
    result = GitHubToolkit().call_tool(
        "GITHUB_GET_RAW_REPOSITORY_CONTENT", {**REPO, "path": folder["path"]}, {"access_token": os.environ["GITHUB_TOKEN"]}
    )
    assert result.successful is False
    assert "directory" in result.error


def test_commit_issue_and_pull_request_by_id(call):
    commits = call("GITHUB_LIST_COMMITS", {**REPO, "per_page": 1}).data["items"]
    call("GITHUB_GET_A_COMMIT", {**REPO, "ref": commits[0]["sha"], "per_page": 5})
    issues = call("GITHUB_LIST_REPOSITORY_ISSUES", {**REPO, "state": "all", "per_page": 1}).data["items"]
    if issues:
        call("GITHUB_GET_AN_ISSUE", {**REPO, "issue_number": issues[0]["number"]})
        call("GITHUB_LIST_ISSUE_COMMENTS", {**REPO, "issue_number": issues[0]["number"], "per_page": 3})
    pulls = call("GITHUB_LIST_PULL_REQUESTS", {**REPO, "state": "all", "per_page": 1}).data["items"]
    if pulls:
        call("GITHUB_GET_A_PULL_REQUEST", {**REPO, "pull_number": pulls[0]["number"]})
        call("GITHUB_LIST_PULL_REQUESTS_FILES", {**REPO, "pull_number": pulls[0]["number"], "per_page": 3})


def test_pagination_hint_is_live(call):
    data = call("GITHUB_LIST_COMMITS", {**REPO, "per_page": 1}).data
    if data["has_more"]:
        assert data["next_page"] == 2


def test_bad_token_says_reconnect():
    result = GitHubToolkit().call_tool(
        "GITHUB_GET_THE_AUTHENTICATED_USER", {}, {"access_token": "gho_definitelyNotAValidToken000000000000"}
    )
    assert result.successful is False
    assert result.error.startswith("401")
    assert result.error_code == "unauthorized"
