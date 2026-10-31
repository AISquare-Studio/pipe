"""Plugin discovery via entry_points for connectors, converters and toolkits,
plus the cross-toolkit views a connector router needs: :func:`catalogue`
(one card per installed toolkit) and :func:`search_tools_across`."""

from __future__ import annotations

import functools
import logging
from collections.abc import Iterable, Mapping
from importlib.metadata import entry_points
from typing import Any

from aisquare.pipe.core.tools import QualifiedTool, Toolkit, ToolSpec, rank_across
from aisquare.pipe.errors import (
    ConnectorNotFoundError,
    ConverterError,
    ToolkitConflictError,
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


def slug_prefix_conflicts(toolkits: Mapping[str, type]) -> list[tuple[str, str]]:
    """Pairs of toolkit names whose slug prefixes collide: equal, or one a
    prefix of the other (``ONE_`` and ``ONE_DRIVE_``). Either way a bare
    slug could belong to both. Names are sorted within and across pairs."""
    prefixes = sorted(
        (name, prefix)
        for name, cls in toolkits.items()
        if isinstance(prefix := getattr(cls, "slug_prefix", None), str) and prefix
    )
    return [
        (a, b)
        for i, (a, prefix_a) in enumerate(prefixes)
        for b, prefix_b in prefixes[i + 1 :]
        if prefix_a.startswith(prefix_b) or prefix_b.startswith(prefix_a)
    ]


@functools.cache
def _installed_toolkits() -> tuple[dict[str, type], str]:
    """The installed toolkits minus any whose slug prefixes collide, and a
    description of the collisions ("" when none). Cached, clash or not, so a
    clash is logged once and the plugins are imported once."""
    toolkits = {
        name: cls
        for name, cls in discover_toolkits().items()
        if isinstance(cls, type) and issubclass(cls, Toolkit)
    }
    conflicts = slug_prefix_conflicts(toolkits)
    detail = "; ".join(
        f"{a} ({toolkits[a].slug_prefix}) and {b} ({toolkits[b].slug_prefix})"
        for a, b in conflicts
    )
    if conflicts:
        # A bare slug could belong to either toolkit of a pair, so neither is
        # trusted with it; every other toolkit stays available.
        clashing = {name for pair in conflicts for name in pair}
        logger.warning(
            "Left out toolkits whose slug prefixes collide (`pipe validate` fails on this): %s",
            detail,
        )
        toolkits = {name: cls for name, cls in toolkits.items() if name not in clashing}
    return dict(sorted(toolkits.items())), detail


def installed_toolkits(*, refresh: bool = False, strict: bool = False) -> dict[str, type[Toolkit]]:
    """Every installed toolkit class by name, sorted. Entry points are read
    once per process; ``refresh=True`` reads them again (after installing a
    plugin).

    Toolkits whose slug prefixes collide (see :func:`slug_prefix_conflicts`)
    are left out, with one warning logged: one clashing pair must not hide
    every other toolkit from :func:`catalogue` and
    :func:`search_tools_across`. ``strict=True`` raises
    :class:`ToolkitConflictError` instead. ``pipe validate`` always fails
    on a collision.

    The first discovery imports every plugin module. What those imports do
    is the plugins' own business: urllib3, which the Slack toolkit imports,
    opens an IPv6 socket at import to see whether the host supports IPv6. A
    host that must stay offline afterwards calls this once at startup."""
    if refresh:
        _installed_toolkits.cache_clear()
    toolkits, conflicts = _installed_toolkits()
    if strict and conflicts:
        raise ToolkitConflictError(f"installed toolkits share a slug prefix: {conflicts}")
    return dict(toolkits)


def catalogue(*, refresh: bool = False) -> dict[str, dict[str, Any]]:
    """One :meth:`Toolkit.card` per installed toolkit, by name — what a
    router shows a model to pick connectors. Offline and cheap: the
    discovery is cached and cards are read from class attributes. The first
    call imports the plugins (see :func:`installed_toolkits`). A toolkit
    whose card fails is left out (and logged), and so are toolkits whose
    slug prefixes collide (see :func:`installed_toolkits`). Each call
    returns fresh dicts."""
    cards: dict[str, dict[str, Any]] = {}
    for name, cls in installed_toolkits(refresh=refresh).items():
        try:
            cards[name] = cls.card()
        except Exception as e:  # noqa: BLE001 — one broken plugin must not hide the rest
            logger.warning(f"Failed to read the card of toolkit {name}: {type(e).__name__}")
    return cards


def search_tools_across(
    query: str | None,
    connectors: Mapping[str, Mapping[str, Any] | None] | Iterable[str] | None = None,
    *,
    limit: int | None = 6,
) -> list[QualifiedTool]:
    """The tools most relevant to ``query`` across several toolkits, each
    named with its connector (see :func:`~aisquare.pipe.core.tools.rank_across`).

    ``connectors`` picks the toolkits: a mapping of name -> the connection's
    ``config`` (so each lists only its :meth:`Toolkit.available_tools`, e.g.
    filtered by ``granted_scopes``), an iterable of names (no config), or None
    for every installed toolkit. An unknown name, or one left out because
    its slug prefix collides with another toolkit's, raises
    :class:`ToolkitNotFoundError`. Offline, like ``available_tools``, once
    the plugins are imported (see :func:`installed_toolkits`)."""
    installed = installed_toolkits()
    if connectors is None:
        chosen: dict[str, Mapping[str, Any] | None] = dict.fromkeys(installed)
    elif isinstance(connectors, Mapping):
        chosen = dict(connectors)
    else:
        chosen = dict.fromkeys([connectors] if isinstance(connectors, str) else connectors)
    unknown = sorted(name for name in chosen if name not in installed)
    if unknown:
        available = ", ".join(installed) or "(none)"
        _, conflicts = _installed_toolkits()
        left_out = f". Left out, their slug prefixes collide: {conflicts}" if conflicts else ""
        raise ToolkitNotFoundError(
            f"Toolkit(s) {', '.join(map(repr, unknown))} not found. Available: {available}{left_out}"
        )
    catalogs: dict[str, list[ToolSpec]] = {}
    keywords: dict[str, list[str]] = {}
    for name, config in chosen.items():
        cls = installed[name]
        try:
            catalogs[name] = list(cls().available_tools(config))
            card = cls.card()
            keywords[name] = [
                name, str(card["display_name"]), str(card["category"]), *map(str, card["keywords"])
            ]
        except Exception as e:  # noqa: BLE001 — one broken plugin must not hide the rest
            logger.warning(f"Failed to list the tools of toolkit {name}: {type(e).__name__}")
            catalogs[name] = []
    return rank_across(catalogs, query, limit, keywords=keywords)
