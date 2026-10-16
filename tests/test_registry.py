"""Tests for the connector registry / plugin discovery."""

import pytest

from aisquare.pipe.core.registry import (
    discover_connectors,
    discover_converters,
    discover_toolkits,
    get_connector,
    get_converter,
    get_toolkit,
)
from aisquare.pipe.errors import (
    ConnectorNotFoundError,
    ConverterError,
    PipeError,
    ToolkitNotFoundError,
)
from aisquare.pipe.testing.mock_connectors import (
    MockConverter,
    MockSink,
    MockSource,
    MockToolkit,
)


class TestDiscoverConnectors:
    def test_discovers_mock_connectors(self):
        """After pip install -e ., mock connectors should be discoverable."""
        connectors = discover_connectors()
        assert "mock-source" in connectors
        assert "mock-sink" in connectors

    def test_discovered_types(self):
        connectors = discover_connectors()
        assert connectors["mock-source"] is MockSource
        assert connectors["mock-sink"] is MockSink


class TestDiscoverConverters:
    def test_discovers_mock_converter(self):
        converters = discover_converters()
        assert "mock-converter" in converters
        assert converters["mock-converter"] is MockConverter


class TestGetConnector:
    def test_get_existing(self):
        cls = get_connector("mock-source")
        assert cls is MockSource

    def test_get_missing(self):
        with pytest.raises(ConnectorNotFoundError):
            get_connector("nonexistent-connector")


class TestGetConverter:
    def test_get_existing(self):
        cls = get_converter("mock-converter")
        assert cls is MockConverter

    def test_get_missing(self):
        with pytest.raises(ConverterError):
            get_converter("nonexistent-converter")


class TestToolkits:
    def test_discovers_mock_toolkit(self):
        toolkits = discover_toolkits()
        assert toolkits["mock"] is MockToolkit

    def test_toolkits_are_not_connectors(self):
        assert "mock" not in discover_connectors()

    def test_get_existing(self):
        assert get_toolkit("mock") is MockToolkit

    def test_get_missing(self):
        with pytest.raises(ToolkitNotFoundError, match="Available: .*mock"):
            get_toolkit("nonexistent-toolkit")

    def test_not_found_is_a_pipe_error(self):
        assert issubclass(ToolkitNotFoundError, PipeError)

    def test_top_level_reexports(self):
        from aisquare import pipe

        assert pipe.discover_toolkits is discover_toolkits
        assert pipe.get_toolkit is get_toolkit
