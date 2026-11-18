"""Amazon Redshift, read-only, on the AWS connectors' shared cross-account credential.

The toolkit ``redshift`` and the source ``redshift-query-source`` read a
customer's Redshift (provisioned or Serverless) through the Redshift Data
API. They live in ``aisquare_pipe_aws`` because they assume the customer's
role exactly as the S3 and AgentCore connectors do, through
:mod:`aisquare_pipe_aws.credentials` (their base classes are
``_AwsToolkit`` and ``_AwsSource``), and one connector never imports
another. SQL is SELECT-only (:mod:`aisquare_pipe_aws.redshift.sqlguard`).

Install: ``aisquare-pipe[redshift]`` (an alias of ``[aws]``, which brings
sqlglot). Nothing here is imported by ``aisquare_pipe_aws`` itself.
"""

from aisquare_pipe_aws.redshift.config import (
    RedshiftConfig,
    RedshiftNotConfiguredError,
    RedshiftTarget,
)
from aisquare_pipe_aws.redshift.connector import RedshiftQuerySource
from aisquare_pipe_aws.redshift.sqlguard import NotReadOnly, check_select_only
from aisquare_pipe_aws.redshift.toolkit import RedshiftToolkit

__all__ = [
    "NotReadOnly",
    "RedshiftConfig",
    "RedshiftNotConfiguredError",
    "RedshiftQuerySource",
    "RedshiftTarget",
    "RedshiftToolkit",
    "check_select_only",
]
