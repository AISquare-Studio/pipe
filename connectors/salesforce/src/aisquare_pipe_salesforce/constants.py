"""Constants for the Salesforce connector."""

DEFAULT_FIELDS = ["Id", "Name", "CreatedDate", "LastModifiedDate"]

DEFAULT_BATCH_SIZE = 200

MAX_RETRIES = 5
INITIAL_BACKOFF = 1.0

OAUTH_TOKEN_TIMEOUT = 30
