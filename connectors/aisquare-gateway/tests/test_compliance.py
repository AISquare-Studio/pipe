"""Framework compliance suite for AISquareGatewaySink."""

from aisquare.pipe.testing.compliance import connector_compliance_suite

from aisquare_pipe_gateway.sink import AISquareGatewaySink


class TestAISquareGatewaySinkCompliance(
    connector_compliance_suite(AISquareGatewaySink)
):
    pass
