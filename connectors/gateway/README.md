# aisquare-pipe-gateway

Sink connector that POSTs envelopes to the AISquare Explainability gateway's
trace-ingest endpoint. Pairs with `aisquare-pipe-n8n` and other Tier-0 sources
that emit `application/x-aisquare-trace+json`.

## Install

```bash
cd connectors/gateway
pip install -e ".[dev]"
```

## Configuration

```python
config = {
    "gateway_url": "https://gateway.aisquare.studio",
    "api_key": "aisquare-key",
    "ingest_path": "/v1/traces/ingest", # optional, default
    "timeout_seconds": 10,              # optional
    "max_retries": 3,                   # optional, retries 429/5xx
}
```

## Behavior

* Each envelope is POSTed as JSON to `{gateway_url}{ingest_path}`.
* Headers: `X-API-KEY`, `X-AISquare-Source-Id`, `X-AISquare-Content-Type`.
  When the envelope carries an `idempotency_key` in its metadata, the sink
  lifts it onto the `X-Idempotency-Key` header so the gateway can dedupe
  retries and steady-state re-emissions.
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
