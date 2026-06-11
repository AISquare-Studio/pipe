"""Defaults and tuning constants for the Composio connector."""

from __future__ import annotations

DEFAULT_USER_ID = "default"
DEFAULT_TIMEOUT_SECONDS = 60

# Retry tuning (mirrors the salesforce connector).
MAX_RETRIES = 5
INITIAL_BACKOFF = 1.0

JSON_CONTENT_TYPE = "application/json"
DEFAULT_FILE_CONTENT_TYPE = "application/octet-stream"

# Sub-directories of the file workdir used when file handling is enabled.
FILES_SUBDIR = "composio-files"
DOWNLOADS_SUBDIR = "downloads"
UPLOADS_SUBDIR = "uploads"

# Resource listing caps — Composio has ~500 toolkits and ~10k tools; these
# bound list_resources() calls, not tool execution.
DEFAULT_RESOURCE_LIMIT = 500
DEFAULT_TOOL_LIMIT = 200

# Conservative advertised rate limit (Composio's real limits are plan-based).
RATE_LIMIT_RPS = 5

# Trigger polling.
DEFAULT_POLL_INTERVAL = 10
CURSOR_FILENAME = "composio-cursor.json"
# Pre-0.1.1 default, kept only as a one-time migration source.
LEGACY_CURSOR_PATH = "/tmp/composio-pipe-cursor.json"
DEFAULT_TRIGGER_PAGE_LIMIT = 100
MAX_TRIGGER_PAGES_PER_POLL = 10
SEEN_IDS_MAX = 500
