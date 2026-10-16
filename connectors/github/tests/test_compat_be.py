"""Compatibility with the BE's Content Vault import, which calls ``/fetch``
with Composio slugs (AISquare-Studio-BE
``aisquare_studio/content_vault/connector_import.py``):

* search: ``GITHUB_SEARCH_CODE`` with ``{"q": <query>, "per_page": 5}``; it
  walks every dict under the fetch payload's ``items`` and keeps those with
  ``path`` and ``repository.full_name``, building the candidate id
  ``"<owner>/<repo>::<path>"``.
* download: ``GITHUB_GET_RAW_REPOSITORY_CONTENT`` with ``{"owner", "repo",
  "path"}`` split back out of that id; it needs the file bytes.

The BE helpers are mirrored here verbatim (``_be_*``) so a drift on either
side fails this file. The bytes travel in ``ToolResult.files``; the pipeAAS
``/fetch`` route serializes them (``s3url`` / ``file_content_bytes`` are what
the BE's ``_extract_bytes`` reads) — that mapping is pipeAAS's side of the
contract.
"""

from __future__ import annotations

import os
from typing import Any

from .fakes import RAW_CONTENT_TYPE, harness, response

BE_MAX_CANDIDATES = 5
COMPOSITE_ID_SEPARATOR = "::"

SEARCH_CODE_REPLY = {
    "total_count": 2,
    "incomplete_results": False,
    "items": [
        {
            "name": "roadmap.md",
            "path": "docs/roadmap.md",
            "sha": "bbcd538c8e72b8c175046e27cc8f907076331401",
            "url": "https://api.github.com/repositories/1296269/contents/docs/roadmap.md?ref=7fd1a60",
            "git_url": "https://api.github.com/repositories/1296269/git/blobs/bbcd538",
            "html_url": "https://github.com/octocat/Hello-World/blob/7fd1a60/docs/roadmap.md",
            "repository": {
                "id": 1296269,
                "name": "Hello-World",
                "full_name": "octocat/Hello-World",
                "owner": {"login": "octocat"},
                "private": False,
            },
            "score": 1.0,
        },
        {
            "name": "roadmap-2025.md",
            "path": "archive/roadmap-2025.md",
            "repository": {"full_name": "acme/plans"},
        },
    ],
}


# ---- mirrored from connector_import.py --------------------------------------


def _be_iter_dicts(node: Any):
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from _be_iter_dicts(value)
    elif isinstance(node, list):
        for value in node:
            yield from _be_iter_dicts(value)


def _be_search_github_candidates(payload: dict[str, Any]) -> list[dict[str, Any]]:
    out = []
    for node in _be_iter_dicts(payload.get("items")):
        path = node.get("path")
        repo_full_name = (node.get("repository") or {}).get("full_name")
        if not path or not repo_full_name:
            continue
        out.append({"file_id": f"{repo_full_name}{COMPOSITE_ID_SEPARATOR}{path}", "name": os.path.basename(path)})
    return out


def _be_github_download_args(meta: dict[str, Any]) -> dict[str, Any]:
    repo_full_name, _, path = meta["source_id"].partition(COMPOSITE_ID_SEPARATOR)
    owner, _, repo = repo_full_name.partition("/")
    return {"owner": owner, "repo": repo, "path": path}


# ------------------------------------------------------------------------------


def _fetch_payload(data: Any) -> dict[str, Any]:
    """What /fetch hands the BE: the tool's data as the result item."""
    return {"items": [data]}


class TestSearchCode:
    def test_accepts_the_arguments_be_sends(self):
        h = harness(response(200, SEARCH_CODE_REPLY))
        result = h.call("GITHUB_SEARCH_CODE", {"q": "roadmap", "per_page": BE_MAX_CANDIDATES})
        assert result.successful, result.error
        call = h.session.last
        assert (call.method, call.path) == ("GET", "/search/code")
        assert call.params == {"q": "roadmap", "page": 1, "per_page": BE_MAX_CANDIDATES}

    def test_result_has_the_fields_be_reads(self):
        h = harness(response(200, SEARCH_CODE_REPLY))
        data = h.call("GITHUB_SEARCH_CODE", {"q": "roadmap", "per_page": BE_MAX_CANDIDATES}).data
        for item in data["items"]:
            assert item["path"]
            assert item["repository"]["full_name"]
        assert _be_search_github_candidates(_fetch_payload(data)) == [
            {"file_id": "octocat/Hello-World::docs/roadmap.md", "name": "roadmap.md"},
            {"file_id": "acme/plans::archive/roadmap-2025.md", "name": "roadmap-2025.md"},
        ]


class TestGetRawRepositoryContent:
    def test_accepts_the_arguments_be_sends(self):
        args = _be_github_download_args({"source_id": "octocat/Hello-World::docs/roadmap.md"})
        assert args == {"owner": "octocat", "repo": "Hello-World", "path": "docs/roadmap.md"}
        h = harness(response(200, content=b"# Roadmap\n", content_type=RAW_CONTENT_TYPE))
        result = h.call("GITHUB_GET_RAW_REPOSITORY_CONTENT", args)
        assert result.successful, result.error
        call = h.session.last
        assert (call.method, call.path) == ("GET", "/repos/octocat/Hello-World/contents/docs/roadmap.md")

    def test_bytes_and_name_reach_the_caller(self):
        h = harness(response(200, content=b"%PDF-1.7 roadmap", content_type=RAW_CONTENT_TYPE))
        result = h.call("GITHUB_GET_RAW_REPOSITORY_CONTENT", {"owner": "octocat", "repo": "Hello-World", "path": "docs/roadmap.pdf"})
        (envelope,) = result.files
        assert envelope.data == b"%PDF-1.7 roadmap"
        assert envelope.content_type == "application/pdf"
        assert envelope.metadata["filename"] == "roadmap.pdf"
        assert result.data["name"] == "roadmap.pdf"
        assert result.data["size"] == len(b"%PDF-1.7 roadmap")

    def test_search_hit_round_trips_into_a_download(self):
        h = harness(
            response(200, SEARCH_CODE_REPLY),
            response(200, content=b"# Roadmap\n", content_type=RAW_CONTENT_TYPE),
        )
        found = h.call("GITHUB_SEARCH_CODE", {"q": "roadmap", "per_page": BE_MAX_CANDIDATES})
        candidate = _be_search_github_candidates(_fetch_payload(found.data))[0]
        download = h.call("GITHUB_GET_RAW_REPOSITORY_CONTENT", _be_github_download_args({"source_id": candidate["file_id"]}))
        assert download.files[0].data == b"# Roadmap\n"
        assert h.session.last.path == "/repos/octocat/Hello-World/contents/docs/roadmap.md"
