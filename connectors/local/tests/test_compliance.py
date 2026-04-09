"""Run framework compliance suite against local filesystem connectors."""

from aisquare.pipe.testing.compliance import connector_compliance_suite

from aisquare_pipe_local.connector import LocalSink, LocalSource


class TestLocalSourceCompliance(connector_compliance_suite(LocalSource)):
    pass


class TestLocalSinkCompliance(connector_compliance_suite(LocalSink)):
    pass
