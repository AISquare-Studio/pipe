# aisquare_pipe_snowflake: Snowflake (read-only, SELECT-only)

A toolkit (`snowflake`) and a source (`snowflake-query-source`) that read a customer's Snowflake account as a
service user with a read-only role: list databases, schemas and tables, describe a table, and run ONE read-only
`SELECT` at a time, inside row, byte and time caps. Each source envelope carries the version markers a caller needs
to re-read when the data changes. This is the native replacement for Composio's `SNOWFLAKE` stop-gap (GoodRx Smart
Connectors, item 3d).

Ships inside the `aisquare-pipe` distribution: `pip install "aisquare-pipe[snowflake]"` (or `[full]`, or
`[toolkits]`). The extra adds two dependencies: `cryptography>=42,<60`, which signs the key-pair JWT with the
service user's RSA key, and `sqlglot>=26.25,<28`, which parses SQL for the SELECT-only guard (the same specifier
as every extra that names sqlglot; CI also runs this suite at that floor, `scripts/dep_floor.py`). HTTP goes through
`requests` (core). Without the extra, `snowflake` and `snowflake-query-source` count as not installed.

**Why the SQL API and not `snowflake-connector-python`.** Snowflake's SQL API (`/api/v2/statements`) is plain
HTTPS + JSON, so the connector needs only `requests` (already in core) and two small libraries. The Python connector
is far heavier (its own HTTP stack, `pyOpenSSL`, `asn1crypto`, `boto3`/`botocore` for stages, optional `pyarrow`),
opens its own sockets (which the hermetic tests could not fake behind a `requests.Session`), and reads ambient
configuration (`SNOWFLAKE_*` environment variables, `~/.snowflake/connections.toml`), which a per-tenant
connector must never do. The SQL API also lets the connector send ONE statement with `MULTI_STATEMENT_COUNT` 1,
a server-side timeout and a row cap on every request.

## Native name and the Composio mapping

The toolkit is `snowflake`, the same name as Composio's toolkit, so a host routes either to it (pipe-as-service
#30: native when the tenant's Snowflake secret is configured, else Composio under its read-only guard). It keeps
Composio's slugs and argument names for the read tools both have:

| Composio (`snowflake`) | Native (`snowflake`) | Difference |
|---|---|---|
| `SNOWFLAKE_EXECUTE_SQL` (`statement`, `database`, `schema_name`, `timeout`) | `SNOWFLAKE_RUN_SELECT` (primary; same arguments, plus `max_rows`) | SELECT only, checked before any call; row, byte and time caps. Composio's runs DDL and DML too. |
| `SNOWFLAKE_SHOW_DATABASES` | `SNOWFLAKE_SHOW_DATABASES` | Same arguments (`like_pattern`, `starts_with`, `limit`, `from_name`, `terse`, `history`, `timeout`); `next_from_name` pages. |
| `SNOWFLAKE_SHOW_SCHEMAS` | `SNOWFLAKE_SHOW_SCHEMAS` | Same, plus `database`. |
| `SNOWFLAKE_SHOW_TABLES` | `SNOWFLAKE_SHOW_TABLES` | Same, plus `database` and `schema`. |
| (none) | `SNOWFLAKE_DESCRIBE_TABLE` | Columns, kind, size and the version marker `last_altered`. |

Composio's `role` and `warehouse` arguments are server-controlled here: a call that sends them is refused
(`invalid_arguments`). `SNOWFLAKE_EXECUTE_SQL` needs one row in pipe-as-service's `NATIVE_TOOL_RENAMES`
(`SNOWFLAKE_EXECUTE_SQL` → `SNOWFLAKE_RUN_SELECT`; the arguments need no translation). Composio's other tools
(warehouse, user and grant management) have no native counterpart, on purpose.

## What it does

| Tool | What it returns |
|---|---|
| `SNOWFLAKE_RUN_SELECT` (primary) | ONE `SELECT` or `WITH ... SELECT`: the rows (objects keyed by column name), the columns' types, `row_count`, `truncated`, `query_id`. `max_rows` (default 100, at most 5,000). `database` and `schema_name` for names written without them (default: the configured ones). `timeout` (seconds, at most 600). |
| `SNOWFLAKE_DESCRIBE_TABLE` | One table or view: its columns (name, type, nullable, default, comment), `kind`, `row_count`, `bytes`, `created`, `comment`, `clustering_key` and the version marker `last_altered`. |
| `SNOWFLAKE_SHOW_TABLES` | The tables in a schema (default: the configured one) or a database, with kind, row count, size, owner and comment. |
| `SNOWFLAKE_SHOW_SCHEMAS` | The schemas in a database (default: the configured one; without one, every database the role sees). |
| `SNOWFLAKE_SHOW_DATABASES` | The databases the role sees. |

The SHOW tools list `limit` objects (default 100, at most 1,000) and answer `has_more` and `next_from_name`: pass
it back as `from_name` for the next page. A tool returns at most 4 MiB of JSON rows. Names are written as in SQL:
`DB.SCHEMA.TABLE`, `SCHEMA.TABLE` or `TABLE` (the configured database and schema fill the rest); unquoted names are
case-insensitive, `"Quoted"` names keep their case.

Values come back as JSON: `NUMBER` with scale 0 as integers, `NUMBER` with a scale as exact strings (`"1234.50"`),
`FLOAT` as numbers (`NaN` and the infinities as strings), `BOOLEAN` as booleans, `DATE` as `YYYY-MM-DD`, `TIME` as
`HH:MM:SS.fff`, `TIMESTAMP_NTZ` as ISO 8601 without a zone, `TIMESTAMP_LTZ` as ISO 8601 UTC (`Z`), `TIMESTAMP_TZ`
with its offset (`-06:00`), `VARIANT`/`OBJECT`/`ARRAY` as the JSON they hold, and `BINARY` (hex), `GEOGRAPHY` and
the rest as the strings Snowflake sends. Fractions keep the column's scale, so nothing is rounded. A repeated column
name gets a unique key (`A`, `A_2`) and its `source_name`.

### The source

`snowflake-query-source` pull params, exactly one of:
- `table`: `DB.SCHEMA.TABLE` (or shorter, with the default database and schema). The connector writes the SELECT
  itself, every name a quoted identifier. Optional `columns` (default all), `order_by` (column names: a stable order)
  and `start_index` (needs `order_by`).
- `sql`: one SELECT, guarded exactly as `SNOWFLAKE_RUN_SELECT`.

For both: `database` and `schema` (the defaults for names written without them), `batch_size` rows per envelope
(default 500, at most 10,000) and `max_batch_bytes` bytes of envelope data (default 256 KiB, what pipe-as-service
returns inline in a synchronous pull, which answers 400 for a larger envelope without a blob store; at least 1 KiB,
at most 64 MiB; a single larger row still gets its own envelope), whichever comes first, `max_rows` (default 10,000, at most 100,000), `max_result_bytes` (JSON bytes of
rows, default 25 MiB, at most 64 MiB, sized for a 512 MiB pod: see [Memory](#memory)) and `timeout_seconds` (the
statement deadline, default 120, at most 600).

Each envelope is one batch of rows: `data` is JSON `{"columns": [...], "rows": [...]}` (`application/json`, and
`schema` holds the columns too). A result with no rows is one envelope with no rows, so its markers still come out.

**Version markers** (in every envelope's `metadata`):
- `result_sha256`: SHA-256 of the whole result, the columns plus every row, whatever order the rows came in. It is
  the same on every batch of one pull and changes when the data the pull read changes.
- `table_last_altered`: the table's `INFORMATION_SCHEMA.TABLES.LAST_ALTERED` (ISO 8601 UTC). It is read BEFORE the
  rows, so a change made while they are read shows at the next pull. For a `sql` pull it is the latest over the
  tables the statement names, and it is set only when every one of those is a base table the role can see. A query
  over a view, a table function or `INFORMATION_SCHEMA` has `result_sha256` only. `tables_last_altered` lists the
  times that were read.
- `version`: `<table_last_altered>|<result_sha256>`, or `result_sha256` alone when there is no table time.

Store `version` and re-read when it changes. The cheap check is `SNOWFLAKE_DESCRIBE_TABLE`'s `last_altered` against
the stored `table_last_altered`; pull again only when it moved. `LAST_ALTERED` moves on every DML or DDL, and also
on Snowflake's background maintenance (reclustering, ...): it never misses a change, and when it moves without one,
`result_sha256` stays the same.

Also in `metadata`: `kind` (`table` or `query`), `table` or `query_sha256` (the statement's hash, never its text),
`query_id`, `batch_index`, `batch_count`, `first_row`, `row_count`, `batch_sha256` and `result_row_count`.

**A pull that stops early says so.** At `max_rows` or `max_result_bytes`, every envelope carries `truncated`
(`max_rows` or `max_result_bytes`), and a `table` pull with `order_by` also `next_start_index`: pass it as
`start_index` and repeat until a pull is not truncated. Nothing is skipped or repeated while the table doesn't
change. Without `order_by` (or for `sql`) a truncated pull can't be continued: add `order_by`, narrow the query or
raise the caps. `result_sha256` then covers the rows read only.

**How a pull fails** (pipe-as-service answers by the exception's type; neither message carries Snowflake's own error
text, which can quote the SQL and name objects, because hosts log these):
- **422**, a `ValueError`: the secret is missing or malformed (`SnowflakeNotConfigured`); a bad param or name; SQL
  that is not one SELECT (`NotReadOnly`); and what Snowflake refused that the caller can fix, as
  `SnowflakeRequestError` (with Snowflake's `status`, `code` and `sql_state`): a statement that does not compile
  (with Snowflake's "line L at position P"), an object that does not exist or the role can't see, a value that
  can't be converted.
- **502** `upstream_error`, a `PipelineError` with `retry_after` when Snowflake sent `Retry-After` (a 503): a key or
  OAuth client Snowflake refused, a missing grant or warehouse, throttling, the statement's deadline, a 5xx or the
  network. The message says which, and what to fix, as the table below does.

### Memory

A pull's peak memory, measured with `tracemalloc` (`tests/test_memory.py`; the fake serves each result partition as
fresh JSON bytes, 10,000 rows a partition, and every envelope is kept, as pipe-as-service keeps a pull's envelopes for
its response):

| Pull | Rows | JSON of the rows | Peak |
|---|---|---|---|
| defaults: 10,000 rows of 50 `NUMBER` columns | 10,000 | 6.7 MiB | 47 MiB (65 MiB before) |
| defaults: 10,000 rows of 18 mixed columns | 10,000 | 3.2 MiB | 21 MiB |
| defaults: 10,000 rows of 4 wide `TEXT` columns (1 KB a row) | 10,000 | 9.9 MiB | 42 MiB |
| `max_result_bytes` 64 MiB, 50 `NUMBER` columns | 90,000 | 60 MiB | 104 MiB (367 MiB at the old 100 MiB) |
| `max_rows` 100,000, 18 mixed columns | 100,000 | 32 MiB | 53 MiB |
| `max_result_bytes` 64 MiB, wide rows | 60,000 | 59 MiB | 93 MiB |
| the same, in 50,000-row (49 MiB) partitions | 50,000 | 49 MiB | 207 MiB |

How: each parsed partition is let go before the next is read, rows are converted one at a time, and the source keeps
each row as its canonical JSON (about a fifth of the memory a row object takes; the envelopes and hashes are byte for
byte what they were). A batch's rows are let go once its envelope is built. Snowflake, not the connector, sizes the
partitions; one is fetched only while its `uncompressedSize` fits the bytes left, so one partition is at most
`max_result_bytes`. A pull peaks at about 1.35 times the rows' JSON plus about three times the largest partition
(its bytes, their text, and the parsed rows): under 256 MiB at the caps whatever the partitions. A host keeps the
envelopes too: pipe-as-service base64-encodes each (about 1.33 times more), which is why `max_result_bytes` stops at
64 MiB on a 512 MiB pod.

## Security

**SELECT only, before any request.** `SNOWFLAKE_RUN_SELECT` and a `sql` pull refuse anything but ONE read-only
statement before a token is even signed. Two independent checks must both pass:
1. **A lexer.** It blanks `'...'` strings, `"..."` identifiers and `--`, `//` and `/* */` comments, reading the text
   once with backslash escapes and once without. Both readings must agree, so write a quote inside a string as `''`,
   not `\'`. A `--` or `//` comment ends at the first CR or LF; one that a bare CR (no LF after it) ends is refused,
   and so is a `/*` inside a `/* */` comment: readers disagree on both (sqlglot ends a line at a CR, and some
   versions nest comments), and the disagreement could hide a statement from one check. The text must then be one
   statement (one trailing `;` is dropped) that starts with `SELECT` or `WITH`.
   - It may name none of `INSERT`, `UPDATE`, `DELETE`, `MERGE`, `UPSERT`, `TRUNCATE`, `CREATE`, `ALTER`, `DROP`,
     `UNDROP`, `RENAME`, `SWAP`, `CLONE`, `RESTORE`, `COPY`, `PUT`, `REMOVE`, `RM`, `LOAD`, `GRANT`, `REVOKE`,
     `USE`, `SET`, `UNSET`, `RESET`, `BEGIN`, `COMMIT`, `ROLLBACK`, `TRANSACTION`, `DECLARE`, `CALL`, `EXEC`,
     `EXECUTE`, `IMMEDIATE`, `PROCEDURE`, `ANALYZE`, `INTO`, `NEXTVAL`. A column with one of those names must be in
     double quotes, except `NEXTVAL`, which is refused quoted too (`s."NEXTVAL"` still takes a sequence's next value).
   - It may hold none of: `$` (`$$` strings, `SYSTEM$` functions, session variables, stage columns), `@` (stages),
     `->`/`->>` (the flow operator chains statements), `|>`, backticks, or a backslash outside a string.
   - A table function (`TABLE(f(...))`, `LATERAL f(...)`) must be `FLATTEN`, `SPLIT_TO_TABLE`,
     `STRTOK_SPLIT_TO_TABLE` or `GENERATOR`, unqualified (`s.FLATTEN(...)` is a user-defined function): Snowflake can
     call a stored procedure from `FROM TABLE(...)`, and a procedure may write.
2. **A parser** (sqlglot, Snowflake dialect): exactly one statement whose root is a query, with no write, DDL,
   session, transaction, bind-variable or unparsed node anywhere in its tree, no `NEXTVAL` name (quoted or not), and
   no table function outside that list (a qualified one is outside it). Text it can't parse is refused. sqlglot logs
   pieces of the SQL it parses (its "unsupported syntax" warning, and "Invalid JSON path syntax" with the path for any
   JSON key with a dash, as in `GET_PATH(v, 'a-b')` or `v:'x-y'`), so the guard parses inside
   `aisquare.pipe.core.sql.quiet_sqlglot()`, the helper every SQL guard shares: a filter on the `sqlglot` logger
   drops every record it emits while the guard parses, in that context only, so not even Python's
   `logging.lastResort` prints them. Nothing is disabled globally, and sqlglot logs as usual everywhere else. On a
   sqlglot older than 26.25 (another package can bring one in) the guard refuses every statement.

So DML, DDL, DCL, `COPY`, `CALL`, Snowflake Scripting blocks, `EXECUTE IMMEDIATE` and several statements are
refused (`invalid_arguments`), and the message never echoes the SQL. Snowflake adds its own layers behind these:
- Every request sets `MULTI_STATEMENT_COUNT` to 1, so Snowflake itself refuses a second statement.
- The role holds read-only grants (checklist step 3), and the user has no other role: `DEFAULT_SECONDARY_ROLES` is
  empty, so no other role's privileges join in.
- Every statement carries `QUERY_TAG` `aisquare-pipe`: the customer finds AISquare's queries in `QUERY_HISTORY`.

**Caps.**
- **Rows and bytes returned.** `max_rows` (sent as `ROWS_PER_RESULTSET`, so Snowflake sends no more, and cut again
  here), and at most 4 MiB of JSON rows from a tool (`max_result_bytes`, at most 64 MiB, for the source). Result
  partitions are read only while they fit. A result cut short says `truncated`.
- **Time.** A tool call fits inside the host's `timeout_seconds`, all of its requests together. Each request, the
  OAuth token POST included, gets the time left (at most 30 s), and a call with less than a second left fails
  `timeout` before it submits anything. Every request starts inside the budget and gets at most the time left
  (requests bounds each connect and each socket read, so one request in flight may finish a little after it). The
  statement's deadline (the budget left, or `timeout` / `timeout_seconds` when lower; 60 s for a SELECT and 30 s for
  metadata without a budget, 120 s for the source) is sent as the statement's `timeout`, so Snowflake stops it then
  too. Polling stops at the deadline, the statement is cancelled, and the call fails `timeout`.
- **Cost.** Snowflake bills warehouse time, not bytes scanned. The deadline bounds each statement. The warehouse's
  own `STATEMENT_TIMEOUT_IN_SECONDS`, its size and a resource monitor bound the rest (checklist step 2).

**Server-controlled.** The account, the user, the key or OAuth client, the role and the warehouse come only from the
tenant's server-side blob. The tool schemas reject them (`additionalProperties: false`), and the source refuses them
as pull params. `database` and `schema` are defaults that a tool argument or pull param may override, as any SQL
may name another database; the role's grants are the boundary.

**Every request goes to `https://<account>.snowflakecomputing.com/api/v2/statements`.** `account` must be a
Snowflake account identifier, never a URL or a host, so nothing else can be reached. The OAuth token endpoint is
the blob's `token_url` (https, a DNS name, never an IP address). Redirects are not followed.

**Ambient configuration is never used**: no `SNOWFLAKE_*` environment variables, no `~/.snowflake/connections.toml`
and no `~/.snowsql/config`. A missing or malformed blob fails closed with "Snowflake is not configured: <what is
wrong>" (`not_connected`) and makes no request. So does a key that can't sign (not RSA, under 2048 bits, or an
encrypted key without its passphrase). Errors and logs never carry the key, the JWT, a token or the client secret.
The connector's own logs carry no SQL, table names or row values.

**Tokens.**
- Key-pair: each call signs a fresh JWT, RS256, valid 59 minutes. Its `iss` is
  `<ACCOUNT>.<USER>.SHA256:<public key fingerprint>` and its `sub` is `<ACCOUNT>.<USER>`.
- OAuth client: the token comes from ONE POST to `token_url` (client credentials, no hidden retries) and is cached in
  memory per client until five minutes before it expires. When Snowflake rejects a cached token, it is dropped and
  the call retries once with a new one. A retried submission keeps its `requestId` and sets `retry=true`, so
  Snowflake runs it once.

| Failure | `error_code` | Meaning |
|---|---|---|
| "Snowflake is not configured: ..." | `not_connected` | The secret is missing or malformed (the message says what). Fix the secret. |
| "Snowflake failed (401, 390144) ... refused the key-pair JWT ..." | `unauthorized` | The user's `RSA_PUBLIC_KEY` isn't this key's, the account identifier is wrong, or the user is disabled. |
| "the OAuth token endpoint refused the client credentials ...", "... External OAuth ..." | `unauthorized` | Wrong or expired client secret, or a token the integration doesn't accept. |
| "... role X lacks a grant ..." / "... cannot use warehouse ..." | `forbidden` | A missing grant (checklist step 3), or the role isn't granted to the user. |
| "... does not exist, or role X has no grant on it" | `not_found` | Snowflake says the same for both: check the name, then the grants. |
| "only one SELECT (or WITH ... SELECT) may run: ..." | `invalid_arguments` | The SQL is not one read-only query. |
| "the statement failed (422, ...)" (a compilation or runtime error) | `invalid_arguments` | Fix the query. |
| "... deadline ..." / Snowflake code 000630 or 000604 | `timeout` | The statement ran out of time. Narrow it, or ask for a longer `timeout`. |
| 429 | `rate_limited` | Retry after `retry_after`. |
| "could not reach ...", a 5xx | `upstream_error` / `timeout` | Snowflake or the network. Retryable. The credential is fine. |

**Not supported:**
- Bind variables, session variables, Snowflake Scripting, stored procedures and user-defined table functions.
- Stages: listing and reading staged files (`LIST @stage`, `GET`) is not built. GoodRx has not yet said whether
  policies live in stages. An external stage is a pointer to an S3, GCS or Azure bucket, and pipe's `s3` and `gcs`
  connectors read the bucket directly with narrower grants. Reading an internal stage needs `GET_PRESIGNED_URL` and
  a download from a non-Snowflake storage host. That is a design decision for when GoodRx answers.
- Continuing a truncated `SNOWFLAKE_RUN_SELECT` result page by page: narrow the query.

**Later (not built):**
- Per-person Snowflake OAuth (a `CREATE SECURITY INTEGRATION ... TYPE = OAUTH` app each user consents to), so each
  user's own Snowflake role applies instead of one service user's. That would make the toolkit `NATIVE_OAUTH`.
- Stage file tools (above), if GoodRx keeps policies as staged files.

## Registration checklist

### The customer: who can do it

- `USERADMIN` (or `SECURITYADMIN`) to create the user and the role and to grant the role to the user.
- `SECURITYADMIN`, or the owner of the database or schema, to grant `USAGE` and `SELECT`.
- `SYSADMIN` (or a role with `CREATE WAREHOUSE`) for the warehouse. `ACCOUNTADMIN` for a resource monitor.
- Optional: `SECURITYADMIN` for a network policy; `ACCOUNTADMIN` for an External OAuth security integration.

Find the account identifier (`<orgname>-<account_name>`) with
`SELECT CURRENT_ORGANIZATION_NAME() || '-' || CURRENT_ACCOUNT_NAME();`. A legacy account locator also works, in
one of Snowflake's forms: alone (`xy12345`, AWS US West Oregon), with a region for the five AWS regions written
without a cloud (`xy12345.us-east-1`, `eu-west-1`, `eu-central-1`, `ap-southeast-1`, `ap-southeast-2`), or with a
region and its cloud (`xy12345.us-east-2.aws`, `xy12345.us-central1.gcp`, `xy12345.west-europe.azure`; government
regions with `fhplus.` or `dod.` first). Any other suffix is refused. For PrivateLink, append `.privatelink`.

### The customer: steps

1. **Decide what AISquare may read.** Grant on the schema (or the tables or views) that hold only what AISquare may
   read, never a whole account. To share some rows or columns only, put secure views in a dedicated schema
   (`POLICIES.AISQUARE_SHARED`, say) and grant on that schema alone.
2. **Create a small warehouse** for AISquare's queries (it bills only while running):

   ```sql
   CREATE WAREHOUSE AISQUARE_WH
     WAREHOUSE_SIZE = XSMALL AUTO_SUSPEND = 60 AUTO_RESUME = TRUE INITIALLY_SUSPENDED = TRUE
     STATEMENT_TIMEOUT_IN_SECONDS = 600;
   -- Optional, as ACCOUNTADMIN: a monthly credit ceiling.
   CREATE RESOURCE MONITOR AISQUARE_RM WITH CREDIT_QUOTA = 10 FREQUENCY = MONTHLY
     START_TIMESTAMP = IMMEDIATELY TRIGGERS ON 90 PERCENT DO SUSPEND ON 100 PERCENT DO SUSPEND_IMMEDIATE;
   ALTER WAREHOUSE AISQUARE_WH SET RESOURCE_MONITOR = AISQUARE_RM;
   ```

3. **Create the read-only role** and grant it only reads:

   ```sql
   CREATE ROLE AISQUARE_READONLY;
   GRANT USAGE ON WAREHOUSE AISQUARE_WH TO ROLE AISQUARE_READONLY;
   GRANT USAGE ON DATABASE POLICIES TO ROLE AISQUARE_READONLY;
   GRANT USAGE ON SCHEMA POLICIES.PUBLIC TO ROLE AISQUARE_READONLY;
   GRANT SELECT ON ALL TABLES IN SCHEMA POLICIES.PUBLIC TO ROLE AISQUARE_READONLY;
   GRANT SELECT ON ALL VIEWS IN SCHEMA POLICIES.PUBLIC TO ROLE AISQUARE_READONLY;
   -- Optional: tables and views created later in that schema too.
   GRANT SELECT ON FUTURE TABLES IN SCHEMA POLICIES.PUBLIC TO ROLE AISQUARE_READONLY;
   GRANT SELECT ON FUTURE VIEWS IN SCHEMA POLICIES.PUBLIC TO ROLE AISQUARE_READONLY;
   ```

   Never grant this role `OWNERSHIP`, any `CREATE ...`, `INSERT`/`UPDATE`/`DELETE`/`TRUNCATE`, `MODIFY` or `OPERATE`
   on the warehouse, `USAGE` on procedures, functions, external functions or integrations, `MANAGE GRANTS`, or
   another role. It needs none of them.
4. **Create the service user** with AISquare's public key (step 6 of the devops part sends it to you; it is not a
   secret):

   ```sql
   CREATE USER AISQUARE_READER
     TYPE = SERVICE
     DEFAULT_ROLE = AISQUARE_READONLY
     DEFAULT_WAREHOUSE = AISQUARE_WH
     DEFAULT_SECONDARY_ROLES = ()
     RSA_PUBLIC_KEY = 'MIIBIjANBgkqh...';  -- the key's base64 body, without the BEGIN/END lines
   GRANT ROLE AISQUARE_READONLY TO USER AISQUARE_READER;
   ```

   `TYPE = SERVICE` means no password and no interactive sign-in. Check the key:
   `DESC USER AISQUARE_READER` shows `RSA_PUBLIC_KEY_FP`, which must equal the fingerprint AISquare devops sends with
   the key.
5. **If the account has a network policy**, AISquare's egress IP addresses must be allowed (AISquare devops gives
   them for each environment). Or restrict this user to them alone:

   ```sql
   CREATE NETWORK RULE AISQUARE_EGRESS MODE = INGRESS TYPE = IPV4 VALUE_LIST = ('<ip-1>', '<ip-2>');
   CREATE NETWORK POLICY AISQUARE_ONLY ALLOWED_NETWORK_RULE_LIST = ('AISQUARE_EGRESS');
   ALTER USER AISQUARE_READER SET NETWORK_POLICY = AISQUARE_ONLY;
   ```

6. **Tell AISquare devops** the account identifier, the user, role and warehouse names, and the default database
   and schema. None of these is a secret.

**Optional: OAuth instead of a key** (`auth: oauth_client`). Use it only if your policy forbids key-pair users.
Snowflake doesn't issue client-credentials tokens itself, so your identity provider (Okta, Microsoft Entra ID, Ping)
issues them and Snowflake trusts it through an External OAuth security integration:
1. In the identity provider, create a confidential client (an app registration) for AISquare, with the client
   credentials grant and a scope or app role naming the Snowflake role (`session:role:AISQUARE_READONLY`).
2. As `ACCOUNTADMIN`, create the integration. Snowflake's guide for your provider gives the exact values:

   ```sql
   CREATE SECURITY INTEGRATION AISQUARE_EXTERNAL_OAUTH
     TYPE = EXTERNAL_OAUTH ENABLED = TRUE
     EXTERNAL_OAUTH_TYPE = AZURE                 -- or OKTA, PING_FEDERATE, CUSTOM
     EXTERNAL_OAUTH_ISSUER = '<issuer>'
     EXTERNAL_OAUTH_JWS_KEYS_URL = '<jwks url>'
     EXTERNAL_OAUTH_AUDIENCE_LIST = ('<audience>')
     EXTERNAL_OAUTH_TOKEN_USER_MAPPING_CLAIM = 'sub'
     EXTERNAL_OAUTH_SNOWFLAKE_USER_MAPPING_ATTRIBUTE = 'LOGIN_NAME'
     EXTERNAL_OAUTH_ANY_ROLE_MODE = 'DISABLE';
   ALTER USER AISQUARE_READER SET LOGIN_NAME = '<the client token sub claim>';
   ```

3. Hand AISquare devops the client id, the token endpoint URL and the scope. Hand over the client secret only
   through AISquare's team vault (1Password): devops invites your admin as a guest to a vault for this handoff.
   Never by email, chat or a ticket.

**Offboarding** (when AISquare should stop reading):

```sql
ALTER USER AISQUARE_READER SET DISABLED = TRUE;   -- stops every call at once
REVOKE ROLE AISQUARE_READONLY FROM USER AISQUARE_READER;
DROP USER AISQUARE_READER;
DROP ROLE AISQUARE_READONLY;                       -- and the warehouse, monitor and policy if made for AISquare
```

### AISquare devops

**What has to be true first:**
1. pipe-as-service runs credential-driven routing (task #30). Its `TENANT_BLOBS` (`credentials/catalog.py`) maps
   `snowflake` and `snowflake-query` to `snowflake`, so the toolkit and `snowflake-query-source` read ONE secret.
   Callers that still send Composio's `SNOWFLAKE_EXECUTE_SQL` also need the `NATIVE_TOOL_RENAMES` row above.
2. Its image has a pipe release that contains `snowflake` (`aisquare-pipe[toolkits]`, from the first release with
   this connector).

**The key pair (the default).** AISquare makes it, so the private key never leaves AISquare. Make one per
environment and never put the production key in staging:

```bash
openssl genrsa 2048 | openssl pkcs8 -topk8 -inform PEM -nocrypt -out rsa_key.p8
openssl rsa -in rsa_key.p8 -pubout -out rsa_key.pub
openssl rsa -pubin -in rsa_key.pub -outform DER | openssl dgst -sha256 -binary | openssl enc -base64   # the fingerprint
```

Send the customer `rsa_key.pub` and the fingerprint (neither is secret). Put `rsa_key.p8` only into the secret
below, then delete both files.

**Where pipe-service runs today.** Production is on ECS only: aisquare-releases has no prod pipe-service
(`environments/prod/services/` holds explainability-gateway and praxis). So GoodRx production follows the ECS path.
`stg` and `dev` run on EKS, and `stg` also on ECS until the cutover.

**Turning Snowflake on for a tenant is a deploy, today.** Every environment runs the env secrets backend
(`PIPE_SERVICE_SECRETS_BACKEND=env`). The blob reaches the service only as the env var
`PIPE_SECRET__<TENANT>__SNOWFLAKE__JSON`. Fill the secret first, then add the entry that reads it:

- **ECS** (production, and `stg` until the cutover):
  1. Create the secret `ais-pipe-service/<env>/snowflake-<tenant>` in the task definition's region (prod
     `us-east-2`, stg `us-east-1`), holding the JSON below. The task execution role reads only
     `ais-pipe-service/<env>/*` in its own region, so a secret anywhere else stops the task from starting.
  2. Add to the task definition's `secrets:` (`deploy/ecs/<env>/task-definition-pipe-service.json` in
     pipe-as-service), in the same ARN form as the entries already there:

     ```json
     {
       "name": "PIPE_SECRET__<TENANT>__SNOWFLAKE__JSON",
       "valueFrom": "arn:aws:secretsmanager:<region>:<account>:secret:ais-pipe-service/<env>/snowflake-<tenant>"
     }
     ```

     The new task-definition revision (the pipe-as-service Deploy workflow) is the deploy. Terraform ignores
     `container_definitions`, so the workflow owns them.
- **EKS** (`stg`, `dev`). Never put the value into Secrets Manager by hand:
  - aisquare-infra creates the secret from `supplied-secrets.json`.
  - Its `secrets-nonprod` workflow fills it from the GitHub environment `nonprod-secrets`, and runs again after every
    `apply-nonprod`.
  - A value edited in Secrets Manager is overwritten with the GitHub one at the next sync.
  - A listed secret whose GitHub value is missing stops the sync for EVERY secret.

  1. Store the value: `gh secret set SNOWFLAKE_<TENANT>_JSON --env nonprod-secrets --repo AISquare-Studio/aisquare-infra < blob.json`
     (the JSON below, from a file that is deleted afterwards). Each environment gets its own key, so its own GitHub
     secret: `SNOWFLAKE_<TENANT>_JSON` for `stg`, `DEV_SNOWFLAKE_<TENANT>_JSON` for `dev` (as
     `DEV_INTERNAL_AUTH_TOKEN` is).
  2. In aisquare-infra, add to `live/nonprod/data/supplied-secrets.json`:

     ```json
     {
       "nonprod/<env>/pipe-service/snowflake-<tenant>": {
         "github_secret": "SNOWFLAKE_<TENANT>_JSON",
         "description": "PIPE_SECRET__<TENANT>__SNOWFLAKE__JSON of the <env> pipe service: the Snowflake service user blob of <tenant>"
       }
     }
     ```

     Also add its line to the `env` of the sync step in `.github/workflows/secrets-nonprod.yml`:
     `SNOWFLAKE_<TENANT>_JSON: ${{ secrets.SNOWFLAKE_<TENANT>_JSON }}`. Then:
     - Merge. The GitHub secret is already set, so the sync can't fail on it.
     - Let `apply-nonprod` create the secret.
     - Check that `secrets-nonprod` ran green.
  3. In aisquare-releases, add to `environments/<env>/services/pipe-service/release.yaml`. Use `release.yaml`, which
     people write, never `values.yaml`, which the pipe-as-service build workflow overwrites:

     ```yaml
     secretEnv:
       PIPE_SECRET__<TENANT>__SNOWFLAKE__JSON: { from: pipe-service/snowflake-<tenant> }
     ```

     Helm merges it with the `secretEnv` of `values.yaml`. A code owner merging that PR is the deploy. Argo CD syncs
     the ExternalSecret (key `<secrets.prefix>/pipe-service/snowflake-<tenant>`) before the Deployment, so a secret
     with no value yet holds the release.

To get `<TENANT>`, upper-case the tenant id, write each run of characters other than A-Z and 0-9 as one `_`, and drop
any `_` at either end: `acme-corp.eu` gives `PIPE_SECRET__ACME_CORP_EU__SNOWFLAKE__JSON`. The value is read when the
pod or task starts.

**Never switch the secrets backend for Snowflake.** `PIPE_SERVICE_SECRETS_BACKEND=aws` moves EVERY secret the
service reads, the shared Composio key included, and a missing secret reads as no key. Flipping it "for Snowflake"
takes Composio down for every connector in that environment. Moving to the aws backend is a whole-service migration
(pipe-as-service `DEPLOYMENT.md`). After that migration, the tenant's secret is
`<PIPE_SERVICE_SECRETS_PREFIX>/tenants/<tenant>/snowflake`, picked up within about 60 s with no deploy.

**The secret's value**, one JSON object, read for both `snowflake` and `snowflake-query-source`. Key-pair (the
default):

```json
{
  "account": "<orgname>-<account_name>",
  "user": "AISQUARE_READER",
  "auth": "keypair",
  "private_key_pem": "-----BEGIN PRIVATE KEY-----\nMIIEvQ...\n-----END PRIVATE KEY-----\n",
  "role": "AISQUARE_READONLY",
  "warehouse": "AISQUARE_WH",
  "database": "POLICIES",
  "schema": "PUBLIC"
}
```

`private_key_pem` is `rsa_key.p8` with its newlines written `\n` (an RSA key of at least 2048 bits, PKCS#8 or
PKCS#1). Add `"private_key_passphrase"` only for an encrypted key. `auth` defaults to `keypair`. `database` and
`schema` are optional defaults. Names follow SQL: unquoted is case-insensitive, `"Quoted"` keeps the case.

OAuth client (optional):

```json
{
  "account": "<orgname>-<account_name>",
  "auth": "oauth_client",
  "client_id": "<client id>",
  "client_secret": "<client secret>",
  "token_url": "https://login.microsoftonline.com/<tenant-id>/oauth2/v2.0/token",
  "scope": "api://<app-id>/.default",
  "role": "AISQUARE_READONLY",
  "warehouse": "AISQUARE_WH",
  "database": "POLICIES",
  "schema": "PUBLIC"
}
```

After filling the secret, delete any local copy and the vault item. For OAuth, also remove the customer's guest
access and the handoff vault. Until the secret is read, pipe-as-service answers 422 `connector_unavailable` /
`not_configured`, naming "credentials for snowflake".

**Rotation** (key-pair, with no downtime: a Snowflake user holds two public keys):
1. Make a new key pair (above) and send the customer the new public key. They run
   `ALTER USER AISQUARE_READER SET RSA_PUBLIC_KEY_2 = '<new public key>';`.
2. Replace `private_key_pem` in the value:
   - ECS: put the new value into `ais-pipe-service/<env>/snowflake-<tenant>`, then force a new deployment of the
     service and wait until the old tasks are gone.
   - EKS: run `gh secret set SNOWFLAKE_<TENANT>_JSON --env nonprod-secrets ...` and `secrets-nonprod`, never an edit
     in Secrets Manager.
     - Wait for External Secrets to refresh (interval 1 h), or force it:
       `kubectl annotate externalsecret <name> -n <namespace> force-sync=$(date +%s) --overwrite`.
     - Restart the Deployment (`kubectl rollout restart`). The chart has no checksum or reloader, so pods keep the
       old value until they restart.
3. Only when every pod or task runs with the new key does the customer run
   `ALTER USER AISQUARE_READER UNSET RSA_PUBLIC_KEY;`. Removing the old key earlier breaks calls on pods that still
   hold it. Next time, rotate into `RSA_PUBLIC_KEY` and unset `RSA_PUBLIC_KEY_2`.

For OAuth, the customer adds a second client secret in the identity provider, and the same steps 2 and 3 apply.

**Offboarding:**
- ECS: remove the task definition's `secrets:` entry (a deploy), then delete the secret.
- EKS:
  - Remove the `secretEnv` entry from `release.yaml` (a deploy).
  - In one PR, remove the `supplied-secrets.json` entry and the `secrets-nonprod.yml` env line.
  - Run `gh secret delete SNOWFLAKE_<TENANT>_JSON --env nonprod-secrets --repo AISquare-Studio/aisquare-infra`.
  - Deleting the Secrets Manager secret by hand while the entry is listed does nothing lasting: the next apply
    re-creates it and the sync refills it.
- Tell the customer to do their offboarding (above) as well. Deleting our copy of the key does not disable the
  user.

## Tests

Run `pytest` from this directory. The suite is hermetic: core's socket guard blocks the network, and
`tests/fakes.py` plays Snowflake and the OAuth token endpoint.
- The fake checks a key-pair JWT's RS256 signature against the user's registered public key, and its claims.
- It serves SHOW, INFORMATION_SCHEMA and the source's own SELECTs from in-memory tables.
- It plays canned results with polling, partitions and failures, and honours `ROWS_PER_RESULTSET` and
  `MULTI_STATEMENT_COUNT`.
- `real_session()` puts the same fake behind a real `requests.Session`.

Keys are generated per run; no real key or account is used.
