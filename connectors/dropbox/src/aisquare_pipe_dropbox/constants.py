"""Constants for the Dropbox connector."""

# Upload: use session-based upload above this threshold (Dropbox limit is 150MB)
CHUNK_THRESHOLD = 140 * 1024 * 1024  # 140 MB

# Upload session chunk size (Dropbox recommends ~8MB)
CHUNK_SIZE = 8 * 1024 * 1024  # 8 MB

# Download: use streaming above this threshold to avoid buffering in memory
STREAM_THRESHOLD = 50 * 1024 * 1024  # 50 MB

# Retry config for rate-limited (429) responses
MAX_RETRIES = 5
INITIAL_BACKOFF = 1.0  # seconds
