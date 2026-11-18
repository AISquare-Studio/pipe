"""Framework compliance for the Snowflake toolkit and source."""

from aisquare.pipe.testing.compliance import (
    connector_compliance_suite,
    toolkit_compliance_suite,
)

from aisquare_pipe_snowflake import SnowflakeQuerySource, SnowflakeToolkit


class TestSnowflakeSourceCompliance(connector_compliance_suite(SnowflakeQuerySource)):
    pass


class TestSnowflakeToolkitCompliance(toolkit_compliance_suite(SnowflakeToolkit)):
    pass
