# aisquare_pipe_databricks: Databricks (read-only, native)

A toolkit (`databricks`) and a files source (`databricks-files-source`) for one Databricks workspace: files
in Unity Catalog volumes and DBFS, read-only SQL on a SQL warehouse, and the Unity Catalog listings. It
replaces Composio's `DATABRICKS` toolkit, which has no volume download and no SQL execution (GoodRx Smart
Connectors, item 3e).

Ships inside the `aisquare-pipe` distribution: `pip install "aisquare-pipe[databricks]"` (or `[full]`, or
`[toolkits]`). The extra adds one dependency, `sqlglot>=25,<28`, which parses SQL so that only a SELECT is
ever sent. HTTP goes through `requests`, which core already has. Without the extra, `databricks` and
`databricks-files-source` count as not installed.

## What it does

| Tool | What it returns |
|---|---|
| `DATABRICKS_READ_FILE` (primary) | One file from `/Volumes/<catalog>/<schema>/<volume>/...` (Files API) or `dbfs:/...` (DBFS API): its bytes as a file (text up to 1 MiB also inline as `content`), `size`, `content_type` and the version markers. `include_content=false` reads the metadata only. `max_bytes` (default 25 MiB, at most 100 MiB). |
| `DATABRICKS_LIST_FILES` | A volume or DBFS directory: `path`, `name`, `is_directory`, `size`, `modified_time`; `page_size`/`page_token` paging for volumes. |
| `DATABRICKS_RUN_SELECT` | ONE read-only query on the credential's warehouse: `columns`, `rows`, `row_count`, `truncated`. `row_limit` (default 1000, at most 10000), a 16 MiB byte cap, optional default `catalog`/`schema`, and `timeout_seconds` (5-120, default 60), after which the statement is cancelled. |
| `DATABRICKS_LIST_CATALOGS`, `_SCHEMAS`, `_TABLES` | Unity Catalog listings (tables with their columns), paged. |

`databricks-files-source` pull params: `path` (a directory, or one file), `recursive` (at most 10 levels),
`modified_after` (RFC 3339), `max_files` (default 100, at most 1000), `max_bytes` per file (larger ones are
skipped and counted in one log line). Each envelope carries the file's bytes and `metadata`: `filename`,
`path`, `storage` (`volume` or `dbfs`), `size`, `content_type` and the version markers.

**Version markers.** `modified_time` (RFC 3339 UTC; the Files API's Last-Modified, DBFS's modification
time), `etag` (volumes) and `sha256` of the bytes (on a read or pull). Store them and re-read the file when
they change.

## SQL is SELECT-only

Before ANY HTTP call (`sqlguard.py`), two independent checks must both pass:

1. **Parser** (sqlglot, Databricks dialect):
   - exactly one statement;
   - a query at the root (`SELECT`, `WITH ... SELECT`, `UNION`/`INTERSECT`/`EXCEPT`, parenthesised);
   - no write, DDL, session or transaction node anywhere in the tree (`INSERT`, `UPDATE`, `DELETE`,
     `MERGE`, `CREATE`, `DROP`, `ALTER`, `SELECT ... INTO`, `SET`, `USE`, `COPY`, `CACHE`, `REFRESH`,
     `GRANT`, unparsed commands, ...).
   - Unparseable text is refused.
2. **Lexer:** literals, quoted identifiers and `--`/`/* */` comments are removed (read with and without
   backslash escapes). What remains must be one statement starting with `SELECT`/`WITH` that names no write
   or session keyword.

A refusal is `forbidden` ("only one SELECT (or WITH ... SELECT) may run: <why>"). The statement is then sent
with `disposition: INLINE`, `row_limit`, `byte_limit` and `wait_timeout`. It is polled until it finishes,
and cancelled at the deadline. Give the principal read-only grants as well (defence in depth).

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
3. **Send us** the workspace URL (e.g. `https://adb-1234567890123456.7.azuredatabricks.net`), the SQL
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
the workspace (the OIDC token endpoint, the Files API, DBFS, statement execution with scripted states and
cancel, Unity Catalog). Statement polling runs on a fake clock. No real workspace or token is used.
