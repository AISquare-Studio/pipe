"""Constants for the DocuSign connector."""

DOCUSIGN_SCOPES = ["signature", "impersonation"]

JWT_EXPIRES_IN = 3600

DEFAULT_PAGE_SIZE = 100

DEFAULT_SUBJECT = "Please sign"

OAUTH_TOKEN_TIMEOUT = 30

MAX_RETRIES = 5

INITIAL_BACKOFF = 1.0
