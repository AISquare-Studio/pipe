# aisquare-pipe-github

GitHub for [aisquare.pipe](../../README.md). The package ships two things:

| What | Registered as | Use it for |
|---|---|---|
| `GitHubToolkit` | toolkit `github` (`aisquare_pipe.toolkits`) | 31 LLM-callable tools over the GitHub REST API — repos, code, issues, pull requests, releases, Actions runs, notifications — plus GitHub's OAuth App spec |
| `GitHubRepoSource` | connector `github-source` (`aisquare_pipe.connectors`) | Shallow-clone one repo into a checkout handle for a pipeline |

## Install

```bash
cd connectors/github
pip install -e ".[dev]"
```

## Toolkit

```python
from aisquare_pipe_github import GitHubToolkit

github = GitHubToolkit()
config = {"access_token": "gho_..."}  # OAuth App user token (a PAT works too)

github.search_tools("list my repos", limit=3)        # -> [ToolSpec, ...]
result = github.call_tool(
    "GITHUB_LIST_REPOSITORIES_FOR_THE_AUTHENTICATED_USER", {"sort": "pushed"}, config
)
result.successful, result.data, result.error
```

`call_tool` never raises. Slugs and argument names are Composio's GitHub
toolkit's, verbatim, so callers built on Composio keep working.

**Results.** Object endpoints return GitHub's object. List endpoints return one
page per call, with the pagination hints first:

```python
{"page": 1, "per_page": 30, "has_more": True, "next_page": 2, "items": [...]}
```

Pass `next_page` back as `page` to continue. Search and workflow-run endpoints
keep GitHub's keys (`total_count`, `items` / `workflow_runs`) and gain the same
hints. Hypermedia `*_url` fields that point back into `api.github.com` are
dropped to save tokens; `html_url`, `clone_url`, `download_url` etc. stay.

**Errors.** A failed call carries core's canonical `error_code` (branch on
this), a human-readable `error` (`"<status>: <GitHub's message>"` plus a hint),
`retry_after` (seconds) when rate limited, and `data={"status": <HTTP status>}`
when GitHub answered:

| Failure | `error_code` | `error` |
|---|---|---|
| 401 | `unauthorized` | `401: Bad credentials — … the GitHub connection must be reconnected` |
| 429, or a rate-limit 403 that outlasts the retries/budget | `rate_limited` (+ `retry_after`) | `429: … — GitHub rate limit; retry in about 42s` |
| other 403 | `forbidden` | `403: … — this GitHub connection is not allowed to do that (missing scope …)` |
| 404, 410 | `not_found` | `404: Not Found — it does not exist, or this GitHub connection cannot access it` |
| 400, 422, and arguments the toolkit refuses (`..` segments, a folder given to a file tool, bad base64, …) | `invalid_arguments` | GitHub's validation message, or the refusal |
| 409, 412 | `conflict` | GitHub's message |
| request timeout, 504, spent `timeout_seconds` | `timeout` | `GitHub did not answer within 30s (ReadTimeout)` |
| 5xx, network failure | `upstream_error` | `500: …` / `network error talking to GitHub (ConnectionError)` |

Core adds `unknown_tool`, `invalid_arguments` (schema) and `unauthorized`
(no `access_token`) before `run_tool` is reached.

**Rate limits.** A 429, or a 403 GitHub marks as a rate limit, is retried
(default 2 retries) after the wait GitHub asks for (`Retry-After`, else
`x-ratelimit-reset`). Total sleep per call is capped at 60 s; a longer wait
fails at once with `retry_after`. A 502/503/504 on a GET is retried with
backoff (1 s, 2 s). Tune with `GitHubToolkit(max_retries=..., max_retry_wait=...)`.

**Time budget.** A host may set `config["timeout_seconds"]` (pipe-as-service
does). The whole call fits inside it: each request's timeout is the smaller of
30 s and the time left, and a retry that would sleep past it fails at once as
`rate_limited`. A spent budget is `timeout`.

**Concurrency.** A `GitHubToolkit` holds no mutable state, and each call opens
and closes its own HTTP session. Concurrent calls, on one instance or on fresh
ones, share nothing. Passing `session=` opts into a shared session the host
manages.

**Secrets.** The token is sent only in the `Authorization` header. It never
appears in a URL, log line, error or repr. Logs carry method, status and timing,
never paths or arguments.

### Tools

Read-only:

| Slug | GitHub endpoint | Use for |
|---|---|---|
| `GITHUB_GET_THE_AUTHENTICATED_USER` | `GET /user` | who the connected user is |
| `GITHUB_LIST_REPOSITORIES_FOR_THE_AUTHENTICATED_USER` | `GET /user/repos` | "my repos" |
| `GITHUB_LIST_ORGANIZATIONS_FOR_THE_AUTHENTICATED_USER` | `GET /user/orgs` | "my orgs" |
| `GITHUB_GET_A_REPOSITORY` | `GET /repos/{owner}/{repo}` | one repo's details |
| `GITHUB_SEARCH_REPOSITORIES` | `GET /search/repositories` | find repos by keyword/qualifier |
| `GITHUB_LIST_BRANCHES` | `GET /repos/{owner}/{repo}/branches` | branches and head shas |
| `GITHUB_LIST_COMMITS` | `GET /repos/{owner}/{repo}/commits` | history, filtered by path/author/date |
| `GITHUB_GET_A_COMMIT` | `GET /repos/{owner}/{repo}/commits/{ref}` | one commit with its diff |
| `GITHUB_GET_REPOSITORY_CONTENT` | `GET /repos/{owner}/{repo}/contents/{path}` | read a file (text decoded) or list a folder |
| `GITHUB_GET_RAW_REPOSITORY_CONTENT` | same, raw media type | download a file's bytes (`ToolResult.files`) |
| `GITHUB_SEARCH_CODE` | `GET /search/code` | find files by content/name |
| `GITHUB_LIST_REPOSITORY_ISSUES` | `GET /repos/{owner}/{repo}/issues` | a repo's issues (and PRs) |
| `GITHUB_LIST_ISSUES_ASSIGNED_TO_THE_AUTHENTICATED_USER` | `GET /issues` | "my issues" |
| `GITHUB_GET_AN_ISSUE` | `GET /repos/{owner}/{repo}/issues/{issue_number}` | one issue |
| `GITHUB_LIST_ISSUE_COMMENTS` | `GET /repos/{owner}/{repo}/issues/{issue_number}/comments` | an issue's discussion |
| `GITHUB_SEARCH_ISSUES_AND_PULL_REQUESTS` | `GET /search/issues` | cross-repo issue/PR search, "PRs awaiting my review" |
| `GITHUB_LIST_PULL_REQUESTS` | `GET /repos/{owner}/{repo}/pulls` | a repo's PRs |
| `GITHUB_GET_A_PULL_REQUEST` | `GET /repos/{owner}/{repo}/pulls/{pull_number}` | one PR |
| `GITHUB_LIST_PULL_REQUESTS_FILES` | `GET /repos/{owner}/{repo}/pulls/{pull_number}/files` | a PR's changed files |
| `GITHUB_LIST_RELEASES` | `GET /repos/{owner}/{repo}/releases` | releases, changelogs |
| `GITHUB_LIST_WORKFLOW_RUNS_FOR_A_REPOSITORY` | `GET /repos/{owner}/{repo}/actions/runs` | CI / Actions status |
| `GITHUB_LIST_REPOSITORY_CONTRIBUTORS` | `GET /repos/{owner}/{repo}/contributors` | top contributors |
| `GITHUB_LIST_NOTIFICATIONS_FOR_THE_AUTHENTICATED_USER` | `GET /notifications` | the user's inbox |

Writes:

| Slug | GitHub endpoint | Destructive |
|---|---|---|
| `GITHUB_CREATE_AN_ISSUE` | `POST /repos/{owner}/{repo}/issues` | no |
| `GITHUB_UPDATE_AN_ISSUE` | `PATCH /repos/{owner}/{repo}/issues/{issue_number}` | yes (replaces fields) |
| `GITHUB_CREATE_AN_ISSUE_COMMENT` | `POST /repos/{owner}/{repo}/issues/{issue_number}/comments` | no |
| `GITHUB_CREATE_A_PULL_REQUEST` | `POST /repos/{owner}/{repo}/pulls` | no |
| `GITHUB_CREATE_A_REVIEW_FOR_A_PULL_REQUEST` | `POST /repos/{owner}/{repo}/pulls/{pull_number}/reviews` | no |
| `GITHUB_CREATE_A_REFERENCE` | `POST /repos/{owner}/{repo}/git/refs` | no |
| `GITHUB_CREATE_OR_UPDATE_FILE_CONTENTS` | `PUT /repos/{owner}/{repo}/contents/{path}` | yes (overwrites the file) |
| `GITHUB_STAR_A_REPOSITORY_FOR_THE_AUTHENTICATED_USER` | `PUT /user/starred/{owner}/{repo}` | no |

`pipe tools github` prints the catalog with every argument.

**Differences from Composio's versions** (argument names are unchanged):

- `GITHUB_GET_RAW_REPOSITORY_CONTENT` returns the bytes as a `ToolResult.files`
  envelope (`content_type` = MIME type, `metadata["filename"]`) instead of a
  staged `s3url`. `data` has `name`, `path`, `repository`, `size`, `mimetype`,
  and `content` (the text) for UTF-8 files up to 100 KB. The host decides how
  to ship the bytes. The BE's Content Vault import reads `s3url` or
  `file_content_bytes`, which pipe-as-service's `/fetch` provides.
- `GITHUB_GET_REPOSITORY_CONTENT` decodes UTF-8 file `content` to text
  (`encoding: "utf-8"`); binary files stay base64.
- `GITHUB_CREATE_OR_UPDATE_FILE_CONTENTS` treats `content` as text unless
  `encoding="base64"` is passed (Composio guesses). The current file `sha` is
  still looked up automatically.
- `owner`/`repo` slips (`repo="owner/name"`, a `.git` suffix) are forgiven;
  `.`/`..` path segments are refused.

### Connecting (GitHub OAuth App)

`GitHubToolkit.oauth` is `GITHUB_OAUTH`:

| | |
|---|---|
| authorize | `https://github.com/login/oauth/authorize` |
| token | `https://github.com/login/oauth/access_token` |
| default scopes | `repo read:user read:org` |
| PKCE | on (`pkce=True`, S256; GitHub does not support `plain`). Build the authorize URL with `code_challenge`, and exchange with the matching `code_verifier`, both from core's `make_pkce_pair()`. Core refuses either step without them. The client secret is still sent, because GitHub requires it for OAuth Apps |
| expiry | none: OAuth App tokens do not expire and carry no refresh token (`token_expires=False`) |
| disconnect | `GitHubToolkit.revoke_token` → `DELETE /applications/{client_id}/grant` (HTTP Basic `client_id:client_secret`), which removes the user's whole authorization of the app |

Run the flow with the toolkit's `build_authorize_url` / `exchange_code` /
`revoke_token`. Core handles GitHub's form-encoded token response and its
`error`-in-a-200 replies. Keep each flow's `code_verifier` with its `state` on
the server side. The verifier binds the code to the flow that requested it, so
a code intercepted or replayed under another pending `state` fails at GitHub
with `invalid_grant`.

```python
from aisquare.pipe import make_pkce_pair

verifier, challenge = make_pkce_pair()            # store verifier with state
url = github.build_authorize_url(client_id=..., redirect_uri=..., state=state, code_challenge=challenge)
# ... callback with ?code=&state= ...
token = github.exchange_code(client_id=..., client_secret=..., code=code,
                             redirect_uri=..., code_verifier=verifier)
```

Setting up the app (one per environment):

1. Open GitHub → *Settings → Developer settings → OAuth Apps → New OAuth App*.
   Use an organization's settings so the app is not tied to one person.
2. *Homepage URL*: the product URL. *Authorization callback URL*: the
   connection callback of the service running the flow, e.g. pipe-as-service's
   `https://<host>/v1/connections/callback`. GitHub allows one callback per
   app, and `redirect_uri` must match it (or be a subpath of it), so use one
   app per environment.
3. Generate a client secret. Store the client id and secret in the hosting
   service's secret store, never in code or config files.
4. Scopes: `repo` covers private repos, issues, PRs, contents, Actions runs and
   notifications. `read:user` covers the profile, and `read:org` covers org
   membership. Ask for fewer by passing `scopes=` to `build_authorize_url`; tools
   whose scope is missing answer 403/404.
5. Organizations with *OAuth app access restrictions* must approve the app
   (*Org settings → Third-party access*) before their private repos show up.
   Until then those repos answer 404 and `GITHUB_LIST_ORGANIZATIONS_…` may omit
   the org.

## Source connector (`github-source`)

Shallow-clones one repo and emits a single *checkout-handle* envelope
(`application/x-aisquare-checkout+json`, `data = {"path", "head_sha"}`) for a
downstream converter/sink to process **in the same run** — the checkout tempdir
is removed when the pipeline finishes with the envelope.

Auth: a GitHub App installation token **or** a personal access token — either
rides an `http.extraHeader` Basic header injected via `GIT_CONFIG_*` env vars
(never argv, never the remote URL). Omit `token` for public repos.

### Configuration

```python
config = {
    "full_name": "owner/name",          # required
    "token": "ghp_… / installation token",  # optional (public repos)
    "default_branch": "main",
    "skip_if_sha": "<sha>",             # yield nothing if remote HEAD matches (ls-remote, no clone)
    "fetch_repo_metadata": False,        # best-effort description/language via api.github.com
    "clone_timeout_seconds": 300,
    "keep_checkout": False,
    "checkout_dir": "/path",            # pin the checkout location (implies keep)
}
```

### Usage (Python API — pairs with aisquare-pipe-graphify)

```python
from aisquare.pipe import Pipeline
from aisquare_pipe_github import GitHubRepoSource

result = Pipeline(source=GitHubRepoSource(), sink=my_sink).run(
    {"github": {"full_name": "acme/api", "token": "ghp_..."}}
)
```

Note the two-name rule: CLI/registry name is the entry point `github-source`;
the config-dict key is the class attr `github`.

## Running tests

```bash
pytest -v                       # hermetic: HTTP mocked at the client boundary
ruff check . && mypy src

# live, read-only, opt-in (self-skips without GITHUB_TOKEN)
GITHUB_TOKEN=... pytest -m live tests/test_live.py
GITHUB_TOKEN=... GITHUB_LIVE_REPO=github/gitignore pytest -m live tests/test_live.py
```

From the repo root, `pipe validate github` runs contract, hygiene and the
unit suite; add `--live` for the live tests.
