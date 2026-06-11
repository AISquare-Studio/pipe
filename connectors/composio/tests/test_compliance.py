"""Framework compliance suites for all Composio connectors.

Runs the auto-generated spec checks against the three entry-point connectors
and one factory-produced class (proving dynamic toolkit-pinned subclasses
conform too). These must pass with zero network access.
"""

from __future__ import annotations

from aisquare.pipe.testing.compliance import connector_compliance_suite

from aisquare_pipe_composio.connector import ComposioSink, ComposioSource
from aisquare_pipe_composio.factory import composio_source
from aisquare_pipe_composio.triggers import ComposioTriggersSource


class TestComposioSourceCompliance(connector_compliance_suite(ComposioSource)):
    pass


class TestComposioSinkCompliance(connector_compliance_suite(ComposioSink)):
    pass


class TestComposioTriggersSourceCompliance(
    connector_compliance_suite(ComposioTriggersSource)
):
    pass


class TestFactorySourceCompliance(
    connector_compliance_suite(composio_source("gmail"))
):
    pass
