# aisquare-pipe-aisquare-gateway

Sink connector that POSTs envelopes to the AISquare Explainability gateway's
trace-ingest endpoint. Pairs with `aisquare-pipe-n8n` and other Tier-0 sources
that emit `application/x-aisquare-trace+json`.

## Install

```bash
cd connectors/aisquare-gateway
pip install -e ".[dev]"
```

## Configuration

```python
config = {
    "gateway_url": "https://gateway.aisquare.studio",
    "api_key": "aisquare-key",
    "ingest_path": "/traces/ingest",   # optional, default
    "timeout_seconds": 10,             # optional
    "max_retries": 3,                  # optional, retries 429/5xx
}
```

## Behavior

* Each envelope is POSTed as JSON to `{gateway_url}{ingest_path}`.
* Headers: `X-AISquare-API-Key`, `X-AISquare-Source-Id`, `X-AISquare-Content-Type`.
* `429` and `5xx` responses are retried up to `max_retries` with exponential
  backoff (0.5s, 1s, 2s, ...).
* Other `4xx` responses surface as `PushResult(success=False)`.
* On `2xx`, the gateway's response `trace_id` (if present) becomes
  `PushResult.ref`.
* `validate_config` probes `GET {gateway_url}/health` and expects HTTP 200.

## Running tests

```bash
pytest -v
```
