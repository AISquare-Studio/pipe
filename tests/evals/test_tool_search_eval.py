"""Relevance eval: the BM25F index (aisquare.pipe.core.search) against the
0.4 keyword ranker it replaced, on labelled queries over the six native
toolkits (tool_search_cases.yaml).

Hermetic: the toolkits' catalogues are read from the installed packages
(``tools()`` and ``card()`` never touch the network). Skipped when one of
the six is not installed. Run with ``-s`` to see the table::

    pytest tests/evals -s
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest

from aisquare.pipe import QualifiedTool, ToolSpec, installed_toolkits
from aisquare.pipe.core.search import STOPWORDS, ToolIndex, search_indexes, stem

yaml = pytest.importorskip("yaml")

NATIVE = ("github", "gmail", "googlecalendar", "googledrive", "onedrive", "slack")
KINDS = {"paraphrase", "first_person", "write", "multi"}
CASES: list[dict[str, Any]] = yaml.safe_load(
    (Path(__file__).parent / "tool_search_cases.yaml").read_text()
)
K = 10


# --------------------------------------------------------------------------
# The 0.4 ranker, frozen (core/tools.py before the search index)
# --------------------------------------------------------------------------

OLD_STOPWORDS = frozenset(
    {
        "the", "and", "for", "with", "that", "this", "what", "are", "you",
        "your", "from", "my", "me", "of", "to", "in", "on", "up", "by", "do",
        "an", "is", "how", "many", "most", "their", "have", "has", "out",
        "any", "all",
    }
)  # fmt: skip
OLD_FIRST_PERSON = frozenset({"my", "me", "mine", "myself", "our", "ours", "i"})


def old_query_terms(text: str) -> set[str]:
    raw = re.split(r"[^a-z0-9]+", text.lower())
    words = {w for w in raw if len(w) > 2 and w not in OLD_STOPWORDS}
    terms = words | {w.rstrip("s") for w in words}
    if any(w in OLD_FIRST_PERSON for w in raw):
        terms.add("authenticated")
    return terms


def old_score(tool: ToolSpec, terms: set[str]) -> int:
    slug = tool.slug.lower()
    text = " ".join((tool.description, *tool.tags)).lower()
    return sum(2 for t in terms if t in slug) + sum(1 for t in terms if t in text)


def old_rank_tools(tools: Sequence[ToolSpec], query: str, limit: int) -> list[ToolSpec]:
    terms = old_query_terms(query)
    scored = sorted(((old_score(t, terms), t) for t in tools), key=lambda p: p[0], reverse=True)
    matched = [t for score, t in scored if score > 0]
    return (matched or [t for _, t in scored])[:limit]


def old_rank_across(
    catalogs: Mapping[str, Sequence[ToolSpec]],
    query: str,
    limit: int,
    keywords: Mapping[str, Sequence[str]],
) -> list[QualifiedTool]:
    terms = old_query_terms(query)
    boost = {
        name: sum(1 for t in terms if t in " ".join(words).lower())
        for name, words in keywords.items()
    }
    scored = [
        (old_score(tool, terms) + boost.get(name, 0), QualifiedTool(name, tool))
        for name, tools in catalogs.items()
        for tool in tools
    ]
    scored.sort(key=lambda pair: pair[0], reverse=True)
    matched = [q for score, q in scored if score > 0]
    if matched:
        return matched[:limit]
    columns = [[QualifiedTool(n, t) for t in tools] for n, tools in catalogs.items()]
    turns: list[QualifiedTool] = []
    for row in range(max((len(c) for c in columns), default=0)):
        turns.extend(column[row] for column in columns if row < len(column))
    return turns[:limit]


# --------------------------------------------------------------------------
# Corpus and rankers
# --------------------------------------------------------------------------


@pytest.fixture(scope="module")
def corpus() -> tuple[dict[str, list[ToolSpec]], dict[str, dict[str, Any]]]:
    installed = installed_toolkits()
    missing = [name for name in NATIVE if name not in installed]
    if missing:
        pytest.skip(f"native toolkits not installed: {', '.join(missing)}")
    tools = {name: list(installed[name]().tools()) for name in NATIVE}
    cards = {name: installed[name].card() for name in NATIVE}
    return tools, cards


def _keywords(card: Mapping[str, Any]) -> list[str]:
    # What search_tools_across passed rank_across as a connector's keywords.
    return [card["name"], str(card["display_name"]), str(card["category"]), *map(str, card["keywords"])]


def rank_old(case: dict[str, Any], tools: dict, cards: dict) -> list[str]:
    if case.get("across"):
        keywords = {name: _keywords(cards[name]) for name in NATIVE}
        return [q.tool.slug for q in old_rank_across(tools, case["query"], K, keywords)]
    return [t.slug for t in old_rank_tools(tools[case["connector"]], case["query"], K)]


def rank_new(case: dict[str, Any], indexes: dict[str, ToolIndex]) -> list[str]:
    if case.get("across"):
        hits = search_indexes([indexes[name] for name in NATIVE], case["query"], K)
    else:
        hits = indexes[case["connector"]].search(case["query"], K)
    return [hit.tool.slug for hit in hits]


def metrics(rankings: list[list[str]], cases: list[dict[str, Any]]) -> dict[str, float]:
    ranks = []
    for ranking, case in zip(rankings, cases):
        good = set(case["acceptable"])
        ranks.append(next((i + 1 for i, slug in enumerate(ranking) if slug in good), None))
    n = len(ranks)
    out = {f"recall@{k}": sum(1 for r in ranks if r is not None and r <= k) / n for k in (1, 5, 10)}
    out["MRR"] = sum(1 / r for r in ranks if r is not None) / n
    return out


def _stems(words: Sequence[str]) -> set[str]:
    return {stem(w) for w in words if len(w) > 1 and w not in STOPWORDS}


def shares_no_word(query: str, slugs: Sequence[str]) -> bool:
    """No query word (stemmed, stopwords aside) is a word of any slug."""
    query_words = _stems(re.findall(r"[a-z0-9]+", query.lower()))
    return all(query_words.isdisjoint(_stems(slug.lower().split("_"))) for slug in slugs)


# --------------------------------------------------------------------------
# The case file itself
# --------------------------------------------------------------------------


def test_case_file_is_well_formed(corpus):
    tools, _ = corpus
    assert len(CASES) >= 80
    seen = set()
    for case in CASES:
        assert set(case) <= {"query", "connector", "acceptable", "kinds", "across"}, case
        assert case["connector"] in NATIVE, case
        assert case.get("across") in (None, "all"), case
        assert set(case.get("kinds", [])) <= KINDS, case
        assert ("multi" in case.get("kinds", [])) == bool(case.get("across")), case
        slugs = {t.slug for t in tools[case["connector"]]}
        assert case["acceptable"] and set(case["acceptable"]) <= slugs, case
        key = (case["query"].lower(), case.get("across"))
        assert key not in seen, f"duplicate case: {case['query']}"
        seen.add(key)


def test_paraphrase_cases_share_no_word_with_their_slugs():
    wrong = [
        c["query"]
        for c in CASES
        if "paraphrase" in c.get("kinds", []) and not shares_no_word(c["query"], c["acceptable"])
    ]
    assert not wrong, f"tagged paraphrase but share a word with an acceptable slug: {wrong}"


def test_case_mix():
    count = {kind: sum(1 for c in CASES if kind in c.get("kinds", [])) for kind in KINDS}
    assert count["paraphrase"] / len(CASES) >= 0.30, count
    assert count["multi"] >= 10, count
    assert count["first_person"] >= 10, count
    assert count["write"] >= 20, count
    assert {c["connector"] for c in CASES} == set(NATIVE)


# --------------------------------------------------------------------------
# OLD vs NEW
# --------------------------------------------------------------------------


def test_new_ranker_beats_old(corpus):
    tools, cards = corpus
    indexes = {name: ToolIndex.build(name, tools[name], cards[name]) for name in NATIVE}
    old = [rank_old(case, tools, cards) for case in CASES]
    new = [rank_new(case, indexes) for case in CASES]
    m_old, m_new = metrics(old, CASES), metrics(new, CASES)

    lines = [f"\ntool search eval: {len(CASES)} cases over {', '.join(NATIVE)}"]
    lines.append(f"{'metric':<11}{'OLD':>8}{'NEW':>8}")
    lines += [f"{name:<11}{m_old[name]:>8.3f}{m_new[name]:>8.3f}" for name in m_new]
    lines.append(f"\n{'kind':<14}{'n':>4}{'OLD r@10':>10}{'NEW r@10':>10}{'OLD r@1':>9}{'NEW r@1':>9}")
    for kind in ["all", *sorted(KINDS)]:
        idx = [i for i, c in enumerate(CASES) if kind == "all" or kind in c.get("kinds", [])]
        sub = [CASES[i] for i in idx]
        mo, mn = metrics([old[i] for i in idx], sub), metrics([new[i] for i in idx], sub)
        lines.append(
            f"{kind:<14}{len(idx):>4}{mo['recall@10']:>10.3f}{mn['recall@10']:>10.3f}"
            f"{mo['recall@1']:>9.3f}{mn['recall@1']:>9.3f}"
        )
    for title, k in (("NEW misses @10", K), ("NEW misses @1", 1)):
        missed = [
            (case, ranking)
            for case, ranking in zip(CASES, new)
            if not set(case["acceptable"]) & set(ranking[:k])
        ]
        lines.append(f"\n{title}: {len(missed)}")
        lines += [
            f"  - {c['query']!r} [{c['connector']}{', all' if c.get('across') else ''}] "
            f"want {c['acceptable'][0]}{' (+)' if len(c['acceptable']) > 1 else ''}; "
            f"got {r[:3]}"
            for c, r in missed
        ]
    print("\n".join(lines))

    assert m_new["recall@10"] >= 0.90
    for name in m_new:
        assert m_new[name] >= m_old[name], (name, m_old[name], m_new[name])
