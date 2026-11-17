"""aisquare-pipe-gmail — Gmail toolkit."""

from aisquare_pipe_gmail.rest import GmailAPIError, GmailRestClient
from aisquare_pipe_gmail.toolkit import GMAIL_OAUTH, GmailToolkit
from aisquare_pipe_gmail.tools import REQUIRED_SCOPES

__all__ = [
    "GMAIL_OAUTH",
    "REQUIRED_SCOPES",
    "GmailAPIError",
    "GmailRestClient",
    "GmailToolkit",
]
