"""Run compliance suite against mock connectors."""

from aisquare.pipe.testing.compliance import connector_compliance_suite
from aisquare.pipe.testing.mock_connectors import MockSink, MockSource


class TestMockSourceCompliance(connector_compliance_suite(MockSource)):
    pass


class TestMockSinkCompliance(connector_compliance_suite(MockSink)):
    pass
