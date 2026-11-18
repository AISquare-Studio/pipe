"""Snowflake for aisquare.pipe: a read-only toolkit and source.

Credentials are the tenant's account, service user and key (or OAuth
client), role and warehouse: a per-tenant blob, see
:mod:`aisquare_pipe_snowflake.credentials`. Ambient Snowflake configuration
is never read. SQL is SELECT-only, checked before any request
(:mod:`aisquare_pipe_snowflake.sqlguard`). Every call goes to the account's
own SQL API (``https://<account>.snowflakecomputing.com/api/v2``).
"""

from aisquare_pipe_snowflake.client import SnowflakeAPIError
from aisquare_pipe_snowflake.connector import SnowflakeQuerySource
from aisquare_pipe_snowflake.credentials import SnowflakeNotConfigured, load_credentials
from aisquare_pipe_snowflake.sqlguard import NotReadOnly, check_select_only
from aisquare_pipe_snowflake.toolkit import SnowflakeToolkit

__all__ = [
    "NotReadOnly",
    "SnowflakeAPIError",
    "SnowflakeNotConfigured",
    "SnowflakeQuerySource",
    "SnowflakeToolkit",
    "check_select_only",
    "load_credentials",
]
