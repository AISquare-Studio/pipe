# aisquare-pipe-onedrive

Microsoft OneDrive for [aisquare.pipe](../../README.md), over the Microsoft Graph API:

- **`OneDriveToolkit`** (`onedrive`): 16 LLM-callable tools for searching, downloading, uploading, organizing and sharing files, plus the Microsoft identity platform OAuth spec that connects a user's OneDrive. Tool slugs and argument names match Composio's `one_drive` toolkit.
- **`onedrive-source` / `onedrive-sink`**: pipeline connectors that pull files from a folder and push files to OneDrive.

## Install

```bash
cd connectors/onedrive
pip install -e ".[dev]"
```

## Toolkit

```python
from aisquare.pipe import get_toolkit

toolkit = get_toolkit("onedrive")()
config = {"access_token": "<delegated Graph token>", "timeout_seconds": 30}

found = toolkit.call_tool("ONE_DRIVE_SEARCH_ITEMS", {"q": "roadmap", "top": 10}, config)
item_id = found.data["items"][0]["id"]
download = toolkit.call_tool("ONE_DRIVE_DOWNLOAD_FILE", {"item_id": item_id}, config)
pdf_bytes = download.files[0].data
```

`config["access_token"]` must be a **delegated** (per-user) token, from the OAuth flow below or from Graph Explorer for a quick try. App-only (client-credentials) tokens cannot call `/me` and do not work with the toolkit. `config["timeout_seconds"]` is optional; hosts set it to bound a whole call (request timeouts and throttling waits).

Every tool works on the connected user's own OneDrive (`/me/drive`) unless `drive_id` names another drive (some tools also take `user_id`, `site_id` or `group_id`).

### Tools

| Slug | Kind | Graph call | Notes |
|------|------|-----------|-------|
| `ONE_DRIVE_SEARCH_ITEMS` | read, paged | `GET {drive}/root/search(q='…')` | Plain keywords. `search_scope='drive'` also finds items shared with the user. |
| `ONE_DRIVE_DOWNLOAD_FILE` | read | `GET {drive}/items/{id}` then `…/content` | The bytes are returned in `ToolResult.files`. `format='pdf'\|'html'` converts the file. Small UTF-8 files also come back as `data.content`. The body is streamed: reading stops once it passes 100 MiB, and the time budget is checked between chunks. |
| `ONE_DRIVE_GET_ITEM` | read | `GET {drive}/items/{id}` | Takes `select_fields` and `expand_relations`. |
| `ONE_DRIVE_GET_ITEM_BY_PATH` | read | `GET {drive}/root:/{path}:` | |
| `ONE_DRIVE_LIST_FOLDER_CHILDREN` | read, paged | `GET {drive}/items/{id}/children` or `root:/{path}:/children` | With no folder argument, it lists the root. |
| `ONE_DRIVE_GET_DRIVE` | read | `GET {drive}` | Returns the drive's owner, type and quota. |
| `ONE_DRIVE_GET_RECENT_ITEMS` | read, paged | `GET /me/drive/recent` | Microsoft is retiring this endpoint. |
| `ONE_DRIVE_GET_SHARED_ITEMS` | read, paged | `GET /me/drive/sharedWithMe` | Microsoft is retiring this endpoint. |
| `ONE_DRIVE_GET_ITEM_PERMISSIONS` | read, paged | `GET {drive}/items/{id}/permissions` | |
| `ONE_DRIVE_ONEDRIVE_UPLOAD_FILE` | write, destructive | `PUT …:/{name}:/content`; an upload session above 4 MB | `content` is text, or base64 with `encoding='base64'`. `folder` is a path or a folder ID. `conflict_behavior='replace'` overwrites the existing file. An upload session that fails part-way is cancelled, not resumed; call the tool again. |
| `ONE_DRIVE_ONEDRIVE_CREATE_FOLDER` | write | `POST {parent}/children` | `parent_folder` is a path or a folder ID. On a name clash the new folder is renamed, or the call fails. |
| `ONE_DRIVE_MOVE_ITEM` | write | `PATCH {drive}/items/{itemId}` | Uses camelCase arguments, as Composio does: `itemId`, `parentReference: {id}`. |
| `ONE_DRIVE_COPY_ITEM` | write, destructive | `POST {drive}/items/{id}/copy` + the async monitor | Waits up to 10 s for `completed` (with `resourceId`); otherwise returns `inProgress`. `replace` overwrites the destination. |
| `ONE_DRIVE_UPDATE_DRIVE_ITEM_METADATA` | write | `PATCH {drive}/items/{id}` | Renames an item, changes its description, or moves it. Takes `if_match`. |
| `ONE_DRIVE_DELETE_ITEM` | write, destructive | `DELETE {drive}/items/{id}` | The item goes to the recycle bin. Takes `if_match`. |
| `ONE_DRIVE_CREATE_LINK` | write | `POST {drive}/items/{id}/createLink` | `type`: `view`, `edit` or `embed`. `scope`: `anonymous`, `organization` or `users` (`users` needs `recipients`). |

**Paged tools** return one page per call, as `{"has_more", "next_page_token", "items": [...]}`. The hints come first. To get the next page, pass `next_page_token` back unchanged as `page_token`, together with the same other arguments. A page token is the Graph nextLink's path for the same kind of listing under a drive root (`/me/drive/…` or `/drives/{id}/…`). Any other token is refused before anything is sent, including `/me/messages`, `/users/…`, `/sites/…` and `/groups/…` paths and another listing's token. The check runs on the percent-decoded path, so encoded traversal (`%2e%2e`, `%2F`, `%252e`) and any `#fragment` are refused too. Requests always go to the Graph base URL. If Graph returns a nextLink the tool would refuse (rare; e.g. a listing addressed through `user_id`), the result has `has_more: true` and `next_page_token: null`.

**What the results leave out.** All `@`-annotations are stripped from results, including `@microsoft.graph.downloadUrl`, which is a pre-authenticated link to the file's content. Upload-session URLs and copy-monitor URLs are pre-authenticated too: they are called without the bearer token and never returned.

**Where the toolkit differs from Composio:**
- `ONE_DRIVE_ONEDRIVE_UPLOAD_FILE` takes `file_name` and `content` (plus `encoding`). Composio's `file` object points at Composio's own storage. `source_url` is not supported because the server fetching arbitrary URLs would be an SSRF risk.
- `ONE_DRIVE_DOWNLOAD_FILE.file_name` and `ONE_DRIVE_GET_DRIVE.drive_id` are optional. If omitted, they default to the item's own name and to the user's drive.
- `ONE_DRIVE_ONEDRIVE_CREATE_FOLDER` does not offer `conflict_behavior='replace'`, because that would replace an existing folder and its contents.
- Composio arguments that are not listed are ignored.

### Errors

A failed call returns `ToolResult(successful=False)`. Its `error` reads `"<status>: <Graph code>: <message> — <what to do>"`, its `data` is `{"status": <HTTP status>}`, and it carries a canonical `error_code`:

| Graph answer | `error_code` | Notes |
|-------------|--------------|-------|
| 401 (`InvalidAuthenticationToken`) | `unauthorized` | The host should refresh once, then ask the user to reconnect. |
| 403 | `forbidden` | Consent is missing, or the user has no access to the item. |
| 404 / 410 | `not_found` | Item IDs go stale after a move or delete. |
| 400 / 422, refused arguments | `invalid_arguments` | Includes `.`/`..` path segments, foreign page tokens, folders passed to download, and downloads over 100 MiB. |
| 408 / 504, client timeout, spent budget, a download that stalls | `timeout` | |
| 409 / 412 / 423 | `conflict` | A name clash, a stale eTag, or a locked item. |
| 429, 509, or 503 with `Retry-After` | `rate_limited` | Sets `retry_after` in whole seconds, capped at 3600. The call is retried up to 2 times while the total wait stays within 60 s and each wait leaves `min(request timeout, 2 s)` of the time budget for the retried request; otherwise it fails at once. A `Retry-After` that is not a finite, non-negative number (`inf`, `1e400`, `nan`, `-5`, a date) counts as absent: 5 s for 429/509, and a 503 is then an outage. |
| 5xx, network errors | `upstream_error` | A GET is retried on 502/503/504. |

### Connection (OAuth)

`OneDriveToolkit.oauth` is `MICROSOFT_OAUTH`. It describes the Microsoft identity platform v2.0 authorization-code flow, with **PKCE (S256) required**. Its default scopes are `offline_access User.Read Files.ReadWrite.All`. Access tokens expire after about an hour, so `token_expires` is set, and every refresh returns a new refresh token, so `rotates_refresh_token` is set. Persist the new refresh token each time, and serialize refreshes for each connection.

The URLs contain a `{tenant}` placeholder: `https://login.microsoftonline.com/{tenant}/oauth2/v2.0/authorize|token`.

- **pipeAAS** fills the placeholder from the `tenant` key of the OAuth app's secret blob. The default is `common`.
- **In Python**, pass the tenant to the constructor, as in `OneDriveToolkit(tenant="organizations")`, or use `microsoft_oauth("<tenant>")` to get a filled spec. If nothing fills the placeholder, the toolkit's flow methods use `common`.
- **Valid tenant values** are `common` (any account), `organizations` (work and school accounts only), `consumers` (personal accounts only), a tenant ID (GUID), or a verified domain.

**Router metadata.** `OneDriveToolkit.card()` is the connector's card for a router —
category `files`, plus `keywords` and `examples` phrased as users ask
("my recent Excel files", "share the budget workbook with a view-only link"). Each tool's `required_scopes` and `risk` (`read` / `write` /
`destructive`, from its flags) are on its `ToolSpec`. Given the connection's
granted scopes in `config["granted_scopes"]` (`OAuthToken.granted_scopes`),
`available_tools` lists only the tools they cover and `call_tool` refuses the
rest as `insufficient_scope` before any request.
Read tools need `Files.Read.All` and write tools `Files.ReadWrite.All` (the
`.All` forms: the tools reach items shared with the user and other drives).
Graph permissions compare by short name and case-insensitively — Microsoft's
token responses may spell them `https://graph.microsoft.com/files.read.all` —
and `Files.ReadWrite.All` covers the read permissions, so the default consent
covers every tool.

**Disconnecting:** `revoke_token` always returns `False` and sends nothing. The Microsoft identity platform has no endpoint that revokes one app's delegated tokens, so the host forgets the token pair and the access token lapses within about an hour. `revokeSignInSessions` is deliberately not called, because it signs the user out of every app on every device. Users can withdraw consent at <https://myapps.microsoft.com> (work and school accounts) or <https://account.live.com/consent/Manage> (personal accounts).

#### Azure app registration

1. In the [Microsoft Entra admin center](https://entra.microsoft.com), go to **App registrations → New registration**.
2. **Supported account types:** choose *Accounts in any organizational directory and personal Microsoft accounts* to use tenant `common`. To serve one organization only, choose *single tenant* and set that tenant's ID as `tenant`.
3. **Redirect URI:** choose the *Web* platform and enter the host's callback, `{public_base_url}/v1/connections/callback` for pipeAAS. The *Web* platform is required because the host is a confidential client; don't register it as *SPA*.
4. **Certificates & secrets → New client secret.** Store the value as the app's `client_secret`, and the **Application (client) ID** as `client_id`. Secrets expire, so rotate them before they do.
5. **API permissions → Microsoft Graph → Delegated:** add `offline_access`, `User.Read` and `Files.ReadWrite.All`. Users can normally consent to these themselves. Some tenants require admin consent, which you grant with **Grant admin consent**. For read-only use, `Files.Read.All` is enough; pass it as the app's `scopes`.

## Source — pull files from OneDrive

```python
from aisquare.pipe import Pipeline
from aisquare.pipe.core.envelope import PullParams

pipeline = Pipeline("onedrive-source", "mock-sink")
result = pipeline.run(source_config=config, sink_config={})

params = PullParams(params={
    "path": "/Documents/Reports",
    "extensions": [".pdf", ".docx"],
    "recursive": True,          # follows subfolders on every page of a listing
    "limit": 50,
    "stream_threshold": 50 * 1024 * 1024,  # stream files > 50MB
})
```

## Sink — push files to OneDrive

```python
from aisquare.pipe.core.envelope import PushParams

params = PushParams(params={
    "target_path": "/Backups/2025",
    "conflict": "replace",   # or "rename" (default), "fail"
})
```

### Source/sink authentication

```python
config = {"access_token": "eyJ0eXAi..."}                     # delegated token
config = {"client_id": "...", "client_secret": "...", "tenant_id": "..."}  # app-only
```

App-only credentials need the **Files.ReadWrite.All** *application* permission. The source and sink use `/me/drive`, which needs a delegated token, so use an access token with them.

| Property | Source | Sink |
|----------|--------|------|
| Name | `onedrive-source` | `onedrive-sink` |
| Types | `*/*` (any file) | `*/*` (any file) |
| Streaming | Yes (configurable threshold) | Yes (auto-chunked) |
| Max file size | — | 250 GB |
| Rate limit | 600 req/min | — |
| Upload threshold | — | 4 MB (simple) / chunked above |
| Chunk size | — | 10 MB (32 × 320 KiB) |

## Running tests

```bash
pytest -q                                  # hermetic: a socket guard fails any test that opens a socket
ONEDRIVE_ACCESS_TOKEN=... pytest -m live   # read-only checks against Graph; skipped without the token
```
