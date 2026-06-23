"""Content-type constants for aisquare-pipe-github.

Duplicated (never imported) by downstream connectors per the repo's
no-cross-connector-imports rule — keep the literal in sync by convention.
"""

# A *handle* to an on-disk checkout: data = {"path": "<abs dir>", "head_sha": "<sha>"}.
# Valid only while the source generator is suspended (same process, same run).
CHECKOUT_CONTENT_TYPE = "application/x-aisquare-checkout+json"
