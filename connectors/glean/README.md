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

**What pipe-as-service runs.** Only `glean-source` runs through
pipe-as-service today (its `main`): its credential resolver classifies
`glean-source` as a per-tenant credential (FIRST_PARTY) and reads the tenant's
`glean` blob below. pipe-as-service does not serve the `GLEAN_SEARCH` and
`GLEAN_GET_DOCUMENT` tools yet: its catalogue marks a toolkit that has no
registry card and no `OAuthSpec` as unsupported. With credential-driven
routing (pipe-as-service #30), the `glean` toolkit is served too, and it reads
the same `glean` secret as `glean-source` (`TENANT_BLOBS` in
`credentials/catalog.py`). So a tenant stores the blob once. The tools also
work in any host that loads the toolkit directly with the blob as its config.

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
5. Make one token per AISquare environment (one for production, one for
   staging), and never use the production token in staging. Hand each token to
   AISquare devops only through AISquare's team vault (1Password), never by
   email, chat or a ticket.

A Client API token cannot be used for Glean's Indexing API, and the reverse is
also true.

### 3. The secret's value

The secret is one JSON object, read by pipe-as-service as the `glean` blob for
the tenant. On the env secrets backend, which every environment runs today,
it is the env var `PIPE_SECRET__<TENANT>__GLEAN__JSON`. On the aws backend, it
is `<PIPE_SERVICE_SECRETS_PREFIX>/tenants/<tenant>/glean`. How it gets there:
[4. Deliver the secret](#4-deliver-the-secret-aisquare-devops).

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

### 4. Deliver the secret (AISquare devops)

**What has to be true first:** pipe-as-service's image has a pipe release
that contains `glean` (`aisquare-pipe[toolkits]`). The toolkit also needs
pipe-as-service #30 (above).

**Where pipe-service runs today.** Production runs on ECS only.
aisquare-releases has no prod pipe-service (`environments/prod/services/`
holds only explainability-gateway and praxis), and aisquare-infra has no
`prod/prod/pipe-service/*` secret. So a customer's production tenant follows
the ECS path. `stg` runs on ECS and on EKS until the cutover, and `dev` runs on
EKS only. While both `stg`s run, turn the tenant on in both (at the least, in
the one the tenant's caller uses): the other one fails the tenant's calls as
not configured. Use one Glean token per environment (section 2, step 5). On
EKS the value also sits in a GitHub environment secret of aisquare-infra
(below), which more people and workflows can reach than an ECS secret.

**Turning Glean on for a tenant is a deploy, today.** Every environment runs
the env secrets backend (`PIPE_SERVICE_SECRETS_BACKEND=env`: EKS
`deploy/values.yaml` and both ECS task definitions in pipe-as-service). The
blob reaches the service only as the env var
`PIPE_SECRET__<TENANT>__GLEAN__JSON`, read when a pod or task starts. Store
the value first (step 3, saved as a local `blob.json`), then add the entry
that reads it.

`<TENANT>` is the tenant id in upper case. Every run of characters other than
A-Z and 0-9 becomes one `_`, and any `_` at either end is dropped
(`acme-corp.eu` gives `PIPE_SECRET__ACME_CORP_EU__GLEAN__JSON`).

- **ECS** (production, and `stg` until the cutover):
  1. Create the secret in the task definition's region: prod `us-east-2`, stg
     `us-east-1`. The task execution role reads only `ais-pipe-service/<env>/*`
     in its own region, so a secret anywhere else stops the task from
     starting:
     `aws secretsmanager create-secret --region <region> --name ais-pipe-service/<env>/glean-<tenant> --secret-string file://blob.json`
  2. Add an entry to the task definition's `secrets:`
     (`deploy/ecs/<env>/task-definition-pipe-service.json` in pipe-as-service).
     Use the same ARN form as the entries already there:

     ```json
     {
       "name": "PIPE_SECRET__<TENANT>__GLEAN__JSON",
       "valueFrom": "arn:aws:secretsmanager:<region>:<account>:secret:ais-pipe-service/<env>/glean-<tenant>"
     }
     ```

  3. Merge it, then run pipe-as-service's `deploy` workflow for that
     environment and region:
     `gh workflow run deploy.yml --repo AISquare-Studio/pipe-as-service -f environment=<env> -f region=<region>`.
     The new task-definition revision is the deploy. Terraform ignores
     `container_definitions`, so this workflow owns them. The workflow builds
     and pushes the image from the head of the branch it runs on (`main`), so
     this deploy also ships everything else merged to pipe-as-service `main`
     since the last one. Check what that is first, above all in production.
- **EKS** (`stg`, `dev`): never put the value into Secrets Manager by hand.
  aisquare-infra creates the secret from
  `live/nonprod/data/supplied-secrets.json`. Its `secrets-nonprod` workflow
  fills the secret from the GitHub environment `nonprod-secrets`, when run by
  hand and after every `apply-nonprod`. So a value edited in Secrets Manager is
  overwritten with the GitHub one at the next sync. And if a listed secret's
  GitHub value is missing, the sync stops for EVERY secret.

  `<GH_SECRET>` below is the environment's own GitHub secret, because each
  environment has its own token: `GLEAN_<TENANT>_JSON` for `stg`, and
  `DEV_GLEAN_<TENANT>_JSON` for `dev` (like `DEV_INTERNAL_AUTH_TOKEN`). Use that
  one name in every step and command for the environment, so `dev` never reads
  or writes stg's secret.
  1. Store the value in GitHub:
     `gh secret set <GH_SECRET> --env nonprod-secrets --repo AISquare-Studio/aisquare-infra < blob.json`.
  2. In aisquare-infra, open one PR that adds the entry to
     `live/nonprod/data/supplied-secrets.json`:

     ```json
     {
       "nonprod/<env>/pipe-service/glean-<tenant>": {
         "github_secret": "<GH_SECRET>",
         "description": "PIPE_SECRET__<TENANT>__GLEAN__JSON of the <env> pipe service: the Glean token blob of <tenant>"
       }
     }
     ```

     The same PR adds its line to the `env` of the sync step in
     `.github/workflows/secrets-nonprod.yml`:
     `<GH_SECRET>: ${{ secrets.<GH_SECRET> }}`. Merge only after
     step 1, so the sync can't fail on it. The merge runs `apply-nonprod`,
     which creates the secret and then runs `secrets-nonprod` to fill it. Check
     that both ran green.
  3. In aisquare-releases, add to
     `environments/<env>/services/pipe-service/release.yaml`. That file is
     written by people. Never edit `values.yaml`, which the pipe-as-service
     build workflow overwrites:

     ```yaml
     secretEnv:
       PIPE_SECRET__<TENANT>__GLEAN__JSON: { from: pipe-service/glean-<tenant> }
     ```

     Helm merges it with the `secretEnv` of `values.yaml`. A code owner merging
     that PR is the deploy. Argo CD syncs the ExternalSecret `pipe` (key
     `<secrets.prefix>/pipe-service/glean-<tenant>`) before the Deployment. One
     ExternalSecret carries every `secretEnv` entry, so a secret with no value
     yet fails that ExternalSecret and holds the release.

After storing the value, delete `blob.json` and the vault item.

**Never switch the secrets backend for Glean.** `PIPE_SERVICE_SECRETS_BACKEND=aws`
moves EVERY secret the service reads, the shared Composio key included, and a
missing secret reads as no key. Flipping the backend "for Glean" takes Composio
down for every connector in that environment. Moving to the aws backend is a
whole-service migration (pipe-as-service `DEPLOYMENT.md`).

**Rotation**, at least before the token expires:
1. The Glean admin creates a new token (step 2) and hands it over. A token's
   permissions and scopes can't be changed, so a new scope also means a new
   token.
2. Replace `token` in the value. Then restart the service, which reads the
   value only when a pod or task starts:
   - ECS: `aws secretsmanager put-secret-value --region <region> --secret-id ais-pipe-service/<env>/glean-<tenant> --secret-string file://blob.json`,
     then `aws ecs update-service --region <region> --cluster ais-studio-ecs-cluster-<env> --service ais-studio-pipe-service-<env> --force-new-deployment`.
     Wait until the old tasks are gone.
   - EKS, in this order (`<GH_SECRET>` as in "Deliver the secret", per
     environment):
     1. `gh secret set <GH_SECRET> --env nonprod-secrets --repo AISquare-Studio/aisquare-infra < blob.json`.
        Never edit the value in Secrets Manager: the next sync puts the
        GitHub value back, and a pod that starts after that reads the old
        token.
     2. Run the sync and wait for it to finish.
        `gh workflow run secrets-nonprod.yml --repo AISquare-Studio/aisquare-infra`
        returns as soon as the run is queued. Find the run with
        `gh run list --workflow secrets-nonprod.yml --repo AISquare-Studio/aisquare-infra --limit 1 --json databaseId,createdAt`
        (its `createdAt` must be after your dispatch; a new run can take a
        few seconds to appear), then
        `gh run watch <run-id> --repo AISquare-Studio/aisquare-infra --exit-status`.
        It must end green, and
        `gh run view <run-id> --repo AISquare-Studio/aisquare-infra --log | grep pipe-service/glean-<tenant>`
        must show `nonprod/<env>/pipe-service/glean-<tenant>: updated`.
     3. Only then refresh the ExternalSecret. Its refresh interval is 1 h, the
        chart default, which pipe-service doesn't override. Or force it now
        with
        `kubectl -n <env> annotate externalsecret pipe force-sync=$(date +%s) --overwrite`.
        Then check that
        `kubectl -n <env> get externalsecret pipe -o jsonpath='{.status.refreshTime}'`
        is later than the run's end
        (`gh run view <run-id> --repo AISquare-Studio/aisquare-infra --json updatedAt`)
        and that `kubectl -n <env> get externalsecret pipe` shows
        `SecretSynced`. A refresh that ran before the sync finished copied the
        old token, and it still shows `SecretSynced`.
     4. Restart the Deployment:
        `kubectl -n <env> rollout restart deployment/pipe-service`, then
        `kubectl -n <env> rollout status deployment/pipe-service`. The chart
        has no checksum annotation or reloader, so pods keep the old value
        until they restart.
3. Only when every pod or task runs with the new token does the Glean admin
   revoke the old one. Revoking it earlier breaks calls on pods that still
   hold it.

**Offboarding** (AISquare's copy):
- ECS: remove the task definition's `secrets:` entry and deploy (as above).
  Then delete the secret:
  `aws secretsmanager delete-secret --region <region> --secret-id ais-pipe-service/<env>/glean-<tenant>`.
  Keep that order, because a task whose secret is gone does not start. The
  name stays taken for the recovery window (30 days by default), so to turn
  the tenant back on within it, restore the secret
  (`aws secretsmanager restore-secret --region <region> --secret-id ais-pipe-service/<env>/glean-<tenant>`)
  and `put-secret-value` the new blob instead of `create-secret`.
- EKS:
  1. Remove the `secretEnv` entry from `release.yaml` (a deploy). Do this
     first: while the entry is listed, a deleted secret fails the
     ExternalSecret.
  2. In aisquare-infra, open one PR that removes the `supplied-secrets.json`
     entry and the `secrets-nonprod.yml` env line, and the tenant's import
     block in `live/nonprod/data/imports.tf` if one is still there (below).
     Its `apply-nonprod` deletes the Secrets Manager secret, with a 7-day
     recovery window.
  3. After that merge, run
     `gh secret delete <GH_SECRET> --env nonprod-secrets --repo AISquare-Studio/aisquare-infra`.
     If you delete it earlier, while the entry is still listed, every sync
     stops.

  **Never delete the Secrets Manager secret by hand.** `delete-secret` keeps
  the name for its recovery window (30 days by default), and Terraform's AWS
  provider reads a secret that is pending deletion as gone. Until the window
  ends, while the entry is listed:
  - the next `apply-nonprod` of `live/nonprod/data` plans to create the
    secret, and `CreateSecret` fails ("already scheduled for deletion"). The
    apply fails, and its `secrets-nonprod` job is skipped;
  - `secrets-nonprod` fails on the secret and stops there, so no secret
    listed after it is written, other services' included;
  - the ExternalSecret `pipe` fails, which holds every later pipe-service
    release in that environment.

  To undo it:
  `aws secretsmanager restore-secret --region us-east-1 --secret-id nonprod/<env>/pipe-service/glean-<tenant>`.
  The value comes back with it. If an `apply-nonprod` ran on
  `live/nonprod/data` in between, its failure also dropped the secret from
  Terraform's state, so import it (below) as well. Then run `secrets-nonprod`,
  wait for it and refresh the ExternalSecret, as the EKS rotation above does.

  **Turning the tenant back on within 7 days** of the offboarding apply fails
  the same way: the name is still pending deletion. Wait out the window, or
  restore the secret (above) first. Only then open the PR that re-adds the
  entry: the provider cannot import a secret that is still pending deletion,
  so a PR opened before the restore fails. In that PR, add an import block to
  `live/nonprod/data/imports.tf` like the ones already there:

  ```hcl
  import {
    to = aws_secretsmanager_secret.supplied["nonprod/<env>/pipe-service/glean-<tenant>"]
    id = "<ARN>"
  }
  ```

  `<ARN>` comes from
  `aws secretsmanager describe-secret --region us-east-1 --secret-id nonprod/<env>/pipe-service/glean-<tenant> --query ARN --output text`.
  The restored secret holds the old value until the sync writes the GitHub
  one.

  **Remove the import block once the `apply-nonprod` that imported it is
  green**, in a follow-up PR (the secret stays in Terraform's state). A block
  left behind outlives the entry: after the next offboarding removes the
  entry, every plan of `live/nonprod/data` fails ("Configuration for import
  target does not exist"), so its `apply-nonprod` fails and the sync is
  skipped. The two blocks already there are safe only while their entries
  stay listed.
- The Glean admin revokes the token (Token management, Client tab). Deleting
  our copy does not revoke it.

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

How a pull fails (pipe-as-service answers by the exception's type; neither
message carries Glean's own error text, which can quote the query, because
hosts log these):

- **422**, a `ValueError`: the secret is missing or malformed
  (`GleanNotConfigured`); a bad param; and what Glean refused that the caller
  can fix, as `GleanRequestError` (with its `status`: 400, 404, 422);
- **502** `upstream_error`, a `PipelineError` with `retry_after` when Glean
  sent `Retry-After` (a 429): a refused token or scope, throttling, a timeout,
  a 5xx or the network.

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
