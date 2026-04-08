"""Plugin discovery via entry_points for connectors and converters."""

from __future__ import annotations

import logging
from importlib.metadata import entry_points

from aisquare.pipe.errors import ConnectorNotFoundError, ConverterError

logger = logging.getLogger("aisquare.pipe")


def discover_connectors() -> dict[str, type]:
    """Auto-discover all installed connectors via entry_points."""
    discovered: dict[str, type] = {}
    for ep in entry_points(group="aisquare_pipe.connectors"):
        try:
            cls = ep.load()
            discovered[ep.name] = cls
        except Exception as e:
            logger.warning(f"Failed to load connector {ep.name}: {e}")
    return discovered


def discover_converters() -> dict[str, type]:
    """Auto-discover all installed converters via entry_points."""
    discovered: dict[str, type] = {}
    for ep in entry_points(group="aisquare_pipe.converters"):
        try:
            cls = ep.load()
            discovered[ep.name] = cls
        except Exception as e:
            logger.warning(f"Failed to load converter {ep.name}: {e}")
    return discovered


def get_connector(name: str) -> type:
    """Get a connector class by name.

    Raises ConnectorNotFoundError if not found.
    """
    connectors = discover_connectors()
    if name not in connectors:
        available = ", ".join(sorted(connectors.keys())) or "(none)"
        raise ConnectorNotFoundError(
            f"Connector '{name}' not found. Available: {available}"
        )
    return connectors[name]


def get_converter(name: str) -> type:
    """Get a converter class by name.

    Raises ConverterError if not found.
    """
    converters = discover_converters()
    if name not in converters:
        available = ", ".join(sorted(converters.keys())) or "(none)"
        raise ConverterError(
            f"Converter '{name}' not found. Available: {available}"
        )
    return converters[name]
