"""Tests for the connector registry / plugin discovery."""

import pytest

from aisquare.pipe import (
    CARD_FIELDS,
    QualifiedTool,
    Toolkit,
    ToolkitConflictError,
    ToolResult,
    ToolSpec,
    catalogue,
    installed_toolkits,
    search_tools_across,
    slug_prefix_conflicts,
)
from aisquare.pipe.core import registry as registry_mod
from aisquare.pipe.core.registry import (
    discover_connectors,
    discover_converters,
    discover_toolkits,
    get_connector,
    get_converter,
    get_toolkit,
)
from aisquare.pipe.errors import (
    ConnectorNotFoundError,
    ConverterError,
    PipeError,
    ToolkitNotFoundError,
)
from aisquare.pipe.testing.mock_connectors import (
    MockConverter,
    MockSink,
    MockSource,
    MockToolkit,
)
from aisquare.pipe.testing.validation import no_network


class TestDiscoverConnectors:
    def test_discovers_mock_connectors(self):
        """After pip install -e ., mock connectors should be discoverable."""
        connectors = discover_connectors()
        assert "mock-source" in connectors
        assert "mock-sink" in connectors

    def test_discovered_types(self):
        connectors = discover_connectors()
        assert connectors["mock-source"] is MockSource
        assert connectors["mock-sink"] is MockSink


class TestDiscoverConverters:
    def test_discovers_mock_converter(self):
        converters = discover_converters()
        assert "mock-converter" in converters
        assert converters["mock-converter"] is MockConverter


class TestGetConnector:
    def test_get_existing(self):
        cls = get_connector("mock-source")
        assert cls is MockSource

    def test_get_missing(self):
        with pytest.raises(ConnectorNotFoundError):
            get_connector("nonexistent-connector")


class TestGetConverter:
    def test_get_existing(self):
        cls = get_converter("mock-converter")
        assert cls is MockConverter

    def test_get_missing(self):
        with pytest.raises(ConverterError):
            get_converter("nonexistent-converter")


class TestToolkits:
    def test_discovers_mock_toolkit(self):
        toolkits = discover_toolkits()
        assert toolkits["mock"] is MockToolkit

    def test_toolkits_are_not_connectors(self):
        assert "mock" not in discover_connectors()

    def test_get_existing(self):
        assert get_toolkit("mock") is MockToolkit

    def test_get_missing(self):
        with pytest.raises(ToolkitNotFoundError, match="Available: .*mock"):
            get_toolkit("nonexistent-toolkit")

    def test_not_found_is_a_pipe_error(self):
        assert issubclass(ToolkitNotFoundError, PipeError)

    def test_top_level_reexports(self):
        from aisquare import pipe

        assert pipe.discover_toolkits is discover_toolkits
        assert pipe.get_toolkit is get_toolkit


# --------------------------------------------------------------------------
# Cross-toolkit catalogue
# --------------------------------------------------------------------------

_SCHEMA = {"type": "object", "properties": {}}


def _kit(kit_name: str, slugs: list[str], prefix: str = "", **attrs) -> type:
    def tools(self) -> list[ToolSpec]:
        return [ToolSpec(s, f"{s.lower()} tool", _SCHEMA, read_only=True) for s in slugs]

    def run_tool(self, tool, arguments, config):
        return ToolResult.ok(tool.slug)

    body = {"name": kit_name, "version": "1.0.0", "tools": tools, "run_tool": run_tool, **attrs}
    if prefix:
        body["slug_prefix"] = prefix
    return type(f"Kit_{kit_name}", (Toolkit,), body)


@pytest.fixture
def fresh_cache():
    registry_mod._installed_toolkits.cache_clear()
    yield
    registry_mod._installed_toolkits.cache_clear()


@pytest.fixture
def installed(monkeypatch, fresh_cache):
    """Replace entry-point discovery with the given toolkit classes."""

    def install(*classes: type) -> dict[str, type]:
        found = {cls.name: cls for cls in classes}
        monkeypatch.setattr(registry_mod, "discover_toolkits", lambda: dict(found))
        return found

    return install


class TestCatalogue:
    def test_real_install_is_offline(self, fresh_cache):
        with no_network() as log:
            cards = catalogue()
        assert log.attempts == []
        assert "mock" in cards
        for name, card in cards.items():
            assert tuple(card) == CARD_FIELDS
            assert card["name"] == name

    def test_cards_of_the_installed_toolkits(self, installed):
        mail = _kit("mail", ["MAIL_SEND"], category="email", keywords=("inbox",))
        installed(mail, _kit("cal", ["CAL_LIST"]))
        cards = catalogue()
        assert list(cards) == ["cal", "mail"]  # sorted
        assert cards["mail"] == mail.card()

    def test_discovery_is_cached(self, monkeypatch, fresh_cache):
        calls = []

        def discover():
            calls.append(1)
            return {"mail": _kit("mail", ["MAIL_SEND"])}

        monkeypatch.setattr(registry_mod, "discover_toolkits", discover)
        catalogue()
        catalogue()
        installed_toolkits()
        assert len(calls) == 1
        catalogue(refresh=True)
        assert len(calls) == 2

    def test_returns_fresh_dicts(self, installed):
        installed(_kit("mail", ["MAIL_SEND"]))
        catalogue()["mail"]["keywords"].append("x")
        catalogue().pop("mail")
        assert catalogue()["mail"]["keywords"] == []
        installed_toolkits().clear()
        assert "mail" in installed_toolkits()

    def test_a_broken_card_is_left_out(self, installed):
        class Broken(_kit("broken", ["BROKEN_X"])):  # type: ignore[misc,valid-type]
            @classmethod
            def card(cls):
                raise RuntimeError("boom")

        installed(Broken, _kit("mail", ["MAIL_SEND"]))
        assert list(catalogue()) == ["mail"]

    def test_non_toolkits_are_skipped(self, monkeypatch, fresh_cache):
        monkeypatch.setattr(
            registry_mod, "discover_toolkits", lambda: {"mail": _kit("mail", ["MAIL_SEND"]), "odd": object}
        )
        assert list(catalogue()) == ["mail"]


class TestSlugPrefixConflicts:
    def test_equal_and_nested_prefixes(self):
        kits = {
            "one": _kit("one", ["ONE_X"]),
            "one_drive": _kit("one_drive", ["ONE_DRIVE_X"]),
            "github": _kit("github", ["GITHUB_X"]),
            "gh2": _kit("gh2", ["GITHUB_Y"], prefix="GITHUB_"),
            "google": _kit("google", ["GOOGLE_X"]),
            "googledrive": _kit("googledrive", ["GOOGLEDRIVE_X"]),
        }
        assert slug_prefix_conflicts(kits) == [("gh2", "github"), ("one", "one_drive")]

    def test_real_install_has_none(self, fresh_cache):
        assert slug_prefix_conflicts(installed_toolkits()) == []

    def test_discovery_asserts_uniqueness(self, installed):
        installed(_kit("github", ["GITHUB_X"]), _kit("gh2", ["GITHUB_Y"], prefix="GITHUB_"))
        with pytest.raises(ToolkitConflictError, match=r"gh2 \(GITHUB_\) and github \(GITHUB_\)"):
            catalogue()
        with pytest.raises(ToolkitConflictError):
            search_tools_across("x")

    def test_conflict_is_a_pipe_error(self):
        assert issubclass(ToolkitConflictError, PipeError)


class TestSearchToolsAcross:
    def _install(self, installed):
        mail = _kit("mail", ["MAIL_SEND_EMAIL", "MAIL_LIST_MESSAGES"], category="email", keywords=("inbox",))
        cal = _kit("cal", ["CAL_CREATE_EVENT", "CAL_LIST_EVENTS"], category="calendar")
        installed(mail, cal)

    def test_every_installed_toolkit_by_default(self, installed):
        self._install(installed)
        found = search_tools_across("send email")
        assert found[0] == QualifiedTool("mail", ToolSpec("MAIL_SEND_EMAIL", "mail_send_email tool", _SCHEMA, read_only=True))
        assert {q.connector for q in search_tools_across(None, limit=None)} == {"mail", "cal"}

    def test_only_the_chosen_connectors(self, installed):
        self._install(installed)
        assert {q.connector for q in search_tools_across("list", ["cal"], limit=None)} == {"cal"}
        assert {q.connector for q in search_tools_across("list", "cal", limit=None)} == {"cal"}

    def test_connector_keywords_and_category_count(self, installed):
        self._install(installed)
        assert search_tools_across("check my inbox", ["cal", "mail"])[0].connector == "mail"
        assert search_tools_across("my calendar", ["mail", "cal"])[0].connector == "cal"

    def test_configs_filter_through_available_tools(self, installed):
        scoped = _kit("scoped", [])

        def tools(self):
            return [
                ToolSpec("SCOPED_READ", "read files", _SCHEMA, read_only=True, required_scopes=("r",)),
                ToolSpec("SCOPED_WRITE", "write files", _SCHEMA, read_only=False, required_scopes=("w",)),
            ]

        scoped.tools = tools  # type: ignore[attr-defined]
        installed(scoped)
        found = search_tools_across("files", {"scoped": {"granted_scopes": ["r"]}}, limit=None)
        assert [q.tool.slug for q in found] == ["SCOPED_READ"]
        found = search_tools_across("files", {"scoped": None}, limit=None)
        assert [q.tool.slug for q in found] == ["SCOPED_READ", "SCOPED_WRITE"]

    def test_unknown_connector(self, installed):
        self._install(installed)
        with pytest.raises(ToolkitNotFoundError, match="'nope'"):
            search_tools_across("x", ["mail", "nope"])

    def test_a_broken_toolkit_lists_nothing(self, installed):
        class Broken(_kit("broken", ["BROKEN_X"])):  # type: ignore[misc,valid-type]
            def available_tools(self, config=None):
                raise RuntimeError("boom")

        installed(Broken, _kit("mail", ["MAIL_SEND_EMAIL"]))
        assert {q.connector for q in search_tools_across(None, limit=None)} == {"mail"}

    def test_real_install_is_offline(self, fresh_cache):
        with no_network() as log:
            found = search_tools_across("list files", limit=10)
        assert log.attempts == []
        assert found and all(isinstance(q, QualifiedTool) for q in found)
