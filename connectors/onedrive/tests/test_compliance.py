"""Run framework compliance suite against OneDrive connectors."""

from aisquare.pipe.testing.compliance import connector_compliance_suite

from aisquare_pipe_onedrive.connector import OneDriveSink, OneDriveSource


class TestOneDriveSourceCompliance(connector_compliance_suite(OneDriveSource)):
    pass


class TestOneDriveSinkCompliance(connector_compliance_suite(OneDriveSink)):
    pass
