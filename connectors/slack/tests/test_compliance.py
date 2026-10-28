"""Framework compliance for SlackToolkit."""

from aisquare.pipe.testing.compliance import toolkit_compliance_suite

from aisquare_pipe_slack.toolkit import SlackToolkit


class TestSlackToolkitCompliance(toolkit_compliance_suite(SlackToolkit)):
    pass
