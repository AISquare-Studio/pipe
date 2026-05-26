"""Run framework compliance suite against DocuSign connectors."""

from aisquare.pipe.testing.compliance import connector_compliance_suite

from aisquare_pipe_docusign.connector import DocusignSink, DocusignSource


class TestDocusignSourceCompliance(connector_compliance_suite(DocusignSource)):
    pass


class TestDocusignSinkCompliance(connector_compliance_suite(DocusignSink)):
    pass
