# aisquare-pipe-dropbox

Dropbox source and sink connectors for [aisquare.pipe](../../README.md).

## Install

```bash
pip install aisquare-pipe-dropbox
```

Or for development (from the repo root):

```bash
cd connectors/dropbox
pip install -e ".[dev]"
```

## Configuration

Two authentication modes are supported:

```python
# Mode 1: Access token (quick testing)
config = {"access_token": "sl.XXXXX"}

# Mode 2: Refresh token (recommended — auto-refreshes)
config = {
    "app_key": "YOUR_APP_KEY",
    "app_secret": "YOUR_APP_SECRET",
    "refresh_token": "YOUR_REFRESH_TOKEN",
}
```

## Usage

### Pull files from Dropbox

```python
from aisquare.pipe import Pipeline, PullParams
from aisquare_pipe_dropbox import DropboxSource, DropboxSink

source = DropboxSource()
params = PullParams(params={
    "path": "/documents",
    "recursive": True,
    "extensions": [".pdf", ".docx"],
    "limit": 10,
})

for envelope in source.pull(config, params):
    print(f"{envelope.metadata['filename']} ({envelope.content_type})")
```

### Push files to Dropbox

```python
from aisquare.pipe import DataEnvelope, PushParams

sink = DropboxSink()
envelope = DataEnvelope(
    content_type="text/plain",
    data="Hello from aisquare.pipe!",
    source_id="my-app",
    metadata={"filename": "hello.txt"},
)
result = sink.push(envelope, config, PushParams(params={"target_path": "/uploads"}))
print(f"Uploaded: {result.ref}")
```

### Pipeline (source to sink)

```python
from aisquare.pipe import Pipeline

source = DropboxSource()
sink = DropboxSink()

pipeline = Pipeline(source=source, sink=sink)
result = pipeline.run({
    "dropbox-source": {"access_token": "SOURCE_TOKEN"},
    "dropbox-sink": {"access_token": "SINK_TOKEN"},
})
print(f"Transferred {result.success_count} files")
```

## Features

- Automatic MIME type detection from filenames
- Streaming for large files (>50MB download, >140MB chunked upload)
- Upload sessions for files up to 350GB
- Automatic retry with exponential backoff on rate limits (429)
- OAuth2 refresh token support with auto-refresh
- Folder listing with pagination and recursive support
