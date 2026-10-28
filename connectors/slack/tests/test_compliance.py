"""Framework compliance for SlackToolkit and SlackMessageSink."""

from aisquare.pipe.testing.compliance import (
    connector_compliance_suite,
    toolkit_compliance_suite,
)

from aisquare_pipe_slack.connector import SlackMessageSink
from aisquare_pipe_slack.toolkit import SlackToolkit


class TestSlackToolkitCompliance(toolkit_compliance_suite(SlackToolkit)):
    pass


class TestSlackMessageSinkCompliance(connector_compliance_suite(SlackMessageSink)):
    pass
