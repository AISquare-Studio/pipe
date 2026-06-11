# AGENTS.md

## Purpose
This repository contains `aisquare.pipe`, a Python 3.11+ connector framework for moving `DataEnvelope` objects between Source and Sink connectors. The root package provides the shared framework, CLI, test helpers, and example/mock connectors used by plugin packages.

## Repository Layout
- `src/aisquare/pipe/core/`: core abstractions and pipeline logic (`connector.py`, `pipeline.py`, `envelope.py`, `merge.py`, `registry.py`, `types.py`)
- `src/aisquare/pipe/cli/`: Click-based CLI entrypoint and commands
- `src/aisquare/pipe/mcp/`: MCP server integration
- `src/aisquare/pipe/testing/`: compliance helpers, fixtures, and mock connectors used by tests
- `tests/`: framework-level test suite
- `connectors/<name>/`: standalone connector plugin packages with their own `pyproject.toml`, source package, README, and tests
- `examples/`: small runnable examples

## Development Environment
- Python: `>=3.11`
- Always work inside the repository virtual environment before running project commands:
  ```bash
  source .venv/bin/activate
  ```
- Install editable package with dev tools:
  ```bash
  pip install -e ".[dev]"
  ```
- Plugin discovery depends on editable/install-time entry points. If CLI discovery behaves unexpectedly, confirm the package has been installed with `pip install -e .`.

## Common Commands
- Validate everything — every connector + the framework, no credentials needed:
  ```bash
  pipe validate                # contract + hygiene + every unit suite
  pipe validate --skip-tests   # contract + hygiene only (seconds)
  pipe validate composio       # one connector
  pipe validate --install      # also pip install -e any missing/broken connector dirs
  ```
  Exit 0 = clean (warnings allowed), 1 = failures. CI runs exactly this command.
- Run framework tests:
  ```bash
  pytest
  ```
- Run a focused test file:
  ```bash
  pytest tests/test_pipeline.py -v
  ```
- Run connector package tests (one connector per pytest invocation — their
  `tests` packages share a name and cannot run in one process):
  ```bash
  cd connectors/local && pytest tests -v
  ```
- Lint:
  ```bash
  ruff check .
  ```
- Type-check:
  ```bash
  mypy src
  ```
- Inspect CLI:
  ```bash
  pipe --help
  pipe list
  ```

## Working Conventions
- Keep framework changes in `src/aisquare/pipe/` and connector-specific changes inside the relevant `connectors/<name>/` package.
- Preserve the public abstractions around `SourceConnector`, `SinkConnector`, `DataEnvelope`, and `Pipeline` unless the task explicitly requires an API change.
- When adding connector behavior, update or add compliance-oriented tests alongside unit tests.
- Prefer small, typed changes that follow existing patterns in the core package and connector packages.
- Do not remove or rename entry points in `pyproject.toml` files unless the task explicitly requires it.
- Each connector under `connectors/<name>/` is an independently published pip package. Its source MUST stay inside its own directory; framework code is not modified from a connector change.
- If a connector needs additional folders (e.g., `migrations/`, `schemas/`, `fixtures/`), those go **inside** `connectors/<name>/` — never at the repo root.
- **Credentials are never required by default.** All validation layers (contract, hygiene, unit suites) are hermetic — connector tests mock at the client boundary. Live tests are opt-in via `pipe validate --live` and must self-skip when their env vars are absent.

## Connector Tiers
All connectors live in the same `connectors/<name>/` directory; the tier signal is encoded in `pyproject.toml` extras bundles, not in folder location.

- **`[popular]`** — Super-popular, broadly useful connectors most users want. Currently: `local`, `dropbox`, `onedrive`. Installed via `pip install "aisquare-pipe[popular]"`.
- **`[full]`** — Everything in `[popular]` plus the long-tail / enterprise / client-specific connectors. Currently adds `salesforce`, `docusign`. Installed via `pip install "aisquare-pipe[full]"`.
- **Direct install** — `pip install aisquare-pipe-<name>` always works for any connector regardless of tier.

**Where does a new connector belong?**
- Super-popular and broadly useful (a typical AISquare user is likely to need it) → add to `[popular]` in `pyproject.toml`.
- Niche / enterprise / client-specific / experimental → leave out of `[popular]` (it still ships via `[full]` and direct install).
- When in doubt, leave it out of `[popular]`. Promoting later is cheap; demoting feels worse to users.

The repo is sized to scale to ~50 connectors in this layout. If the long-tail grows past that, the `[popular]` set may need to spin out into its own repo — but that's a future problem, not a current one.

## Testing Expectations
- Run `pipe validate` before pushing any connector change — it is the single gate (contract, hygiene, unit suites) and what CI runs.
- Any change to pipeline logic, type matching, envelope handling, registry behavior, or merge behavior should include or update tests under `tests/`.
- Any connector package change should run that connector's test suite and keep compliance tests passing.
- If a change affects CLI output or connector discovery, verify with the CLI where practical.

## Notes For Agents
- Always activate the local virtual environment first with `source .venv/bin/activate` before running Python, pytest, mypy, ruff, or `pipe` commands.
- Search with `rg` first; the codebase is small and consistent.
- Read large files in chunks.
- Check for existing tests before introducing new helpers or abstractions.
- The worktree may contain user changes; do not overwrite unrelated edits.
