# aisquare/pipe-n8n

A drop-in container that streams every n8n workflow execution into the
AISquare Explainability gateway. One image, two required env vars, no
changes to existing n8n workflows.

## Build

```bash
docker build -t aisquare/pipe-n8n:latest \
  -f connectors/n8n/docker/pipe-n8n/Dockerfile .
```

(Build context is the repo root — the Dockerfile copies the framework
and both connectors directly.)

## Run

```bash
docker run --rm \
  -e N8N_URL=http://n8n:5678 \
  -e N8N_API_KEY=$N8N_API_KEY \
  -e AISQUARE_GATEWAY_URL=https://gateway.aisquare.studio \
  -e AISQUARE_API_KEY=$AISQUARE_API_KEY \
  -v pipe-cursor:/var/lib/pipe \
  aisquare/pipe-n8n:latest
```

The named volume keeps the cursor file durable across restarts so the
bridge doesn't re-emit historical executions.

## docker-compose snippet

```yaml
services:
  pipe-n8n:
    image: aisquare/pipe-n8n:latest
    depends_on: [n8n]
    environment:
      N8N_URL: http://n8n:5678
      N8N_API_KEY: ${N8N_API_KEY}
      AISQUARE_GATEWAY_URL: ${AISQUARE_GATEWAY_URL}
      AISQUARE_API_KEY: ${AISQUARE_API_KEY}
    volumes:
      - pipe-cursor:/var/lib/pipe

volumes:
  pipe-cursor:
```

## Environment variables

| Env var | Required | Maps to | Notes |
|---|---|---|---|
| `N8N_URL` | yes | source.n8n_url | e.g. `http://n8n:5678` |
| `N8N_API_KEY` | yes | source.api_key | n8n API key |
| `N8N_POLL_INTERVAL` | no | source.poll_interval_seconds | seconds, integer |
| `N8N_WORKFLOW_FILTER` | no | source.workflow_id_filter | comma-separated workflow IDs |
| `N8N_CURSOR_PATH` | no | source.cursor_path | default `/var/lib/pipe/n8n-cursor.json` |
| `AISQUARE_GATEWAY_URL` | yes | sink.gateway_url | |
| `AISQUARE_API_KEY` | yes | sink.api_key | |
| `AISQUARE_INGEST_PATH` | no | sink.ingest_path | default `/v1/traces/ingest` |
| `AISQUARE_TIMEOUT_SECONDS` | no | sink.timeout_seconds | |
| `AISQUARE_MAX_RETRIES` | no | sink.max_retries | retries 429/5xx with exponential backoff |

Any required env var that is missing makes the container exit immediately
with a non-zero status and a single-line message on stderr.

## What flows through

For each n8n execution, the bridge POSTs one TraceBatch envelope per
emission: a `stub` when the run is first seen, `progress` envelopes as
nodes complete, and a `final` envelope once the run is `finished`. All
envelopes use the content type `application/x-aisquare-trace+json` and
carry execution + workflow identifiers and a stable `idempotency_key` in
their metadata. The gateway dedupes by `X-Idempotency-Key` so retries and
steady-state polls collapse to no-ops.
