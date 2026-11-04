#!/usr/bin/env python
"""Benchmark the BM25F search index (aisquare.pipe.core.search) on synthetic
catalogues (aisquare.pipe.testing.synthetic):

  (a) one toolkit of 10,000 tools: index build time and memory, query
      latency p50/p95;
  (b) 1,000 connectors x 50 tools: ConnectorIndex build and query latency,
      building every per-toolkit index, and a hierarchical search (connector
      shortlist -> those toolkits' indexes) as a router runs it;
  (c) the CACHED path a host takes, at 1,000 and 2,000 installed toolkits:
      the synthetic toolkits are installed (entry-point discovery replaced
      for the run) and searched through search_scored (after a
      search_connectors shortlist, and over all of them),
      search_tools_across and Toolkit.search_tools (every toolkit in turn),
      so each toolkit's index comes from the index cache. Twice: toolkits
      whose tools() returns the same ToolSpec objects each call ("stable"),
      and toolkits that build new, equal ones each call ("fresh"). One cold
      search builds every index; the repeated searches after it report the
      real reuse (1 - indexes built / toolkits searched; every
      ToolIndex.build is counted), the cache hit rate, the ad-hoc LRU's
      entries, and latency.

Budgets (the search task's acceptance): 10k build <= 2 s, 10k query p95 <=
20 ms, ConnectorIndex over 1,000 cards query p95 <= 10 ms, and in (c), on
the repeated searches of every size, shape and path, reuse and cache hit
rate >= 99% with no ad-hoc LRU entries. Exit status 1 when a budget is
missed.

    python scripts/bench_search.py                          # the defaults above
    python scripts/bench_search.py --json                   # machine-readable
    python scripts/bench_search.py --cached-sizes 600,1200  # other (c) sizes
    python scripts/bench_search.py --cached-specs fresh     # one (c) shape
"""

from __future__ import annotations

import argparse
import contextlib
import dataclasses
import gc
import itertools
import json
import platform
import random
import statistics
import sys
import time
import tracemalloc
from collections.abc import Callable, Iterator
from typing import Any

from aisquare.pipe.core import registry
from aisquare.pipe.core.registry import installed_toolkits, search_tools_across
from aisquare.pipe.core.search import (
    ConnectorIndex,
    ToolIndex,
    cache_clear,
    cache_info,
    search_connectors,
    search_indexes,
    search_scored,
)
from aisquare.pipe.core.tools import Toolkit, ToolResult, ToolSpec
from aisquare.pipe.testing.synthetic import synthetic_catalog

BUDGET = {"build_10k_s": 2.0, "query_10k_p95_ms": 20.0, "connectors_query_p95_ms": 10.0}
#: (c): the least reuse and cache hit rate on repeated searches.
HIT_RATE_FLOOR = 0.99
#: (c): how the synthetic toolkits' tools() hands out its ToolSpecs.
SPEC_SHAPES = ("stable", "fresh")
#: (c): the paths measured, as printed.
CACHED_PATHS = {
    "hierarchical": "hierarchical",
    "search_scored": "search_scored",
    "search_tools_across": "search_tools_across",
    "toolkit_search_tools": "Toolkit.search_tools",
}

# Queries a user or agent would type: exact, paraphrased, first-person,
# several intents, some with no match at all.
QUERY_TEMPLATES = (
    "list {os}", "show me all the {os}", "find the {o} for Acme", "search {os} about the launch",
    "create a new {o}", "add a {o} for the Q3 review", "delete the old {o}", "remove that {o}",
    "update the {o} status to done", "rename the {o}", "archive last year's {os}",
    "who owns this {o}", "my {os}", "{os} assigned to me", "export {os} as csv",
    "send the {o} to the team", "share the {o} with Priya", "move the {o} to another folder",
    "get {o} details by id", "what changed in the {o} this week",
)
OBJECTS = (
    "invoice", "contact", "deal", "ticket", "issue", "file", "folder", "message", "channel",
    "event", "calendar", "repository", "branch", "document", "page", "dashboard", "report",
    "table", "order", "customer", "payment", "subscription", "reminder", "label", "draft",
)


def queries(n: int, seed: int) -> list[str]:
    rng = random.Random(seed)
    out = []
    for _ in range(n):
        obj = rng.choice(OBJECTS)
        plural = obj[:-1] + "ies" if obj.endswith("y") and obj[-2] not in "aeiou" else obj + "s"
        out.append(rng.choice(QUERY_TEMPLATES).format(o=obj, os=plural))
    return out


def timed(fn: Callable[[], Any]) -> tuple[Any, float]:
    start = time.perf_counter()
    result = fn()
    return result, time.perf_counter() - start


def latencies(fn: Callable[[str], Any], qs: list[str], warmup: int = 20) -> dict[str, float]:
    for q in qs[:warmup]:
        fn(q)
    samples = []
    for q in qs:
        start = time.perf_counter()
        fn(q)
        samples.append((time.perf_counter() - start) * 1000)
    samples.sort()
    return {
        "n": len(samples),
        "p50_ms": statistics.median(samples),
        "p95_ms": samples[min(len(samples) - 1, int(len(samples) * 0.95))],
        "max_ms": samples[-1],
    }


def measured_build(build: Callable[[], Any]) -> tuple[Any, float, float, float]:
    """(result, seconds untraced, retained MB, peak MB). Memory comes from a
    second, traced build (tracemalloc slows Python several times)."""
    gc.collect()
    result, seconds = timed(build)
    del result
    gc.collect()
    tracemalloc.start()
    result = build()
    retained, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    return result, seconds, retained / 1e6, peak / 1e6


def bench_big_toolkit(n_tools: int, n_queries: int, seed: int) -> dict[str, Any]:
    cards, tools = synthetic_catalog(1, n_tools, seed=seed)
    name = next(iter(tools))
    catalogue, card = tools[name], cards[name]
    builds = [timed(lambda: ToolIndex.build(name, catalogue, card))[1] for _ in range(3)]
    index, _, retained, peak = measured_build(lambda: ToolIndex.build(name, catalogue, card))
    qs = queries(n_queries, seed)
    lat = latencies(lambda q: index.search(q, 10), qs)
    matched = sum(1 for q in qs if index.search(q, 10)) / len(qs)
    return {
        "tools": len(catalogue),
        "build_s": min(builds),
        "build_s_runs": builds,
        "index_mb": retained,
        "build_peak_mb": peak,
        "query": lat,
        "queries_with_hits": matched,
    }


def bench_many_connectors(
    n_connectors: int, tools_each: int, n_queries: int, shortlist: int, seed: int
) -> dict[str, Any]:
    (cards, tools), gen_s = timed(lambda: synthetic_catalog(n_connectors, tools_each, seed=seed))
    connector_index, build_s, retained, peak = measured_build(lambda: ConnectorIndex.build(cards))
    qs = queries(n_queries, seed + 1)
    lat = latencies(lambda q: connector_index.search(q, shortlist), qs)

    indexes, all_build_s = timed(
        lambda: {name: ToolIndex.build(name, tools[name], cards[name]) for name in cards}
    )

    def hierarchical(q: str) -> Any:
        names = [name for name, _ in connector_index.search(q, shortlist)]
        return search_indexes([indexes[n] for n in names], q, 10)

    hier = latencies(hierarchical, qs)
    flat_qs = qs[: max(20, n_queries // 10)]
    flat = latencies(lambda q: search_indexes(list(indexes.values()), q, 10), flat_qs, warmup=2)
    return {
        "connectors": len(cards),
        "tools_each": tools_each,
        "generate_s": gen_s,
        "connector_index": {"build_s": build_s, "mb": retained, "build_peak_mb": peak, "query": lat},
        "toolkit_indexes": {"build_all_s": all_build_s, "per_toolkit_ms": all_build_s * 1000 / len(cards)},
        "hierarchical_query": {"shortlist": shortlist, **hier},
        "flat_query_all_toolkits": flat,
    }


def synthetic_toolkits(
    cards: dict[str, dict[str, Any]], tools: dict[str, list[ToolSpec]], *, fresh: bool = False
) -> dict[str, type]:
    """A Toolkit class per synthetic connector, as a plugin would define it:
    its card from class attributes; tools() returns the same ToolSpec
    objects on each call, or (``fresh``) new, equal ones."""

    def make(name: str) -> type:
        card, specs = cards[name], tuple(tools[name])

        def list_tools(self: Toolkit) -> list[ToolSpec]:
            return [dataclasses.replace(spec) for spec in specs] if fresh else list(specs)

        def run_tool(self: Toolkit, tool: ToolSpec, arguments: dict[str, Any], config: dict[str, Any]) -> ToolResult:
            return ToolResult.ok(None)

        attrs = {key: card[key] for key in ("display_name", "description", "category")}
        attrs.update(keywords=tuple(card["keywords"]), examples=tuple(card["examples"]))
        return type(f"Bench_{name}", (Toolkit,), {"name": name, "version": "1.0.0", "tools": list_tools, "run_tool": run_tool, **attrs})

    return {name: make(name) for name in cards}


@contextlib.contextmanager
def counting_builds() -> Iterator[list[int]]:
    """Count every ToolIndex.build (each index any cache builds) while open."""
    count = [0]
    build = ToolIndex.__dict__["build"]

    def counted_build(cls: type[ToolIndex], *args: Any, **kwargs: Any) -> ToolIndex:
        count[0] += 1
        return build.__func__(cls, *args, **kwargs)  # type: ignore[no-any-return]

    setattr(ToolIndex, "build", classmethod(counted_build))  # noqa: B010
    try:
        yield count
    finally:
        setattr(ToolIndex, "build", build)  # noqa: B010


def reused(fn: Callable[[str], int], qs: list[str], warmup: int, builds: list[int]) -> dict[str, Any]:
    """latencies() of ``fn`` over ``qs`` (``fn`` returns how many toolkits it
    searched) and what the caches did in the timed calls (the warm-up calls
    are not counted): indexes built, real reuse (1 - builds / toolkits
    searched), and the cache's hits and misses."""
    for q in qs[:warmup]:
        fn(q)
    searched = [0]

    def run(q: str) -> None:
        searched[0] += fn(q)

    before, built = cache_info(), builds[0]
    lat = latencies(run, qs, warmup=0)
    after, n_built = cache_info(), builds[0] - built
    hits, misses = after["hits"] - before["hits"], after["misses"] - before["misses"]
    return {
        **lat,
        "toolkits_searched": searched[0],
        "builds": n_built,
        "reuse": 1 - n_built / searched[0] if searched[0] else 0.0,
        "hits": hits,
        "misses": misses,
        "hit_rate": hits / (hits + misses) if hits + misses else 0.0,
    }


def bench_cached(
    n_connectors: int, tools_each: int, n_queries: int, shortlist: int, seed: int, *, fresh: bool
) -> dict[str, Any]:
    """(c) at one size and spec shape: install ``n_connectors`` synthetic
    toolkits, search them cold once, then repeatedly through each cached
    path."""
    cards, tools = synthetic_catalog(n_connectors, tools_each, seed=seed)
    toolkits = synthetic_toolkits(cards, tools, fresh=fresh)
    discover = registry.discover_toolkits
    registry.discover_toolkits = lambda: dict(toolkits)  # type: ignore[assignment]
    try:
        installed = installed_toolkits(refresh=True)
        assert len(installed) == n_connectors, f"{n_connectors - len(installed)} synthetic toolkits left out"
        cache_clear()
        with counting_builds() as builds:
            qs = queries(n_queries, seed + 2)
            _, cold_s = timed(lambda: search_scored(qs[0], limit=10))
            cold_builds = builds[0]

            def hierarchical(q: str) -> int:
                names = [name for name, _ in search_connectors(q, shortlist)]
                search_scored(q, names, limit=10)
                return len(names)

            def scored_all(q: str) -> int:
                search_scored(q, limit=10)
                return n_connectors

            def across_all(q: str) -> int:
                search_tools_across(q, limit=10)
                return n_connectors

            turn = itertools.cycle(toolkits.values())

            def toolkit_search(q: str) -> int:
                next(turn)().search_tools(q)
                return 1

            flat_qs = qs[: max(20, n_queries // 10)]
            # Every toolkit in turn, twice (warm-up, then timed): the cyclic
            # pattern an LRU smaller than the install set cannot hold.
            each_qs = queries(n_connectors, seed + 3)
            paths = {
                "hierarchical": reused(hierarchical, qs, 20, builds),
                "search_scored": reused(scored_all, flat_qs, 2, builds),
                "search_tools_across": reused(across_all, flat_qs, 2, builds),
                "toolkit_search_tools": reused(toolkit_search, each_qs, n_connectors, builds),
            }
        return {
            "connectors": n_connectors,
            "tools_each": tools_each,
            "specs": "fresh" if fresh else "stable",
            "cold_search": {"s": cold_s, "builds": cold_builds},
            **paths,
            # One slot per toolkit; anything else the cache holds is in the LRU.
            "lru_entries": cache_info()["size"] - n_connectors,
        }
    finally:
        registry.discover_toolkits = discover  # type: ignore[assignment]
        installed_toolkits(refresh=True)
        cache_clear()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--tools", type=int, default=10_000, help="tools in the big toolkit")
    parser.add_argument("--connectors", type=int, default=1_000)
    parser.add_argument("--tools-each", type=int, default=50)
    parser.add_argument("--queries", type=int, default=500)
    parser.add_argument("--shortlist", type=int, default=8, help="connectors searched per query")
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument(
        "--cached-sizes", default="1000,2000",
        help="installed-toolkit counts for (c), comma-separated (empty: skip (c))",
    )
    parser.add_argument("--cached-queries", type=int, default=200, help="repeated searches per size in (c)")
    parser.add_argument(
        "--cached-specs", default=",".join(SPEC_SHAPES),
        help="tools() shapes for (c): stable (same ToolSpecs each call), fresh (new, equal ones)",
    )
    parser.add_argument("--json", action="store_true", help="print JSON only")
    args = parser.parse_args(argv)

    big = bench_big_toolkit(args.tools, args.queries, args.seed)
    many = bench_many_connectors(args.connectors, args.tools_each, args.queries, args.shortlist, args.seed)
    sizes = [int(n) for n in args.cached_sizes.split(",") if n.strip()]
    shapes = [shape.strip() for shape in args.cached_specs.split(",") if shape.strip()]
    assert set(shapes) <= set(SPEC_SHAPES), f"--cached-specs takes {', '.join(SPEC_SHAPES)}"
    cached = [
        bench_cached(n, args.tools_each, args.cached_queries, args.shortlist, args.seed, fresh=shape == "fresh")
        for n in sizes
        for shape in shapes
    ]
    checks = {
        "build_10k_s": big["build_s"],
        "query_10k_p95_ms": big["query"]["p95_ms"],
        "connectors_query_p95_ms": many["connector_index"]["query"]["p95_ms"],
    }
    ok = {k: checks[k] <= BUDGET[k] for k in BUDGET}
    floors = {
        f"cached_{c['connectors']}_{c['specs']}_{path}_{metric}": c[path][metric]
        for c in cached
        for path in CACHED_PATHS
        for metric in ("reuse", "hit_rate")
    }
    ok.update({k: rate >= HIT_RATE_FLOOR for k, rate in floors.items()})
    lru = {f"cached_{c['connectors']}_{c['specs']}_lru_entries": c["lru_entries"] for c in cached}
    ok.update({k: n == 0 for k, n in lru.items()})
    report = {
        "python": platform.python_version(),
        "machine": f"{platform.system()} {platform.machine()} ({platform.processor() or 'n/a'})",
        "big_toolkit": big,
        "many_connectors": many,
        "cached": cached,
        "budget": {**BUDGET, "cached_reuse_and_hit_rate_min": HIT_RATE_FLOOR, "cached_lru_entries_max": 0},
        "within_budget": ok,
    }
    if args.json:
        print(json.dumps(report, indent=2))
        return 0 if all(ok.values()) else 1

    q, cq, hq, fq = big["query"], many["connector_index"]["query"], many["hierarchical_query"], many["flat_query_all_toolkits"]
    lines = [
        f"python {report['python']} on {report['machine']}",
        "",
        f"(a) one toolkit, {big['tools']:,} tools",
        f"    build            {big['build_s']:.3f} s (best of 3: {', '.join(f'{s:.3f}' for s in big['build_s_runs'])})",
        f"    memory           index {big['index_mb']:.1f} MB, build peak {big['build_peak_mb']:.1f} MB",
        f"    query (n={q['n']})    p50 {q['p50_ms']:.2f} ms, p95 {q['p95_ms']:.2f} ms, max {q['max_ms']:.2f} ms",
        f"    queries with hits {big['queries_with_hits']:.0%}",
        "",
        f"(b) {many['connectors']:,} connectors x {many['tools_each']} tools",
        f"    ConnectorIndex   build {many['connector_index']['build_s']:.3f} s, {many['connector_index']['mb']:.1f} MB",
        f"    connector query  p50 {cq['p50_ms']:.2f} ms, p95 {cq['p95_ms']:.2f} ms, max {cq['max_ms']:.2f} ms",
        (
            f"    toolkit indexes  build all {many['toolkit_indexes']['build_all_s']:.2f} s "
            f"({many['toolkit_indexes']['per_toolkit_ms']:.2f} ms each)"
        ),
        f"    hierarchical     shortlist {hq['shortlist']} -> tools: p50 {hq['p50_ms']:.2f} ms, p95 {hq['p95_ms']:.2f} ms",
        f"    flat (all {many['connectors']:,})  p50 {fq['p50_ms']:.2f} ms, p95 {fq['p95_ms']:.2f} ms (n={fq['n']}; for scale only)",
    ]
    for c in cached:
        lines += [
            "",
            f"(c) cached path, {c['connectors']:,} installed toolkits x {c['tools_each']} tools, {c['specs']} specs",
            f"    cold search      {c['cold_search']['s']:.2f} s, built {c['cold_search']['builds']:,} indexes",
            *(
                f"    {label:<20} reuse {r['reuse']:.2%} ({r['builds']:,} builds / {r['toolkits_searched']:,} toolkit "
                f"searches), hits {r['hit_rate']:.2%}, p50 {r['p50_ms']:.2f} ms, p95 {r['p95_ms']:.2f} ms (n={r['n']})"
                for path, label in CACHED_PATHS.items()
                for r in (c[path],)
            ),
            f"    ad-hoc LRU           {c['lru_entries']:,} entries",
        ]
    lines += [
        "",
        "budget: " + ", ".join(f"{k} {checks[k]:.3f} <= {BUDGET[k]} {'ok' if ok[k] else 'MISSED'}" for k in BUDGET),
        *(
            f"        {k} {rate:.2%} >= {HIT_RATE_FLOOR:.0%} {'ok' if ok[k] else 'MISSED'}"
            for k, rate in floors.items()
        ),
        *(f"        {k} {n:,} == 0 {'ok' if ok[k] else 'MISSED'}" for k, n in lru.items()),
    ]
    print("\n".join(lines))
    return 0 if all(ok.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
