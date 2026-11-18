# aisquare_pipe_gcs: Google Cloud Storage (read-only)

A toolkit (`gcs`) and a source (`gcs-source`) that read the files in ONE Google Cloud Storage bucket,
optionally under one prefix, with the version markers a caller needs to re-read a file when it changes.
Composio has no GCS toolkit; this is the native connector (GoodRx Smart Connectors, item 3b).

Ships inside the `aisquare-pipe` distribution: `pip install "aisquare-pipe[gcs]"` (or `[full]`, or
`[toolkits]`). The extra adds one dependency, `google-auth>=2.20,<3`, which signs the service account's
key into short-lived tokens. Without the extra, `gcs` and `gcs-source` count as not installed.

## What it does

| Tool | What it returns |
|---|---|
| `GCS_GET_OBJECT` (primary) | One object by name: its bytes as a file (text up to 1 MiB also inline as `content`), `content_type`, `size` and the version markers. `include_content=false` reads the metadata only. `generation` reads an older version. `max_bytes` (default 25 MiB, at most 100 MiB) bounds the download. |
| `GCS_LIST_OBJECTS` | The objects under a prefix with their version markers, `page_size` up to 1000 and `page_token` paging. `folders=true` lists one level, with the sub-prefixes in `folders`. |

`gcs-source` pull params:
- `prefix` (a narrower prefix inside the configured one), or `object_names` (a list of at most `max_objects`
  names), not both.
- `start_offset` (with a prefix): list from this object name on. GCS lists names in lexicographic order, and
  this name is included.
- `modified_after` (RFC 3339).
- `max_objects` (default 100, at most 1000).
- `max_bytes` per object. A larger object is skipped and counted in one log line.

Each envelope carries the object's bytes and `metadata`: `bucket`, `object_name`, `filename`, `size`,
`content_type`, `generation`, `md5_hash`, `updated`, `etag`, `crc32c`, `time_created`, and `content_encoding`
when the stored bytes are gzip. "Folder" placeholders (names ending in `/`) are skipped.

**A pull that stops early says so.** It can stop at `max_objects`, or after 100 listing pages (100,000
names), for example when a `modified_after` matches little in a big prefix. Then:
- the LAST envelope's metadata carries `truncated` (`max_objects` or `max_list_pages`) and
  `next_start_offset`;
- if no envelope came out (every name listed was older than `modified_after`, or too large), the pull raises
  `GcsPullTruncated` (a `ValueError`) with `reason` and `next_start_offset`. Its message names the
  `start_offset` to continue with, and pipe-as-service passes it on (422 `bad_request`);
- a warning is logged either way. This connector's own logs never carry object names. The host may log the
  error's message, which names the `start_offset` (an object name): pipe-as-service's async worker logs a
  failed job's error text (`async_/worker.py`).

Pass `next_start_offset` as `start_offset` to continue, and repeat until a pull is not truncated. Nothing is
skipped and nothing is repeated. A caller that keeps `max(updated)` as its watermark must finish a truncated
pull first, or it misses changes.

**Version markers.**
- `generation` changes on every content write. Store it and re-read the object when it changes.
- `md5_hash` is the content's MD5 (absent for composite objects).
- `updated` is the last change time (metadata changes move it too).
- These, with `size` and `crc32c`, describe the object as STORED. For an object stored with
  `Content-Encoding: gzip` (`content_encoding: gzip`), a read returns the inflated bytes, so they won't
  match `size` or `md5_hash`. `max_bytes` counts the inflated bytes as they arrive.

Read-only by construction: the token's scope is `devstorage.read_only`, the service account only needs
`roles/storage.objectViewer`, and no tool writes.

## Security

- **The prefix is enforced by this connector, not by Google.** The key can read everything its IAM grant
  covers, through any client. The bucket, the prefix and the key come only from the tenant's server-side
  blob.
  - A tool argument or pull param can't name another bucket (the schemas reject `bucket` and
    `service_account_json`).
  - A name or prefix outside the configured prefix is refused before any request: `forbidden` from a tool,
    `ValueError` from a pull.
  - The configured prefix must end with `/`, so `policies/` never admits `policies-hr/`.
  - To have Google enforce the boundary too, see step 1 of the checklist.
- **Application Default Credentials are never used**: no `google.auth.default()`, no
  `GOOGLE_APPLICATION_CREDENTIALS`, no metadata server. A missing or malformed blob fails closed with
  "Google Cloud Storage is not configured: <what is wrong>" (`not_connected`) and makes no request. So does a
  key that can't sign (not an RSA service account key).
- The key's `token_uri` must be Google's token endpoint and its `universe_domain` `googleapis.com`, so the
  signed assertion only ever goes to Google. Every API call goes to `https://storage.googleapis.com`.
  Redirects are not followed. Errors and logs never carry the key, the assertion or a token.
- **Tokens:**
  - A token is minted with ONE POST to the token endpoint, with no hidden retries.
  - Tokens are cached in memory per key until five minutes before they expire.
  - A token Cloud Storage rejects (401) is dropped from the cache. A call whose cached token was rejected
    retries once with a new token.
- **Time budget.** A tool call shares the host's `timeout_seconds` across all of its requests: every request
  starts inside the budget and gets at most the time left (and at most 30 s). `GCS_GET_OBJECT` makes up to
  three requests (token, metadata, download), or four after a rejected cached token (the rejected one, a new
  token, then the retry and the download). The token POST counts too. A call with no time left fails
  `timeout` before it sends anything more. requests' timeout bounds each connect and each socket read, not a
  whole body, so a download also checks the deadline between 64 KiB chunks. A request already in flight can
  still end after the deadline, by up to its own timeout (a body trickling in within one chunk is not cut
  until that chunk ends). `gcs-source` has no budget: each request gets 30 s.

| Failure | `error_code` | Meaning |
|---|---|---|
| "Google Cloud Storage is not configured: ..." | `not_connected` | The secret is missing or malformed (the message says what). Fix the secret. |
| "Google did not issue a token ..." (`invalid_grant` ...), "Cloud Storage failed (401)" | `unauthorized` | Google refused the key: deleted, disabled or expired key, or a disabled service account. The customer makes a new key. |
| "... lacks roles/storage.objectViewer ..." | `forbidden` | The role binding is missing, or it is on another bucket or folder. |
| "... VPC Service Controls perimeter ..." | `forbidden` | The bucket's project is inside a perimeter with no ingress rule for the service account. |
| "... requester-pays buckets are not supported" / "... (CSEK) can't be read" | `invalid_arguments` | Not supported (see "Not supported"). |
| a timeout, "could not reach ...", "... token endpoint failed (5xx)", a 5xx | `timeout`, `upstream_error`, `rate_limited` | Google or the network. Retryable. The credential is fine. |
| "Google refused the token request's issue and expiry times: this host's clock is off ..." | `upstream_error` | Our clock is off (Google: "Check your iat and exp values"). Fix NTP on the host. The credential is fine. |

**Not supported:**
- Requester-pays buckets. No `userProject` is sent, so Google answers 400.
- Objects encrypted with a customer-supplied key (CSEK). CMEK (a Cloud KMS key on the bucket) works with
  nothing extra.
- Workload Identity Federation (see "Later").

## Registration checklist

### The customer: who can do it

The steps need one admin, or several, holding:
- `roles/iam.serviceAccountAdmin` (or `roles/iam.serviceAccountCreator`) on the project, to create the
  service account.
- `storage.buckets.setIamPolicy` on the bucket, for example `roles/storage.admin`, to bind the role. With a
  managed folder, `roles/storage.admin` also covers creating the folder, setting its policy and turning on
  uniform bucket-level access (`storage.buckets.update`).
- `roles/iam.serviceAccountKeyAdmin` on the project or the service account, to create the key.
- Only behind VPC Service Controls: `roles/accesscontextmanager.policyEditor` on the access policy, to add
  the ingress rule (below).

Check these organization settings first:
- **Key creation may be blocked.** In organizations created on or after 3 May 2024, Google enforces
  `iam.managed.disableServiceAccountKeyCreation` by default (older organizations may set the legacy
  constraint `iam.disableServiceAccountKeyCreation`). Step 4 then fails until an org-policy admin
  (`roles/orgpolicy.policyAdmin`) makes an exception for this project.
- **Keys may expire.** If `iam.serviceAccountKeyExpiryHours` is set, the key expires after that time and the
  connector goes `unauthorized`. Put the rotation (below) on a calendar ahead of the expiry.
- **VPC Service Controls.** If the bucket's project is inside a perimeter that restricts
  `storage.googleapis.com`, AISquare's calls come from outside it and get 403 "Request is prohibited by
  organization's policy". Add an ingress rule to the perimeter:

  ```yaml
  - ingressFrom:
      identities:
        - serviceAccount:aisquare-reader@<project>.iam.gserviceaccount.com
      sources:
        - accessLevel: "*"
    ingressTo:
      operations:
        - serviceName: storage.googleapis.com
          methodSelectors:
            - method: google.storage.objects.get
            - method: google.storage.objects.list
      resources:
        - projects/<project-number>
  ```

  Apply it with `gcloud access-context-manager perimeters update <perimeter> --policy=<policy-id>
  --set-ingress-policies=<file>`. That flag REPLACES the perimeter's ingress rules, so the file must also
  list the rules that exist today.

### The customer: steps

1. **Decide what the key may read.** The prefix (step 5) is enforced by AISquare's connector, not by Google.
   The key can read every object its grant covers, through any client. With a bucket-level grant, that is
   the whole bucket: if `hr/` sits next to `policies/`, the key can read `hr/` too. So grant only what
   AISquare may read:
   - **Recommended: a dedicated bucket** that holds only those documents. Sync them in on a schedule:
     `gcloud storage rsync gs://<source-bucket>/policies gs://<aisquare-bucket>/policies --recursive --delete-unmatched-destination-objects`.
     Without `--delete-unmatched-destination-objects`, a document you delete at the source, or move out of
     `policies/`, stays in the AISquare bucket and AISquare keeps reading it. With it, removals reach the
     AISquare bucket at the next sync. Then do step 3a on that bucket.
   - **Or a managed folder** (step 3b). It needs uniform bucket-level access. **We have not verified this
     option.** Our tests are hermetic, with no real Google project, so we have not confirmed that
     `GCS_LIST_OBJECTS` and `gcs-source` can list with `prefix=policies/` under a grant on the folder only.
     Before you rely on it, test it in your project as the service account:
     `gcloud storage ls gs://<bucket>/policies/ --impersonate-service-account=aisquare-reader@<project>.iam.gserviceaccount.com`
     and `gcloud storage cat gs://<bucket>/policies/<a file> --impersonate-service-account=...`. The tester
     needs `roles/iam.serviceAccountTokenCreator` on the service account. Also check that `gcloud storage
     ls gs://<bucket>/hr/` is refused.
   - **Either way, keep Object Versioning off on the bucket AISquare reads** (the dedicated bucket, or the
     managed folder's bucket). With versioning on, an overwritten or deleted document stays readable:
     `objectViewer` covers its noncurrent versions, and `GCS_GET_OBJECT` reads one by `generation`. If the
     bucket must keep versions, treat every version as readable by AISquare.
2. **Create a service account** for AISquare:
   `gcloud iam service-accounts create aisquare-reader --project=<project> --display-name="AISquare read-only (GCS)"`.
3. **Grant it `roles/storage.objectViewer`** on ONE bucket (3a) or ONE managed folder (3b), never on the
   project.
   - 3a: `gcloud storage buckets add-iam-policy-binding gs://<bucket> --member=serviceAccount:aisquare-reader@<project>.iam.gserviceaccount.com --role=roles/storage.objectViewer`
   - 3b: `gcloud storage managed-folders create gs://<bucket>/policies/`, then
     `gcloud storage managed-folders add-iam-policy-binding gs://<bucket>/policies/ --member=serviceAccount:aisquare-reader@<project>.iam.gserviceaccount.com --role=roles/storage.objectViewer`.
     The prefix in step 5 must then be exactly that folder, `policies/`.
   - **Encryption:**
     - CMEK (a Cloud KMS key on the bucket): nothing extra. The bucket's Cloud Storage service agent already
       holds the key role and decrypts for every reader. Do NOT grant the service account a KMS role, which
       would let the key's holder call Cloud KMS decrypt directly.
     - CSEK (customer-supplied keys): those objects can't be read.
   - Requester-pays buckets are not supported.
4. **Create a JSON key**:
   `gcloud iam service-accounts keys create key.json --iam-account=aisquare-reader@<project>.iam.gserviceaccount.com`.
   Make one key per AISquare environment (one for production, one for staging), and never use the production
   key in staging.
5. **Hand AISquare devops** key.json, the bucket name and, if the documents live under one folder, its prefix
   (it must end with `/`, for example `policies/`). Leaving the prefix out allows everything the grant
   covers.
   - Hand over key.json only through AISquare's team vault (1Password): devops invites your admin as a guest
     to a vault for this handoff, and you store key.json there as a document. Never by email, chat or a
     ticket.
   - Delete your local copy of key.json afterwards.

**Offboarding** (when AISquare should stop reading): remove the role binding from step 3, then delete the
key or the service account.
- 3a: `gcloud storage buckets remove-iam-policy-binding gs://<bucket> --member=serviceAccount:aisquare-reader@<project>.iam.gserviceaccount.com --role=roles/storage.objectViewer`
- 3b: `gcloud storage managed-folders remove-iam-policy-binding gs://<bucket>/policies/ --member=serviceAccount:aisquare-reader@<project>.iam.gserviceaccount.com --role=roles/storage.objectViewer`

Then delete the key (`gcloud iam service-accounts keys delete <key-id> --iam-account=aisquare-reader@<project>.iam.gserviceaccount.com`)
or the whole service account (`gcloud iam service-accounts delete aisquare-reader@<project>.iam.gserviceaccount.com`).
Removing the binding stops every read, a token AISquare already holds included, once the IAM change takes
effect (usually within a few minutes).

### AISquare devops

**What has to be true first** (both):
1. pipe-as-service runs credential-driven routing (task #30), which reads one `gcs` secret for both `gcs` and
   `gcs-source` (`TENANT_BLOBS` in `credentials/catalog.py`).
2. Its image has a pipe release that contains `gcs` (`aisquare-pipe[toolkits]`, from the first release with
   this connector).

**Where pipe-service runs today.** Production runs on ECS only. aisquare-releases has no prod pipe-service
(`environments/prod/services/` holds only explainability-gateway and praxis), and aisquare-infra has no
`prod/prod/pipe-service/*` secret. So GoodRx production follows the ECS path. `stg` runs on ECS and on EKS until
the cutover, and `dev` runs on EKS only. While both `stg`s run, turn the tenant on in both (at the least, in the one
the tenant's caller uses): the other one answers 422 `not_configured` for it.

**One customer key per environment.** Ask the customer for a separate key for each environment that reads the
bucket (step 4), and never put the production key in `stg` or `dev`. On EKS the value also sits in a GitHub
environment secret of aisquare-infra (below), which more people and workflows can reach than an ECS secret.

**Turning GCS on for a tenant is a deploy, today.** Every environment runs the env secrets backend
(`PIPE_SERVICE_SECRETS_BACKEND=env`: EKS `deploy/values.yaml` and both ECS task definitions in
pipe-as-service). The blob reaches the service only as the env var `PIPE_SECRET__<TENANT>__GCS__JSON`. Store
the value first ("The secret's value" below, saved as a local `blob.json`), then add the entry that reads
it. A pod or task reads the value when it starts.

`<TENANT>` is the tenant id in upper case. Every run of characters other than A-Z and 0-9 becomes one `_`, and
any `_` at either end is dropped (`acme-corp.eu` gives `PIPE_SECRET__ACME_CORP_EU__GCS__JSON`).

- **ECS** (production, and `stg` until the cutover):
  1. Create the secret in the task definition's region: prod `us-east-2`, stg `us-east-1`. The task
     execution role reads only `ais-pipe-service/<env>/*` in its own region, so a secret anywhere else stops
     the task from starting:
     `aws secretsmanager create-secret --region <region> --name ais-pipe-service/<env>/gcs-<tenant> --secret-string file://blob.json`
  2. Add an entry to the task definition's `secrets:` (`deploy/ecs/<env>/task-definition-pipe-service.json`
     in pipe-as-service). Use the same ARN form as the entries already there:

     ```json
     {
       "name": "PIPE_SECRET__<TENANT>__GCS__JSON",
       "valueFrom": "arn:aws:secretsmanager:<region>:<account>:secret:ais-pipe-service/<env>/gcs-<tenant>"
     }
     ```

  3. Merge it, then run pipe-as-service's `deploy` workflow for that environment and region:
     `gh workflow run deploy.yml --repo AISquare-Studio/pipe-as-service -f environment=<env> -f region=<region>`.
     The new task-definition revision is the deploy. Terraform ignores `container_definitions`, so this
     workflow owns them. The workflow builds and pushes the image from the head of the branch it runs on
     (`main`), so this deploy also ships everything else merged to pipe-as-service `main` since the last one.
     Check what that is first, above all in production.
- **EKS** (`stg`, `dev`): never put the value into Secrets Manager by hand. aisquare-infra creates the secret
  from `live/nonprod/data/supplied-secrets.json`. Its `secrets-nonprod` workflow fills the secret from the
  GitHub environment `nonprod-secrets`, when run by hand and after every `apply-nonprod`. So a value edited in
  Secrets Manager is overwritten with the GitHub one at the next sync. And if a listed secret's GitHub value
  is missing, the sync stops for EVERY secret.

  `<GH_SECRET>` below is the environment's own GitHub secret, because each environment has its own key:
  `GCS_<TENANT>_JSON` for `stg`, and `DEV_GCS_<TENANT>_JSON` for `dev` (like `DEV_INTERNAL_AUTH_TOKEN`). Use
  that one name in every step and command for the environment, so `dev` never reads or writes stg's secret.
  1. Store the value in GitHub:
     `gh secret set <GH_SECRET> --env nonprod-secrets --repo AISquare-Studio/aisquare-infra < blob.json`.
  2. In aisquare-infra, open one PR that adds the entry to `live/nonprod/data/supplied-secrets.json`:

     ```json
     {
       "nonprod/<env>/pipe-service/gcs-<tenant>": {
         "github_secret": "<GH_SECRET>",
         "description": "PIPE_SECRET__<TENANT>__GCS__JSON of the <env> pipe service: the GCS service account blob of <tenant>"
       }
     }
     ```

     The same PR adds its line to the `env` of the sync step in `.github/workflows/secrets-nonprod.yml`:
     `<GH_SECRET>: ${{ secrets.<GH_SECRET> }}`. Merge only after step 1, so the sync can't fail on it. The merge runs `apply-nonprod`, which creates the secret and then runs `secrets-nonprod` to fill
     it. Check that both ran green.
  3. In aisquare-releases, add to `environments/<env>/services/pipe-service/release.yaml`. That file is
     written by people. Never edit `values.yaml`, which the pipe-as-service build workflow overwrites:

     ```yaml
     secretEnv:
       PIPE_SECRET__<TENANT>__GCS__JSON: { from: pipe-service/gcs-<tenant> }
     ```

     Helm merges it with the `secretEnv` of `values.yaml`. A code owner merging that PR is the deploy. Argo CD
     syncs the ExternalSecret `pipe` (key `<secrets.prefix>/pipe-service/gcs-<tenant>`, for example
     `nonprod/stg/pipe-service/gcs-<tenant>`) before the Deployment. One ExternalSecret carries every
     `secretEnv` entry, so a secret with no value yet fails that ExternalSecret and holds the release.

After storing the value, delete `blob.json`, the vault item and any local copy of key.json. Then remove the
customer's guest access and the handoff vault.

**Never switch the secrets backend for GCS.** `PIPE_SERVICE_SECRETS_BACKEND=aws` moves EVERY secret the
service reads, the shared Composio key included. Under that backend, that key is read from
`<PIPE_SERVICE_SECRETS_PREFIX>/tenants/_shared/composio/api_key`, and a missing or denied secret reads as no
key. Flipping the backend "for GCS" takes Composio down for every connector in that environment. Moving to the
aws backend is a whole-service migration (pipe-as-service `DEPLOYMENT.md`, go-live checklist: the Composio key
and the OAuth apps first), never a GCS step. After that migration, the tenant's secret is
`<PIPE_SERVICE_SECRETS_PREFIX>/tenants/<tenant>/gcs` and needs no deploy. A new secret is picked up within
about 60 s (a missing secret is not cached, so only the 60 s routing TTL applies), and a changed one within
300 s (the secret cache).

**The secret's value.** pipe-as-service reads it as the tenant's credential blob for both `gcs` and
`gcs-source`. It is one JSON object:

```json
{
  "service_account_json": {
    "type": "service_account",
    "project_id": "<project>",
    "private_key_id": "<from key.json>",
    "private_key": "-----BEGIN PRIVATE KEY-----\n...\n-----END PRIVATE KEY-----\n",
    "client_email": "aisquare-reader@<project>.iam.gserviceaccount.com",
    "client_id": "<from key.json>",
    "token_uri": "https://oauth2.googleapis.com/token",
    "universe_domain": "googleapis.com"
  },
  "bucket": "<bucket>",
  "prefix": "policies/"
}
```

`service_account_json` is key.json as it was downloaded (an object, or the same JSON as a string). `prefix`
is optional and must end with `/`. Until the secret is read, pipe-as-service answers 422
`connector_unavailable` / `not_configured`, naming "credentials for gcs".

**Rotation**, at least before any key expiry:
1. The customer creates a new key for the same service account (step 4) and hands it over as in step 5.
2. Replace `service_account_json` in the value. Then restart the service, which reads the value only when a
   pod or task starts:
   - ECS: `aws secretsmanager put-secret-value --region <region> --secret-id ais-pipe-service/<env>/gcs-<tenant> --secret-string file://blob.json`,
     then `aws ecs update-service --region <region> --cluster ais-studio-ecs-cluster-<env> --service ais-studio-pipe-service-<env> --force-new-deployment`.
     Wait until the old tasks are gone.
   - EKS, in this order (`<GH_SECRET>` as in "Turning GCS on", per environment):
     1. `gh secret set <GH_SECRET> --env nonprod-secrets --repo AISquare-Studio/aisquare-infra < blob.json`.
        Never edit the value in Secrets Manager: the next sync puts the GitHub value back, and a pod that
        starts after that reads the old key.
     2. Run the sync and wait for it to finish. `gh workflow run secrets-nonprod.yml --repo AISquare-Studio/aisquare-infra`
        returns as soon as the run is queued. Find the run with
        `gh run list --workflow secrets-nonprod.yml --repo AISquare-Studio/aisquare-infra --limit 1 --json databaseId,createdAt`
        (its `createdAt` must be after your dispatch; a new run can take a few seconds to appear), then
        `gh run watch <run-id> --repo AISquare-Studio/aisquare-infra --exit-status`. It must end green, and
        `gh run view <run-id> --repo AISquare-Studio/aisquare-infra --log | grep pipe-service/gcs-<tenant>`
        must show `nonprod/<env>/pipe-service/gcs-<tenant>: updated`.
     3. Only then refresh the ExternalSecret. Its refresh interval is 1 h, the chart default, which
        pipe-service doesn't override. Or force it now with
        `kubectl -n <env> annotate externalsecret pipe force-sync=$(date +%s) --overwrite`. Then check that
        `kubectl -n <env> get externalsecret pipe -o jsonpath='{.status.refreshTime}'` is later than the run's
        end (`gh run view <run-id> --repo AISquare-Studio/aisquare-infra --json updatedAt`) and that
        `kubectl -n <env> get externalsecret pipe` shows `SecretSynced`. A refresh that ran before the sync
        finished copied the old key, and it still shows `SecretSynced`.
     4. Restart the Deployment: `kubectl -n <env> rollout restart deployment/pipe-service`, then
        `kubectl -n <env> rollout status deployment/pipe-service`. The chart has no checksum annotation or
        reloader, so pods keep the old value until they restart.
   - After a migration to the aws backend: no restart, but wait at least 300 s.
3. Only when every pod or task runs with the new key does the customer delete the old one. Deleting it
   earlier breaks calls on pods that still hold it.

**Offboarding** (AISquare's copy):
- ECS: remove the task definition's `secrets:` entry and deploy (as above). Then delete the secret:
  `aws secretsmanager delete-secret --region <region> --secret-id ais-pipe-service/<env>/gcs-<tenant>`.
  Keep that order, because a task whose secret is gone does not start. The name stays taken for the recovery
  window (30 days by default), so to turn the tenant back on within it, restore the secret
  (`aws secretsmanager restore-secret --region <region> --secret-id ais-pipe-service/<env>/gcs-<tenant>`) and
  `put-secret-value` the new blob instead of `create-secret`.
- EKS:
  1. Remove the `secretEnv` entry from `release.yaml` (a deploy). Do this first: while the entry is listed, a
     deleted secret fails the ExternalSecret.
  2. In aisquare-infra, open one PR that removes the `supplied-secrets.json` entry and the
     `secrets-nonprod.yml` env line, and the tenant's import block in `live/nonprod/data/imports.tf` if one
     is still there (below). Its `apply-nonprod` deletes the Secrets Manager secret, with a 7-day recovery
     window.
  3. After that merge, run
     `gh secret delete <GH_SECRET> --env nonprod-secrets --repo AISquare-Studio/aisquare-infra`.
     If you delete it earlier, while the entry is still listed, every sync stops.

  **Never delete the Secrets Manager secret by hand.** `delete-secret` keeps the name for its recovery window
  (30 days by default), and Terraform's AWS provider reads a secret that is pending deletion as gone. Until the
  window ends, while the entry is listed:
  - the next `apply-nonprod` of `live/nonprod/data` plans to create the secret, and `CreateSecret` fails
    ("already scheduled for deletion"). The apply fails, and its `secrets-nonprod` job is skipped;
  - `secrets-nonprod` fails on the secret and stops there, so no secret listed after it is written, other
    services' included;
  - the ExternalSecret `pipe` fails, which holds every later pipe-service release in that environment.

  To undo it: `aws secretsmanager restore-secret --region us-east-1 --secret-id nonprod/<env>/pipe-service/gcs-<tenant>`.
  The value comes back with it. If an `apply-nonprod` ran on `live/nonprod/data` in between, its failure also
  dropped the secret from Terraform's state, so import it (below) as well. Then run `secrets-nonprod`, wait for
  it and refresh the ExternalSecret, as the EKS rotation above does.

  **Turning the tenant back on within 7 days** of the offboarding apply fails the same way: the name is still
  pending deletion. Wait out the window, or restore the secret (above) first. Only then open the PR that re-adds
  the entry: the provider cannot import a secret that is still pending deletion, so a PR opened before the
  restore fails. In that PR, add an import block to `live/nonprod/data/imports.tf` like the ones already there:

  ```hcl
  import {
    to = aws_secretsmanager_secret.supplied["nonprod/<env>/pipe-service/gcs-<tenant>"]
    id = "<ARN>"
  }
  ```

  `<ARN>` comes from
  `aws secretsmanager describe-secret --region us-east-1 --secret-id nonprod/<env>/pipe-service/gcs-<tenant> --query ARN --output text`.
  The restored secret holds the old value until the sync writes the GitHub one.

  **Remove the import block once the `apply-nonprod` that imported it is green**, in a follow-up PR (the
  secret stays in Terraform's state). A block left behind outlives the entry: after the next offboarding
  removes the entry, every plan of `live/nonprod/data` fails ("Configuration for import target does not
  exist"), so its `apply-nonprod` fails and the sync is skipped. The two blocks already there are safe only
  while their entries stay listed.
- Tell the customer to do their offboarding (above) as well: deleting our copy does not revoke the key.

**Later (not built):** Workload Identity Federation from our AWS principal to the customer's service
account. It avoids a long-lived key, and needs setup on both sides.

## Tests

`pytest` from this directory, hermetic: core's socket guard blocks the network, and `tests/fakes.py`
plays Google (the token endpoint decodes the signed assertion; the storage API serves an in-memory bucket).
`real_session()` puts the same fake behind a real `requests.Session`, so a body that breaks off midway fails
as it would off a socket. Keys are generated per run; no real key or account is used.
