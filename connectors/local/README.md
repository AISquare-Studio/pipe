# aisquare-pipe-local

Local filesystem source and sink connectors for [aisquare.pipe](../../README.md). No external dependencies — uses only Python stdlib.

## Install

```bash
cd connectors/local
pip install -e ".[dev]"
```

## Configuration

```python
config = {"base_path": "/path/to/directory"}
```

No authentication required — just a directory path.

## Usage

### Source — pull files from disk

```python
from aisquare.pipe import Pipeline
from aisquare.pipe.core.envelope import PullParams

pipeline = Pipeline("local-source", "mock-sink")
result = pipeline.run(
    source_config={"base_path": "/data/input"},
    sink_config={},
)

# With filters
params = PullParams(params={
    "path": "reports",                      # subdirectory
    "recursive": True,                      # include nested dirs
    "extensions": [".pdf", ".csv"],         # file type filter
    "glob": "**/*.json",                    # glob pattern (alternative)
    "limit": 100,                           # max files
    "stream_threshold": 50 * 1024 * 1024,   # stream files > 50MB
})
```

### Sink — push files to disk

```python
from aisquare.pipe.core.envelope import PushParams

params = PushParams(params={
    "target_path": "output/2025",           # subdirectory (created automatically)
    "conflict": "rename",                   # "fail" (default), "overwrite", or "rename"
})
```

## Connector Details

| Property | Source | Sink |
|----------|--------|------|
| Name | `local-source` | `local-sink` |
| Types | `*/*` (any file) | `*/*` (any file) |
| Auth | None | None |
| Streaming | Yes (configurable threshold) | Yes (stream → disk) |
| Max file size | — | No limit |
| Path traversal protection | Yes | Yes |

## Running Tests

```bash
pytest -v   # 69 tests
```
