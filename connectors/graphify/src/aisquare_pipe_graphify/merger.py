"""GraphMerger — N per-repo graphs → ONE studio-level merged graph (V2 Phase 2).

The deterministic linker behind the "studio master graph": load already-built
per-repo ``graph.json`` payloads, namespace them so same-named nodes can't
collide, compose them into one DIRECTED graph, dedup shared external-library
nodes, and inject caller-provided cross-repo BRIDGE edges (the deterministic
FE→BE/lib/infra links the engine itself never draws). $0 by construction — no
clone, no LLM, no network; recomposing from stored blobs takes well under a
second at any realistic studio size.

Reuses the engine's own multi-repo primitives (``build.prefix_graph_for_global``
+ the external-lib dedup rule from ``global_graph.global_add``) but NOT its
persistence: ``global_add`` accumulates into a ``~/.graphify`` home-dir
singleton with no locking — unusable multi-tenant. Persistence is the caller's
problem; this function is pure (inputs → merged JSON + stats).

Decisions baked in (V2 build plan §8):
- **D4 (middle):** compose a plain DiGraph — ONE traversable edge per
  (source, target) pair carrying a ``relations`` list with every relationship
  type observed between them. Direction preserved (``global_add`` itself loads
  undirected and would flatten it); parallel-edge richness kept on the label,
  not as separate traversable edges, so ``serve``/``affected`` consume the
  result unchanged.
- **Leiden is NEVER re-run over the union** — community detection over a
  multi-repo graph just rediscovers the repo boundaries. Per-repo community
  attributes pass through untouched; cross-repo structure is EXCLUSIVELY the
  explicit bridge-edge layer.
"""

from __future__ import annotations

import json

try:
    import networkx as nx
    from networkx.readwrite import json_graph
except ImportError as exc:  # pragma: no cover — import-time guard
    raise ImportError(
        "aisquare-pipe-graphify's merger needs networkx/graphifyy importable — "
        "install the engine extra: pip install 'aisquare-pipe-graphify[engine]'"
    ) from exc

from aisquare_pipe_graphify.query import load_graph_from_json

__all__ = ["merge_graphs"]

# Synthetic per-repo anchor for bridge edges whose evidence file maps to no
# graph node (different head_shas between pack and graph, skeleton packs, …).
# The bridge still renders — anchored at the repo, honestly marked unmapped.
_REPO_ROOT_SUFFIX = "::__repo__"


def _repo_root(merged: "nx.DiGraph", repo_tag: str) -> str:
    node_id = f"{repo_tag}{_REPO_ROOT_SUFFIX}"
    if node_id not in merged:
        merged.add_node(
            node_id,
            label=repo_tag,
            kind="repo",
            repo=repo_tag,
            local_id="__repo__",
        )
    return node_id


def _node_for_file(merged: "nx.DiGraph", repo_tag: str, source_file: str) -> str | None:
    """Exact repo-relative-posix match on the nodes' ``source_file`` attr —
    the ONLY reliable join: node ids are file-STEM slugs, not derivable from
    paths. Prefers the lowest-degree match (a file-level node over a hub)."""
    if not source_file:
        return None
    candidates = [
        node_id
        for node_id, data in merged.nodes(data=True)
        if data.get("repo") == repo_tag and data.get("source_file") == source_file
    ]
    if not candidates:
        return None
    return min(candidates, key=lambda node_id: merged.degree(node_id))


def _add_collapsed_edge(merged: "nx.DiGraph", source: str, target: str, data: dict) -> None:
    """D4 middle option: one directed edge per pair, ``relations`` carries all."""
    relation = str(data.get("relation", "")) or "related"
    if merged.has_edge(source, target):
        existing = merged[source][target]
        relations = existing.get("relations") or [existing.get("relation", "related")]
        if relation not in relations:
            relations.append(relation)
        existing["relations"] = relations
        return
    attrs = dict(data)
    attrs["relation"] = relation
    attrs["relations"] = [relation]
    merged.add_edge(source, target, **attrs)


def merge_graphs(
    repo_graphs: list,
    bridge_edges: list | None = None,
) -> tuple[str, dict]:
    """Compose per-repo graphs + bridge edges into one merged node-link JSON.

    Args:
        repo_graphs: list of ``(repo_tag, graph_json_str)`` pairs. The tag
            namespaces node ids (``repo_tag::node``) and should be the repo's
            ``full_name`` so bridge edges key on the same string.
        bridge_edges: cross-repo links from the deterministic extractors, each
            ``{kind, source_repo, target_repo, source_file?, target_file?,
            evidence?, confidence?}``. The caller applies its confidence gate
            BEFORE passing them — everything received is injected.

    Returns:
        ``(merged_json, stats)`` — node-link JSON (``edges="links"``) readable
        by every existing graphify tool, and a stats dict:
        ``{repos, nodes, edges, external_nodes_deduped, bridges_added,
        bridges: [{...edge, source_node, target_node, mapping}]}`` where
        ``mapping`` ∈ ``exact | source_root | target_root | both_roots``.
    """
    from graphify.build import prefix_graph_for_global

    merged = nx.DiGraph()
    # External-library dedup (the ``global_add`` rule): nodes with NO
    # source_file are shared third-party libs — one canonical node per label
    # across all repos, endpoints remapped onto it.
    external_by_label: dict = {}
    deduped = 0

    for repo_tag, graph_json_str in repo_graphs:
        source_graph = prefix_graph_for_global(load_graph_from_json(graph_json_str), repo_tag)
        remap: dict = {}
        for node_id, data in source_graph.nodes(data=True):
            label = data.get("label", "")
            if not data.get("source_file") and label:
                canonical = external_by_label.get(label)
                if canonical is not None:
                    remap[node_id] = canonical
                    deduped += 1
                    continue
                external_by_label[label] = node_id
            merged.add_node(node_id, **data)
        for source, target, data in source_graph.edges(data=True):
            _add_collapsed_edge(
                merged, remap.get(source, source), remap.get(target, target), data
            )

    bridges_audit = []
    for edge in bridge_edges or []:
        source_repo = edge.get("source_repo", "")
        target_repo = edge.get("target_repo", "")
        source_node = _node_for_file(merged, source_repo, edge.get("source_file", ""))
        target_node = _node_for_file(merged, target_repo, edge.get("target_file", ""))
        mapping = "exact"
        if source_node is None and target_node is None:
            mapping = "both_roots"
        elif source_node is None:
            mapping = "source_root"
        elif target_node is None:
            mapping = "target_root"
        source_node = source_node or _repo_root(merged, source_repo)
        target_node = target_node or _repo_root(merged, target_repo)
        _add_collapsed_edge(
            merged,
            source_node,
            target_node,
            {
                "relation": str(edge.get("kind", "bridge")),
                "bridge": True,
                "kind": edge.get("kind", ""),
                "confidence": edge.get("confidence", ""),
                "evidence": edge.get("evidence", ""),
                "mapping": mapping,
            },
        )
        bridges_audit.append(
            {**edge, "source_node": source_node, "target_node": target_node, "mapping": mapping}
        )

    try:
        data = json_graph.node_link_data(merged, edges="links")
    except TypeError:  # older networkx
        data = json_graph.node_link_data(merged)
    stats = {
        "repos": len(repo_graphs),
        "nodes": merged.number_of_nodes(),
        "edges": merged.number_of_edges(),
        "external_nodes_deduped": deduped,
        "bridges_added": len(bridges_audit),
        "bridges": bridges_audit,
    }
    return json.dumps(data), stats
