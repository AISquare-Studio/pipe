"""Plugin discovery via entry_points for connectors, converters and toolkits,
plus the cross-toolkit views a connector router needs: :func:`catalogue`
(one card per installed toolkit) and :func:`search_tools_across`."""

from __future__ import annotations

import functools
import logging
from collections.abc import Iterable, Mapping
from importlib.metadata import entry_points
from typing import Any

from aisquare.pipe.core.search import ToolIndex, scoped_index, search_indexes
from aisquare.pipe.core.tools import (
    TOOLKIT_NAME_RULE,
    QualifiedTool,
    Toolkit,
    ToolSpec,
    take_turns,
    valid_toolkit_name,
)
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


# Why _installed_toolkits() left each installed toolkit out, name -> reason;
# rebuilt with every (cached) discovery and read through left_out_toolkits().
# A record beside the cache rather than a third return value, because the
# (toolkits, clash detail) pair is unpacked in several places.
_LEFT_OUT: dict[str, str] = {}
# Clash reasons start with this; the not-found message already names clashes.
_CLASH_REASON = "slug prefix "


@functools.cache
def _installed_toolkits() -> tuple[dict[str, type], str]:
    """The installed toolkits minus any whose name hosts cannot route, whose
    class ``name`` differs from its entry-point name, or whose slug prefixes
    collide, and a description of the collisions ("" when none). Each reason
    is logged once (the result is cached, and the plugins imported once) and
    recorded in ``_LEFT_OUT``. The name checks come first, so a bad plugin
    can't take a good toolkit down with it in a clash."""
    left_out: dict[str, str] = {}
    toolkits = {
        name: cls
        for name, cls in discover_toolkits().items()
        if isinstance(cls, type) and issubclass(cls, Toolkit)
    }
    unroutable = sorted(name for name in toolkits if not valid_toolkit_name(name))
    if unroutable:
        # Installed but never servable (pipe-as-service routes only these
        # names): leave it out everywhere rather than half-appear.
        logger.warning(
            "Left out toolkits whose names break %s (`pipe validate` fails on this): %s",
            TOOLKIT_NAME_RULE,
            ", ".join(map(repr, unroutable)),
        )
        left_out.update(dict.fromkeys(unroutable, f"name breaks {TOOLKIT_NAME_RULE}"))
        toolkits = {name: cls for name, cls in toolkits.items() if name not in unroutable}
    renamed = {
        name: getattr(cls, "name", None)
        for name, cls in sorted(toolkits.items())
        if getattr(cls, "name", None) != name
    }
    if renamed:
        # Hosts key a toolkit by its entry-point name and its card by `name`:
        # served, it would answer to one name and describe itself by another.
        logger.warning(
            "Left out toolkits whose class name differs from their entry point "
            "(`pipe validate` fails on this): %s",
            ", ".join(f"{ep!r} (class name {cls_name!r})" for ep, cls_name in renamed.items()),
        )
        left_out.update(
            {
                ep: f"name differs from its entry point (class name {cls_name!r})"
                for ep, cls_name in renamed.items()
            }
        )
        toolkits = {name: cls for name, cls in toolkits.items() if name not in renamed}
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
        for a, b in conflicts:
            for one, other in ((a, b), (b, a)):
                reason = (
                    f"{_CLASH_REASON}{toolkits[one].slug_prefix} collides with "
                    f"{other} ({toolkits[other].slug_prefix})"
                )
                left_out[one] = f"{left_out[one]}; {reason}" if one in left_out else reason
        toolkits = {name: cls for name, cls in toolkits.items() if name not in clashing}
    _LEFT_OUT.clear()
    _LEFT_OUT.update(sorted(left_out.items()))
    return dict(sorted(toolkits.items())), detail


def left_out_toolkits(*, refresh: bool = False) -> dict[str, str]:
    """The installed toolkits :func:`installed_toolkits` leaves out, by name,
    sorted, each with why: ``name breaks <TOOLKIT_NAME_RULE>``, ``name
    differs from its entry point (class name '...')`` or ``slug prefix
    <PREFIX_> collides with <other> (<PREFIX_>)``. Read from the same cached
    discovery (``refresh=True`` reads the entry points again). Names and
    prefixes only: a host can log it as is."""
    if refresh:
        _installed_toolkits.cache_clear()
    _installed_toolkits()
    return dict(_LEFT_OUT)


def _not_found(unknown: Iterable[str], installed: Mapping[str, type]) -> ToolkitNotFoundError:
    """The error for toolkit names that aren't served, saying why any of them
    was left out (a slug-prefix clash, a name hosts can't route, a name that
    differs from its entry point)."""
    names = sorted(unknown)
    available = ", ".join(installed) or "(none)"
    _, conflicts = _installed_toolkits()
    clash = f". Left out, their slug prefixes collide: {conflicts}" if conflicts else ""
    why = [
        f"{name!r} left out: {_LEFT_OUT[name]}"
        for name in names
        if name in _LEFT_OUT and not _LEFT_OUT[name].startswith(_CLASH_REASON)
    ]
    reasons = f". {'; '.join(why)}" if why else ""
    return ToolkitNotFoundError(
        f"Toolkit(s) {', '.join(map(repr, names))} not found. Available: {available}{clash}{reasons}"
    )


def installed_toolkits(*, refresh: bool = False, strict: bool = False) -> dict[str, type[Toolkit]]:
    """Every installed toolkit class by name, sorted. Entry points are read
    once per process; ``refresh=True`` reads them again (after installing a
    plugin).

    Toolkits whose entry-point name breaks
    :data:`~aisquare.pipe.core.tools.TOOLKIT_NAME_RULE` (a hyphen, say), or
    whose class ``name`` differs from the entry-point name, are left out,
    with one warning logged each way: a host could not serve them as they
    are. :func:`left_out_toolkits` says which and why. So are toolkits whose
    slug prefixes collide (see :func:`slug_prefix_conflicts`),
    with one warning: one clashing pair must not hide
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


def _require_installed(names: Iterable[str]) -> dict[str, type[Toolkit]]:
    """The installed toolkits; :class:`ToolkitNotFoundError` naming any of
    ``names`` that is not served, and why it was left out (see
    :func:`_not_found`)."""
    installed = installed_toolkits()
    unknown = sorted({name for name in names if name not in installed})
    if unknown:
        raise _not_found(unknown, installed)
    return installed


def _searchable(
    connectors: Mapping[str, Mapping[str, Any] | None] | Iterable[str] | None,
) -> list[tuple[str, ToolIndex, frozenset[str] | None, list[ToolSpec]]]:
    """For each chosen toolkit: its name, its cached index, the slugs its
    connection may call (None: all) and those tools, in catalogue order. A
    toolkit that fails to list its tools is logged and has none."""
    if connectors is None:
        chosen: dict[str, Mapping[str, Any] | None] = dict.fromkeys(installed_toolkits())
    elif isinstance(connectors, Mapping):
        chosen = dict(connectors)
    else:
        chosen = dict.fromkeys([connectors] if isinstance(connectors, str) else connectors)
    installed = _require_installed(chosen)
    found: list[tuple[str, ToolIndex, frozenset[str] | None, list[ToolSpec]]] = []
    for name, config in chosen.items():
        try:
            toolkit = installed[name]()
            available = list(toolkit.available_tools(config))
            index, allowed = scoped_index(toolkit, available)
        except Exception as e:  # noqa: BLE001 — one broken plugin must not hide the rest
            logger.warning(f"Failed to list the tools of toolkit {name}: {type(e).__name__}")
            continue
        found.append((name, index, allowed, available))
    return found


def search_tools_across(
    query: str | None,
    connectors: Mapping[str, Mapping[str, Any] | None] | Iterable[str] | None = None,
    *,
    limit: int | None = 6,
) -> list[QualifiedTool]:
    """The tools most relevant to ``query`` across several toolkits, each
    named with its connector, ranked by each toolkit's cached search index
    (see :mod:`aisquare.pipe.core.search` and
    :func:`~aisquare.pipe.core.tools.rank_across`).

    ``connectors`` picks the toolkits: a mapping of name -> the connection's
    ``config`` (so each lists only its :meth:`Toolkit.available_tools`, e.g.
    filtered by ``granted_scopes``), an iterable of names (no config), or None
    for every installed toolkit. An unknown name, or one left out because
    its slug prefix collides with another toolkit's, raises
    :class:`ToolkitNotFoundError`. With no query, or no match, the
    connectors take turns. Each toolkit's index is cached by spec identity,
    so its specs must be immutable (a spec changed in place keeps a stale
    index). Offline, like ``available_tools``, once the
    plugins are imported (see :func:`installed_toolkits`)."""
    found = _searchable(connectors)
    cap = sum(len(available) for *_, available in found) if limit is None else max(limit, 0)
    hits = search_indexes([(index, allowed) for _, index, allowed, _ in found], query, cap)
    if hits:
        return [QualifiedTool(hit.connector, hit.tool) for hit in hits]
    return take_turns({name: available for name, _, _, available in found}, cap)
