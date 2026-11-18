# aisquare_pipe_bigquery: Google BigQuery (read-only, SELECT-only)

A toolkit (`bigquery`) and a source (`bigquery-query-source`) that read a customer's BigQuery data warehouse with a
service account key: list datasets and tables, describe a table, read a table's rows, and run ONE read-only
GoogleSQL `SELECT` at a time, inside byte, row and time caps. Each source envelope carries the version markers a
caller needs to re-read when the data changes. This is the native replacement for Composio's `GOOGLEBIGQUERY`
stop-gap (GoodRx Smart Connectors, item 3c).

Ships inside the `aisquare-pipe` distribution: `pip install "aisquare-pipe[bigquery]"` (or `[full]`, or
`[toolkits]`). The extra adds two dependencies: `google-auth>=2.20,<3`, which signs the service account's key into
short-lived tokens, and `sqlglot>=26.25,<28`, which parses SQL for the SELECT-only guard. HTTP goes through `requests`
(core). Without the extra, `bigquery` and `bigquery-query-source` count as not installed.

## Native name and the Composio mapping

The toolkit is `bigquery`, a name of its own: Composio's toolkit is `googlebigquery`. They are different toolkits,
so a host maps one onto the other. pipe-as-service lists `googlebigquery` among the Composio toolkits a native
toolkit may also serve (`COMPOSIO_ALSO_SERVES`).

| Composio (`googlebigquery`) | Native (`bigquery`) | Difference |
|---|---|---|
| `GOOGLEBIGQUERY_QUERY` | `BIGQUERY_RUN_SELECT` (primary) | SELECT only; a dry run checks the bytes first; row, byte and time caps. Composio's runs DML too. |
| `GOOGLEBIGQUERY_LIST_TABLE_DATA` | `BIGQUERY_READ_TABLE_ROWS` | Pages by `start_index`; rows come back typed and keyed by column name. |
| `GOOGLEBIGQUERY_LIST_DATASETS` | `BIGQUERY_LIST_DATASETS` | |
| `GOOGLEBIGQUERY_LIST_TABLES` | `BIGQUERY_LIST_TABLES` | |
| `GOOGLEBIGQUERY_GET_TABLE_SCHEMA` | `BIGQUERY_GET_TABLE_SCHEMA` | Adds `last_modified_time`, partitioning and clustering. |

Composio's other ~58 BigQuery tools (job, table, dataset and routine writes) have no native counterpart, on purpose.

## What it does

| Tool | What it returns |
|---|---|
| `BIGQUERY_RUN_SELECT` (primary) | ONE GoogleSQL `SELECT` or `WITH ... SELECT`: the rows (objects keyed by column name), the columns' types, `row_count`, `total_rows`, `truncated`, `total_bytes_processed`, `job_id`. `max_rows` (default 100, at most 5,000). `max_bytes_processed` (default 1 GiB, or the configured ceiling when that is lower). `default_dataset` for unqualified table names. `dry_run=true` returns the byte estimate only. |
| `BIGQUERY_READ_TABLE_ROWS` | Up to `max_rows` rows of one table from `start_index`, optionally only `selected_fields`, without a query job (free). `next_start_index` continues when rows remain. Views can't be read this way. |
| `BIGQUERY_GET_TABLE_SCHEMA` | One table's columns (name, type, mode, description, nested fields), `num_rows`, `num_bytes`, partitioning, clustering and the version marker `last_modified_time`. |
| `BIGQUERY_LIST_TABLES` | The tables and views in one dataset, with their types. |
| `BIGQUERY_LIST_DATASETS` | The datasets the key can read, in the configured project or in another `project`. |

A tool returns at most 4 MiB of JSON rows. Tables are written `dataset.table` or `project.dataset.table` (backticks
allowed); datasets `dataset` or `project.dataset`. Without a project, the tenant's `project_id` is meant.

Values come back as JSON: `INT64` as numbers, `FLOAT64` as numbers (`NaN` and the infinities as strings), `BOOL` as
booleans, `TIMESTAMP` as RFC 3339 UTC with microseconds, `NUMERIC`/`BIGNUMERIC` as exact strings, `RECORD` as
objects, `REPEATED` as lists, and `DATE`, `TIME`, `DATETIME`, `GEOGRAPHY` (WKT), `JSON`, `BYTES` (base64) and the
rest as the strings BigQuery sends.

### The source

`bigquery-query-source` pull params, exactly one of:
- `table`: `dataset.table` or `project.dataset.table`, read with tabledata.list (free, no query job). Optional
  `selected_fields` and `start_index`.
- `sql`: one SELECT, guarded exactly as `BIGQUERY_RUN_SELECT`. Optional `default_dataset`, `max_bytes_processed`
  (default 1 GiB, never above the ceiling) and `job_timeout_seconds` (default 120, at most 600).

For both: `batch_size` rows per envelope (default 500, at most 10,000), `max_rows` (default 10,000, at most 100,000)
and `max_result_bytes` (JSON bytes of rows, default 25 MiB, at most 100 MiB).

Each envelope is one batch of rows: `data` is JSON `{"columns": [...], "rows": [...]}` (`application/json`, and
`schema` holds the columns too). A result with no rows is one envelope with no rows, so its markers still come out.

**Version markers** (in every envelope's `metadata`):
- `result_sha256`: SHA-256 of the whole result, the schema plus every row, whatever order the rows came in. It is
  the same on every batch of one pull and changes when the data the pull read changes.
- `table_last_modified` (RFC 3339) and `table_last_modified_ms` (BigQuery's `lastModifiedTime`): when the table last
  changed. They are read BEFORE the rows, so a change made while the rows are read shows at the next pull. For a
  `sql` pull they are the latest time over the tables the statement names, and they are set only when every one of
  those could be read as a table. A query over a view, a wildcard table, `INFORMATION_SCHEMA` or a table function
  has `result_sha256` only. `tables_last_modified` lists the times that were read.
- `version`: `<table_last_modified_ms>:<result_sha256>`, or `result_sha256` alone when there is no table time.

Store `version` and re-read when it changes. The cheap check is `BIGQUERY_GET_TABLE_SCHEMA`'s `last_modified_time`
against the stored `table_last_modified`; then pull again only when it moved. Rows streamed in through the legacy
streaming API may show before `lastModifiedTime` moves; `result_sha256` still catches them.

Also in `metadata`: `kind` (`table` or `query`), `table` or `query_sha256` (the statement's hash, never its text),
`batch_index`, `batch_count`, `first_row`, `row_count`, `batch_sha256`, `result_row_count`, `total_rows`, and for
`sql` `job_id` and `total_bytes_processed`.

**A pull that stops early says so.** At `max_rows` or `max_result_bytes`, every envelope carries `truncated`
(`max_rows` or `max_result_bytes`), and a `table` pull also `next_start_index`: pass it as `start_index` and repeat
until a pull is not truncated. Nothing is skipped or repeated while the table doesn't change. A truncated `sql` pull
can't be continued: narrow the query or raise the caps. `result_sha256` then covers the rows read only.

## Security

**SELECT only, before any request.** `BIGQUERY_RUN_SELECT` and a `sql` pull refuse anything but ONE read-only
statement before a token is even minted. Two independent checks must both pass:
1. **A lexer** that reads text as BigQuery does (backslash escapes always on in `'...'`, `"..."`, `'''...'''`,
   `"""..."""` and backtick identifiers; `--`, `#` and `/* */` comments). With those blanked, the text must be one
   statement (one trailing `;` is dropped) starting with `SELECT` or `WITH`, naming none of `INSERT`, `UPDATE`,
   `DELETE`, `MERGE`, `TRUNCATE`, `CREATE`, `DROP`, `ALTER`, `UNDROP`, `GRANT`, `REVOKE`, `EXPORT`, `LOAD`,
   `DECLARE`, `SET`, `BEGIN`, `COMMIT`, `ROLLBACK`, `TRANSACTION`, `CALL`, `EXECUTE`, `IMMEDIATE`, `ASSERT`,
   `RAISE`, `RETURN`, `LOOP`, `WHILE`, `LEAVE`, `ITERATE`, `BREAK`, `CONTINUE`, `INTO`. A column with one of those
   names must be quoted in backticks. As in BigQuery's tokenizer, a line ends at a CR or an LF, a bare CR
   included: `--` and `#` comments end at either, and a one-line string or backtick identifier holding either is
   refused. GoogleSQL `/* */` comments don't nest (the first `*/` ends one), so a `/*` inside one is refused.
2. **A parser** (sqlglot `>=26.25`, BigQuery dialect; 25.3 to 26.24 read `/* */` comments as nested): exactly
   one statement whose root is a query, with no write, DDL, scripting or transaction node anywhere in its tree.
   Text it can't parse is refused. Its "unsupported syntax" warning, which would log the SQL, is switched off. CI
   runs this suite a second time at the sqlglot floor (`scripts/dep_floor.py`), not only at the newest sqlglot.

So DML, DDL, DCL, scripts (`DECLARE`, `BEGIN ... END`, `EXECUTE IMMEDIATE`, `CALL`, `ASSERT`...) and several
statements are refused (`invalid_arguments`), and the message never echoes the SQL. Google adds its own layers
behind these:
- The token's scope is `bigquery.readonly`. Every endpoint the connector calls accepts it; `jobs.insert` (load,
  extract and copy jobs) and `jobs.cancel` don't.
- The roles grant no write: `roles/bigquery.dataViewer` reads data, `roles/bigquery.jobUser` only runs jobs.
- The dry run's `statementType` must be `SELECT`; any other is refused before the job starts.

**Caps.**
- **Bytes processed (cost).** A free dry run first: a query that would process more than `max_bytes_processed` is
  refused before it runs. The job then carries `maximumBytesBilled` equal to the same cap, so BigQuery itself refuses
  to bill more, even when the estimate was low (`bytesBilledLimitExceeded`, nothing billed). The cap defaults to
  1 GiB; a caller may ask for more, up to the tenant's `max_bytes_billed` ceiling (default 10 GiB, at most 1 TiB).
  It is at least 10 MiB, BigQuery's minimum charge per table.
- **Rows and bytes returned.** `max_rows`, and at most 4 MiB of JSON rows from a tool (`max_result_bytes`, at most
  100 MiB, for the source). A result cut short says `truncated`.
- **Time.** A tool call fits inside the host's `timeout_seconds`, all of its requests together: each one, the token
  POST included, gets the time left (at most 30 s), and a call with no time left fails `timeout` before it sends
  anything more. Every request starts inside the budget and gets at most the time left (requests bounds each connect
  and each socket read, so one request in flight may finish a little after it). The query job's deadline is the
  budget left when it starts (60 s without a budget; `job_timeout_seconds` for the source), sent as `jobTimeoutMs`
  so BigQuery stops the job then too. Polling stops at that deadline with `timeout`.

**Server-controlled.** The key, `project_id` (where every job runs and is billed), `location` and the
`max_bytes_billed` ceiling come only from the tenant's server-side blob. The tool schemas reject them
(`additionalProperties: false`), and the source refuses them as pull params.

**IAM is the boundary.** A table reference or a query may name another project's data (`other-proj.ds.table`), as
any BigQuery client can; the key reads only what its roles grant. So grant `roles/bigquery.dataViewer` on the
datasets (or tables) AISquare may read, never on a project (checklist step 1).

**Application Default Credentials are never used**: no `google.auth.default()`, no `GOOGLE_APPLICATION_CREDENTIALS`,
no metadata server. A missing or malformed blob fails closed with "BigQuery is not configured: <what is wrong>"
(`not_connected`) and makes no request. So does a key that can't sign (not an RSA service account key). The key's
`token_uri` must be Google's token endpoint and its `universe_domain` `googleapis.com`. Every API call goes to
`https://bigquery.googleapis.com`. Redirects are not followed. Errors and logs never carry the key, the assertion or a
token; the connector's own logs carry no SQL, table names or row values.

**Tokens:** minted with ONE POST to the token endpoint, with no hidden retries; cached in memory per key until five
minutes before they expire; a token BigQuery rejects (401) is dropped, and a call whose cached token was rejected
retries once with a new one (a retried `jobs.query` keeps its `requestId`, so BigQuery runs it once).

| Failure | `error_code` | Meaning |
|---|---|---|
| "BigQuery is not configured: ..." | `not_connected` | The secret is missing or malformed (the message says what). Fix the secret. |
| "Google did not issue a token ...", "BigQuery failed (401 ...)" | `unauthorized` | Google refused the key: deleted, disabled or expired key, or a disabled service account. The customer makes a new key. |
| "... lacks roles/bigquery.dataViewer ..." / "... lacks roles/bigquery.jobUser ..." | `forbidden` | The role is missing, or on another dataset or project. |
| "... VPC Service Controls perimeter ..." | `forbidden` | The project is in a perimeter with no ingress rule for the service account. |
| "... billing is not enabled on project ..." | `forbidden` | The job project has no billing account. |
| "only one SELECT (or WITH ... SELECT) may run: ..." | `invalid_arguments` | The SQL is not one read-only query. |
| "the query would process N bytes, over max_bytes_processed ..." / `bytesBilledLimitExceeded` | `invalid_arguments` | The byte cap. Filter on the partition column, select fewer columns, or raise the cap (up to the ceiling). |
| `invalidQuery`, `responseTooLarge`, `resourcesExceeded` | `invalid_arguments` | Fix the query (add a `LIMIT`, simplify it). |
| `rateLimitExceeded`, `quotaExceeded` (BigQuery sends them as 403) | `rate_limited` | Retry later. |
| a timeout, "could not reach ...", `backendError`, a 5xx | `timeout`, `upstream_error` | Google or the network. Retryable. The credential is fine. |
| "Google refused the token request's issue and expiry times: this host's clock is off ..." | `upstream_error` | Our clock is off. Fix NTP on the host. |

**Not supported:**
- Legacy SQL (every query runs as GoogleSQL), query parameters, sessions and multi-statement scripts.
- Reading a view, materialized view or external table with `BIGQUERY_READ_TABLE_ROWS` or a `table` pull: use a SELECT.
- Continuing a truncated `BIGQUERY_RUN_SELECT` result page by page: narrow the query.
- Cancelling a running job: the `bigquery.readonly` scope can't. The job deadline (`jobTimeoutMs`) stops it instead.

**Later (not built):**
- Per-person OAuth through AISquare's verified Google OAuth app (scope `bigquery.readonly`), so each user's own
  BigQuery permissions apply instead of one service account's. That would make the toolkit `NATIVE_OAUTH`.
- Workload Identity Federation from our AWS principal to the customer's service account, which avoids a long-lived
  key.

## Registration checklist

### The customer: who can do it

The steps need one admin, or several, holding:
- `roles/iam.serviceAccountAdmin` (or `roles/iam.serviceAccountCreator`) on the job project, to create the service
  account.
- `roles/resourcemanager.projectIamAdmin` on the job project, to grant `roles/bigquery.jobUser`.
- `roles/bigquery.dataOwner` (or `roles/bigquery.admin`) on each dataset AISquare may read, to grant
  `roles/bigquery.dataViewer` on it.
- `roles/iam.serviceAccountKeyAdmin` on the job project or the service account, to create the key.
- Only behind VPC Service Controls: `roles/accesscontextmanager.policyEditor` on the access policy.

Check these organization settings first:
- **Key creation may be blocked.** In organizations created on or after 3 May 2024, Google enforces
  `iam.managed.disableServiceAccountKeyCreation` by default (older organizations may set the legacy constraint
  `iam.disableServiceAccountKeyCreation`). Step 5 then fails until an org-policy admin (`roles/orgpolicy.policyAdmin`)
  makes an exception for the job project.
- **Keys may expire.** If `iam.serviceAccountKeyExpiryHours` is set, the key expires after that time and the
  connector goes `unauthorized`. Put the rotation (below) on a calendar ahead of the expiry.
- **VPC Service Controls.** If the job project or a data project is inside a perimeter that restricts
  `bigquery.googleapis.com`, AISquare's calls come from outside it and get 403 "Request is prohibited by
  organization's policy". Add an ingress rule for the service account with `serviceName: bigquery.googleapis.com`
  on each of those projects: `methodSelectors: [{method: "*"}]`, or permission selectors such as
  `bigquery.jobs.create` and `bigquery.tables.getData` if your policy admin lists them one by one. Apply it with `gcloud access-context-manager perimeters update <perimeter> --policy=<policy-id>
  --set-ingress-policies=<file>`. That flag REPLACES the perimeter's ingress rules, so the file must also list the
  rules that exist today.

### The customer: steps

1. **Decide what AISquare may read, and where its queries run.**
   - The **data**: grant on the datasets that hold only what AISquare may read (step 4), never on a project.
     `roles/bigquery.dataViewer` on a dataset covers every table and view in it. To share some tables or some rows
     only, put authorized views in a dedicated dataset (`aisquare_shared`, say) and grant on that dataset alone.
   - The **job project**: every query runs as a job in ONE project and is billed there (on-demand, by bytes
     processed). That is `project_id`. It may be the data's project or another one. A query reads data in other
     projects when the data's datasets grant `dataViewer`.
   - **Cost**: tell AISquare the most one query may process (`max_bytes_billed`; we default to 10 GiB). For a hard
     daily limit too, set the job project's custom quota "Query usage per day per user" for the service account.
   - **Location**: if the data lives in one location (`US`, `EU`, `europe-west2`...), tell AISquare; jobs then run
     there.
2. **Create a service account** in the job project:
   `gcloud iam service-accounts create aisquare-bq-reader --project=<job-project> --display-name="AISquare read-only (BigQuery)"`.
3. **Grant it `roles/bigquery.jobUser` on the job project** (it may run query jobs; it reads nothing by itself):
   `gcloud projects add-iam-policy-binding <job-project> --member=serviceAccount:aisquare-bq-reader@<job-project>.iam.gserviceaccount.com --role=roles/bigquery.jobUser`.
4. **Grant it `roles/bigquery.dataViewer` on each dataset** AISquare may read, in the BigQuery console's query editor
   (or with `bq query --nouse_legacy_sql`), once per dataset:

   ```sql
   GRANT `roles/bigquery.dataViewer`
   ON SCHEMA `<data-project>.<dataset>`
   TO "serviceAccount:aisquare-bq-reader@<job-project>.iam.gserviceaccount.com";
   ```

   For single tables instead, write ``ON TABLE `<data-project>.<dataset>.<table>` ``. Never grant `dataViewer`,
   `dataEditor`, `user` or `admin` on a project or the organization.
5. **Create a JSON key**:
   `gcloud iam service-accounts keys create key.json --iam-account=aisquare-bq-reader@<job-project>.iam.gserviceaccount.com`.
   Make one key per AISquare environment (one for production, one for staging), never the production key in staging.
6. **Hand AISquare devops** key.json, the job project id, and, if any, the location and the byte ceiling.
   - Hand over key.json only through AISquare's team vault (1Password): devops invites your admin as a guest to a
     vault for this handoff, and you store key.json there as a document. Never by email, chat or a ticket.
   - Delete your local copy of key.json afterwards.

Before handing over, you can check the grants as the service account. The tester needs
`roles/iam.serviceAccountTokenCreator` on it; `bq` follows gcloud's impersonation setting:

```bash
gcloud config set auth/impersonate_service_account aisquare-bq-reader@<job-project>.iam.gserviceaccount.com
bq --project_id=<job-project> query --nouse_legacy_sql --dry_run 'SELECT 1 FROM `<data-project>.<dataset>.<table>`'
bq --project_id=<job-project> query --nouse_legacy_sql --dry_run 'SELECT 1 FROM `<data-project>.<other-dataset>.<table>`'
gcloud config unset auth/impersonate_service_account
```

The first dry run should succeed; the second, against a dataset AISquare must NOT read, should fail with "Access
Denied".

**Offboarding** (when AISquare should stop reading):
1. Revoke the dataset grants, once per dataset:

   ```sql
   REVOKE `roles/bigquery.dataViewer`
   ON SCHEMA `<data-project>.<dataset>`
   FROM "serviceAccount:aisquare-bq-reader@<job-project>.iam.gserviceaccount.com";
   ```

2. Remove the job role:
   `gcloud projects remove-iam-policy-binding <job-project> --member=serviceAccount:aisquare-bq-reader@<job-project>.iam.gserviceaccount.com --role=roles/bigquery.jobUser`.
3. Delete the key (`gcloud iam service-accounts keys delete <key-id> --iam-account=aisquare-bq-reader@<job-project>.iam.gserviceaccount.com`)
   or the whole service account (`gcloud iam service-accounts delete aisquare-bq-reader@<job-project>.iam.gserviceaccount.com`).

Revoking the grants stops every read, a token AISquare already holds included, once the IAM change takes effect
(usually within a few minutes).

### AISquare devops

**What has to be true first:**
1. pipe-as-service runs credential-driven routing (task #30), with `bigquery-query` → `bigquery` in its
   `TENANT_BLOBS` (`credentials/catalog.py`), so the source reads the same secret as the toolkit. Without that row,
   `bigquery-query-source` looks for a secret named `bigquery-query`.
2. Its image has a pipe release that contains `bigquery` (`aisquare-pipe[toolkits]`, from the first release with this
   connector).

**Where pipe-service runs today.** Production is on ECS only: aisquare-releases has no prod pipe-service
(`environments/prod/services/` holds explainability-gateway and praxis), so GoodRx production follows the ECS path.
`stg` and `dev` run on EKS, and also on ECS (`stg`) until the cutover. Use one customer key per environment (step 5).

**Turning BigQuery on for a tenant is a deploy, today.** Every environment runs the env secrets backend
(`PIPE_SERVICE_SECRETS_BACKEND=env`). The blob reaches the service only as the env var
`PIPE_SECRET__<TENANT>__BIGQUERY__JSON`. Fill the secret first, then add the entry that reads it:

- **ECS** (production, and `stg` until the cutover):
  1. Create the secret `ais-pipe-service/<env>/bigquery-<tenant>` in the task definition's region (prod
     `us-east-2`, stg `us-east-1`), holding the JSON below. The task execution role reads only
     `ais-pipe-service/<env>/*` in its own region, so a secret anywhere else stops the task from starting.
  2. Add to the task definition's `secrets:` (`deploy/ecs/<env>/task-definition-pipe-service.json` in
     pipe-as-service), in the same ARN form as the entries already there:

     ```json
     {
       "name": "PIPE_SECRET__<TENANT>__BIGQUERY__JSON",
       "valueFrom": "arn:aws:secretsmanager:<region>:<account>:secret:ais-pipe-service/<env>/bigquery-<tenant>"
     }
     ```

     The new task-definition revision (the pipe-as-service Deploy workflow) is the deploy. Terraform ignores
     `container_definitions`, so the workflow owns them.
- **EKS** (`stg`, `dev`): the value never goes into Secrets Manager by hand. aisquare-infra creates the secret from
  `supplied-secrets.json` and its `secrets-nonprod` workflow fills it from the GitHub environment `nonprod-secrets`
  (and runs again after every `apply-nonprod`). A value edited in Secrets Manager is overwritten with the GitHub one
  at the next sync, and a listed secret whose GitHub value is missing stops the sync for EVERY secret.
  1. Store the value: `gh secret set BIGQUERY_<TENANT>_JSON --env nonprod-secrets --repo AISquare-Studio/aisquare-infra < blob.json`
     (the JSON below, from a file that is deleted afterwards). Each environment gets its own key, so its own GitHub
     secret: `BIGQUERY_<TENANT>_JSON` for `stg`, `DEV_BIGQUERY_<TENANT>_JSON` for `dev` (as
     `DEV_INTERNAL_AUTH_TOKEN` is).
  2. In aisquare-infra, add to `live/nonprod/data/supplied-secrets.json`:

     ```json
     {
       "nonprod/<env>/pipe-service/bigquery-<tenant>": {
         "github_secret": "BIGQUERY_<TENANT>_JSON",
         "description": "PIPE_SECRET__<TENANT>__BIGQUERY__JSON of the <env> pipe service: the BigQuery service account blob of <tenant>"
       }
     }
     ```

     and its line to the `env` of the sync step in `.github/workflows/secrets-nonprod.yml`:
     `BIGQUERY_<TENANT>_JSON: ${{ secrets.BIGQUERY_<TENANT>_JSON }}`. Merge (the GitHub secret is already set, so the
     sync can't fail on it), let `apply-nonprod` create the secret, and check that `secrets-nonprod` ran green.
  3. In aisquare-releases, add to `environments/<env>/services/pipe-service/release.yaml` (written by people; never
     `values.yaml`, which the pipe-as-service build workflow overwrites):

     ```yaml
     secretEnv:
       PIPE_SECRET__<TENANT>__BIGQUERY__JSON: { from: pipe-service/bigquery-<tenant> }
     ```

     Helm merges it with the `secretEnv` of `values.yaml`. A code owner merging that PR is the deploy; Argo CD syncs
     the ExternalSecret (key `<secrets.prefix>/pipe-service/bigquery-<tenant>`) before the Deployment, so a secret
     that has no value yet holds the release.

`<TENANT>` is the tenant id upper-cased, with every run of characters other than A-Z and 0-9 written as one `_` and
any `_` at either end dropped (`acme-corp.eu` gives `PIPE_SECRET__ACME_CORP_EU__BIGQUERY__JSON`). The value is read
when the pod or task starts.

**Never switch the secrets backend for BigQuery.** `PIPE_SERVICE_SECRETS_BACKEND=aws` moves EVERY secret the service
reads, the shared Composio key included, and a missing secret reads as no key: flipping it "for BigQuery" takes
Composio down for every connector in that environment. Moving to the aws backend is a whole-service migration
(pipe-as-service `DEPLOYMENT.md`). After that migration, the tenant's secret is
`<PIPE_SERVICE_SECRETS_PREFIX>/tenants/<tenant>/bigquery`, picked up within about 60 s with no deploy.

**The secret's value**, one JSON object, read for both `bigquery` and `bigquery-query-source`:

```json
{
  "service_account_json": {
    "type": "service_account",
    "project_id": "<job-project>",
    "private_key_id": "<from key.json>",
    "private_key": "-----BEGIN PRIVATE KEY-----\n...\n-----END PRIVATE KEY-----\n",
    "client_email": "aisquare-bq-reader@<job-project>.iam.gserviceaccount.com",
    "client_id": "<from key.json>",
    "token_uri": "https://oauth2.googleapis.com/token",
    "universe_domain": "googleapis.com"
  },
  "project_id": "<job-project>",
  "location": "US",
  "max_bytes_billed": 10737418240
}
```

`service_account_json` is key.json as it was downloaded (an object, or the same JSON as a string). `project_id` is
required: the project jobs run in. `location` (`US`, `EU`, a region) and `max_bytes_billed` (bytes, 10 MiB to
1 TiB, default 10 GiB) are optional. After filling it, delete the vault item and any local copy, then remove the
customer's guest access and the handoff vault. Until the secret is read, pipe-as-service answers 422
`connector_unavailable` / `not_configured`, naming "credentials for bigquery".

**Rotation**, at least before any key expiry:
1. The customer creates a new key for the same service account (step 5) and hands it over (step 6).
2. Replace `service_account_json` in the value:
   - ECS: put the new value into `ais-pipe-service/<env>/bigquery-<tenant>`, then start new tasks (force a new
     deployment of the service) and wait until the old tasks are gone.
   - EKS: `gh secret set BIGQUERY_<TENANT>_JSON --env nonprod-secrets ...` and run `secrets-nonprod`, never an edit
     in Secrets Manager. Then wait for External Secrets (refresh interval 1 h) or refresh it now
     (`kubectl annotate externalsecret <name> -n <namespace> force-sync=$(date +%s) --overwrite`), and restart the
     Deployment (`kubectl rollout restart`): the chart has no checksum or reloader, so pods keep the old value until
     they restart.
3. Only when every pod or task runs with the new key does the customer delete the old one. Deleting it earlier breaks
   calls on pods that still hold it.

**Offboarding:**
- ECS: remove the task definition's `secrets:` entry (a deploy), then delete the secret.
- EKS: remove the `secretEnv` entry from `release.yaml` (a deploy), then remove the `supplied-secrets.json` entry
  and the `secrets-nonprod.yml` env line (one PR), and `gh secret delete BIGQUERY_<TENANT>_JSON --env nonprod-secrets
  --repo AISquare-Studio/aisquare-infra`. Deleting the Secrets Manager secret by hand while the entry is listed does
  nothing lasting: the next apply re-creates it and the sync refills it.
- Tell the customer to do their offboarding (above) as well: deleting our copy does not revoke the key.

## Tests

`pytest` from this directory, hermetic: core's socket guard blocks the network, and `tests/fakes.py` plays Google
(the token endpoint decodes the signed assertion; the BigQuery API serves in-memory datasets and tables and canned
query results, with dry runs, polling, paging and `maximumBytesBilled` enforced). `real_session()` puts the same fake
behind a real `requests.Session`. Keys are generated per run; no real key or account is used.
