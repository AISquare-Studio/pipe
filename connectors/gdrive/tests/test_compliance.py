"""Framework compliance for the Google Drive connectors."""

from aisquare_pipe_gdrive.connector import GDriveSink, GDriveSource

from aisquare.pipe.testing.compliance import connector_compliance_suite


class TestGDriveSourceCompliance(connector_compliance_suite(GDriveSource)):
    pass


class TestGDriveSinkCompliance(connector_compliance_suite(GDriveSink)):
    pass
