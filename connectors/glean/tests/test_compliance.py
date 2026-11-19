"""Framework compliance for the Glean toolkit and source."""

from aisquare.pipe.testing.compliance import (
    connector_compliance_suite,
    toolkit_compliance_suite,
)

from aisquare_pipe_glean import GleanSource, GleanToolkit


class TestGleanSourceCompliance(connector_compliance_suite(GleanSource)):
    pass


class TestGleanToolkitCompliance(toolkit_compliance_suite(GleanToolkit)):
    pass
