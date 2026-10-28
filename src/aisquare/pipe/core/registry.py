"""Plugin discovery via entry_points for connectors, converters and toolkits."""

from __future__ import annotations

import logging
from importlib.metadata import entry_points

from aisquare.pipe.errors import (
    ConnectorNotFoundError,
    ConverterError,
    ToolkitNotFoundError,
)

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


def discover_toolkits() -> dict[str, type]:
    """Auto-discover all installed toolkits via the ``aisquare_pipe.toolkits``
    entry-point group (entry-point name == toolkit name)."""
    discovered: dict[str, type] = {}
    for ep in entry_points(group="aisquare_pipe.toolkits"):
        try:
            cls = ep.load()
            discovered[ep.name] = cls
        except Exception as e:  # noqa: BLE001 — one broken plugin must not hide the rest
            logger.warning(f"Failed to load toolkit {ep.name}: {e}")
    return discovered


def get_toolkit(name: str) -> type:
    """Get a toolkit class by name.

    Raises ToolkitNotFoundError if not found.
    """
    toolkits = discover_toolkits()
    if name not in toolkits:
        available = ", ".join(sorted(toolkits.keys())) or "(none)"
        raise ToolkitNotFoundError(
            f"Toolkit '{name}' not found. Available: {available}"
        )
    return toolkits[name]
