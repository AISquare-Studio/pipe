"""Framework compliance for the Databricks toolkit and files source."""

from aisquare.pipe.testing.compliance import (
    connector_compliance_suite,
    toolkit_compliance_suite,
)

from aisquare_pipe_databricks import DatabricksFilesSource, DatabricksToolkit


class TestDatabricksFilesSourceCompliance(
    connector_compliance_suite(DatabricksFilesSource)
):
    pass


class TestDatabricksToolkitCompliance(toolkit_compliance_suite(DatabricksToolkit)):
    pass
