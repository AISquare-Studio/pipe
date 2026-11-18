# aisquare_pipe_databricks: Databricks (read-only, native)

A toolkit (`databricks`) and a files source (`databricks-files-source`) for one Databricks workspace: files
in Unity Catalog volumes and DBFS, read-only SQL on a SQL warehouse, and the Unity Catalog listings. It
replaces Composio's `DATABRICKS` toolkit, which has no volume download and no SQL execution (GoodRx Smart
Connectors, item 3e).

Ships inside the `aisquare-pipe` distribution: `pip install "aisquare-pipe[databricks]"` (or `[full]`, or
`[toolkits]`). The extra adds one dependency, `sqlglot>=26.25,<28`, which parses SQL so that only a SELECT
is ever sent. It is the one specifier every extra naming sqlglot shares (`scripts/dep_floor.py`), and CI runs
this suite at its floor too. HTTP goes through `requests`, which core already has. Without the extra,
`databricks` and `databricks-files-source` count as not installed.

## What it does

| Tool | What it returns |
|---|---|
| `DATABRICKS_READ_FILE` (primary) | One file from `/Volumes/<catalog>/<schema>/<volume>/...` (Files API) or `dbfs:/...` (DBFS API): its bytes as a file (text up to 1 MiB also inline as `content`), `size`, `content_type` and the version markers. `include_content=false` reads the metadata only. `max_bytes` (default 25 MiB, at most 100 MiB). |
| `DATABRICKS_LIST_FILES` | A volume or DBFS directory: `path`, `name`, `is_directory`, `size`, `modified_time`; `page_size`/`page_token` paging for volumes. |
| `DATABRICKS_RUN_SELECT` | ONE read-only query on the credential's warehouse: `columns`, `rows`, `row_count`, `truncated`. `row_limit` (default 1000, at most 10000), an 8 MiB byte cap, optional default `catalog`/`schema`, and `timeout_seconds` (5-120, default 60; a host budget lowers it), after which the statement is cancelled (see "The deadline"). |
| `DATABRICKS_LIST_CATALOGS`, `_SCHEMAS`, `_TABLES` | Unity Catalog listings (tables with their columns), paged. |

`databricks-files-source` pull params: `path` (a directory, or one file), `recursive` (at most 10 levels),
`modified_after` (RFC 3339), `max_files` (default 100, at most 1000), `max_bytes` per file (default 25 MiB, at
most 64 MiB for the source, sized for a 512 MiB pod; larger files are skipped and counted in one log line).
Each envelope carries the file's bytes and `metadata`: `filename`, `path`, `storage` (`volume` or `dbfs`),
`size`, `content_type` and the version markers.

**A failed pull** raises, as pipe-as-service answers it:
- 422, a `ValueError`: no usable credential (`DatabricksNotConfigured`, before any request), bad params, and
  what Databricks refused that the caller can fix: a path that does not exist (404) or a request it calls
  invalid (400), as `DatabricksRequestError`;
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
its own; tests pin what only one of them catches, so dropping either one fails the suite.

1. **Lexer** (no parser). It reads the text as Spark's own lexer does (`SqlBaseLexer.g4`), and refuses wherever
   two readers could disagree on where a token ends:
   - a `--` comment ends at the first CR or LF. Spark carries it on past a backslash right before the LF, and
     sqlglot doesn't, so a backslash right before a line break in a `--` comment is refused. So is a `--`
     comment a bare CR ends;
   - Spark nests `/* */` comments: a `/*` inside one is refused, and so is one that never closes. `/*+ ... */`
     is a hint, whose contents Spark reads as tokens, and so does the lexer;
   - Spark takes backslash escapes in `'...'` and `"..."`, never in backticks, and none in `r'...'`. Every
     quoted token must end in the same place whether a backslash escapes or not; if not, it is refused. So
     write a quote inside a string as `''` (or `"it's"`), not `\'`;
   - `${` is refused anywhere: Spark substitutes `${...}` variables before it reads the text, inside strings
     and comments too;
   - outside quotes and comments only ASCII, and no backslash (Spark 3 upper-cases `ı` to `I`, so `ınsert`
     would read as `INSERT`); anywhere, no control character but tab, LF and CR, and no NEL, LS or PS.

   What remains must be one statement (a trailing `;` is fine) starting with `SELECT`/`WITH` that names no
   write, DDL, session or scripting keyword (a column with such a name goes in backticks).
2. **Parser** (sqlglot, Databricks dialect):
   - exactly one statement;
   - a query at the root (`SELECT`, `WITH ... SELECT`, `UNION`/`INTERSECT`/`EXCEPT`, parenthesised);
   - no write, DDL, session, transaction or script node anywhere in the tree (`INSERT`, `UPDATE`, `DELETE`,
     `MERGE`, `CREATE`, `DROP`, `ALTER`, `SELECT ... INTO`, `SET`, `USE`, `COPY`, `CACHE`, `REFRESH`,
     `GRANT`, a `FOR SHARE`/`FOR UPDATE` lock, `TRANSFORM ... USING`, unparsed commands, ...).
   - Unparseable text is refused.
   - sqlglot parses inside core's `quiet_sqlglot()`: what it logs while it parses (a JSON path it can't read
     is logged, path and all, at WARNING) never gets out. A sqlglot older than 26.25 refuses every statement.

Both refuse calls of the functions with side effects outside the query: `http_request` and `remote_query`
(a Unity Catalog connection can write there), `reflect`/`java_method`/`try_reflect`, `secret`/`try_secret`,
`IDENTIFIER(...)` (a name built from a string), and any `ai_*` function (model serving, billed outside the
warehouse), named plainly, qualified or quoted. Both refuse `TRANSFORM ... USING '<script>'`, which runs a
command. A column named `secret` or `ai_score` is fine: only a call is refused.

A refusal is `forbidden` ("only one SELECT (or WITH ... SELECT) may run: <why>"), and the message never echoes
the SQL. The statement is then sent with `disposition: INLINE`, `row_limit` and `byte_limit`. Give the principal
read-only grants as well (defence in depth).

### The deadline

`timeout_seconds` (or the host's budget, if lower) bounds the whole call, the OAuth token included, and every
request's timeout fits what is left:

- **Up to 55 s left** (`timeout_seconds` up to 55): one synchronous call. `wait_timeout` is what is left less 5 s
  (5 to 50 s), the HTTP read timeout is `wait_timeout` + 5 s, and `on_wait_timeout` is `CANCEL`. So the
  warehouse cancels the statement itself at `wait_timeout`, even when its answer is lost (a client timeout).
  A statement can run until about 5 s before the deadline.
- **More than 55 s left** (`timeout_seconds` 56 to 120), or a host budget under 6 s: asynchronous.
  `wait_timeout` `0s` answers at once with the `statement_id`. The statement is polled until it ends, and
  cancelled when the deadline comes (2 s, or a quarter of a short deadline, is kept back for the cancel). A poll
  that fails or times out cancels it too.

The one case the connector can't cancel is an asynchronous submit whose answer (the `statement_id`) never
arrives. Step 3 of the checklist has the customer set the warehouses' `STATEMENT_TIMEOUT` for that.

## Security

- The workspace URL, the token or service principal, and the warehouse come only from the tenant's blob.
  No tool argument or pull param can set them: the strict schemas reject them, and the source refuses them
  as server-controlled.
- `workspace_url` must be https on `*.cloud.databricks.com`, `*.azuredatabricks.net` or
  `*.gcp.databricks.com`, with no port, path or userinfo. Every call goes there, and redirects are not
  followed.
- Paths must be volume (`/Volumes/c/s/v/...`) or DBFS (`dbfs:/...`) paths, with no `.`/`..` segments.
- Ambient configuration (`DATABRICKS_HOST`/`DATABRICKS_TOKEN`, `~/.databrickscfg`) is never read. A
  missing or malformed blob fails closed: "Databricks is not configured: <what>" (`not_connected`), and no
  request is made.
- Secrets never appear in errors, logs or reprs. OAuth M2M tokens are cached in memory until five minutes
  before they expire.

## Registration checklist (the customer, then us)

In the customer's Databricks workspace:

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
   - DBFS paths: the workspace's DBFS access for that principal (DBFS has no Unity Catalog grants; prefer
     volumes).
3. **Bound every statement on the warehouse side** (recommended): a workspace admin sets the SQL
   configuration parameter `STATEMENT_TIMEOUT` (the workspace's SQL warehouse settings, "SQL configuration
   parameters", or the SQL Warehouses API's workspace configuration) to `600` or less. The connector cancels
   its own statements at their deadline (at most 120 s), so this only ends one whose submit answer was lost.
   It applies to every warehouse in the workspace, so pick a value its other workloads allow (the default is
   two days).
4. **Send us** the workspace URL (e.g. `https://adb-1234567890123456.7.azuredatabricks.net`), the SQL
   warehouse id (SQL Warehouses → the warehouse → Connection details → the id in its HTTP path), and the
   client id + secret (or the PAT), over a secure channel.

On our side, store the tenant's credential blob ONCE in AWS Secrets Manager. pipe-as-service reads the
same secret for the toolkit `databricks` and the source `databricks-files-source` (its tenant-secret
mapping, `credentials/catalog.TENANT_BLOBS`: `databricks-files` -> `databricks`):

- `<PIPE_SERVICE_SECRETS_PREFIX>/tenants/<tenant>/databricks` (env backend
  `PIPE_SECRET__<TENANT>__DATABRICKS__JSON`)

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

`warehouse_id` is optional: without it `DATABRICKS_RUN_SELECT` is "not configured" and the file tools
still work. Once the secret exists, pipe-as-service serves `databricks` natively for that tenant within its
credential TTL (no deploy). Until then it falls back to Composio's `DATABRICKS` toolkit, which is
read-guarded there.

**Rotation:** create a new OAuth secret (or PAT), replace it in the secret(s), then revoke the old one in
Databricks.

## Tests

`pytest` from this directory, hermetic: core's socket guard blocks the network, and `tests/fakes.py` plays
the workspace (the OIDC token endpoint, the Files API, DBFS, statement execution and cancel, Unity Catalog).
Statements run on a fake clock: the fake warehouse holds a submit for `min(wait_timeout, run time)`, honours
`on_wait_timeout`, and raises `ReadTimeout` when the hold is longer than the request's timeout
(`tests/test_deadline.py`). `tests/test_sqlguard.py` also fuzzes the lexer against a reference Spark reading.
No real workspace or token is used.
