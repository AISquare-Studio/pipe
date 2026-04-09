"""Constants for the local filesystem connector."""

# Stream files above this threshold instead of buffering in memory.
STREAM_THRESHOLD = 50 * 1024 * 1024  # 50 MB

# Chunk size when writing from a stream to disk.
WRITE_CHUNK_SIZE = 8 * 1024 * 1024  # 8 MB
