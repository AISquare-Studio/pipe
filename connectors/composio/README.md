# aisquare-pipe-composio

One API key, 500+ SaaS toolkits: Gmail, Slack, GitHub, Notion, Google Drive, HubSpot, ... — this meta-connector bridges [aisquare.pipe](../../README.md) to the entire [Composio](https://composio.dev) catalog, so any pipe source can feed a Composio app and any Composio app can feed a pipe sink. Ships in `aisquare-pipe[full]`.

## Install

```bash
pip install aisquare-pipe-composio
```

For development:

```bash
pip install -e "connectors/composio[dev]"
```

## How it works

| Composio concept | Meaning here |
|---|---|
| **toolkit** | An app (gmail, slack, github, ...) |
| **tool** | One action in a toolkit, addressed by slug (`GMAIL_FETCH_EMAILS`, `SLACK_SEND_MESSAGE`) |
| **user_id** | The end user (Composio "entity") tools execute as — `"default"` for single-user setups |
| **connected account** | A user's authenticated link to an app. Composio runs the OAuth and stores/refreshes the tokens |

The connector executes tools: `composio-source` runs a tool and yields its results as envelopes; `composio-sink` runs a tool with envelope data as the arguments; `composio-triggers-source` polls trigger events (new email, new row, ...). The only credential pipe ever sees is your Composio API key.

## Configuration

All three connectors share the same config shape:

```python
config = {
    "api_key": "ck_...",              # required — Composio API key
    "user_id": "default",             # entity tools run as (set per end-user in multi-tenant setups)
    "connected_account_id": None,     # optional — pin execution to one connected account
    "toolkit_filter": ["gmail"],      # optional — allow-list of toolkit slugs (governance)
    "base_url": None,                 # optional — Composio backend override
    "timeout_seconds": 60,            # optional — request timeout
    "file_workdir": None,             # optional — directory for file uploads/downloads
}
```

The `COMPOSIO_API_KEY` environment variable is intentionally **not** read — config is the single source of truth, matching every other pipe connector.

## Connect an account

Tool execution requires the user to have an ACTIVE connected account for the tool's toolkit (except no-auth toolkits like Hacker News). Either connect in the [Composio dashboard](https://platform.composio.dev), or programmatically:

```python
from aisquare_pipe_composio import initiate_connection, wait_for_active, connection_status

request = initiate_connection(config, "gmail")
print("Authorize at:", request.redirect_url)   # send the user here
account = wait_for_active(config, request.id)  # blocks until OAuth completes
print(connection_status(config, "gmail"))      # "ACTIVE"
```

`ComposioSource().list_resources(config)` browses all toolkits with your connection status per toolkit.

## Pull (composio-source)

```python
from aisquare.pipe import PullParams
from aisquare_pipe_composio import ComposioSource

source = ComposioSource()
params = PullParams(params={
    "tool": "GMAIL_FETCH_EMAILS",
    "arguments": {"max_results": 10},
    "unwrap": True,                  # one envelope per message instead of one blob
})
for envelope in source.pull(config, params):
    print(envelope.metadata["composio_tool"], envelope.data)
```

PullParams keys:

- `tool` (required) — tool slug, case-insensitive
- `arguments` — tool input arguments (see the tool's schema in Composio docs)
- `unwrap` — `False` (default): one envelope with the whole result; `True`: auto-fan-out when the result is a list or a single-list-key dict; `"dot.path"`: explicit path to the list (raises if not a list). Unwrapped envelopes carry `item_index`/`item_count` metadata.
- `download_files` — also yield one **bytes** envelope per file output (real MIME type, `filename` + `file_field` metadata). Files are materialised under `file_workdir`.
- `user_id`, `connected_account_id`, `tool_version` — per-call overrides

## Push (composio-sink)

```python
from aisquare.pipe import DataEnvelope, PushParams
from aisquare_pipe_composio import ComposioSink

sink = ComposioSink()
envelope = DataEnvelope(
    content_type="application/json",
    data={"channel": "#alerts", "text": "deploy finished"},
    source_id="my-app",
)
result = sink.push(envelope, config, PushParams(params={"tool": "SLACK_SEND_MESSAGE"}))
assert result.success
```

Argument layering (later wins, shallow per-key merge):

1. **Envelope payload** — a JSON-object envelope *is* the base arguments; with `data_key="text"` the payload is nested under that argument; with `file_arg="file"` a binary envelope is uploaded and the file reference passed in that argument
2. `envelope.metadata["composio_arguments"]` — per-envelope steering from upstream
3. `params["arguments"]` — operator overrides, always win

```python
# Text envelope into a named argument:
PushParams(params={"tool": "SLACK_SEND_MESSAGE", "data_key": "text",
                   "arguments": {"channel": "#general"}})

# Binary envelope as a file upload:
PushParams(params={"tool": "GOOGLEDRIVE_UPLOAD_FILE", "file_arg": "file_to_upload"})
```

The tool's response data is returned in `PushResult.metadata["data"]`.

## Toolkit-pinned connectors (factory)

```python
from aisquare.pipe import Pipeline, PullParams, PushParams
from aisquare_pipe_composio import composio_source, composio_sink

GmailSource = composio_source("gmail")     # name: composio-gmail-source
SlackSink = composio_sink("slack")         # name: composio-slack-sink

result = Pipeline(source=GmailSource(), sink=SlackSink()).run(
    {"composio-gmail-source": config, "composio-slack-sink": config},
    pull_params=PullParams(params={"tool": "GMAIL_FETCH_EMAILS", "unwrap": True}),
    push_params=PushParams(params={"tool": "SLACK_SEND_MESSAGE", "data_key": "text",
                                   "arguments": {"channel": "#inbox"}}),
)
```

Pinned classes reject tools from other toolkits and scope `list_resources()` to their toolkit. They are deliberately **not** entry-point registered (`pipe list` shows the three generic connectors only): entry points are static, Composio has ~500 toolkits — build what you need on demand.

## Trigger events (composio-triggers-source)

Prerequisite: enable trigger instances in Composio (dashboard → toolkit → triggers), e.g. `GMAIL_NEW_GMAIL_MESSAGE`. Then:

```python
from aisquare.pipe import PullParams
from aisquare_pipe_composio import ComposioTriggersSource

source = ComposioTriggersSource()
config = {
    "api_key": "ck_...",
    "user_id": "default",
    "trigger_slugs": ["GMAIL_NEW_GMAIL_MESSAGE"],   # optional filter
    "poll_interval_seconds": 10,
    "cursor_path": "/tmp/composio-pipe-cursor.json",
}
for envelope in source.pull(config):               # polls forever
    print(envelope.data["payload"])
```

- One `application/json` envelope per event; `envelope.data["payload"]` is the app payload.
- `idempotency_key` metadata (`composio:event:<id>`) is stable across re-polls, pairing with sinks that dedupe (e.g. aisquare-gateway).
- Position is a timestamp watermark + bounded seen-id ring persisted atomically to `cursor_path`; `PullParams` `since` sets the initial watermark (default: now). `max_polls`/`sleep` params support tests and one-shot drains.
- `list_resources(config)` browses available trigger types and your active trigger instances.

## Example pipelines

```python
from aisquare.pipe import Pipeline, PullParams, PushParams
from aisquare_pipe_composio import ComposioSource, ComposioSink

# Save Gmail attachments to disk (composio → local)
from aisquare_pipe_local import LocalSink
Pipeline(source=ComposioSource(), sink=LocalSink()).run(
    {"composio-source": config, "local-sink": {"base_path": "/tmp/attachments"}},
    pull_params=PullParams(params={
        "tool": "GMAIL_GET_ATTACHMENT",
        "arguments": {"message_id": "...", "attachment_id": "...", "file_name": "x.pdf"},
        "download_files": True,
    }),
)

# Salesforce records into Notion (salesforce → composio)
from aisquare_pipe_salesforce import SalesforceSource
Pipeline(source=SalesforceSource(), sink=ComposioSink()).run(
    {"salesforce-source": sf_config, "composio-sink": config},
    pull_params=PullParams(params={"object_type": "Account", "limit": 10}),
    push_params=PushParams(params={
        "tool": "NOTION_ADD_PAGE_CONTENT",
        "data_key": "content",
        "arguments": {"parent_block_id": "..."},
    }),
)
```

## Features

- Whole Composio catalog through three connectors + an on-demand factory
- Retry with exponential backoff on rate limits (HTTP 429)
- SDK exceptions mapped to framework errors; failed pushes return `PushResult(success=False)`, never raise
- `toolkit_filter` allow-listing for governance
- File outputs → bytes envelopes with real MIME types; binary envelopes → file-upload arguments (uploads restricted to the connector's own workdir)
- Connection status surfaced in `list_resources()`; programmatic OAuth helpers

## Notes & limitations

- **Toolkit pinning is a slug-prefix check** (`GMAIL_*` belongs to `gmail`). It avoids a per-call API lookup; use `list_resources()`/Composio docs for ground truth on slugs.
- **Multi-tenant deployments must set `user_id` per end-user** — otherwise everything executes as `"default"` against whatever account that entity has connected.
- **File downloads are buffered in memory** when yielded as envelopes (the SDK enforces a 100 MB cap per file); streamed envelopes are a future upgrade.
- **Trigger polling reads Composio's event log** (the SDK's first-class trigger interface is a realtime websocket). The log endpoint is versioned under `/api/v3.1/internal/` — pinned SDK versions keep this stable, but it is the most drift-prone surface; all access is isolated in `client.py`.
- **Tool arguments and results are never logged** — only tool slugs and counts.
- The `composio` SDK is pinned `>=0.13.1,<2.0` (verified against 0.13.1 / SDK v1.0.0-rc2). All SDK touchpoints live in `client.py`; version bumps should only ever touch that file.
