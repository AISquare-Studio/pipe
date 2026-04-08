# aisquare.pipe

[![License: Apache 2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)

**Universal anything-to-anything connector framework.** Instead of building N² bespoke integrations between services, each service implements a single Source (pulls data) and/or Sink (pushes data) connector against a universal `DataEnvelope` spec. The framework handles type matching, pipeline orchestration, and plugin discovery.

## Install

```bash
pip install aisquare-pipe
```

For development:

```bash
git clone git@github.com:AISquare-Studio/pipe.git && cd pipe
pip install -e ".[dev]"
```

> **Note:** Plugin discovery via `entry_points` requires the package to be installed (`pip install -e .`).

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

## License

[Apache License 2.0](LICENSE)
