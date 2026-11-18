# aisquare_pipe_databricks: Databricks (read-only, native)

A toolkit (`databricks`) and a files source (`databricks-files-source`) for one Databricks workspace: files
in Unity Catalog volumes (and DBFS, where the tenant enables it), read-only SQL on a SQL warehouse, and the
Unity Catalog listings. It replaces Composio's `DATABRICKS` toolkit, which has no volume download and no SQL
execution (GoodRx Smart Connectors, item 3e).

Ships inside the `aisquare-pipe` distribution: `pip install "aisquare-pipe[databricks]"` (or `[full]`, or
`[toolkits]`). The extra adds one dependency, `sqlglot>=26.25,<28`, which parses SQL so that only a SELECT
is ever sent. It is the one specifier every extra naming sqlglot shares (`scripts/dep_floor.py`), and CI runs
this suite at its floor too. HTTP goes through `requests`, which core already has. Without the extra,
`databricks` and `databricks-files-source` count as not installed.

## What it does

| Tool | What it returns |
|---|---|
| `DATABRICKS_READ_FILE` (primary) | One file from `/Volumes/<catalog>/<schema>/<volume>/...` (Files API) or `dbfs:/...` (DBFS API, when the blob says `"dbfs": true`): its bytes as a file (text up to 1 MiB also inline as `content`), `size`, `content_type` and the version markers. `include_content=false` reads the metadata only. `max_bytes` (default 25 MiB, at most 100 MiB). |
| `DATABRICKS_LIST_FILES` | A volume (or, when enabled, DBFS) directory: `path`, `name`, `is_directory`, `size`, `modified_time`; `page_size`/`page_token` paging for volumes. |
| `DATABRICKS_RUN_SELECT` | ONE read-only query on the credential's warehouse: `columns`, `rows`, `row_count`, `truncated`. `row_limit` (default 1000, at most 10000), an 8 MiB byte cap, optional default `catalog`/`schema`, and `timeout_seconds` (5-120, default 60; a host budget lowers it), after which the statement is cancelled (see "The deadline"). |
| `DATABRICKS_LIST_CATALOGS`, `_SCHEMAS`, `_TABLES` | Unity Catalog listings (tables with their columns), paged. |

`databricks-files-source` pull params: `path` (a directory, or one file), `recursive` (at most 10 levels),
`modified_after` (RFC 3339), `max_files` (default 100, at most 1000), `max_bytes` per file (default 25 MiB, at
most 64 MiB for the source, sized for a 512 MiB pod; larger files are skipped and counted in one log line).
Each envelope carries the file's bytes and `metadata`: `filename`, `path`, `storage` (`volume` or `dbfs`),
`size`, `content_type` and the version markers.

**A failed pull** raises, as pipe-as-service answers it:
- 422, a `ValueError`: no usable credential (`DatabricksNotConfigured`, before any request), bad params (a
  `dbfs:/` path for a tenant whose blob doesn't enable DBFS included), and what Databricks refused that the
  caller can fix: a path that does not exist (404) or a request it calls invalid (400), as
  `DatabricksRequestError`;
- 502, a `PipelineError`: any other failure (a refused credential or grant, throttling with `retry_after` from
  `Retry-After`, a timeout, a 5xx, a download that broke off).

Neither message carries Databricks's own error text (it can name paths, and hosts log these). The tools still
show it to the caller.

**Memory.** A pull holds about two files at a time: the one being read, and the one the consumer still holds
while the next downloads (pipe-as-service's pull loop does). A download (volume or DBFS) goes into one buffer
that hands its bytes over without a copy, and the source lets go of each file before the next. Measured with
`tracemalloc` (`tests/test_memory.py`, three files, the consumer dropping each envelope):

| Files | Volume | DBFS |
|---|---|---|
| 25 MiB (the default `max_bytes`) | 50 MiB | 58 MiB |
| 64 MiB (the source's cap) | 129 MiB | 135 MiB |

Before, a download held its chunks and then their join (DBFS: a bytearray and its copy), and the source cap was
100 MiB. Tools still read up to 100 MiB (`DATABRICKS_READ_FILE`), one file per call.
`DATABRICKS_RUN_SELECT`'s worst case is many short values (each a Python string of about 60 bytes): 10,000 rows
of 168 two-character columns, about 8 MiB of JSON, peak at 117 MiB (232 MiB at the old 16 MiB cap).

**Version markers.** `modified_time` (RFC 3339 UTC; the Files API's Last-Modified, DBFS's modification
time), `etag` (volumes) and `sha256` of the bytes (on a read or pull). Store them and re-read the file when
they change.

## SQL is SELECT-only

Before ANY HTTP call (`sqlguard.py`), two independent checks must both pass. Each one refuses every write on
its own, so neither is a backup for the other: the suite runs each layer ALONE against a reference reading of
Spark's lexer, on every combination of up to four pieces of a quote, comment, hint and escape alphabet,
hundreds of thousands of seeded random strings and, for the parser, every combination of up to eight pieces
of a block-comment alphabet (where sqlglot nests comments unlike Spark), and whatever that layer admits has
no write in Spark's reading. What only one layer refuses is pinned too (none of it a write in Spark's
reading), so dropping either one fails the suite.

The two read the text differently. The lexer decides where Spark's tokens end and refuses wherever any two
readers could disagree. The parser reads the text as sqlglot does, refuses wherever sqlglot's reading and
Spark's differ, and adds the statement's structure (a `FOR SHARE` lock, text that doesn't parse).

1. **Lexer** (no parser). It reads the text as Spark's own lexer does (`SqlBaseLexer.g4`), and refuses wherever
   two readers could disagree on where a token ends:
   - a `--` comment ends at the first CR or LF. Spark carries it on past a backslash right before the LF, and
     sqlglot doesn't, so no comment (of either kind) may end in a backslash. A `--` comment a bare CR ends is
     refused too;
   - Spark nests `/* */` comments: a `/*` inside one is refused, and so is one that never closes. `/*+ ... */`
     is a hint, whose contents Spark reads as tokens, and so does the lexer;
   - Spark takes backslash escapes in `'...'` and `"..."`, never in backticks, and none in `r'...'`. Every
     quoted token must end in the same place whether a backslash escapes or not; if not, it is refused. So
     write a quote inside a string as `''` (or `"it's"`), not `\'`;
   - `${` is refused anywhere: Spark substitutes `${...}` variables before it reads the text, inside strings
     and comments too;
   - `$` is refused outside quotes and comments: Databricks reads `$$...$$` as text in places (routine
     bodies), sqlglot reads `$` as a parameter, and no read-only query needs one there (parameter markers are
     `:name` and `?`);
   - outside quotes and comments only ASCII, and no backslash (Spark 3 upper-cases `ı` to `I`, so `ınsert`
     would read as `INSERT`); anywhere, no control character but tab, LF and CR, and no NEL, LS or PS.

   What remains must be one statement (a trailing `;` is fine) starting with `SELECT`/`WITH` that names no
   write, DDL, session or scripting keyword (a column with such a name goes in backticks).
2. **Parser** (sqlglot, Databricks dialect). First, sqlglot's own tokens must read the way Spark's lexer does.
   Each place the two lexers differ is refused:
   - a comment that ends in a backslash (Spark carries a `--` comment on past it at a line break; sqlglot
     doesn't);
   - a comment whose text, up to its closer's `*` included, holds `/*` (the lexer's rule, from sqlglot's
     side). sqlglot nests comments unlike Spark: it doesn't nest a `/*` right after a comment's opener
     (`/*/**/`), reads `*/*` inside a nested comment as a close and then an open, reads `/*/*/` as one
     comment, and nests a `/*+`. Its comment text doesn't say which kind of comment it was, so a `--` comment
     that holds `/*` or ends in `/` is refused too;
   - a `/*+` outside quotes that is not a hint sqlglot parsed (right after `SELECT`) holding only names,
     numbers, commas and parentheses (`/*+ BROADCAST(t) */`, `/*+ REPARTITION(8, c) */`). Spark reads a hint's
     insides as tokens wherever it stands; sqlglot reads `/*+` anywhere else as a comment;
   - `{#` outside quotes (sqlglot reads `{# ... #}` as a Jinja comment, Spark doesn't), and `*` right before
     `/` (Spark reads `*/` as one token, so `*/*` opens no comment);
   - a `"..."` string that ends elsewhere when a backslash escapes nothing (a reader may take no escapes
     there; Spark's lexer and sqlglot both take them whatever `spark.sql.ansi.doubleQuotedIdentifiers` says,
     so this only refuses more than it needs to);
   - `${` anywhere, and non-ASCII outside quotes and comments.

   Then:
   - exactly one statement;
   - a query at the root (`SELECT`, `WITH ... SELECT`, `UNION`/`INTERSECT`/`EXCEPT`, parenthesised);
   - no write, DDL, session, transaction or script node anywhere in the tree (`INSERT`, `UPDATE`, `DELETE`,
     `MERGE`, `CREATE`, `DROP`, `ALTER`, `SELECT ... INTO`, `SET`, `USE`, `COPY`, `CACHE`, `REFRESH`,
     `GRANT`, a `FOR SHARE`/`FOR UPDATE` lock, `TRANSFORM ... USING`, unparsed commands, ...).
   - Text it can't tokenize or parse is refused.
   - sqlglot tokenizes and parses inside core's `quiet_sqlglot()`: what it logs meanwhile (a JSON path it can't
     read is logged, path and all, at WARNING) never gets out. A sqlglot older than 26.25 refuses every
     statement.

Both refuse calls of the functions with side effects outside the query: `http_request` and `remote_query`
(a Unity Catalog connection can write there), `read_kafka`, `read_kinesis`, `read_pubsub` and `read_pulsar`
(the warehouse connects to the broker or endpoint the caller names), `reflect`/`java_method`/`try_reflect`,
`secret`/`try_secret` and `list_secrets` (every secret scope and key name), `vector_search` (it bills an
embedding endpoint for its `query_text`, as `ai_*` does), `IDENTIFIER(...)` (a name built from a string), and
any `ai_*` function (model serving, billed outside the warehouse), named plainly, qualified or quoted. Both
refuse `TRANSFORM ... USING '<script>'`, which runs a command. A column named `secret` or `ai_score` is fine:
only a call is refused.

A refusal is `forbidden` ("only one SELECT (or WITH ... SELECT) may run: <why>"), and the message never echoes
the SQL: a refused function is named by its canonical name (`REFLECT`, or "an AI_* function"), never as the
caller spelled it. The statement is then sent with `disposition: INLINE`, `row_limit` and `byte_limit`. Give
the principal read-only grants as well (defence in depth).

### The deadline

`timeout_seconds` (or the host's budget, if lower) bounds the whole call, the OAuth token included, and every
request's timeout fits what is left:

- **Up to 55 s left** (`timeout_seconds` up to 55, or a host budget below that): one synchronous call with
  `on_wait_timeout` `CANCEL`, so the warehouse cancels the statement itself at `wait_timeout`, even when its
  answer is lost (a client timeout). `wait_timeout` is what is left less 5 s (5 to 50 s), and the HTTP read
  timeout `wait_timeout` + 5 s. Under 10 s left, `wait_timeout` is the API's least, 5 s, and the read timeout
  is what is left: the client may give up first, and the statement still ends 5 s after it was sent. A
  statement can run until about 5 s before the deadline.
- **More than 55 s left** (`timeout_seconds` 56 to 120): asynchronous. `wait_timeout` `0s` answers at once with
  the `statement_id`. The statement is polled until it ends, and cancelled when the deadline comes (2 s is kept
  back for the cancel). A poll that fails or times out cancels it too.

The one case the connector can't cancel is an asynchronous submit whose answer (the `statement_id`) never
arrives. Step 3 of the checklist has the customer set the warehouses' `STATEMENT_TIMEOUT` for that.

**File reads** fit the host's budget (`config["timeout_seconds"]`) too, from the client's creation on. Every
request (the OAuth token, a listing, a HEAD, each 1 MB DBFS read) gets at most the time left as its timeout,
and none is sent once it is spent (the call fails `timeout`). requests' timeout bounds each socket read, not a
whole body, so a volume download is read one socket read at a time (urllib3 2's `read1`), each sent with the
time left as its socket timeout: a body that trickles in, or stalls, still ends by the deadline. With urllib3
1.26 (no `read1`), the deadline is checked between 64 KiB chunks instead. Without a host budget, each request
has its own 30 s timeout.

## Security

- The workspace URL, the token or service principal, the warehouse and the DBFS switch come only from the
  tenant's blob. No tool argument or pull param can set them: the strict schemas reject them, and the source
  refuses them as server-controlled.
- `workspace_url` must be https on `*.cloud.databricks.com`, `*.azuredatabricks.net` or
  `*.gcp.databricks.com`, with no port, path or userinfo. The account consoles (`accounts.cloud.databricks.com`
  and its Azure and GCP twins) are refused: they serve no workspace. Every call goes to the workspace host, and
  redirects are not followed.
- Paths must be volume (`/Volumes/c/s/v/...`) or DBFS (`dbfs:/...`) paths, with no `.`/`..` segments.
- **DBFS is off unless the blob says `"dbfs": true`.** DBFS has no Unity Catalog grants: the DBFS root and every
  mount (`dbfs:/mnt/...`, which Databricks reads with the mount's own cloud credentials) are readable by every
  user of the workspace, this principal included, unless a workspace admin has disabled DBFS root and mounts.
  No grant in step 2 narrows that. So with DBFS on, the principal can read everything there, whatever volumes
  it was granted. Off, a `dbfs:/` path is refused (`forbidden`; 422 in the source) before any request.
- Ambient configuration (`DATABRICKS_HOST`/`DATABRICKS_TOKEN`, `~/.databrickscfg`) is never read. A
  missing or malformed blob fails closed: "Databricks is not configured: <what>" (`not_connected`), and no
  request is made.
- Secrets never appear in errors, logs or reprs. OAuth M2M tokens are cached in memory until five minutes
  before they expire.

## Registration checklist

### The customer (a Databricks workspace admin)

1. **Choose the principal** (prefer a service principal):
   - **Service principal (OAuth M2M):** create one (Settings → Identity and access → Service principals),
     then generate an OAuth secret for it. Keep its application (client) id and the secret.
   - **Or a personal access token** of a dedicated read-only user (User settings → Developer → Access
     tokens), with an expiry they will rotate before.
2. **Grant read access only, to that principal:**
   - Files in a volume: `GRANT USE CATALOG ON CATALOG <c> TO <p>`, `GRANT USE SCHEMA ON SCHEMA <c>.<s> TO
     <p>`, `GRANT READ VOLUME ON VOLUME <c>.<s>.<v> TO <p>`.
   - Tables for SQL: `USE CATALOG`/`USE SCHEMA` as above, plus `GRANT SELECT ON TABLE <c>.<s>.<t> TO <p>`
     (or on the schema).
   - The warehouse: `CAN USE` on the SQL warehouse (Permissions on the warehouse).
   - DBFS paths: there is no grant to give. DBFS has no Unity Catalog grants: its root and every mount are
     readable by every user of the workspace (a mount with its own cloud credentials), unless a workspace
     admin has disabled DBFS root and mounts. So AISquare reads DBFS only if you ask for it (we set
     `"dbfs": true`), and then the principal can read all of it, whatever the grants above say. Prefer moving
     the files into a volume.
3. **Bound every statement on the warehouse side** (recommended): a workspace admin sets the SQL
   configuration parameter `STATEMENT_TIMEOUT` (the workspace's SQL warehouse settings, "SQL configuration
   parameters", or the SQL Warehouses API's workspace configuration) to `600` or less. The connector cancels
   its own statements at their deadline (at most 120 s), so this only ends one whose submit answer was lost.
   It applies to every warehouse in the workspace, so pick a value its other workloads allow (the default is
   two days).
4. **Hand AISquare devops** the workspace URL (e.g. `https://adb-1234567890123456.7.azuredatabricks.net`), the
   SQL warehouse id (SQL Warehouses → the warehouse → Connection details → the id in its HTTP path), whether
   DBFS should be read (step 2), and the client id + secret (or the PAT). Hand over the secret (or the PAT)
   only through AISquare's team vault (1Password): devops invites your admin as a guest to a vault for this
   handoff. Never by email, chat or a ticket. Use one principal (or one OAuth secret) per AISquare environment,
   and never give the production one for staging.

**Offboarding** (when AISquare should stop reading): delete the service principal's OAuth secret (or revoke the
PAT), then remove its grants or the principal itself. Deleting AISquare's copy does not revoke it.

### AISquare devops

**What has to be true first:**
1. pipe-as-service runs credential-driven routing (task #30). Its `TENANT_BLOBS` (`credentials/catalog.py`) maps
   `databricks-files` → `databricks`, so the toolkit and `databricks-files-source` read ONE secret. Without that
   row, the source looks for a secret named `databricks-files`.
2. Its image has a pipe release that contains `databricks` (`aisquare-pipe[toolkits]`, from the first release
   with this connector).

**Where pipe-service runs today.** Production runs on ECS only. aisquare-releases has no prod pipe-service
(`environments/prod/services/` holds only explainability-gateway and praxis), and aisquare-infra has no
`prod/prod/pipe-service/*` secret. So GoodRx production follows the ECS path. `stg` runs on ECS and on EKS until the
cutover, and `dev` runs on EKS only. While both `stg`s run, turn the tenant on in both (at the least, in the one the
tenant's caller uses): the other one sends the tenant's Databricks calls to Composio's `DATABRICKS` (read-only
guard), on whatever Composio connection the person has, not to the customer's principal.

**The whole tenant moves at once.** `databricks` is a tenant-credential toolkit that Composio also serves
(pipe-as-service `COMPOSIO_ALSO_SERVES`), so it has no per-person stickiness (pipe-as-service `DEPLOYMENT.md`,
decision M3 of review seq 13759). Once the service reads the tenant's secret, every person in the tenant leaves
their own Composio Databricks connection: their reads switch identity to the tenant's principal, and people who
never connected Databricks can now read whatever that principal reads. Tell the customer before turning it on.

**One credential per environment** (step 4): never put the production principal's secret in `stg` or `dev`. On
EKS the value also sits in a GitHub environment secret of aisquare-infra (below), which more people and
workflows can reach than an ECS secret.

**Turning Databricks on for a tenant is a deploy, today.** Every environment runs the env secrets backend
(`PIPE_SERVICE_SECRETS_BACKEND=env`: EKS `deploy/values.yaml` and both ECS task definitions in pipe-as-service).
The blob reaches the service only as the env var `PIPE_SECRET__<TENANT>__DATABRICKS__JSON`. Store the value first
("The secret's value" below, saved as a local `blob.json`), then add the entry that reads it. A pod or task reads
the value when it starts.

`<TENANT>` is the tenant id in upper case. Every run of characters other than A-Z and 0-9 becomes one `_`, and any
`_` at either end is dropped (`acme-corp.eu` gives `PIPE_SECRET__ACME_CORP_EU__DATABRICKS__JSON`).

- **ECS** (production, and `stg` until the cutover):
  1. Create the secret in the task definition's region: prod `us-east-2`, stg `us-east-1`. The task execution
     role reads only `ais-pipe-service/<env>/*` in its own region, so a secret anywhere else stops the task from
     starting:
     `aws secretsmanager create-secret --region <region> --name ais-pipe-service/<env>/databricks-<tenant> --secret-string file://blob.json`
  2. Add an entry to the task definition's `secrets:` (`deploy/ecs/<env>/task-definition-pipe-service.json` in
     pipe-as-service). Use the same ARN form as the entries already there:

     ```json
     {
       "name": "PIPE_SECRET__<TENANT>__DATABRICKS__JSON",
       "valueFrom": "arn:aws:secretsmanager:<region>:<account>:secret:ais-pipe-service/<env>/databricks-<tenant>"
     }
     ```

  3. Merge it, then run pipe-as-service's `deploy` workflow for that environment and region:
     `gh workflow run deploy.yml --repo AISquare-Studio/pipe-as-service -f environment=<env> -f region=<region>`. The
     new task-definition revision is the deploy. Terraform ignores `container_definitions`, so this workflow owns
     them. The workflow builds and pushes the image from the head of the branch it runs on (`main`), so this deploy
     also ships everything else merged to pipe-as-service `main` since the last one. Check what that is first, above
     all in production.
- **EKS** (`stg`, `dev`): never put the value into Secrets Manager by hand. aisquare-infra creates the secret from
  `live/nonprod/data/supplied-secrets.json`. Its `secrets-nonprod` workflow fills the secret from the GitHub environment
  `nonprod-secrets`, when run by hand and after every `apply-nonprod`. So a value edited in Secrets Manager is
  overwritten with the GitHub one at the next sync. And if a listed secret's GitHub value is missing, the sync stops
  for EVERY secret.

  `<GH_SECRET>` below is the environment's own GitHub secret, because each environment has its own credential:
  `DATABRICKS_<TENANT>_JSON` for `stg`, and `DEV_DATABRICKS_<TENANT>_JSON` for `dev` (like `DEV_INTERNAL_AUTH_TOKEN`).
  Use that one name in every step and command for the environment, so `dev` never reads or writes stg's secret.
  1. Store the value in GitHub:
     `gh secret set <GH_SECRET> --env nonprod-secrets --repo AISquare-Studio/aisquare-infra < blob.json`.
  2. In aisquare-infra, open one PR that adds the entry to `live/nonprod/data/supplied-secrets.json`:

     ```json
     {
       "nonprod/<env>/pipe-service/databricks-<tenant>": {
         "github_secret": "<GH_SECRET>",
         "description": "PIPE_SECRET__<TENANT>__DATABRICKS__JSON of the <env> pipe service: the Databricks credential blob of <tenant>"
       }
     }
     ```

     The same PR adds its line to the `env` of the sync step in `.github/workflows/secrets-nonprod.yml`:
     `<GH_SECRET>: ${{ secrets.<GH_SECRET> }}`. Merge only after step 1, so the sync can't fail on it. The merge runs
     `apply-nonprod`, which creates the secret and then runs `secrets-nonprod` to fill it. Check that both ran green.
  3. In aisquare-releases, add to `environments/<env>/services/pipe-service/release.yaml`. That file is written by
     people. Never edit `values.yaml`, which the pipe-as-service build workflow overwrites:

     ```yaml
     secretEnv:
       PIPE_SECRET__<TENANT>__DATABRICKS__JSON: { from: pipe-service/databricks-<tenant> }
     ```

     Helm merges it with the `secretEnv` of `values.yaml`. A code owner merging that PR is the deploy. Argo CD syncs
     the ExternalSecret `pipe` (key `<secrets.prefix>/pipe-service/databricks-<tenant>`) before the Deployment. One
     ExternalSecret carries every `secretEnv` entry, so a secret with no value yet fails that ExternalSecret and holds
     the release.

After storing the value, delete `blob.json` and the vault item. Then remove the customer's guest access and the
handoff vault.

**Never switch the secrets backend for Databricks.** `PIPE_SERVICE_SECRETS_BACKEND=aws` moves EVERY secret the
service reads, the shared Composio key included, and a missing secret reads as no key. Flipping the backend "for
Databricks" takes Composio down for every connector in that environment. Moving to the aws backend is a
whole-service migration (pipe-as-service `DEPLOYMENT.md`). After that migration, the tenant's secret is
`<PIPE_SERVICE_SECRETS_PREFIX>/tenants/<tenant>/databricks` and needs no deploy. A new secret is picked up within
about 60 s (a missing secret is not cached, so only the 60 s routing TTL applies), and a changed one within 300 s
(the secret cache).

**The secret's value** is one JSON object, read for both `databricks` and `databricks-files-source`. With a
service principal:

```json
{"workspace_url": "https://adb-1234567890123456.7.azuredatabricks.net",
 "auth": "oauth_m2m",
 "client_id": "<service principal application id>",
 "client_secret": "<its OAuth secret>",
 "warehouse_id": "<SQL warehouse id>"}
```

or, with a PAT:

```json
{"workspace_url": "https://acme.cloud.databricks.com",
 "auth": "pat",
 "token": "<personal access token>",
 "warehouse_id": "<SQL warehouse id>"}
```

| Key | Required | Meaning |
|---|---|---|
| `workspace_url` | yes | `https://<workspace>` on `cloud.databricks.com`, `azuredatabricks.net` or `gcp.databricks.com`: no port, path or userinfo, not an `accounts.` console. |
| `auth` | no (`pat`) | `pat` or `oauth_m2m`. |
| `token` | with `pat` | The personal access token. |
| `client_id`, `client_secret` | with `oauth_m2m` | The service principal's application id and OAuth secret (the token comes from the workspace's `/oidc/v1/token`, scope `all-apis`). |
| `warehouse_id` | no | The SQL warehouse SQL runs on. Without it `DATABRICKS_RUN_SELECT` is "not configured" and the file tools still work. |
| `dbfs` | no (`false`) | `true` lets the tools and the source read `dbfs:/` paths. Only if the customer asked for it (step 2): the principal can then read the whole DBFS root and every mount. |

If the secret is missing or malformed, every tool fails with `not_connected` ("Databricks is not configured:
…") and the source raises `DatabricksNotConfigured`, both before any request. The reason never contains a
secret. Until the secret is read, the tenant's calls go to Composio's `DATABRICKS` (routing rule d, read-only
guard), not to the customer's principal.

**Rotation**, at least before the PAT or the OAuth secret expires:
1. The customer creates a new OAuth secret for the same service principal (or a new PAT) and hands it over
   (step 4).
2. Replace `client_secret` (or `token`) in the value. Then restart the service, which reads the value only when a pod
   or task starts:
   - ECS: `aws secretsmanager put-secret-value --region <region> --secret-id ais-pipe-service/<env>/databricks-<tenant> --secret-string file://blob.json`,
     then `aws ecs update-service --region <region> --cluster ais-studio-ecs-cluster-<env> --service ais-studio-pipe-service-<env> --force-new-deployment`.
     Wait until the old tasks are gone.
   - EKS, in this order (`<GH_SECRET>` as in "Turning Databricks on", per environment):
     1. `gh secret set <GH_SECRET> --env nonprod-secrets --repo AISquare-Studio/aisquare-infra < blob.json`. Never
        edit the value in Secrets Manager: the next sync puts the GitHub value back, and a pod that starts after that
        reads the old secret.
     2. Run the sync and wait for it to finish. `gh workflow run secrets-nonprod.yml --repo AISquare-Studio/aisquare-infra`
        returns as soon as the run is queued. Find the run with
        `gh run list --workflow secrets-nonprod.yml --repo AISquare-Studio/aisquare-infra --limit 1 --json databaseId,createdAt`
        (its `createdAt` must be after your dispatch; a new run can take a few seconds to appear), then
        `gh run watch <run-id> --repo AISquare-Studio/aisquare-infra --exit-status`. It must end green, and
        `gh run view <run-id> --repo AISquare-Studio/aisquare-infra --log | grep pipe-service/databricks-<tenant>` must
        show `nonprod/<env>/pipe-service/databricks-<tenant>: updated`.
     3. Only then refresh the ExternalSecret. Its refresh interval is 1 h, the chart default, which pipe-service
        doesn't override. Or force it now with
        `kubectl -n <env> annotate externalsecret pipe force-sync=$(date +%s) --overwrite`. Then check that
        `kubectl -n <env> get externalsecret pipe -o jsonpath='{.status.refreshTime}'` is later than the run's end
        (`gh run view <run-id> --repo AISquare-Studio/aisquare-infra --json updatedAt`) and that
        `kubectl -n <env> get externalsecret pipe` shows `SecretSynced`. A refresh that ran before the sync finished
        copied the old secret, and it still shows `SecretSynced`.
     4. Restart the Deployment: `kubectl -n <env> rollout restart deployment/pipe-service`, then
        `kubectl -n <env> rollout status deployment/pipe-service`. The chart has no checksum annotation or reloader,
        so pods keep the old value until they restart.
   - After a migration to the aws backend: no restart, but wait at least 300 s.
3. Only when every pod or task runs with the new secret does the customer delete the old OAuth secret (or revoke
   the old PAT). Deleting it earlier breaks calls on pods that still hold it.

**Offboarding** (AISquare's copy):
- ECS: remove the task definition's `secrets:` entry and deploy (as above). Then delete the secret:
  `aws secretsmanager delete-secret --region <region> --secret-id ais-pipe-service/<env>/databricks-<tenant>`. Keep that
  order, because a task whose secret is gone does not start. The name stays taken for the recovery window (30 days by
  default), so to turn the tenant back on within it, restore the secret
  (`aws secretsmanager restore-secret --region <region> --secret-id ais-pipe-service/<env>/databricks-<tenant>`) and
  `put-secret-value` the new blob instead of `create-secret`.
- EKS:
  1. Remove the `secretEnv` entry from `release.yaml` (a deploy). Do this first: while the entry is listed, a deleted
     secret fails the ExternalSecret.
  2. In aisquare-infra, open one PR that removes the `supplied-secrets.json` entry and the `secrets-nonprod.yml` env
     line, and the tenant's import block in `live/nonprod/data/imports.tf` if one is still there (below). Its
     `apply-nonprod` deletes the Secrets Manager secret, with a 7-day recovery window.
  3. After that merge, run
     `gh secret delete <GH_SECRET> --env nonprod-secrets --repo AISquare-Studio/aisquare-infra`. If you delete it
     earlier, while the entry is still listed, every sync stops.

  **Never delete the Secrets Manager secret by hand.** `delete-secret` keeps the name for its recovery window (30 days
  by default), and Terraform's AWS provider reads a secret that is pending deletion as gone. Until the window ends,
  while the entry is listed:
  - the next `apply-nonprod` of `live/nonprod/data` plans to create the secret, and `CreateSecret` fails ("already
    scheduled for deletion"). The apply fails, and its `secrets-nonprod` job is skipped;
  - `secrets-nonprod` fails on the secret and stops there, so no secret listed after it is written, other services'
    included;
  - the ExternalSecret `pipe` fails, which holds every later pipe-service release in that environment.

  To undo it: `aws secretsmanager restore-secret --region us-east-1 --secret-id nonprod/<env>/pipe-service/databricks-<tenant>`.
  The value comes back with it. If an `apply-nonprod` ran on `live/nonprod/data` in between, its failure also dropped
  the secret from Terraform's state, so import it (below) as well. Then run `secrets-nonprod`, wait for it and refresh
  the ExternalSecret, as the EKS rotation above does.

  **Turning the tenant back on within 7 days** of the offboarding apply fails the same way: the name is still pending
  deletion. Wait out the window, or restore the secret (above) first. Only then open the PR that re-adds the entry:
  the provider cannot import a secret that is still pending deletion, so a PR opened before the restore fails. In
  that PR, add an import block to `live/nonprod/data/imports.tf` like the ones already there:

  ```hcl
  import {
    to = aws_secretsmanager_secret.supplied["nonprod/<env>/pipe-service/databricks-<tenant>"]
    id = "<ARN>"
  }
  ```

  `<ARN>` comes from
  `aws secretsmanager describe-secret --region us-east-1 --secret-id nonprod/<env>/pipe-service/databricks-<tenant> --query ARN --output text`.
  The restored secret holds the old value until the sync writes the GitHub one.

  **Remove the import block once the `apply-nonprod` that imported it is green**, in a follow-up PR (the secret stays
  in Terraform's state). A block left behind outlives the entry: after the next offboarding removes the entry, every
  plan of `live/nonprod/data` fails ("Configuration for import target does not exist"), so its `apply-nonprod` fails
  and the sync is skipped. The two blocks already there are safe only while their entries stay listed.
- Tell the customer to do their offboarding (above) as well: deleting our copy does not revoke the principal's
  secret or the PAT.

## Tests

`pytest` from this directory, hermetic: core's socket guard blocks the network, and `tests/fakes.py` plays
the workspace (the OIDC token endpoint, the Files API, DBFS, statement execution and cancel, Unity Catalog).
Statements and file reads run on a fake clock: the fake warehouse holds a submit for `min(wait_timeout, run
time)`, honours `on_wait_timeout`, and raises `ReadTimeout` when the hold is longer than the request's timeout;
a DBFS read and each read of a trickling download take time, and one slower than its (socket) timeout raises
(`tests/test_deadline.py`). `tests/test_sqlguard.py` fuzzes each SQL layer alone against a reference Spark
reading. No real workspace or token is used.
