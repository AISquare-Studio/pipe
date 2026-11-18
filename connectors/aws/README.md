# aisquare_pipe_aws: Bedrock AgentCore policies and S3 (read-only, cross-account role)

Read-only access to a customer's AWS account for [aisquare.pipe](../../README.md), through one
cross-account IAM role. It ships inside the `aisquare-pipe` distribution:
`pip install "aisquare-pipe[aws]"` (or `[full]`, or `[toolkits]`). The extra adds `boto3` and
`botocore` (see [Install](#install)); without it, these connectors and toolkits count as not
installed. It provides:

| What | Registered as | Use it for |
|---|---|---|
| `BedrockAgentCorePoliciesSource` | connector `bedrock-agentcore-policies-source` | One envelope per Amazon Bedrock AgentCore policy (Cedar, Dogwood or generated), in every configured region |
| `S3Source` | connector `s3-source` | One envelope per object under the configured S3 bucket and prefix |
| `BedrockAgentCoreToolkit` | toolkit `bedrock_agentcore` | `BEDROCK_AGENTCORE_LIST_POLICY_ENGINES`, `BEDROCK_AGENTCORE_LIST_POLICIES`, `BEDROCK_AGENTCORE_GET_POLICY` |
| `S3Toolkit` | toolkit `s3` | `S3_LIST_OBJECTS`, `S3_GET_OBJECT`, `S3_HEAD_OBJECT` |

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

## Customer setup (registration checklist)

Order matters: AISquare's role must exist before the customer saves the trust policy that
names it. IAM refuses a trust policy whose principal doesn't exist ("Invalid principal in
policy").

### Before the customer starts (AISquare devops)

1. **Pick the environment that reads the account.** Its pipe-service role is the principal the
   customer trusts, and its secrets backend holds the blob:
   - prod: cluster `aisquare-prod`, us-east-2. Use it for a customer's real data.
   - nonprod: cluster `aisquare-nonprod`, us-east-1 (dev and stg). Use it for a trial only.

   They are different roles with different ARNs. To let both read, the customer lists both
   ARNs in `Principal.AWS`, and both environments' secrets carry the same external ID. The
   cluster's region doesn't limit what is read: the blob's `regions` do.
2. **Create that environment's pipe-service role** (the EKS Pod Identity role for service
   account `pipe` in its namespace), if it doesn't exist yet. As of 2026-11-17 none does.
   Give it this policy, listing each customer role ARN rather than `*`:

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

- **Our principal:** `arn:aws:iam::<AISQUARE_ACCOUNT_ID>:role/<AISQUARE_PIPE_SERVICE_ROLE>`,
  the role from step 2.
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
      "Principal": {"AWS": "arn:aws:iam::<AISQUARE_ACCOUNT_ID>:role/<AISQUARE_PIPE_SERVICE_ROLE>"},
      "Action": "sts:AssumeRole",
      "Condition": {"StringEquals": {"sts:ExternalId": "<EXTERNAL_ID>"}}
    },
    {
      "Sid": "AISquareSessionTags",
      "Effect": "Allow",
      "Principal": {"AWS": "arn:aws:iam::<AISQUARE_ACCOUNT_ID>:role/<AISQUARE_PIPE_SERVICE_ROLE>"},
      "Action": "sts:TagSession"
    }
  ]
}
```

`sts:TagSession` lets our session's EKS tags pass through (see step 2 above). On its own it
grants nothing: the role can still only be assumed with the external ID. EKS's own
cross-account example splits the two statements the same way.

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

Build the [credential blob](#the-credential-blob) from what the customer sent and the
external ID. Store it in the environment from step 1, under the
[secret names above](#the-credential-blob): `bedrock-agentcore-policies` for AgentCore and
`s3` for S3. When both read the same account, the same JSON can go under both names.

### If it fails

| Error | Check |
|---|---|
| `AWS refused sts:AssumeRole: …` (`forbidden`) | In the trust policy: the principal ARN, the external ID and `sts:TagSession`. In our role's policy: `sts:AssumeRole` and `sts:TagSession` on that role ARN. |
| `AWS is not configured on this host: no service principal …` | The `pipe` Pod Identity association, or the role, is missing in that environment. |
| `refused: the role is in this service's own AWS account` (or "an AWS account this service is set to refuse") | The blob names an AISquare role, not the customer's. |
| `AWS refused bedrock-agentcore:… (AccessDeniedException)` or `s3:… (AccessDenied)` / `(403)` | The permissions policy, the KMS grant, or the prefix. |

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

## Tests

The tests are hermetic and every socket is blocked:

- moto plays STS and S3, with the bucket in the customer's account;
- AgentCore, which moto does not model, is a botocore `Stubber` on the real client, checked
  against AWS's own model (id patterns, ARN lengths, required fields).
