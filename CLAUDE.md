<!-- verified-against: 3bb1d7f 2026-07-05 -->
# aisquare-pipe (the pipe framework)

> **Live context:** query the `aisquare-graph` MCP server (workspace-root, all repos merged) for where a symbol lives / what calls what, instead of recalling; cross-repo runtime contracts (Redis bus, shared secret, wire keys, version pins) are machine-checked in the workspace `seams.json`. `make context-check` verifies.


`aisquare-pipe` (import namespace `aisquare.pipe`, PyPI package `aisquare-pipe`) is the universal "anything-to-anything" connector **framework**: every service implements a `SourceConnector` (pull) and/or `SinkConnector` (push) against a single `DataEnvelope` spec, and `Pipeline` handles type matching, conversion, merging, and orchestration. This repo is a monorepo of the framework plus the first-party connector plugin packages under `connectors/`, each independently published to PyPI. Working conventions (venv-first, the `pipe validate` gate, one-pytest-process-per-connector, never rename entry points) live in `AGENTS.md`; read it first.

## How it fits the AISquare stack

- **Consumed as a library, not run as a service.** The service wrapper is the separate repo `aisquare-pipe-service` (REST + MCP on `:8090` locally via the workspace's `make go-full`); it depends on `aisquare-pipe` and imports `Pipeline`, `discover_connectors`, the `PipeError` hierarchy, `PipeMCPServer.generate_tools()`, and the composio factories. Do not modify pipe from that repo, and do not put service concerns in this one.
- In the AISquare workspace this clone lives **inside** the workspace at `./pipe` (gitignored there); the pipe-service README's `../pipe` sibling convention refers to this checkout. The workspace's docker-compose additionally bind-mounts this repo read-only into the BE's `api` and `celery` containers at `/opt/pipe` with `PYTHONPATH` entries for `src/`, `connectors/github/src`, and `connectors/graphify/src`, so BE code can import branch connectors without a PyPI release. Moving or deleting this checkout breaks those containers' imports.
- **Base branch is `main`** (like pipe-service; unlike the workspace-wide `develop` default).
- Note: Studio-BE's graphify enrichment does NOT go through pipe today. The BE pipx-installs the `graphifyy` engine directly in its Dockerfiles and shells out to the `graphify` CLI from `integrations_github.tasks`.

## Public API (`src/aisquare/pipe/__init__.py`, plus the MCP submodule)

- Envelope: `DataEnvelope` (content_type, data, source_id, metadata, schema, stream), `MetaField`, `PullParams`/`PushParams`, `PushResult`, `Resource`, `RateLimit`
- Connectors: `SourceConnector`, `SinkConnector`, `DuplexConnector`, `AuthType` (NONE/API_KEY/OAUTH2/CUSTOM)
- Orchestration: `Pipeline` (`run()` / `dry_run()`), `PipelineResult`, `CompatibilityReport`, `MergeStrategy` (ENRICH/BATCH/ZIP/CONCAT)
- Types: `TypeMatcher` (EXACT then WILDCARD then CONVERTER; `MatchLevel.AGENT` is declared but unimplemented), `TypeConverter`, `MatchResult`, `MatchLevel`
- Registry: `discover_connectors()` / `discover_converters()` / `get_connector()` / `get_converter()` over the entry-point groups **`aisquare_pipe.connectors`** and **`aisquare_pipe.converters`** (a connector only exists after its package is pip-installed, editable ok)
- Errors: `PipeError` and 6 subclasses (`ConnectorNotFoundError`, `ConfigValidationError`, `TypeMismatchError`, `EnvelopeValidationError`, `PipelineError`, `ConverterError`)
- MCP: `PipeMCPServer` (imported from `aisquare.pipe.mcp.server`, not the top-level package) is tool-spec generation only; `start()` raises NotImplementedError and `pipe serve-mcp` is a stub. The real MCP transport lives in aisquare-pipe-service.
- CLI: `pipe list | describe | check | run | validate | new-connector | serve-mcp`

Design invariants: credentials ride the per-connector `config` dict, never env vars (the framework reads no credential env; `COMPOSIO_API_KEY` is intentionally not read). Config is name-scoped with whole-dict fallback: `config.get(connector.name, config)`. Sink `metadata_spec` required keys warn, they do not fail. `SinkConnector.push` returns `PushResult(success=False)` on business failure and never raises. Converters run only on CONVERTER-level matches: a sink whose declared types already wildcard-match the source short-circuits the converter silently.

## Connectors on `main`

| Package | Entry points | Notes |
|---|---|---|
| `aisquare-pipe-local` | local-source / local-sink | filesystem |
| `aisquare-pipe-dropbox` | dropbox-source / dropbox-sink | |
| `aisquare-pipe-onedrive` | onedrive-source / onedrive-sink | |
| `aisquare-pipe-salesforce` | salesforce-source / salesforce-sink | |
| `aisquare-pipe-docusign` | docusign-source / docusign-sink | |
| `aisquare-pipe-composio` | composio-source / composio-sink / composio-triggers-source | meta-connector over Composio's whole catalog; `factory.py` mints toolkit-pinned subclasses (`composio_source("gmail")`); all SDK contact centralized in `ComposioClient`; added to the `[full]` extra in 0.1.1 |
| `aisquare-pipe-n8n` | n8n-source | streams n8n workflow executions as TraceBatch envelopes shaped for the Explainability gateway |
| `aisquare-pipe-gateway` | aisquare-gateway-sink | pushes `application/x-aisquare-trace+json` envelopes to the Explainability gateway ingest endpoint (requires `idempotency_key` metadata) |

Install tiers are encoded in pyproject extras: `[popular]` = local + dropbox + onedrive; `[full]` = popular + salesforce + docusign + composio. The pyproject extras are the source of truth; the README connector table and one AGENTS.md line trail them.

## The graphify/github connectors (branch only)

`connectors/graphify/` and `connectors/github/` on `main` are empty shells (cache residue only; `git ls-files` returns nothing). The real sources live on the un-merged branch **`feat/github-graphify-connectors`**: `aisquare-pipe-graphify` (GraphifySource + GraphifyConverter; wraps a `GraphifyEngine` that shells out to the `graphify` CLI; its `[engine]` extra pins the separate PyPI engine package `graphifyy`) and `aisquare-pipe-github` (GitHubRepoSource producing the checkout handle graphify consumes). The branch pre-dates `publish.yml`, so merging it must also extend the publish matrix. Do not document these as part of `main`.

## Local dev

- `source .venv/bin/activate` first (AGENTS.md mandate), `pip install -e ".[dev]"`, plus `pip install -e connectors/<name>` for each connector you need discovered.
- The single quality gate is what CI runs: **`pipe validate`** (4 layers: contract, hygiene, unit, opt-in `--live`). Variants: `--skip-tests` (seconds), `pipe validate composio` (one connector), `--install` (fix broken editable installs).
- Framework tests: `pytest` from the repo root. Connector tests: `cd connectors/<name> && pytest tests` and only one connector per pytest process (their identically named `tests` packages collide).
- Lint: `ruff check .`; types: `mypy src`.
- This repo is not part of `make go`; there is no Docker here.

## Releasing

Version bump goes in BOTH the connector class `version` attribute and its pyproject (hygiene-enforced; `pipe validate` flags drift), plus a CHANGELOG entry. Publishing is PyPI Trusted Publishing (OIDC) triggered by a GitHub Release; the matrix in `.github/workflows/publish.yml` lists the framework + each connector package and must be extended when a connector is added. `skip-existing: true` makes re-runs safe. See `RELEASING.md`. Downstream note: aisquare-pipe-service's Docker image installs `aisquare-pipe[full]` from PyPI, so its Composio routes depend on your release actually landing there (and on a `--no-cache` rebuild).
