"""Framework compliance for GoogleDriveSource, GoogleDriveSink and GoogleDriveToolkit."""

from aisquare.pipe.testing.compliance import (
    connector_compliance_suite,
    toolkit_compliance_suite,
)

from aisquare_pipe_googledrive import (
    GoogleDriveSink,
    GoogleDriveSource,
    GoogleDriveToolkit,
)


class TestGoogleDriveSourceCompliance(connector_compliance_suite(GoogleDriveSource)):
    pass


class TestGoogleDriveSinkCompliance(connector_compliance_suite(GoogleDriveSink)):
    pass


class TestGoogleDriveToolkitCompliance(toolkit_compliance_suite(GoogleDriveToolkit)):
    pass
