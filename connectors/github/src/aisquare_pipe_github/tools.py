"""The GitHub toolkit's catalog: one :class:`ToolSpec` plus one handler per tool.

Slugs and argument names are Composio's GitHub toolkit's, verbatim, so
callers written against Composio (the BE's Content Vault import, saved
prompts) keep working. Arguments map 1:1 onto GitHub REST parameters.

Result shapes (``ToolResult.data``):

* object endpoints return GitHub's object;
* list endpoints return ``{"page", "per_page", "has_more", "next_page",
  "items": [...]}`` — one page per call; pass ``next_page`` back as ``page``
  to continue. Endpoints that already answer with an object (search,
  workflow runs) keep GitHub's keys and gain the same four hints.

The pagination hints come first so they survive a caller truncating a long
result. Hypermedia ``*_url`` fields pointing back into the REST API (and
``_links``) are dropped: a repository carries ~30 of them, they are
derivable, and they cost an LLM tokens without telling it anything.
``html_url``, ``clone_url``, ``download_url`` and friends are kept.
"""

from __future__ import annotations

import base64
import binascii
import mimetypes
import posixpath
import re
import urllib.parse
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

from aisquare.pipe import DataEnvelope, ToolError, ToolResult, ToolSpec

from aisquare_pipe_github.rest import (
    RAW_MEDIA,
    GitHubAPIError,
    GitHubRestClient,
    json_body,
    page_hint,
)

Handler = Callable[[GitHubRestClient, dict[str, Any]], ToolResult]

#: UTF-8 files up to this size also come back as text in ``data["content"]``.
RAW_TEXT_INLINE_MAX = 100_000

_MAX_PER_PAGE = 100
_DEFAULT_PER_PAGE = 30
# Path parameters that are themselves slash-separated (file paths, refs).
_SLASHED_PARAMS = frozenset({"path", "ref"})
_PLACEHOLDER = re.compile(r"\{(\w+)\}")
_MIME = mimetypes.MimeTypes()  # stdlib defaults only: same answer on every host
_MIME_OVERRIDES = {".md": "text/markdown", ".markdown": "text/markdown"}


@dataclass(frozen=True)
class GitHubTool:
    spec: ToolSpec
    handler: Handler


# --------------------------------------------------------------------------
# Schema helpers
# --------------------------------------------------------------------------


def _schema(properties: dict[str, Any], required: Iterable[str] = ()) -> dict[str, Any]:
    return {"type": "object", "properties": properties, "required": list(required)}


def _string(description: str, *, enum: Iterable[str] | None = None, nullable: bool = False) -> dict[str, Any]:
    schema: dict[str, Any] = {
        "type": ["string", "null"] if nullable else "string",
        "description": description,
    }
    if enum is not None:
        schema["enum"] = [*enum, None] if nullable else list(enum)
    return schema


def _integer(description: str) -> dict[str, Any]:
    return {"type": "integer", "description": description}


def _boolean(description: str) -> dict[str, Any]:
    return {"type": "boolean", "description": description}


def _strings(description: str) -> dict[str, Any]:
    return {"type": "array", "items": {"type": "string"}, "description": description}


def _paging(max_per_page: int = _MAX_PER_PAGE) -> dict[str, Any]:
    return {
        "page": {
            "type": "integer",
            "minimum": 1,
            "default": 1,
            "description": "Page number, 1-based. Pass the previous result's next_page to continue.",
        },
        "per_page": {
            "type": "integer",
            "minimum": 1,
            "maximum": max_per_page,
            "default": _DEFAULT_PER_PAGE,
            "description": f"Results per page (max {max_per_page}).",
        },
    }


OWNER = _string("Login of the user or organization that owns the repository, e.g. 'octocat'.")
REPO = _string("Repository name without the owner or '.git', e.g. 'Hello-World'.")
REPO_ARGS = {"owner": OWNER, "repo": REPO}
ISSUE_NUMBER = _integer("The issue's (or pull request's) number in the repository.")
PULL_NUMBER = _integer("The pull request's number in the repository.")
DIRECTION = _string("Sort direction.", enum=("asc", "desc"))
SINCE = _string("Only items updated at or after this ISO 8601 timestamp (YYYY-MM-DDTHH:MM:SSZ).")
REF = _string(
    "Branch, tag or commit SHA to read from. Defaults to the repository's default branch."
)
FILE_PATH = _string(
    "File path relative to the repository root with forward slashes, e.g. 'docs/README.md'."
)
SEARCH_ORDER = _string("Sort order; only used together with sort.", enum=("desc", "asc"))


# --------------------------------------------------------------------------
# Argument and response helpers
# --------------------------------------------------------------------------


def normalize_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    """Forgive the common slips: ``repo='owner/name'``, ``owner='owner/name'``,
    a ``.git`` suffix, a leading ``/`` on a file path."""
    args = dict(arguments)
    repo = args.get("repo")
    if isinstance(repo, str):
        args["repo"] = repo.strip().strip("/").removesuffix(".git").rpartition("/")[2]
    owner = args.get("owner")
    if isinstance(owner, str):
        args["owner"] = owner.strip().strip("/").partition("/")[0]
    path = args.get("path")
    if isinstance(path, str):
        args["path"] = path.strip().strip("/")
    return args


def _scalar(value: Any) -> Any:
    # The schema validator accepts 3.0 as an integer; GitHub would not.
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return value


def expand_path(template: str, args: dict[str, Any]) -> str:
    """Fill ``{name}`` placeholders from ``args``, percent-encoded.

    A ``.`` or ``..`` segment is refused: the HTTP stack resolves dot
    segments, so ``path='../../collaborators/x'`` would turn a file write
    into a call on a different endpoint.
    """

    def fill(match: re.Match[str]) -> str:
        name = match.group(1)
        value = str(_scalar(args[name]))
        slashed = name in _SLASHED_PARAMS
        if any(seg in (".", "..") for seg in (value.split("/") if slashed else [value])):
            raise ToolError(f"{name} must not contain '.' or '..' path segments")
        return urllib.parse.quote(value, safe="/" if slashed else "")

    return _PLACEHOLDER.sub(fill, template).rstrip("/")


def _query_value(value: Any) -> Any:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (list, tuple)):
        return ",".join(str(v) for v in value)
    return _scalar(value)


def slim(value: Any, api_prefix: str) -> Any:
    """Drop REST hypermedia: ``*_url`` keys whose value points back into the
    API (``api_prefix``), and ``_links``."""
    if isinstance(value, dict):
        return {
            key: slim(item, api_prefix)
            for key, item in value.items()
            if not (
                key == "_links"
                or (key.endswith("_url") and isinstance(item, str) and item.startswith(api_prefix))
            )
        }
    if isinstance(value, list):
        return [slim(item, api_prefix) for item in value]
    return value


def _data(client: GitHubRestClient, response: Any) -> Any:
    return slim(json_body(response), client.base_url + "/")


def _rest(
    method: str,
    path: str,
    *,
    query: Iterable[str] = (),
    body: Iterable[str] = (),
    paged: bool = False,
    max_per_page: int = _MAX_PER_PAGE,
) -> Handler:
    """Handler mapping arguments 1:1 onto one REST call: ``{placeholders}``
    in ``path``, ``query`` names as query parameters, ``body`` names as JSON
    body fields. ``paged`` adds page/per_page and the pagination hints."""
    query_names = tuple(query)
    body_names = tuple(body)

    def handler(client: GitHubRestClient, args: dict[str, Any]) -> ToolResult:
        params = {k: _query_value(args[k]) for k in query_names if k in args}
        page = per_page = 0
        if paged:
            page = max(1, int(args.get("page") or 1))
            per_page = min(max(1, int(args.get("per_page") or _DEFAULT_PER_PAGE)), max_per_page)
            params.update(page=page, per_page=per_page)
        payload = {k: args[k] for k in body_names if k in args} if body_names else None
        response = client.request(method, expand_path(path, args), params=params, json=payload)
        data = _data(client, response)
        if not paged:
            return ToolResult.ok(data if data is not None else {})
        has_more, next_page = page_hint(response)
        hints = {"page": page, "per_page": per_page, "has_more": has_more, "next_page": next_page}
        if isinstance(data, dict):
            return ToolResult.ok({**hints, **data})
        return ToolResult.ok({**hints, "items": data if isinstance(data, list) else []})

    return handler


def _guess_mimetype(name: str, content: bytes) -> str:
    ext = posixpath.splitext(name)[1].lower()
    guessed = _MIME_OVERRIDES.get(ext) or _MIME.guess_type(name)[0]
    if guessed:
        return guessed
    return "text/plain" if _utf8(content) is not None else "application/octet-stream"


def _utf8(content: bytes) -> str | None:
    try:
        return content.decode("utf-8")
    except UnicodeDecodeError:
        return None


# --------------------------------------------------------------------------
# Custom handlers
# --------------------------------------------------------------------------

_CONTENTS = "/repos/{owner}/{repo}/contents/{path}"


def _ref_param(args: dict[str, Any]) -> dict[str, Any] | None:
    return {"ref": args["ref"]} if args.get("ref") else None


def _get_repository_content(client: GitHubRestClient, args: dict[str, Any]) -> ToolResult:
    response = client.get(expand_path(_CONTENTS, args), _ref_param(args))
    data = _data(client, response)
    if isinstance(data, list):
        return ToolResult.ok({"type": "dir", "path": args["path"], "items": data})
    if (
        isinstance(data, dict)
        and data.get("encoding") == "base64"
        and isinstance(data.get("content"), str)
    ):
        try:
            # GitHub wraps the base64 at 60 columns; b64decode skips the newlines.
            text = _utf8(base64.b64decode(data["content"]))
        except (binascii.Error, ValueError):
            text = None
        if text is not None:
            data["content"] = text
            data["encoding"] = "utf-8"
    return ToolResult.ok(data)


def _is_directory_listing(response: Any) -> bool:
    """GitHub answers the raw media type for a folder with the JSON listing."""
    content_type = response.headers.get("Content-Type", "")
    if "json" not in content_type or "raw" in content_type:
        return False
    try:
        listing = response.json()
    except ValueError:
        return False
    return isinstance(listing, list) and all(
        isinstance(entry, dict) and {"type", "path", "sha"} <= entry.keys() for entry in listing
    )


def _get_raw_repository_content(client: GitHubRestClient, args: dict[str, Any]) -> ToolResult:
    response = client.get(expand_path(_CONTENTS, args), _ref_param(args), accept=RAW_MEDIA)
    if _is_directory_listing(response):
        raise ToolError("path is a directory, not a file — list it with GITHUB_GET_REPOSITORY_CONTENT")
    content = response.content
    path = args["path"]
    name = posixpath.basename(path)
    mimetype = _guess_mimetype(name, content)
    repository = f"{args['owner']}/{args['repo']}"
    data: dict[str, Any] = {
        "name": name,
        "path": path,
        "repository": repository,
        "size": len(content),
        "mimetype": mimetype,
    }
    if args.get("ref"):
        data["ref"] = args["ref"]
    text = _utf8(content) if len(content) <= RAW_TEXT_INLINE_MAX else None
    if text is not None:
        data["content"] = text
    envelope = DataEnvelope(
        content_type=mimetype,
        data=content,
        source_id=f"github:{repository}:{path}",
        metadata={"filename": name, "path": path, "repository": repository, "ref": args.get("ref")},
    )
    return ToolResult.ok(data, files=[envelope])


def _current_file_sha(client: GitHubRestClient, path: str, branch: str | None) -> str | None:
    """The blob SHA of the file at ``path`` (needed to replace it), or None
    when there is no file there yet."""
    try:
        response = client.get(path, {"ref": branch} if branch else None)
    except GitHubAPIError as e:
        if e.status == 404:
            return None
        raise
    data = json_body(response)
    sha = data.get("sha") if isinstance(data, dict) else None
    return sha if isinstance(sha, str) else None


def _create_or_update_file_contents(client: GitHubRestClient, args: dict[str, Any]) -> ToolResult:
    if args.get("encoding") == "base64":
        try:
            raw = base64.b64decode(args["content"], validate=True)
        except (binascii.Error, ValueError):
            raise ToolError("content is not valid base64 (encoding is 'base64')") from None
    else:
        raw = str(args["content"]).encode("utf-8")
    path = expand_path(_CONTENTS, args)
    body: dict[str, Any] = {
        "message": args["message"],
        "content": base64.b64encode(raw).decode("ascii"),
    }
    if args.get("branch"):
        body["branch"] = args["branch"]
    sha = args.get("sha") or _current_file_sha(client, path, args.get("branch"))
    if sha:
        body["sha"] = sha
    for role in ("author", "committer"):
        person = {
            field: args[f"{role}__{field}"]
            for field in ("name", "email", "date")
            if args.get(f"{role}__{field}")
        }
        if person:
            body[role] = person
    response = client.request("PUT", path, json=body)
    return ToolResult.ok(_data(client, response))


_create_pull_request_call = _rest(
    "POST",
    "/repos/{owner}/{repo}/pulls",
    body=("title", "head", "head_repo", "base", "body", "maintainer_can_modify", "draft", "issue"),
)


def _create_a_pull_request(client: GitHubRestClient, args: dict[str, Any]) -> ToolResult:
    if not args.get("title") and args.get("issue") is None:
        raise ToolError("title is required (or pass issue to turn an existing issue into the pull request)")
    return _create_pull_request_call(client, args)


_create_review_call = _rest(
    "POST",
    "/repos/{owner}/{repo}/pulls/{pull_number}/reviews",
    body=("commit_id", "body", "event", "comments"),
)


def _create_a_review(client: GitHubRestClient, args: dict[str, Any]) -> ToolResult:
    if args.get("event") in ("REQUEST_CHANGES", "COMMENT") and not args.get("body"):
        raise ToolError(f"body is required when event is {args['event']}")
    return _create_review_call(client, args)


def _star_a_repository(client: GitHubRestClient, args: dict[str, Any]) -> ToolResult:
    client.request("PUT", expand_path("/user/starred/{owner}/{repo}", args))
    return ToolResult.ok({"starred": True, "repository": f"{args['owner']}/{args['repo']}"})


# --------------------------------------------------------------------------
# Catalog
# --------------------------------------------------------------------------


def _read(slug: str, description: str, schema: dict[str, Any], handler: Handler, *tags: str) -> GitHubTool:
    return GitHubTool(ToolSpec(slug, description, schema, read_only=True, tags=tags), handler)


def _write(
    slug: str,
    description: str,
    schema: dict[str, Any],
    handler: Handler,
    *tags: str,
    destructive: bool = False,
) -> GitHubTool:
    return GitHubTool(
        ToolSpec(slug, description, schema, read_only=False, destructive=destructive, tags=tags),
        handler,
    )


CATALOG: tuple[GitHubTool, ...] = (
    # ---------------------------------------------------------------- users
    _read(
        "GITHUB_GET_THE_AUTHENTICATED_USER",
        "Get the profile of the GitHub user this connection belongs to: login, name, email, "
        "bio, company, plan, and public repo / follower counts. Use for 'who am I on GitHub' "
        "or to learn the user's login before a search such as 'author:<login>'.",
        _schema({}),
        _rest("GET", "/user"),
        "me", "profile", "account", "whoami",
    ),
    _read(
        "GITHUB_LIST_REPOSITORIES_FOR_THE_AUTHENTICATED_USER",
        "List the repositories the connected user can access — owned, collaborator and "
        "organization-member repos — one page at a time. Use for 'my repos' / 'my projects'. "
        "Each item has full_name, description, private, language, default_branch, "
        "stargazers_count and pushed_at. Ordered by full_name unless sort is given "
        "(sort='pushed' for most recently active). To find repositories by keyword use "
        "GITHUB_SEARCH_REPOSITORIES.",
        _schema(
            {
                "type": _string(
                    "Which repositories: all (default), owner (the user's own), public, "
                    "private, or member (collaborator / organization member).",
                    enum=("all", "owner", "public", "private", "member"),
                ),
                "sort": _string(
                    "Sort by created, updated, pushed or full_name (default).",
                    enum=("created", "updated", "pushed", "full_name"),
                ),
                "direction": _string(
                    "asc or desc. Defaults to asc for full_name, desc otherwise.",
                    enum=("asc", "desc"),
                ),
                "since": _string("Only repositories updated at or after this ISO 8601 timestamp."),
                "before": _string("Only repositories updated before this ISO 8601 timestamp."),
                **_paging(),
            }
        ),
        _rest(
            "GET",
            "/user/repos",
            query=("type", "sort", "direction", "since", "before"),
            paged=True,
        ),
        "repos", "repositories", "projects",
    ),
    _read(
        "GITHUB_LIST_ORGANIZATIONS_FOR_THE_AUTHENTICATED_USER",
        "List the organizations the connected user is a member of (login, description). "
        "Use for 'my orgs' / 'which companies am I in', or to find an org login for "
        "'org:<login>' searches. Organizations that restrict OAuth app access may be missing.",
        _schema(_paging()),
        _rest("GET", "/user/orgs", paged=True),
        "orgs", "organizations", "companies", "teams",
    ),
    # --------------------------------------------------------- repositories
    _read(
        "GITHUB_GET_A_REPOSITORY",
        "Get one repository by owner and name: description, visibility, default_branch, "
        "language, topics, stargazers/forks/open_issues counts, license, created/pushed dates "
        "and its parent when it is a fork. Use when the user names a specific repo.",
        _schema(dict(REPO_ARGS), ["owner", "repo"]),
        _rest("GET", "/repos/{owner}/{repo}"),
        "repo", "repository", "details",
    ),
    _read(
        "GITHUB_SEARCH_REPOSITORIES",
        "Search all of GitHub for repositories with GitHub search syntax, e.g. "
        "'vector database language:rust stars:>500', 'user:octocat', 'org:acme topic:cli'. "
        "Returns total_count and one page of items (best match first unless sort is set). "
        "For the connected user's own repositories prefer "
        "GITHUB_LIST_REPOSITORIES_FOR_THE_AUTHENTICATED_USER.",
        _schema(
            {
                "q": _string("Search query: keywords plus qualifiers such as language:, user:, org:, stars:, topic:."),
                "sort": _string(
                    "Sort field; omit for best match.",
                    enum=("stars", "forks", "help-wanted-issues", "updated", "created"),
                ),
                "order": SEARCH_ORDER,
                **_paging(),
            },
            ["q"],
        ),
        _rest("GET", "/search/repositories", query=("q", "sort", "order"), paged=True),
        "search", "find", "repos", "repositories",
    ),
    _read(
        "GITHUB_LIST_BRANCHES",
        "List a repository's branches with each branch's latest commit sha and whether it is "
        "protected. Use to see branches or to get a branch's head sha (e.g. to branch off it).",
        _schema(
            {
                **REPO_ARGS,
                "protected": _boolean("true: only protected branches; false: only unprotected ones."),
                **_paging(),
            },
            ["owner", "repo"],
        ),
        _rest("GET", "/repos/{owner}/{repo}/branches", query=("protected",), paged=True),
        "branches",
    ),
    _read(
        "GITHUB_LIST_COMMITS",
        "List a repository's commits, newest first: sha, message, author, committer and dates. "
        "Filter by branch or start sha (sha), file path, author, committer, and since/until. "
        "Use for 'recent commits', 'who changed this file', 'what landed last week'. For one "
        "commit's diff use GITHUB_GET_A_COMMIT.",
        _schema(
            {
                **REPO_ARGS,
                "sha": _string("Branch name or commit SHA to start listing from (default branch if omitted)."),
                "path": _string("Only commits that touch this file path."),
                "author": _string("Only commits by this GitHub login or email."),
                "committer": _string("Only commits committed by this GitHub login or email."),
                "since": _string("Only commits on or after this ISO 8601 timestamp."),
                "until": _string("Only commits before this ISO 8601 timestamp."),
                **_paging(),
            },
            ["owner", "repo"],
        ),
        _rest(
            "GET",
            "/repos/{owner}/{repo}/commits",
            query=("sha", "path", "author", "committer", "since", "until"),
            paged=True,
        ),
        "commits", "history", "log", "changes",
    ),
    _read(
        "GITHUB_GET_A_COMMIT",
        "Get one commit by SHA, branch or tag: message, author, parents, stats and the changed "
        "files with their patches. Very large commits page their file list (page/per_page).",
        _schema(
            {
                **REPO_ARGS,
                "ref": _string("Commit SHA, branch name or tag name."),
                **_paging(),
            },
            ["owner", "repo", "ref"],
        ),
        _rest("GET", "/repos/{owner}/{repo}/commits/{ref}", paged=True),
        "commit", "diff", "patch", "sha",
    ),
    # ----------------------------------------------------------------- code
    _read(
        "GITHUB_GET_REPOSITORY_CONTENT",
        "Read a file or list a folder in a repository, at an optional ref. For a file: name, "
        "path, sha, size and content — decoded to text for UTF-8 files (encoding 'utf-8'), "
        "left base64 for binary ones. For a folder: items with name, path, type (file/dir) "
        "and size. Use path '' for the repository root. Files over 1 MB come back without "
        "content; read those with GITHUB_GET_RAW_REPOSITORY_CONTENT.",
        _schema(
            {
                **REPO_ARGS,
                "path": _string("File or folder path relative to the repository root; '' for the root."),
                "ref": REF,
            },
            ["owner", "repo", "path"],
        ),
        _get_repository_content,
        "file", "folder", "directory", "read", "readme", "browse", "tree",
    ),
    _read(
        "GITHUB_GET_RAW_REPOSITORY_CONTENT",
        "Download one file from a repository as raw bytes (up to 100 MB), at an optional ref. "
        "Returns name, path, repository, size and mimetype, plus the text in content for UTF-8 "
        "files up to 100 KB; the bytes come back as a file attachment. Use to fetch a document "
        "(e.g. a GITHUB_SEARCH_CODE hit) or a large file. To browse folders use "
        "GITHUB_GET_REPOSITORY_CONTENT.",
        _schema({**REPO_ARGS, "path": FILE_PATH, "ref": REF}, ["owner", "repo", "path"]),
        _get_raw_repository_content,
        "file", "download", "raw", "read", "document",
    ),
    _read(
        "GITHUB_SEARCH_CODE",
        "Search file contents and paths in repositories the connection can see, with GitHub "
        "code-search syntax, e.g. 'TODO repo:owner/name', 'filename:roadmap.md', "
        "'useState language:typescript org:acme'. Returns total_count and items with name, "
        "path, sha, html_url and repository.full_name — read a hit with "
        "GITHUB_GET_RAW_REPOSITORY_CONTENT. Default branches only; at most 10 searches a minute.",
        _schema(
            {
                "q": _string(
                    "Code search query: keywords plus qualifiers such as repo:, org:, user:, "
                    "language:, filename:, extension:, path:."
                ),
                "sort": _string("Deprecated by GitHub; omit for best match.", enum=("indexed", "updated")),
                "order": SEARCH_ORDER,
                **_paging(),
            },
            ["q"],
        ),
        _rest("GET", "/search/code", query=("q", "sort", "order"), paged=True),
        "search", "find", "code", "files", "grep",
    ),
    # --------------------------------------------------------------- issues
    _read(
        "GITHUB_LIST_REPOSITORY_ISSUES",
        "List issues in one repository, newest first (open only by default). Pull requests "
        "are included too and carry a pull_request key. Filter by state, labels, assignee, "
        "creator, mentioned, milestone or since. For keyword or "
        "cross-repository search use GITHUB_SEARCH_ISSUES_AND_PULL_REQUESTS.",
        _schema(
            {
                **REPO_ARGS,
                "state": _string("open (default), closed or all.", enum=("open", "closed", "all")),
                "labels": _string("Comma-separated label names; issues must have all of them, e.g. 'bug,ui'."),
                "assignee": _string("Assignee login; 'none' for unassigned, '*' for any assignee."),
                "creator": _string("Login of the user who opened the issue."),
                "mentioned": _string("Login of a user mentioned in the issue."),
                "milestone": _string("Milestone number; '*' for any milestone, 'none' for none."),
                "type": _string("Issue type name; '*' for any type, 'none' for no type."),
                "sort": _string("created (default), updated or comments.", enum=("created", "updated", "comments")),
                "direction": DIRECTION,
                "since": SINCE,
                **_paging(),
            },
            ["owner", "repo"],
        ),
        _rest(
            "GET",
            "/repos/{owner}/{repo}/issues",
            query=(
                "state", "labels", "assignee", "creator", "mentioned", "milestone",
                "type", "sort", "direction", "since",
            ),
            paged=True,
        ),
        "issues", "bugs", "tickets",
    ),
    _read(
        "GITHUB_LIST_ISSUES_ASSIGNED_TO_THE_AUTHENTICATED_USER",
        "List issues and pull requests assigned to the connected user across all their "
        "repositories and organizations (open only by default). Use for 'my issues', "
        "'what is assigned to me', 'my open tasks'. filter switches to issues the user "
        "created, is mentioned in, is subscribed to, or all of those.",
        _schema(
            {
                "filter": _string(
                    "assigned (default), created, mentioned, subscribed, repos or all.",
                    enum=("assigned", "created", "mentioned", "subscribed", "repos", "all"),
                ),
                "state": _string("open (default), closed or all.", enum=("open", "closed", "all")),
                "labels": _string("Comma-separated label names, e.g. 'bug,ui'."),
                "sort": _string("created (default), updated or comments.", enum=("created", "updated", "comments")),
                "direction": DIRECTION,
                "since": SINCE,
                **_paging(),
            }
        ),
        _rest(
            "GET",
            "/issues",
            query=("filter", "state", "labels", "sort", "direction", "since"),
            paged=True,
        ),
        "issues", "assigned", "tasks", "todo",
    ),
    _read(
        "GITHUB_GET_AN_ISSUE",
        "Get one issue (or pull request) by number: title, body, state, labels, assignees, "
        "milestone, author, comment count and dates. Read its discussion with "
        "GITHUB_LIST_ISSUE_COMMENTS.",
        _schema({**REPO_ARGS, "issue_number": ISSUE_NUMBER}, ["owner", "repo", "issue_number"]),
        _rest("GET", "/repos/{owner}/{repo}/issues/{issue_number}"),
        "issue", "bug", "ticket",
    ),
    _read(
        "GITHUB_LIST_ISSUE_COMMENTS",
        "List the comments in an issue's or pull request's conversation, oldest first "
        "(author, body, created_at). Use to read a discussion. Review comments attached to "
        "pull request code lines are not included.",
        _schema(
            {**REPO_ARGS, "issue_number": ISSUE_NUMBER, "since": SINCE, **_paging()},
            ["owner", "repo", "issue_number"],
        ),
        _rest(
            "GET",
            "/repos/{owner}/{repo}/issues/{issue_number}/comments",
            query=("since",),
            paged=True,
        ),
        "comments", "discussion", "conversation", "issue",
    ),
    _read(
        "GITHUB_SEARCH_ISSUES_AND_PULL_REQUESTS",
        "Search issues and pull requests across GitHub with search qualifiers; include "
        "is:issue or is:pr. E.g. 'is:pr is:open author:@me', 'is:pr is:open "
        "review-requested:@me', 'is:issue label:bug repo:owner/name', 'is:issue org:acme "
        "mentions:octocat'. Returns total_count and one page of items. Best for keywords, "
        "cross-repo questions and 'PRs waiting for my review'.",
        _schema(
            {
                "q": _string("Search query with qualifiers such as is:, repo:, org:, author:, assignee:, label:, state:."),
                "sort": _string(
                    "Sort field; omit for best match.",
                    enum=(
                        "comments", "reactions", "reactions-+1", "reactions--1",
                        "reactions-smile", "reactions-thinking_face", "reactions-heart",
                        "reactions-tada", "interactions", "created", "updated",
                    ),
                ),
                "order": SEARCH_ORDER,
                **_paging(),
            },
            ["q"],
        ),
        _rest("GET", "/search/issues", query=("q", "sort", "order"), paged=True),
        "search", "find", "issues", "pull requests", "prs", "review",
    ),
    # -------------------------------------------------------- pull requests
    _read(
        "GITHUB_LIST_PULL_REQUESTS",
        "List pull requests in one repository (open only by default): number, title, state, "
        "draft, author, head and base branches, dates. Filter by state (open/closed/all), "
        "base branch, or head 'user:branch'; sort by created, updated, popularity or "
        "long-running.",
        _schema(
            {
                **REPO_ARGS,
                "state": _string("open (default), closed or all.", enum=("open", "closed", "all")),
                "head": _string("Only PRs from this head, as 'user:branch' or 'org:branch'."),
                "base": _string("Only PRs into this base branch, e.g. 'main'."),
                "sort": _string(
                    "created (default), updated, popularity or long-running.",
                    enum=("created", "updated", "popularity", "long-running"),
                ),
                "direction": DIRECTION,
                **_paging(),
            },
            ["owner", "repo"],
        ),
        _rest(
            "GET",
            "/repos/{owner}/{repo}/pulls",
            query=("state", "head", "base", "sort", "direction"),
            paged=True,
        ),
        "pull requests", "prs", "pulls", "merge requests", "reviews",
    ),
    _read(
        "GITHUB_GET_A_PULL_REQUEST",
        "Get one pull request by number: title, body, state, draft, merged, mergeable, author, "
        "head/base branches, requested reviewers, additions/deletions/changed_files and dates. "
        "List its changed files with GITHUB_LIST_PULL_REQUESTS_FILES.",
        _schema({**REPO_ARGS, "pull_number": PULL_NUMBER}, ["owner", "repo", "pull_number"]),
        _rest("GET", "/repos/{owner}/{repo}/pulls/{pull_number}"),
        "pull request", "pr", "merge",
    ),
    _read(
        "GITHUB_LIST_PULL_REQUESTS_FILES",
        "List the files a pull request changes: filename, status (added, modified, removed, "
        "renamed), additions, deletions and patch. Use to see or summarize a PR's diff.",
        _schema(
            {**REPO_ARGS, "pull_number": PULL_NUMBER, **_paging()},
            ["owner", "repo", "pull_number"],
        ),
        _rest("GET", "/repos/{owner}/{repo}/pulls/{pull_number}/files", paged=True),
        "pull request", "pr", "diff", "files", "changes",
    ),
    # ------------------------------------------------------ releases and CI
    _read(
        "GITHUB_LIST_RELEASES",
        "List a repository's releases, newest first: tag_name, name, body (release notes), "
        "draft, prerelease, published_at and assets. Use for 'latest release', versions and "
        "changelogs.",
        _schema({**REPO_ARGS, **_paging()}, ["owner", "repo"]),
        _rest("GET", "/repos/{owner}/{repo}/releases", paged=True),
        "releases", "versions", "tags", "changelog",
    ),
    _read(
        "GITHUB_LIST_WORKFLOW_RUNS_FOR_A_REPOSITORY",
        "List GitHub Actions workflow runs for a repository, newest first: name, status, "
        "conclusion, head_branch, event, head_sha, actor, run_started_at and html_url, plus "
        "total_count. Filter by branch, event, status (e.g. failure, success, in_progress), "
        "actor, head_sha or created date. Use for CI / build / deploy status questions.",
        _schema(
            {
                **REPO_ARGS,
                "branch": _string("Only runs for this branch."),
                "event": _string("Only runs triggered by this event, e.g. push, pull_request, schedule."),
                "status": _string(
                    "Only runs with this status or conclusion.",
                    enum=(
                        "completed", "action_required", "cancelled", "failure", "neutral",
                        "skipped", "stale", "success", "timed_out", "in_progress", "queued",
                        "requested", "waiting", "pending",
                    ),
                ),
                "actor": _string("Only runs started by this login."),
                "head_sha": _string("Only runs for this commit SHA."),
                "created": _string("Created-date filter, e.g. '>=2026-01-01' or '2026-01-01..2026-01-31'."),
                "check_suite_id": _integer("Only runs in this check suite."),
                "exclude_pull_requests": _boolean("true leaves out pull-request-triggered runs."),
                **_paging(),
            },
            ["owner", "repo"],
        ),
        _rest(
            "GET",
            "/repos/{owner}/{repo}/actions/runs",
            query=(
                "branch", "event", "status", "actor", "head_sha", "created",
                "check_suite_id", "exclude_pull_requests",
            ),
            paged=True,
        ),
        "actions", "ci", "builds", "workflows", "runs", "deploys", "checks",
    ),
    _read(
        "GITHUB_LIST_REPOSITORY_CONTRIBUTORS",
        "List a repository's contributors, most commits first (login, contributions). Use for "
        "'who works on this repo' / 'top contributors'.",
        _schema(
            {
                **REPO_ARGS,
                "anon": _string("'1' or 'true' to include anonymous (email-only) contributors."),
                **_paging(),
            },
            ["owner", "repo"],
        ),
        _rest("GET", "/repos/{owner}/{repo}/contributors", query=("anon",), paged=True),
        "contributors", "authors", "people", "team",
    ),
    _read(
        "GITHUB_LIST_NOTIFICATIONS_FOR_THE_AUTHENTICATED_USER",
        "List the connected user's GitHub notifications (unread only by default), newest "
        "first: reason (mention, review_requested, assign, ...), subject title and type, "
        "repository, updated_at. all=true includes read ones; participating=true keeps only "
        "threads the user takes part in or is mentioned in.",
        _schema(
            {
                "all": _boolean("true includes notifications already marked read."),
                "participating": _boolean("true keeps only direct participation and mentions."),
                "since": _string("Only notifications updated after this ISO 8601 timestamp."),
                "before": _string("Only notifications updated before this ISO 8601 timestamp."),
                **_paging(max_per_page=50),
            }
        ),
        _rest(
            "GET",
            "/notifications",
            query=("all", "participating", "since", "before"),
            paged=True,
            max_per_page=50,
        ),
        "notifications", "inbox", "mentions", "unread",
    ),
    # --------------------------------------------------------------- writes
    _write(
        "GITHUB_CREATE_AN_ISSUE",
        "Open a new issue in a repository with a title and optional Markdown body, labels, "
        "assignees and milestone. Returns the created issue (number, html_url). Labels, "
        "assignees and milestone are dropped unless the user has push access.",
        _schema(
            {
                **REPO_ARGS,
                "title": _string("Issue title."),
                "body": _string("Issue body in GitHub Flavored Markdown."),
                "labels": _strings("Label names to apply."),
                "assignees": _strings("Logins to assign."),
                "assignee": _string("Login to assign (deprecated; prefer assignees)."),
                "milestone": {
                    "type": ["string", "integer"],
                    "description": "Milestone number to attach, e.g. 5.",
                },
            },
            ["owner", "repo", "title"],
        ),
        _rest(
            "POST",
            "/repos/{owner}/{repo}/issues",
            body=("title", "body", "labels", "assignees", "assignee", "milestone"),
        ),
        "create", "open", "new", "issue", "bug", "report",
    ),
    _write(
        "GITHUB_UPDATE_AN_ISSUE",
        "Change an existing issue or pull request: title, body, state (open/closed, with "
        "state_reason), labels, assignees or milestone. Only the fields you pass change; "
        "labels and assignees replace the current lists. Use to close, reopen, relabel, "
        "reassign or edit.",
        _schema(
            {
                **REPO_ARGS,
                "issue_number": ISSUE_NUMBER,
                "title": _string("New title."),
                "body": _string("New body; null clears it.", nullable=True),
                "state": _string("open or closed.", enum=("open", "closed")),
                "state_reason": _string(
                    "Why the state changed: completed, not_planned or reopened.",
                    enum=("completed", "not_planned", "reopened"),
                    nullable=True,
                ),
                "labels": _strings("Replaces all labels; [] removes them all."),
                "assignees": _strings("Replaces all assignees; [] removes them all."),
                "assignee": _string("Login to assign (deprecated); null unassigns.", nullable=True),
                "milestone": {
                    "type": ["string", "integer", "null"],
                    "description": "Milestone number; null removes the milestone.",
                },
            },
            ["owner", "repo", "issue_number"],
        ),
        _rest(
            "PATCH",
            "/repos/{owner}/{repo}/issues/{issue_number}",
            body=(
                "title", "body", "state", "state_reason", "labels", "assignees",
                "assignee", "milestone",
            ),
        ),
        "update", "edit", "close", "reopen", "label", "assign", "issue",
        destructive=True,
    ),
    _write(
        "GITHUB_CREATE_AN_ISSUE_COMMENT",
        "Add a Markdown comment to an issue's or pull request's conversation. Returns the "
        "comment (id, html_url).",
        _schema(
            {**REPO_ARGS, "issue_number": ISSUE_NUMBER, "body": _string("Comment text in GitHub Flavored Markdown.")},
            ["owner", "repo", "issue_number", "body"],
        ),
        _rest(
            "POST",
            "/repos/{owner}/{repo}/issues/{issue_number}/comments",
            body=("body",),
        ),
        "comment", "reply", "issue", "pull request",
    ),
    _write(
        "GITHUB_CREATE_A_PULL_REQUEST",
        "Open a pull request merging head (a branch of this repository, or 'user:branch' from "
        "a fork) into base. Needs a title, or issue to turn an existing issue into the pull "
        "request; optional body and draft. Both branches must exist and differ. Returns the "
        "pull request (number, html_url).",
        _schema(
            {
                **REPO_ARGS,
                "title": _string("Pull request title (required unless issue is given)."),
                "head": _string("Branch with the changes: 'branch', or 'user:branch' for a fork."),
                "base": _string("Branch to merge into, e.g. 'main'."),
                "body": _string("Description in GitHub Flavored Markdown."),
                "draft": _boolean("Open as a draft pull request."),
                "issue": _integer("Number of an existing issue to convert into this pull request."),
                "head_repo": _string("'owner/name' of the head repository when it differs from the base one."),
                "maintainer_can_modify": _boolean("Let base-repository maintainers push to the head branch."),
            },
            ["owner", "repo", "head", "base"],
        ),
        _create_a_pull_request,
        "open", "new", "pull request", "pr", "merge",
    ),
    _write(
        "GITHUB_CREATE_A_REVIEW_FOR_A_PULL_REQUEST",
        "Review a pull request: event APPROVE, REQUEST_CHANGES or COMMENT, with a body "
        "(required for REQUEST_CHANGES and COMMENT) and optional inline comments on file "
        "lines. Without event the review stays PENDING (not submitted).",
        _schema(
            {
                **REPO_ARGS,
                "pull_number": PULL_NUMBER,
                "event": _string(
                    "APPROVE, REQUEST_CHANGES or COMMENT; omit to leave the review pending.",
                    enum=("APPROVE", "REQUEST_CHANGES", "COMMENT"),
                ),
                "body": _string("Review summary in GitHub Flavored Markdown."),
                "commit_id": _string("Commit SHA to review; defaults to the pull request's latest commit."),
                "comments": {
                    "type": "array",
                    "description": "Inline comments; each needs path and body, plus line (and optionally side, start_line, start_side).",
                    "items": {
                        "type": "object",
                        "properties": {
                            "path": _string("File path in the pull request."),
                            "body": _string("Comment text."),
                            "line": _integer("Line in the file's diff to comment on."),
                            "side": _string("LEFT (old) or RIGHT (new, default).", enum=("LEFT", "RIGHT")),
                            "start_line": _integer("First line of a multi-line comment."),
                            "start_side": _string("Side of start_line.", enum=("LEFT", "RIGHT")),
                            "position": _integer("Deprecated diff position; prefer line."),
                        },
                        "required": ["path", "body"],
                    },
                },
            },
            ["owner", "repo", "pull_number"],
        ),
        _create_a_review,
        "review", "approve", "request changes", "pull request", "pr",
    ),
    _write(
        "GITHUB_CREATE_A_REFERENCE",
        "Create a git reference pointing at an existing commit — a new branch "
        "('refs/heads/<name>') or tag ('refs/tags/<name>'). To branch off main, take main's "
        "head sha from GITHUB_LIST_BRANCHES first.",
        _schema(
            {
                **REPO_ARGS,
                "ref": _string("Fully qualified reference, starting with 'refs/', e.g. 'refs/heads/feature-x'."),
                "sha": _string("Commit SHA the new reference points to."),
            },
            ["owner", "repo", "ref", "sha"],
        ),
        _rest("POST", "/repos/{owner}/{repo}/git/refs", body=("ref", "sha")),
        "create", "branch", "tag", "reference",
    ),
    _write(
        "GITHUB_CREATE_OR_UPDATE_FILE_CONTENTS",
        "Create a file, or replace a file's whole contents, in a repository as one commit on a "
        "branch (the default branch if omitted). content is the complete new file text (set "
        "encoding='base64' when passing base64 bytes); message is the commit message. The "
        "current file's sha is looked up automatically when replacing. Returns the commit "
        "and the file.",
        _schema(
            {
                **REPO_ARGS,
                "path": FILE_PATH,
                "message": _string("Commit message."),
                "content": _string("The complete new file contents."),
                "encoding": _string(
                    "How content is encoded: utf-8 text (default) or base64.",
                    enum=("utf-8", "base64"),
                ),
                "branch": _string("Branch to commit to; defaults to the repository's default branch."),
                "sha": _string("Blob SHA of the file being replaced; looked up automatically when omitted."),
                "author__name": _string("Commit author name (with author__email)."),
                "author__email": _string("Commit author email (with author__name)."),
                "author__date": _string("Commit author timestamp, ISO 8601."),
                "committer__name": _string("Committer name (with committer__email)."),
                "committer__email": _string("Committer email (with committer__name)."),
                "committer__date": _string("Committer timestamp, ISO 8601."),
            },
            ["owner", "repo", "path", "message", "content"],
        ),
        _create_or_update_file_contents,
        "write", "edit", "commit", "file", "upload", "save",
        destructive=True,
    ),
    _write(
        "GITHUB_STAR_A_REPOSITORY_FOR_THE_AUTHENTICATED_USER",
        "Star a repository as the connected user. Safe to repeat; returns starred: true.",
        _schema(dict(REPO_ARGS), ["owner", "repo"]),
        _star_a_repository,
        "star", "favorite", "bookmark", "like",
    ),
)

TOOL_SPECS: tuple[ToolSpec, ...] = tuple(tool.spec for tool in CATALOG)
HANDLERS: dict[str, Handler] = {tool.spec.slug: tool.handler for tool in CATALOG}
