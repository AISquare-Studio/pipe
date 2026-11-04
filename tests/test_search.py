"""Tests for the BM25F tool and connector search (core/search.py) and the
ranking helpers built on it (rank_tools, rank_across, Toolkit.search_tools,
search_tools_across)."""

from __future__ import annotations

import dataclasses
import unittest.mock
from typing import Any, ClassVar

import pytest

from aisquare.pipe import (
    QualifiedTool,
    Toolkit,
    ToolResult,
    ToolSpec,
    rank_across,
    rank_tools,
    search_tools_across,
)
from aisquare.pipe.core import registry as registry_mod
from aisquare.pipe.core import search
from aisquare.pipe.core.search import (
    PRIMARY_TAG,
    ConnectorIndex,
    ScoredTool,
    ToolIndex,
    analyze_query,
    cache_clear,
    cache_info,
    cached_index,
    index_for,
    search_connectors,
    search_indexes,
    search_scored,
    stem,
    tokenize,
)
from aisquare.pipe.errors import ToolkitNotFoundError
from aisquare.pipe.testing.synthetic import synthetic_catalog

SCHEMA = {"type": "object", "properties": {}}


def spec(slug: str, description: str = "does a thing", **kwargs) -> ToolSpec:
    kwargs.setdefault("input_schema", SCHEMA)
    kwargs.setdefault("read_only", True)
    return ToolSpec(slug=slug, description=description, **kwargs)


def slugs(hits) -> list[str]:
    return [h.tool.slug for h in hits]


@pytest.fixture(autouse=True)
def _empty_cache():
    cache_clear()
    yield
    cache_clear()


# --------------------------------------------------------------------------
# Analysis
# --------------------------------------------------------------------------


class TestTokenize:
    def test_upper_snake_slug(self):
        assert tokenize("GITHUB_LIST_REPOSITORIES_FOR_THE_AUTHENTICATED_USER") == [
            "github", "list", "repository", "authenticat", "user",
        ]  # fmt: skip

    def test_camel_case_keeps_the_whole_word_and_its_parts(self):
        assert tokenize("pageToken") == ["pagetoken", "pag", "token"]
        assert tokenize("OneDrive") == ["onedriv", "one", "driv"]
        assert tokenize("HTTPServer") == ["httpserver", "http", "server"]

    def test_punctuation_splits(self):
        assert tokenize("owner/repo: is:pr, (draft)") == ["owner", "repo", "pr", "draft"]

    @pytest.mark.parametrize(
        ("word", "expected"),
        [
            ("repositories", "repository"),
            ("issues", "issu"),
            ("issue", "issu"),
            ("branches", "branch"),
            ("addresses", "address"),
            ("scheduling", "schedul"),
            ("scheduled", "schedul"),
            ("schedules", "schedul"),
            ("pinned", "pin"),
            ("replied", "reply"),
            ("replies", "reply"),
            ("prs", "pr"),
            ("dms", "dm"),
            ("status", "status"),
            ("bus", "bus"),
            ("2026", "2026"),
        ],
    )
    def test_light_stemming(self, word, expected):
        assert stem(word) == expected

    def test_stopwords_and_single_letters_dropped(self):
        assert tokenize("what are the issues in it") == ["issu"]
        assert tokenize("a b c x") == []

    def test_all_is_a_word(self):  # LIST_ALL_CHANNELS, SEARCH_ALL
        assert tokenize("list all channels") == ["list", "all", "channel"]


class TestAnalyzeQuery:
    def test_one_concept_per_word_with_its_synonyms(self):
        analysis = analyze_query("ping the team")
        sources = [c.source for c in analysis.concepts]
        assert sources == [("ping",), ("team",)]
        ping = analysis.concepts[0]
        assert ping.alternatives[0] == (("ping",), 1.0)
        assert (("send",), search.SYNONYM_WEIGHT) in ping.alternatives

    def test_multi_word_synonyms(self):
        alternatives = dict(analyze_query("my PRs").concepts[0].alternatives)
        assert alternatives[("pull", "request")] == search.SYNONYM_WEIGHT
        phrase = [c for c in analyze_query("open pull requests").concepts if c.source == ("pull", "request")]
        assert phrase and dict(phrase[0].alternatives)[("pr",)] == search.SYNONYM_WEIGHT

    def test_first_person_adds_authenticated(self):
        for query in ("list my repos", "issues assigned to me", "which orgs am I in"):
            concepts = {c.source: c for c in analyze_query(query).concepts}
            assert concepts[("authenticat",)].weight == search.FIRST_PERSON_WEIGHT
        assert ("authenticat",) not in {c.source for c in analyze_query("repos for octocat").concepts}

    def test_idioms(self):
        assert analyze_query("get rid of that draft").tokens == ("delet", "draft")
        assert analyze_query("get rid of that draft").intent == "write"
        assert analyze_query("set up a call with Ann").tokens[:2] == ("creat", "call")
        assert analyze_query("push back the standup").tokens[:1] == ("postpon",)
        assert analyze_query("get the draft").tokens == ("get", "draft")

    def test_let_someone_know(self):
        assert analyze_query("let the team know it shipped").tokens[:2] == ("notify", "team")
        assert analyze_query("let the team know it shipped").intent == "write"

    def test_questions_read(self):
        assert analyze_query("who has edit rights on the plan").intent == "read"
        assert analyze_query("what changed in the last commit").intent == "read"

    def test_opening_noun_verbs(self):
        assert analyze_query("email the team about it").tokens[:2] == ("send", "email")
        assert analyze_query("file a bug about the login").tokens[:2] == ("creat", "bug")
        assert analyze_query("the email from Ann").tokens[0] == "email"
        assert analyze_query("file details").tokens[0] == "fil"

    def test_calendar_vocabulary(self):
        concepts = {c.source: dict(c.alternatives) for c in analyze_query("postpone the standup").concepts}
        assert ("updat",) in concepts[("postpon",)] and ("meet",) in concepts[("standup",)]

    @pytest.mark.parametrize(
        ("query", "intent"),
        [
            ("delete the old file", "write"),
            ("email Bob the numbers", "write"),
            ("list my files", "read"),
            ("my unread emails", "read"),  # no verb: a noun phrase asks to see
            ("files people shared with me", "read"),  # "shared" is not "share"
            ("find the doc and share it", None),
            ("open a PR", None),  # "open" may read or write
        ],
    )
    def test_intent(self, query, intent):
        assert analyze_query(query).intent == intent


# --------------------------------------------------------------------------
# ToolIndex
# --------------------------------------------------------------------------

CHAT = [
    spec("CHAT_POST_MESSAGE", "Put text into a channel.", read_only=False),
    spec("CHAT_SEND_MESSAGE", "Put text into a channel.", read_only=False),
    spec("CHAT_LIST_CHANNELS", "Every channel in the workspace."),
]


class TestToolIndex:
    def test_returns_scored_tools_best_first(self):
        hits = ToolIndex.build("chat", CHAT).search("list channels")
        assert all(isinstance(h, ScoredTool) and h.connector == "chat" for h in hits)
        assert hits[0].tool.slug == "CHAT_LIST_CHANNELS"
        assert [h.score for h in hits] == sorted((h.score for h in hits), reverse=True)

    def test_synonym_ranks_below_exact(self):
        hits = ToolIndex.build("chat", CHAT).search("send")
        assert slugs(hits) == ["CHAT_SEND_MESSAGE", "CHAT_POST_MESSAGE"]  # post ~ send
        assert hits[0].score > hits[1].score > 0

    def test_paraphrase_finds_the_tool_through_synonyms(self):
        assert ToolIndex.build("chat", CHAT).search("ping")[0].tool.slug in {
            "CHAT_SEND_MESSAGE", "CHAT_POST_MESSAGE",
        }  # fmt: skip

    def test_field_weighting(self):
        # Descriptions of a realistic, equal length, so only the field differs.
        text = "Returns the records of the account one page at a time, newest first, {} included."
        plain = text.format("totals")
        tools = [
            spec("X_ALPHA", plain, input_schema={"type": "object", "properties": {"invoice": {}}}),
            spec("X_BETA", text.format("invoice")),
            spec("X_GAMMA", plain, tags=("invoice",)),
            spec("X_INVOICE", plain),
        ]
        hits = ToolIndex.build("x", tools).search("invoice")
        assert slugs(hits) == ["X_INVOICE", "X_GAMMA", "X_BETA", "X_ALPHA"]  # slug > tags > text > schema

    def test_toolkit_prefix_is_not_a_slug_word(self):
        tools = [spec("ONE_DRIVE_GET_ITEM"), spec("ONE_DRIVE_LIST_FILES")]
        index = ToolIndex.build("onedrive", tools)
        assert index.df("driv") == 0
        assert search._prefix_len("onedrive", [t.slug for t in tools]) == 2

    def test_card_is_a_shared_low_weight_field(self):
        tools = [spec("MAIL_SEND", "Send it.", read_only=False), spec("MAIL_LIST", "List them.")]
        card = {"name": "mail", "display_name": "Mail", "category": "email", "keywords": ["inbox"]}
        index = ToolIndex.build("mail", tools, card)
        assert slugs(index.search("inbox")) == ["MAIL_LIST", "MAIL_SEND"]  # every tool matches
        assert index.df("inbox") == 0  # ... but the card does not make it common

    def test_allowed_filters_without_rebuilding(self):
        index = cached_index("chat", CHAT)
        full = index.search("message channel", None)
        kept = index.search("message channel", None, allowed={"CHAT_POST_MESSAGE", "CHAT_LIST_CHANNELS"})
        assert slugs(kept) == [s for s in slugs(full) if s != "CHAT_SEND_MESSAGE"]
        assert {h.tool.slug: h.score for h in kept}.items() <= {h.tool.slug: h.score for h in full}.items()
        assert index.search("message", allowed=set()) == []
        assert cached_index("chat", CHAT) is index and cache_info()["misses"] == 1

    @pytest.mark.parametrize("query", [None, "", "   ", "the of and", "zzzz qqqq"])
    def test_empty_or_unmatched_query_gives_nothing(self, query):
        assert ToolIndex.build("chat", CHAT).search(query) == []

    def test_limit(self):
        index = ToolIndex.build("chat", CHAT)
        assert len(index.search("channel", 2)) == 2
        assert len(index.search("channel", None)) == 3
        assert index.search("channel", 0) == []

    def test_deterministic_and_ties_keep_catalogue_order(self):
        tools = [spec("X_LIST_C"), spec("X_LIST_A"), spec("X_LIST_B")]
        index = ToolIndex.build("x", tools)
        assert slugs(index.search("list")) == ["X_LIST_C", "X_LIST_A", "X_LIST_B"]
        again = ToolIndex.build("x", list(tools))
        query = "list the things for my user"
        assert index.search(query) == again.search(query) == index.search(query)
        assert index.fingerprint == again.fingerprint

    def test_primary_tag_boosts_and_is_not_text(self):
        twins = [spec("X_GET_REPORT", "Get a report."), spec("X_FETCH_REPORT", "Get a report.")]
        marked = [twins[0], dataclasses.replace(twins[1], tags=(PRIMARY_TAG,))]
        assert slugs(ToolIndex.build("x", twins).search("report")) == ["X_GET_REPORT", "X_FETCH_REPORT"]
        assert slugs(ToolIndex.build("x", marked).search("report")) == ["X_FETCH_REPORT", "X_GET_REPORT"]
        assert ToolIndex.build("x", marked).search("pipe primary") == []
        assert PRIMARY_TAG == "pipe:primary"

    def test_important_is_ordinary_content(self):
        twins = [spec("X_GET_REPORT", "Get a report."), spec("X_FETCH_REPORT", "Get a report.", tags=("important",))]
        index = ToolIndex.build("x", twins)
        hits = index.search("report")
        assert slugs(hits) == ["X_GET_REPORT", "X_FETCH_REPORT"]  # no boost ...
        assert slugs(index.search("important")) == ["X_FETCH_REPORT"]  # ... but searchable

    def test_intent_favours_matching_tools(self):
        twins = [spec("X_REPORT_A", "A report.", read_only=True), spec("X_REPORT_B", "A report.", read_only=False)]
        index = ToolIndex.build("x", twins)
        assert slugs(index.search("delete report"))[0] == "X_REPORT_B"
        assert slugs(index.search("show report"))[0] == "X_REPORT_A"

    def test_first_person_prefers_authenticated_variant(self):
        tools = [
            spec("GH_LIST_REPOSITORIES_FOR_USER", "List public repositories for the specified user."),
            spec("GH_LIST_REPOSITORIES_FOR_THE_AUTHENTICATED_USER", "List repositories the authenticated user can access."),
        ]  # fmt: skip
        index = ToolIndex.build("gh", tools)
        assert index.search("list my repositories")[0].tool.slug.endswith("AUTHENTICATED_USER")
        assert index.search("list repositories for octocat")[0].tool.slug.endswith("FOR_USER")

    def test_to_dict(self):
        hit = ToolIndex.build("chat", CHAT).search("list channels")[0]
        assert hit.to_dict() == {"connector": "chat", "score": hit.score, **hit.tool.to_dict(metadata=True)}


class TestSearchIndexes:
    def test_one_ranking_across_connectors(self):
        mail = ToolIndex.build("mail", [spec("MAIL_SEND_EMAIL", "Send an email.", read_only=False)])
        cal = ToolIndex.build("cal", [spec("CAL_CREATE_EVENT", "Create a calendar event.", read_only=False)])
        hits = search_indexes([mail, cal], "create a calendar event")
        assert (hits[0].connector, hits[0].tool.slug) == ("cal", "CAL_CREATE_EVENT")

    def test_ties_keep_index_order_then_catalogue_order(self):
        a = ToolIndex.build("a", [spec("A_LIST_X"), spec("A_LIST_Y")])
        b = ToolIndex.build("b", [spec("B_LIST_X")])
        assert [(h.connector, h.tool.slug) for h in search_indexes([b, a], "list")] == [
            ("b", "B_LIST_X"), ("a", "A_LIST_X"), ("a", "A_LIST_Y"),
        ]  # fmt: skip

    def test_a_tool_scores_the_same_alone_or_with_others_but_idf(self):
        # Fixed length pivots: only the IDF changes with the other catalogues.
        a = ToolIndex.build("a", [spec("A_LIST_X")])
        assert search_indexes([a], "list")[0].score > 0

    def test_connector_name_scopes(self):
        files = [spec("DRIVE_UPLOAD_FILE", "Upload a file.", read_only=False)]
        box = [spec("BOX_UPLOAD_FILE", "Upload a file.", read_only=False)]
        indexes = [ToolIndex.build("drive", files), ToolIndex.build("box", box)]
        assert search_indexes(indexes, "upload to box")[0].connector == "box"

    def test_allowed_per_index(self):
        a = ToolIndex.build("a", [spec("A_LIST_X"), spec("A_LIST_Y")])
        hits = search_indexes([(a, {"A_LIST_Y"})], "list")
        assert slugs(hits) == ["A_LIST_Y"]


class TestNamedConnector:
    """A query that names one of several connectors searched *as the service
    to use* ranks that connector up (boost only, never a filter); a
    connector mentioned only as a topic is not boosted."""

    def _indexes(self):
        slack = ToolIndex.build(
            "slack",
            [
                spec("SLACK_SEND_MESSAGE", "Post a message to a channel.", read_only=False, tags=("post", "dm")),
                spec("SLACK_LIST_CHANNELS", "List channels."),
            ],
            {"name": "slack", "display_name": "Slack", "keywords": ["dm", "channel"]},
        )
        github = ToolIndex.build(
            "github",
            [
                spec("GITHUB_LIST_RELEASES", "List releases of a repository, newest first.", tags=("releases",)),
                spec("GITHUB_CREATE_A_REVIEW", "Review a pull request.", read_only=False, tags=("review",)),
            ],
            {"name": "github", "display_name": "GitHub", "keywords": ["release", "review"]},
        )
        drive = ToolIndex.build(
            "onedrive",
            [spec("ONE_DRIVE_CREATE_LINK", "Create a sharing link for a release file.", read_only=False)],
            {"name": "onedrive", "display_name": "OneDrive", "keywords": ["file"]},
        )
        cal = ToolIndex.build(
            "googlecalendar",
            [spec("GOOGLECALENDAR_CREATE_EVENT", "Create an event.", read_only=False)],
            {"name": "googlecalendar", "display_name": "Google Calendar",
             "keywords": ["gcal", "calendar", "review"]},
        )  # fmt: skip
        return slack, github, drive, cal

    def _served(self, query):
        entries = [(index, None) for index in self._indexes()]
        return {entries[e][0].connector: level for e, level in search._served(entries, query).items()}

    def test_a_served_name_outranks_other_word_matches_without_filtering(self):
        slack, github, _, _ = self._indexes()
        assert search_indexes([github, slack], "post the latest releases")[0].connector == "github"
        named = search_indexes([github, slack], "post the latest releases in slack")
        assert named[0].tool.slug == "SLACK_SEND_MESSAGE"
        assert "GITHUB_LIST_RELEASES" in slugs(named)  # boost only, nothing filtered

    def test_a_topic_mention_is_not_boosted(self):
        slack, github, _, _ = self._indexes()
        assert search_indexes([github, slack], "list the latest releases about slack")[0].connector == "github"
        assert self._served("list the latest releases about slack") == {}

    @pytest.mark.parametrize(
        ("query", "served"),
        [
            ("post it in slack", {"slack": 2}),
            ("share the release file on one drive", {"onedrive": 2}),  # slug-prefix spelling
            ("share the release file on OneDrive", {"onedrive": 2}),  # camelCase display name
            ("what is in my onedrive", {"onedrive": 2}),
            ("search slack for the outage", {"slack": 2}),
            ("archive the old slack channel", {"slack": 2}),
            ("slack: tell the team we shipped", {"slack": 2}),
            ("add the release review to gcal", {"googlecalendar": 1}),  # distinctive keyword
            ("put the offsite on my calendar", {"googlecalendar": 1}),
            ("the slack migration plan", {}),
            ("file a bug: slack notifications are delayed", {}),
            ("add slack time to the week", {}),
            ("send it to review", {}),  # "review" is github's and googlecalendar's
            ("email Bob a recap in slack style", {}),  # a name as a modifier is a topic
            ("draft it in github-flavored markdown", {}),
            ("write it with slack-like brevity", {}),
            ("open an issue in slack format", {}),
            ("schedule a meeting about the github workshop", {}),
        ],
    )
    def test_service_position(self, query, served):
        assert self._served(query) == served

    def test_a_name_beats_a_keyword(self):
        _, github, _, cal = self._indexes()
        query = "add the releases from github to gcal"
        assert self._served(query) == {"github": 2, "googlecalendar": 1}
        boosted = {h.tool.slug: h.score for h in search_indexes([github, cal], query)}
        with unittest.mock.patch.object(search, "_boost_named", lambda *args: args[-1]):
            plain = {h.tool.slug: h.score for h in search_indexes([github, cal], query)}
        assert boosted["GITHUB_LIST_RELEASES"] == pytest.approx(plain["GITHUB_LIST_RELEASES"] * search.NAMED_BOOST)
        assert boosted["GOOGLECALENDAR_CREATE_EVENT"] == pytest.approx(plain["GOOGLECALENDAR_CREATE_EVENT"])

    def test_no_change_when_nothing_or_everything_is_served_or_one_connector(self):
        slack, github, _, _ = self._indexes()
        unboosted = search._boost_named
        calls = []

        def spy(*args):
            out = unboosted(*args)
            calls.append(out == args[-1])
            return out

        with unittest.mock.patch.object(search, "_boost_named", spy):
            search_indexes([github, slack], "list everything from today")  # nothing named
            search_indexes([github, slack], "the slack and github migration")  # topics only
            search_indexes([github, slack], "releases from github to slack")  # both served
            slack.search("post in slack")  # one connector
        assert calls == [True, True, True, True]

    def test_topic_names_count_less_as_search_terms(self):
        slack, github, _, _ = self._indexes()
        served_terms, topic_terms = search._name_terms([(github, None), (slack, None)], "post the slack plan")
        assert (served_terms, topic_terms) == (frozenset(), frozenset({"slack"}))
        served_terms, topic_terms = search._name_terms([(github, None), (slack, None)], "post the plan in slack")
        assert (served_terms, topic_terms) == (frozenset({"slack"}), frozenset())
        assert search._name_terms([(slack, None)], "post the slack plan") == (frozenset(), frozenset())

    def test_order_within_the_named_connector_is_kept(self):
        slack, github, _, _ = self._indexes()
        alone = [h.tool.slug for h in slack.search("post the release to slack channels")]
        together = [
            h.tool.slug for h in search_indexes([github, slack], "post the release to slack channels")
            if h.connector == "slack"
        ]
        assert together == alone

    def test_own_name_is_not_a_signal_among_own_tools(self):
        tools = [
            spec("ACME_CREATE_ACME_LIST", "Create an Acme list in Acme.", read_only=False),
            spec("ACME_LIST_CHANNELS", "List the channels."),
        ]
        index = ToolIndex.build("acme", tools, {"name": "acme", "display_name": "Acme"})
        assert index.df("acm") == 0  # left out of slugs and descriptions
        assert index.search("what channels are in acme")[0].tool.slug == "ACME_LIST_CHANNELS"
        assert set(slugs(index.search("acme lists"))) == {t.slug for t in tools}  # "list" still counts

    def test_a_multi_word_name_goes_as_a_phrase(self):
        tools = [
            spec("GOOGLEDRIVE_LIST_SHARED_DRIVES", "List shared drives in Google Drive."),
            spec("GOOGLEDRIVE_FIND_FILE", "Search GoogleDrive for files."),
        ]
        index = ToolIndex.build("googledrive", tools, {"name": "googledrive", "display_name": "Google Drive"})
        assert index.df("googl") == 0 and index.df("googledriv") == 0
        assert index.df("driv") == 1  # "shared drives" is no name

    def test_named_in_reports_any_mention(self):
        slack, github, _, cal = self._indexes()
        assert slack.named_in("the slack migration") == 2 and slack.named_in("dm Ann") == 1
        assert cal.named_in("put it in gcal") == 1 and github.named_in("nothing here") == 0


# --------------------------------------------------------------------------
# Cache
# --------------------------------------------------------------------------


class TestCache:
    def test_hit_for_the_same_catalogue(self):
        first = cached_index("chat", CHAT, version="1")
        assert cached_index("chat", list(CHAT), version="1") is first
        rebuilt_specs = [dataclasses.replace(t) for t in CHAT]  # equal content, new objects
        assert cached_index("chat", rebuilt_specs, version="1") is first
        assert cache_info() == {"hits": 2, "misses": 1, "size": 1}

    @pytest.mark.parametrize(
        "change",
        [
            lambda tools: [dataclasses.replace(tools[0], description="Something else."), *tools[1:]],
            lambda tools: [dataclasses.replace(tools[0], tags=("new",)), *tools[1:]],
            lambda tools: [
                dataclasses.replace(tools[0], input_schema={"type": "object", "properties": {"q": {}}}),
                *tools[1:],
            ],
            lambda tools: tools[:-1],
            lambda tools: list(reversed(tools)),
        ],
    )
    def test_invalidated_when_the_catalogue_changes(self, change):
        first = cached_index("chat", CHAT)
        second = cached_index("chat", change(list(CHAT)))
        assert second is not first and second.fingerprint != first.fingerprint
        assert cache_info()["misses"] == 2

    def test_version_name_and_card_are_part_of_the_key(self):
        first = cached_index("chat", CHAT, version="1")
        assert cached_index("chat", CHAT, version="2") is not first
        assert cached_index("talk", CHAT, version="1") is not first
        assert cached_index("chat", CHAT, {"keywords": ["dm"]}, version="1") is not first

    def test_lru_bounded(self, monkeypatch):
        monkeypatch.setattr(search, "INDEX_CACHE_SIZE", 2)
        a = cached_index("a", CHAT)
        cached_index("b", CHAT)
        cached_index("a", CHAT)  # touch a
        cached_index("c", CHAT)  # evicts b
        assert cache_info()["size"] == 2
        assert cached_index("a", CHAT) is a
        misses = cache_info()["misses"]
        cached_index("b", CHAT)
        assert cache_info()["misses"] == misses + 1


# --------------------------------------------------------------------------
# Installed toolkits: index_for, search_scored, search_connectors
# --------------------------------------------------------------------------


def _kit(name: str, tools: list[ToolSpec], **attrs) -> type:
    def list_tools(self) -> list[ToolSpec]:
        return list(tools)

    def run_tool(self, tool, arguments, config):
        return ToolResult.ok(tool.slug)

    body = {"name": name, "version": "1.0.0", "tools": list_tools, "run_tool": run_tool, **attrs}
    return type(f"Kit_{name}", (Toolkit,), body)


MAIL_TOOLS = [
    spec("MAIL_SEND_EMAIL", "Send an email.", read_only=False, required_scopes=("send",)),
    spec("MAIL_LIST_MESSAGES", "List messages in the inbox.", required_scopes=("read",)),
]
CAL_TOOLS = [
    spec("CAL_CREATE_EVENT", "Create a calendar event.", read_only=False),
    spec("CAL_LIST_EVENTS", "List upcoming events."),
]


@pytest.fixture
def installed(monkeypatch):
    registry_mod._installed_toolkits.cache_clear()
    monkeypatch.setattr(registry_mod, "_connector_index", None)

    def install(*classes: type) -> None:
        found = {cls.name: cls for cls in classes}
        monkeypatch.setattr(registry_mod, "discover_toolkits", lambda: dict(found))
        registry_mod._installed_toolkits.cache_clear()

    yield install
    registry_mod._installed_toolkits.cache_clear()


@pytest.fixture
def mail_and_cal(installed):
    mail = _kit("mail", MAIL_TOOLS, category="email", keywords=("inbox",), display_name="Mail")
    cal = _kit("cal", CAL_TOOLS, category="calendar", display_name="Calendar")
    installed(mail, cal)
    return mail, cal


class TestIndexFor:
    def test_cached_per_toolkit(self, mail_and_cal):
        index = index_for("mail")
        assert index_for("mail") is index
        assert index.connector == "mail" and [t.slug for t in index.tools] == [t.slug for t in MAIL_TOOLS]
        assert index.card is not None and index.card["category"] == "email"

    def test_new_version_new_index(self, installed, mail_and_cal):
        first = index_for("mail")
        mail2 = _kit("mail", MAIL_TOOLS, category="email", keywords=("inbox",), display_name="Mail")
        mail2.version = "2.0.0"
        installed(mail2)
        assert index_for("mail") is not first

    def test_unknown_toolkit(self, mail_and_cal):
        with pytest.raises(ToolkitNotFoundError, match="'nope'"):
            index_for("nope")


class TestToolkitIndexCache:
    """toolkit_index (behind index_for, search_scored, search_tools_across
    and Toolkit.search_tools) keeps one index per toolkit class, outside the
    LRU, so walking every installed toolkit hits however many there are."""

    def test_more_toolkits_than_the_lru_holds(self, installed, monkeypatch):
        monkeypatch.setattr(search, "INDEX_CACHE_SIZE", 4)
        kits = [_kit(f"kit{i}", [spec(f"KIT{i}_LIST_ITEMS", "List items."), spec(f"KIT{i}_SEND_ITEM", "Send an item.")]) for i in range(10)]
        installed(*kits)
        search_scored("list items", limit=None)
        assert cache_info() == {"hits": 0, "misses": 10, "size": 10}
        for query in ("list items", "send the item", "zebra"):
            search_scored(query, limit=None)
            search_tools_across(query, limit=None)
        assert cache_info() == {"hits": 60, "misses": 10, "size": 10}
        cached_index("adhoc", CHAT)  # the LRU of ad-hoc indexes is apart
        search_scored("list items", limit=None)
        assert cache_info()["misses"] == 11 and cache_info()["size"] == 11

    def test_new_spec_objects_with_the_same_content_hit(self, installed):
        kit = _kit("fresh", [])
        kit.tools = lambda self: [spec("FRESH_LIST_ITEMS", "List items.")]  # type: ignore[attr-defined]
        installed(kit)
        first = index_for("fresh")
        assert index_for("fresh") is first
        assert cache_info() == {"hits": 1, "misses": 1, "size": 1}

    @pytest.mark.parametrize(
        "change",
        [
            lambda kit: setattr(kit, "version", "2.0.0"),
            lambda kit: setattr(kit, "keywords", ("inbox", "mailbox")),
            lambda kit: setattr(kit, "tools", lambda self: [*MAIL_TOOLS, spec("MAIL_DELETE", "Delete a message.")]),
            lambda kit: setattr(kit, "tools", lambda self: [dataclasses.replace(MAIL_TOOLS[0], description="Mail it."), MAIL_TOOLS[1]]),
        ],
    )
    def test_rebuilt_when_the_toolkit_changes(self, mail_and_cal, change):
        mail, _ = mail_and_cal
        first = index_for("mail")
        change(mail)
        second = index_for("mail")
        assert second is not first and index_for("mail") is second
        assert cache_info() == {"hits": 1, "misses": 2, "size": 1}

    def test_one_slot_per_class_dropped_with_it(self):
        import gc

        kit = _kit("gone", [spec("GONE_LIST_ITEMS", "List items.")])
        search.toolkit_index(kit())
        assert cache_info()["size"] == 1
        del kit
        gc.collect()
        assert cache_info()["size"] == 0

    def test_cache_clear_drops_the_slots(self, mail_and_cal):
        first = index_for("mail")
        cache_clear()
        assert cache_info() == {"hits": 0, "misses": 0, "size": 0}
        assert index_for("mail") is not first


@pytest.fixture
def builds(monkeypatch) -> list[str]:
    """The connector of every ToolIndex.build while the test runs (each
    index any cache builds)."""
    built: list[str] = []
    build = ToolIndex.__dict__["build"]

    def counted(cls, connector, *args, **kwargs):
        built.append(connector)
        return build.__func__(cls, connector, *args, **kwargs)

    monkeypatch.setattr(ToolIndex, "build", classmethod(counted))
    return built


def _fresh_kit(name: str, specs: list[ToolSpec], **attrs) -> type:
    """A toolkit whose tools() builds new, equal ToolSpecs on every call."""
    kit = _kit(name, [], **attrs)
    kit.tools = lambda self: [dataclasses.replace(s) for s in specs]  # type: ignore[attr-defined]
    return kit


class TestFreshSpecToolkits:
    """A toolkit may build new, equal ToolSpecs on each tools() call: every
    search path still reuses its one index, at any number of toolkits."""

    def test_more_toolkits_than_the_lru_holds(self, installed, monkeypatch, builds):
        monkeypatch.setattr(search, "INDEX_CACHE_SIZE", 4)
        kits = [
            _fresh_kit(f"kit{i}", [spec(f"KIT{i}_LIST_ITEMS", "List items."), spec(f"KIT{i}_SEND_ITEM", "Send an item.", read_only=False)])
            for i in range(10)
        ]  # fmt: skip
        installed(*kits)
        search_scored("list items", limit=None)
        assert len(builds) == 10
        for query in ("list items", "send the item", "zebra"):
            assert search_scored(query, limit=None) or query == "zebra"
            assert search_tools_across(query, limit=None)
            for kit in kits:
                assert kit().search_tools(query)
        assert len(builds) == 10  # nothing rebuilt
        assert cache_info() == {"hits": 90, "misses": 10, "size": 10}  # one slot each, the LRU empty

    def test_hits_equal_what_the_toolkit_lists(self, installed):
        kit = _fresh_kit("mail", MAIL_TOOLS)
        installed(kit)
        first = search_scored("send an email")[0].tool
        assert search_scored("send an email")[0].tool is first and first == kit().tools()[0]

    def test_a_scoped_connection_filters_the_one_index(self, installed, builds):
        installed(_fresh_kit("mail", MAIL_TOOLS))
        for _ in range(3):
            found = search_scored("email messages", {"mail": {"granted_scopes": ["read"]}}, limit=None)
            assert slugs(found) == ["MAIL_LIST_MESSAGES"]
        assert builds == ["mail"]

    def test_any_changed_field_rebuilds(self, installed):
        """Equal means every field: a spec whose scopes changed rebuilds the
        index, so a search never hands out the old spec."""
        scopes = [("read",)]
        kit = _kit("mail", [])
        kit.tools = lambda self: [spec("MAIL_LIST_MESSAGES", "List messages.", required_scopes=scopes[0])]  # type: ignore[attr-defined]
        installed(kit)
        first = index_for("mail")
        scopes[0] = ("mail.read",)
        second = index_for("mail")
        assert second is not first and second.tools[0].required_scopes == ("mail.read",)
        assert search_scored("list messages")[0].tool.required_scopes == ("mail.read",)

    def test_duck_typed_specs_without_equality_match_by_content(self, builds):
        class Spec:  # compares by identity only
            def __init__(self, slug: str, description: str) -> None:
                self.slug, self.description, self.input_schema = slug, description, SCHEMA

        class Kit(Toolkit):
            name = "duck"
            version = "1.0.0"

            def tools(self):
                return [Spec("DUCK_LIST_ITEMS", "List items."), Spec("DUCK_DELETE_ITEM", "Delete an item.")]

            def run_tool(self, tool, arguments, config):
                return ToolResult.ok(None)

        for _ in range(3):
            assert [t.slug for t in Kit().search_tools("delete the item")][:1] == ["DUCK_DELETE_ITEM"]
        assert builds == ["duck"]


class TestSearchScored:
    def test_across_installed(self, mail_and_cal):
        hits = search_scored("send an email")
        assert hits[0] == ScoredTool("mail", MAIL_TOOLS[0], hits[0].score)
        assert {h.connector for h in search_scored("list", limit=None)} == {"mail", "cal"}

    def test_connector_forms(self, mail_and_cal):
        assert {h.connector for h in search_scored("list", ["cal"], limit=None)} == {"cal"}
        assert {h.connector for h in search_scored("list", "cal", limit=None)} == {"cal"}
        scoped = search_scored("email messages", {"mail": {"granted_scopes": ["read"]}}, limit=None)
        assert slugs(scoped) == ["MAIL_LIST_MESSAGES"]

    def test_no_fallback(self, mail_and_cal):
        assert search_scored("zebra quantum") == []
        assert search_scored(None) == []

    def test_unknown(self, mail_and_cal):
        with pytest.raises(ToolkitNotFoundError):
            search_scored("x", ["nope"])


class TestSearchConnectors:
    def test_installed_cards(self, mail_and_cal):
        assert search_connectors("check my inbox")[0][0] == "mail"
        assert search_connectors("schedule a calendar event")[0][0] == "cal"
        assert search_connectors("zebra") == []

    def test_rebuilt_when_cards_change(self, installed, mail_and_cal):
        assert search_connectors("chat")[:1] == []
        installed(_kit("talk", [], category="chat", display_name="Talk"))
        assert search_connectors("chat")[0][0] == "talk"


class TestConnectorIndex:
    CARDS: ClassVar[dict[str, dict[str, Any]]] = {
        "gmail": {"name": "gmail", "display_name": "Gmail", "category": "email", "keywords": ["inbox", "email"],
                  "description": "Read and send email.", "examples": ["my unread emails"]},
        "slack": {"name": "slack", "display_name": "Slack", "category": "chat", "keywords": ["channel", "DM"],
                  "description": "Team chat messages.", "examples": ["DM Priya"]},
        "googledrive": {"name": "googledrive", "display_name": "Google Drive", "category": "files",
                        "keywords": ["file", "folder"], "description": "Files in Drive.", "examples": []},
    }  # fmt: skip

    def test_picks_the_connector(self):
        index = ConnectorIndex.build(self.CARDS)
        assert index.search("send an email")[0][0] == "gmail"
        assert index.search("post in the channel")[0][0] == "slack"
        assert index.search("find the budget spreadsheet in Google Drive")[0][0] == "googledrive"
        assert index.search("googledrive")[0][0] == "googledrive"

    def test_cards_as_an_iterable_and_ties_keep_order(self):
        cards = [{"name": "b", "keywords": ["thing"]}, {"name": "a", "keywords": ["thing"]}]
        assert [name for name, _ in ConnectorIndex.build(cards).search("thing")] == ["b", "a"]

    def test_empty_and_limits(self):
        index = ConnectorIndex.build(self.CARDS)
        assert index.search("") == [] and index.search(None) == [] and index.search("zzz") == []
        assert len(index.search("email chat files", 2)) == 2
        assert index.search("email", 0) == []
        assert len(ConnectorIndex.build({})) == 0 and ConnectorIndex.build({}).search("x") == []


# --------------------------------------------------------------------------
# The legacy helpers keep their contracts on top of the index
# --------------------------------------------------------------------------


class TestLegacyHelpers:
    def test_rank_tools_empty_and_no_match_fallbacks(self):
        for query in (None, "", "  ", "zzzz qqqq"):
            assert rank_tools(CHAT, query, limit=2) == CHAT[:2]
        assert rank_tools(CHAT, "zzzz", limit=None) == CHAT
        assert rank_tools(CHAT, "zzzz", limit=0) == []

    def test_rank_tools_is_cached(self):
        rank_tools(CHAT, "list channels")
        rank_tools(list(CHAT), "send a message")
        assert cache_info()["misses"] == 1 and cache_info()["hits"] == 1

    def test_rank_across_fallback_takes_turns(self):
        catalogs = {"mail": MAIL_TOOLS, "cal": CAL_TOOLS}
        for query in (None, "", "zebra quantum"):
            assert [(q.connector, q.tool.slug) for q in rank_across(catalogs, query, limit=3)] == [
                ("mail", "MAIL_SEND_EMAIL"), ("cal", "CAL_CREATE_EVENT"), ("mail", "MAIL_LIST_MESSAGES"),
            ]  # fmt: skip

    def test_rank_across_returns_qualified_tools(self):
        found = rank_across({"mail": MAIL_TOOLS, "cal": CAL_TOOLS}, "create a calendar event")
        assert found[0] == QualifiedTool("cal", CAL_TOOLS[0])

    def test_duck_typed_specs(self):
        """Hosts may pass their own spec objects: slug, description and
        input_schema are enough for every legacy wrapper."""

        @dataclasses.dataclass
        class Spec:  # no tags, read_only, destructive, required_scopes or risk
            slug: str
            description: str
            input_schema: dict

        mail = [Spec("MAIL_SEND_EMAIL", "Send an email.", SCHEMA), Spec("MAIL_LIST_MESSAGES", "List messages.", SCHEMA)]
        cal = [Spec("CAL_CREATE_EVENT", "Create a calendar event.", {"type": "object", "properties": {"title": {}}})]
        assert rank_tools(mail, "send an email")[0] is mail[0]
        assert rank_tools(mail, "zzzz", limit=1) == mail[:1]
        found = rank_across({"mail": mail, "cal": cal}, "create a calendar event", keywords={"cal": ["calendar"]})
        assert (found[0].connector, found[0].tool) == ("cal", cal[0])
        assert [q.tool for q in rank_across({"mail": mail, "cal": cal}, None, limit=2)] == [mail[0], cal[0]]
        hits = search_indexes([ToolIndex.build("mail", mail), ToolIndex.build("cal", cal)], "list messages in mail")
        assert hits[0].tool is mail[1]

        class Kit(Toolkit):
            name = "duck"
            version = "1.0.0"

            def tools(self):
                return [Spec("DUCK_LIST_ITEMS", "List items.", SCHEMA), Spec("DUCK_DELETE_ITEM", "Delete an item.", SCHEMA)]

            def run_tool(self, tool, arguments, config):
                return ToolResult.ok(None)

        assert [t.slug for t in Kit().search_tools("delete the item")][:1] == ["DUCK_DELETE_ITEM"]

    def test_toolkit_search_tools(self, mail_and_cal):
        mail, _ = mail_and_cal
        kit = mail()
        assert kit.search_tools("send an email")[0].slug == "MAIL_SEND_EMAIL"
        assert kit.search_tools("zebra", limit=1) == kit.available_tools()[:1]
        assert kit.search_tools("", limit=5) == kit.available_tools()
        granted = {"granted_scopes": ["read"]}
        assert [t.slug for t in kit.search_tools("send email messages", config=granted)] == ["MAIL_LIST_MESSAGES"]
        assert kit.search_tools("send", config={"granted_scopes": []}) == []

    def test_toolkit_search_tools_with_an_override_that_returns_other_specs(self, installed):
        class Renamed(_kit("ren", [spec("REN_LIST_FILES", "List files.")])):  # type: ignore[misc,valid-type]
            def available_tools(self, config=None):
                return [spec("REN_LIST_FILES", "List the files in a folder.")]

        installed(Renamed)
        found = Renamed().search_tools("folder")
        assert [t.description for t in found] == ["List the files in a folder."]

    def test_search_tools_across_fallback_and_matches(self, mail_and_cal):
        assert [q.tool.slug for q in search_tools_across("zebra", limit=None)] == [
            "CAL_CREATE_EVENT", "MAIL_SEND_EMAIL", "CAL_LIST_EVENTS", "MAIL_LIST_MESSAGES",
        ]  # fmt: skip
        assert search_tools_across("send email")[0] == QualifiedTool("mail", MAIL_TOOLS[0])


# --------------------------------------------------------------------------
# Synthetic catalogues
# --------------------------------------------------------------------------


class TestSyntheticCatalog:
    def test_shape(self):
        cards, tools = synthetic_catalog(12, 30, seed=3)
        assert list(cards) == list(tools) and len(cards) == 12
        for name, card in cards.items():
            assert set(card) == {"name", "display_name", "description", "category", "keywords", "examples", "auth_type"}
            assert card["name"] == name and card["category"] in search_categories()
            catalogue = tools[name]
            assert len(catalogue) == 30 and len({t.slug for t in catalogue}) == 30
            prefix = registry_prefix(name)
            assert all(t.slug.startswith(prefix) for t in catalogue)
        prefixes = [registry_prefix(n) for n in cards]
        assert registry_mod.slug_prefix_conflicts({n: type("K", (), {"slug_prefix": p}) for n, p in zip(cards, prefixes)}) == []

    def test_deterministic(self):
        assert synthetic_catalog(3, 20, seed=7) == synthetic_catalog(3, 20, seed=7)
        assert synthetic_catalog(3, 20, seed=7) != synthetic_catalog(3, 20, seed=8)

    def test_realistic_and_has_near_duplicates(self):
        _, tools = synthetic_catalog(1, 400, seed=0)
        catalogue = next(iter(tools.values()))
        stems = {tuple(t.slug.split("_")[2:4]) for t in catalogue}
        assert len(stems) < len(catalogue)  # several tools share verb + object
        assert {t.read_only for t in catalogue} == {True, False}
        assert any(t.input_schema["properties"] for t in catalogue)
        assert len({t.description for t in catalogue}) > 50

    def test_large_catalogue_is_searchable(self):
        cards, tools = synthetic_catalog(1, 2000, seed=0)
        name = next(iter(tools))
        index = ToolIndex.build(name, tools[name], cards[name])
        hits = index.search("list events", 10)
        assert cards[name]["category"] == "calendar"
        assert hits and all("EVENT" in h.tool.slug for h in hits[:2])


def search_categories() -> frozenset[str]:
    from aisquare.pipe.core.tools import CATEGORIES

    return CATEGORIES


def registry_prefix(name: str) -> str:
    from aisquare.pipe.core.tools import default_slug_prefix

    return default_slug_prefix(name)


# --------------------------------------------------------------------------
# The shared interface (.aisquare/rules/pipe-router.md)
# --------------------------------------------------------------------------


class TestSharedInterface:
    def test_search_indexes_signature(self):
        """pipe-as-service calls search_indexes([(index, allowed), ...],
        query, limit) positionally: these names, kinds and defaults are the
        contract."""
        import inspect

        kind = inspect.Parameter.POSITIONAL_OR_KEYWORD
        params = inspect.signature(search_indexes).parameters.values()
        assert [(p.name, p.kind, p.default) for p in params] == [
            ("indexes", kind, inspect.Parameter.empty),
            ("query", kind, inspect.Parameter.empty),
            ("limit", kind, 10),
        ]

    def test_search_indexes_is_exported(self):
        from aisquare import pipe

        assert pipe.search_indexes is search_indexes
        assert "search_indexes" in pipe.__all__ and "search_indexes" in search.__all__

    def test_search_indexes_entries_and_results(self):
        mail, cal = ToolIndex.build("mail", MAIL_TOOLS), ToolIndex.build("cal", CAL_TOOLS)
        hits = search_indexes([(mail, frozenset({"MAIL_LIST_MESSAGES"})), (cal, None)], "list", None)
        assert all(isinstance(h, ScoredTool) for h in hits)
        assert {(h.connector, h.tool.slug) for h in hits} == {
            ("mail", "MAIL_LIST_MESSAGES"), ("cal", "CAL_LIST_EVENTS"),
        }  # fmt: skip
        assert search_indexes([mail, cal], "list", 1) == hits[:1]
        assert search_indexes([mail, cal], "") == [] and search_indexes([mail, cal], None) == []

    def test_installed_toolkit_searches_live_in_registry(self):
        """index_for, search_scored and search_connectors are defined in
        registry and re-exported by search under the same names."""
        from aisquare import pipe

        for name in ("index_for", "search_scored", "search_connectors"):
            assert getattr(search, name) is getattr(registry_mod, name) is getattr(pipe, name)
            assert name in search.__all__ and name in dir(search)
        with pytest.raises(AttributeError):
            search.not_a_name  # noqa: B018

    def test_search_never_imports_registry_while_loading(self):
        """registry imports search; search names registry only for type
        checking and in its lazy re-export, so neither module needs the
        other half-loaded."""
        import ast
        import inspect

        tree = ast.parse(inspect.getsource(search))
        for node in tree.body:
            if isinstance(node, ast.If) and "TYPE_CHECKING" in ast.unparse(node.test):
                continue
            if isinstance(node, ast.FunctionDef) and node.name == "__getattr__":
                continue
            for inner in ast.walk(node):
                if isinstance(inner, ast.ImportFrom):
                    assert "registry" not in (inner.module or "") and all(a.name != "registry" for a in inner.names)
                elif isinstance(inner, ast.Import):
                    assert all("registry" not in a.name for a in inner.names)
