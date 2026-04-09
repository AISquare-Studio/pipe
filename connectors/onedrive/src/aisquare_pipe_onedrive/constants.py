"""Constants for the OneDrive connector."""

GRAPH_BASE_URL = "https://graph.microsoft.com/v1.0"

# Upload: simple PUT limit is 4 MB for OneDrive
SIMPLE_UPLOAD_LIMIT = 4 * 1024 * 1024  # 4 MB

# Resumable upload chunk size — must be a multiple of 320 KiB
# Using 10 MiB (= 32 × 320 KiB)
CHUNK_SIZE = 10 * 1024 * 1024  # 10 MB

# Download: stream above this threshold
STREAM_THRESHOLD = 50 * 1024 * 1024  # 50 MB

# Retry config for 429/503/504 responses
MAX_RETRIES = 5
INITIAL_BACKOFF = 1.0  # seconds
