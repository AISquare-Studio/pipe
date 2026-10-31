# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **Router metadata on every native toolkit** — github, googledrive,
  onedrive, slack, gmail and googlecalendar — so `catalogue()` returns six
  cards and scope filtering works on all of them.
  - **Cards.** Each sets `category` (`code`, `files`, `files`, `chat`,
    `email`, `calendar`), `keywords` and 3-4 `examples` phrased as users ask.
  - **Scopes.** Every tool carries `required_scopes`, and each toolkit's
    `scope_granted` counts its provider's broader scopes:
    - GitHub: `repo`, except `read:user`, `read:org` and `notifications`
      for the profile, orgs and notifications. `repo` covers
      `public_repo`/`notifications`, `user` covers `read:user`, and
      `admin:org`/`write:org` cover `read:org`.
    - Google Drive: `drive.readonly` to read and `drive` to write; `drive`
      covers the rest.
    - OneDrive: `Files.Read.All` / `Files.ReadWrite.All`. Graph names
      compare short and case-insensitively.
    - Slack: its per-identity scope map, promoted. `required_scopes` holds
      the user scopes (the bot ones for bot-only tools), and a bot
      connection is checked against the bot scopes. Behaviour without
      `granted_scopes` is unchanged.
  - **Risk** follows each tool's flags (Slack: read → `read`, send and write
    → `write`, destructive and edits → `destructive`).
  - **GitHub grant revoke.** `GitHubToolkit.revoke_grant()` (`DELETE
    /applications/{client_id}/grant`) removes the app from the user's GitHub
    and ends every token they gave it. `revoke_token` still revokes one
    connection's token.
  - **Google Drive OAuth.** `GOOGLEDRIVE_OAUTH` no longer sends
    `include_granted_scopes=true`, like Gmail's and Calendar's specs. One
    Google OAuth client can serve all three without a Drive token carrying
    their scopes. Revoke stays grant-wide.
  - **Versions.** `aisquare-pipe-github`, `-googledrive` and `-onedrive` go to
    0.3.0. Slack, Gmail and Google Calendar stay at their unreleased 0.1.0.
    All six need `aisquare-pipe>=0.4.0`.
- **Router metadata in core** (`aisquare-pipe` 0.4.0), for a host that
  picks connectors from short cards and then tools from the chosen ones. No
  LLM or model dependency.
  - **Connector cards.** `Toolkit` gains `category` (one of `CATEGORIES`:
    code, files, email, calendar, chat, docs, project, crm, data, other;
    default `other`), `keywords` and `examples` (2–4 short user asks), all
    defaulted so existing toolkits are unchanged. The `Toolkit.card()`
    classmethod returns `name`, `display_name`, `description`, `category`,
    `keywords`, `examples` and `auth_type` from class attributes alone.
  - **Per-tool scopes and risk.** `ToolSpec` gains `required_scopes` (all
    needed; default none) and `risk` (`read` / `write` / `destructive`,
    derived from `read_only` / `destructive` unless set, and re-derived by
    `dataclasses.replace` when it was not set).
    `ToolSpec.to_dict(metadata=True)` adds both; the default shape is
    unchanged.
  - **Scope-aware listing.** When `config["granted_scopes"]` is present (pass
    the stored token's `OAuthToken.granted_scopes`; a list or a space/comma
    string works too), the default `available_tools(config)` leaves out
    tools whose `required_scopes` it doesn't grant, and `call_tool` refuses
    them as `insufficient_scope` before any request. None or a blank string
    means "not known" and filters nothing. A toolkit with broader scopes that imply narrower
    ones overrides `scope_granted()`. Without the key nothing changes.
  - **Cross-toolkit catalogue.** `catalogue()` returns one card per
    installed toolkit, discovered once per process (`refresh=True` to
    re-read; `installed_toolkits()` for the classes). The first discovery
    imports the plugin modules, and their import-time side effects are the
    plugins' own (urllib3 probes for IPv6), so a host that must stay offline
    discovers once at startup. The
    `search_tools_across(query, connectors)` function ranks tools across
    several toolkits and returns `QualifiedTool(connector, tool)`; the pure
    ranker is `rank_across()`. Discovery raises the new
    `ToolkitConflictError` when two installed toolkits' slug prefixes
    collide (equal, or one starting the other); see
    `slug_prefix_conflicts()`.
  - **Error codes.** `not_connected` (no usable credentials; nothing was
    sent) and `insufficient_scope` (the token lacks a scope the call needs)
    join `ERROR_CODES` and `ToolError`.
    `error_code_for_status(status, www_authenticate=...)` maps a 403 with an
    RFC 6750 `insufficient_scope` challenge to `insufficient_scope`.
    `call_tool` does the same for an `HTTPError` raised from `run_tool`.
  - **OAuth identity and grants.** `OAuthToken.granted_scopes` parses the
    granted `scope` (space- or comma-separated; None when the provider
    didn't say). `OAuthToken.account` is an `OAuthAccount` (`id`, `email`,
    `name`, `tenant`), filled when the token response carries an OpenID
    Connect `id_token` issued to this client. The claims are read but not
    verified, so use them as a label. `to_dict()` adds `account` only when
    it is known.
  - **Compliance.** New toolkit contract checks: `contract.toolkit.card`,
    `contract.toolkit.category`, `contract.toolkit.card-text`,
    `contract.toolkit.tool-metadata` (scope strings, risk consistent with the
    flags), `contract.toolkit.scopes-requested` (a warning) and, in
    `pipe validate`, `contract.toolkit.slug-prefix-unique`.
  - **CLI.** `pipe tools --json` rows add the card fields, and
    `pipe tools <toolkit> --json` specs add `required_scopes` and `risk`.
    Existing keys are unchanged.
- **Gmail** (`aisquare-pipe-gmail` 0.1.0, first release): `GmailToolkit`
  (`gmail`, 13 tools: 7 read, 6 write, 1 destructive) and `GMAIL_OAUTH`.
  Slugs and argument names follow Composio's Gmail toolkit.
  - **Tools.** Search and read messages and threads, attachments (returned
    as a file envelope, like Google Drive downloads), labels and the profile;
    drafts, sending and threaded replies (`In-Reply-To` / `References`, the
    last sender by default); label changes (mark read, archive, star) and
    trash.
  - **MIME.** Messages are built with the standard library's `email`
    package (RFC 2047 headers, an HTML body with a text alternative,
    attachments, header-injection and address checks); large messages go as
    one multipart upload. Read messages come back with `messageText` (plain
    text preferred, HTML converted, charsets honoured) and `attachmentList`.
  - **Least privilege.** Read-only consent by default (`gmail.readonly`);
    each write tool names the scopes it needs (`gmail.compose`, `gmail.send`,
    `gmail.modify`; plus `gmail.readonly` for the tools that read the thread
    they answer) in `ToolSpec.required_scopes` and its description.
    `scope_granted` counts Google's broader scopes (`https://mail.google.com/`,
    `gmail.modify` over read/compose/send, `gmail.compose` over send).
- **Google Calendar** (`aisquare-pipe-googlecalendar` 0.1.0, first
  release): `GoogleCalendarToolkit` (`googlecalendar`, 14 tools: 10 read,
  4 write, 1 destructive) and `GOOGLECALENDAR_OAUTH`. Slugs and argument
  names follow Composio's Google Calendar toolkit.
  - **Tools.** Calendars, events (by range and text, expanded or not),
    recurring-event instances, events across every calendar, free/busy and
    free slots, the current time in a zone; create, update (a patch that
    keeps unmentioned fields and converts all-day <-> timed), quick-add and
    delete events.
  - **Time zones.** Times without an offset are read in the given zone or
    the calendar's own; query bounds get the zone's offset for that date
    (daylight saving included); event times stay wall-clock with an IANA
    zone so recurring events expand correctly.
  - **Least privilege.** Read-only consent by default (`calendar.readonly`);
    writes need `calendar.events`, and create and update also
    `calendar.readonly` (they read the calendar's time zone and the event)
    (`ToolSpec.required_scopes`; the full `calendar` scope covers both).
- Both Google toolkits need `aisquare-pipe>=0.4.0`. They tell a 403 for a
  missing OAuth scope apart from other 403s and return core's
  `insufficient_scope` (with `data.insufficient_scope` and
  `data.required_scopes`). Their OAuth specs omit `include_granted_scopes`,
  so one Google OAuth client can serve Drive, Gmail and Calendar without one
  connector's token carrying another's scopes.
- **Slack** (`aisquare-pipe-slack` 0.1.0, first release): `SlackToolkit`
  (`slack`, 111 tools: 47 read, 6 send, 38 write, 20 destructive),
  `SLACK_OAUTH`, `SlackMessageSink` (`slack-message-sink`) and Events API
  helpers. Slugs and argument names follow Composio's Slack toolkit.
  - **Areas.** Messages, history and threads, search, channels and DMs,
    reactions, pins, people and presence, files, canvases, bookmarks, Do Not
    Disturb, reminders, user groups, Slack Lists, remote files and workspace
    info.
  - **Identity.** One toolkit serves both identities. `config["identity"]` is
    `"user"` (act as the person who connected) or `"bot"` (post as the app's
    bot user). A bot token (`xoxb-…`) with no identity set acts as the bot; with
    `"user"`, it is a config error. By default one consent mints both tokens
    (`build_authorize_url(identity="both")`, `exchange_installation()`).
  - **Risk tiers.** Every tool is read, send, write or destructive.
    - The defaults are read + send for the user (53 tools) and send only for
      the bot (6 tools), because a shared bot token would expose other
      people's conversations.
    - Anything more needs `config["enabled_tiers"]` or
      `config["enabled_tools"]`.
    - 17 tools are user-only and 4 bot-only. The gate refuses a tool before
      any request.
  - **Listing.** `available_tools(config)` and `search_tools(..., config=...)`
    list only what a connection may call; with no config, that is the default
    exposure. An invalid config lists no tools, and calls fail as
    `forbidden`.
  - **Attribution.** Messages sent as the user carry a visible "Sent by <name>"
    line (`config["attribution_name"]`, default `pipe`). It covers posts,
    replies, DMs, ephemeral, scheduled and `/me` messages, edits, file shares
    and sink pushes. It is opt-in for the bot.
  - **Scopes.** 44 user and 43 bot scopes, derived from the tools, including
    the events-only `app_mentions:read`. The default consent asks for all of
    them. Named scope groups (by area, by tier, and `events`) select a subset.
  - **Files.** Downloads return a `DataEnvelope` in `ToolResult.files`. The
    token goes only to `files.slack.com` (the host is checked the way
    requests resolves it), with a 100 MiB cap. Uploads use Slack's v2 flow
    (`aisquare_pipe_slack.files.upload_file`).
  - **Errors.** Slack's `ok: false` codes map to the canonical `error_code`s,
    with hints. Retry-After is honoured within the host's time budget, and
    server errors are retried only for reads.
  - **`SlackMessageSink`.** It posts envelopes as messages, ephemeral messages
    or v2 file uploads, with the toolkit's identity, attribution and send
    gate. `PushResult.ref` is the `ts`, `message_ts` or file ID.
  - **Events API helpers** (`aisquare_pipe_slack.events`).
    - `verify_request` / `parse_request` check the v0 signature and the
      replay window in one call.
    - Typed parsing of `url_verification`, `app_mention`, `message.im`,
      `reaction_added` / `reaction_removed`, `tokens_revoked` and
      `app_uninstalled`.
    - `event_to_envelope` maps events to Composio's trigger names. The app's
      own posts get no trigger.
  - **Packaging.** It requires `aisquare-pipe>=0.3.0`, `requests` and
    `urllib3`. It ships an app manifest template and joins `[full]`.

### Changed

- `Toolkit.call_tool` now reports a config with no usable credentials
  (`validate_config` false) as `not_connected` instead of `unauthorized`.
  `unauthorized` now means only that the provider rejected the credentials.
  Hosts that branched on `unauthorized` to prompt a first connection should
  branch on `not_connected` as well.

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
