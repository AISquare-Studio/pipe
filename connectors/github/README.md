# aisquare-pipe-github

GitHub **source connector** for [aisquare.pipe](../../README.md): shallow-clones one
repo and emits a single *checkout-handle* envelope
(`application/x-aisquare-checkout+json`, `data = {"path", "head_sha"}`) for a
downstream converter/sink to process **in the same run** — the checkout tempdir
is removed when the pipeline finishes with the envelope.

Auth: a GitHub App installation token **or** a personal access token — either
rides an `http.extraHeader` Basic header injected via `GIT_CONFIG_*` env vars
(never argv, never the remote URL). Omit `token` for public repos.

## Install

```bash
cd connectors/github
pip install -e ".[dev]"
```

## Configuration

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

## Usage (Python API — pairs with aisquare-pipe-graphify)

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
pytest -v
```
