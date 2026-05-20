# aisquare-pipe-n8n

n8n source connector for [aisquare.pipe](../../README.md). Polls n8n's
Executions API and emits one trace envelope per workflow-start, node-step,
and workflow-complete event.

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
}
```

All keys are documented in `N8nSource.CONFIG_SPEC`.

## Output

Single content type: `application/x-aisquare-trace+json`. Each envelope's
`data` carries an `event` discriminator:

| event | when | data extras |
|---|---|---|
| `workflow_start` | first envelope per execution | `started_at`, `mode` |
| `node_step` | per executed node run | `node_name`, `input_items`, `output_items`, `ai` (LangChain nodes) |
| `workflow_complete` | once n8n marks `finished=true` | `status`, `stopped_at` |

Envelope `metadata` always carries `n8n_execution_id`, `n8n_workflow_id`,
`n8n_workflow_name`, and `n8n_event`.

## Cursor durability

The connector persists the highest seen execution ID to `cursor_path` after
each poll. Restarting the process resumes at the next execution — historical
runs are not re-emitted.

## Running tests

```bash
pytest -v
```
