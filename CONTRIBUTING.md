# Contributing: Building a Connector Plugin

This guide explains how to build a connector plugin for `aisquare.pipe`.

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
