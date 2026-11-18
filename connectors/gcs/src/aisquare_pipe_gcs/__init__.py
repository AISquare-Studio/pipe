"""Google Cloud Storage for aisquare.pipe: a read-only toolkit and source.

Credentials are the tenant's service account key and bucket (a per-tenant
blob, see :mod:`aisquare_pipe_gcs.credentials`); Application Default
Credentials are never used.
"""

from aisquare_pipe_gcs.client import GcsAPIError
from aisquare_pipe_gcs.connector import GcsSource
from aisquare_pipe_gcs.credentials import GcsNotConfigured, load_credentials
from aisquare_pipe_gcs.toolkit import GcsToolkit

__all__ = [
    "GcsAPIError",
    "GcsNotConfigured",
    "GcsSource",
    "GcsToolkit",
    "load_credentials",
]
