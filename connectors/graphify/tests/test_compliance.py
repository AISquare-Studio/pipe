"""Framework compliance for GraphifySource (converters aren't suite-covered)."""

from aisquare.pipe.testing.compliance import connector_compliance_suite

from aisquare_pipe_graphify.connector import GraphifySource


class TestGraphifySourceCompliance(connector_compliance_suite(GraphifySource)):
    pass
