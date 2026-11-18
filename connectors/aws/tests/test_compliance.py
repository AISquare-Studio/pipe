"""The framework's compliance suites, for every source and toolkit (Redshift's too)."""

from aisquare.pipe.testing.compliance import (
    connector_compliance_suite,
    toolkit_compliance_suite,
)

from aisquare_pipe_aws import (
    BedrockAgentCorePoliciesSource,
    BedrockAgentCoreToolkit,
    S3Source,
    S3Toolkit,
)
from aisquare_pipe_aws.redshift import RedshiftQuerySource, RedshiftToolkit


class TestBedrockAgentCorePoliciesSource(
    connector_compliance_suite(BedrockAgentCorePoliciesSource)
):
    pass


class TestS3Source(connector_compliance_suite(S3Source)):
    pass


class TestBedrockAgentCoreToolkit(toolkit_compliance_suite(BedrockAgentCoreToolkit)):
    pass


class TestS3Toolkit(toolkit_compliance_suite(S3Toolkit)):
    pass


class TestRedshiftQuerySource(connector_compliance_suite(RedshiftQuerySource)):
    pass


class TestRedshiftToolkit(toolkit_compliance_suite(RedshiftToolkit)):
    pass
