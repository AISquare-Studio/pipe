"""Google BigQuery for aisquare.pipe: a read-only toolkit and source.

Credentials are the tenant's service account key and job project (a
per-tenant blob, see :mod:`aisquare_pipe_bigquery.credentials`);
Application Default Credentials are never used. SQL is SELECT-only, checked
before any request (:mod:`aisquare_pipe_bigquery.sqlguard`).
"""

from aisquare_pipe_bigquery.client import BigQueryAPIError
from aisquare_pipe_bigquery.connector import BigQueryQuerySource
from aisquare_pipe_bigquery.credentials import BigQueryNotConfigured, load_credentials
from aisquare_pipe_bigquery.query import CapExceeded, NotATable
from aisquare_pipe_bigquery.sqlguard import NotReadOnly, check_select_only
from aisquare_pipe_bigquery.toolkit import BigQueryToolkit

__all__ = [
    "BigQueryAPIError",
    "BigQueryNotConfigured",
    "BigQueryQuerySource",
    "BigQueryToolkit",
    "CapExceeded",
    "NotATable",
    "NotReadOnly",
    "check_select_only",
    "load_credentials",
]
