"""Tests for the connector registry / plugin discovery."""

import pytest

from aisquare.pipe.core.registry import (
    discover_connectors,
    discover_converters,
    get_connector,
    get_converter,
)
from aisquare.pipe.errors import ConnectorNotFoundError, ConverterError
from aisquare.pipe.testing.mock_connectors import MockConverter, MockSink, MockSource


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
