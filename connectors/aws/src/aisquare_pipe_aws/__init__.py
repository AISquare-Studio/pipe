"""AWS for aisquare.pipe (``aisquare-pipe[aws]``): read a customer's account through a cross-account role.

One shared credential (:mod:`aisquare_pipe_aws.credentials`: STS AssumeRole
with a server-issued external ID) and, on it, read-only connectors and
toolkits for Amazon Bedrock AgentCore policies and Amazon S3 objects.
"""

from aisquare_pipe_aws.connector import (
    AwsRequestError,
    BedrockAgentCorePoliciesSource,
    S3Source,
)
from aisquare_pipe_aws.credentials import (
    AwsAccessError,
    AwsNotConfiguredError,
    CrossAccountConfig,
    assume_role_session,
)
from aisquare_pipe_aws.toolkit import BedrockAgentCoreToolkit, S3Toolkit

__all__ = [
    "AwsAccessError",
    "AwsNotConfiguredError",
    "AwsRequestError",
    "BedrockAgentCorePoliciesSource",
    "BedrockAgentCoreToolkit",
    "CrossAccountConfig",
    "S3Source",
    "S3Toolkit",
    "assume_role_session",
]
