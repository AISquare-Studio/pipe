# aisquare.pipe

[![CI](https://github.com/AISquare-Studio/pipe/actions/workflows/validate.yml/badge.svg)](https://github.com/AISquare-Studio/pipe/actions/workflows/validate.yml)
[![PyPI](https://img.shields.io/pypi/v/aisquare-pipe.svg)](https://pypi.org/project/aisquare-pipe/)
[![Python](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/downloads/)
[![License: Apache 2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)
[![Code style: Ruff](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json)](https://github.com/astral-sh/ruff)
[![PRs welcome](https://img.shields.io/badge/PRs-welcome-brightgreen.svg)](CONTRIBUTING.md)

**Universal anything-to-anything connector framework.** Instead of building N² bespoke integrations between services, each service implements a single Source (pulls data) and/or Sink (pushes data) connector against a universal `DataEnvelope` spec. The framework handles type matching, pipeline orchestration, and plugin discovery.

## Install

```bash
pip install aisquare-pipe                       # framework only
pip install "aisquare-pipe[popular]"            # framework + the canonical core connectors
pip install "aisquare-pipe[full]"               # framework + all connectors maintained in this repo
pip install aisquare-pipe-<service>             # framework + a single connector (e.g. aisquare-pipe-dropbox)
```

For development:

```bash
git clone git@github.com:AISquare-Studio/pipe.git && cd pipe
pip install -e ".[dev]"
```

> **Note:** Plugin discovery via `entry_points` requires the package to be installed (`pip install -e .`).

## Connectors

Each connector is its own independently published pip package under `connectors/<name>/`. The user-facing tier is encoded in the extras bundles — folder location does not determine tier.

| Connector | Package | In `[popular]` | In `[full]` |
|---|---|:-:|:-:|
| Local filesystem | `aisquare-pipe-local` | ✓ | ✓ |
| Dropbox | `aisquare-pipe-dropbox` | ✓ | ✓ |
| OneDrive | `aisquare-pipe-onedrive` | ✓ | ✓ |
| Salesforce | `aisquare-pipe-salesforce` |   | ✓ |
| DocuSign | `aisquare-pipe-docusign` |   | ✓ |

Third-party connectors published as `aisquare-pipe-<name>` (or any package declaring the `aisquare_pipe.connectors` entry-point group) are auto-discovered after install.

## Quick Start

```python
from aisquare.pipe import Pipeline
from aisquare.pipe.testing.mock_connectors import MockSource, MockSink

source = MockSource(count=5)
sink = MockSink()

result = Pipeline(source=source, sink=sink).run({})

print(f"Transferred {result.success_count} envelopes")
print(f"Sink received: {[e.data for e in sink.received]}")
```

## Architecture

```
┌─────────────┐    ┌──────────────┐    ┌─────────────┐
│   Source     │───>│ DataEnvelope │───>│    Sink     │
│  Connector   │    │              │    │  Connector   │
│              │    │ content_type │    │              │
│  pull() ─────┼──> │ data         │ ──>│──── push()  │
│              │    │ metadata     │    │              │
└─────────────┘    └──────────────┘    └─────────────┘
       │                  │                    │
       │          ┌───────┴────────┐           │
       │          │  TypeMatcher   │           │
       │          │  ┌──────────┐  │           │
       │          │  │Converter │  │           │
       │          │  └──────────┘  │           │
       │          └────────────────┘           │
       │                                       │
       └──────── Pipeline.run() ───────────────┘
```

## CLI

```bash
pipe list                            # list installed connectors
pipe describe mock-source            # show connector details
pipe check mock-source mock-sink     # check type compatibility
pipe run --source mock-source --sink mock-sink --config config.json
pipe new-connector my-service        # scaffold a new connector plugin
```

## Building a Connector

See [CONTRIBUTING.md](CONTRIBUTING.md) for the full guide. In short:

1. `pipe new-connector my-service` — scaffolds a plugin project
2. Implement `SourceConnector.pull()` and/or `SinkConnector.push()`
3. Register via `entry_points` in your `pyproject.toml`
4. Run `connector_compliance_suite(MyConnector)` to validate

## Contributing & Community

Contributions are welcome! See **[CONTRIBUTING.md](CONTRIBUTING.md)** for the full guide — `pipe new-connector <service>` scaffolds a connector plugin in seconds.

- 📋 [Changelog](CHANGELOG.md)
- 🔒 [Security policy](SECURITY.md)
- 🤝 [Code of Conduct](CODE_OF_CONDUCT.md)

## License

[Apache License 2.0](LICENSE) © AISquare Studio
