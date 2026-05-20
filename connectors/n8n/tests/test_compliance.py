"""Framework compliance suite for N8nSource."""

from aisquare.pipe.testing.compliance import connector_compliance_suite

from aisquare_pipe_n8n.source import N8nSource


class TestN8nSourceCompliance(connector_compliance_suite(N8nSource)):
    pass
