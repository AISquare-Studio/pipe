"""The framework's compliance suites, for both sources and both toolkits."""

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
