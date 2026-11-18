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
| `SNOWFLAKE_SHOW_SCHEMAS` | `SNOWFLAKE_SHOW_SCHEMAS` | Same as above, plus `database` and `in_scope` (`ACCOUNT` or `DATABASE`). |
| `SNOWFLAKE_SHOW_TABLES` | `SNOWFLAKE_SHOW_TABLES` | Same as above, plus `database`, `schema` and `in_scope` (`ACCOUNT`, `DATABASE` or `SCHEMA`). |
| (none) | `SNOWFLAKE_DESCRIBE_TABLE` | Columns, kind, size and the version marker `last_altered`. |

`in_scope` takes any case and picks the scope the tool lists: `ACCOUNT` (with no `database` or `schema`), `DATABASE`
(`database`, else the configured one) or `SCHEMA` (`schema`, else the configured one). Without it, the tool lists the
narrowest scope its arguments and the configured defaults give. An `in_scope` the other arguments contradict
(`ACCOUNT` with a `database`, `DATABASE` with a `schema`) is refused, and so is one with nothing to list in (`SCHEMA`
with no schema given or configured). Composio's `role` and `warehouse` arguments are server-controlled here: a call
that sends them is refused (`invalid_arguments`). `SNOWFLAKE_EXECUTE_SQL` needs one row in pipe-as-service's `NATIVE_TOOL_RENAMES`
(`SNOWFLAKE_EXECUTE_SQL` → `SNOWFLAKE_RUN_SELECT`; the arguments need no translation). Composio's other tools
(warehouse, user and grant management) have no native counterpart, on purpose.

## What it does

| Tool | What it returns |
|---|---|
| `SNOWFLAKE_RUN_SELECT` (primary) | ONE `SELECT` or `WITH ... SELECT`: the rows (objects keyed by column name), the columns' types, `row_count`, `truncated`, `query_id`. `max_rows` (default 100, at most 5,000). `database` and `schema_name` for names written without them (default: the configured ones). `timeout` (seconds, at most 600). |
| `SNOWFLAKE_DESCRIBE_TABLE` | One table or view: its columns (name, type, nullable, default, comment), `kind`, `row_count`, `bytes`, `created`, `comment`, `clustering_key` and the version marker `last_altered`. |
| `SNOWFLAKE_SHOW_TABLES` | The tables in a schema (default: the configured one), a database or the account (`in_scope`), with kind, row count, size, owner and comment. |
| `SNOWFLAKE_SHOW_SCHEMAS` | The schemas in a database (default: the configured one; without one, or with `in_scope` `ACCOUNT`, every database the role sees). |
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
(default 500, at most 10,000), `max_rows` (default 10,000, at most 100,000), `max_result_bytes` (JSON bytes of
rows, default 25 MiB, at most 100 MiB) and `timeout_seconds` (the statement deadline, default 120, at most 600).

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

## Security

**SELECT only, before any request.** `SNOWFLAKE_RUN_SELECT` and a `sql` pull refuse anything but ONE read-only
statement before a token is even signed. Two checks must both pass. Each reads the text on its own, and most forms
below are refused by both, each by itself. The few that only one of them refuses are listed after the two.
1. **A lexer.** It blanks `'...'` strings, `"..."` identifiers and `--`, `//` and `/* */` comments.
   - Backslashes: it reads them as Snowflake does, as an escape in a `'...'` string and never in a `"..."`
     identifier. Then it reads the text the three other ways a reader could (escapes in both, in neither, in
     `"..."` only). All four readings must agree on where every quote, every comment and the statement end, so
     write a quote inside a string as `''`, not `\'`.
   - Comments: a `--` or `//` comment ends at the first CR or LF. One that a bare CR (no LF after it) ends is
     refused, and so is a `/*` inside a `/* */` comment. Readers disagree on both (sqlglot ends a line at a CR, and
     some versions nest comments), and the disagreement could hide a statement from one check.
   - In Snowflake's reading, the text must be one statement (one trailing `;` is dropped) that starts with `SELECT`
     or `WITH`.
   - It may name none of `INSERT`, `UPDATE`, `DELETE`, `MERGE`, `UPSERT`, `TRUNCATE`, `CREATE`, `ALTER`, `DROP`,
     `UNDROP`, `RENAME`, `SWAP`, `CLONE`, `RESTORE`, `COPY`, `PUT`, `REMOVE`, `RM`, `LOAD`, `GRANT`, `REVOKE`,
     `USE`, `SET`, `UNSET`, `RESET`, `BEGIN`, `COMMIT`, `ROLLBACK`, `TRANSACTION`, `DECLARE`, `CALL`, `EXEC`,
     `EXECUTE`, `IMMEDIATE`, `PROCEDURE`, `ANALYZE`, `INTO`, `NEXTVAL`. A column with one of those names must be in
     double quotes, except `NEXTVAL`, which is refused quoted too (`s."NEXTVAL"` still takes a sequence's next value).
   - It may hold none of: `$` (`$$` strings, `SYSTEM$` functions, session variables, stage columns), `@` (stages),
     `->`/`->>` (the flow operator chains statements), `|>`, a `!` other than `!=` (a model's method,
     `m!PREDICT(...)`), backticks, or a backslash outside a string. No `"..."` name may hold a `$` either
     (`"SYSTEM$ABORT_SESSION"(...)`).
   - It may call no Cortex AI function: no `AI_*` call and no `CORTEX` qualifier (`SNOWFLAKE.CORTEX.COMPLETE`),
     quoted or not. `IDENTIFIER(...)` is refused too: it turns a string, which neither check reads, into a name.
   - A table function (`TABLE(f(...))`, `LATERAL f(...)`) must be `FLATTEN`, `SPLIT_TO_TABLE`,
     `STRTOK_SPLIT_TO_TABLE` or `GENERATOR`, unqualified (`s.FLATTEN(...)` is a user-defined function): Snowflake can
     call a stored procedure from `FROM TABLE(...)`, and a procedure may write. A qualified call named like one of
     them is refused wherever it stands.
2. **A parser**: sqlglot's tokens and tree (Snowflake dialect; its tokenizer reads quotes as Snowflake does). The
   text must be exactly one statement whose root is a query, and nowhere in it may there be:
   - a write, DDL, session, transaction, bind-variable or unparsed node;
   - a `$` or `@` token, a `$$` string, a name holding `$` (quoted or not, a function's too), or a table named `@...`;
   - a `NEXTVAL` name (quoted or not);
   - an `AI_*` call, a `CORTEX` qualifier, an `IDENTIFIER(...)` or a model's method;
   - a table function outside that list, including a qualified one in a bare `FROM` or `JOIN`
     (`FROM s.FLATTEN(...)`).

   Text it can't parse is refused. sqlglot logs pieces of the SQL it parses (its "unsupported syntax" warning, and
   "Invalid JSON path syntax" with the path for any JSON key with a dash, as in `GET_PATH(v, 'a-b')` or `v:'x-y'`),
   so a filter on the `sqlglot` logger drops every record it emits while the guard parses, in that context only.
   Nothing is disabled globally, and sqlglot logs as usual everywhere else.

What only one check refuses:
- Only the lexer: a write keyword as a bare word (`SELECT update FROM t`, where sqlglot reads a column), `|>`, a
  control character, a second `;`, and the text readers are known to disagree on (a backslash before a quote, a
  bare CR, a nested comment). The parser reads such text one way, as Snowflake does where they are known to agree.
  The lexer refuses it because the parser is only as faithful as sqlglot's reading of Snowflake.
- Only the parser: a table function in a bare `FROM` or `JOIN` under a name other than the built-ins'
  (`FROM t, my_udtf(1)`), which the lexer can't tell from `EXTRACT(YEAR FROM TO_DATE(x))`, and the `?` and `:1`
  binds. `test_what_the_lexer_leaves_to_the_parser` lists them.
- Neither looks inside a string: what it holds is data.

A default schema named `CORTEX` (the `schema_name` argument, a pull's `schema`, or the blob's `schema`) is refused as
well: there, an unqualified `COMPLETE(...)` is `SNOWFLAKE.CORTEX.COMPLETE`.

So DML, DDL, DCL, `COPY`, `CALL`, Snowflake Scripting blocks, `EXECUTE IMMEDIATE` and several statements are
refused (`invalid_arguments`), and the message never echoes the SQL. Snowflake adds its own layers behind these:
- Every request sets `MULTI_STATEMENT_COUNT` to 1, so Snowflake itself refuses a second statement.
- The role holds read-only grants (checklist step 3), and the user has no other role: `DEFAULT_SECONDARY_ROLES` is
  empty, so no other role's privileges join in. The exception is `PUBLIC`, which every role inherits: whatever the
  customer granted to `PUBLIC` is readable too, and step 3 has them check it.
- Every statement carries `QUERY_TAG` `aisquare-pipe`: the customer finds AISquare's queries in `QUERY_HISTORY`.

**Caps.**
- **Rows and bytes returned.** `max_rows` (sent as `ROWS_PER_RESULTSET`, so Snowflake sends no more, and cut again
  here), and at most 4 MiB of JSON rows from a tool (`max_result_bytes`, at most 100 MiB, for the source). Result
  partitions are read only while they fit. A result cut short says `truncated`.
- **Time.** A tool call fits inside the host's `timeout_seconds`, all of its requests together. Each request, the
  OAuth token POST included, gets the time left (at most 30 s), and a call with less than a second left fails
  `timeout` before it submits anything. Every request starts inside the budget and gets at most the time left
  (requests bounds each connect and each socket read, so one request in flight may finish a little after it). The
  statement's deadline (the budget left, or `timeout` / `timeout_seconds` when lower; 60 s for a SELECT and 30 s for
  metadata without a budget, 120 s for the source) is sent as the statement's `timeout`, so Snowflake stops it then
  too. Polling stops at the deadline, the statement is cancelled, and the call fails `timeout`.
- **Cost.** Snowflake bills warehouse time, not bytes scanned. The deadline bounds each statement. The warehouse's
  own `STATEMENT_TIMEOUT_IN_SECONDS`, its size and a resource monitor bound the rest (checklist step 2). Cortex AI
  functions are the exception: they bill AI credits by the token, and no warehouse limit or resource monitor bounds
  those. So the guard refuses every `SNOWFLAKE.CORTEX.*` and `AI_*` call and a model's methods. A view or a function
  that calls Cortex itself is out of the guard's sight, and only the role's grants stop it (checklist step 3,
  `CORTEX_USER`).

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
- Cortex AI functions (`SNOWFLAKE.CORTEX.*`, `AI_*`) and a model's methods (`m!PREDICT(...)`): they bill AI credits
  outside the warehouse's limits. `IDENTIFIER(...)`, and `$` in any name.
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
- `ACCOUNTADMIN` to revoke `SNOWFLAKE.CORTEX_USER` from `PUBLIC`, if you decide to (step 3).
- Optional: `SECURITYADMIN` for a network policy, with `CREATE NETWORK RULE` on the schema that holds its rule;
  `ACCOUNTADMIN` for an External OAuth security integration.

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

   Then check what the role gets without being granted it. Every role inherits `PUBLIC`:

   ```sql
   SHOW GRANTS TO ROLE PUBLIC;
   ```

   - Whatever `PUBLIC` holds there (`USAGE` on a database or schema, `SELECT` on a table or view) AISquare can read
     too. Revoke from `PUBLIC` what AISquare must not read.
   - By default, Snowflake grants the database role `SNOWFLAKE.CORTEX_USER` to `PUBLIC` (look for that name in the
     list). It lets every role call the Cortex AI functions. They bill AI credits, which no warehouse limit or
     resource monitor bounds. The connector refuses direct calls
     (`SNOWFLAKE.CORTEX.*`, `AI_*`), but a view or a function that the role reads can call Cortex itself. Decide:
     - AISquare's role gets no Cortex (recommended). Revoke it from `PUBLIC` and grant it only to the roles that use
       Cortex. This changes every role in the account, so check with whoever uses Cortex first:

       ```sql
       USE ROLE ACCOUNTADMIN;
       REVOKE DATABASE ROLE SNOWFLAKE.CORTEX_USER FROM ROLE PUBLIC;
       GRANT DATABASE ROLE SNOWFLAKE.CORTEX_USER TO ROLE <a role that uses Cortex>;
       ```

     - Or keep it. Then a view or function that calls Cortex spends AI credits whenever AISquare reads it. Don't
       grant AISquare's role such views, and tell AISquare devops that this is your choice.
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
   them for each environment). Or restrict this user to them alone. A network rule is a schema-level object: create
   it in a schema AISquare's role can't read, as a role with `CREATE NETWORK RULE` on that schema, and name it in
   full (`<db>.<schema>.AISQUARE_EGRESS`) in the policy:

   ```sql
   CREATE NETWORK RULE <db>.<schema>.AISQUARE_EGRESS MODE = INGRESS TYPE = IPV4 VALUE_LIST = ('<ip-1>', '<ip-2>');
   CREATE NETWORK POLICY AISQUARE_ONLY ALLOWED_NETWORK_RULE_LIST = ('<db>.<schema>.AISQUARE_EGRESS');
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
DROP ROLE AISQUARE_READONLY;                       -- and the warehouse, monitor, policy and rule if made for AISquare
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

  Each environment gets its own key, so its own GitHub secret. Below, `<GH_SECRET>` is its name:
  `SNOWFLAKE_<TENANT>_JSON` for `stg`, `DEV_SNOWFLAKE_<TENANT>_JSON` for `dev` (as `DEV_INTERNAL_AUTH_TOKEN` is).

  1. Store the value: `gh secret set <GH_SECRET> --env nonprod-secrets --repo AISquare-Studio/aisquare-infra < blob.json`
     (the JSON below, from a file that is deleted afterwards).
  2. In aisquare-infra, add to `live/nonprod/data/supplied-secrets.json`:

     ```json
     {
       "nonprod/<env>/pipe-service/snowflake-<tenant>": {
         "github_secret": "<GH_SECRET>",
         "description": "PIPE_SECRET__<TENANT>__SNOWFLAKE__JSON of the <env> pipe service: the Snowflake service user blob of <tenant>"
       }
     }
     ```

     Also add its line to the `env` of the sync step in `.github/workflows/secrets-nonprod.yml`:
     `<GH_SECRET>: ${{ secrets.<GH_SECRET> }}` (for dev,
     `DEV_SNOWFLAKE_<TENANT>_JSON: ${{ secrets.DEV_SNOWFLAKE_<TENANT>_JSON }}`). Then:
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
`<PIPE_SERVICE_SECRETS_PREFIX>/tenants/<tenant>/snowflake`, read with no deploy. A new secret is picked up within
about 60 s (`PIPE_SERVICE_NATIVE_CREDENTIALS_TTL_SECONDS`). A changed value can be served from the service's cache
for up to 300 s, so a rotation keeps the old key valid at least that long.

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
   - EKS: run `gh secret set <GH_SECRET> --env nonprod-secrets ...` (`DEV_SNOWFLAKE_<TENANT>_JSON` for dev) and
     `secrets-nonprod`, never an edit in Secrets Manager.
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
  - Run `gh secret delete <GH_SECRET> --env nonprod-secrets --repo AISquare-Studio/aisquare-infra`
    (`DEV_SNOWFLAKE_<TENANT>_JSON` for dev).
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
