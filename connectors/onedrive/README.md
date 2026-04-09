# aisquare-pipe-onedrive

OneDrive source and sink connectors for [aisquare.pipe](../../README.md). Uses the Microsoft Graph API via `requests` with `azure-identity` for authentication.

## Install

```bash
cd connectors/onedrive
pip install -e ".[dev]"
```

## Authentication

### Access Token (quick testing)

```python
config = {"access_token": "eyJ0eXAi..."}
```

### Client Credentials (service accounts)

```python
config = {
    "client_id": "YOUR_CLIENT_ID",
    "client_secret": "YOUR_CLIENT_SECRET",
    "tenant_id": "YOUR_TENANT_ID",
}
```

Register an app in [Azure AD](https://portal.azure.com/#view/Microsoft_AAD_RegisteredApps/ApplicationsListBlade) with **Files.ReadWrite.All** application permission.

## Usage

### Source — pull files from OneDrive

```python
from aisquare.pipe import Pipeline
from aisquare.pipe.core.envelope import PullParams

source = "onedrive-source"
sink = "mock-sink"

# Basic pull
pipeline = Pipeline(source, sink)
result = pipeline.run(
    source_config=config,
    sink_config={},
)

# With filters
params = PullParams(params={
    "path": "/Documents/Reports",
    "extensions": [".pdf", ".docx"],
    "recursive": True,
    "limit": 50,
    "stream_threshold": 50 * 1024 * 1024,  # stream files > 50MB
})
```

### Sink — push files to OneDrive

```python
from aisquare.pipe.core.envelope import PushParams

params = PushParams(params={
    "target_path": "/Backups/2025",
    "conflict": "replace",   # or "rename" (default), "fail"
})
```

## Connector Details

| Property | Source | Sink |
|----------|--------|------|
| Name | `onedrive-source` | `onedrive-sink` |
| Types | `*/*` (any file) | `*/*` (any file) |
| Streaming | Yes (configurable threshold) | Yes (auto-chunked) |
| Max file size | — | 250 GB |
| Rate limit | 600 req/min | — |
| Upload threshold | — | 4 MB (simple) / chunked above |
| Chunk size | — | 10 MB (32 × 320 KiB) |

## Running Tests

```bash
pytest -v
```
