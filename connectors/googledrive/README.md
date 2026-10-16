# aisquare-pipe-googledrive

Google Drive for [aisquare.pipe](../../README.md), over the Drive v3 REST API
(plain `requests`, no Google SDK). The package ships three things:

| What | Registered as | Use it for |
|---|---|---|
| `GoogleDriveToolkit` | toolkit `googledrive` (`aisquare_pipe.toolkits`) | 23 LLM-callable tools — search, metadata, download/export, upload, folders, move/copy/rename/trash, sharing, comments, shared drives — plus Google's OAuth spec |
| `GoogleDriveSource` | connector `googledrive-source` | Pull a folder's files into a pipeline (Google Docs/Sheets/Slides exported) |
| `GoogleDriveSink` | connector `googledrive-sink` | Upload envelopes to a folder (multipart up to 5 MB, resumable above) |

My Drive and shared drives both work everywhere.

## Install

```bash
pip install aisquare-pipe-googledrive        # or: pip install "aisquare-pipe[popular]"
cd connectors/googledrive && pip install -e ".[dev]"   # development
```

## Connecting (OAuth)

`GOOGLEDRIVE_OAUTH` (also `GoogleDriveToolkit.oauth`) describes a Google OAuth
2.0 *web application* client:

| | |
|---|---|
| authorize | `https://accounts.google.com/o/oauth2/v2/auth` with `access_type=offline`, `prompt=consent`, `include_granted_scopes=true` |
| token | `https://oauth2.googleapis.com/token` |
| revoke | `https://oauth2.googleapis.com/revoke` |
| PKCE | required (S256) |
| tokens | access tokens expire after one hour; the refresh token does not rotate (`token_expires=True`, `rotates_refresh_token=False`) |
| default scope | `https://www.googleapis.com/auth/drive` |

`prompt=consent` makes Google return a refresh token on every connect, not
only the first one. Revoking either token of a connection (access or refresh)
revokes the user's whole grant to your OAuth client: Google has no narrower
per-token revocation, so two connections made by the same Google user for the
same OAuth client end together.

**Other Google toolkits need their own OAuth client.** `include_granted_scopes=true`
merges every scope a user has granted to an OAuth client into each new token.
So if a future Gmail or Google Calendar port reused this Drive client, a Drive
token would carry their scopes, and a Drive disconnect (a grant-wide revoke)
would end those connections too. Give each Google toolkit its own OAuth client,
or drop `include_granted_scopes` there.

**Scope and Google verification.** The default `drive` scope (full access to
every file) is a Google **restricted** scope. A production app that requests it
must pass Google's OAuth app verification *and* an annual third-party security
assessment (CASA). Until then the consent screen shows an "unverified app"
warning, and the app is limited to 100 test users. Narrower scopes, passed as
`scopes=[...]` to `build_authorize_url`:

| Scope | Class | What works |
|---|---|---|
| `https://www.googleapis.com/auth/drive.file` | non-sensitive | Only files the app created or the user opened with it (Google Picker). Search, download and write work on those files only. |
| `https://www.googleapis.com/auth/drive.readonly` | restricted | Every read tool and the source; no writes |
| `https://www.googleapis.com/auth/drive.metadata.readonly` | restricted | Search and metadata only; no downloads |
| `https://www.googleapis.com/auth/drive.appdata` | non-sensitive | The app's hidden app-data folder only |

Set up: Google Cloud console → APIs & Services → enable the **Google Drive
API** → OAuth consent screen → Credentials → *OAuth client ID* (Web
application) with your callback as an authorized redirect URI.

## Toolkit

```python
from aisquare_pipe_googledrive import GoogleDriveToolkit

drive = GoogleDriveToolkit()
config = {"access_token": "ya29...."}

drive.search_tools("find the roadmap doc", limit=3)      # -> [ToolSpec, ...]
result = drive.call_tool("GOOGLEDRIVE_FIND_FILE", {"q": "roadmap", "pageSize": 10}, config)
result.successful, result.data, result.error, result.error_code
```

`call_tool` never raises. Slugs and argument names are Composio's Google
Drive toolkit's, verbatim, including Composio's mixed casing (`fileId` on
some tools, `file_id` on others). The other spelling is accepted too: the
BE's Content Vault import sends `file_id` to `GOOGLEDRIVE_GET_FILE_METADATA`
and `GOOGLEDRIVE_DOWNLOAD_FILE`, whose schemas say `fileId`. A Drive/Docs URL
works wherever a file or folder id goes.

**Results.** Object endpoints return Drive's resource (minus its `kind`
markers). List tools return one page per call, with the pagination hints
first:

```python
{"has_more": True, "next_page_token": "…", "incomplete_search": False, "items": [...]}
```

Pass `next_page_token` back as `pageToken` (`page_token` for
`GOOGLEDRIVE_FIND_FOLDER`). Files come with `id, name, mimeType, size,
modifiedTime, createdTime, parents, webViewLink, trashed, starred, driveId`
unless `fields` asks for others.

**Searching.** `q` takes [Drive query syntax](https://developers.google.com/drive/api/guides/search-files)
(`name contains 'roadmap' and trashed = false`). Plain words become a
full-text search and a bare email an owner search. Searches cover My Drive,
shared-with-me and every shared drive (`corpora=allDrives`); `driveId`
narrows to one shared drive. With `folder_id` / `folderId` the caller's `q`
is ANDed with the folder scope as one parenthesised group. A `q` that could
escape that group fails with `invalid_arguments` before any request: unbalanced
parentheses outside quoted strings, a `)` that closes early, or an unclosed
quote. A `q` without a folder is sent verbatim.

**Downloads.** `GOOGLEDRIVE_DOWNLOAD_FILE` and
`GOOGLEDRIVE_EXPORT_GOOGLE_WORKSPACE_FILE` put the bytes in `ToolResult.files`
(one `DataEnvelope`: `content_type` = MIME type, `metadata["filename"]`) and
describe the file in `data` (`id`, `name`, `mimetype`, `size`, `exported`,
plus `content` for UTF-8 text up to 100 KB). Google Docs, Slides and Drawings
are exported as PDF and Sheets as XLSX unless `mime_type` picks another
format (`text/markdown`, `text/plain`, DOCX, `text/csv`, …). Shortcuts are
followed once; folders, Forms and Sites are refused. Downloads are read in
chunks, capped at 100 MB and checked against the time budget between chunks.
Google caps exports at 10 MB.

**Uploads.** `GOOGLEDRIVE_UPLOAD_FILE` takes the bytes base64-encoded in
`file_to_upload.file_content_bytes`. Composio's `s3key` names Composio's own
storage and is not supported. Up to 5 MB goes as one multipart request, more
through a resumable session in 8 MiB chunks. A chunk that fails with a
500/502/503/504 or a dropped connection does not end the upload. The client
backs off (1 s, 2 s, 4 s; up to 3 times per chunk, within the time budget),
asks Google how much it holds (`Content-Range: bytes */<total>`), and carries
on from there.

**Errors.** A failed call carries core's canonical `error_code` (branch on
this), a human-readable `error` (`"<status>: <Google's message>"` plus a
hint), `retry_after` (seconds) when rate limited, and
`data={"status": <HTTP status>, "reason": <Google's reason>}` when Google
answered:

| Failure | `error_code` | `error` |
|---|---|---|
| 401 | `unauthorized` | `401: Invalid Credentials — … refresh it, or the Google Drive connection must be reconnected` |
| 429, or a 403 with reason `rateLimitExceeded` / `userRateLimitExceeded`, once retries or the budget run out | `rate_limited` (+ `retry_after`) | `403: User Rate Limit Exceeded — Google Drive rate limit; retry in about 8s` |
| any other 403 (`insufficientFilePermissions`, `dailyLimitExceeded`, `storageQuotaExceeded`, `domainPolicy`, …) | `forbidden` | `403: … — this Google Drive connection is not allowed to do that (…)` |
| 404, 410 | `not_found` | `404: File not found: … — it does not exist, or this Google Drive connection cannot see it` |
| 400, and arguments the toolkit refuses (`.`/`..` ids, a folder given to a download, bad base64, a file over the download cap, …) | `invalid_arguments` | Google's message, or the refusal |
| 409, 412 | `conflict` | Google's message |
| request timeout, 504, spent `timeout_seconds` | `timeout` | `Google Drive did not answer within 30s (ReadTimeout)` |
| 5xx, network failure | `upstream_error` | `500: …` / `network error talking to Google Drive (ConnectionError)` |

Core adds `unknown_tool`, `invalid_arguments` (schema) and `unauthorized`
(no `access_token`) before a request is made.

**Rate limits and retries.** Rate limits (above) are retried up to 3 times
with exponential backoff (1 s, 2 s, 4 s; or Google's `Retry-After` when it is a
finite number of seconds; `inf`, `nan` or an overflowing value falls back to
the backoff),
on any method, since Google did not process the request. A 500/502/503/504 is
retried the same way on GETs only. Total sleep per call is capped at 60 s; a
longer wait fails at once with `retry_after`. Tune with
`GoogleDriveToolkit(max_retries=..., max_retry_wait=...)`.

**Time budget.** A host may set `config["timeout_seconds"]` (pipe-as-service
does). The whole call fits inside it: each request's timeout is the smaller
of 30 s (300 s for file bytes) and the time left. A retry whose sleep would
not leave `min(30 s, 2 s)` for the request fails at once as `rate_limited`
(with `retry_after`). A budget spent before a request, or between download
chunks, is `timeout`.

**Concurrency.** A `GoogleDriveToolkit` holds no mutable state, and each call
opens and closes its own HTTP session. Passing `session=` opts into a shared
session the host manages.

**Secrets.** The token is sent only in the `Authorization` header (never to
a resumable-upload session URL, which must be an `https://www.googleapis.com`
URL). It never appears in a URL, log line, error or repr. Logs carry method,
status and timing, never paths, ids or arguments.

### Tools

Read-only:

| Slug | Drive endpoint | Use for |
|---|---|---|
| `GOOGLEDRIVE_FIND_FILE` | `GET /files` | search by name, content, type, date, owner, folder |
| `GOOGLEDRIVE_LIST_FILES` | `GET /files` | list a folder (`folderId`) or a query |
| `GOOGLEDRIVE_FIND_FOLDER` | `GET /files` (folders) | find a folder id by name |
| `GOOGLEDRIVE_GET_FILE_METADATA` | `GET /files/{fileId}` | one file's details |
| `GOOGLEDRIVE_DOWNLOAD_FILE` | `GET /files/{fileId}?alt=media` or `/export` | a file's bytes (`ToolResult.files`) |
| `GOOGLEDRIVE_EXPORT_GOOGLE_WORKSPACE_FILE` | `GET /files/{fileId}/export` | a Doc/Sheet/Slides file in a chosen format |
| `GOOGLEDRIVE_GET_ABOUT` | `GET /about` | the user and their storage quota |
| `GOOGLEDRIVE_LIST_SHARED_DRIVES` | `GET /drives` | shared drives (ids for `driveId`) |
| `GOOGLEDRIVE_LIST_PERMISSIONS` | `GET /files/{fileId}/permissions` | who has access |
| `GOOGLEDRIVE_LIST_COMMENTS` | `GET /files/{fileId}/comments` | a file's comments and replies |

Writes:

| Slug | Drive endpoint | Notes |
|---|---|---|
| `GOOGLEDRIVE_CREATE_FOLDER` | `POST /files` | `parent_id` may be a folder id or an exact folder name |
| `GOOGLEDRIVE_UPLOAD_FILE` | `POST /upload/…/files` | base64 bytes in `file_to_upload.file_content_bytes` |
| `GOOGLEDRIVE_CREATE_FILE_FROM_TEXT` | `POST /upload/…/files` | text, or a Google Doc / Sheet (from CSV) by `mime_type` |
| `GOOGLEDRIVE_EDIT_FILE` | `PATCH /upload/…/files/{file_id}` | **destructive**: replaces a stored file's content |
| `GOOGLEDRIVE_COPY_FILE` | `POST /files/{file_id}/copy` | |
| `GOOGLEDRIVE_MOVE_FILE` | `PATCH /files/{file_id}` | the current folder is removed unless `remove_parents` is given |
| `GOOGLEDRIVE_UPDATE_FILE_PUT` | `PATCH /files/{fileId}` | rename, description, star, parents (metadata only) |
| `GOOGLEDRIVE_DELETE_FILE` | `PATCH /files/{fileId}` (`trashed=true`) | **destructive**: moves to the trash (recoverable for 30 days) |
| `GOOGLEDRIVE_TRASH_FILE` | same | **destructive**: Composio's soft-delete slug |
| `GOOGLEDRIVE_UNTRASH_FILE` | `PATCH /files/{file_id}` (`trashed=false`) | restore from the trash |
| `GOOGLEDRIVE_ADD_FILE_SHARING_PREFERENCE` | `POST /files/{file_id}/permissions` | grant a role to a user, group, domain or anyone with the link |
| `GOOGLEDRIVE_CREATE_PERMISSION` | same | plus notification text, expiration, discovery options |
| `GOOGLEDRIVE_CREATE_COMMENT` | `POST /files/{file_id}/comments` | |

Differences from Composio, on purpose:
- `GOOGLEDRIVE_UPDATE_FILE_PUT` is Composio's real metadata-update slug. There is no `GOOGLEDRIVE_UPDATE_FILE_METADATA`.
- `GOOGLEDRIVE_DELETE_FILE` trashes. Composio's deprecated slug deleted permanently, which is not recoverable.
- `GOOGLEDRIVE_UPLOAD_FILE` refuses a missing destination folder rather than silently uploading to the root.

## Source

```python
from aisquare.pipe import PullParams
from aisquare_pipe_googledrive import GoogleDriveSource

for envelope in GoogleDriveSource().pull(
    {"access_token": "ya29...."},
    PullParams({"path": "Reports/2026", "recursive": True, "extensions": [".pdf", ".docx"]}),
):
    print(envelope.metadata["path"], envelope.content_type, len(envelope.data))
```

| Param | Default | Meaning |
|---|---|---|
| `folder_id` / `drive_id` | `root` | folder to pull; a shared drive's id is its root folder |
| `path` | — | folder path below `folder_id`, e.g. `Reports/2026` |
| `recursive` | `False` | descend into subfolders (each folder once) |
| `mime_types` | all | Drive's type or the exported one; `image/*` wildcards |
| `extensions` | all | of the delivered file name, e.g. `[".pdf"]` |
| `modified_after` | — | RFC 3339 time (incremental pulls) |
| `query` | — | extra Drive query clause |
| `export_formats` | see below | `{google type: export MIME}` overrides |
| `limit` | — | stop after N files |
| `stream_threshold` | 50 MB | larger stored files arrive as `envelope.stream` |

Exports by default: Docs → DOCX, Sheets → CSV (**first tab only**; use
`{"application/vnd.google-apps.spreadsheet": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"}`
for every tab), Slides → PDF, Drawings → PNG, Apps Script → JSON. Folders,
shortcuts, Forms and Sites are skipped. Metadata per envelope: `filename`,
`file_id`, `mime_type`, `path`, `name`, `source_mime_type`, `exported`,
`size`, `modified_time`, `web_view_link`, `drive_id`, `md5_checksum`.

A rejected token raises `ConfigValidationError` (reconnect); other Drive
failures raise `PipelineError`. `validate_config` is an offline shape check.

## Sink

```python
from aisquare.pipe import PushParams
from aisquare_pipe_googledrive import GoogleDriveSink

result = GoogleDriveSink().push(envelope, {"access_token": "ya29...."}, PushParams({"path": "Imports", "conflict": "replace"}))
result.success, result.ref, result.metadata  # ref = the Drive file id
```

| Param | Default | Meaning |
|---|---|---|
| `folder_id` | `root` | destination folder |
| `path` | — | folder path below `folder_id`; created unless `create_folders=False` |
| `conflict` | `rename` | a same-named file exists: `rename` (`a (1).txt`), `replace` (new revision, same id), `fail`, `keep_both` |
| `convert_to` | — | Google type to convert into, e.g. `application/vnd.google-apps.document` for a DOCX |
| `description` | — | the new file's description |

`push` never raises. Failures return `PushResult(success=False)` with
`metadata={"status", "error_code", "retry_after"?}`.

## Tests

```bash
pytest -q                                   # hermetic: HTTP mocked, sockets blocked
GOOGLEDRIVE_ACCESS_TOKEN=ya29.... pytest -m live tests/test_live.py   # read-only live checks
GOOGLEDRIVE_LIVE_WRITE=1 ...                # also a write round trip in a scratch folder (trashed afterwards)
```

`tests/test_compat_be.py` mirrors the BE's Content Vault import, which calls
`GOOGLEDRIVE_FIND_FILE`, `GOOGLEDRIVE_GET_FILE_METADATA` and
`GOOGLEDRIVE_DOWNLOAD_FILE`. It checks the arguments the BE sends and the
fields it reads.
