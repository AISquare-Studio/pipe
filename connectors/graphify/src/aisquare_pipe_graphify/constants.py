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
DEFAULT_CLUSTER_TIMEOUT = 600

# Build-time LLM model for the enriched extract pass. Extraction is
# pattern-matching over docs, not reasoning — a small model holds quality at a
# fraction of the cost; frontier capability belongs at query time, not build
# time. Empty string defers to graphify's own backend default.
DEFAULT_EXTRACT_MODEL = "claude-haiku-4-5"

# Doc-volume preflight ceiling. graphify's enriched pass LLM-processes ONLY
# doc-class content (code is always local AST); a doc corpus above this many
# bytes degrades that build to the free AST tier instead of running an
# unbounded LLM pass (vendored HTML trees and PDF dumps are the blow-up case).
# 0 disables the cap. ~20MB of docs ≈ low-single-digit dollars on Haiku.
DEFAULT_DOC_VOLUME_CAP_BYTES = 20_000_000

# The content classes graphify sends to the LLM (everything else is AST-only).
# Mirrors graphifyy 0.8.36's ingest set; used by the doc-volume preflight.
DOC_EXTENSIONS = (
    ".md",
    ".mdx",
    ".txt",
    ".rst",
    ".html",
    ".htm",
    ".yaml",
    ".yml",
    ".pdf",
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".svg",
    ".webp",
)

# Which env var carries the API key for each LLM backend. The subprocess env is
# scrubbed-minimal: ONLY the intended var is ever set (stray ambient keys must
# never hijack graphify's backend auto-detection). ollama is keyless.
KEY_ENV = {
    "claude": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
    "gemini": "GEMINI_API_KEY",
    "deepseek": "DEEPSEEK_API_KEY",
}
