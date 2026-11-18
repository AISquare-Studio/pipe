"""Framework compliance for the GCS toolkit and source."""

from aisquare.pipe.testing.compliance import (
    connector_compliance_suite,
    toolkit_compliance_suite,
)

from aisquare_pipe_gcs import GcsSource, GcsToolkit


class TestGcsSourceCompliance(connector_compliance_suite(GcsSource)):
    pass


class TestGcsToolkitCompliance(toolkit_compliance_suite(GcsToolkit)):
    pass
