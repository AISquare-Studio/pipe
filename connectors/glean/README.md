# Glean for aisquare.pipe

Read-only access to a customer's [Glean](https://www.glean.com/) index over
Glean's Client API. Composio has no Glean toolkit, so this connector is native.

- **Toolkit `glean`**: `GLEAN_SEARCH` (the primary tool) and `GLEAN_GET_DOCUMENT`.
- **Source `glean-source`**: a saved query or a list of document ids, turned into
  `text/plain` envelopes. Each envelope carries the document's full text and its
  update time, which serves as the version marker.

Nothing here writes to Glean.

## Install

```bash
pip install "aisquare-pipe[glean]"   # or [full] / [toolkits]
```

The connector ships inside the `aisquare-pipe` distribution. Its HTTP goes
through `requests`, which core pipe already depends on, so the `[glean]` extra
adds no packages.

## Auth: what the customer sets up

Glean issues the token. There is no OAuth here. A host such as pipe-as-service
treats the connector as a **per-tenant credential (FIRST_PARTY)**: the toolkit
declares no `OAuthSpec`. The credential comes from the tenant's server-side
secret only. Callers cannot set the host, the token or the user Glean reads as.

### 1. Find the host

The host is the customer's Client API host: `<instance>-be.glean.com`, for
example `acme-be.glean.com`. It must match `^[a-z0-9-]+-be\.glean\.com$`.

If the customer puts its own domain in front of Glean, list that exact host in
the secret's `allowed_hosts`. Any other host is refused before a request is
sent, and redirects are never followed.

### 2. Create a Client API token (Glean admin)

1. In Glean, open **Admin console → Platform → Token management → Client tab**
   (`https://app.glean.com/admin/platform/tokenManagement?tab=client`).
2. Click **Add New Token**.
3. Fill in the form:
   - **Description**: e.g. `AISquare policy reader`.
   - **Permissions**: choose one of:
     - **USER** (recommended): the token reads exactly what one Glean user
       can see. Use a dedicated reader account that can see the policy
       documents, and set **User Email** to that account. Super Admins can
       create USER tokens for anyone. Admins and API Token Creators can create
       them only for themselves.
     - **GLOBAL**: only Super Admins can create these. Every call must say
       which user to read as. The service sends `X-Glean-ActAs: <act_as>` on
       every call; the `act_as` value comes from the secret. Rate limits count
       against that user.
   - **Scopes**: `SEARCH` (search) and `DOCUMENTS` (reading a document).
   - **Expires**: optional. Put the expiry date in the runbook.
4. Copy the token secret. Glean shows it only once, and its permissions and
   scopes cannot be changed later.

A Client API token cannot be used for Glean's Indexing API, and the reverse is
also true.

### 3. Store the secret (operator)

The secret is one JSON object, read by pipe-as-service as the `glean` blob for
the tenant:

| Secrets backend | Name |
|---|---|
| AWS Secrets Manager | `<PIPE_SERVICE_SECRETS_PREFIX>/tenants/<tenant>/glean` |
| env (local) | `PIPE_SECRET__<TENANT>__GLEAN__JSON` |

USER token:

```json
{"host": "acme-be.glean.com", "token": "<Glean Client API token>", "token_kind": "user"}
```

GLOBAL token:

```json
{
  "host": "acme-be.glean.com",
  "token": "<Glean Client API token>",
  "token_kind": "global",
  "act_as": "policy-reader@acme.com"
}
```

The keys:

| Key | Required | Meaning |
|---|---|---|
| `host` | yes | `<instance>-be.glean.com`, or an entry of `allowed_hosts`. A bare hostname: no scheme, port or path. |
| `token` | yes | The Glean-issued token. |
| `token_kind` | no (`user`) | `user` or `global`. |
| `act_as` | only with `global` | The email Glean reads as. It is refused with a `user` token. |
| `allowed_hosts` | no | Extra hosts allowed for `host`, exact hostnames. |

If the secret is missing or malformed, every tool fails with `not_connected`
("Glean is not configured: …") and the source raises `GleanNotConfigured`, both
before any request. The reason never contains the token.

## Toolkit

| Tool | Arguments | Returns |
|---|---|---|
| `GLEAN_SEARCH` (primary) | `query` (required), `page_size` 1-50 (10), `cursor`, `datasources` (up to 20, e.g. `confluence`, `gdrive`) | `results[]` with `document_id`, `title`, `url`, `datasource`, `doc_type`, `snippets`, `update_time`, `create_time`, `author`; plus `has_more` and `next_cursor` |
| `GLEAN_GET_DOCUMENT` | exactly one of `document_id` and `url`; `max_chars` 1-200000 (20000) | the same fields plus `mime_type`, `text`, `truncated`, `text_chars` |

Both tool schemas reject unknown arguments (`additionalProperties: false`), so
no argument can name a host or a credential.

How Glean failures map to error codes:

| Glean | Error code |
|---|---|
| 401 | `unauthorized`: the token is bad or expired, or `act_as` is not a user |
| 403 | `forbidden`: a scope is missing, or the user cannot see the document |
| 400 or 422 | `invalid_arguments`. Glean's "Required header missing: X-Glean-ActAs" means a GLOBAL token is configured as `user`. |
| 429 | `rate_limited`, with `retry_after` when Glean sends it |
| 408 or a client timeout | `timeout` |
| 5xx, a redirect, or a body that is not JSON | `upstream_error` |

A document that Glean answers with `{"error": …}` is `not_found`.

## Source

```python
from aisquare.pipe import PullParams
from aisquare_pipe_glean import GleanSource

for env in GleanSource().pull(blob, PullParams({"query": "policy", "datasources": ["confluence"]})):
    env.metadata["update_time"]   # the version marker
```

Pull params:
- `query` or `document_ids`: exactly one. A query is paged with Glean's cursor
  and duplicate ids are dropped.
- `datasources`: only together with `query`.
- `max_documents`: 1-1000, default 100.

Documents are read 20 per `getdocuments` call.

Each envelope's `metadata` holds `glean_document_id`, `title`, `url`,
`datasource`, `mime_type`, `create_time`, `author` and `update_time` (Glean's
`metadata.updateTime`). To re-read a document only when it changes, keep
`update_time` and compare it on the next pull.

A param that names a credential key (`host`, `token`, `token_kind`, `act_as`,
`allowed_hosts`) is refused, and so is any unknown param. Documents Glean
cannot read for this user are skipped. They are counted in one INFO line, with
no ids or text.

## Limits and follow-ups

- **Rate limits**, from Glean's published limits: `/search` allows 5 requests
  per second per user token, and the other endpoints 30 per second.
- **Follow-up (size L): per-person OAuth.** OAuth through Glean's own
  authorization server, or through the customer's IdP with
  `X-Glean-Auth-Type: OAUTH`, has endpoints that differ per tenant. pipe's
  OAuth core assumes fixed endpoints, so it is out of scope here.
- **Map keys not relied on.** The key of `getdocuments`' `documents` map is not
  pinned down by Glean's models, so the code reads each entry's own `id`.

## API references

- Search: <https://developers.glean.com/api/client-api/search/search>
- Read documents: <https://developers.glean.com/api/client-api/documents/getdocuments>
- Authentication: <https://developers.glean.com/api-info/client/authentication/overview>
- Glean-issued tokens: <https://developers.glean.com/api-info/client/authentication/glean-issued>
- Rate limits: <https://developers.glean.com/get-started/rate-limits>
- Request and response models: `gleanwork/api-client-python` (`SearchRequest`,
  `SearchResponse`, `GetDocumentsRequest`, `GetDocumentsResponse`, `Document`).

## Tests

```bash
pip install -e ".[full,dev]"           # from the repo root
cd connectors/glean && pytest tests     # hermetic: sockets are blocked, Glean is faked
```
