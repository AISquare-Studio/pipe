# n8n ↔ AISquare Explainability — Integration Guide

A complete picture of how n8n workflow runs flow into AISquare Explainability —
the pipe-based connector, the gateway sink, the SDK's in-process bridge, and the
path forward.

> **TL;DR**
> * **n8n connector** = a pipe-style Source that polls n8n and emits DataEnvelopes.
> * **aisquare-gateway sink** = the universal cable that turns any source's envelopes
>   into TraceBatches and POSTs them. Reusable across future sources.
> * **Cloud n8n → Explainability** = container polls n8n REST API, transforms,
>   POSTs to gateway, gateway pushes to FE via SSE.
> * **Docker wrapper (`connectors/n8n/docker/pipe-n8n/`)** = the BYO-bridge
>   container — one image, two env vars, no Python, no n8n workflow changes.
> * **Why we kept the SDK version** = it does stub/progress emit, definition
>   enrichment, span-kind classification, and idempotency that the pipe version
>   doesn't yet do — and the FE depends on those.
> * **Path forward** = extract the wire format into a shared protocol library;
>   both code paths consume it; no drift; new sources reuse the sink for free.

---

## 1. The n8n connector — what it is and how it's shaped

A **Source plug** for the `aisquare.pipe` framework. It polls n8n's REST API on
a schedule, turns every workflow execution into a stream of universal
`DataEnvelope` objects, and persists a cursor so restarts don't replay history.

It's one of many possible sources. The pipe framework treats it identically to
a Dropbox source or a Local-filesystem source — same `pull()` contract, same
envelope shape.

### Internals

```
                     ┌──────────────────────────────────────┐
                     │           N8nSource (pipe)            │
                     ├──────────────────────────────────────┤
                     │                                      │
   config: {         │   ┌─────────────────────────────┐    │
     n8n_url     ───►│   │  N8nClient                  │    │
     api_key     ───►│   │   GET /executions           │────┼──► n8n REST API
     poll_seconds    │   │   GET /workflows            │    │     (X-N8N-API-KEY)
     cursor_path     │   └─────────────┬───────────────┘    │
     workflow_filter │                 │                    │
   }                 │                 ▼                    │
                     │   ┌─────────────────────────────┐    │
                     │   │  For each new execution:    │    │
                     │   │   1. yield workflow_start   │    │
                     │   │   2. yield node_step (×N)   │    │  DataEnvelope
                     │   │   3. yield workflow_complete│────┼──►   stream
                     │   └─────────────┬───────────────┘    │     out
                     │                 │                    │
                     │                 ▼                    │
                     │   ┌─────────────────────────────┐    │
                     │   │  Save cursor to JSON file   │────┼──► /var/lib/pipe/
                     │   │  (last execution id seen)   │    │     cursor.json
                     │   └─────────────────────────────┘    │
                     │                                      │
                     │   Sleep poll_interval_seconds.       │
                     │   Loop.                              │
                     └──────────────────────────────────────┘
```

### What one execution becomes

```
   n8n execution #1009  (workflow 42: "Newsletter Builder")
   ───────────────────────────────────────────────────────
   Webhook  ──▶  AI Agent  ──▶  Send Email
                              ◀──── finished=true

                 │
                 │   N8nSource.pull() yields 5 envelopes
                 ▼

   ① workflow_start    ─┐
   ② node_step Webhook  │  all share metadata:
   ③ node_step AI Agent │   n8n_execution_id = "1009"
   ④ node_step Email    │   n8n_workflow_id  = "42"
   ⑤ workflow_complete ─┘   n8n_event        = (per envelope)

   Every envelope's content_type = "application/x-aisquare-trace+json"
```

The envelope body itself stays universal:

```python
DataEnvelope(
    content_type = "application/x-aisquare-trace+json",
    source_id    = "n8n",
    data         = {"event": "node_step", "node_name": "AI Agent", …},
    metadata     = {"n8n_execution_id": "1009", "n8n_workflow_id": "42", …},
)
```

Anything downstream — the sink, the gateway, the FE — only needs to know about
this universal shape.

---

## 2. The aisquare-gateway sink — and why it gets reused

A **Sink plug** for the same framework. Its job has two halves:

1. **Transport.** POST each envelope to the AISquare gateway's
   `/v1/traces/ingest` endpoint with the right headers, retry 429/5xx with
   exponential backoff, surface non-retryable HTTP errors.
2. **Reshape.** Convert each universal `DataEnvelope` into the gateway's wire
   format — an OTel-style `TraceBatch` (`{trace_id, spans[]}`) with parent/child
   links across an execution.

### Why this matters for *future* sources

```
   TODAY  (one source):                  FUTURE  (many sources, same sink):

   ┌──────────┐                          ┌──────────┐
   │   n8n    │                          │   n8n    │──┐
   └────┬─────┘                          └──────────┘  │
        │                                ┌──────────┐  │
        │                                │  Zapier  │──┤
        │                                └──────────┘  │
        ▼                                ┌──────────┐  ├──►  aisquare-gateway sink
   ┌──────────────────┐                  │   Make   │──┤      (envelope → TraceBatch
   │ aisquare-gateway │                  └──────────┘  │       + HTTP POST + retry)
   │     sink         │                  ┌──────────┐  │            │
   └────────┬─────────┘                  │ Airflow  │──┤            ▼
            │                            └──────────┘  │     AISquare gateway
            ▼                            ┌──────────┐  │
     gateway HTTP                        │ Custom py│──┘
                                         └──────────┘
```

When we add a Zapier source next quarter, **we don't touch the sink**. Zapier
source emits `DataEnvelope`s with the same `content_type`; the sink already
knows how to turn that into a TraceBatch and POST it.

That's the whole point of having the sink as a separate plug:
**N sources × 1 sink** instead of **N sources × N transports**.

### Concrete reuse rules

| New source | Build | Reuse |
|---|---|---|
| Zapier task history | `aisquare-pipe-zapier` source (polls Zapier API) | aisquare-gateway sink |
| Airflow DAG runs | `aisquare-pipe-airflow` source (polls Airflow REST) | aisquare-gateway sink |
| Custom Python scripts | `aisquare-pipe-pyhook` source (file-based event log) | aisquare-gateway sink |
| Make.com scenarios | `aisquare-pipe-make` source | aisquare-gateway sink |

Each new source ships one new Docker image (`aisquare/pipe-zapier`,
`aisquare/pipe-airflow`, …) that pairs the new source with the same sink and
the same `pipe run` CLI.

---

## 3. The full flow — n8n.aisquare.studio → Explainability

Concrete example. You have `https://n8n.aisquare.studio` with two workflows:

- **wf-42 "Newsletter Builder"** — Webhook → AI Agent → Send Email
- **wf-91 "Lead Scorer"** — Schedule Trigger → HTTP Request → Score Lead

Both run multiple times a day. Here's exactly what happens.

### Visual

```
   ┌─────────────────────────────────────────┐
   │     n8n.aisquare.studio                 │
   │  ┌──────────────────────────────────┐   │
   │  │ wf-42 "Newsletter Builder"       │   │   user clicks Execute,
   │  │   run #1009  finished=true       │   │   or schedule fires
   │  │ wf-91 "Lead Scorer"              │   │
   │  │   run #1010  finished=true       │   │
   │  └──────────────────────────────────┘   │
   └─────────────────┬───────────────────────┘
                     │
                     │   GET /api/v1/executions
                     │     header: X-N8N-API-KEY
                     │
   ┌─────────────────▼───────────────────────┐
   │     pipe-n8n  (Docker container)         │
   │     ── runs anywhere with outbound       │
   │        HTTPS to n8n + to gateway         │
   │                                          │
   │   ┌─────────────────────────────────┐   │
   │   │ N8nSource.pull()                │   │
   │   │  cursor.json: last_id = 1008    │   │
   │   │  sees 1009 & 1010 → yields:     │   │
   │   │   5 envelopes for run 1009      │   │
   │   │   4 envelopes for run 1010      │   │
   │   └────────────┬────────────────────┘   │
   │                │                         │
   │                ▼                         │
   │   ┌─────────────────────────────────┐   │
   │   │ AISquareGatewaySink.push()      │   │
   │   │  envelope → TraceBatch spans    │   │
   │   │  POST + retry 429/5xx           │   │
   │   └────────────┬────────────────────┘   │
   │                │                         │
   │   cursor.json (mounted volume)           │
   │     written back: last_id = 1010         │
   └────────────────┼─────────────────────────┘
                    │
                    │   POST /v1/traces/ingest
                    │     header: X-API-KEY
                    │     body: {trace_id, spans[]}
                    │
   ┌────────────────▼─────────────────────────┐
   │     AISquare Explainability Gateway      │
   │                                          │
   │   auth → structural worker (sync)        │
   │            ↓                             │
   │   Neo4j (graph) + Postgres + Redis       │
   │            ↓                             │
   │   Redis pub/sub: node_added / edge_added │
   └────────────────┬─────────────────────────┘
                    │   SSE  (long-lived push)
                    ▼
   ┌──────────────────────────────────────────┐
   │     Explainability FE  (browser)         │
   │                                          │
   │   trace_id = n8n-wf-42-1009              │
   │     ├ workflow_start span                │
   │     ├ node Webhook                       │
   │     ├ node AI Agent                      │
   │     ├ node Send Email                    │
   │     └ workflow_complete                  │
   │                                          │
   │   trace_id = n8n-wf-91-1010              │
   │     ├ workflow_start span                │
   │     └ … etc                              │
   └──────────────────────────────────────────┘
```

### Step-by-step

```
   ① User clicks "Execute" on "Newsletter Builder" inside n8n
   ②   n8n schedules execution #1009 of wf-42
   ③   Webhook → AI Agent → Send Email runs end-to-end
   ④   n8n marks the execution finished=true in its database

   …meanwhile, every poll_interval_seconds…

   ⑤ pipe-n8n container polls n8n.aisquare.studio
   ⑥   Reads cursor.json: last seen = 1008
   ⑦   GET /api/v1/executions?limit=100&includeData=true
   ⑧   Filters client-side to id > 1008 → sees 1009 and 1010
   ⑨   For each execution, N8nSource yields:
         ─ workflow_start envelope
         ─ node_step envelope (one per node)
         ─ workflow_complete envelope
   ⑩   Pipeline.run() iterates each envelope through the sink

   ⑪ For each envelope:
       AISquareGatewaySink converts → TraceBatch (one span per envelope)
       POST /v1/traces/ingest with X-API-KEY header
   ⑫   Gateway authenticates, then store_ingest_batch():
         ─ writes Run + Span nodes to Neo4j
         ─ writes artifact content to Postgres
         ─ publishes node_added / edge_added to Redis pub/sub
   ⑬   FE (SSE-subscribed) receives push, renders graph live
   ⑭ pipe-n8n container writes cursor.json: last seen = 1010
```

### Customer's deployment

```bash
docker run -d --name pipe-n8n --restart unless-stopped \
  -e N8N_URL=https://n8n.aisquare.studio \
  -e N8N_API_KEY="$N8N_API_KEY" \
  -e AISQUARE_GATEWAY_URL=https://gateway.aisquare.studio \
  -e AISQUARE_API_KEY="$AISQUARE_API_KEY" \
  -v pipe-cursor:/var/lib/pipe \
  aisquare/pipe-n8n:latest
```

One container. Two real env vars. Cursor volume keeps the bookmark durable.
Restarts pick up where it left off — no replay of historical executions.

---

## 4. The Docker wrapper — `connectors/n8n/docker/pipe-n8n/`

The pipe framework and the two connectors are pure Python wheels. To turn that
into something a customer can deploy without writing any code, we ship a
purpose-built container image. **This is the artifact a customer actually runs.**

### What's in the folder

```
   connectors/n8n/docker/pipe-n8n/
   ├── Dockerfile          ← image recipe (python:3.11-slim + wheels)
   ├── entrypoint.py       ← env vars → JSON config → exec `pipe run`
   ├── README.md           ← docker-compose snippet for customers
   └── test_entrypoint.py  ← unit tests for the env translation
```

### What's inside the image

```
   ┌────────────────────────────────────────────────────────────┐
   │   aisquare/pipe-n8n:latest                                 │
   │   ────────────────────────                                 │
   │                                                            │
   │   base:    python:3.11-slim                                │
   │                                                            │
   │   pip-installed:                                           │
   │     • aisquare-pipe              (framework + CLI)         │
   │     • aisquare-pipe-n8n          (source connector)        │
   │     • aisquare-pipe-gateway      (sink connector)          │
   │                                                            │
   │   /usr/local/bin/pipe-n8n-entrypoint  ← entrypoint.py      │
   │   /var/lib/pipe/                       ← cursor volume     │
   │                                                            │
   │   ENTRYPOINT: pipe-n8n-entrypoint                          │
   └────────────────────────────────────────────────────────────┘
```

### What happens when you `docker run` it

```
   1.  docker run … aisquare/pipe-n8n
                │
                ▼
   2.  entrypoint.py:
         reads env vars:
           N8N_URL, N8N_API_KEY                    (required)
           AISQUARE_GATEWAY_URL, AISQUARE_API_KEY  (required)
           N8N_POLL_INTERVAL, N8N_WORKFLOW_FILTER, …  (optional)
         missing required var?  print error to stderr, exit 1
         otherwise:  write /tmp/pipe-config.json
                │
                ▼
   3.  execvp("pipe", "run",
               "--source", "n8n-source",
               "--sink",   "aisquare-gateway-sink",
               "--config", "/tmp/pipe-config.json")
                │
                ▼
   4.  pipe runs forever:
         poll n8n → transform envelopes → POST gateway
       cursor file at /var/lib/pipe survives container restarts.
```

`execvp` (instead of `subprocess.run`) means the entrypoint *replaces itself*
with the `pipe` process — signals propagate cleanly and the container's
lifecycle matches the pipeline's.

### Without the image vs with the image

Without:

```
   • install Python 3.11 on a host
   • pip install aisquare-pipe + both connectors
   • write a script that builds Pipeline() and calls .run()
   • configure systemd / supervisor / launchd to keep it alive
   • figure out cursor persistence yourself
```

With:

```bash
docker run -d --restart unless-stopped \
  -e N8N_URL=https://n8n.aisquare.studio \
  -e N8N_API_KEY=$N8N_API_KEY \
  -e AISQUARE_GATEWAY_URL=https://gateway.aisquare.studio \
  -e AISQUARE_API_KEY=$AISQUARE_API_KEY \
  -v pipe-cursor:/var/lib/pipe \
  aisquare/pipe-n8n:latest
```

### When to use the container

The container is **the "BYO-bridge" deployment mode**. Use it when:

* The customer's n8n is **behind a firewall / on their VPC** — the SDK gateway
  can't poll it directly, but the customer can run a container internally with
  outbound HTTPS to both n8n and the gateway. *This is the main reason it exists.*
* The customer is **self-hosting n8n on prem** and you don't want their
  credentials in the gateway DB.
* You're prototyping a **new pipe source** and want a clean way to ship it to
  customers (the same Dockerfile pattern works for `pipe-zapier`, `pipe-airflow`,
  etc.).

If the SDK gateway can already reach the customer's n8n directly (the
`n8n.aisquare.studio` case), the container is optional — the SDK's in-process
connector handles it. See section 5.

---

## 5. Why we kept the SDK's in-process n8n connector

The SDK already has a much richer n8n integration at
`gateway/connectors/n8n.py` (~880 lines). We did **not** replace it with the
pipe-based one — because it does things the pipe version doesn't.

### Side-by-side

```
   IN-PROCESS SDK CONNECTOR (today's prod)         PIPE CONNECTOR (new, basic)

   ┌──────────────────────────────────────┐        ┌─────────────────────────┐
   │ Lives inside gateway/ FastAPI process│        │ Standalone container     │
   │ Multi-tenant: one asyncio task per   │        │ Single tenant per        │
   │ studio_id                            │        │ container                │
   │                                      │        │                          │
   │ Three kinds of span emit per cycle:  │        │ One kind of emit:        │
   │   1. STUB on first sight             │        │                          │
   │      (FE sees full graph shape       │        │   • FINAL only           │
   │       immediately, before run ends)  │        │     trace appears only   │
   │   2. PROGRESS as nodes complete      │        │     after execution      │
   │      (FE updates live mid-run)       │        │     finishes             │
   │   3. FINAL when execution done       │        │                          │
   │                                      │        │                          │
   │ Calls /api/v1/workflows/{id} to      │        │ Uses /executions data    │
   │ pre-fetch graph shape                │        │ only                     │
   │                                      │        │                          │
   │ classify_node_kind:                  │        │ No classification        │
   │   "LLM"  for openai/claude/chain/…   │        │   FE icons all generic   │
   │   "TOOL" for http/webhook/sheets/…   │        │                          │
   │   → drives FE icon + per-kind tabs   │        │                          │
   │                                      │        │                          │
   │ Emits agent.name + agent.metadata.*  │        │ Missing — FE renders     │
   │   FE shows real workflow name        │        │   "Unknown agent"        │
   │                                      │        │                          │
   │ Idempotency keys:                    │        │ No dedupe                │
   │   n8n:stub:trace_id                  │        │                          │
   │   n8n:progress:trace_id:fingerprint  │        │                          │
   │   n8n:final:trace_id                 │        │                          │
   │                                      │        │                          │
   │ Cursor in Postgres                   │        │ Cursor in JSON file      │
   │ Auto-disable after N failures        │        │ No auto-disable          │
   └──────────────────────────────────────┘        └─────────────────────────┘
```

### What it looks like in the FE today vs with pipe

| FE feature                              | SDK in-process  | pipe sink |
|----------------------------------------|-----------------|-----------|
| Run labeled with workflow name          | ✅ via `agent.name` | ❌ shows "Unknown" |
| Per-node icons (LLM / tool / chain)     | ✅ via `openinference.span.kind` | ❌ all generic |
| Graph shape visible while still running | ✅ stub spans | ❌ appears only after finish |
| Live node-completion updates            | ✅ progress spans | ❌ |
| Tool input/output panels populated      | ✅ via `tool.name` / `tool.return_value` | ❌ |
| LLM response panel populated            | ✅ via `llm.output_messages.*` | ❌ |
| Duration computed correctly             | ✅ nano-precision `start_time` | ⚠️ ISO string in attrs |

So: the pipe connector is **not yet a drop-in replacement** for the SDK one. It's a
fine starting point for the "n8n behind a firewall" deployment shape (we can't
reach the customer's n8n from our gateway), but for the existing SDK-hosted
setup the in-process version is materially better.

### Decision rule for which one to use

```
   ┌──────────────────────────────────────────────────────────┐
   │ Can the SDK gateway reach the customer's n8n directly?   │
   └──────────────┬───────────────────────────────────────────┘
                  │
        ┌─────────┴─────────┐
        │                   │
       YES                 NO
        │                   │
        ▼                   ▼
   ┌───────────┐      ┌────────────────────┐
   │ SDK       │      │ pipe-n8n container │
   │ in-process│      │ runs on customer   │
   │ connector │      │ side, only needs   │
   │           │      │ outbound HTTPS     │
   └───────────┘      └────────────────────┘
```

---

## 6. Migration plan — moving to the pipe connector cleanly

The goal isn't to delete the SDK code; it's to **consolidate so the transform
exists in exactly one place**. The pipe connector and the SDK orchestrator
should both use the same wire-format library.

### The three problems to solve

```
   Problem 1: TRANSFORM DRIFT
   ──────────────────────────
   gateway/connectors/n8n.py :: execution_to_spans
   pipe/.../aisquare_pipe_gateway/sink.py :: _payload_for
   Two functions, same job, already diverging.

   Problem 2: WHERE DOES THE WIRE FORMAT BELONG?
   ─────────────────────────────────────────────
   It's the gateway's contract, not pipe's. It should
   live where the gateway lives — the SDK repo.

   Problem 3: WHAT ABOUT FUTURE NON-n8n SOURCES?
   ─────────────────────────────────────────────
   Zapier / Airflow / Make would each duplicate the
   transform unless we extract the protocol first.
```

### The migration in five steps

```
   ─── Step 1 ─────────────────────────────────────────────────────────
   In the SDK repo, extract a new sub-package:

       AISquare-Explainability-SDK/
       └── gateway/
           └── protocol/
               ├── trace_batch.py        (TraceBatch dataclasses)
               ├── n8n_transform.py      (execution_to_spans,
               │                          execution_to_stub_spans)
               ├── span_ids.py           (_safe_id, _to_nano, _attr_safe)
               └── openinference_kinds.py (LLM/TOOL/CHAIN classifier)

   Same code as today — just moved out of gateway/connectors/n8n.py.
   Pure functions over dicts. Zero runtime deps.

   ─── Step 2 ─────────────────────────────────────────────────────────
   Publish that sub-package as a small pip wheel:

       aisquare-gateway-protocol  v0.1.0

   Versioned alongside the gateway. Owned by the gateway team.

   ─── Step 3 ─────────────────────────────────────────────────────────
   In pipe, update aisquare-pipe-gateway:

       pyproject.toml:
         dependencies = [
           "aisquare-pipe>=0.1.0",
           "aisquare-gateway-protocol>=0.1.0",  # NEW
           "requests>=2.31",
         ]

       sink.py:
         from aisquare_gateway_protocol import envelope_to_trace_batch
         …
         payload = envelope_to_trace_batch(envelope)   # not _payload_for()

   Delete pipe's local _payload_for. Sink shrinks to transport-only.

   ─── Step 4 ─────────────────────────────────────────────────────────
   In pipe, update aisquare-pipe-n8n source:

       Instead of pre-shaping {event, execution_id, …} envelopes, yield
       the raw n8n execution dict (or a thin n8n-shaped wrapper). The
       protocol library does the full transform.

       → less code in the source
       → no shape drift between SDK and pipe
       → adding stub/progress emit later is one library upgrade

   ─── Step 5 ─────────────────────────────────────────────────────────
   In the SDK, slim down gateway/connectors/n8n.py:

       Remove the embedded execution_to_spans function.
       Import from aisquare_gateway_protocol instead.

       Keep:  the orchestrator
              postgres state
              stub / progress / final emit logic
              idempotency keys
              auto-disable
              — these are SDK-specific, not protocol concerns.
```

### What the architecture looks like after migration

```
   ┌─────────────────────────────────────────────────────────────────┐
   │   aisquare-gateway-protocol  (published from SDK repo)           │
   │                                                                  │
   │     execution_to_spans()         ◄── used by BOTH paths           │
   │     execution_to_stub_spans()    ◄── owns the gateway's          │
   │     classify_node_kind()             wire format                  │
   │     envelope_to_trace_batch()    ◄── tested ONCE                  │
   └─────────────────┬───────────────────────────────┬────────────────┘
                     │                               │
        imports      │                               │  imports
                     ▼                               ▼
   ┌──────────────────────────────┐    ┌────────────────────────────┐
   │ SDK: gateway/connectors/n8n  │    │ pipe: aisquare-pipe-       │
   │                              │    │       aisquare-gateway     │
   │  orchestrator + postgres     │    │                            │
   │  asyncio polling             │    │  sink: HTTP + retry        │
   │  stub/progress/final emit    │    │                            │
   │  idempotency keys            │    │  pairs with:               │
   │  auto-disable                │    │   aisquare-pipe-n8n        │
   │                              │    │   aisquare-pipe-zapier     │
   │  "we host the bridge"        │    │   aisquare-pipe-airflow    │
   └──────────────────────────────┘    │                            │
                                       │  "BYO bridge"              │
                                       └────────────────────────────┘
```

### Decision rule (after migration)

- **Customer's n8n is reachable from your gateway?**
  Use the SDK in-process connector. Zero customer infra.
- **Customer's n8n is behind a firewall / on their VPC?**
  Use the pipe container. Customer runs the bridge; only needs outbound HTTPS.
- **New non-n8n source?**
  Build a pipe Source connector. Reuse the same sink. No SDK changes.

### Migration risk and rollout

* **Step 1–2 are non-breaking** for the SDK. Extracting + publishing the
  protocol library doesn't change runtime behavior; existing imports just move.
* **Step 3** is non-breaking for pipe — the sink's HTTP shape stays identical,
  the transform just gets sharper (adds `agent.name`, `openinference.span.kind`,
  nano timestamps).
* **Step 4** changes what pipe's N8nSource yields — but only the sink consumes
  those envelopes today, so it's contained. Bump pipe to v0.2.
* **Step 5** is non-breaking for the SDK. Same code, different import path.

Estimated effort: 1–2 engineer-days. The win: every future source connector
ships with the gateway's full wire format for free.
