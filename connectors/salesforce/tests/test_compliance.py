"""Run framework compliance suite against Salesforce connectors."""

from aisquare.pipe.testing.compliance import connector_compliance_suite

from aisquare_pipe_salesforce.connector import SalesforceSink, SalesforceSource


class TestSalesforceSourceCompliance(connector_compliance_suite(SalesforceSource)):
    pass


class TestSalesforceSinkCompliance(connector_compliance_suite(SalesforceSink)):
    pass
