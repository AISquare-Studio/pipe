"""Framework compliance for GoogleCalendarToolkit."""

from aisquare.pipe.testing.compliance import toolkit_compliance_suite

from aisquare_pipe_googlecalendar import GoogleCalendarToolkit


class TestGoogleCalendarToolkitCompliance(toolkit_compliance_suite(GoogleCalendarToolkit)):
    pass
