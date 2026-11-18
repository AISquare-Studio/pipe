# aisquare_pipe_gcs: Google Cloud Storage (read-only)

A toolkit (`gcs`) and a source (`gcs-source`) that read the files in ONE Google Cloud Storage bucket,
optionally under one prefix, with the version markers a caller needs to re-read a file when it changes.
Composio has no GCS toolkit; this is the native connector (GoodRx Smart Connectors, item 3b).

Ships inside the `aisquare-pipe` distribution: `pip install "aisquare-pipe[gcs]"` (or `[full]`, or
`[toolkits]`). The extra adds one dependency, `google-auth>=2.20,<3`, which signs the service account's
key into short-lived tokens. Without the extra, `gcs` and `gcs-source` count as not installed.

## What it does

| Tool | What it returns |
|---|---|
| `GCS_GET_OBJECT` (primary) | One object by name: its bytes as a file (text up to 1 MiB also inline as `content`), `content_type`, `size` and the version markers. `include_content=false` reads the metadata only. `generation` reads an older version. `max_bytes` (default 25 MiB, at most 100 MiB) bounds the download. |
| `GCS_LIST_OBJECTS` | The objects under a prefix with their version markers, `page_size` up to 1000 and `page_token` paging. `folders=true` lists one level, with the sub-prefixes in `folders`. |

`gcs-source` pull params: `prefix` (a narrower prefix inside the configured one) or `object_names` (a list),
`modified_after` (RFC 3339), `max_objects` (default 100, at most 1000), `max_bytes` per object (larger ones
are skipped and counted in one log line). Each envelope carries the object's bytes and `metadata`:
`bucket`, `object_name`, `filename`, `size`, `content_type`, `generation`, `md5_hash`, `updated`, `etag`,
`crc32c`, `time_created`. "Folder" placeholders (names ending in `/`) are skipped.

**Version markers.** `generation` changes on every content write, so store it and re-read the object when it
changes. `md5_hash` is the content's MD5 (absent for composite objects). `updated` is the last change
time (metadata changes move it too).

Read-only by construction: the token's scope is `devstorage.read_only`, the service account only needs
`roles/storage.objectViewer`, and no tool writes.

## Security

- The bucket, the prefix and the key come only from the tenant's server-side blob. A tool argument or pull
  param can't name another bucket (the schemas reject `bucket`, `service_account_json`), and a name or
  prefix outside the configured prefix is refused (`forbidden`; `ValueError` on a pull) before any request.
- **Application Default Credentials are never used**: no `google.auth.default()`, no
  `GOOGLE_APPLICATION_CREDENTIALS`, no metadata server. A missing or malformed blob fails closed with
  "Google Cloud Storage is not configured: <what is wrong>" (`not_connected`) and makes no request.
- The key's `token_uri` must be Google's token endpoint and its `universe_domain` `googleapis.com`, so the
  signed assertion only ever goes to Google. Every API call goes to `https://storage.googleapis.com`;
  redirects are not followed. Errors and logs never carry the key or a token.
- Tokens are cached in memory per key until five minutes before they expire.

## Registration checklist (the customer, then us)

In the customer's Google Cloud project:

1. Create a service account for AISquare, e.g. `aisquare-reader@<project>.iam.gserviceaccount.com`:
   `gcloud iam service-accounts create aisquare-reader --project <project>`.
2. Grant it read access on the ONE bucket (not the project):
   `gcloud storage buckets add-iam-policy-binding gs://<bucket> --member=serviceAccount:aisquare-reader@<project>.iam.gserviceaccount.com --role=roles/storage.objectViewer`.
   If the bucket uses a customer-managed KMS key, also grant the service account
   `roles/cloudkms.cryptoKeyDecrypter` on that key.
3. Create a JSON key for it and send it to us over a secure channel:
   `gcloud iam service-accounts keys create key.json --iam-account=aisquare-reader@<project>.iam.gserviceaccount.com`.
   (An org policy may block key creation: `iam.disableServiceAccountKeyCreation`. See "Later" below.)
4. Tell us the bucket name and, if the documents live under one folder, its prefix (e.g. `policies/`).

On our side, store one secret per tenant, which pipe-as-service reads as the tenant's credential blob for
both `gcs` and `gcs-source`:

- AWS Secrets Manager name: `<PIPE_SERVICE_SECRETS_PREFIX>/tenants/<tenant>/gcs`
  (local env backend: `PIPE_SECRET__<TENANT>__GCS__JSON`).
- Secret value, one JSON object:

```json
{
  "service_account_json": {
    "type": "service_account",
    "project_id": "<project>",
    "private_key_id": "<from key.json>",
    "private_key": "-----BEGIN PRIVATE KEY-----\n...\n-----END PRIVATE KEY-----\n",
    "client_email": "aisquare-reader@<project>.iam.gserviceaccount.com",
    "client_id": "<from key.json>",
    "token_uri": "https://oauth2.googleapis.com/token",
    "universe_domain": "googleapis.com"
  },
  "bucket": "<bucket>",
  "prefix": "policies/"
}
```

`service_account_json` is key.json as it was downloaded (an object, or the same JSON as a string).
`prefix` is optional: omit it to allow the whole bucket. Once the secret exists, pipe-as-service serves
`gcs` natively for that tenant within its credential TTL (no deploy); until then it answers 422
`connector_unavailable` / `not_configured` naming "credentials for gcs".

**Rotation:** create a new key, replace `service_account_json` in the secret, then delete the old key in
Google Cloud.

**Later (not built):** Workload Identity Federation from our AWS principal to the customer's service
account, which avoids a long-lived key; it needs setup on both sides.

## Tests

`pytest` from this directory, hermetic: core's socket guard blocks the network, and `tests/fakes.py`
plays Google (the token endpoint decodes the signed assertion; the storage API serves an in-memory bucket).
Keys are generated per run; no real key or account is used.
