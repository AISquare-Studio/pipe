#!/usr/bin/env python
"""Benchmark the BM25F search index (aisquare.pipe.core.search) on synthetic
catalogues (aisquare.pipe.testing.synthetic):

  (a) one toolkit of 10,000 tools: index build time and memory, query
      latency p50/p95;
  (b) 1,000 connectors x 50 tools: ConnectorIndex build and query latency,
      building every per-toolkit index, and a hierarchical search (connector
      shortlist -> those toolkits' indexes) as a router runs it.

Budgets (the search task's acceptance): 10k build <= 2 s, 10k query p95 <=
20 ms, ConnectorIndex over 1,000 cards query p95 <= 10 ms. Exit status 1 when
a budget is missed.

    python scripts/bench_search.py            # the defaults above
    python scripts/bench_search.py --json     # machine-readable
"""

from __future__ import annotations

import argparse
import gc
import json
import platform
import random
import statistics
import sys
import time
import tracemalloc
from collections.abc import Callable
from typing import Any

from aisquare.pipe.core.search import ConnectorIndex, ToolIndex, search_indexes
from aisquare.pipe.testing.synthetic import synthetic_catalog

BUDGET = {"build_10k_s": 2.0, "query_10k_p95_ms": 20.0, "connectors_query_p95_ms": 10.0}

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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--tools", type=int, default=10_000, help="tools in the big toolkit")
    parser.add_argument("--connectors", type=int, default=1_000)
    parser.add_argument("--tools-each", type=int, default=50)
    parser.add_argument("--queries", type=int, default=500)
    parser.add_argument("--shortlist", type=int, default=8, help="connectors searched per query")
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--json", action="store_true", help="print JSON only")
    args = parser.parse_args(argv)

    big = bench_big_toolkit(args.tools, args.queries, args.seed)
    many = bench_many_connectors(args.connectors, args.tools_each, args.queries, args.shortlist, args.seed)
    checks = {
        "build_10k_s": big["build_s"],
        "query_10k_p95_ms": big["query"]["p95_ms"],
        "connectors_query_p95_ms": many["connector_index"]["query"]["p95_ms"],
    }
    ok = {k: checks[k] <= BUDGET[k] for k in BUDGET}
    report = {
        "python": platform.python_version(),
        "machine": f"{platform.system()} {platform.machine()} ({platform.processor() or 'n/a'})",
        "big_toolkit": big,
        "many_connectors": many,
        "budget": BUDGET,
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
        "",
        "budget: " + ", ".join(f"{k} {checks[k]:.3f} <= {BUDGET[k]} {'ok' if ok[k] else 'MISSED'}" for k in BUDGET),
    ]
    print("\n".join(lines))
    return 0 if all(ok.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
