# aisquare-pipe[glean]

Glean for [aisquare.pipe](../../README.md): read-only access to a customer's
[Glean](https://www.glean.com/) index over Glean's Client API. Composio has no
Glean toolkit, so this connector is native.

- **Toolkit `glean`**: `GLEAN_SEARCH` (the primary tool) and `GLEAN_GET_DOCUMENT`.
- **Source `glean-source`**: a saved query or a list of document ids, turned into
  `text/plain` envelopes. Each envelope carries the document's full text and a
  version marker: its update time, or a hash of the text when Glean sends no
  update time.

Nothing here writes to Glean.

## Install

This connector ships inside the `aisquare-pipe` distribution. Its third-party dependencies are the `[glean]` extra:

```bash
pip install "aisquare-pipe[glean]"   # or "aisquare-pipe[full]": every connector
```

For development, from the repo root (one editable install covers the framework and every connector):

```bash
pip install -e ".[full,dev]"
cd connectors/glean && pytest tests
```

The `[glean]` extra adds no packages: HTTP goes through `requests`, which core
pipe already depends on.

## Auth: what the customer sets up

Glean issues the token. There is no OAuth here: the toolkit declares no
`OAuthSpec`, and the credential is a per-tenant blob that comes from the
tenant's server-side secret only. Callers cannot set the host, the token or
the user Glean reads as.

**What pipe-as-service runs today.** Only `glean-source` runs through
pipe-as-service: its credential resolver classifies `glean-source` as a
per-tenant credential (FIRST_PARTY) and reads the tenant's `glean` blob below.
pipe-as-service does not serve the `GLEAN_SEARCH` and `GLEAN_GET_DOCUMENT`
tools yet: its catalogue marks a toolkit that has no registry card and no
`OAuthSpec` as unsupported. The tools work in any host that loads the toolkit
directly with the blob as its config. Serving them from pipe-as-service waits
on its native-toolkit routing work.

### 1. Find the host

The host is the customer's Client API host: `<instance>-be.glean.com`, for
example `acme-be.glean.com`. It must match `^[a-z0-9-]+-be\.glean\.com$`.

If the customer puts its own domain in front of Glean, list that exact host in
the secret's `allowed_hosts`. Any other host is refused before a request is
sent, and redirects are never followed. IP addresses are refused, both as
`host` and in `allowed_hosts`, in every form a resolver reads as one (for
example `169.254.169.254`, `127.1` or `0xa9.0xfe.0xa9.0xfe`).

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
       which user to read as. The connector sends `X-Glean-ActAs: <act_as>` on
       every call; the `act_as` value comes from the secret. Rate limits count
       against that user.

       **Warning:** a GLOBAL token can read as *any* user in the Glean
       tenant, so it is a tenant-wide secret: whoever holds it can read
       everything any employee can. The connector always reads as the one
       `act_as` fixed in the secret (no caller can change it), but the token
       itself is not limited to that user. Prefer a USER token, and if you
       use a GLOBAL one, store it like any other tenant-wide admin credential.
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
| `host` | yes | `<instance>-be.glean.com`, or an entry of `allowed_hosts`. A bare hostname: no scheme, port, path or IP address. |
| `token` | yes | The Glean-issued token: printable ASCII, no spaces. |
| `token_kind` | no (`user`) | `user` or `global`. |
| `act_as` | only with `global` | The email Glean reads as, in ASCII. It is fixed here: no caller can change it. It is refused with a `user` token. |
| `allowed_hosts` | no | Extra hosts allowed for `host`, exact hostnames (no IP addresses). |

The token and `act_as` are sent as HTTP headers, so both must be ASCII. A
value that is not is refused as not configured.

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
| 408, 504 or a client timeout | `timeout` |
| any other 5xx, a redirect, or a body that is not JSON | `upstream_error` |

A document that Glean answers with `{"error": …}` is `forbidden` when the
error mentions permission, and `not_found` otherwise.

## Source

```python
from aisquare.pipe import PullParams
from aisquare_pipe_glean import GleanSource

for env in GleanSource().pull(
    blob, PullParams({"query": "policy", "datasources": ["confluence"]})
):
    env.metadata.get("update_time") or env.metadata["content_sha256"]  # the marker
```

Pull params:
- `query` or `document_ids`: exactly one. A query is paged with Glean's cursor
  and duplicate ids are dropped.
- `datasources`: only together with `query`.
- `max_documents`: 1-1000, default 100.

Documents are read 20 per `getdocuments` call. A query follows at most 50
search pages. Glean can send short pages, so a pull can stop at that cap with
fewer than `max_documents` documents; when it does while Glean still has
results, the source logs one WARNING with the counts (no query, ids or text).

Each envelope's `metadata` holds `glean_document_id`, `title`, `url`,
`datasource`, `mime_type`, `create_time`, `author` and exactly one version
marker:

- `update_time`: Glean's `metadata.updateTime`, when Glean sends one;
- `content_sha256`: otherwise, the hex SHA-256 of the envelope's `data`
  (UTF-8). The source counts these documents in one INFO line.

To re-read a document only when it changes, keep its marker and compare it on
the next pull.

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

Every test is hermetic: an autouse guard fails any test that opens a socket,
and Glean is faked. After the dev install above:

```bash
cd connectors/glean && pytest tests
```
