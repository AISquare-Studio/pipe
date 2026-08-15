# aisquare-pipe-gdrive

Google Drive source and sink connectors for aisquare.pipe.

## Install

```bash
pip install aisquare-pipe-gdrive
```

## Auth

Three credential shapes, in precedence order. Credentials go in `config`; everything
else goes in pull/push params.

**Service account** (recommended for unattended pipelines):

```json
{"gdrive-source": {"service_account_json": "/path/to/key.json"}}
```

`service_account_json` accepts a path, a JSON string, or an inline object. For
Workspace domain-wide delegation, add `"subject": "user@example.com"`.

**OAuth refresh token:**

```json
{"gdrive-source": {
  "client_id": "...", "client_secret": "...", "refresh_token": "..."
}}
```

**Access token** (testing only): `{"access_token": "..."}`.

### Scopes

Both `gdrive-source` and `gdrive-sink` default to the full `drive` scope — the
client does not vary the default by connector. If you only need read access,
narrow it explicitly: `{"scopes": ["https://www.googleapis.com/auth/drive.readonly"]}`.

Neither connector can use `drive.file` against folders it did not create — that
scope only grants access to files the app itself created, so writing into a
pre-existing `folder_id`, and the `overwrite`/`rename` conflict modes, will fail.
Override with `"scopes": [...]` only when the app owns the target folder.

## Source params

| Key | Default | Meaning |
|---|---|---|
| `folder_id` | `"root"` | Folder to list |
| `recursive` | `false` | Descend into subfolders |
| `query` | `""` | Raw Drive query, ANDed with the parent/trashed clauses |
| `extensions` | — | Filter by extension, e.g. `[".pdf"]` |
| `glob` | — | Glob matched against the filename |
| `limit` | — | Max files to emit |
| `stream_threshold` | 50 MB | Size above which `envelope.stream` is used |
| `export_formats` | Office formats | Override native export targets |
| `skip_google_native` | `false` | Skip Docs/Sheets/Slides entirely |

`content_type` comes from Drive's own `mimeType` field, not from the filename.

## Google-native files

Docs, Sheets and Slides have no downloadable bytes — they are exported. Defaults:

| Native type | Exported as |
|---|---|
| `document` | `.docx` |
| `spreadsheet` | `.xlsx` |
| `presentation` | `.pptx` |
| `drawing` | `.png` |

Override by short name: `{"export_formats": {"document": "text/markdown"}}`.

The exported filename gains the target extension, and `metadata["exported_from"]`
records the original native MIME type.

Two limits worth knowing. Drive caps exported content at **10 MB** regardless of
format; larger documents surface a clear `PipelineError`. Forms, Apps Script and
Shortcuts have no export path at all — they are logged and skipped rather than
failing the run.

## Sink params

| Key | Default | Meaning |
|---|---|---|
| `folder_id` | `"root"` | Destination folder |
| `target_path` | `""` | Slash-separated subfolders, created as needed |
| `conflict` | `"fail"` | `fail`, `overwrite`, or `rename` |

Drive permits duplicate filenames in a folder, so the conflict check is explicit.
`rename` appends ` (1)`, ` (2)`, … before the extension.

When a stream envelope is pushed with `conflict="overwrite"`, the whole stream is
read into memory before the update, because Drive's update-in-place path takes
bytes rather than a resumable session. Pushing very large files with `overwrite`
carries that memory cost; prefer `"rename"` or `"fail"` for large streamed uploads.

`GDriveSink.metadata_spec` only declares `filename`, matching `OneDriveSink` and
`DropboxSink`. If an envelope carries a `stream`, it must also carry
`metadata["size"]` — `push()` reads it to size the resumable upload session, and
without it the size is treated as 0 and the upload fails.

`push()` never raises; failures return `PushResult(success=False, error=...)`.

## Shared drives

Set `"drive_id": "..."` in config to target a shared drive. This adds `driveId`,
`corpora=drive`, `includeItemsFromAllDrives` and `supportsAllDrives` to every query.

## Example

```bash
pipe run --source gdrive-source --sink local-sink --config config.json
```

```json
{
  "gdrive-source": {"service_account_json": "/path/to/key.json"},
  "local-sink": {"base_path": "./downloads"}
}
```

## Development

```bash
pip install -e ".[dev]"
pytest tests -v
pipe validate gdrive
```
