# aisquare_pipe_aws: Bedrock AgentCore policies, S3 and Redshift (read-only, cross-account role)

Read-only access to a customer's AWS account for [aisquare.pipe](../../README.md), through one
cross-account IAM role. It ships inside the `aisquare-pipe` distribution:
`pip install "aisquare-pipe[aws]"` (or `[redshift]`, the same thing, or `[full]`, or
`[toolkits]`). The extra adds `boto3`, `botocore` and `sqlglot` (see [Install](#install));
without it, these connectors and toolkits count as not installed. It provides:

| What | Registered as | Use it for |
|---|---|---|
| `BedrockAgentCorePoliciesSource` | connector `bedrock-agentcore-policies-source` | One envelope per Amazon Bedrock AgentCore policy (Cedar, Dogwood or generated), in every configured region |
| `S3Source` | connector `s3-source` | One envelope per object under the configured S3 bucket and prefix |
| `BedrockAgentCoreToolkit` | toolkit `bedrock_agentcore` | `BEDROCK_AGENTCORE_LIST_POLICY_ENGINES`, `BEDROCK_AGENTCORE_LIST_POLICIES`, `BEDROCK_AGENTCORE_GET_POLICY` |
| `S3Toolkit` | toolkit `s3` | `S3_LIST_OBJECTS`, `S3_GET_OBJECT`, `S3_HEAD_OBJECT` |
| `RedshiftQuerySource` | connector `redshift-query-source` | One SELECT's rows (or a table's) from Amazon Redshift, as JSON batches: [Amazon Redshift](#amazon-redshift) |
| `RedshiftToolkit` | toolkit `redshift` | `REDSHIFT_RUN_SELECT` (one read-only query), `REDSHIFT_LIST_SCHEMAS`, `REDSHIFT_LIST_TABLES`, `REDSHIFT_DESCRIBE_TABLE` |

Every tool is read-only. Every connector and tool reads the account the same way. The
service's own AWS principal calls STS `AssumeRole` into the customer's role, passing the
external ID that AISquare issued. All reads then use the role's temporary credentials,
refreshed before they lapse.

- A role in the service's own AWS account is refused, and so is one in an account listed in
  `PIPE_AWS_REFUSED_ACCOUNT_IDS` (see [AISquare's side](#before-the-customer-starts-aisquare-devops)).
- Ambient credentials never read customer data. The process's own credentials (on EKS, the
  pod's role) make only `sts:GetCallerIdentity` and `sts:AssumeRole`.
- Without a valid credential blob, or without a service principal, everything fails closed
  with "AWS is not configured: …". No client is ever built from empty or default credentials.

## The credential blob

The host (aisquare-pipe-service) passes this as the connector's or toolkit's `config`. It is
server-side only: callers can never send a role, region, bucket, external ID or credential.

```json
{
  "role_arn": "arn:aws:iam::123456789012:role/AISquareReadOnly",
  "external_id": "<the external ID AISquare issued to this tenant>",
  "regions": ["us-east-1", "eu-west-1"],
  "s3": {"bucket": "policy-docs", "prefix": "rulebooks/", "region": "us-east-1"}
}
```

- `role_arn` (required): an IAM role in the customer's account, in the AWS commercial
  partition (`arn:aws:iam::…`). An `aws-cn` or `aws-us-gov` role is refused: STS can't assume
  across partitions.
- `external_id` (required): 32-1224 characters of letters, digits and `+=,.@:/-_`. AISquare
  generates it per tenant and stores it only here. A shorter one is refused as not AISquare's.
- `regions` (required): the regions whose AgentCore policy engines are read.
- `s3` (needed by `s3-source` and the `s3` toolkit only):
  - `bucket` (required);
  - `prefix` (default `""`, the whole bucket);
  - `region` (default: the first of `regions`).

**Where aisquare-pipe-service reads it** (a first-party connector's blob is keyed by its service slug):

| Connector | `SECRETS_BACKEND=env` | `SECRETS_BACKEND=aws` (Secrets Manager) |
|---|---|---|
| `bedrock-agentcore-policies-source` | `PIPE_SECRET__<TENANT>__BEDROCK_AGENTCORE_POLICIES__JSON` | `<PIPE_SERVICE_SECRETS_PREFIX>/tenants/<tenant>/bedrock-agentcore-policies` |
| `s3-source` | `PIPE_SECRET__<TENANT>__S3__JSON` | `<PIPE_SERVICE_SECRETS_PREFIX>/tenants/<tenant>/s3` |

The same JSON can be stored under both names. The service does not yet serve a toolkit
without an OAuth app. How it will find the `bedrock_agentcore` and `s3` toolkits' blob is
that service change's decision.

## Pull params (narrowing only)

| Source | Params |
|---|---|
| `bedrock-agentcore-policies-source` | `policy_engine_id` (one engine), `updated_after` (ISO 8601 with a timezone), `max_policies` |
| `s3-source` | `prefix` (inside the configured prefix), `modified_after`, `max_objects`, `max_object_bytes` (at most 25 MiB, the default) |

Any other param is refused.

## Paging (toolkits)

Every list tool returns `items`, `has_more` and a cursor for the rest:

- `S3_LIST_OBJECTS`: `next_continuation_token`, passed back as `continuation_token`.
- `BEDROCK_AGENTCORE_LIST_POLICY_ENGINES` and `BEDROCK_AGENTCORE_LIST_POLICIES`: `next_token`,
  passed back as `next_token` (with the same `policy_engine_id`, for policies). `limit` (at most
  500) bounds one call; following `next_token` until it is `null` reaches every engine and every
  policy, across every configured region.

`next_token` is opaque: it records the region, the policy engine and AgentCore's own `nextToken`
where the previous call stopped. Each AgentCore call asks only for as many items as the tool call
still needs, so a tool call always stops on one of AgentCore's own page boundaries and nothing is
skipped or repeated. `has_more` can be true with an empty next page when only empty engines or
regions are left. A token is checked before anything reaches AWS:

- one naming a region the credential blob doesn't list is refused;
- so is one from the other tool, or from a call with another `policy_engine_id`;
- one whose policy engine has been deleted since is refused as stale. List again without it.

## Version markers

Each envelope's `metadata["version"]` changes when the document changes, so a caller can
re-read it then:

- a policy: its `updatedAt`;
- an S3 object: its `VersionId` in a versioned bucket, otherwise its `ETag`.

The metadata also carries `etag`, `last_modified` and `version_id`. The S3 tools `S3_GET_OBJECT`
and `S3_HEAD_OBJECT` return the same `version`. `S3_LIST_OBJECTS` items carry only `etag`:
`ListObjectsV2` returns no version id. So in a versioned bucket a listed `etag` is not the
`version` to compare. `S3_HEAD_OBJECT` gives it without downloading the object.

An object stored before versioning was enabled, or written while it is suspended, has the
`VersionId` `"null"`. In a suspended bucket every write replaces that null version, so the id stays
`"null"` across edits. It therefore counts as no version id: `version_id` is left out, and
`version` is the `ETag`.

## Errors

A refused AWS call is a tool error with AWS's error code, never AWS's message:

| Tool error code | From AWS |
|---|---|
| `not_connected` | (no call made) the blob is missing or malformed, or the host has no AWS principal |
| `forbidden` | `AccessDenied*`, `Forbidden`, a body-less `403` (S3 `HEAD`), a role in a refused account |
| `invalid_arguments` | `ValidationException` (AgentCore), `InvalidArgument` (S3: a bad continuation token or version id), a body-less `400` (S3 `HEAD`: a bad version id); also anything our own checks refuse first |
| `not_found` | `ResourceNotFoundException`, `NoSuchKey`, `NoSuchBucket`, `NoSuchVersion`, `404` |
| `rate_limited` | `ThrottlingException`, `SlowDown`, … |
| `unauthorized` | `ExpiredToken`, `InvalidClientTokenId`, … |
| `upstream_error` | anything else |

The sources fail the same way, as pipe-as-service answers them by the exception's type (a `PipeError` by its exact
type, so the AWS error classes themselves would be a 500):
- **422**, a `ValueError`: `not_connected` (`AwsNotConfiguredError`, a `ValueError` first and a
  `ConfigValidationError` too), our own checks (`ConfigValidationError`), and `not_found` and `invalid_arguments`
  from AWS, as `AwsRequestError` (with `code` and `aws_code`).
- **502** `upstream_error`, a `PipelineError`: `forbidden` (AssumeRole included), `rate_limited`, `unauthorized`
  and `upstream_error`.

The message is the table's: the operation and AWS's error code. An S3 object that grew past `max_object_bytes`
between the listing and the read is skipped, as one the listing already shows too large is.

## Customer setup (registration checklist)

Order matters, in both directions:

- AISquare's role must exist before the customer saves the trust policy that names it. IAM
  refuses a trust policy whose principal doesn't exist ("Invalid principal in policy").
- AISquare's role can call `AssumeRole` only on the customer role ARNs its own policy lists,
  and that ARN arrives last (the customer's step 3). Until it is added, every read fails with
  `forbidden`.

So AISquare creates its role first (the steps below), the customer creates theirs, and
AISquare then adds the customer's role ARN and stores the blob
([AISquare stores the blob](#aisquare-stores-the-blob)).

### Before the customer starts (AISquare devops)

1. **Pick the environment that reads the account.** Its pipe-service role is the principal the
   customer trusts, and its secrets backend holds the blob:
   - prod: cluster `aisquare-prod`, us-east-2, namespace `prod`. Use it for a customer's real
     data. As of 2026-11-17 prod's pipe-service still runs on ECS (no EKS release there yet),
     so prod has two principals: see step 2.
   - stg or dev: cluster `aisquare-nonprod`, us-east-1, namespace `stg` or `dev`. Use one of
     them for a trial only.

   Every environment has its own role with its own ARN; dev and stg don't share one. To let
   several read, the customer lists each one's ARN in `Principal.AWS`, and each environment's
   secret carries the same external ID. The cluster's region doesn't limit what is read: the
   blob's `regions` do.
2. **Create that environment's pipe-service role now**, before the customer starts. Its
   permissions policy (below) names the customer's role, so it is attached later, when that
   ARN arrives.
   - **On EKS (dev, stg, and prod once it moves there):** the principal is the EKS Pod
     Identity role from pipe-service's cutover runbook (pipe-as-service `DEPLOYMENT.md`,
     "Cutover runbook" > "EKS", item 2; aisquare-infra creates it). The chart runs every
     component under the one service account `pipe`, so add this policy to that role, not to a
     second one. As of 2026-11-17 no environment has it yet. If it doesn't exist, create it now,
     and the runbook's item 2 later adds its DynamoDB and KMS grants to the same role:
     - one role per environment;
     - its trust policy, EKS's Pod Identity one:

       ```json
       {
         "Version": "2012-10-17",
         "Statement": [{
           "Sid": "AllowEksAuthToAssumeRoleForPodIdentity",
           "Effect": "Allow",
           "Principal": {"Service": "pods.eks.amazonaws.com"},
           "Action": ["sts:AssumeRole", "sts:TagSession"]
         }]
       }
       ```

     - its Pod Identity association: cluster `aisquare-nonprod`, namespace `stg` (or `dev`),
       service account `pipe`; for prod, cluster `aisquare-prod`, namespace `prod`, service
       account `pipe`.
   - **On ECS (prod, until it moves to EKS):** the principal is the ECS task role
     `ais-studio-pipe-task-role-prod`, which exists. Cloud-Infra's Terraform manages it
     (`aws-ecs-docker/modules/ecs/pipe_service.tf`, `aws_iam_role.pipe_service_task_role`), so
     add the policy below there, as an `aws_iam_role_policy` beside its ECS Exec one, not by
     hand.
     ECS adds no session tags, so `sts:TagSession` is harmless there.
   - **Prod: have the customer trust both prod roles from day one.** At prod's ECS → EKS
     cutover the principal changes from the ECS task role to prod's Pod Identity role. A trust
     policy that names only the ECS role then refuses every `AssumeRole` until the customer
     edits it. So create prod's Pod Identity role now as well (IAM rejects a trust policy that
     names a role that doesn't exist yet), and send the customer both ARNs. If it can't be
     created first, send the ECS role alone. The customer must then add the EKS role's ARN
     before cutover: tell them so, and don't cut prod over until they confirm it.

   **When the customer sends their role ARN** (their step 3), attach this policy to every
   AISquare role they trust (for prod, both). List each customer role ARN rather than `*`:

   ```json
   {
     "Version": "2012-10-17",
     "Statement": [{
       "Sid": "AssumeCustomerReadOnlyRoles",
       "Effect": "Allow",
       "Action": ["sts:AssumeRole", "sts:TagSession"],
       "Resource": ["arn:aws:iam::<CUSTOMER_ACCOUNT_ID>:role/AISquareReadOnly"]
     }]
   }
   ```

   `sts:GetCallerIdentity` needs no permission.

   **Why `sts:TagSession`:** EKS Pod Identity tags our session (cluster, namespace, service
   account), and those tags are transitive. STS passes them on our `AssumeRole` into the
   customer's role, and that needs `sts:TagSession` in our role's policy and in the customer's
   trust policy. Without it, AssumeRole fails with "not authorized to perform:
   sts:TagSession".

   The alternative is to disable session tags on the `pipe` Pod Identity association. Set
   `disableSessionTags: true` in `CreatePodIdentityAssociation` or
   `UpdatePodIdentityAssociation` (CLI `--disable-session-tags`; console: *Session tags* →
   *Disable session tags*). Then neither policy needs `sts:TagSession`. The policies here keep
   it, so they work either way.
3. **Issue the external ID**, one per customer, and never accept one from the customer or a
   caller. For example, this prints 43 characters:

   ```bash
   python -c "import secrets; print(secrets.token_urlsafe(32))"
   ```

   The service refuses one shorter than 32 characters.
4. *Optional:* set `PIPE_AWS_REFUSED_ACCOUNT_IDS` on pipe-service to AISquare's other AWS
   account ids (comma-separated, 12 digits each). A blob naming a role in one of them is then
   refused, as one in the pod's own account always is. A malformed value fails closed: every
   AWS read reports "not configured".

**AISquare sends the customer:**

- **Our principal ARNs**, `arn:aws:iam::<AISQUARE_ACCOUNT_ID>:role/<AISQUARE_ROLE>`: each
  role from step 2. For prod, both `ais-studio-pipe-task-role-prod` (ECS, today) and prod's
  Pod Identity role (EKS, after the cutover).
- **The external ID** from step 3.

### Which account (one per secret)

A blob names one role, so it reads one AWS account:

- **AgentCore** (secret `bedrock-agentcore-policies`): one account's policy engines, in every
  region the blob lists. Engines spread over two accounts aren't supported. The customer puts
  them in one account, or picks the account to read.
- **S3** (secret `s3`): one bucket and prefix, in one account. It may be the AgentCore account
  or another one.

The customer creates the role (steps 1 and 2 below) in each account the secrets name: once
when both are in the same account, twice otherwise.

### In the customer's account (the customer)

**1. Create an IAM role** (for example `AISquareReadOnly`) with this trust policy:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "AISquareAssumesWithTheExternalId",
      "Effect": "Allow",
      "Principal": {"AWS": [
        "arn:aws:iam::<AISQUARE_ACCOUNT_ID>:role/<AISQUARE_ROLE_1>",
        "arn:aws:iam::<AISQUARE_ACCOUNT_ID>:role/<AISQUARE_ROLE_2>"
      ]},
      "Action": "sts:AssumeRole",
      "Condition": {"StringEquals": {"sts:ExternalId": "<EXTERNAL_ID>"}}
    },
    {
      "Sid": "AISquareSessionTags",
      "Effect": "Allow",
      "Principal": {"AWS": [
        "arn:aws:iam::<AISQUARE_ACCOUNT_ID>:role/<AISQUARE_ROLE_1>",
        "arn:aws:iam::<AISQUARE_ACCOUNT_ID>:role/<AISQUARE_ROLE_2>"
      ]},
      "Action": "sts:TagSession"
    }
  ]
}
```

List every principal ARN AISquare sent, in both statements: two for prod (its ECS task role
and its EKS Pod Identity role), one for a trial. If AISquare sent only prod's ECS role, it
sends the EKS role's ARN later: add it to both statements before prod moves to EKS, or every
read fails from the cutover on.

`sts:TagSession` lets our session's EKS tags pass through (see step 2 above). On its own it
grants nothing: the role can still only be assumed with the external ID. EKS's own
target-role example that uses an external ID splits the two statements the same way.

**2. Attach a read-only permissions policy**, keeping only the statements they use:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "AgentCorePolicies",
      "Effect": "Allow",
      "Action": [
        "bedrock-agentcore:ListPolicyEngines", "bedrock-agentcore:GetPolicyEngine",
        "bedrock-agentcore:ListPolicies", "bedrock-agentcore:GetPolicy",
        "bedrock-agentcore:ListPolicyEngineSummaries", "bedrock-agentcore:GetPolicyEngineSummary",
        "bedrock-agentcore:ListPolicySummaries", "bedrock-agentcore:GetPolicySummary"
      ],
      "Resource": "*"
    },
    {
      "Sid": "ListThePrefix",
      "Effect": "Allow",
      "Action": "s3:ListBucket",
      "Resource": "arn:aws:s3:::<BUCKET>",
      "Condition": {"StringLike": {"s3:prefix": ["<PREFIX>*"]}}
    },
    {
      "Sid": "ReadThePrefix",
      "Effect": "Allow",
      "Action": ["s3:GetObject", "s3:GetObjectVersion"],
      "Resource": "arn:aws:s3:::<BUCKET>/<PREFIX>*"
    },
    {
      "Sid": "UseCustomerManagedKeys",
      "Effect": "Allow",
      "Action": ["kms:Decrypt", "kms:DescribeKey"],
      "Resource": ["arn:aws:kms:<REGION>:<ACCOUNT>:key/<KEY_ID>"],
      "Condition": {"StringEquals": {"kms:ViaService": [
        "bedrock-agentcore.<REGION>.amazonaws.com",
        "s3.<REGION>.amazonaws.com"
      ]}}
    }
  ]
}
```

- **KMS** is needed only for a customer-managed key: one that encrypts a policy engine, or
  the bucket's SSE-KMS key.
  - AgentCore's `ListPolicyEngines`, `GetPolicyEngine`, `ListPolicies` and `GetPolicy` check
    the key and decrypt before they answer, so without the grant they fail.
  - `kms:DescribeKey` is read-only; AgentCore uses it to validate the key. `kms:Decrypt` does
    the reading.
  - `kms:ViaService` limits both to requests that AgentCore or S3 make for the role, so the
    role can't call KMS directly. List each region in use: every engine region for
    `bedrock-agentcore`, and the bucket's region for `s3`. Keep only the services whose data
    uses the key.
  - If the key policy doesn't delegate to IAM, it must also allow the role these two actions.
- `GetPolicyEngine`, `GetObjectVersion` and the `*Summary` actions are optional. AWS
  recommends the summary ones, and `GetObjectVersion` serves reads by `version_id`.

**3. Send AISquare:**

- the role ARN (one per account);
- the regions whose policy engines to read;
- for S3, the bucket, its region and the prefix.

### AISquare stores the blob

1. **Add the customer's role ARN to our role's policy** (step 2's `Resource`), on every
   AISquare role they trust: for prod, both. Without it, the first `AssumeRole` fails with
   `forbidden`.
2. **Build the [credential blob](#the-credential-blob)** from what the customer sent and the
   external ID. Store it in the environment from step 1, under the
   [secret names above](#the-credential-blob): `bedrock-agentcore-policies` for AgentCore and
   `s3` for S3. When both read the same account, the same JSON can go under both names. For
   prod, store it again in prod's EKS environment when pipe-service moves there.

### If it fails

| Error | Check |
|---|---|
| `AWS refused sts:AssumeRole: …` (`forbidden`) | In the trust policy: the principal ARN, the external ID and `sts:TagSession`. In our role's policy: `sts:AssumeRole` and `sts:TagSession` on that role ARN. If it began at prod's EKS cutover: the trust policy lacks prod's Pod Identity role. |
| `AWS is not configured on this host: no service principal …` | The `pipe` Pod Identity association, or the role, is missing in that environment. |
| `refused: the role is in this service's own AWS account` (or "an AWS account this service is set to refuse") | The blob names an AISquare role, not the customer's. |
| `AWS refused bedrock-agentcore:… (AccessDeniedException)` or `s3:… (AccessDenied)` / `(403)` | The permissions policy, the KMS grant, or the prefix. |

## Amazon Redshift

`aisquare_pipe_aws.redshift` reads a customer's Amazon Redshift, provisioned or Serverless,
through the [Redshift Data API](https://docs.aws.amazon.com/redshift/latest/mgmt/data-api.html),
on the same cross-account role as everything above. It lives in this package, not in a
`connectors/redshift` of its own, because it must assume the role through
`aisquare_pipe_aws.credentials` (#31), and one connector never imports another (`pipe validate`'s
`hygiene.no-cross-connector-imports`). Its classes are `_AwsToolkit` and `_AwsSource`
subclasses, so they share the S3 and AgentCore connectors' session code exactly. The install
line is `aisquare-pipe[redshift]`, an alias of `[aws]`.

| Tool | Does |
|---|---|
| `REDSHIFT_RUN_SELECT` (primary) | Runs ONE read-only query: `ExecuteStatement` → `DescribeStatement` (polled) → `GetStatementResult` (paged). Returns `columns`, `rows` (objects keyed by column), `row_count`, `total_rows`, `truncated` (`max_rows` or `max_result_bytes`, else `null`), `result_sha256`, `statement_id`, `duration_ms`. `max_rows` 1-5000 (default 100); at most 4 MiB of JSON rows. |
| `REDSHIFT_LIST_SCHEMAS` | `ListSchemas`, optionally filtered by a LIKE `schema_pattern`. |
| `REDSHIFT_LIST_TABLES` | `ListTables`: each table's or view's `schema`, `name` and `type`, optionally for one exact `schema` and a LIKE `table_pattern`. |
| `REDSHIFT_DESCRIBE_TABLE` | `DescribeTable`: a table's columns in order, with type, length, precision, scale, nullability and default. |

The list tools page like the other toolkits: `items`, `has_more`, `next_token` (pass it back
with the same filters); `limit` is 1-1000 (default 100). The metadata tools use the Data
API's own `ListSchemas`, `ListTables` and `DescribeTable`, so no SQL is built from a caller's
names.

### The `redshift` secret

The toolkit `redshift` and the source `redshift-query-source` read the same tenant secret,
`redshift` (pipe-as-service's `credentials/catalog.TENANT_BLOBS`, #30):

| `SECRETS_BACKEND=env` | `SECRETS_BACKEND=aws` (Secrets Manager) |
|---|---|
| `PIPE_SECRET__<TENANT>__REDSHIFT__JSON` | `<PIPE_SERVICE_SECRETS_PREFIX>/tenants/<tenant>/redshift` |

It is the [credential blob](#the-credential-blob) above plus a `redshift` section, one of
these four shapes:

```json
{
  "role_arn": "arn:aws:iam::210987654321:role/AISquareReadOnly",
  "external_id": "<the external ID AISquare issued to this tenant>",
  "regions": ["us-east-1"],
  "redshift": {"cluster_identifier": "analytics", "database": "policies"}
}
```

| `redshift` section | Connects as | The role needs |
|---|---|---|
| `{"cluster_identifier": "analytics", "database": "policies"}` | the role's own database user, `IAMR:<role name>` (e.g. `IAMR:AISquareReadOnly`) | `redshift:GetClusterCredentialsWithIAM` |
| `{"workgroup_name": "policies-wg", "database": "policies"}` (Serverless) | the role's own database user, `IAMR:<role name>` | `redshift-serverless:GetCredentials` |
| `{"cluster_identifier": "analytics", "database": "policies", "db_user": "aisquare_reader"}` | that database user | `redshift:GetClusterCredentials` |
| `{"cluster_identifier": "analytics" or "workgroup_name": "policies-wg", "database": "policies", "secret_arn": "arn:aws:secretsmanager:us-east-1:210987654321:secret:redshift/aisquare-reader-AbCdEf"}` | the user in the customer's secret | `secretsmanager:GetSecretValue` on it |

- `cluster_identifier` or `workgroup_name`: exactly one. A workgroup is its name (3-63 of
  `a-z0-9-`), not an ARN.
- `database` (required).
- `db_user` (a cluster only) or `secret_arn`: at most one. With neither, the role's IAM
  identity is the database user. `secret_arn` must be in the role's own account.
- `region` (optional): the cluster's or workgroup's region; default the first of `regions`.
- Any other key in the section is refused. Every key here is server-side: a tool argument or
  pull param naming one is refused (`invalid_arguments` / "server-controlled").

### SELECT only

`REDSHIFT_RUN_SELECT` and the source's `sql` run one read-only query. Two independent checks
refuse anything else before any AWS call (not even `sts:AssumeRole`), with
`invalid_arguments` and a reason that never quotes the SQL:

1. **A lexer** that reads Redshift's quoting: strings `'...'` and identifiers `"..."` (quotes
   doubled to escape), comments `--` and `/* */`. With those blanked, the text must be one
   statement (a single trailing `;` is fine) that starts with `SELECT` or `WITH` and has none
   of `INSERT`, `UPDATE`, `DELETE`, `MERGE`, `CREATE`, `DROP`, `ALTER`, `TRUNCATE`, `COPY`,
   `UNLOAD`, `INTO` (`SELECT ... INTO` makes a table), `GRANT`, `REVOKE`, `CALL`, `EXECUTE`,
   `PREPARE`, `SET`, `BEGIN`, `COMMIT`, `ROLLBACK`, `LOCK`, `VACUUM`, `ANALYZE`, `FETCH`,
   `PG_TERMINATE_BACKEND`, `PG_CANCEL_BACKEND`, `SET_CONFIG`, ..., and names none of those
   three functions in double quotes either (`"pg_terminate_backend"(1)`). Where two readers
   could split the text differently, it refuses rather than guess (the lessons of the BigQuery
   guard, seq 13647):
   - a `--` comment ends at a CR or an LF, a bare CR too, as in PostgreSQL's lexer;
   - a `/*` inside a `/* */` comment is refused: PostgreSQL and sqlglot nest comments, a
     non-nesting reader doesn't;
   - a quoted string or identifier must end at the same place whether `\` escapes or not
     (`standard_conforming_strings`, `E'...'`). Escape a quote by doubling it;
   - `$` outside quotes is refused (`$$...$$` and `$tag$` quoting, `$1`), and so are `\`
     outside quotes and control characters other than tab, CR, LF and form feed.
2. **A parser** (sqlglot, Redshift dialect, `sqlglot>=26.25,<28`): exactly one statement
   whose root is a query (`SELECT`, `WITH ... SELECT`, `UNION`/`INTERSECT`/`EXCEPT`, a
   parenthesised query), with no write, DDL, session or transaction node at any depth
   (`SELECT ... INTO`, `FOR UPDATE` and unparsed commands included) and no call of the
   functions above.

Neither check logs the SQL, and nothing sqlglot logs while the guard parses gets out. sqlglot
does log pieces of it: "Invalid JSON path syntax. ... <the path>" at WARNING for a JSON path
it can't read (a key with a dash, a space or an open bracket in `JSON_EXTRACT`,
`JSON_EXTRACT_SCALAR` or `JSONB_EXISTS`, in queries the guard admits), and its "unsupported
syntax" warning. While the guard parses, a filter on the `sqlglot` logger drops those records
in that thread or task only, so no handler sees them, Python's last-resort stderr handler
included. Nothing is disabled globally, and sqlglot logs as usual everywhere else.

A column named like a keyword is written in double quotes (`"comment"`, `"update"`).
Redshift adds its own layer behind these: the Data API runs a single statement only, and the
database user should hold `SELECT` and nothing else (the checklist below).

### Deadline and caps

- A tool call fits the host's time budget (`config["timeout_seconds"]`), or 60 s without one.
  The source's deadline is `timeout_seconds` (default 120, at most 600, never above the host's
  budget).
- Each Data API request goes out on a client whose connect timeout plus read timeout fits
  inside what is left of the deadline at that moment: 5 s + 20 s at most, shorter as the deadline
  nears. The read timeout bounds each wait for data. A new client is built (no AWS call) when
  the current one's timeouts no longer fit. It gets half of what is left, so a call builds a
  handful. Fewer than 0.2 s left: no request is sent, and the call fails with `timeout`.
- botocore never retries, because a retry would have to fit too. A throttled or dropped request
  fails the call with its code (`rate_limited`, `upstream_error`).
- While the statement runs, a fifth of the deadline (at least 0.2 s, at most 3 s) is kept back
  for `CancelStatement`. Polling backs off from 50 ms to 1 s and stops where that slice starts,
  or when a request times out. The statement is cancelled in that slice, on a client of its own
  whose connect plus read timeout is all of what is left (never the last poll's shorter one), and
  the call fails with `timeout`, saying whether the cancel was accepted. A poll that AWS refuses
  cancels it too. An `ExecuteStatement` that times out leaves no statement id to cancel.
- The deadline covers the Data API requests. The AssumeRole before them is the shared helper's,
  on botocore's own timeouts, and the time it takes counts against the deadline.
- Paging `GetStatementResult` stops at `max_rows`, at the byte cap or at the deadline.

### `redshift-query-source`

Pull params, exactly one of:

- `sql`: one read-only statement (above);
- `table`: `schema.table` (plain names: letters, digits, `_`, `$`), read as
  `SELECT * FROM "schema"."table"`.

And `batch_size` (default 500, at most 10,000 rows per envelope), `max_rows` (default 10,000,
at most 100,000), `max_result_bytes` (JSON bytes of rows, default 25 MiB, at most 100 MiB),
`timeout_seconds`. Any other param is refused, and so is one naming a key of the secret.

Each envelope is one batch: `data` is `{"columns": [...], "rows": [{...}]}`
(`application/json`). Its metadata:

- `version` = `result_sha256`: the whole result's hash (columns and rows, whatever the rows'
  order), the same on every batch of one pull. It changes when the rows the pull read change:
  keep it and re-read when it does. Redshift has no per-table last-modified time to add, so
  the hash is the marker.
- `batch_index`, `batch_count`, `first_row`, `row_count`, `batch_sha256`, `result_row_count`,
  `total_rows`, `statement_id`, `database`, `cluster_identifier` or `workgroup_name`, and
  `table` or `query_sha256` (the statement's hash, never its text).
- `truncated` (`max_rows` or `max_result_bytes`) on every envelope of a pull cut short; its
  `result_sha256` then covers the rows read only.

A result with no rows is one envelope with no rows. The connector's own logs carry no SQL,
names or values at any level, and nothing sqlglot logs while the guard parses gets out (see
[SELECT only](#select-only)). botocore's own DEBUG log of each request ("Making request for ...
with params") does hold the request body, SQL included. It is off unless botocore logs at
DEBUG: setting the `botocore` logger to DEBUG does that, and so does a root logger at DEBUG
while `botocore` is left unset. A host handling health data should keep `botocore` at INFO or
above.

Values: `isNull` → `null`, booleans, integers (`longValue`), floats (`NaN` and the infinities
as strings), `NUMERIC`/`DECIMAL`, dates and timestamps as the strings Redshift sends, bytes as
base64. A repeated column name gets `_2`, `_3`, ...

### Errors

As for every AWS call above, a refused call carries AWS's error code, never its message:
`AccessDeniedException` → `forbidden`, `ValidationException` → `invalid_arguments`,
`ResourceNotFoundException` → `not_found`, `ActiveStatementsExceededException`,
`ActiveSessionsExceededException` and throttling → `rate_limited`, `QueryTimeoutException` →
`timeout`, `DatabaseConnectionException` and `ExecuteStatementException` → `upstream_error`.

A statement that ran and failed is different: the database's own error is about the caller's
SQL, so it is passed on (at most 500 characters, never logged): "the statement failed in
Redshift: ERROR: permission denied for schema hr". `permission denied` → `forbidden`,
`does not exist` → `not_found`, anything else → `invalid_arguments`; an `ABORTED` statement
→ `upstream_error`.

### Redshift registration checklist

AISquare's side is the [one above](#before-the-customer-starts-aisquare-devops): our
principals, the external ID, our role's `sts:AssumeRole` on the customer's role. Then:

**1. The customer's role.** The [trust policy above](#in-the-customers-account-the-customer)
(step 1), in the account that holds the cluster or workgroup. The role S3 or AgentCore uses
can serve Redshift too, or a role of its own.

**2. Its Redshift permissions**, keeping the credential statement of the one mode the secret
uses. The Data API's statement calls take no resource, so they are limited to the role's own
statements:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "RedshiftDataApiOnTheWarehouse",
      "Effect": "Allow",
      "Action": [
        "redshift-data:ExecuteStatement",
        "redshift-data:ListSchemas",
        "redshift-data:ListTables",
        "redshift-data:DescribeTable"
      ],
      "Resource": [
        "arn:aws:redshift:<REGION>:<ACCOUNT>:cluster:<CLUSTER>",
        "arn:aws:redshift-serverless:<REGION>:<ACCOUNT>:workgroup/<WORKGROUP_ID>"
      ]
    },
    {
      "Sid": "RedshiftDataApiOwnStatements",
      "Effect": "Allow",
      "Action": [
        "redshift-data:DescribeStatement",
        "redshift-data:GetStatementResult",
        "redshift-data:CancelStatement"
      ],
      "Resource": "*",
      "Condition": {"StringEquals": {"redshift-data:statement-owner-iam-userid": "${aws:userid}"}}
    },
    {
      "Sid": "ClusterUserFromTheRole",
      "Effect": "Allow",
      "Action": "redshift:GetClusterCredentialsWithIAM",
      "Resource": "arn:aws:redshift:<REGION>:<ACCOUNT>:dbname:<CLUSTER>/<DATABASE>"
    },
    {
      "Sid": "ServerlessUserFromTheRole",
      "Effect": "Allow",
      "Action": "redshift-serverless:GetCredentials",
      "Resource": "arn:aws:redshift-serverless:<REGION>:<ACCOUNT>:workgroup/<WORKGROUP_ID>"
    },
    {
      "Sid": "ClusterNamedDatabaseUser",
      "Effect": "Allow",
      "Action": "redshift:GetClusterCredentials",
      "Resource": [
        "arn:aws:redshift:<REGION>:<ACCOUNT>:dbname:<CLUSTER>/<DATABASE>",
        "arn:aws:redshift:<REGION>:<ACCOUNT>:dbuser:<CLUSTER>/aisquare_reader"
      ]
    },
    {
      "Sid": "DatabaseUserSecret",
      "Effect": "Allow",
      "Action": "secretsmanager:GetSecretValue",
      "Resource": "arn:aws:secretsmanager:<REGION>:<ACCOUNT>:secret:redshift/aisquare-reader-*"
    }
  ]
}
```

- Keep the `Resource` of the first statement that applies (a cluster's ARN uses its
  identifier, a workgroup's its ID, shown in the Serverless console), and ONE of the last
  four statements: `ClusterUserFromTheRole` (cluster, no `db_user` or `secret_arn`),
  `ServerlessUserFromTheRole` (workgroup, neither), `ClusterNamedDatabaseUser` (`db_user`) or
  `DatabaseUserSecret` (`secret_arn`).
- A secret encrypted with a customer managed key also needs `kms:Decrypt` on that key, with
  `kms:ViaService` `secretsmanager.<REGION>.amazonaws.com`.
- Nothing here writes: no `BatchExecuteStatement`, no `redshift:*` management action.

**3. A read-only database user**, as a superuser. Its name depends on the mode: the role's
own user `"IAMR:AISquareReadOnly"` (cluster or workgroup with neither key; the quotes matter),
or `aisquare_reader` (`db_user` or `secret_arn`):

```sql
-- the role's own user: create it first so it can be granted to (it is otherwise created,
-- without privileges, at the first call)
CREATE USER "IAMR:AISquareReadOnly" PASSWORD DISABLE;
-- or a named user: PASSWORD DISABLE for db_user; a password for the secret mode
CREATE USER aisquare_reader PASSWORD DISABLE;

GRANT USAGE ON SCHEMA policies TO "IAMR:AISquareReadOnly";
GRANT SELECT ON ALL TABLES IN SCHEMA policies TO "IAMR:AISquareReadOnly";
-- tables created later in the schema (run as the user who creates them):
ALTER DEFAULT PRIVILEGES IN SCHEMA policies GRANT SELECT ON TABLES TO "IAMR:AISquareReadOnly";
```

- Grant only the schemas AISquare may read: they are its scope. Grant no `INSERT`,
  `UPDATE`, `DELETE`, `CREATE` or `TEMP`, no ownership and no superuser.
- Redshift grants `PUBLIC` `CREATE` on schema `public` and `TEMP` on a database by default.
  Our checks never send a write, but for the database to refuse one too, revoke them from
  `PUBLIC` (it affects every user).
- New user-defined functions are executable by `PUBLIC` by default. A side-effecting one (a
  Lambda UDF that writes) could run inside a `SELECT`: revoke `EXECUTE` on it from `PUBLIC`, or
  don't create one.
- To check the role's user after a first call: `SELECT usename FROM pg_user WHERE usename
  LIKE 'IAMR:%';`

**4. The database user's secret** (the `secret_arn` mode only), in Secrets Manager in the role's
account, in the warehouse's region:

```json
{"username": "aisquare_reader", "password": "<its password>"}
```

For a cluster it may also hold `"dbClusterIdentifier"`, which must then match the
`cluster_identifier`. The `RedshiftDataFullAccess` tag is needed only with AWS's managed
policy of that name; the policy above names the secret instead.

**5. The customer sends AISquare:** the role ARN, the region, the cluster identifier or the
workgroup name, the database, and the `db_user` name or the secret's ARN if they use one.

**6. AISquare stores the blob** under the `redshift` secret name ([above](#the-redshift-secret)),
after adding the role ARN to our role's policy ([as for S3](#aisquare-stores-the-blob)).

| Error | Check |
|---|---|
| `AWS refused sts:AssumeRole: …` | The trust policy and our role's policy, as [above](#if-it-fails). |
| `AWS refused redshift-data:ExecuteStatement (AccessDeniedException)` | The first statement's `Resource` (the cluster identifier, or the workgroup's ID), and the credential statement of the mode in use. |
| `AWS refused redshift-data:DescribeStatement (AccessDeniedException)` | The `RedshiftDataApiOwnStatements` statement. |
| `AWS refused redshift-data:ExecuteStatement (ValidationException)` | The cluster or workgroup name, the database, or the region (`redshift.region`). |
| `the statement failed in Redshift: … permission denied …` (`forbidden`) | The database user's `USAGE` and `SELECT` grants (step 3). |
| `Amazon Redshift is not connected` (a tool, `not_connected`) or `Redshift is not configured: …` (the source) | The `redshift` secret: its `redshift` section, or the AWS keys above it. |

## Install

```bash
pip install "aisquare-pipe[aws]"
```

For development, from the repository root (as CI does; `[dev]` brings moto, the tests' AWS fake):

```bash
pip install -e ".[full,dev]"
pipe validate            # every connector, as CI runs it
cd connectors/aws && pytest
```

The `[aws]` extra needs `boto3`/`botocore` >= 1.43.32:
- the AgentCore Policy model (`ListPolicyEngines`, `ListPolicies` and `GetPolicy`, with
  paginators) is there from 1.42.60, its GA release;
- a policy's `enforcementMode` arrives only in 1.43.32. botocore drops response fields its
  model lacks, so an older release silently loses that field.

No stubbed client is needed: the real `bedrock-agentcore-control` client is used, from botocore's
own model.

The `[aws]` extra also brings `sqlglot>=26.25,<28` for the Redshift SQL guard: the one
specifier every extra naming sqlglot shares. Its Data API calls (`ExecuteStatement` with
`ClientToken`, `DescribeStatement`, `GetStatementResult`, `CancelStatement`, `ListSchemas`,
`ListTables`, `DescribeTable`) are all in botocore 1.43.32's model; the Redshift suite passes at
both floors (botocore 1.43.32, sqlglot 26.25.0).

## Tests

The tests are hermetic and every socket is blocked:

- moto plays STS and S3, with the bucket in the customer's account;
- AgentCore, which moto does not model, is a botocore `Stubber` on the real client, checked
  against AWS's own model (id patterns, ARN lengths, required fields).
- The Redshift Data API is a `Stubber` on the real `redshift-data` client too
  (`tests/redshift_fakes.py`), and its clock is fake: polling, the deadline and the cancel run
  in no real time. The SQL guard's lexer and parser are tested apart and together
  (`tests/test_redshift_sqlguard.py`), the BigQuery bypass of seq 13647 included.
