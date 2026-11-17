"""Framework compliance for GmailToolkit."""

from aisquare.pipe.testing.compliance import toolkit_compliance_suite

from aisquare_pipe_gmail import GmailToolkit


class TestGmailToolkitCompliance(toolkit_compliance_suite(GmailToolkit)):
    pass
