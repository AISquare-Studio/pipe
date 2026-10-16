# Contributing: Building a Connector Plugin

This guide explains how to build a connector plugin for `aisquare.pipe` —
its data-moving connectors and, optionally, its agent-facing toolkit
(see [Building a Toolkit](#building-a-toolkit)).

## Quick Start

```bash
# Scaffold a new connector project
pipe new-connector my-service

# This creates:
# aisquare-pipe-my-service/
#   pyproject.toml
#   README.md
#   src/aisquare_pipe_my_service/connector.py
#   tests/test_compliance.py
```

## The SourceConnector Interface

A source connector pulls data from an external service and yields `DataEnvelope` objects.

```python
from collections.abc import Iterator
from aisquare.pipe import SourceConnector, AuthType, DataEnvelope, MetaField, PullParams

class MyServiceSource(SourceConnector):
    # Required attributes
    name = "my-service"               # unique identifier
    version = "0.1.0"                 # semver
    output_types = ["text/plain"]     # MIME types this source produces
    auth_type = AuthType.API_KEY

    # Optional attributes
    description = "Pulls data from My Service"
    docs_url = "https://docs.myservice.com"
    metadata_spec = {
        "filename": MetaField(type=str, required=False, description="Original filename"),
    }

    def pull(self, config: dict, params: PullParams | None = None) -> Iterator[DataEnvelope]:
        """Yield envelopes from the source. Must be a generator."""
        api_key = config["api_key"]
        # ... fetch data from service ...
        yield DataEnvelope(
            content_type="text/plain",
            data="fetched content",
            source_id=self.name,
            metadata={"filename": "doc.txt"},
        )

    def validate_config(self, config: dict) -> bool:
        """Return True if credentials/config are valid."""
        return "api_key" in config
```

## The SinkConnector Interface

A sink connector pushes `DataEnvelope` objects to an external service.

```python
from aisquare.pipe import SinkConnector, AuthType, DataEnvelope, PushResult, PushParams

class MyServiceSink(SinkConnector):
    name = "my-service"
    version = "0.1.0"
    input_types = ["text/plain", "application/json"]
    auth_type = AuthType.OAUTH2

    def push(self, envelope: DataEnvelope, config: dict, params: PushParams | None = None) -> PushResult:
        """Push an envelope to the service. Return a PushResult."""
        try:
            # ... upload data to service ...
            return PushResult(success=True, ref="item-123")
        except Exception as e:
            return PushResult(success=False, error=str(e))

    def validate_config(self, config: dict) -> bool:
        return "access_token" in config
```

## Required vs Optional

### Required (abstract — must implement):
- `name`, `version`, `auth_type` — class attributes
- `output_types` (source) or `input_types` (sink)
- `pull()` (source) or `push()` (sink)
- `validate_config()`

### Optional (defaults provided):
- `description`, `docs_url` — documentation strings
- `metadata_spec` — declares what metadata keys the connector produces/consumes
- `list_resources()` — browse available items (source only)
- `supports_streaming()` — return `True` if the source can stream large files
- `rate_limit()` — return a `RateLimit` object
- `accepts()` — fine-grained acceptance check (sink only)
- `max_size()` — maximum envelope size in bytes (sink only)

## How metadata_spec Works

`metadata_spec` is a dict mapping metadata key names to `MetaField` descriptors. It serves two purposes:

1. **For sources**: declares what metadata keys the source produces on each envelope
2. **For sinks**: declares what metadata keys the sink reads/uses from envelopes

```python
from aisquare.pipe import MetaField

metadata_spec = {
    "filename": MetaField(type=str, required=True, description="Original filename"),
    "caption": MetaField(type=str, required=False, description="Image caption", max_length=500),
    "tags": MetaField(type=list, required=False, description="Tag list", default=[]),
}
```

The pipeline uses this to warn when a sink requires metadata that the source doesn't produce.

## Entry Points Registration

Register your connector in `pyproject.toml` so the framework auto-discovers it:

```toml
[project.entry-points."aisquare_pipe.connectors"]
my-service-source = "aisquare_pipe_my_service.connector:MyServiceSource"
my-service-sink = "aisquare_pipe_my_service.connector:MyServiceSink"
```

After `pip install -e .`, running `pipe list` should show your connector.

## Validating Your Connector

One command validates everything — no credentials required:

```bash
pipe validate                # all connectors + framework
pipe validate my-service     # just yours
pipe validate --skip-tests   # contract + hygiene only (seconds)
```

It runs four layers per connector:

| Layer | What it checks | Credentials |
|---|---|---|
| **Contract** | spec attributes, plus behavior with empty config under a socket guard: `validate_config({})` returns `False` without network, `pull({})` fails cleanly (`ConfigValidationError`/`ValueError`, never `KeyError`/`TypeError`), `push(garbage, {})` returns `PushResult(success=False)` without raising | none |
| **Hygiene** | packaging rules (package name, `aisquare-pipe>=` dep, entry points resolve, class version == pyproject version), scaffold completeness, no cross-connector imports, no bare `except:` | none |
| **Unit suite** | your `tests/` directory, run in its own pytest process | none — mock your service at the client boundary |
| **Live** | optional `tests/test_live.py` against the real API | opt-in (`--live` + env vars) |

Exit 0 = clean (warnings allowed) · 1 = failures · 2 = usage error. Check ids in failure output (`contract.source.pull-no-creds`, `hygiene.version-sync`, ...) name the exact rule.

The compliance suite is the per-connector entry to the contract layer:

```python
# tests/test_compliance.py
from aisquare.pipe.testing.compliance import connector_compliance_suite
from aisquare_pipe_my_service.connector import MyServiceSource

class TestMyServiceSource(connector_compliance_suite(MyServiceSource)):
    pass
```

Run with: `pytest tests/test_compliance.py -v`

## Building a Toolkit

A **toolkit** is the agent-facing side of a connector: a catalog of named
tools an LLM can call (each with a JSON-Schema input), their execution, and
the connection (OAuth) logic for the service. It lives in the same
`connectors/<name>/` package as the connector and is discovered through its
own entry-point group, `aisquare_pipe.toolkits`.

```python
from aisquare.pipe import AuthType, OAuthSpec, Toolkit, ToolError, ToolResult, ToolSpec

SEARCH = ToolSpec(
    slug="MYSVC_SEARCH_FILES",             # UPPER_SNAKE, starts with slug_prefix
    description="Search files by name.",   # 1..1024 chars — the model reads this
    input_schema={
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Name substring."},
            "limit": {"type": "integer", "default": 10},
        },
        "required": ["query"],
    },
    read_only=True,                        # destructive=True for deletes/overwrites
    tags=("files",),
)


class MyServiceToolkit(Toolkit):
    name = "mysvc"                         # == entry-point name
    version = "0.1.0"                      # == pyproject version
    display_name = "My Service"
    description = "Files in My Service"
    auth_type = AuthType.OAUTH2
    # slug_prefix defaults to name.upper() + "_"  ->  "MYSVC_"
    oauth = OAuthSpec(
        provider="mysvc",
        authorize_url="https://mysvc.example/oauth/authorize",
        token_url="https://mysvc.example/oauth/token",
        revoke_url="https://mysvc.example/oauth/revoke",
        default_scopes=("files.read",),
        pkce=True,
    )

    def tools(self) -> list[ToolSpec]:
        return [SEARCH]                    # never touch the network here

    def run_tool(self, tool, arguments, config) -> ToolResult:
        if tool.slug == "MYSVC_SEARCH_FILES":
            items = search(config["access_token"], arguments["query"], arguments["limit"])
            return ToolResult.ok({"items": items})
        raise ToolError(f"{tool.slug} is not implemented")
```

What the base class does for you:

- **`call_tool(slug, arguments, config)` never raises.** It resolves the slug,
  validates `arguments` against `input_schema` (required keys, primitive
  types, enums, nested objects/arrays; `null` for an optional argument is
  dropped and top-level `default`s are filled in), checks
  `validate_config(config)`, and only then calls your `run_tool`. Every
  failure — unknown slug, bad arguments, no credentials, a `ToolError`, any
  other exception — comes back as `ToolResult(successful=False, error=...)`.
  Secret-looking config values are masked out of error messages.
- **`search_tools(query, limit=6)`** ranks the catalog with a deterministic
  keyword score (+2 per query term in the slug, +1 in the description or
  tags; first-person queries favour `..._AUTHENTICATED_...` tools).
- **`validate_config(config)`** defaults to "has a non-empty
  `access_token`" (or always true for `AuthType.NONE`). Override it for
  other credential shapes, but keep it offline — `call_tool` runs it on
  every call.
- **OAuth**: `build_authorize_url`, `exchange_code`, `refresh_access_token`
  and `revoke_token` run the standard authorization-code flow from `oauth`
  over stdlib `urllib` (JSON or form-encoded token responses, refresh-token
  retention and rotation, PKCE, `client_secret_post`/`client_secret_basic`).
  Override the method for provider quirks (e.g. a non-RFC revoke endpoint).
  Failures raise `OAuthError` carrying the provider's error code — never a
  token, code or secret.

Conventions:

- `config` carries `access_token` plus connector-specific non-secret keys.
  The toolkit never stores tokens; the host (e.g. pipe-as-service) does.
- Return JSON-serializable `data`. Binary payloads (downloads) go in
  `ToolResult.files` as `DataEnvelope`s with the MIME type as `content_type`
  and `metadata["filename"]`.
- Raise `ToolError("human-readable message")` for expected failures (not
  found, permission denied, rate limited). Never put tokens or the raw
  argument payload in it, and never log them — log slugs, counts, timings.
- When porting a Composio toolkit, keep Composio's slugs and argument names
  so existing callers keep working.

Register it next to the connector's entry points:

```toml
[project.entry-points."aisquare_pipe.toolkits"]
mysvc = "aisquare_pipe_mysvc.toolkit:MyServiceToolkit"
```

and add the toolkit compliance suite to `tests/test_compliance.py` —
`pipe validate` requires it for any package that registers a toolkit:

```python
from aisquare.pipe.testing.compliance import toolkit_compliance_suite
from aisquare_pipe_mysvc.toolkit import MyServiceToolkit

class TestMyServiceToolkit(toolkit_compliance_suite(MyServiceToolkit)):
    pass
```

The suite (and the contract layer of `pipe validate`) checks, without
credentials or network: zero-arg instantiation, name + semver version, the
entry-point name equals `name`, slugs are unique and carry `slug_prefix`,
every `input_schema` is `{"type": "object", "properties": {...}}` with
`required` ⊆ `properties`, descriptions are non-empty and ≤ 1024 chars,
`call_tool` of an unknown slug and of every real slug with `({}, {})` returns
a failed `ToolResult`, and OAuth endpoints are `https`.

For hermetic tests of code that *hosts* toolkits, use
`aisquare.pipe.testing.mock_connectors.MockToolkit` (registered as toolkit
`mock`): an in-memory items store with list/get/create/delete, a file
download and a whoami, plus `MOCK_OAUTH`.

Inspect any installed toolkit from the CLI:

```bash
pipe tools                              # installed toolkits
pipe tools mysvc                        # every slug + description
pipe tools mysvc --search "find a file" # ranked, top 6
pipe tools mysvc --json                 # full ToolSpecs
```

## Live Tests (optional)

Everything above is hermetic by design. If you also want to catch real
vendor-API drift, add a live tier — it must never make plain `pytest` or
`pipe validate` need credentials:

```python
# tests/test_live.py
import os

import pytest

from aisquare.pipe import PullParams
from aisquare_pipe_my_service.connector import MyServiceSource

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        not os.environ.get("MY_SERVICE_API_KEY"),
        reason="MY_SERVICE_API_KEY not set",
    ),
]


def test_pull_one_real_item():
    config = {"api_key": os.environ["MY_SERVICE_API_KEY"]}
    envelopes = list(
        MyServiceSource().pull(config, PullParams(params={"limit": 1}))
    )
    assert envelopes
```

And register the marker in your `pyproject.toml` so plain `pytest` deselects
it even when credentials happen to be exported:

```toml
[tool.pytest.ini_options]
testpaths = ["tests"]
markers = ["live: hits real APIs using env credentials; deselected by default"]
addopts = "-m 'not live'"
```

Conventions: env vars are named `<SERVICE>_API_KEY` (or the service's natural
credential set), tests self-skip without them, and `pipe validate --live` is
the only thing that runs them (`SKIP (no creds)` when the env is absent).

## Publishing to PyPI

1. Name your package: `aisquare-pipe-{service}` (e.g., `aisquare-pipe-google-drive`)
2. Add `aisquare-pipe>=0.1.0` as a dependency
3. Register entry points as shown above
4. Build and publish:

```bash
python -m build
twine upload dist/*
```

## Naming Convention

- Package name: `aisquare-pipe-{service}` (e.g., `aisquare-pipe-s3`)
- Connector name attribute: `{service}` (e.g., `"s3"`)
- Entry point name: `{service}` or `{service}-source` / `{service}-sink`
- Toolkit entry point name: exactly the toolkit's `name` (e.g. `"github"`)

## Which Tier?

All connectors live under `connectors/<name>/` regardless of popularity; the user-facing tier is controlled by the `[popular]` and `[full]` extras in the root `pyproject.toml`.

- **Super-popular and broadly useful** → add the package name to `[popular]`. Users get it via `pip install "aisquare-pipe[popular]"`.
- **Niche / enterprise / client-specific / experimental** → leave it out of `[popular]`. It still ships via `[full]` (`pip install "aisquare-pipe[full]"`) and via direct install (`pip install aisquare-pipe-{service}`).
- **When in doubt, leave it out of `[popular]`.** Promoting later is cheap; demoting feels worse to users.

## Code & Folder Isolation

Connector PRs must keep all new code inside `connectors/<name>/`:

- Source under `connectors/<name>/src/aisquare_pipe_<name>/`
- Tests under `connectors/<name>/tests/`
- Any auxiliary directories the connector needs (e.g., `migrations/`, `schemas/`, `fixtures/`) go **inside** `connectors/<name>/`, not at the repo root.
- No imports from another connector's `src/`. Connectors compose via the framework, not via each other.
- No edits to `src/aisquare/pipe/` from a connector PR. Framework changes ship in separate PRs.
