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

- A role in the service's own AWS account is refused.
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

- `role_arn` (required): an IAM role in the customer's account.
- `external_id` (required): 2-1224 characters of letters, digits and `+=,.@:/-_`. AISquare
  generates it per tenant (random, at least 32 characters) and stores it only here.
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
and `S3_HEAD_OBJECT` return the same `version`.

An object stored before versioning was enabled, or written while it is suspended, has the
`VersionId` `"null"`. In a suspended bucket every write replaces that null version, so the id stays
`"null"` across edits. It therefore counts as no version id: `version_id` is left out, and
`version` is the `ETag`.

## Customer setup (registration checklist)

AISquare gives the customer two values:

- **Our principal:** `arn:aws:iam::<AISQUARE_ACCOUNT_ID>:role/<AISQUARE_PIPE_SERVICE_ROLE>`, the
  role the pipe service runs as (on EKS, the pod's role for service account `pipe`).
- **An external ID**, unique to the customer.

In each AWS account that holds policy engines or the bucket, the customer then does three things.

**1. Create an IAM role** (for example `AISquareReadOnly`) with this trust policy:

```json
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Principal": {"AWS": "arn:aws:iam::<AISQUARE_ACCOUNT_ID>:role/<AISQUARE_PIPE_SERVICE_ROLE>"},
    "Action": "sts:AssumeRole",
    "Condition": {"StringEquals": {"sts:ExternalId": "<EXTERNAL_ID>"}}
  }]
}
```

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
      "Sid": "DecryptCustomerManagedKeys",
      "Effect": "Allow",
      "Action": "kms:Decrypt",
      "Resource": ["arn:aws:kms:<REGION>:<ACCOUNT>:key/<KEY_ID>"]
    }
  ]
}
```

- `kms:Decrypt` is needed only when a policy engine uses a customer-managed KMS key (without
  it, AgentCore's List and Get fail) or the bucket uses SSE-KMS with one. If the key's
  policy doesn't delegate to IAM, it must allow the role too.
- `GetPolicyEngine`, `GetObjectVersion` and the `*Summary` actions are optional. AWS
  recommends the summary ones, and `GetObjectVersion` serves reads by `version_id`.

**3. Send AISquare** the role ARN, the regions to read and, for S3, the bucket, its region and
the prefix.

AISquare's side:

- The service's role needs `sts:AssumeRole` on the customer role ARNs (list them, rather
  than `*`). `sts:GetCallerIdentity` needs no permission.
- Store the blob above under the secret names in the table.
- Issue the external ID, and never accept one from a caller.

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
