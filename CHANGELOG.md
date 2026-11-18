# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **Version markers on OneDrive and Dropbox pull envelopes**, so a scheduled
  re-check can tell an edited file from an unchanged one.
  - `onedrive-source` adds `ctag` and `etag` (Graph's `cTag` and `eTag`,
    verbatim) to its `metadata_spec` and to each envelope. `cTag` changes only
    when the content does. `eTag` also changes on a rename or a move. They
    come from the item as Graph returned it just before the download, so they
    are never newer than the bytes (the listing's values are used only if that
    response has none).
  - `dropbox-source` adds `content_hash` (Dropbox's hash of the bytes) beside
    `rev`. Compare `rev` or `content_hash` with Dropbox's own values, not
    `server_modified` (see Changed).
  - A marker the provider does not send is left out, not set to `""`.
  - `googledrive-source` already carried `modified_time` and `md5_checksum`,
    so it is unchanged. Drive has no MD5 for a Google-native Doc, Sheet or
    Slides deck, so those envelopes have no `md5_checksum`. A re-check keys on
    `modified_time` there, which also moves on a rename.
- **AWS connectors** (`connectors/aws`, module `aisquare_pipe_aws`, extra
  `aisquare-pipe[aws]`, in `[full]` and `[toolkits]`): read-only access to a
  customer's AWS account through one cross-account IAM role.
  - `aisquare_pipe_aws.credentials`: the shared credential. STS `AssumeRole`
    runs with the server-issued `ExternalId`, and reads use the role's
    refreshable temporary credentials. A role in the service's own account is
    refused, as is one in an account listed in `PIPE_AWS_REFUSED_ACCOUNT_IDS`.
    So are a role outside the commercial partition and an external ID
    shorter than 32 characters. No customer client is built from ambient,
    default or empty credentials. The blob is
    `{role_arn, external_id, regions[], s3: {bucket, prefix, region}?}`. The
    README's registration checklist covers `sts:TagSession` (EKS Pod
    Identity's session tags are transitive), KMS `Decrypt` and
    `DescribeKey` under `kms:ViaService`, and the order of the two sides'
    steps. Prod's customer trusts both prod principals from day one: the ECS
    task role `ais-studio-pipe-task-role-prod` and the EKS Pod Identity role
    of pipe-service's cutover runbook.
  - `bedrock-agentcore-policies-source`: one envelope per Amazon Bedrock
    AgentCore policy, in every configured region (`ListPolicyEngines` →
    `ListPolicies`, every page; `GetPolicy` only for a policy listed without
    its definition). Each envelope carries the definition text, with the
    name, ids, ARN, status, enforcement mode and timestamps as metadata, and
    `version` = `updatedAt`.
  - `s3-source`: one envelope per object under the configured bucket and
    prefix (`ListObjectsV2`, every page; `GetObject`). Its `version` is the
    `VersionId`, else the `ETag`. A `VersionId` of `"null"` (a bucket whose
    versioning is suspended) counts as none, so the `ETag` is used.
  - Toolkits `bedrock_agentcore` (`LIST_POLICY_ENGINES`, `LIST_POLICIES`,
    `GET_POLICY`) and `s3` (`LIST_OBJECTS`, `GET_OBJECT`, `HEAD_OBJECT`). All
    their tools are read-only, with strict schemas. Both AgentCore list
    tools page with an opaque `next_token` (region, engine and AgentCore's
    `nextToken`), so every engine and policy is reachable past `limit`.
  - Pull params and tool arguments only narrow what the blob allows (a
    prefix inside the configured one, one engine, a time filter). Neither
    can name an account, role, region, bucket or credential.
  - AWS's refusals map to tool error codes: a body-less S3 `403` is
    `forbidden`; `ValidationException`, `InvalidArgument` and a body-less S3
    `400` are `invalid_arguments`.
  - The `[aws]` extra is `boto3`/`botocore` >= 1.43.32, < 2. The AgentCore
    Policy GA model arrived in 1.42.60, but a policy's `enforcementMode` only
    in 1.43.32. The tests' AWS fake, `moto[s3,sts]`, is in `[dev]`.
- **Glean connector** (`connectors/glean`, module `aisquare_pipe_glean`, extra
  `aisquare-pipe[glean]`, in `[full]` and `[toolkits]`). It is read-only over
  Glean's Client API.
  - **Toolkit `glean`**: `GLEAN_SEARCH` (`pipe:primary`) and `GLEAN_GET_DOCUMENT`.
  - **Source `glean-source`**: a saved query or document ids, read as
    `text/plain` envelopes. Each carries exactly one version marker: the
    document's `update_time`, or `content_sha256` (the SHA-256 of the text)
    when Glean sends no update time. Those documents are counted in one INFO
    line, and a search that stops at the 50-page cap while Glean still has
    results logs one WARNING (counts only).
  - **Auth**: a Glean-issued token, held in a per-tenant credential blob
    `{host, token, token_kind: user|global, act_as?, allowed_hosts?}`. No
    OAuthSpec. pipe-as-service runs `glean-source` with this blob; it does not
    serve the `GLEAN_*` tools yet.
    - A global token always sends `X-Glean-ActAs`; a user token never does.
    - `host` must match `<instance>-be.glean.com` or the blob's own
      `allowed_hosts`. IP addresses are refused in both. Redirects are not
      followed.
    - `token` and `act_as` must be ASCII (they are sent as headers).
    - Neither tool arguments nor pull params can set any credential key.
    - Without a usable blob it fails closed (`not_connected` /
      `GleanNotConfigured`) before any request.
  - The extra is empty: HTTP goes through `requests`, which core already
    requires.
- **Google Cloud Storage connector** (`connectors/gcs`, module `aisquare_pipe_gcs`, extra
  `aisquare-pipe[gcs]`, in `[full]` and `[toolkits]`): the read-only toolkit `gcs`
  (`GCS_GET_OBJECT`, primary; `GCS_LIST_OBJECTS`) and the source `gcs-source`, over ONE bucket
  and an optional prefix from the tenant's credential blob
  `{service_account_json, bucket, prefix?}`. Tokens are minted from the service account key
  (scope `devstorage.read_only`; `google-auth>=2.20,<3`, the extra's only dependency) and never
  from Application Default Credentials. Each object carries its version markers `generation`,
  `md5_hash` and `updated`. A missing or malformed blob fails closed ("Google Cloud Storage is
  not configured", no request); a bucket, key or out-of-prefix name from a caller is refused.
  The configured prefix must end with `/`. The prefix is enforced by the connector, not by
  Google: the README's checklist recommends a dedicated bucket and says CMEK needs no KMS grant.
  - A token is minted with one POST, without hidden retries. Only a key Google refuses is
    `unauthorized`; Google's `invalid_grant` for the assertion's times (this host's clock is off)
    is `upstream_error`. A token endpoint that times out is `timeout`; one that is unreachable or
    answers 5xx is `upstream_error`. A token Cloud Storage rejects is dropped from the cache, and a
    call whose cached token was rejected retries once.
  - A tool call fits inside the host's `timeout_seconds`: every request, the token POST included,
    gets the time remaining, a download checks it between chunks, and a call with none left fails
    `timeout`.
  - A download that breaks off midway, or a key that can't sign (an EC key), comes out as a typed
    error: `GcsAPIError` (`timeout`/`upstream_error`) or `GcsNotConfigured`.
  - `gcs-source` takes `start_offset`. A pull cut short by `max_objects` or the listing-page cap
    marks its last envelope (`truncated`, `next_start_offset`) and logs a warning. One cut short
    before any envelope came out raises `GcsPullTruncated` (a `ValueError`) with
    `next_start_offset`, so a `modified_after` sync over a big prefix still advances.
  Setup and the exact secret: `connectors/gcs/README.md`.
- **Google BigQuery connector** (`connectors/bigquery`, module `aisquare_pipe_bigquery`, extra
  `aisquare-pipe[bigquery]`, in `[full]` and `[toolkits]`): the read-only toolkit `bigquery`
  (`BIGQUERY_RUN_SELECT`, primary; `BIGQUERY_READ_TABLE_ROWS`, `BIGQUERY_GET_TABLE_SCHEMA`,
  `BIGQUERY_LIST_TABLES`, `BIGQUERY_LIST_DATASETS`) and the source `bigquery-query-source`, on the tenant's
  credential blob `{service_account_json, project_id, location?, max_bytes_billed?}`. It is the native
  replacement for Composio's `googlebigquery` (a distinct name; the README maps the tools). Tokens are minted
  from the service account key (scope `bigquery.readonly`; `google-auth>=2.20,<3`) and never from Application
  Default Credentials; a missing or malformed blob fails closed ("BigQuery is not configured", no request).
  - SQL is SELECT-only, refused before any request otherwise: a lexer that reads GoogleSQL's quoting and
    comments and a sqlglot parse (`sqlglot>=26.25,<28`, BigQuery dialect) must both find ONE `SELECT`/`WITH`
    statement with no write, DDL or scripting keyword or node. The dry run's `statementType` must be `SELECT`.
    The lexer ends `--` and `#` comments at a CR or an LF (a bare CR too, as BigQuery does), refuses a CR or LF
    inside a one-line string or backtick identifier, and refuses a `/*` inside a `/* */` comment. The floor is
    sqlglot 26.25 because 25.3 to 26.24 read BigQuery comments as nested, and CI runs every suite whose extra names
    sqlglot at that floor too (`scripts/dep_floor.py`).
  - Caps: a dry run refuses a query over `max_bytes_processed` (default 1 GiB, never above the tenant's
    `max_bytes_billed` ceiling, default 10 GiB) before it runs, and the job carries the same cap as
    `maximumBytesBilled`; `max_rows` and a JSON byte cap on the rows returned (`truncated` says which); the job
    deadline (the host's time budget left, 60 s without one, `job_timeout_seconds` for the source) is sent as
    `jobTimeoutMs`, and polling stops there.
  - BigQuery's rate-limit and quota 403s are `rate_limited`; its error `reason` picks the code.
  - `bigquery-query-source` pulls a `table` (tabledata.list, no job) or one `sql` SELECT, one envelope per row
    batch. Each carries `result_sha256` (order-independent) and the table's `lastModifiedTime`
    (`table_last_modified`, read before the rows; for a query the latest over the tables it names), combined as
    `version`. A pull cut at `max_rows` or `max_result_bytes` says `truncated`, and a table pull
    `next_start_index`.
  Setup, the registration checklist and the exact secret: `connectors/bigquery/README.md`.
- **Amazon Redshift connector** (module `aisquare_pipe_aws.redshift` in `connectors/aws`; extra
  `aisquare-pipe[redshift]`, an alias of `[aws]`, which now also brings `sqlglot>=26.25,<28`): the
  read-only toolkit `redshift` (`REDSHIFT_RUN_SELECT`, primary; `REDSHIFT_LIST_SCHEMAS`,
  `REDSHIFT_LIST_TABLES`, `REDSHIFT_DESCRIBE_TABLE`) and the source `redshift-query-source`, for a
  provisioned cluster or a Serverless workgroup, through the Redshift Data API. They read the tenant
  secret `redshift`: the AWS blob plus `redshift: {cluster_identifier | workgroup_name, database,
  db_user | secret_arn, region?}` (neither user key: the role's own `IAMR:` user, through
  `GetClusterCredentialsWithIAM` or Serverless `GetCredentials`).
  - It lives in `aisquare_pipe_aws` because it assumes the role through
    `aisquare_pipe_aws.credentials` and one connector never imports another; the toolkit and source
    subclass `_AwsToolkit` and `_AwsSource`, so the session, the service principal and the refusals
    are the S3 and AgentCore connectors' own.
  - SQL is SELECT-only, refused before any AWS call (not even `sts:AssumeRole`) otherwise: a lexer
    and a sqlglot parse (Redshift dialect) must both find ONE `SELECT`/`WITH` statement with no
    write, DDL, session or side-effect word or node (`SELECT ... INTO`, `UNLOAD`, `COPY`, `SET`,
    `PG_TERMINATE_BACKEND`, ...). The lexer ends `--` comments at a CR or an LF, refuses a `/*`
    inside a comment, a string whose end depends on whether `\` escapes, `$` (dollar quoting)
    and control characters outside quotes, `U&` Unicode escapes, and the side-effect functions
    in double quotes. It reads a number whole, exponent included, before any word, as Redshift
    does, so `SELECT 1e5INTO t` is refused as `INTO`.
    Nothing sqlglot logs while the guard parses gets out: its "Invalid JSON path syntax"
    warning quotes a path such as `JSON_EXTRACT(p, 'member.ssn-123-45-6789')`, so a filter on
    the `sqlglot` logger drops its records in the parsing thread or task only (no global
    switch). Python's last-resort stderr handler doesn't see them either.
  - `ExecuteStatement` → `DescribeStatement` (polled, 50 ms to 1 s) → `GetStatementResult`
    (paged) inside one deadline: the host's time budget, else 60 s (the source's
    `timeout_seconds`, default 120, at most 600). Each request's connect plus read timeout fits
    inside what is left when it goes out, because a client is rebuilt with shorter timeouts as
    the deadline nears (and with longer ones when they are under half of what a request may use:
    the first result page after the polls), and botocore never retries. A slice of the deadline
    (a fifth, 0.2 s to 3 s) is kept back to cancel a statement still running: polling stops
    there, the statement is cancelled on a client of its own sized to all of what is left, and
    the call fails with `timeout`. A statement the cancel misses is stopped by the database user's
    `statement_timeout` (at least 600000 ms), which the checklist has the customer set.
    Caps: `max_rows` and a JSON byte cap (`truncated` says which).
    The metadata tools use the Data API's `ListSchemas`, `ListTables` and `DescribeTable`, paged.
  - `redshift-query-source` pulls one `sql` SELECT or a `table`, one envelope per row batch, with
    `version` = `result_sha256` (order-independent).
  - CI runs the AWS suite at the sqlglot floor too: `scripts/dep_floor.py` and validate.yml's
    "SQL guards at the sqlglot floor" step, byte for byte #37's, so the branches merge cleanly.
  Setup, the registration checklist (the role's `redshift-data` and credential permissions, the
  read-only database user and its `statement_timeout`, the secret JSON, and how AISquare stores
  the `redshift` secret on ECS and on EKS) and the exact secret: `connectors/aws/README.md`,
  "Amazon Redshift".

### Fixed

- **Composio: composio-client 2.x** (supersedes Dependabot #60). The `[composio]`
  extra now allows `composio-client>=1.39,<3.0`, which resolves composio 0.33.0
  with composio-client 2.1.33. 2.x moved the trigger log from
  `client.logs.triggers` to `client._internal.logs.triggers`.
  `ComposioClient.list_trigger_events` now uses whichever the installed SDK has.
  Before this, 2.x raised `AttributeError`, and `composio-triggers-source`
  logged it and polled on forever, emitting nothing.
  - An SDK shape the connector doesn't know is a `ComposioSDKShapeError`, a
    `PipelineError`. Only SDK access raises it: a method or resource that is
    gone, a call that doesn't bind to the installed signature (every SDK call
    is bound with `inspect.signature` first), or `tools.execute` returning
    something other than a dict. A `TypeError` or `AttributeError` raised
    inside an SDK call keeps its type, and malformed API data is no SDK error.
  - A malformed trigger-log entry (the entry, or its `meta`, not an object) no
    longer stops the triggers source. It is skipped with one WARNING, and its
    id and time are recorded in the cursor, so one bad event can't crash-loop
    the poller. A timestamp that isn't a finite number reads as no time.
  - The triggers source stops on the two errors no retry can fix, a
    `ComposioSDKShapeError` and a `ConfigValidationError` (Composio refused the
    API key: HTTP 401/403). It logs one ERROR line, with no traceback of its
    own (the caller logs the raised error once), and raises. Before, a 401 was
    logged and re-polled every cycle. Other failures still retry next cycle.
  - composio-client 2.x gives a POST no SDK retries (1.x retried a 429, 5xx or
    connection error twice on any method). The trigger-log read is a POST that
    only reads, so on 2.x it passes `request_options={"max_retries": 2}`: a 503
    is tried 3 times and a 429 15 times (with our own 429 backoff), as on 1.x.
    `tools.execute` is unaffected: composio sends it without SDK retries on
    both lines (0.22.0 and 0.33.0). `authorize_toolkit` and
    `initiate_connection` have no retry of their own, so on 2.x a 429 or 5xx
    fails them at once, where 1.x retried twice. They create a connection
    request, so that is left as is.
  - `connectors/composio/tests/test_sdk_compat.py` runs against the REAL
    installed composio classes, with sockets blocked. Autospecs of the real
    SDK methods (all 13 this connector calls) make a call that doesn't bind to
    the installed SDK fail, and a faked `httpx` transport runs the SDK's own
    request, retry and parsing code. validate.yml's new `composio-sdk` job runs
    the connector's suite on composio 0.22.0 + client 1.43.0 and on the newest
    pair the extra resolves (0.33.0 + 2.1.33 today), on every PR and on
    `workflow_dispatch`.

### Changed

- **OneDrive and Dropbox pull envelopes describe the version delivered**, not
  the folder listing's. Before, a file edited after the listing got an
  envelope whose `size` was not `len(data)` and whose Dropbox `rev` was older
  than the bytes.
  - `onedrive-source` takes `filename`, `path`, `size`, `last_modified`,
    `web_url`, `mime_type` and the content type from the item as Graph
    returned it just before the bytes, field by field, else the listing's.
  - `dropbox-source` takes `filename`, `path`, `size`, `dropbox_id`, `rev` and
    `server_modified` from the download's own metadata (the same response as
    the bytes), else the listing's.
  - The listing still decides which files are pulled (extension filter,
    `limit`) and whether one is streamed.
  - Dropbox's `server_modified` keeps its format: the SDK's naive UTC datetime
    in ISO form, `2026-11-17T10:01:00` for Dropbox's `2026-11-17T10:01:00Z`.

## [0.5.0] - 2026-11-04

The tool-router release, and the first release as **one distribution**
(D8): `aisquare-pipe` now carries the framework and every connector, and
each connector's third-party deps are its `aisquare-pipe[<name>]` extra.
It is the first PyPI release of the framework since 0.1.1. 0.4.0 was never
published, so 0.5.0 also carries everything under 0.4.0 and 0.3.0 below.
`aisquare.pipe.core.search` is new in it, so a host that needs it
(pipe-as-service's `/v1/pipe/*` router) requires `aisquare-pipe>=0.5.0`.
Publish steps: RELEASING.md, "The 0.5.0 release".

### Upgrading from `aisquare-pipe-<x>`

In an environment that has an old per-connector package (`aisquare-pipe-onedrive`
0.1.0, say; before 0.5.0, `aisquare-pipe[popular]` and `[full]` installed
several), **uninstall every old package first**, then install the extras:
`pip uninstall -y aisquare-pipe-onedrive && pip install "aisquare-pipe[onedrive]>=0.5.0"`.
The old package and `aisquare-pipe` 0.5.0 install the same files, so pip
removing the old one (`pip install -U aisquare-pipe-<x>`, or a later `pip
uninstall aisquare-pipe-<x>`) deletes the connector. If you already upgraded
in place, the repair reinstalls the `aisquare-pipe` version you have:
`pip install --force-reinstall --no-deps "aisquare-pipe==$(python -c 'from importlib.metadata import version; print(version("aisquare-pipe"))')"`.
Keep the pin: unpinned, pip would install the newest release and skip its new
dependencies. Run the repair right after a `pip install -U aisquare-pipe-<x>`
over an old release, which breaks the connector at once. If you installed or
upgraded `aisquare-pipe` (any extras, `[popular]` too) while an old package
was installed, run the repair after every later `pip uninstall
aisquare-pipe-<x>`. Fresh environments are not affected. Details: README.md,
"Upgrading from `aisquare-pipe-<x>`".

### Changed

- **One distribution, every connector inside.** The `aisquare-pipe` wheel
  carries all 14 connector modules (`aisquare_pipe_composio` …
  `aisquare_pipe_slack`) with unchanged import paths, and the root
  `pyproject.toml` declares every entry point (auto-registration is
  unchanged).
  - **Extras.** Each connector's third-party deps are an extra named after
    it: `[composio]`, `[docusign]`, `[dropbox]`, `[onedrive]` (azure-identity),
    `[salesforce]`, `[graphify]` (tiktoken; the engine is `[graphify-engine]`),
    and empty extras for the rest. `requests` and `urllib3` move into core.
  - **Bundles.** `[full]` is now every connector, including gateway, n8n and
    graphify. `[toolkits]` is `aisquare-pipe[github,gmail,googlecalendar,googledrive,onedrive,slack]`.
  - **Not installed.** A connector whose extra isn't installed counts as not
    installed: discovery leaves it out with one info line naming the extra
    (`registry.not_installed`, `registry.missing_requirements`,
    `registry.connector_extra`), instead of a load-error warning. That takes
    a missing third-party module (a `ModuleNotFoundError`, or an error raised
    from one or while handling one, the chain a traceback prints; `from None`
    hides it) and a missing requirement of the connector's own extra, counted
    only where its env marker applies. Anything else stays a broken-plugin
    warning, even with the extra missing: a missing `aisquare.*` or
    `aisquare_pipe_*` module (a half-removed install), or `cannot import
    name`. `pipe validate` reads load failures the same way, and labels a
    plugin in its own `aisquare-pipe-<x>` distribution by that distribution,
    not as an extra.
  - **Per-connector pyprojects.** `connectors/<name>/pyproject.toml` keeps only
    the suite's pytest/ruff settings. No connector builds on its own. The ten
    that were PyPI projects (composio, docusign, dropbox, gateway, github,
    graphify, local, n8n, onedrive, salesforce) get a code-free 0.5.0 shim
    requiring `aisquare-pipe[<name>]>=0.5.0`, published once (RELEASING.md,
    "The shims"). googledrive, slack, gmail and googlecalendar were never on
    PyPI and get none.
  - **Connector versions.** A connector class's `version` is the connector's
    own contract version, independent of the distribution's.
    `hygiene.version-sync` now checks that one connector's classes agree.
  - **`pipe validate`.** Hygiene checks the root packaging instead of a
    per-connector `[project]`:
    - `hygiene.packaged`, `hygiene.extra-declared` and
      `hygiene.not-a-package` are new;
    - `hygiene.tier-listed` (now: in `[full]`) is an error;
    - `hygiene.package-name`, `hygiene.framework-dep` and
      `hygiene.requires-python` are gone.

    Install-state covers a missing extra and entry points that are declared
    but not installed. `--install` runs one `pip install -e ".[full,dev]"`.
    `pipe validate aisquare-pipe[<name>]` selects that connector.
  - **Development** is one editable install, `pip install -e ".[full,dev]"`.
    `scripts/dev_install_toolkits.sh` runs `pip install -e "<checkout>[toolkits]"`.
  - **Dependency floors** that Dependabot raised in the old per-connector
    pyprojects now live in the root: `requests>=2.34.2` (core),
    `azure-identity>=1.27.0` (`[onedrive]`), `graphifyy==0.9.106`
    (`[graphify-engine]`), `pytest>=9.2.0` (`[dev]`) and `setuptools>=84.2.1`
    (build). Dependabot watches only the root `pyproject.toml`.
  - **graphify** remediation messages name
    `aisquare-pipe[graphify,graphify-engine]`, not
    `aisquare-pipe-graphify[engine]`.
- **Publishing** (`publish.yml`, new `scripts/release_packages.py`). A run can
  no longer pass without uploading.
  - A version already on PyPI is skipped by an explicit lookup and listed in
    the run summary.
  - The upload runs without `skip-existing`, and a `verify` job checks every
    uploaded file's sha256 on PyPI.
  - A release tag must be `v<version>`, and nothing ever creates a PyPI project.
  - Every PR (and Run workflow, which defaults to a dry run) builds the
    distribution, checks that its wheel's RECORD holds every module of
    `src/` and `connectors/*/src`, and runs `twine check --strict`. A PR
    builds and checks the ten shims too.
  - Run workflow with target `shims` builds and checks the ten shims, and
    publishes them once `aisquare-pipe` 0.5.0 is on PyPI.

  Why: on 2026-07-10 the v0.2.0 release failed because new PyPI projects had
  no pending trusted publisher yet, and its framework job went green after
  silently skipping a re-upload of 0.1.1.
- `validate.yml` installs `.[full,dev]` once and declares
  `permissions: contents: read`.

### Added

- **`aisquare-pipe[toolkits]`**: the deps of every connector that registers
  an `aisquare_pipe.toolkits` entry point (github, gmail, googlecalendar,
  googledrive, onedrive, slack). `scripts/check_toolkits_extra.py` (CI)
  fails when a toolkit connector is missing from it or an entry has no
  toolkit. `scripts/dev_install_toolkits.sh` editable-installs the checkout
  with `[toolkits]` into another venv.
- **Tool and connector search index** (`aisquare.pipe.core.search`): BM25F
  in pure Python, no new dependency, no network, no model.
  - `ToolIndex.build(connector, tools, card=None)` indexes one catalogue
    (slug words, tags, description and input-schema properties, weighted in
    that order, plus the connector's name, category and keywords); its
    `.search(query, limit=10, *, allowed=None)` returns `ScoredTool`s
    (connector, tool, score). `allowed` filters by slug, e.g. what a
    connection's granted scopes allow, without rebuilding.
  - `ConnectorIndex.build(cards).search(query, limit)` ranks connector
    cards, for routers that shortlist connectors before tools.
  - `index_for(name)` keeps one index per installed toolkit, rebuilt only
    when its name, version, card or catalogue changes. Specs are matched
    by content (equal ToolSpecs count as the same), so a toolkit whose
    `tools()` builds new ToolSpecs on each call keeps its index too. These
    indexes are not in an LRU: `search_scored`, `search_tools_across` and
    `Toolkit.search_tools` reuse them however many toolkits are installed
    (no index rebuilt on repeated searches at 1,000 and 2,000 toolkits in
    `scripts/bench_search.py`). Ad-hoc catalogues (`rank_tools`,
    `rank_across`, or an `available_tools()` override listing specs the
    catalogue doesn't have) share an LRU of `INDEX_CACHE_SIZE` (512).
    `search_scored(query, connectors=None, *, limit=10)` searches several
    toolkits as one ranking, with the forms of `search_tools_across`;
    `search_connectors(query, limit)` ranks the installed cards.
  - `search_indexes(indexes, query, limit=10)`, also
    `aisquare.pipe.search_indexes`: one ranking across several `ToolIndex`es
    (IDF over the union of their catalogues), each item an index or
    `(index, allowed)`. pipe-as-service ranks its own per-toolkit indexes
    with it.
  - Query analysis: UPPER_SNAKE/camelCase splitting, stopwords, light
    stemming, a synonym table (`SYNONYMS`; a synonym counts less than the
    word itself), `authenticated` for first-person queries, and a small
    read/write intent boost.
  - Across several connectors, a connector the query names as the service
    to use ("post it in slack", "on one drive", "in gcal", "github repo",
    "slack: ...") has its scores doubled (`NAMED_BOOST`). Boost only; a
    name used only as a topic ("the slack migration") or as a modifier
    ("in slack style", "github-flavored markdown") gets no boost and counts
    as a weak search term (`TOPIC_WEIGHT`). A connector's own name is left
    out of its tools' fields at index time, so it never ranks one of its
    tools above another.
  - Idioms ("get rid of", "set up", "push back") and opening noun-verbs
    ("email the team ...", "file a bug ...") read as the verbs they mean,
    "let <someone> know" as notify, and a question ("who ...", "what ...")
    as a read;
    calendar vocabulary (postpone/push/shift ~ update, standup/sync/huddle/
    call ~ meeting, bring ~ invite) joins the synonym table. Ranking accepts
    duck-typed specs (`slug`, `description`, `input_schema`; other fields
    default as in ToolSpec, `read_only` as unknown).
  - The primary-tool marker is the tag `pipe:primary` (`PRIMARY_TAG`): a
    5% boost, never indexed as text. A plain `important` tag is ordinary
    content. See CONTRIBUTING.md, "Making tools findable".
- `aisquare.pipe.testing.synthetic.synthetic_catalog(n_connectors,
  tools_per_connector, *, seed=0)`: deterministic, realistic catalogues
  (with near-duplicate tools) for benchmarks; `scripts/bench_search.py`
  measures index build, memory and query latency on 10,000 tools and on
  1,000 connectors x 50 tools, and how often the installed toolkits'
  indexes are reused (index builds counted), the cache's hit rate and
  latency through `search_scored`, `search_connectors`,
  `search_tools_across` and `Toolkit.search_tools`, with 1,000 and 2,000
  toolkits installed whose `tools()` returns the same or new ToolSpecs.
- `tests/evals/`: 182 labelled tool-search queries over the six native
  toolkits, scored for recall@1/5/10 and MRR against the previous ranker.
- The native toolkits' main tools carry `pipe:primary`:
  `GMAIL_FETCH_EMAILS`, `GMAIL_SEND_EMAIL`, `GOOGLECALENDAR_EVENTS_LIST`,
  `GOOGLECALENDAR_CREATE_EVENT`, `GOOGLEDRIVE_FIND_FILE`,
  `GOOGLEDRIVE_UPLOAD_FILE`, `ONE_DRIVE_ONEDRIVE_UPLOAD_FILE`,
  `SLACK_SEND_MESSAGE`, `SLACK_SEARCH_MESSAGES`.
- **`left_out_toolkits()`**: the installed toolkits `installed_toolkits()`
  leaves out, name -> why (`name breaks <rule>`, `name differs from its entry
  point (class name '…')`, `slug prefix … collides with …`), from the same
  cached discovery. Names and prefixes only, so a host can log it.
- `ToolkitNotFoundError` for a left-out name says why (`'led-ger' left out:
  name breaks …`). A clash keeps its existing wording.

### Changed

- `rank_tools`, `rank_across`, `Toolkit.search_tools` and
  `search_tools_across` rank with the search index instead of substring
  counting (+2 slug, +1 description). Signatures, return types and the
  fallbacks are unchanged: with no query or no match, the head of the
  catalogue (or the connectors taking turns). Results differ: plurals and
  other word forms match (`repository` finds `..._REPOSITORIES_...`),
  synonyms match below exact words, and IDF makes rare words count more.
  On the eval, recall@1 goes from 0.560 to 0.797 and recall@10 from 0.885
  to 1.000.
- `search_tools_across` no longer rebuilds every catalogue's ranking on
  each call: each installed toolkit's index is built once and reused until
  the toolkit's version, card or catalogue changes, however many toolkits
  are installed.

- **Toolkit names must be routable**: `^[a-z][a-z0-9_]{0,63}$`
  (`TOOLKIT_NAME_RULE`, `valid_toolkit_name()`), so no hyphens and at most
  64 characters. A toolkit's name is its entry-point name and the connector
  id hosts route on. pipe-as-service serves only these names (it keeps
  hyphenated ones for pull/push connectors), so a hyphenated toolkit
  installed but was never served. `pipe validate` (`contract.toolkit.name`)
  and `scripts/check_toolkits_extra.py` now fail one, and
  `installed_toolkits()` (and so `catalogue()`, `search_tools_across()`)
  leaves it out with one warning. `discover_toolkits()` still reads every
  entry point. Connector entry-point names are unaffected, and every shipped
  toolkit already complies.
- **A toolkit whose class `name` differs from its entry-point name is left
  out** of `installed_toolkits()` too, with one warning (`pipe validate`
  already failed it): served, it would answer to one name and describe
  itself by another.
- Whatever `installed_toolkits()` leaves out is left out everywhere:
  `get_toolkit(name)` reads the served set, so a left-out name raises
  `ToolkitNotFoundError` saying why (`discover_toolkits()` still reads every
  entry point). Like `installed_toolkits()` it reads the cached discovery:
  a toolkit installed after the first one is found after
  `installed_toolkits(refresh=True)`; `installed_toolkits(strict=True)` raises
  `ToolkitConflictError` for any toolkit left out, not only a slug-prefix
  clash; and `pipe tools` lists left-out toolkits with why and won't open
  them.

### Removed

- `aisquare.pipe.core.tools.query_terms` and `aisquare.pipe.core.tools.STOPWORDS`,
  the 0.4 keyword ranker's query analysis, unused since ranking moved to
  `aisquare.pipe.core.search` (never exported from `aisquare.pipe`). Use
  `aisquare.pipe.core.search.analyze_query` and its `STOPWORDS`.

## [0.4.0] - 2026-10-31 [NOT PUBLISHED]

Never published to PyPI and never tagged: 0.5.0 replaced it as the first
release after 0.1.1, as one distribution. Its changes first ship on PyPI in
0.5.0 above. Package versions below are the per-connector ones it was
planned with.

### Added

- **Router metadata on every native toolkit** — github, googledrive,
  onedrive, slack, gmail and googlecalendar — so `catalogue()` returns six
  cards and scope filtering works on all of them.
  - **Cards.** Each sets `category` (`code`, `files`, `files`, `chat`,
    `email`, `calendar`), `keywords` and 3-4 `examples` phrased as users ask.
  - **Scopes.** Every tool carries `required_scopes`, and each toolkit's
    `scope_granted` counts its provider's broader scopes:
    - GitHub: `repo`, except `read:user`, `read:org` and `notifications`
      for the profile, orgs and notifications. `repo` covers
      `public_repo`/`notifications`, `user` covers `read:user`, and
      `admin:org`/`write:org` cover `read:org`.
    - Google Drive: `drive.readonly` to read and `drive` to write; `drive`
      covers the rest. A 403 for a missing scope is `insufficient_scope`
      (with `data.required_scopes`), as in Gmail and Calendar; any other
      403 stays `forbidden`.
    - OneDrive: `Files.Read.All` / `Files.ReadWrite.All`. Graph names
      compare short and case-insensitively.
    - Slack: its per-identity scope map, promoted. `required_scopes` holds
      the user scopes (the bot ones for bot-only tools), and a bot
      connection is checked against the bot scopes. Behaviour without
      `granted_scopes` is unchanged.
  - **Risk** follows each tool's flags (Slack: read → `read`, send and write
    → `write`, destructive and edits → `destructive`).
  - **GitHub grant revoke.** `GitHubToolkit.revoke_grant()` (`DELETE
    /applications/{client_id}/grant`) removes the app from the user's GitHub
    and ends every token they gave it, in every partition that shares the
    app. Call it only on the user's explicit request to remove the app from
    their GitHub, never on a disconnect or an account deletion while the app
    is shared. `revoke_token` still revokes one connection's token and is
    the routine path.
  - **Google Drive OAuth.** `GOOGLEDRIVE_OAUTH` no longer sends
    `include_granted_scopes=true`, like Gmail's and Calendar's specs. One
    Google OAuth client can serve all three without a Drive token carrying
    their scopes. Revoke stays grant-wide.
  - **Versions.** `aisquare-pipe-github`, `-googledrive` and `-onedrive` go to
    0.3.0. Slack, Gmail and Google Calendar stay at their unreleased 0.1.0.
    All six need `aisquare-pipe>=0.4.0`.
- **Router metadata in core** (`aisquare-pipe` 0.4.0), for a host that
  picks connectors from short cards and then tools from the chosen ones. No
  LLM or model dependency.
  - **Connector cards.** `Toolkit` gains `category` (one of `CATEGORIES`:
    code, files, email, calendar, chat, docs, project, crm, data, other;
    default `other`), `keywords` and `examples` (2–4 short user asks), all
    defaulted so existing toolkits are unchanged. The `Toolkit.card()`
    classmethod returns `name`, `display_name`, `description`, `category`,
    `keywords`, `examples` and `auth_type` from class attributes alone.
  - **Per-tool scopes and risk.** `ToolSpec` gains `required_scopes` (all
    needed; default none) and `risk` (`read` / `write` / `destructive`,
    derived from `read_only` / `destructive` unless set, and re-derived by
    `dataclasses.replace` when it was not set). An explicit `risk` may raise
    the risk above the flags but not lower it: one below them, or not
    `read` on a `read_only` tool, raises `ValueError`, also through
    `dataclasses.replace`.
    `ToolSpec.to_dict(metadata=True)` adds both; the default shape is
    unchanged.
  - **Scope-aware listing.** When `config["granted_scopes"]` is present (pass
    the stored token's `OAuthToken.granted_scopes`; a list or a space/comma
    string works too), the default `available_tools(config)` leaves out
    tools whose `required_scopes` it doesn't grant, and `call_tool` refuses
    them as `insufficient_scope` before any request. None or a blank string
    means "not known" and filters nothing. A toolkit with broader scopes that imply narrower
    ones overrides `scope_granted()`. Without the key nothing changes.
  - **Cross-toolkit catalogue.** `catalogue()` returns one card per
    installed toolkit, discovered once per process (`refresh=True` to
    re-read; `installed_toolkits()` for the classes). The first discovery
    imports the plugin modules, and their import-time side effects are the
    plugins' own (urllib3 probes for IPv6), so a host that must stay offline
    discovers once at startup. The
    `search_tools_across(query, connectors)` function ranks tools across
    several toolkits and returns `QualifiedTool(connector, tool)`; the pure
    ranker is `rank_across()`. Toolkits whose slug prefixes collide (equal,
    or one starting the other; see `slug_prefix_conflicts()`) are left out
    of discovery with one warning, so one clashing pair cannot take down
    the catalogue. `installed_toolkits(strict=True)` raises the new
    `ToolkitConflictError` instead, and `pipe validate` fails on it.
  - **Error codes.** `not_connected` (no usable credentials; nothing was
    sent) and `insufficient_scope` (the token lacks a scope the call needs)
    join `ERROR_CODES` and `ToolError`.
    `error_code_for_status(status, www_authenticate=...)` maps a 403 with an
    RFC 6750 `insufficient_scope` challenge to `insufficient_scope`.
    `call_tool` does the same for an `HTTPError` raised from `run_tool`.
  - **OAuth identity and grants.** `OAuthToken.granted_scopes` parses the
    granted `scope` (space- or comma-separated; None when the provider
    didn't say). `OAuthToken.account` is an `OAuthAccount` (`id`, `email`,
    `name`, `tenant`), filled when the token response carries an OpenID
    Connect `id_token` issued to this client. The claims are read but not
    verified, so use them as a label. `to_dict()` adds `account` only when
    it is known.
  - **Compliance.** New toolkit contract checks: `contract.toolkit.card`,
    `contract.toolkit.category`, `contract.toolkit.card-text`,
    `contract.toolkit.tool-metadata` (scope strings, risk consistent with the
    flags), `contract.toolkit.scopes-requested` (a warning) and, in
    `pipe validate`, `contract.toolkit.slug-prefix-unique`.
  - **CLI.** `pipe tools --json` rows add the card fields, and
    `pipe tools <toolkit> --json` specs add `required_scopes` and `risk`.
    Existing keys are unchanged.
- **Gmail** (`aisquare-pipe-gmail` 0.1.0, first release): `GmailToolkit`
  (`gmail`, 13 tools: 7 read, 6 write, 1 destructive) and `GMAIL_OAUTH`.
  Slugs and argument names follow Composio's Gmail toolkit.
  - **Tools.** Search and read messages and threads, attachments (returned
    as a file envelope, like Google Drive downloads), labels and the profile;
    drafts, sending and threaded replies (`In-Reply-To` / `References`, the
    last sender by default); label changes (mark read, archive, star) and
    trash. The label tools are `write` and refuse to add `TRASH` or `SPAM`:
    trashing is `GMAIL_MOVE_TO_TRASH`, the destructive tool.
  - **MIME.** Messages are built with the standard library's `email`
    package (RFC 2047 headers, an HTML body with a text alternative,
    attachments, header-injection and address checks); large messages go as
    one multipart upload. Header values, recipient display names included,
    refuse every control character but tab (sent as a space) and every
    Unicode line break. Encoded-words in display names, the subject,
    `In-Reply-To` and `References` are decoded and checked before use (an
    undecodable or doubly encoded one is refused), so an encoded CRLF can't
    add a header on Pythons without the CVE-2024-6923 fix; replies take
    their threading ids from the inbound mail as checked msg-ids only.
    Read messages come back with `messageText` (plain text preferred, HTML
    converted, charsets honoured) and `attachmentList`.
  - **Least privilege.** Read-only consent by default (`gmail.readonly`);
    each write tool names the scopes it needs (`gmail.compose`, `gmail.send`,
    `gmail.modify`; plus `gmail.readonly` for the tools that read the thread
    they answer) in `ToolSpec.required_scopes` and its description.
    `scope_granted` counts Google's broader scopes (`https://mail.google.com/`,
    `gmail.modify` over read/compose/send, `gmail.compose` over send).
- **Google Calendar** (`aisquare-pipe-googlecalendar` 0.1.0, first
  release): `GoogleCalendarToolkit` (`googlecalendar`, 14 tools: 10 read,
  4 write, 2 of them destructive) and `GOOGLECALENDAR_OAUTH`. Slugs and argument
  names follow Composio's Google Calendar toolkit.
  - **Tools.** Calendars, events (by range and text, expanded or not),
    recurring-event instances, events across every calendar, free/busy and
    free slots, the current time in a zone; create, update (a patch that
    keeps unmentioned fields and converts all-day <-> timed), quick-add and
    delete events. Update is destructive like delete: `attendees` and
    `recurrence` replace the whole list (CONTRIBUTING: an edit that
    overwrites is destructive, in every toolkit).
  - **Time zones.** Times without an offset are read in the given zone or
    the calendar's own; query bounds get the zone's offset for that date
    (daylight saving included); event times stay wall-clock with an IANA
    zone so recurring events expand correctly.
  - **Least privilege.** Read-only consent by default (`calendar.readonly`);
    writes need `calendar.events`, and create and update also
    `calendar.readonly` (they read the calendar's time zone and the event)
    (`ToolSpec.required_scopes`; the full `calendar` scope covers both).
- Both Google toolkits need `aisquare-pipe>=0.4.0`. They tell a 403 for a
  missing OAuth scope apart from other 403s and return core's
  `insufficient_scope` (with `data.insufficient_scope` and
  `data.required_scopes`). Their OAuth specs omit `include_granted_scopes`,
  so one Google OAuth client can serve Drive, Gmail and Calendar without one
  connector's token carrying another's scopes.
- **Slack** (`aisquare-pipe-slack` 0.1.0, first release): `SlackToolkit`
  (`slack`, 111 tools: 47 read, 6 send, 38 write, 20 destructive),
  `SLACK_OAUTH`, `SlackMessageSink` (`slack-message-sink`) and Events API
  helpers. Slugs and argument names follow Composio's Slack toolkit.
  - **Areas.** Messages, history and threads, search, channels and DMs,
    reactions, pins, people and presence, files, canvases, bookmarks, Do Not
    Disturb, reminders, user groups, Slack Lists, remote files and workspace
    info.
  - **Identity.** One toolkit serves both identities. `config["identity"]` is
    `"user"` (act as the person who connected) or `"bot"` (post as the app's
    bot user). A bot token (`xoxb-…`) with no identity set acts as the bot; with
    `"user"`, it is a config error. By default one consent mints both tokens
    (`build_authorize_url(identity="both")`, `exchange_installation()`).
  - **Risk tiers.** Every tool is read, send, write or destructive.
    - The defaults are read + send for the user (53 tools) and send only for
      the bot (6 tools), because a shared bot token would expose other
      people's conversations.
    - Anything more needs `config["enabled_tiers"]` or
      `config["enabled_tools"]`.
    - 17 tools are user-only and 4 bot-only. The gate refuses a tool before
      any request.
  - **Listing.** `available_tools(config)` and `search_tools(..., config=...)`
    list only what a connection may call; with no config, that is the default
    exposure. An invalid config lists no tools, and calls fail as
    `forbidden`.
  - **Attribution.** Messages sent as the user carry a visible "Sent by <name>"
    line (`config["attribution_name"]`, default `pipe`). It covers posts,
    replies, DMs, ephemeral, scheduled and `/me` messages, edits, file shares
    and sink pushes. It is opt-in for the bot.
  - **Scopes.** 44 user and 43 bot scopes, derived from the tools, including
    the events-only `app_mentions:read`. The default consent asks for all of
    them. Named scope groups (by area, by tier, and `events`) select a subset.
  - **Files.** Downloads return a `DataEnvelope` in `ToolResult.files`. The
    token goes only to `files.slack.com` (the host is checked the way
    requests resolves it), with a 100 MiB cap. Uploads use Slack's v2 flow
    (`aisquare_pipe_slack.files.upload_file`).
  - **Errors.** Slack's `ok: false` codes map to the canonical `error_code`s,
    with hints. Retry-After is honoured within the host's time budget, and
    server errors are retried only for reads.
  - **`SlackMessageSink`.** It posts envelopes as messages, ephemeral messages
    or v2 file uploads, with the toolkit's identity, attribution and send
    gate. `PushResult.ref` is the `ts`, `message_ts` or file ID.
  - **Events API helpers** (`aisquare_pipe_slack.events`).
    - `verify_request` / `parse_request` check the v0 signature and the
      replay window in one call.
    - Typed parsing of `url_verification`, `app_mention`, `message.im`,
      `reaction_added` / `reaction_removed`, `tokens_revoked` and
      `app_uninstalled`.
    - `event_to_envelope` maps events to Composio's trigger names. The app's
      own posts get no trigger.
  - **Packaging.** It requires `aisquare-pipe>=0.4.0`, `requests` and
    `urllib3`. It ships an app manifest template and joins `[full]`.

### Changed

- `Toolkit.call_tool` now reports a config with no usable credentials
  (`validate_config` false) as `not_connected` instead of `unauthorized`.
  `unauthorized` now means only that the provider rejected the credentials.
  Hosts that branched on `unauthorized` to prompt a first connection should
  branch on `not_connected` as well.

## [0.3.0] - 2026-10-15 [NOT PUBLISHED]

Never published to PyPI and never tagged. Its changes first ship on PyPI in
0.4.0 above (`aisquare-pipe-graphify` 0.2.1 too).

### Added

- **Toolkits** (`aisquare.pipe.core.tools`): a first-class home for a
  connector's LLM-callable tools. `ToolSpec` (slug, description, JSON-Schema
  `input_schema`, `read_only`/`destructive`, tags; `to_llm_tool()`),
  `ToolResult` (`successful`/`data`/`error`, a machine-readable
  `error_code` and `retry_after`, plus binary `files` as `DataEnvelope`s),
  and the `Toolkit` base class with `tools()`, `get_tool()`,
  `search_tools()` (deterministic keyword ranking), `call_tool()` (never
  raises, even on malformed toolkit results; validates arguments against
  the schema; masks secret config values out of errors; maps an
  `urllib.error.HTTPError` from `run_tool` through
  `error_code_for_status()`, with a 429's `Retry-After` as `retry_after`,
  capped at 3600 s — the message names only the status, never the
  provider's URL, reason or body) and
  `validate_config()`.
  Helpers `rank_tools()`, `validate_arguments()`,
  `error_code_for_status()` and `time_budget()` (the host's per-call
  `config["timeout_seconds"]`, shared by the native toolkits).
- Canonical tool failure kinds as `ERROR_*` constants (`ERROR_CODES`):
  `unauthorized`, `forbidden`, `not_found`, `rate_limited`,
  `invalid_arguments`, `unknown_tool`, `conflict`, `upstream_error`,
  `timeout`.
- **OAuth 2.0** (`aisquare.pipe.core.oauth`), stdlib-only: `OAuthSpec`,
  `OAuthToken`, `make_pkce_pair()`, `pkce_challenge()`,
  `build_authorize_url()`, `exchange_code()`, `refresh_access_token()`
  (keeps or rotates the refresh token; keeps the previous scope when the
  provider omits it) and best-effort `revoke_token()`. JSON and
  form-encoded token responses are both understood. Endpoints must be
  https (loopback http allowed for tests), redirects are never followed,
  `state` is required, and spec extras cannot override flow parameters.
  Toolkits expose the same steps as overridable methods.
- Errors: `OAuthError` (carries the provider error code, never secrets),
  `ToolError(message, code=..., retry_after=...)`, `ToolkitNotFoundError`.
- Registry: `discover_toolkits()` / `get_toolkit()` over the new
  `aisquare_pipe.toolkits` entry-point group.
- Testing: `toolkit_compliance_suite()`, toolkit contract checks in
  `pipe validate` (entry-point name must equal the toolkit name; packages
  registering a toolkit must call `toolkit_compliance_suite()` in
  `tests/test_compliance.py`), and `MockToolkit` registered as toolkit
  `mock` for hermetic downstream tests (with `MOCK_EXPIRED_TOKEN` /
  `MOCK_RATE_LIMITED_TOKEN` failure triggers).
- CLI: `pipe tools [<toolkit>] [--search Q] [--limit N] [--json]`.
- **Native connector toolkits**, built on the core above, so hosts can serve
  these services without Composio. Tool slugs and argument names follow
  Composio's; each toolkit registers under `aisquare_pipe.toolkits` and
  ships an `OAuthSpec`:
  - **GitHub** (`aisquare-pipe-github` 0.2.0): `GitHubToolkit` (`github`,
    31 tools: 23 read, 8 write) and `GITHUB_OAUTH` (OAuth App with PKCE;
    tokens do not expire; `revoke_token` revokes only this connection's
    token).
  - **OneDrive** (`aisquare-pipe-onedrive` 0.2.0): `OneDriveToolkit`
    (`onedrive`, 16 tools) and `MICROSOFT_OAUTH` / `microsoft_oauth(tenant)`
    (PKCE, rotating refresh tokens). `revoke_token` sends nothing, because
    Microsoft has no per-app revoke endpoint.
  - **Google Drive**: new package `aisquare-pipe-googledrive` 0.2.0 with
    `googledrive-source` (folder pulls with filters; Google-native files are
    exported), `googledrive-sink` (uploads with name-conflict policies),
    `GoogleDriveToolkit` (`googledrive`, 23 tools) and `GOOGLEDRIVE_OAUTH`
    (PKCE, offline access).
- Install extras: `aisquare-pipe-googledrive` joins `[popular]` (and so
  `[full]`); `aisquare-pipe-github` joins `[full]`.

- `Toolkit.available_tools(config=None)`: the tools a connection with that
  config may call, so hosts can list only those to a model. It returns
  every tool (`tools()`) unless a toolkit overrides it.
  `search_tools(query, limit, *, config=None)` now ranks
  `available_tools(config)`. Existing calls without `config` behave as before.
  `get_tool` and `call_tool` are unchanged, and `call_tool` still enforces
  on its own. A toolkit that overrides `search_tools` must accept
  `*, config=None`. An `available_tools` override returns a subset of
  `tools()` and never raises; for an unreadable config it returns `[]`. The
  new contract check `contract.toolkit.available-tools` (run by
  `pipe validate` and the toolkit compliance suite) verifies this for
  `None` and `{}`.

### Changed

- `pipe validate` legacy file mode also validates `Toolkit` subclasses.
- Root `pyproject.toml` configures mypy for the `src/` namespace layout, so
  `mypy src` works with an editable install.
- The GitHub, OneDrive and Google Drive packages require
  `aisquare-pipe>=0.3.0`.
- `aisquare-pipe-graphify` 0.2.1: the `[engine]` extra pins
  `graphifyy==0.9.72` (was 0.8.36).
- CI uses `actions/setup-python@v7`.

### Fixed

- Type annotations in `merge_batch` / `merge_concat` (mypy errors; no
  behavior change).
- OneDrive: `OneDriveClient.list_folder(recursive=True)` collected
  subfolders from the first page of a listing only. It now collects them
  from every page and paginates each subfolder.
- `pipe validate` hygiene now converts `file://` URLs from editable-install
  metadata into real paths. On Windows, every editable connector was
  reported as a broken install. On every platform, a path containing a
  percent-escape (such as `%20` for a space) never matched.
- `aisquare-pipe-graphify` 0.2.1: without the `[engine]` extra, the graph
  modules raise `ModuleNotFoundError` and their unit tests skip explicitly.
  Under pytest 9 they errored at collection, so a bare `pipe validate`
  failed.

## [0.2.0] - 2026-07-10

A connector-only release: the framework (`aisquare-pipe`) stayed at 0.1.1.

### Added

- `aisquare-pipe-github` 0.1.0 (first publish): the `GitHubRepoSource`
  connector.
- `aisquare-pipe-graphify` 0.2.0 (first publish): `GraphifyConverter`, plus
  an `[engine]` extra with in-process graph query verbs. Covers the
  enriched-tier fix, the query face, `GraphMerger` and incremental state.
- The publish matrix and `RELEASING.md` carry both packages.

## [0.1.1] - 2026-06-29

### Fixed

- The `full` install extra now includes `aisquare-pipe-composio`, so
  `pip install "aisquare-pipe[full]"` provides the Composio connector. The
  published 0.1.0 metadata omitted it.

## [0.1.0] - 2026-06-19

Initial public release.

### Added

- Core framework: the `DataEnvelope` spec, `SourceConnector` / `SinkConnector`
  abstractions, `Pipeline` orchestration, type matching with converters, and
  entry-point–based plugin discovery (`aisquare_pipe.connectors`).
- `pipe` CLI: `list`, `describe`, `check`, `run`, `new-connector`, and
  `validate` (contract + hygiene + unit suites, no credentials required).
- Compliance test suite (`connector_compliance_suite`) and packaging-hygiene
  checks for connector plugins.
- MCP server integration (`aisquare.pipe.mcp`).
- Connectors: Local filesystem, Dropbox, OneDrive, Salesforce, DocuSign,
  Composio, n8n, and the AISquare gateway.
- Install extras: `[popular]`, `[full]`, and per-connector packages
  (`aisquare-pipe-<service>`).

[0.4.0]: https://github.com/AISquare-Studio/pipe/compare/v0.2.0...v0.4.0
[0.2.0]: https://github.com/AISquare-Studio/pipe/compare/v0.1.1...v0.2.0
[0.1.1]: https://github.com/AISquare-Studio/pipe/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/AISquare-Studio/pipe/releases/tag/v0.1.0
