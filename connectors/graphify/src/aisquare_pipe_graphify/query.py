"""Query face over a stored ``graph.json`` — graphify V2 Phase 1.

Pure functions over an in-memory networkx graph; no filesystem walks, no CLI
subprocess, no network. The consumer (the Studio BE's chat tools) fetches the
blob bytes itself, calls :func:`load_graph_from_json` ONCE, caches the parsed
graph per worker, and serves every verb from that object — traversal is
sub-millisecond at any realistic scale; the only real cost is the cold parse.

The traversal/scoring logic is graphifyy's own (``graphify.serve`` /
``graphify.affected`` pure functions) — re-exported, not reimplemented, so the
chat answers and the local ``graphify query`` CLI stay byte-consistent. This
module is the connector's Python-API face of those functions; it needs the
``[engine]`` extra (``pip install "aisquare-pipe-graphify[engine]"``) so that
``graphify`` and ``networkx`` are importable in-process.

Verb contract (mirrors the V2 build plan §5.2): node ids in graph.json are
lossy slugs (``session_validatetoken``) a model can never produce cold —
``find_nodes`` is the mandatory first verb; everything else takes the ids it
returned.
"""

from __future__ import annotations

import json
from collections import Counter

try:
    import networkx as nx
    from networkx.readwrite import json_graph
except ImportError as exc:  # pragma: no cover — import-time guard
    raise ImportError(
        "aisquare-pipe-graphify's query face needs networkx/graphifyy importable — "
        "install the engine extra: pip install 'aisquare-pipe-graphify[engine]'"
    ) from exc

__all__ = [
    "load_graph_from_json",
    "find_nodes",
    "query_graph_text",
    "neighbors_text",
    "shortest_path_text",
    "blast_radius_text",
    "resolve_node_id",
]

# Render caps — neighbors of a god node can be thousands; the count-first
# header carries the full truth, the line listing is just a sample.
_NEIGHBOR_LINE_CAP = 30
_FIND_LIMIT = 8


def load_graph_from_json(graph_json: str) -> "nx.DiGraph":
    """Parse node-link ``graph.json`` text into a DIRECTED graph.

    Mirrors ``graphify.affected.load_graph`` (#1174): ``directed`` is forced so
    the stored caller→callee orientation survives the round-trip — blast-radius
    and path direction are meaningless without it. Accepts both the modern
    ``links`` and legacy ``edges`` key. Raises ``ValueError`` on unparseable
    input (the caller decides how to degrade; nothing here ``sys.exit``s).
    """
    try:
        data = json.loads(graph_json)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError(f"graph.json is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError("graph.json must be a node-link JSON object")
    if "links" not in data and "edges" in data:
        data = dict(data, links=data["edges"])
    data = {**data, "directed": True}
    try:
        return json_graph.node_link_graph(data, edges="links")
    except TypeError:  # older networkx without the edges= kwarg
        return json_graph.node_link_graph(data)


def _sanitize(text: str) -> str:
    """LLM-derived fields pass through graphify's output hardening (F-010) —
    a corpus document must not inject ANSI/markup into the model's context."""
    from graphify.security import sanitize_label

    return sanitize_label(str(text))


def find_nodes(G: "nx.Graph", query: str, *, limit: int = _FIND_LIMIT) -> list[dict]:
    """Ranked candidates for a human name — the mandatory first verb.

    Wraps ``graphify.serve._find_node`` (exact > prefix > substring,
    diacritic-insensitive) and decorates each id with what the model needs to
    disambiguate: label, kind, source file, and degree (how connected it is).
    """
    from graphify.serve import _find_node

    results = []
    for node_id in _find_node(G, query)[: max(1, int(limit))]:
        data = G.nodes[node_id]
        results.append(
            {
                "id": node_id,
                "label": _sanitize(data.get("label", node_id)),
                "kind": _sanitize(data.get("kind", data.get("type", ""))),
                "source_file": _sanitize(data.get("source_file", "")),
                "degree": int(G.degree(node_id)),
            }
        )
    return results


def resolve_node_id(G: "nx.Graph", query: str) -> str | None:
    """Single best node id for ``query``, or None when ambiguous/missing.

    Exact id → unique exact label → unique source_file → unique substring
    (``graphify.affected.resolve_seed``), then the top ``find_nodes`` hit as a
    last resort so verbs stay forgiving when the model passes a label.
    """
    from graphify.affected import resolve_seed

    seed = resolve_seed(G, query)
    if seed is not None:
        return seed
    hits = find_nodes(G, query, limit=1)
    return hits[0]["id"] if hits else None


def query_graph_text(
    G: "nx.Graph",
    question: str,
    *,
    mode: str = "bfs",
    depth: int = 3,
    token_budget: int = 2000,
) -> str:
    """Token-budgeted subgraph-as-text for a natural-language question.

    graphifyy's own IDF-seeded, hub-capped traversal (``serve._query_graph_text``)
    — identical output to the local ``graphify query`` CLI.
    """
    from graphify.serve import _query_graph_text

    return _query_graph_text(
        G, question, mode=mode, depth=max(1, int(depth)), token_budget=max(200, int(token_budget))
    )


def neighbors_text(
    G: "nx.Graph",
    node_id: str,
    *,
    relation: str | None = None,
    direction: str = "both",
    limit: int = _NEIGHBOR_LINE_CAP,
) -> str:
    """Count-first neighbor listing with a god-node guard.

    The header always carries the FULL degree + per-relation breakdown; the
    line listing is a capped sample — a hub with 900 callers must never return
    900 lines into a chat context. ``relation``/``direction`` narrow the page.
    """
    resolved = resolve_node_id(G, node_id)
    if resolved is None:
        return f"No node found for {node_id!r}. Use the node search first."
    data = G.nodes[resolved]
    directed = G.is_directed()
    in_edges = list(G.in_edges(resolved, data=True)) if directed else []
    out_edges = list(G.out_edges(resolved, data=True)) if directed else list(G.edges(resolved, data=True))

    rows = []  # (arrow, other_id, relation)
    if direction in ("in", "both"):
        rows += [("<-", u, str(d.get("relation", ""))) for u, _v, d in in_edges]
    if direction in ("out", "both"):
        rows += [("->", v, str(d.get("relation", ""))) for _u, v, d in out_edges]
    relation_counts = Counter(rel or "(untyped)" for _a, _o, rel in rows)
    if relation:
        rows = [row for row in rows if row[2] == relation]

    label = _sanitize(data.get("label", resolved))
    header = (
        f"{label} [src={_sanitize(data.get('source_file', ''))}] — "
        f"{len(in_edges)} incoming / {len(out_edges)} outgoing"
    )
    breakdown = ", ".join(f"{rel}: {count}" for rel, count in relation_counts.most_common())
    lines = [header, f"Relations: {breakdown or '(none)'}", ""]
    cap = max(1, int(limit))
    for arrow, other, rel in rows[:cap]:
        other_data = G.nodes[other]
        lines.append(
            f"  {arrow} {_sanitize(other_data.get('label', other))} "
            f"[{_sanitize(rel)}] (id={other}, src={_sanitize(other_data.get('source_file', ''))})"
        )
    hidden = len(rows) - cap
    if hidden > 0:
        lines.append(
            f"  … +{hidden} more — narrow with relation=<one of: "
            f"{', '.join(rel for rel, _ in relation_counts.most_common(5))}> or direction=in/out"
        )
    return "\n".join(lines)


def shortest_path_text(G: "nx.Graph", source: str, target: str) -> str:
    """Directed shortest path A→B (undirected fallback), with edge relations.

    Both ends accept ids or labels (forgiving resolution). The undirected
    fallback is announced in the output — "connected, but not in that
    direction" is a materially different answer from "connected"."""
    src = resolve_node_id(G, source)
    dst = resolve_node_id(G, target)
    if src is None or dst is None:
        missing = source if src is None else target
        return f"No node found for {missing!r}. Use the node search first."
    via_undirected = False
    try:
        path = nx.shortest_path(G, src, dst)
    except (nx.NetworkXNoPath, nx.NodeNotFound):
        try:
            path = nx.shortest_path(G.to_undirected(as_view=True), src, dst)
            via_undirected = True
        except (nx.NetworkXNoPath, nx.NodeNotFound):
            return (
                f"No path between {_sanitize(G.nodes[src].get('label', src))} and "
                f"{_sanitize(G.nodes[dst].get('label', dst))} in either direction."
            )
    parts = []
    for i, node_id in enumerate(path):
        parts.append(_sanitize(G.nodes[node_id].get("label", node_id)))
        if i < len(path) - 1:
            edge = G.get_edge_data(path[i], path[i + 1]) or G.get_edge_data(path[i + 1], path[i]) or {}
            if G.is_multigraph() and edge:  # multi edge data is keyed — unwrap (serve.py pattern)
                edge = next(iter(edge.values()), {})
            rel = str(edge.get("relation", "")) or "related"
            parts.append(f" --{_sanitize(rel)}--> ")
    rendered = "".join(parts)
    note = " (note: path exists only ignoring edge direction)" if via_undirected else ""
    return f"Path ({len(path) - 1} hops){note}: {rendered}"


def blast_radius_text(
    G: "nx.Graph", query: str, *, depth: int = 2, extra_relations: tuple = ()
) -> str:
    """Reverse-BFS impact set: everything that (transitively) depends on the
    node — ``graphify.affected.format_affected`` verbatim (resolves its own
    seed, names the relations it followed, lists source locations).

    ``extra_relations`` EXTENDS the engine's default dependency-relation set —
    the seam merged-graph consumers need: cross-repo bridge edges carry their
    own relation kinds (``http_call``/``lib_dep``/``infra``,
    :data:`aisquare_pipe_graphify.merger.BRIDGE_RELATIONS`), which the engine's
    code-level defaults don't include, so without this a blast radius over a
    merged graph silently stops at repo boundaries.
    """
    from graphify.affected import DEFAULT_AFFECTED_RELATIONS, format_affected

    relations = tuple(DEFAULT_AFFECTED_RELATIONS) + tuple(extra_relations)
    return format_affected(G, query, relations=relations, depth=max(1, min(int(depth), 4)))
