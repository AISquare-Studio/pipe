# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.2.0] - 2026-10-15

### Added

- **Toolkits** (`aisquare.pipe.core.tools`): a first-class home for a
  connector's LLM-callable tools. `ToolSpec` (slug, description, JSON-Schema
  `input_schema`, `read_only`/`destructive`, tags; `to_llm_tool()`),
  `ToolResult` (`successful`/`data`/`error`, a machine-readable
  `error_code` and `retry_after`, plus binary `files` as `DataEnvelope`s),
  and the `Toolkit` base class with `tools()`, `get_tool()`,
  `search_tools()` (deterministic keyword ranking), `call_tool()` (never
  raises, even on malformed toolkit results; validates arguments against
  the schema; masks secret config values out of errors) and
  `validate_config()`.
  Helpers `rank_tools()`, `validate_arguments()` and
  `error_code_for_status()`.
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
  `aisquare-pipe>=0.2.0`.
- `aisquare-pipe-graphify`'s `[engine]` extra pins `graphifyy==0.9.72`
  (was 0.8.36).
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
- `aisquare-pipe-graphify`: without the `[engine]` extra, the graph modules
  raise `ModuleNotFoundError` and their unit tests skip explicitly. Under
  pytest 9 they errored at collection, so a bare `pipe validate` failed.

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

[Unreleased]: https://github.com/AISquare-Studio/pipe/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/AISquare-Studio/pipe/compare/v0.1.1...v0.2.0
[0.1.1]: https://github.com/AISquare-Studio/pipe/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/AISquare-Studio/pipe/releases/tag/v0.1.0
