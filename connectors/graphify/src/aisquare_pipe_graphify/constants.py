"""Constants for aisquare-pipe-graphify.

Content-type literals are duplicated (never imported) across connector
packages per the repo's no-cross-connector-imports rule.
"""

# Input: a *handle* to an on-disk checkout (emitted by aisquare-pipe-github):
# data = {"path": "<abs dir>", "head_sha": "<sha>"}.
CHECKOUT_CONTENT_TYPE = "application/x-aisquare-checkout+json"

# Output: the graph bundle:
# data = {"report_md": str, "graph_json": str ("" when absent),
#         "stats": {"nodes": int, "edges": int, "communities": int}}
GRAPH_CONTENT_TYPE = "application/x-aisquare-graph+json"

# Engine defaults (per-tier knobs — the AST tier is minutes-fast, the enriched
# extract tier owns the 25-minute budget).
DEFAULT_EXTRACT_FLAGS = "--no-viz"
DEFAULT_EXTRACT_TIMEOUT = 1500
DEFAULT_UPDATE_TIMEOUT = 600

# Which env var carries the API key for each LLM backend. The subprocess env is
# scrubbed-minimal: ONLY the intended var is ever set (stray ambient keys must
# never hijack graphify's backend auto-detection). ollama is keyless.
KEY_ENV = {
    "claude": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
    "gemini": "GEMINI_API_KEY",
    "deepseek": "DEEPSEEK_API_KEY",
}
