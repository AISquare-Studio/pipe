"""Framework compliance for the BigQuery toolkit and source."""

from aisquare.pipe.testing.compliance import (
    connector_compliance_suite,
    toolkit_compliance_suite,
)

from aisquare_pipe_bigquery import BigQueryQuerySource, BigQueryToolkit


class TestBigQuerySourceCompliance(connector_compliance_suite(BigQueryQuerySource)):
    pass


class TestBigQueryToolkitCompliance(toolkit_compliance_suite(BigQueryToolkit)):
    pass
