"""Constants for the Google Drive connector."""

DRIVE_API_BASE = "https://www.googleapis.com/drive/v3"
DRIVE_UPLOAD_BASE = "https://www.googleapis.com/upload/drive/v3"

# OAuth scopes. The sink cannot use drive.file: that scope only grants access to
# files the app itself created, so writing into a pre-existing folder fails.
SCOPE_READONLY = "https://www.googleapis.com/auth/drive.readonly"
SCOPE_FULL = "https://www.googleapis.com/auth/drive"

# Google-native files have no downloadable bytes; they must be exported.
NATIVE_MIME_PREFIX = "application/vnd.google-apps."
FOLDER_MIME = "application/vnd.google-apps.folder"

# Short config key -> native MIME type. export_formats is keyed by short name.
NATIVE_SHORT_NAMES = {
    "document": "application/vnd.google-apps.document",
    "spreadsheet": "application/vnd.google-apps.spreadsheet",
    "presentation": "application/vnd.google-apps.presentation",
    "drawing": "application/vnd.google-apps.drawing",
}

# Native MIME -> default export target (Office formats: highest fidelity,
# round-trippable to onedrive/dropbox).
DEFAULT_EXPORT_FORMATS = {
    "application/vnd.google-apps.document": (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    ),
    "application/vnd.google-apps.spreadsheet": (
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    ),
    "application/vnd.google-apps.presentation": (
        "application/vnd.openxmlformats-officedocument.presentationml.presentation"
    ),
    "application/vnd.google-apps.drawing": "image/png",
}

# Export target -> filename extension, so a downstream file sink writes
# something openable.
EXPORT_EXTENSIONS = {
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": ".pptx",
    "application/pdf": ".pdf",
    "application/rtf": ".rtf",
    "application/vnd.oasis.opendocument.text": ".odt",
    "text/markdown": ".md",
    "text/csv": ".csv",
    "text/tab-separated-values": ".tsv",
    "text/plain": ".txt",
    "text/html": ".html",
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/svg+xml": ".svg",
}

# Drive caps files.export output at 10 MB regardless of target format.
EXPORT_SIZE_LIMIT = 10 * 1024 * 1024  # 10 MB

# Upload: multipart below this limit, resumable session above.
SIMPLE_UPLOAD_LIMIT = 5 * 1024 * 1024  # 5 MB

# Resumable upload chunk size — must be a multiple of 256 KiB.
# Using 8 MiB (= 32 x 256 KiB).
CHUNK_SIZE = 8 * 1024 * 1024  # 8 MB

# Download: stream above this threshold.
STREAM_THRESHOLD = 50 * 1024 * 1024  # 50 MB

# Fields requested from files.list / files.get.
FILE_FIELDS = "id,name,mimeType,size,modifiedTime,md5Checksum,parents,webViewLink"

# Retry config for 429/500/502/503/504 responses.
MAX_RETRIES = 5
INITIAL_BACKOFF = 1.0  # seconds

# Per-request timeout in seconds.
REQUEST_TIMEOUT = 300
