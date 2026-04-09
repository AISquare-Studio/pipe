"""Run framework compliance suite against Dropbox connectors."""

from aisquare.pipe.testing.compliance import connector_compliance_suite

from aisquare_pipe_dropbox.connector import DropboxSink, DropboxSource


class TestDropboxSourceCompliance(connector_compliance_suite(DropboxSource)):
    pass


class TestDropboxSinkCompliance(connector_compliance_suite(DropboxSink)):
    pass
