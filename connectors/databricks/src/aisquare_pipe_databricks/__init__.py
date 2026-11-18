"""Databricks for aisquare.pipe: a read-only toolkit (volume and DBFS files,
SELECT-only SQL, Unity Catalog listings) and a files source.

Credentials are the tenant's workspace URL and PAT or service principal (a
per-tenant blob, see :mod:`aisquare_pipe_databricks.credentials`); ambient
Databricks configuration is never read.
"""

from aisquare_pipe_databricks.client import DatabricksAPIError
from aisquare_pipe_databricks.connector import DatabricksFilesSource
from aisquare_pipe_databricks.credentials import (
    DatabricksNotConfigured,
    load_credentials,
)
from aisquare_pipe_databricks.sqlguard import NotReadOnly, check_select_only
from aisquare_pipe_databricks.toolkit import DatabricksRequestError, DatabricksToolkit

__all__ = [
    "DatabricksAPIError",
    "DatabricksFilesSource",
    "DatabricksNotConfigured",
    "DatabricksRequestError",
    "DatabricksToolkit",
    "NotReadOnly",
    "check_select_only",
    "load_credentials",
]
