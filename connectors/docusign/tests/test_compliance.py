"""Run framework compliance suite against DocuSign connectors."""

from aisquare.pipe.testing.compliance import connector_compliance_suite

from aisquare_pipe_docusign.connector import DocuSignSink, DocuSignSource


class TestDocuSignSourceCompliance(connector_compliance_suite(DocuSignSource)):
    pass


class TestDocuSignSinkCompliance(connector_compliance_suite(DocuSignSink)):
    pass
