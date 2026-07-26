from aisquare.pipe.testing.compliance import connector_compliance_suite

from aisquare_pipe_slack.connector import SlackMessageSink


class TestSlackMessageSinkCompliance(connector_compliance_suite(SlackMessageSink)):
    pass
