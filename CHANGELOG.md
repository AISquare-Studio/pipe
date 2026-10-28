# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **Slack** (`aisquare-pipe-slack` 0.1.0, first release): `SlackToolkit`
  (`slack`, 51 tools — messages, history and threads, search, channels and
  DMs, reactions, pins, people and presence, workspace) and `SLACK_OAUTH`.
  - **One toolkit for both identities.** `config["identity"]` is `"user"`
    (act as the person who connected) or `"bot"` (post as the app's bot).
    By default one consent mints both tokens
    (`build_authorize_url(identity="both")`, `exchange_installation()`).
  - **Risk tiers.** Every tool has a tier: read, send, write or destructive.
    The defaults are read + send for the user and send only for the bot,
    because a shared bot token would expose other people's conversations.
    Anything more needs `config["enabled_tiers"]` / `config["enabled_tools"]`.
  - **Listing.** `available_tools(config)` and `search_tools(..., config=...)`
    list only what a connection may call; with no config, that is the default
    exposure. An invalid config fails as `forbidden`.
  - **Attribution.** Messages sent as the user carry a visible "Sent by <name>"
    line (`config["attribution_name"]`, default `pipe`; opt-in for the bot).
    The line is dropped only for a token that is positively a bot token.
  - **Scopes.** Named scope groups (by area and by tier) select what a consent
    asks for; the default is every scope.
  - **Slack Lists and remote files** (16 tools): Lists CRUD, items and access
    (`slackLists.*`), and remote files (`files.remote.*`; add / update / remove
    are bot-only, as Slack requires). Reads are `read`, the rest opt-in
    `write` / `destructive`.
  - **`SlackMessageSink`** (`slack-message-sink`): posts envelopes as messages,
    ephemeral messages or v2 file uploads, with the toolkit's identity,
    attribution and send gate. `PushResult.ref` is the `ts`, `message_ts` or
    file ID.
  - **Events API helpers** (`aisquare_pipe_slack.events`): v0 signature check
    and replay window in one call (`verify_request` / `parse_request`), typed
    parsing of `url_verification`, `app_mention`, `message.im`,
    `reaction_added` / `reaction_removed`, `tokens_revoked`, `app_uninstalled`,
    and `event_to_envelope` with Composio's trigger names. The endpoint and
    the Signing Secret are the host's.
  - Joins `[full]`.
- **Slack breadth tools**: 44 more `SlackToolkit` tools (95 in all) for files,
  canvases, bookmarks, Do Not Disturb, reminders and user groups, plus `/me`
  messages, unread-from-user, the read cursor, team profile, profile photo,
  bot info and auth teams.
  - **Tiers.** They use the same tiers, identity gate and attribution as the
    core tools. Most are opt-in (write or destructive). Making a file public,
    editing a canvas, disabling a user group and replacing its members are
    destructive.
  - **Files** travel as bytes. Downloads return a `DataEnvelope` in
    `ToolResult.files`, and the token is sent only to `files.slack.com`.
    Uploads use Slack's v2 flow (`aisquare_pipe_slack.files.upload_file`).
  - **Scopes.** User scopes go from 29 to 41, and bot scopes from 28 to 37.

- `Toolkit.available_tools(config=None)`: the tools a connection with that
  config may call, so hosts can list only those to a model. It returns
  every tool (`tools()`) unless a toolkit overrides it.
  `search_tools(query, limit, *, config=None)` now ranks
  `available_tools(config)`. Existing calls without `config` behave as before.
  `get_tool` and `call_tool` are unchanged, and `call_tool` still enforces
  on its own. A toolkit that overrides `search_tools` must accept
  `*, config=None`. An `available_tools` override returns a subset of
  `tools()` and never raises; for an unreadable config it returns `[]`. The
  new contract check `contract.toolkit.available-tools` (run by
  `pipe validate` and the toolkit compliance suite) verifies this for
  `None` and `{}`.

## [0.3.0] - 2026-10-15

### Added

- **Toolkits** (`aisquare.pipe.core.tools`): a first-class home for a
  connector's LLM-callable tools. `ToolSpec` (slug, description, JSON-Schema
  `input_schema`, `read_only`/`destructive`, tags; `to_llm_tool()`),
  `ToolResult` (`successful`/`data`/`error`, a machine-readable
  `error_code` and `retry_after`, plus binary `files` as `DataEnvelope`s),
  and the `Toolkit` base class with `tools()`, `get_tool()`,
  `search_tools()` (deterministic keyword ranking), `call_tool()` (never
  raises, even on malformed toolkit results; validates arguments against
  the schema; masks secret config values out of errors; maps an
  `urllib.error.HTTPError` from `run_tool` through
  `error_code_for_status()`, with a 429's `Retry-After` as `retry_after`,
  capped at 3600 s — the message names only the status, never the
  provider's URL, reason or body) and
  `validate_config()`.
  Helpers `rank_tools()`, `validate_arguments()`,
  `error_code_for_status()` and `time_budget()` (the host's per-call
  `config["timeout_seconds"]`, shared by the native toolkits).
- Canonical tool failure kinds as `ERROR_*` constants (`ERROR_CODES`):
  `unauthorized`, `forbidden`, `not_found`, `rate_limited`,
  `invalid_arguments`, `unknown_tool`, `conflict`, `upstream_error`,
  `timeout`.
- **OAuth 2.0** (`aisquare.pipe.core.oauth`), stdlib-only: `OAuthSpec`,
  `OAuthToken`, `make_pkce_pair()`, `pkce_challenge()`,
  `build_authorize_url()`, `exchange_code()`, `refresh_access_token()`
  (keeps or rotates the refresh token; keeps the previous scope when the
  provider omits it) and best-effort `revoke_token()`. JSON and
  form-encoded token responses are both understood. Endpoints must be
  https (loopback http allowed for tests), redirects are never followed,
  `state` is required, and spec extras cannot override flow parameters.
  Toolkits expose the same steps as overridable methods.
- Errors: `OAuthError` (carries the provider error code, never secrets),
  `ToolError(message, code=..., retry_after=...)`, `ToolkitNotFoundError`.
- Registry: `discover_toolkits()` / `get_toolkit()` over the new
  `aisquare_pipe.toolkits` entry-point group.
- Testing: `toolkit_compliance_suite()`, toolkit contract checks in
  `pipe validate` (entry-point name must equal the toolkit name; packages
  registering a toolkit must call `toolkit_compliance_suite()` in
  `tests/test_compliance.py`), and `MockToolkit` registered as toolkit
  `mock` for hermetic downstream tests (with `MOCK_EXPIRED_TOKEN` /
  `MOCK_RATE_LIMITED_TOKEN` failure triggers).
- CLI: `pipe tools [<toolkit>] [--search Q] [--limit N] [--json]`.
- **Native connector toolkits**, built on the core above, so hosts can serve
  these services without Composio. Tool slugs and argument names follow
  Composio's; each toolkit registers under `aisquare_pipe.toolkits` and
  ships an `OAuthSpec`:
  - **GitHub** (`aisquare-pipe-github` 0.2.0): `GitHubToolkit` (`github`,
    31 tools: 23 read, 8 write) and `GITHUB_OAUTH` (OAuth App with PKCE;
    tokens do not expire; `revoke_token` revokes only this connection's
    token).
  - **OneDrive** (`aisquare-pipe-onedrive` 0.2.0): `OneDriveToolkit`
    (`onedrive`, 16 tools) and `MICROSOFT_OAUTH` / `microsoft_oauth(tenant)`
    (PKCE, rotating refresh tokens). `revoke_token` sends nothing, because
    Microsoft has no per-app revoke endpoint.
  - **Google Drive**: new package `aisquare-pipe-googledrive` 0.2.0 with
    `googledrive-source` (folder pulls with filters; Google-native files are
    exported), `googledrive-sink` (uploads with name-conflict policies),
    `GoogleDriveToolkit` (`googledrive`, 23 tools) and `GOOGLEDRIVE_OAUTH`
    (PKCE, offline access).
- Install extras: `aisquare-pipe-googledrive` joins `[popular]` (and so
  `[full]`); `aisquare-pipe-github` joins `[full]`.

### Changed

- `pipe validate` legacy file mode also validates `Toolkit` subclasses.
- Root `pyproject.toml` configures mypy for the `src/` namespace layout, so
  `mypy src` works with an editable install.
- The GitHub, OneDrive and Google Drive packages require
  `aisquare-pipe>=0.3.0`.
- `aisquare-pipe-graphify` 0.2.1: the `[engine]` extra pins
  `graphifyy==0.9.72` (was 0.8.36).
- CI uses `actions/setup-python@v7`.

### Fixed

- Type annotations in `merge_batch` / `merge_concat` (mypy errors; no
  behavior change).
- OneDrive: `OneDriveClient.list_folder(recursive=True)` collected
  subfolders from the first page of a listing only. It now collects them
  from every page and paginates each subfolder.
- `pipe validate` hygiene now converts `file://` URLs from editable-install
  metadata into real paths. On Windows, every editable connector was
  reported as a broken install. On every platform, a path containing a
  percent-escape (such as `%20` for a space) never matched.
- `aisquare-pipe-graphify` 0.2.1: without the `[engine]` extra, the graph
  modules raise `ModuleNotFoundError` and their unit tests skip explicitly.
  Under pytest 9 they errored at collection, so a bare `pipe validate`
  failed.

## [0.2.0] - 2026-07-10

A connector-only release: the framework (`aisquare-pipe`) stayed at 0.1.1.

### Added

- `aisquare-pipe-github` 0.1.0 (first publish): the `GitHubRepoSource`
  connector.
- `aisquare-pipe-graphify` 0.2.0 (first publish): `GraphifyConverter`, plus
  an `[engine]` extra with in-process graph query verbs. Covers the
  enriched-tier fix, the query face, `GraphMerger` and incremental state.
- The publish matrix and `RELEASING.md` carry both packages.

## [0.1.1] - 2026-06-29

### Fixed

- The `full` install extra now includes `aisquare-pipe-composio`, so
  `pip install "aisquare-pipe[full]"` provides the Composio connector. The
  published 0.1.0 metadata omitted it.

## [0.1.0] - 2026-06-19

Initial public release.

### Added

- Core framework: the `DataEnvelope` spec, `SourceConnector` / `SinkConnector`
  abstractions, `Pipeline` orchestration, type matching with converters, and
  entry-point–based plugin discovery (`aisquare_pipe.connectors`).
- `pipe` CLI: `list`, `describe`, `check`, `run`, `new-connector`, and
  `validate` (contract + hygiene + unit suites, no credentials required).
- Compliance test suite (`connector_compliance_suite`) and packaging-hygiene
  checks for connector plugins.
- MCP server integration (`aisquare.pipe.mcp`).
- Connectors: Local filesystem, Dropbox, OneDrive, Salesforce, DocuSign,
  Composio, n8n, and the AISquare gateway.
- Install extras: `[popular]`, `[full]`, and per-connector packages
  (`aisquare-pipe-<service>`).

[Unreleased]: https://github.com/AISquare-Studio/pipe/compare/v0.3.0...HEAD
[0.3.0]: https://github.com/AISquare-Studio/pipe/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/AISquare-Studio/pipe/compare/v0.1.1...v0.2.0
[0.1.1]: https://github.com/AISquare-Studio/pipe/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/AISquare-Studio/pipe/releases/tag/v0.1.0
