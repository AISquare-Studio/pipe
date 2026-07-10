# aisquare-pipe-graphify

Graphify knowledge-graph transform for [aisquare.pipe](../../README.md): turns a
code tree into `GRAPH_REPORT.md` (plain-language digest: god nodes, Leiden
communities, cross-module edges) + `graph.json` (machine-readable graph), via
the [graphify](https://github.com/safishamsi/graphify) CLI (PyPI `graphifyy`,
pinned `==0.8.36`).

One engine, **two faces**:

| Face | Class | Use |
|---|---|---|
| Source | `GraphifySource` (`graphify-source`) | CLI-runnable on a **local directory** — `pipe run --source graphify-source --sink local-sink` |
| Converter | `GraphifyConverter` (Python API only) | composes after `aisquare-pipe-github`'s checkout envelope in `Pipeline(converters=[...])` |

Two tiers: **AST** (free, keyless — `graphify update`; full structural graph,
"Community N" placeholder labels) and **enriched** (`graphify extract
--backend <b>` with your API key; adds LLM-inferred cross-module edges +
community names). Enriched failures fall back to a working AST graph with the
error recorded in `metadata["enrichment_error"]`. The subprocess env is
scrubbed-minimal so stray ambient keys can never hijack backend detection.

## Install

```bash
cd connectors/graphify
pip install -e ".[dev]"
pip install -e ".[engine]"   # installs graphifyy==0.8.36 (or bring your own pipx install)
```

## Quickstart (no API key — free AST tier)

```bash
cat > config.json <<'EOF'
{ "graphify":   { "path": "/home/me/code/myrepo" },
  "local-sink": { "base_path": "./graphify-artifacts" } }
EOF
pipe run --source graphify-source --sink local-sink --config config.json
# -> ./graphify-artifacts/GRAPH_REPORT.md (+ graph.json)
```

Add `"backend": "claude", "api_key": "sk-ant-..."` to the `graphify` block for
the enriched tier. Config key = class name `graphify`; CLI flag = entry point
`graphify-source`.

## Python API (private repo via the github source)

```python
from aisquare.pipe import Pipeline
from aisquare_pipe_github import GitHubRepoSource
from aisquare_pipe_graphify import GraphifyConverter

result = Pipeline(
    source=GitHubRepoSource(),
    converters=[GraphifyConverter(backend="claude", api_key="sk-ant-...")],
    sink=my_graph_sink,   # MUST declare input_types=[GRAPH_CONTENT_TYPE] exactly — never "*/*"
).run({"github": {"full_name": "acme/api", "token": "ghp_..."}, "my-sink": {...}})
```

## Running tests

```bash
pytest -v
```
