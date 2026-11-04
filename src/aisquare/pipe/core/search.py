"""Lexical tool and connector search: an inverted index scored with BM25F.

A :class:`ToolIndex` indexes one connector's catalogue once; a search reads
only the postings of the query's terms, so its cost follows the query, not
the catalogue. A :class:`ConnectorIndex` does the same over connector cards,
for a router that picks connectors before tools. :func:`index_for` caches a
toolkit's index per (name, version, catalogue fingerprint), and
:func:`search_scored` / :func:`search_connectors` search the installed
toolkits. Pure Python, no dependencies, no network, no model.

**Analysis.** :func:`tokenize` lowercases; splits UPPER_SNAKE slugs,
camelCase and punctuation into words; drops :data:`STOPWORDS`; and stems
lightly (plurals, ``-ing``, ``-ed``, a final ``e``) with :func:`stem`.
:func:`analyze_query` turns each query word into a *concept*: the word itself
plus its :data:`SYNONYMS`, which count :data:`SYNONYM_WEIGHT` as much. A
first-person query ("my repos") adds ``authenticated`` (the
``..._FOR_THE_AUTHENTICATED_USER`` tools).

**Scoring.** BM25F over a tool's fields with :data:`FIELD_WEIGHTS`: the slug
words (highest; the toolkit prefix is left out), tags, description, and the
input schema's property names and descriptions (low). The connector's name,
display name, category and keywords are a low-weight field shared by all of
its tools. A concept scores its best alternative for a tool (the word, or
one synonym), never their sum, and a synonym never weighs more than the word
it stands for. Two small multiplicative boosts follow: :data:`PRIMARY_TAG`
on a tool, and read/write intent (a query with only write verbs favours tools
that are not ``read_only``; one with read verbs, or no verb at all, favours
``read_only`` ones). Ties keep connector order, then catalogue order.

Searching several connectors at once scores every tool with IDF over the
union of their catalogues. Length normalisation pivots on fixed field
lengths, so the same tool scores the same in any search and scores compare
across connectors.
"""

from __future__ import annotations

import functools
import hashlib
import heapq
import json
import logging
import math
import re
import threading
from array import array
from collections import OrderedDict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from aisquare.pipe.core.tools import Toolkit, ToolSpec

logger = logging.getLogger("aisquare.pipe")

__all__ = [
    "FIELD_WEIGHTS",
    "MARKER_PREFIX",
    "PRIMARY_TAG",
    "STOPWORDS",
    "SYNONYMS",
    "SYNONYM_WEIGHT",
    "ConnectorIndex",
    "QueryAnalysis",
    "ScoredTool",
    "ToolIndex",
    "analyze_query",
    "cache_clear",
    "cache_info",
    "cached_index",
    "index_for",
    "scoped_index",
    "search_connectors",
    "search_indexes",
    "search_scored",
    "stem",
    "tokenize",
    "toolkit_index",
]

# --------------------------------------------------------------------------
# Tunables
# --------------------------------------------------------------------------

#: Words dropped from queries and documents alike: articles, pronouns,
#: auxiliaries, prepositions and request fillers. Verbs that name tool
#: actions ("list", "send", "open", "new") are kept. First-person words are
#: here too; :func:`analyze_query` turns them into ``authenticated``.
STOPWORDS = frozenset(
    {
        "a", "about", "above", "after", "again", "against", "also", "am", "an", "and", "any",
        "are", "as", "at", "be", "been", "before", "being", "below", "between", "both", "but",
        "by", "can", "could", "d", "did", "do", "does", "doing", "done", "down", "during", "e",
        "each", "eg", "else", "etc", "ever", "for", "from", "further", "g", "had", "has", "have",
        "having", "he", "her", "here", "hers", "herself", "him", "himself", "his", "how", "i",
        "ie", "if", "in", "into", "is", "it", "its", "itself", "just", "let", "lets", "like", "ll",
        "m", "many", "may", "me", "might", "mine", "more", "most", "much", "must", "my", "myself",
        "no", "nor", "not", "now", "of", "off", "on", "once", "only", "or", "other", "our", "ours",
        "ourselves", "out", "over", "own", "per", "please", "re", "s", "same", "shall", "she",
        "should", "so", "some", "such", "t", "than", "that", "the", "their", "theirs", "them",
        "themselves", "then", "there", "these", "they", "this", "those", "through", "thus", "to",
        "too", "under", "until", "up", "upon", "us", "ve", "very", "via", "want", "was", "we",
        "were", "what", "whatever", "which", "while", "whom", "why", "will", "with", "within",
        "without", "would", "yet", "you", "your", "yours", "yourself", "yourselves"
    }
)

#: Query words that point at the connected user's own data ("my repos",
#: "issues assigned to me"): they add the term ``authenticated``, which the
#: GitHub-style ``..._FOR_THE_AUTHENTICATED_USER`` slugs carry.
FIRST_PERSON = frozenset({"i", "me", "my", "mine", "myself", "our", "ours", "ourselves"})
#: Weight of the ``authenticated`` term a first-person query adds.
FIRST_PERSON_WEIGHT = 0.6

#: Groups of interchangeable words and phrases. A query word (or phrase)
#: brings in the other members of every group it is in, each at
#: :data:`SYNONYM_WEIGHT`. Entries are analysed like any text (``"pull
#: request"`` -> ``pull``, ``request``). Documented in CONTRIBUTING.md;
#: extend it by what the search eval (``tests/evals``) shows.
SYNONYMS: tuple[tuple[str, ...], ...] = (
    # actions
    ("send", "post", "message", "notify", "ping", "dm", "tell", "say"),
    ("list", "get", "fetch", "show", "find", "read", "view", "see", "display", "browse"),
    ("create", "add", "new", "make", "open", "start"),
    ("delete", "remove", "trash", "archive", "erase", "discard", "bin"),
    ("update", "edit", "modify", "change", "rename", "fix", "amend"),
    ("search", "find", "query", "lookup", "locate"),
    ("reply", "respond", "answer"),
    ("upload", "attach", "put", "store"),
    ("download", "save", "export"),
    ("schedule", "book", "plan", "arrange"),
    ("move", "relocate", "reschedule"),
    ("cancel", "delete"),
    ("copy", "duplicate", "clone"),
    ("share", "grant", "invite", "access"),
    ("star", "favorite", "favourite", "bookmark"),
    ("draft", "compose", "write"),
    ("react", "reaction", "emoji"),
    ("restore", "undelete", "recover", "untrash"),
    ("mute", "snooze", "silence", "dnd", "do not disturb"),
    # objects
    ("file", "document", "doc", "attachment"),
    ("email", "mail", "inbox", "message"),
    ("event", "meeting", "calendar", "invite", "appointment"),
    ("repo", "repository", "project", "codebase"),
    ("issue", "ticket", "bug"),
    ("pr", "pull request", "merge request"),
    ("channel", "room"),
    ("folder", "directory"),
    ("user", "person", "people", "member", "colleague", "teammate", "coworker"),
    ("thread", "conversation", "replies"),
    ("comment", "note", "feedback", "remark"),
    ("spreadsheet", "sheet", "excel", "workbook"),
    ("free", "available", "availability"),
    ("profile", "account", "whoami"),
    ("reminder", "remind"),
    ("commit", "change", "diff"),
    ("release", "version"),
    ("workflow", "ci", "build", "pipeline"),
    ("link", "url", "permalink"),
    ("unread", "missed"),
    ("label", "tag", "category"),
    ("all", "every", "everything", "everyone"),
    ("recent", "latest", "today"),
    ("contents", "inside", "children"),
)

#: How much a synonym counts relative to the query word it stands for.
SYNONYM_WEIGHT = 0.15

#: The tag that marks a toolkit's primary tools (the one to pick when a
#: query is ambiguous); such a tool's score is raised by
#: :data:`PRIMARY_BOOST`. A ranking marker, never content: tags in the
#: ``pipe:`` namespace are not indexed as text (a plain ``important`` tag,
#: such as Gmail's IMPORTANT label, is ordinary content and gets no boost).
#: Hosts read this constant rather than spelling the string. Documented in
#: CONTRIBUTING.md.
PRIMARY_TAG = "pipe:primary"
PRIMARY_BOOST = 0.05
#: Tags with this prefix are markers for the ranker, not words to match.
MARKER_PREFIX = "pipe:"
#: How much a tool whose ``read_only`` flag matches the query's intent is raised.
INTENT_BOOST = 0.1

#: BM25F field weights. ``connector`` (its name, display name and slug
#: prefix) and ``card`` (its category and keywords) are shared by all of a
#: connector's tools: they matter when several connectors are searched at
#: once ("upload the report to OneDrive").
FIELD_WEIGHTS: dict[str, float] = {
    "connector": 1.0,
    "slug": 3.0,
    "tags": 2.0,
    "description": 1.0,
    "schema": 0.1,
    "card": 0.2,
}
_TOOL_FIELDS = ("slug", "tags", "description", "schema")
#: Length normalisation strength per field.
_FIELD_B = {"slug": 0.3, "tags": 0.3, "description": 0.75, "schema": 0.75}
#: The field lengths (in terms) that length normalisation pivots on. Fixed,
#: not each catalogue's average, so a tool scores the same in any catalogue
#: and scores compare across connectors (the six native toolkits average
#: about 3, 5, 30 and 50).
_FIELD_PIVOT = {"slug": 3.0, "tags": 5.0, "description": 30.0, "schema": 50.0}
#: How much a term counts toward document frequency when it is only in that
#: field: every input schema names its ``owner`` or ``page_token``, and those
#: mentions must not make the words common.
_FIELD_DF = {"slug": 1.0, "tags": 1.0, "description": 1.0, "schema": 0.25}
#: BM25 term-frequency saturation.
K1 = 1.6
#: The least IDF a concept gets (rank_bm25 floors IDF the same way), so a
#: word most tools of a toolkit share still prefers the tools named for it.
IDF_FLOOR = 0.5

# Verbs (as typed, not stemmed: "shared with me" is not a request to share)
# that make a query read or write.
_WRITE_VERBS = frozenset(
    {
        "add", "approve", "archive", "assign", "book", "bookmark", "cancel", "change", "close",
        "comment", "compose", "copy", "create", "delete", "disable", "discard", "dm", "draft",
        "duplicate", "edit", "enable", "erase", "favorite", "give", "grant", "invite", "join",
        "kick", "label", "leave", "make", "mark", "merge", "modify", "move", "mute", "notify",
        "overwrite", "pin", "ping", "post", "put", "react", "recover", "relocate", "remind",
        "remove", "rename", "replace", "reply", "reschedule", "respond", "restore", "revoke",
        "schedule", "send", "set", "share", "snooze", "star", "tell", "trash", "turn", "unarchive",
        "undelete", "unpin", "untrash", "update", "upload", "write"
    }
)
# Nouns that are write verbs when they open the query ("email Bob the
# numbers", "file a bug"), not elsewhere ("the email from Bob").
_IMPERATIVE_NOUNS = frozenset({"email", "mail", "message", "text", "file", "call", "invite"})
# Verbs that may read or write ("open a PR" / "open the email"): no intent.
_AMBIGUOUS_VERBS = frozenset({"open", "close", "do"})
_READ_VERBS = frozenset(
    {
        "browse", "check", "display", "download", "export", "fetch", "find", "get", "list", "look",
        "lookup", "read", "search", "see", "show", "view", "when", "where", "who"
    }
)

# --------------------------------------------------------------------------
# Analysis
# --------------------------------------------------------------------------

_CAMEL_LOWER_UPPER = re.compile(r"([a-z0-9])([A-Z])")
_CAMEL_ACRONYM = re.compile(r"([A-Z]+)([A-Z][a-z])")
_RUN = re.compile(r"[A-Za-z0-9]+")
_VOWEL = re.compile(r"[aeiouy]")
_UNDOUBLE = frozenset("bdfgmnprt")


@functools.lru_cache(maxsize=65536)
def stem(word: str) -> str:
    """A light, deterministic stemmer: plurals (``-ies``, ``-es``, ``-s``),
    ``-ied``/``-ing``/``-ed`` (undoubling ``pinned`` -> ``pin``) and a final
    ``e``, so ``schedule``, ``scheduled``, ``schedules`` and ``scheduling``
    all give ``schedul``. Three-letter words only lose a plural ``s``
    (``prs`` -> ``pr``); shorter words and numbers are kept."""
    w = word
    if len(w) <= 2 or w.isdigit():
        return w
    if len(w) == 3:  # "prs" -> "pr", "dms" -> "dm"; "bus", "gas" stay
        return w[:2] if w.endswith("s") and w[1] not in "aeiousy" else w
    if w.endswith("ies") and len(w) > 4:
        w = w[:-3] + "y"
    elif w.endswith(("sses", "xes", "zes", "ches", "shes")):
        w = w[:-2]
    elif w.endswith("s") and not w.endswith(("ss", "us", "is")):
        w = w[:-1]
    if w.endswith("ied") and len(w) > 4:
        w = w[:-3] + "y"
    elif w.endswith("ing") and len(w) > 5 and _VOWEL.search(w[:-3]):
        w = _undouble(w[:-3])
    elif w.endswith("ed") and len(w) > 4 and _VOWEL.search(w[:-2]):
        w = _undouble(w[:-2])
    if w.endswith("e") and len(w) > 3 and not w.endswith("ee"):
        w = w[:-1]
    return w


def _undouble(w: str) -> str:
    if len(w) > 2 and w[-1] == w[-2] and w[-1] in _UNDOUBLE:
        return w[:-1]
    return w


def _words(text: str) -> list[str]:
    """Lowercase words of ``text``: split at UPPER_SNAKE underscores and
    punctuation. A camelCase word gives the whole word, then its parts
    (``OneDrive`` -> ``onedrive``, ``one``, ``drive``), so a brand typed
    as one word still meets its parts and the other way round."""
    words: list[str] = []
    for run in _RUN.findall(text):
        if run.islower() or run.isupper() or run[1:].islower() or run.isdigit():
            words.append(run.lower())  # no camelCase boundary: the common case
            continue
        split = _CAMEL_ACRONYM.sub(r"\1 \2", _CAMEL_LOWER_UPPER.sub(r"\1 \2", run))
        parts = split.lower().split()
        if len(parts) > 1:
            words.append(run.lower())
        words.extend(parts)
    return words


def tokenize(text: str) -> list[str]:
    """Index terms of ``text`` in order: its words (UPPER_SNAKE, camelCase
    and punctuation split, lowercased) minus :data:`STOPWORDS` and one-letter
    words, stemmed with :func:`stem`."""
    return [stem(w) for w in _words(text) if len(w) > 1 and w not in STOPWORDS]


Phrase = tuple[str, ...]


@functools.cache
def _synonym_table() -> tuple[dict[Phrase, tuple[Phrase, ...]], int]:
    """Each synonym phrase -> the other phrases of its groups, and the
    longest phrase length."""
    groups = [[p for p in (tuple(tokenize(e)) for e in group) if p] for group in SYNONYMS]
    others: dict[Phrase, dict[Phrase, None]] = {}
    for group in groups:
        for phrase in group:
            slot = others.setdefault(phrase, {})
            slot.update(dict.fromkeys(p for p in group if p != phrase))
    longest = max((len(p) for p in others), default=1)
    return {p: tuple(alts) for p, alts in others.items()}, longest


@dataclass(frozen=True)
class Concept:
    """One query word (or multi-word synonym phrase) and what may stand for
    it in a tool. ``alternatives`` are ``(phrase, weight)``: the word itself
    at 1.0, its synonyms at :data:`SYNONYM_WEIGHT`. They score as weighted
    occurrences of one term (Lucene's SynonymQuery): their term frequencies
    add up before BM25 saturates them, and the concept's document frequency
    is theirs, weighted. ``weight`` scales the concept's score."""

    source: Phrase
    alternatives: tuple[tuple[Phrase, float], ...]
    weight: float = 1.0


@dataclass(frozen=True)
class QueryAnalysis:
    """What :func:`analyze_query` makes of a query: its ``concepts`` and its
    ``intent`` (``"read"``, ``"write"`` or None)."""

    concepts: tuple[Concept, ...]
    intent: str | None

    @property
    def terms(self) -> frozenset[str]:
        """Every term any concept may match."""
        return frozenset(t for c in self.concepts for p, _ in c.alternatives for t in p)


@functools.lru_cache(maxsize=4096)
def analyze_query(query: str) -> QueryAnalysis:
    """The concepts and read/write intent of ``query``: one concept per
    distinct query term with its synonyms, one per multi-word synonym phrase
    in the query ("pull request" -> ``pr``), and ``authenticated`` (weighted
    :data:`FIRST_PERSON_WEIGHT`) for first-person phrasing. Intent is
    ``"write"`` when the query has a write verb and no read verb, ``"read"``
    when it has a read verb and no write verb, or no verb at all (a noun
    phrase such as "my unread emails" asks to see something), else None."""
    raw = _words(query)
    tokens = tokenize(query)
    table, longest = _synonym_table()
    concepts: dict[Phrase, Concept] = {}
    for term in dict.fromkeys(tokens):
        source = (term,)
        alternatives: list[tuple[Phrase, float]] = [(source, 1.0)]
        alternatives += [(p, SYNONYM_WEIGHT) for p in table.get(source, ())]
        concepts[source] = Concept(source, tuple(alternatives))
    for n in range(2, longest + 1):
        for i in range(len(tokens) - n + 1):
            phrase = tuple(tokens[i : i + n])
            if phrase in table and phrase not in concepts:
                alternatives = [(p, SYNONYM_WEIGHT) for p in table[phrase]]
                concepts[phrase] = Concept(phrase, tuple(alternatives))
    if any(w in FIRST_PERSON for w in raw):
        source = (_AUTHENTICATED,)
        concepts.setdefault(source, Concept(source, ((source, 1.0),), FIRST_PERSON_WEIGHT))
    writes = any(w in _WRITE_VERBS for w in raw) or bool(raw and raw[0] in _IMPERATIVE_NOUNS)
    reads = any(w in _READ_VERBS for w in raw)
    if writes != reads:
        intent: str | None = "write" if writes else "read"
    elif not writes and not any(w in _AMBIGUOUS_VERBS for w in raw):
        intent = "read"
    else:
        intent = None
    return QueryAnalysis(tuple(concepts.values()), intent)


_AUTHENTICATED = stem("authenticated")


def _schema_text(schema: Any) -> str:
    """Property names and descriptions of a JSON schema (two levels deep)."""
    parts: list[str] = []

    def walk(node: Any, depth: int) -> None:
        if not isinstance(node, Mapping) or depth > 2:
            return
        props = node.get("properties")
        if not isinstance(props, Mapping):
            return
        for name, sub in props.items():
            parts.append(str(name))
            if isinstance(sub, Mapping):
                desc = sub.get("description")
                if isinstance(desc, str):
                    parts.append(desc)
                walk(sub, depth + 1)
                items = sub.get("items")
                if isinstance(items, Mapping):
                    walk(items, depth + 1)

    walk(schema, 0)
    return " ".join(parts)


def _card_words(card: Mapping[str, Any] | None) -> str:
    """The card fields a tool index uses: name, display name, category, keywords."""
    if not card:
        return ""
    keywords = card.get("keywords") or ()
    if isinstance(keywords, str):
        keywords = (keywords,)
    values = (card.get("name"), card.get("display_name"), card.get("category"), *keywords)
    return " ".join(str(v) for v in values if v)


def _slug_words(slug: str) -> list[str]:
    return [w for w in slug.lower().split("_") if w]


def _prefix_len(connector: str, slugs: Sequence[str]) -> int:
    """How many leading slug words are the toolkit prefix: the words that
    spell the connector's name (``ONE_DRIVE_`` for ``onedrive``), else the
    first word when every slug shares it."""
    if not slugs:
        return 0
    first = _slug_words(slugs[0])
    target = re.sub(r"[^a-z0-9]", "", connector.lower())
    if target:
        spelled, n = "", 0
        for word in first:
            if not target.startswith(spelled + word):
                break
            spelled, n = spelled + word, n + 1
            if spelled == target:
                break
        if (
            n
            and spelled == target
            and n < len(first)
            and all(_slug_words(s)[:n] == first[:n] for s in slugs)
        ):
            return n
    if len(slugs) > 1 and len(first) > 1 and all(_slug_words(s)[:1] == first[:1] for s in slugs):
        return 1
    return 0


# --------------------------------------------------------------------------
# BM25F core
# --------------------------------------------------------------------------


def _idf(n: float, df: float) -> float:
    return math.log(1.0 + (n - df + 0.5) / (df + 0.5))


def _sat(x: float) -> float:
    return x / (K1 + x)


class _Postings:
    """An inverted index over documents that are tuples of per-field token
    lists: term -> (doc ids, length-normalised weighted term frequency), and
    each term's (field-weighted) document frequency."""

    __slots__ = ("df", "n", "postings")

    def __init__(
        self,
        docs: Sequence[Sequence[Sequence[str]]],
        weights: Sequence[float],
        bs: Sequence[float],
        *,
        pivots: Sequence[float] | None = None,
        df_weights: Sequence[float] | None = None,
    ) -> None:
        n = len(docs)
        nfields = len(weights)
        if pivots is None:  # the collection's average field lengths
            pivots = [
                (sum(len(doc[f]) for doc in docs) / n if n else 0.0) or 1.0 for f in range(nfields)
            ]
        dfw = list(df_weights) if df_weights is not None else [1.0] * nfields
        tfn: dict[str, dict[int, float]] = {}
        df: dict[str, float] = {}
        for d, doc in enumerate(docs):
            presence: dict[str, float] = {}
            for f in range(nfields):
                tokens = doc[f]
                if not tokens:
                    continue
                k = weights[f] / (1.0 - bs[f] + bs[f] * len(tokens) / pivots[f])
                counts: dict[str, int] = {}
                for term in tokens:
                    counts[term] = counts.get(term, 0) + 1
                for term, tf in counts.items():
                    slot = tfn.get(term)
                    if slot is None:
                        tfn[term] = {d: tf * k}
                    else:
                        slot[d] = slot.get(d, 0.0) + tf * k
                    if presence.get(term, 0.0) < dfw[f]:
                        presence[term] = dfw[f]
            for term, weight in presence.items():
                df[term] = df.get(term, 0.0) + weight
        self.n = n
        self.df = df
        self.postings: dict[str, tuple[array[int], array[float]]] = {
            term: (array("i", slot.keys()), array("d", slot.values())) for term, slot in tfn.items()
        }


def _concept_idf(concept: Concept, n: float, df: Mapping[str, float]) -> float:
    """IDF of a concept: its alternatives' document frequencies, weighted
    (a phrase is as frequent as its rarest word), floored at
    :data:`IDF_FLOOR` so a toolkit's most common words still rank."""
    freq = sum(w * min(df.get(t, 0.0) for t in phrase) for phrase, w in concept.alternatives)
    return max(_idf(n, min(freq, n)), IDF_FLOOR)


def _score_concepts(
    postings: _Postings,
    concepts: Sequence[Concept],
    idfs: Sequence[float],
    shared: Mapping[str, float] | None = None,
) -> tuple[dict[int, float], float]:
    """BM25F of each concept per doc. ``shared`` maps the terms of fields
    every doc has (a tool index's connector and card fields) to their
    weight. Returns, for the docs in the concepts' postings, their score
    over ``base``, and ``base``: what every doc gets from ``shared`` alone."""
    shared = shared or {}
    acc: dict[int, float] = {}
    total_base = 0.0
    for concept, idf in zip(concepts, idfs):
        tf: dict[int, float] = {}
        tf_base = 0.0
        for phrase, weight in concept.alternatives:
            share = weight / len(phrase)
            for term in phrase:
                if term in shared:
                    tf_base += share * shared[term]
                hit = postings.postings.get(term)
                if hit is None:
                    continue
                get = tf.get
                for d, x in zip(*hit):
                    tf[d] = get(d, 0.0) + share * x
        scale = concept.weight * idf
        base = scale * _sat(tf_base) if tf_base else 0.0
        total_base += base
        get = acc.get
        for d, x in tf.items():
            acc[d] = get(d, 0.0) + scale * _sat(tf_base + x) - base
    return acc, total_base


# --------------------------------------------------------------------------
# Tools
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ScoredTool:
    """A search hit: the tool, its connector (toolkit name) and its score.
    Scores order the hits of one search; they are not probabilities."""

    connector: str
    tool: ToolSpec
    score: float

    def to_dict(self) -> dict[str, Any]:
        return {"connector": self.connector, "score": self.score, **self.tool.to_dict(metadata=True)}


class ToolIndex:
    """A BM25F index over one connector's tools. Build it with
    :meth:`build`; it never changes afterwards (a new catalogue is a new
    index; see :func:`index_for`). ``fingerprint`` identifies what was
    indexed: equal catalogues and cards give equal fingerprints."""

    __slots__ = (
        "_postings",
        "_primary",
        "_read_only",
        "_shared",
        "_slugs",
        "card",
        "connector",
        "fingerprint",
        "tools",
    )

    _postings: _Postings
    _primary: tuple[bool, ...]
    _read_only: tuple[bool, ...]
    _shared: dict[str, float]
    _slugs: tuple[str, ...]
    connector: str
    tools: tuple[ToolSpec, ...]
    card: dict[str, Any] | None
    fingerprint: str

    @classmethod
    def build(
        cls, connector: str, tools: Iterable[ToolSpec], card: Mapping[str, Any] | None = None
    ) -> ToolIndex:
        """Index ``tools`` (in catalogue order) for ``connector``. ``card``
        (a :meth:`Toolkit.card`, or any mapping with some of ``name``,
        ``display_name``, ``category`` and ``keywords``) is the
        connector-level field."""
        self = cls.__new__(cls)
        catalogue = tuple(tools)
        self.connector = connector
        self.tools = catalogue
        self.card = dict(card) if card is not None else None
        self.fingerprint = _fingerprint(connector, catalogue, card)
        slugs = tuple(t.slug for t in catalogue)
        cut = _prefix_len(connector, slugs)
        docs = [
            (
                tokenize(" ".join(_slug_words(t.slug)[cut:])),
                [
                    term
                    for tag in t.tags
                    if not tag.startswith(MARKER_PREFIX)
                    for term in tokenize(tag)
                ],
                tokenize(t.description),
                tokenize(_schema_text(t.input_schema)),
            )
            for t in catalogue
        ]
        self._postings = _Postings(
            docs,
            [FIELD_WEIGHTS[f] for f in _TOOL_FIELDS],
            [_FIELD_B[f] for f in _TOOL_FIELDS],
            pivots=[_FIELD_PIVOT[f] for f in _TOOL_FIELDS],
            df_weights=[_FIELD_DF[f] for f in _TOOL_FIELDS],
        )
        prefix = " ".join(_slug_words(slugs[0])[:cut]) if cut else ""
        # Shared by every tool: the card's category and keywords (low), and
        # the connector's own names, including its slug prefix (higher).
        shared = dict.fromkeys(tokenize(_card_words(card)), FIELD_WEIGHTS["card"])
        names = [(card or {}).get("name"), (card or {}).get("display_name"), connector, prefix]
        names_text = " ".join(str(v) for v in names if v)
        shared.update(dict.fromkeys(tokenize(names_text), FIELD_WEIGHTS["connector"]))
        self._shared = shared
        self._slugs = slugs
        self._primary = tuple(PRIMARY_TAG in t.tags for t in catalogue)
        self._read_only = tuple(bool(t.read_only) for t in catalogue)
        return self

    def __len__(self) -> int:
        return len(self.tools)

    def __repr__(self) -> str:
        return f"<ToolIndex {self.connector!r}: {len(self.tools)} tools, {self.fingerprint[:12]}>"

    def search(
        self,
        query: str | None,
        limit: int | None = 10,
        *,
        allowed: Iterable[str] | None = None,
    ) -> list[ScoredTool]:
        """The tools matching ``query``, best first; at most ``limit``
        (None: every match). Only tools that match at least one term are
        returned, so an empty or unmatched query gives ``[]``. ``allowed`` (a
        set of slugs, e.g. what a connection's granted scopes allow) filters
        the ranking without rebuilding the index."""
        return _search([(self, allowed)], query, limit)

    def df(self, term: str) -> float:
        """How many tools have ``term`` in their own fields (a schema-only
        mention counts a quarter). The shared card field does not count: it
        is in every tool, and counting it would take the IDF of a toolkit's
        own subject ("calendar" in Google Calendar) to 0."""
        return self._postings.df.get(term, 0.0)

    def _boost(self, d: int, intent: str | None) -> float:
        boost = 1.0 + PRIMARY_BOOST if self._primary[d] else 1.0
        if intent is not None and self._read_only[d] == (intent == "read"):
            boost *= 1.0 + INTENT_BOOST
        return boost


Entry = tuple[ToolIndex, Iterable[str] | None]


def _search(entries: Sequence[Entry], query: str | None, limit: int | None) -> list[ScoredTool]:
    """Search several indexes as one ranking: IDF over the union of their
    catalogues; ties in entry order, then catalogue order."""
    if (limit is not None and limit <= 0) or not query or not query.strip() or not entries:
        return []
    analysis = analyze_query(query)
    if not analysis.concepts:
        return []
    total = sum(len(index.tools) for index, _ in entries)
    df = {term: sum(index.df(term) for index, _ in entries) for term in analysis.terms}
    idfs = [_concept_idf(concept, total, df) for concept in analysis.concepts]
    ranked: list[tuple[float, int, int]] = []
    for e, (index, allowed) in enumerate(entries):
        acc, base = _score_concepts(index._postings, analysis.concepts, idfs, index._shared)
        keep = None if allowed is None else frozenset(allowed)
        slugs = index._slugs
        candidates: Iterable[int] = range(len(index.tools)) if base > 0.0 else acc
        for d in candidates:
            if keep is not None and slugs[d] not in keep:
                continue
            score = (acc.get(d, 0.0) + base) * index._boost(d, analysis.intent)
            if score > 0.0:
                ranked.append((-score, e, d))
    best = heapq.nsmallest(limit, ranked) if limit is not None else sorted(ranked)
    return [ScoredTool(entries[e][0].connector, entries[e][0].tools[d], -neg) for neg, e, d in best]


def search_indexes(
    indexes: Iterable[ToolIndex | Entry], query: str | None, limit: int | None = 10
) -> list[ScoredTool]:
    """Search several :class:`ToolIndex` es as one ranking (IDF over the
    union of their catalogues; ties keep index order, then catalogue order).
    Each item is an index, or ``(index, allowed)`` to filter that one by
    slug. Only matches are returned."""
    entries = [item if isinstance(item, tuple) else (item, None) for item in indexes]
    return _search(entries, query, limit)


# --------------------------------------------------------------------------
# Fingerprints and the index cache
# --------------------------------------------------------------------------

#: Most indexes (and catalogue digests) kept by the LRU cache behind
#: :func:`cached_index`, :func:`index_for` and the ranking helpers.
INDEX_CACHE_SIZE = 512

_lock = threading.Lock()
_indexes: OrderedDict[tuple[str, str, str], ToolIndex] = OrderedDict()
# Catalogue digests by ToolSpec identity, so a toolkit that hands out the
# same ToolSpec objects on each call is not re-hashed on each search. An
# entry holds its ToolSpecs, so their ids cannot be reused while it lives.
_digests: OrderedDict[tuple[int, ...], tuple[tuple[ToolSpec, ...], bytes]] = OrderedDict()
_counters = {"hits": 0, "misses": 0}


def _tool_digest(tool: ToolSpec) -> bytes:
    payload = json.dumps(
        [tool.slug, tool.description, list(tool.tags), tool.read_only, tool.destructive,
         tool.input_schema],
        sort_keys=True,
        default=str,
        separators=(",", ":"),
    )  # fmt: skip
    return hashlib.blake2b(payload.encode(), digest_size=16).digest()


def _catalogue_digest(tools: Sequence[ToolSpec]) -> bytes:
    key = tuple(map(id, tools))
    with _lock:
        hit = _digests.get(key)
        if hit is not None:
            _digests.move_to_end(key)
            return hit[1]
    h = hashlib.blake2b(digest_size=16)
    for tool in tools:
        h.update(_tool_digest(tool))
    digest = h.digest()
    with _lock:
        _digests[key] = (tuple(tools), digest)
        while len(_digests) > INDEX_CACHE_SIZE:
            _digests.popitem(last=False)
    return digest


def _fingerprint(connector: str, tools: Sequence[ToolSpec], card: Mapping[str, Any] | None) -> str:
    h = hashlib.blake2b(digest_size=16)
    h.update(json.dumps([connector, _card_words(card)]).encode())
    h.update(_catalogue_digest(tools))
    return h.hexdigest()


def cached_index(
    connector: str,
    tools: Sequence[ToolSpec],
    card: Mapping[str, Any] | None = None,
    *,
    version: str = "",
) -> ToolIndex:
    """The :class:`ToolIndex` of this catalogue from an LRU cache keyed on
    (connector, version, fingerprint), built on a miss. Any change to a
    tool's slug, description, tags, flags or schema, to the order of the
    tools, or to the card gives a new fingerprint, so a new index. A
    toolkit's ToolSpecs are frozen; a schema dict mutated in place is not
    noticed while the same ToolSpec objects are cached."""
    key = (connector, version, _fingerprint(connector, tools, card))
    with _lock:
        index = _indexes.get(key)
        if index is not None:
            _indexes.move_to_end(key)
            _counters["hits"] += 1
            return index
        _counters["misses"] += 1
    index = ToolIndex.build(connector, tools, card)
    with _lock:
        _indexes[key] = index
        while len(_indexes) > INDEX_CACHE_SIZE:
            _indexes.popitem(last=False)
    return index


def cache_info() -> dict[str, int]:
    """Index cache counters: ``hits``, ``misses`` and ``size``."""
    with _lock:
        return {**_counters, "size": len(_indexes)}


def cache_clear() -> None:
    """Drop every cached index and catalogue digest, and zero the counters."""
    with _lock:
        _indexes.clear()
        _digests.clear()
        _counters.update(hits=0, misses=0)


def _card_of(cls: type[Toolkit]) -> dict[str, Any] | None:
    try:
        return cls.card()
    except Exception as e:  # noqa: BLE001 — a broken card only loses the card field
        name = getattr(cls, "name", cls.__name__)
        logger.warning(f"Failed to read the card of toolkit {name}: {type(e).__name__}")
        return None


def toolkit_index(toolkit: Toolkit) -> ToolIndex:
    """The cached index of ``toolkit``'s whole catalogue (:meth:`Toolkit.tools`)
    with its card, keyed on the toolkit's name and version."""
    cls = type(toolkit)
    return cached_index(
        str(getattr(cls, "name", "")),
        toolkit.tools(),
        _card_of(cls),
        version=str(getattr(cls, "version", "")),
    )


def scoped_index(
    toolkit: Toolkit, available: Sequence[ToolSpec]
) -> tuple[ToolIndex, frozenset[str] | None]:
    """The index to search ``available`` (a subset of ``toolkit``'s
    catalogue, such as its :meth:`Toolkit.available_tools`) and the slugs to
    keep: the whole catalogue's cached index, filtered by slug (None when
    ``available`` is the whole catalogue). When ``available`` holds ToolSpecs
    the catalogue does not, an index of ``available`` itself, unfiltered."""
    index = toolkit_index(toolkit)
    by_slug = {tool.slug: tool for tool in index.tools}
    if all(by_slug.get(tool.slug) is tool for tool in available):
        if len(available) == len(index.tools):
            return index, None
        return index, frozenset(tool.slug for tool in available)
    cls = type(toolkit)
    scoped = cached_index(
        str(getattr(cls, "name", "")),
        available,
        _card_of(cls),
        version=str(getattr(cls, "version", "")),
    )
    return scoped, None


def index_for(toolkit_name: str) -> ToolIndex:
    """The index of an installed toolkit's whole catalogue, cached per
    (name, version, catalogue fingerprint), so it is built once and rebuilt
    only when the catalogue changes. Raises
    :class:`~aisquare.pipe.errors.ToolkitNotFoundError` for a toolkit that is
    not installed (see :func:`~aisquare.pipe.core.registry.installed_toolkits`)."""
    from aisquare.pipe.core.registry import _require_installed

    return toolkit_index(_require_installed([toolkit_name])[toolkit_name]())


def search_scored(
    query: str | None,
    connectors: Mapping[str, Mapping[str, Any] | None] | Iterable[str] | None = None,
    *,
    limit: int | None = 10,
) -> list[ScoredTool]:
    """The installed tools matching ``query`` across ``connectors``, best
    first, with scores. ``connectors`` takes the forms of
    :func:`~aisquare.pipe.core.registry.search_tools_across`: a mapping of
    name -> the connection's ``config`` (each toolkit then offers only its
    :meth:`Toolkit.available_tools` for it), an iterable of names, or None
    for every installed toolkit; an unknown name raises
    :class:`~aisquare.pipe.errors.ToolkitNotFoundError`. Only matches are
    returned: unlike ``search_tools_across`` there is no fallback."""
    from aisquare.pipe.core.registry import _searchable

    return _search([(index, allowed) for _, index, allowed, _ in _searchable(connectors)], query, limit)


# --------------------------------------------------------------------------
# Connectors
# --------------------------------------------------------------------------

#: ConnectorIndex fields: (card keys, weight, b). The first field also
#: holds the connector's name.
_CARD_FIELDS: tuple[tuple[tuple[str, ...], float, float], ...] = (
    (("display_name",), 3.0, 0.0),
    (("keywords",), 2.0, 0.3),
    (("category",), 1.5, 0.0),
    (("description",), 1.0, 0.75),
    (("examples",), 0.6, 0.5),
)


def _values(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, Iterable):
        return [str(v) for v in value]
    return [str(value)]


class ConnectorIndex:
    """A BM25F index over connector cards (:meth:`Toolkit.card`), for
    picking the connectors to search before their tools. Fields, best
    first: name and display name, keywords, category, description,
    examples."""

    __slots__ = ("_postings", "names")

    _postings: _Postings
    names: tuple[str, ...]

    @classmethod
    def build(
        cls, cards: Mapping[str, Mapping[str, Any]] | Iterable[Mapping[str, Any]]
    ) -> ConnectorIndex:
        """Index ``cards``: a mapping of name -> card, or cards that carry
        their ``name``. Their order breaks ties."""
        items = (
            list(cards.items())
            if isinstance(cards, Mapping)
            else [(str(card.get("name", "")), card) for card in cards]
        )
        self = cls.__new__(cls)
        self.names = tuple(name for name, _ in items)
        docs = []
        for name, card in items:
            fields = [
                [term for key in keys for value in _values(card.get(key)) for term in tokenize(value)]
                for keys, _, _ in _CARD_FIELDS
            ]
            # The name, and "Google Drive" typed as one word ("googledrive").
            joined = re.sub(r"[^a-z0-9]", "", str(card.get("display_name") or "").lower())
            fields[0] += tokenize(f"{name} {joined}")
            docs.append(fields)
        self._postings = _Postings(
            docs, [w for _, w, _ in _CARD_FIELDS], [b for _, _, b in _CARD_FIELDS]
        )
        return self

    def __len__(self) -> int:
        return len(self.names)

    def search(self, query: str | None, limit: int | None = 10) -> list[tuple[str, float]]:
        """``(name, score)`` of the connectors matching ``query``, best
        first, at most ``limit`` (None: all of them); ties keep card order.
        An empty or unmatched query gives ``[]``."""
        if (limit is not None and limit <= 0) or not query or not query.strip():
            return []
        analysis = analyze_query(query)
        idfs = [_concept_idf(c, self._postings.n, self._postings.df) for c in analysis.concepts]
        acc, _ = _score_concepts(self._postings, analysis.concepts, idfs)
        ranked = [(-score, d) for d, score in acc.items() if score > 0.0]
        best = heapq.nsmallest(limit, ranked) if limit is not None else sorted(ranked)
        return [(self.names[d], -neg) for neg, d in best]


_connector_index: tuple[str, ConnectorIndex] | None = None


def search_connectors(query: str | None, limit: int | None = 10) -> list[tuple[str, float]]:
    """:meth:`ConnectorIndex.search` over the installed toolkits' cards
    (:func:`~aisquare.pipe.core.registry.catalogue`); the index is rebuilt
    only when the cards change."""
    global _connector_index
    from aisquare.pipe.core.registry import catalogue

    cards = catalogue()
    key = hashlib.blake2b(
        json.dumps(cards, sort_keys=True, default=str).encode(), digest_size=16
    ).hexdigest()
    current = _connector_index
    if current is None or current[0] != key:
        current = (key, ConnectorIndex.build(cards))
        _connector_index = current
    return current[1].search(query, limit)
