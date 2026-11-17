"""Plugin discovery via entry_points for connectors, converters and toolkits,
plus the cross-toolkit views a connector router needs: :func:`catalogue`
(one card per installed toolkit), :func:`search_tools_across`, and the
installed-toolkit searches of the search interface (:func:`index_for`,
:func:`search_scored`, :func:`search_connectors`, re-exported by
:mod:`aisquare.pipe.core.search`). This module imports the search module;
search imports nothing from here while loading, only on first use of
those three names."""

from __future__ import annotations

import functools
import hashlib
import json
import logging
from collections.abc import Iterable, Mapping
from importlib.metadata import entry_points
from typing import Any, NamedTuple

from aisquare.pipe.core.search import (
    ConnectorIndex,
    ScoredTool,
    ToolIndex,
    scoped_index,
    search_indexes,
    toolkit_index,
)
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
    """Get an installed toolkit class by name, from :func:`installed_toolkits`:
    a toolkit it leaves out (a name hosts can't route, a class name that
    differs from its entry point, a slug-prefix clash) is not found, and the
    error says why. :func:`discover_toolkits` still reads every entry point.
    The discovery is cached: a toolkit installed after it is found once
    ``installed_toolkits(refresh=True)`` reads the entry points again.

    Raises ToolkitNotFoundError if not found.
    """
    discovery = _installed_toolkits()
    if name not in discovery.toolkits:
        raise _not_found([name], discovery, label="Toolkit")
    return discovery.toolkits[name]


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


# Clash reasons start with this; the not-found message already names clashes.
_CLASH_REASON = "slug prefix "


class _Discovery(NamedTuple):
    """One read of the toolkit entry points (see :func:`_installed_toolkits`)."""

    #: The toolkits served, by name, sorted.
    toolkits: dict[str, type[Toolkit]]
    #: The slug-prefix collisions, described ("" when none).
    conflicts: str
    #: Why each other installed toolkit was left out, by name, sorted.
    left_out: dict[str, str]


@functools.cache
def _installed_toolkits() -> _Discovery:
    """The installed toolkits minus any whose name hosts cannot route, whose
    class ``name`` differs from its entry-point name, or whose slug prefixes
    collide; a description of the collisions; and why each was left out.
    Each reason is logged once (the result is cached, and the plugins
    imported once). The name checks come first, so a bad plugin can't take
    a good toolkit down with it in a clash. Callers copy what they hand out:
    the cached dicts are never changed."""
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
    return _Discovery(dict(sorted(toolkits.items())), detail, dict(sorted(left_out.items())))


def left_out_toolkits(*, refresh: bool = False) -> dict[str, str]:
    """The installed toolkits :func:`installed_toolkits` leaves out, by name,
    sorted, each with why: ``name breaks <TOOLKIT_NAME_RULE>``, ``name
    differs from its entry point (class name '...')`` or ``slug prefix
    <PREFIX_> collides with <other> (<PREFIX_>)``. Read from the same cached
    discovery (``refresh=True`` reads the entry points again). Names and
    prefixes only: a host can log it as is."""
    if refresh:
        _installed_toolkits.cache_clear()
    return dict(_installed_toolkits().left_out)


def _not_found(
    unknown: Iterable[str], discovery: _Discovery, *, label: str = "Toolkit(s)"
) -> ToolkitNotFoundError:
    """The error for toolkit names that aren't served, saying why any of them
    was left out (a slug-prefix clash, a name hosts can't route, a name that
    differs from its entry point)."""
    names = sorted(unknown)
    available = ", ".join(discovery.toolkits) or "(none)"
    conflicts, left_out = discovery.conflicts, discovery.left_out
    clash = f". Left out, their slug prefixes collide: {conflicts}" if conflicts else ""
    why = [
        f"{name!r} left out: {left_out[name]}"
        for name in names
        if name in left_out and not left_out[name].startswith(_CLASH_REASON)
    ]
    reasons = f". {'; '.join(why)}" if why else ""
    return ToolkitNotFoundError(
        f"{label} {', '.join(map(repr, names))} not found. Available: {available}{clash}{reasons}"
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
    :class:`ToolkitConflictError` instead when any installed toolkit is left
    out, for any of these reasons. ``pipe validate`` always fails on each.

    The first discovery imports every plugin module. What those imports do
    is the plugins' own business: urllib3, which the Slack toolkit imports,
    opens an IPv6 socket at import to see whether the host supports IPv6. A
    host that must stay offline afterwards calls this once at startup."""
    if refresh:
        _installed_toolkits.cache_clear()
    discovery = _installed_toolkits()
    if strict and discovery.left_out:
        raise ToolkitConflictError(_left_out_message(discovery))
    return dict(discovery.toolkits)


def _left_out_message(discovery: _Discovery) -> str:
    """What ``installed_toolkits(strict=True)`` raises: the slug-prefix
    collisions, then every other toolkit left out and why."""
    parts = [f"installed toolkits share a slug prefix: {discovery.conflicts}"] if discovery.conflicts else []
    others = [
        f"{name!r} ({why})"
        for name, why in discovery.left_out.items()
        if not why.startswith(_CLASH_REASON)
    ]
    if others:
        parts.append(f"installed toolkits left out: {'; '.join(others)}")
    return ". ".join(parts)


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


def _require_installed(
    names: Iterable[str], discovery: _Discovery | None = None
) -> dict[str, type[Toolkit]]:
    """The installed toolkits; :class:`ToolkitNotFoundError` naming any of
    ``names`` that is not served, and why it was left out (see
    :func:`_not_found`), both from one discovery (``discovery``, else the
    cached one). Not a copy: don't change it."""
    if discovery is None:
        discovery = _installed_toolkits()
    unknown = sorted({name for name in names if name not in discovery.toolkits})
    if unknown:
        raise _not_found(unknown, discovery)
    return discovery.toolkits


def _searchable(
    connectors: Mapping[str, Mapping[str, Any] | None] | Iterable[str] | None,
) -> list[tuple[str, ToolIndex, frozenset[str] | None, list[ToolSpec]]]:
    """For each chosen toolkit: its name, its cached index, the slugs its
    connection may call (None: all) and those tools, in catalogue order. A
    toolkit that fails to list its tools is logged and has none."""
    # One snapshot: a refresh meanwhile can't make an installed name unknown.
    discovery = _installed_toolkits()
    if connectors is None:
        chosen: dict[str, Mapping[str, Any] | None] = dict.fromkeys(discovery.toolkits)
    elif isinstance(connectors, Mapping):
        chosen = dict(connectors)
    else:
        chosen = dict.fromkeys([connectors] if isinstance(connectors, str) else connectors)
    installed = _require_installed(chosen, discovery)
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


# --------------------------------------------------------------------------
# The installed-toolkit searches of aisquare.pipe.core.search
# --------------------------------------------------------------------------


def index_for(toolkit_name: str) -> ToolIndex:
    """The index of an installed toolkit's whole catalogue, with its card
    (:func:`~aisquare.pipe.core.search.toolkit_index`): built once, and
    rebuilt only when the toolkit's name, version, card or catalogue
    changes, however many toolkits are installed. Raises
    :class:`~aisquare.pipe.errors.ToolkitNotFoundError` for a toolkit that
    is not installed or is left out (see :func:`installed_toolkits`). Also
    importable from :mod:`aisquare.pipe.core.search`."""
    return toolkit_index(_require_installed([toolkit_name])[toolkit_name]())


def search_scored(
    query: str | None,
    connectors: Mapping[str, Mapping[str, Any] | None] | Iterable[str] | None = None,
    *,
    limit: int | None = 10,
) -> list[ScoredTool]:
    """The installed tools matching ``query`` across ``connectors``, best
    first, with scores. ``connectors`` takes the forms of
    :func:`search_tools_across`: a mapping of name -> the connection's
    ``config`` (each toolkit then offers only its
    :meth:`Toolkit.available_tools` for it), an iterable of names, or None
    for every installed toolkit; an unknown name raises
    :class:`~aisquare.pipe.errors.ToolkitNotFoundError`. Only matches are
    returned: unlike ``search_tools_across`` there is no fallback. Each
    toolkit's index comes from :func:`index_for`'s cache. Also importable
    from :mod:`aisquare.pipe.core.search`."""
    return search_indexes([(index, allowed) for _, index, allowed, _ in _searchable(connectors)], query, limit)


_connector_index: tuple[str, ConnectorIndex] | None = None


def search_connectors(query: str | None, limit: int | None = 10) -> list[tuple[str, float]]:
    """:meth:`~aisquare.pipe.core.search.ConnectorIndex.search` over the
    installed toolkits' cards (:func:`catalogue`); the index is rebuilt only
    when the cards change. Also importable from
    :mod:`aisquare.pipe.core.search`."""
    global _connector_index
    cards = catalogue()
    key = hashlib.blake2b(
        json.dumps(cards, sort_keys=True, default=str).encode(), digest_size=16
    ).hexdigest()
    current = _connector_index
    if current is None or current[0] != key:
        current = (key, ConnectorIndex.build(cards))
        _connector_index = current
    return current[1].search(query, limit)
