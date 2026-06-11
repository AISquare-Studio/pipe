# aisquare-pipe-n8n

n8n source connector for [aisquare.pipe](../../README.md). Polls n8n's
Executions API and emits one TraceBatch envelope per execution emission:
a stub on first sight, progress emissions as nodes complete, and a final
emission once the run finishes.

## Install

```bash
cd connectors/n8n
pip install -e ".[dev]"
```

## Configuration

```python
config = {
    "n8n_url": "http://n8n:5678",
    "api_key": "n8n-api-key",
    "poll_interval_seconds": 5,                 # optional, default 5
    "cursor_path": "/var/lib/pipe/cursor.json", # optional
    "workflow_id_filter": ["wf-1", "wf-7"],     # optional
    "include_running": True,                    # optional, default True
}
```

All keys are documented in `N8nSource.CONFIG_SPEC`.

## Output

Single content type: `application/x-aisquare-trace+json`. Each envelope's
`data` is a full TraceBatch dict (`{trace_id, spans: [...]}`) and the
envelope `metadata["n8n_event"]` carries the emission discriminator:

| n8n_event | when | idempotency key |
|---|---|---|
| `stub` | first sight of an in-progress run; spans are pending | `n8n:stub:<trace_id>` |
| `progress` | partial `runData` has accumulated for an in-progress run | `n8n:progress:<trace_id>:<fingerprint>` |
| `final` | n8n marks `finished=true` | `n8n:final:<trace_id>` |

Envelope `metadata` always carries `n8n_execution_id`, `n8n_workflow_id`,
`n8n_workflow_name`, `n8n_event`, `trace_id`, and `idempotency_key`. The
sink lifts `idempotency_key` onto the gateway's `X-Idempotency-Key` header.

## Cursor durability

The connector persists the highest seen execution ID to `cursor_path` after
each poll. Restarting the process resumes at the next execution — historical
runs are not re-emitted.

`cursor_path` defaults to `~/.cache/aisquare-pipe/n8n-cursor.json` (honouring
`$XDG_CACHE_HOME`) — per-user, not shared `/tmp`. A pre-0.2.1 cursor at
`/tmp/n8n-pipe-cursor.json` is migrated automatically on first run.

## Running tests

```bash
pytest -v
```
