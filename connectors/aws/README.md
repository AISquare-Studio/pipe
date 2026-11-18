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

**Where aisquare-pipe-service reads it.** Each blob is a tenant secret named after the source
connector's service slug, and its toolkit shares it (see below the table):

| Connector and toolkit | Tenant secret | `SECRETS_BACKEND=env` | `SECRETS_BACKEND=aws` (Secrets Manager) |
|---|---|---|---|
| `bedrock-agentcore-policies-source`, toolkit `bedrock_agentcore` | `bedrock-agentcore-policies` | `PIPE_SECRET__<TENANT>__BEDROCK_AGENTCORE_POLICIES__JSON` | `<PIPE_SERVICE_SECRETS_PREFIX>/tenants/<tenant>/bedrock-agentcore-policies` |
| `s3-source`, toolkit `s3` | `s3` | `PIPE_SECRET__<TENANT>__S3__JSON` | `<PIPE_SERVICE_SECRETS_PREFIX>/tenants/<tenant>/s3` |

The same JSON can be stored under both names. Today pipe-as-service serves only the two sources:
it doesn't yet serve a toolkit without an OAuth app. With credential-driven routing
(pipe-as-service #30), each toolkit reads its source's secret (`TENANT_BLOBS` in
`credentials/catalog.py`): `bedrock_agentcore` reads `bedrock-agentcore-policies`, and `s3` reads
`s3`. How the secret reaches the service:
[AISquare stores the blob](#aisquare-stores-the-blob).

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
     them for a trial only. stg also runs on ECS until its cutover (Cloud-Infra's stg sets
     `enable_pipe_service`). A trial through stg's ECS service has the principal
     `ais-studio-pipe-task-role-stg`, set up as for prod's ECS role in step 2.

   Every environment has its own role with its own ARN; dev and stg don't share one. To let
   several read, the customer lists each one's ARN in `Principal.AWS`, and each environment's
   secret carries the same external ID. The cluster's region doesn't limit what is read: the
   blob's `regions` do.

   **Keep production apart from trials.** Never add a stg or dev principal to the role that
   reads production data. A trial gets a customer role of its own, with its own external ID
   and, for S3, its own bucket or prefix. This is the AWS form of one customer key per
   environment. A nonprod blob also sits in a GitHub environment secret of aisquare-infra
   (see [AISquare stores the blob](#aisquare-stores-the-blob)).
2. **Create that environment's pipe-service role now**, before the customer starts. Its
   permissions policy (below) names the customer's role, so it is attached later, when that
   ARN arrives.
   - **On EKS (dev, stg, and prod once it moves there):** the principal is the EKS Pod
     Identity role from pipe-service's cutover runbook (pipe-as-service `DEPLOYMENT.md`,
     "Cutover runbook" > "EKS", item 2; aisquare-infra creates it). The chart runs every
     component under the one service account `pipe`, so add this policy to that role, not to a
     second one. As of 2026-11-17 no environment has it yet. If it doesn't exist, create it now
     in aisquare-infra's Terraform, never by hand, as the role item 2 will use: item 2 later adds
     its DynamoDB and KMS grants to that same role. aisquare-infra makes its Pod Identity roles
     with the `terraform-aws-modules/eks-pod-identity` module (`modules/eks-cluster/addon_roles.tf`,
     `main` d975dcf). A role made by hand clashes with Terraform later: aisquare-infra's apply
     fails on its name (`EntityAlreadyExists`), and a second role is a principal the customer
     never trusted. Never delete and recreate it either: IAM saves the role in the customer's
     trust policy as the role's unique ID, not its ARN, so a recreated role, even under the same
     ARN, is refused until the customer puts the ARN back and saves the policy again. The role
     needs:
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
     edits it. So create prod's Pod Identity role now as well, in aisquare-infra as above (IAM
     rejects a trust policy that names a role that doesn't exist yet), and send the customer
     both ARNs. If it can't be created first, send the ECS role alone. The customer must then
     add the EKS role's ARN before cutover: tell them so, and don't cut prod over until they
     confirm it.

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
   external ID, and save it as a local `blob.json`. When both connectors read the same account,
   the same JSON can go under both secrets.
3. **Store it in the environment from step 1**, once per secret the tenant uses:

   | `<secret>` | Env var `<ENV_VAR>` | `<GH_SECRET>` for `stg` (EKS) | `<GH_SECRET>` for `dev` (EKS) |
   |---|---|---|---|
   | `bedrock-agentcore-policies` | `PIPE_SECRET__<TENANT>__BEDROCK_AGENTCORE_POLICIES__JSON` | `BEDROCK_AGENTCORE_POLICIES_<TENANT>_JSON` | `DEV_BEDROCK_AGENTCORE_POLICIES_<TENANT>_JSON` |
   | `s3` | `PIPE_SECRET__<TENANT>__S3__JSON` | `S3_<TENANT>_JSON` | `DEV_S3_<TENANT>_JSON` |

   `<TENANT>` is the tenant id in upper case. Every run of characters other than A-Z and 0-9
   becomes one `_`, and any `_` at either end is dropped (`acme-corp.eu` gives `ACME_CORP_EU`).
   `<GH_SECRET>` is the environment's own GitHub secret (`dev`'s has the `DEV_` prefix, like
   `DEV_INTERNAL_AUTH_TOKEN`). Use that one name in every step and command for the environment,
   so `dev` never reads or writes stg's secret.

   **Where pipe-service runs today.** Production runs on ECS only. aisquare-releases has no prod
   pipe-service (`environments/prod/services/` holds only explainability-gateway and praxis),
   and aisquare-infra has no `prod/prod/pipe-service/*` secret. So a customer's production
   account follows the ECS path. `stg` runs on ECS and on EKS until the cutover, and `dev` runs
   on EKS only. While both `stg`s run, turn the tenant on in both (at the least, in the one the
   tenant's caller uses): the other one fails the tenant's calls as not configured.

   **Turning a connector on for a tenant is a deploy, today.** Every environment runs the env
   secrets backend (`PIPE_SERVICE_SECRETS_BACKEND=env`: EKS `deploy/values.yaml` and both ECS
   task definitions in pipe-as-service). The blob reaches the service only as `<ENV_VAR>`, read
   when a pod or task starts. Store the value first, then add the entry that reads it.

   - **ECS** (production, and `stg` until the cutover):
     1. Create the secret in the task definition's region: prod `us-east-2`, stg `us-east-1`.
        The task execution role reads only `ais-pipe-service/<env>/*` in its own region, so a
        secret anywhere else stops the task from starting:
        `aws secretsmanager create-secret --region <region> --name ais-pipe-service/<env>/<secret>-<tenant> --secret-string file://blob.json`
     2. Add an entry to the task definition's `secrets:`
        (`deploy/ecs/<env>/task-definition-pipe-service.json` in pipe-as-service). Use the same
        ARN form as the entries already there:

        ```json
        {
          "name": "<ENV_VAR>",
          "valueFrom": "arn:aws:secretsmanager:<region>:<account>:secret:ais-pipe-service/<env>/<secret>-<tenant>"
        }
        ```

     3. Merge it, then run pipe-as-service's `deploy` workflow for that environment and region:
        `gh workflow run deploy.yml --repo AISquare-Studio/pipe-as-service -f environment=<env> -f region=<region>`.
        The new task-definition revision is the deploy. Terraform ignores
        `container_definitions`, so this workflow owns them. The workflow builds and pushes the
        image from the head of the branch it runs on (`main`), so this deploy also ships
        everything else merged to pipe-as-service `main` since the last one. Check what that is
        first, above all in production.
   - **EKS** (`stg`, `dev`): never put the value into Secrets Manager by hand. aisquare-infra
     creates the secret from `live/nonprod/data/supplied-secrets.json`. Its `secrets-nonprod`
     workflow fills the secret from the GitHub environment `nonprod-secrets`, when run by hand
     and after every `apply-nonprod`. So a value edited in Secrets Manager is overwritten with
     the GitHub one at the next sync. And if a listed secret's GitHub value is missing, the sync
     stops for EVERY secret.
     1. Store the value in GitHub:
        `gh secret set <GH_SECRET> --env nonprod-secrets --repo AISquare-Studio/aisquare-infra < blob.json`.
     2. In aisquare-infra, open one PR that adds the entry to
        `live/nonprod/data/supplied-secrets.json`:

        ```json
        {
          "nonprod/<env>/pipe-service/<secret>-<tenant>": {
            "github_secret": "<GH_SECRET>",
            "description": "<ENV_VAR> of the <env> pipe service: the AWS role blob of <tenant>"
          }
        }
        ```

        The same PR adds its line to the `env` of the sync step in
        `.github/workflows/secrets-nonprod.yml`: `<GH_SECRET>: ${{ secrets.<GH_SECRET> }}`. Merge
        only after step 1, so the sync can't fail on it. The merge runs `apply-nonprod`, which
        creates the secret and then runs `secrets-nonprod` to fill it. Check that both ran green.
     3. In aisquare-releases, add to `environments/<env>/services/pipe-service/release.yaml`.
        That file is written by people. Never edit `values.yaml`, which the pipe-as-service
        build workflow overwrites:

        ```yaml
        secretEnv:
          <ENV_VAR>: { from: pipe-service/<secret>-<tenant> }
        ```

        Helm merges it with the `secretEnv` of `values.yaml`. A code owner merging that PR is the
        deploy. Argo CD syncs the ExternalSecret `pipe` (key
        `<secrets.prefix>/pipe-service/<secret>-<tenant>`) before the Deployment. One
        ExternalSecret carries every `secretEnv` entry, so a secret with no value yet fails that
        ExternalSecret and holds the release.

   Then delete `blob.json`. When pipe-service moves to EKS in prod, the same EKS steps apply to
   prod, with aisquare-infra's `live/prod/data/supplied-secrets.json`, `secrets-prod.yml` and the
   GitHub environment `prod-secrets`. Store the blob there before prod's cutover.

   **Never switch the secrets backend for AWS.** `PIPE_SERVICE_SECRETS_BACKEND=aws` moves EVERY
   secret the service reads, the shared Composio key included, and a missing secret reads as no
   key. Flipping the backend "for AWS" takes Composio down for every connector in that
   environment. Moving to the aws backend is a whole-service migration (pipe-as-service
   `DEPLOYMENT.md`). After it, the secret is `<PIPE_SERVICE_SECRETS_PREFIX>/tenants/<tenant>/<secret>`
   and needs no deploy.

### Changing the blob

A new external ID, role ARN, region or bucket means a new value. The service reads the value only
when a pod or task starts, so store the new value and then restart:

- ECS: `aws secretsmanager put-secret-value --region <region> --secret-id ais-pipe-service/<env>/<secret>-<tenant> --secret-string file://blob.json`,
  then `aws ecs update-service --region <region> --cluster ais-studio-ecs-cluster-<env> --service ais-studio-pipe-service-<env> --force-new-deployment`.
  Wait until the old tasks are gone.
- EKS, in this order (`<GH_SECRET>` from the table above, per environment):
  1. `gh secret set <GH_SECRET> --env nonprod-secrets --repo AISquare-Studio/aisquare-infra < blob.json`.
     Never edit the value in Secrets Manager: the next sync puts the GitHub value back, and a
     pod that starts after that reads the old blob.
  2. Run the sync and wait for it to finish.
     `gh workflow run secrets-nonprod.yml --repo AISquare-Studio/aisquare-infra` returns as soon
     as the run is queued. Find the run with
     `gh run list --workflow secrets-nonprod.yml --repo AISquare-Studio/aisquare-infra --limit 1 --json databaseId,createdAt`
     (its `createdAt` must be after your dispatch; a new run can take a few seconds to appear),
     then `gh run watch <run-id> --repo AISquare-Studio/aisquare-infra --exit-status`. It must end
     green, and
     `gh run view <run-id> --repo AISquare-Studio/aisquare-infra --log | grep pipe-service/<secret>-<tenant>`
     must show `nonprod/<env>/pipe-service/<secret>-<tenant>: updated`.
  3. Only then refresh the ExternalSecret. Its refresh interval is 1 h, the chart default, which
     pipe-service doesn't override. Or force it now with
     `kubectl -n <env> annotate externalsecret pipe force-sync=$(date +%s) --overwrite`. Then check
     that `kubectl -n <env> get externalsecret pipe -o jsonpath='{.status.refreshTime}'` is later
     than the run's end (`gh run view <run-id> --repo AISquare-Studio/aisquare-infra --json updatedAt`)
     and that `kubectl -n <env> get externalsecret pipe` shows `SecretSynced`. A refresh that ran
     before the sync finished copied the old blob, and it still shows `SecretSynced`.
  4. Restart the Deployment: `kubectl -n <env> rollout restart deployment/pipe-service`, then
     `kubectl -n <env> rollout status deployment/pipe-service`. The chart has no checksum
     annotation or reloader, so pods keep the old value until they restart.

To rotate the external ID without a gap:
1. The customer adds the new ID next to the old one in the trust policy
   (`"sts:ExternalId": ["<OLD>", "<NEW>"]`).
2. AISquare stores and rolls out the new blob (above).
3. Once every pod or task runs with it, the customer removes the old ID.

### Offboarding

AISquare's copy, per secret:
- ECS: remove the task definition's `secrets:` entry and deploy (as above). Then delete the
  secret: `aws secretsmanager delete-secret --region <region> --secret-id ais-pipe-service/<env>/<secret>-<tenant>`.
  Keep that order, because a task whose secret is gone does not start. The name stays taken
  for the recovery window (30 days by default), so to turn the tenant back on within it,
  restore the secret
  (`aws secretsmanager restore-secret --region <region> --secret-id ais-pipe-service/<env>/<secret>-<tenant>`)
  and `put-secret-value` the new blob instead of `create-secret`.
- EKS:
  1. Remove the `secretEnv` entry from `release.yaml` (a deploy). Do this first: while the entry
     is listed, a deleted secret fails the ExternalSecret.
  2. In aisquare-infra, open one PR that removes the `supplied-secrets.json` entry and the
     `secrets-nonprod.yml` env line, and the tenant's import block in
     `live/nonprod/data/imports.tf` if one is still there (below). Its `apply-nonprod` deletes
     the Secrets Manager secret, with a 7-day recovery window.
  3. After that merge, run
     `gh secret delete <GH_SECRET> --env nonprod-secrets --repo AISquare-Studio/aisquare-infra`.
     If you delete it earlier, while the entry is still listed, every sync stops.

  **Never delete the Secrets Manager secret by hand.** `delete-secret` keeps the name for its
  recovery window (30 days by default), and Terraform's AWS provider reads a secret that is
  pending deletion as gone. Until the window ends, while the entry is listed:
  - the next `apply-nonprod` of `live/nonprod/data` plans to create the secret, and
    `CreateSecret` fails ("already scheduled for deletion"). The apply fails, and its
    `secrets-nonprod` job is skipped;
  - `secrets-nonprod` fails on the secret and stops there, so no secret listed after it is
    written, other services' included;
  - the ExternalSecret `pipe` fails, which holds every later pipe-service release in that
    environment.

  To undo it:
  `aws secretsmanager restore-secret --region us-east-1 --secret-id nonprod/<env>/pipe-service/<secret>-<tenant>`.
  The value comes back with it. If an `apply-nonprod` ran on `live/nonprod/data` in between, its
  failure also dropped the secret from Terraform's state, so import it (below) as well. Then run
  `secrets-nonprod`, wait for it and refresh the ExternalSecret, as the EKS steps of "Changing
  the blob" do.

  **Turning the tenant back on within 7 days** of the offboarding apply fails the same way: the
  name is still pending deletion. Wait out the window, or restore the secret (above) first. Only
  then open the PR that re-adds the entry: the provider cannot import a secret that is still
  pending deletion, so a PR opened before the restore fails. In that PR, add an import block to
  `live/nonprod/data/imports.tf` like the ones already there:

  ```hcl
  import {
    to = aws_secretsmanager_secret.supplied["nonprod/<env>/pipe-service/<secret>-<tenant>"]
    id = "<ARN>"
  }
  ```

  `<ARN>` comes from
  `aws secretsmanager describe-secret --region us-east-1 --secret-id nonprod/<env>/pipe-service/<secret>-<tenant> --query ARN --output text`.
  The restored secret holds the old value until the sync writes the GitHub one.

  **Remove the import block once the `apply-nonprod` that imported it is green**, in a
  follow-up PR (the secret stays in Terraform's state). A block left behind outlives the entry:
  after the next offboarding removes the entry, every plan of `live/nonprod/data` fails
  ("Configuration for import target does not exist"), so its `apply-nonprod` fails and the sync
  is skipped. The two blocks already there are safe only while their entries stay listed.
- Remove the customer's role ARN from our role's policy (step 2's `Resource`).

The customer deletes the role, or removes AISquare's principal ARNs from its trust policy. Either
one stops new sessions. After a trust-policy change, a session AISquare already holds keeps
working until it expires (at most 1 hour). To end it at once, the customer revokes the role's
active sessions (IAM console: *Revoke active sessions*) or deletes the role. Deleting our copy
alone does not stop the customer's role from trusting us.

### If it fails

| Error | Check |
|---|---|
| `AWS refused sts:AssumeRole: …` (`forbidden`) | In the trust policy: the principal ARN, the external ID and `sts:TagSession`. In our role's policy: `sts:AssumeRole` and `sts:TagSession` on that role ARN. If it began at prod's EKS cutover: the trust policy lacks prod's Pod Identity role. If it began after our role was recreated: the customer must put its ARN back in the trust policy and save it again. |
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
