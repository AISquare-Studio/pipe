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
    left_out_toolkits,
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
        # The first discovery imports the plugin modules; their import-time
        # side effects (urllib3 probes for IPv6) are theirs, not catalogue()'s.
        installed_toolkits()
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


RULE = "^[a-z][a-z0-9_]{0,63}$"


class TestLeftOutReasons:
    """Whatever installed_toolkits leaves out, left_out_toolkits says why, and
    naming it in a search says why too."""

    @pytest.fixture
    def entry_points(self, monkeypatch, fresh_cache):
        def install(found: dict[str, type]) -> None:
            monkeypatch.setattr(registry_mod, "discover_toolkits", lambda: dict(found))

        return install

    def test_a_class_name_that_differs_from_its_entry_point_is_left_out(self, entry_points, caplog):
        entry_points(
            {
                "ledgermis": _kit("ledger-mis", ["LEDGERMIS_X"], prefix="LEDGERMIS_"),
                "ledger": _kit("ledger_v2", ["LEDGER_X"], prefix="LEDGER_"),
                "mail": _kit("mail", ["MAIL_SEND"]),
            }
        )
        with caplog.at_level("WARNING", logger="aisquare.pipe"):
            assert list(installed_toolkits()) == ["mail"]
            assert list(catalogue()) == ["mail"]
            assert list(installed_toolkits()) == ["mail"]
        warnings = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
        assert warnings == [
            (
                "Left out toolkits whose class name differs from their entry point "
                "(`pipe validate` fails on this): 'ledger' (class name 'ledger_v2'), "
                "'ledgermis' (class name 'ledger-mis')"
            )
        ]

    def test_the_checks_run_name_rule_then_mismatch_then_clash(self, entry_points):
        """Pins the order. ledgermis's class name 'ledger-mis' gives the default
        prefix LEDGER_MIS_, which nests under ledger's LEDGER_: if the mismatch
        filter ran after the clash check, the pair would clash and take the
        good ledger down. led-ger's class name differs too, but the name rule
        comes first, so that is the reason it gets."""
        entry_points(
            {
                "ledger": _kit("ledger", ["LEDGER_X"]),
                "ledgermis": _kit("ledger-mis", ["LEDGER_MIS_X"]),
                "led-ger": _kit("led_ger2", ["LED_GER2_X"]),
            }
        )
        assert registry_mod.discover_toolkits()["ledgermis"].slug_prefix == "LEDGER_MIS_"
        assert list(installed_toolkits()) == ["ledger"]
        assert left_out_toolkits() == {
            "led-ger": f"name breaks {RULE}",
            "ledgermis": "name differs from its entry point (class name 'ledger-mis')",
        }

    def test_every_reason_is_recorded(self, entry_points):
        entry_points(
            {
                "led-ger": _kit("led-ger", ["LED_GER_X"]),
                "ledgermis": _kit("ledger-mis", ["LEDGERMIS_X"], prefix="LEDGERMIS_"),
                "one": _kit("one", ["ONE_X"]),
                "one_drive": _kit("one_drive", ["ONE_DRIVE_X"]),
                "mail": _kit("mail", ["MAIL_SEND"]),
            }
        )
        assert left_out_toolkits() == {
            "led-ger": f"name breaks {RULE}",
            "ledgermis": "name differs from its entry point (class name 'ledger-mis')",
            "one": "slug prefix ONE_ collides with one_drive (ONE_DRIVE_)",
            "one_drive": "slug prefix ONE_DRIVE_ collides with one (ONE_)",
        }
        assert list(installed_toolkits()) == ["mail"]

    def test_nothing_left_out(self, entry_points):
        entry_points({"mail": _kit("mail", ["MAIL_SEND"])})
        assert left_out_toolkits() == {}

    def test_it_follows_the_cache_and_refresh(self, monkeypatch, fresh_cache):
        found = {"led-ger": _kit("led-ger", ["LED_GER_X"])}
        monkeypatch.setattr(registry_mod, "discover_toolkits", lambda: dict(found))
        assert list(left_out_toolkits()) == ["led-ger"]
        found = {"ledger": _kit("ledger", ["LEDGER_X"])}
        assert list(left_out_toolkits()) == ["led-ger"]  # cached, like installed_toolkits
        assert left_out_toolkits(refresh=True) == {}
        assert list(installed_toolkits()) == ["ledger"]

    def test_refresh_rebuilds_the_record(self, monkeypatch, fresh_cache):
        """The record comes with the cached discovery (no module global):
        refresh=True gives the new discovery's record, and each call hands
        out a copy."""
        found = {"ledger": _kit("ledger", ["LEDGER_X"])}
        monkeypatch.setattr(registry_mod, "discover_toolkits", lambda: dict(found))
        assert left_out_toolkits() == {}
        found = {**found, "led-ger": _kit("led-ger", ["LED_GER_X"])}
        assert left_out_toolkits() == {}  # cached
        assert left_out_toolkits(refresh=True) == {"led-ger": f"name breaks {RULE}"}
        left_out_toolkits().clear()
        assert left_out_toolkits() == {"led-ger": f"name breaks {RULE}"}
        assert registry_mod._installed_toolkits().left_out == {"led-ger": f"name breaks {RULE}"}
        assert not hasattr(registry_mod, "_LEFT_OUT")
        found = {"ledger": found["ledger"]}
        assert list(installed_toolkits(refresh=True)) == ["ledger"]
        assert left_out_toolkits() == {}

    def test_the_real_install_leaves_nothing_out(self, fresh_cache):
        assert left_out_toolkits() == {}

    def test_strict_raises_for_any_left_out_toolkit(self, entry_points):
        entry_points(
            {
                "led-ger": _kit("led-ger", ["LED_GER_X"]),
                "ledgermis": _kit("ledger-mis", ["LEDGERMIS_X"], prefix="LEDGERMIS_"),
                "mail": _kit("mail", ["MAIL_SEND"]),
            }
        )
        with pytest.raises(ToolkitConflictError) as caught:
            installed_toolkits(strict=True)
        assert str(caught.value) == (
            f"installed toolkits left out: 'led-ger' (name breaks {RULE}); "
            "'ledgermis' (name differs from its entry point (class name 'ledger-mis'))"
        )
        assert list(installed_toolkits()) == ["mail"]

    def test_strict_names_clashes_then_the_rest(self, entry_points):
        entry_points(
            {
                "github": _kit("github", ["GITHUB_X"]),
                "gh2": _kit("gh2", ["GITHUB_Y"], prefix="GITHUB_"),
                "led-ger": _kit("led-ger", ["LED_GER_X"]),
            }
        )
        with pytest.raises(ToolkitConflictError) as caught:
            installed_toolkits(strict=True)
        assert str(caught.value) == (
            "installed toolkits share a slug prefix: gh2 (GITHUB_) and github (GITHUB_). "
            f"installed toolkits left out: 'led-ger' (name breaks {RULE})"
        )

    def test_strict_passes_when_nothing_is_left_out(self, entry_points):
        entry_points({"mail": _kit("mail", ["MAIL_SEND"])})
        assert list(installed_toolkits(strict=True)) == ["mail"]

    def test_get_toolkit_honours_it(self, entry_points):
        hyphenated = _kit("led-ger", ["LED_GER_X"])
        entry_points({"led-ger": hyphenated, "mail": _kit("mail", ["MAIL_SEND"])})
        with pytest.raises(ToolkitNotFoundError) as caught:
            get_toolkit("led-ger")
        assert str(caught.value) == (
            f"Toolkit 'led-ger' not found. Available: mail. 'led-ger' left out: name breaks {RULE}"
        )
        assert registry_mod.discover_toolkits()["led-ger"] is hyphenated  # the raw read still has it
        assert get_toolkit("mail").name == "mail"

    @pytest.mark.parametrize(
        "found, name, says",
        [
            ({"led-ger": _kit("led-ger", ["LED_GER_X"])}, "led-ger", f"'led-ger' left out: name breaks {RULE}"),
            (
                {"ledgermis": _kit("ledger-mis", ["LEDGERMIS_X"], prefix="LEDGERMIS_")},
                "ledgermis",
                "'ledgermis' left out: name differs from its entry point (class name 'ledger-mis')",
            ),
        ],
    )
    def test_naming_a_left_out_toolkit_says_why(self, entry_points, found, name, says):
        entry_points({**found, "mail": _kit("mail", ["MAIL_SEND"])})
        with pytest.raises(ToolkitNotFoundError) as caught:
            search_tools_across("x", [name, "nowhere"])
        message = str(caught.value)
        assert message.startswith(f"Toolkit(s) {name!r}, 'nowhere' not found. Available: mail")
        assert message.endswith(f". {says}") and "collide" not in message

    def test_a_clash_keeps_its_own_wording(self, entry_points):
        entry_points({"github": _kit("github", ["GITHUB_X"]), "gh2": _kit("gh2", ["GITHUB_Y"], prefix="GITHUB_")})
        with pytest.raises(ToolkitNotFoundError) as caught:
            search_tools_across("x", ["github"])
        assert str(caught.value) == (
            "Toolkit(s) 'github' not found. Available: (none). "
            "Left out, their slug prefixes collide: gh2 (GITHUB_) and github (GITHUB_)"
        )


class TestUnroutableNames:
    def test_a_hyphenated_toolkit_is_left_out_with_one_warning(self, installed, caplog):
        # "led-ger" and "led_ger" both default to the LED_GER_ prefix: the bad
        # name goes first, so it can't take the good one down in a clash.
        installed(
            _kit("led-ger", ["LED_GER_X"]),
            _kit("led_ger", ["LED_GER_Y"]),
            _kit("mail", ["MAIL_SEND"]),
        )
        with caplog.at_level("WARNING", logger="aisquare.pipe"):
            assert list(installed_toolkits()) == ["led_ger", "mail"]
            assert list(catalogue()) == ["led_ger", "mail"]
            assert {q.connector for q in search_tools_across("x", limit=None)} == {"led_ger", "mail"}
        warnings = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
        assert warnings == [
            (
                "Left out toolkits whose names break ^[a-z][a-z0-9_]{0,63}$ "
                "(`pipe validate` fails on this): 'led-ger'"
            )
        ]

    def test_naming_a_hyphenated_toolkit_is_not_found(self, installed):
        installed(_kit("led-ger", ["LED_GER_X"]))
        with pytest.raises(ToolkitNotFoundError, match="'led-ger' not found"):
            search_tools_across("x", ["led-ger"])

    def test_raw_discovery_still_sees_it(self, installed):
        """discover_toolkits is the raw entry-point read (what `pipe validate`
        reports on); only the served set leaves it out."""
        found = installed(_kit("led-ger", ["LED_GER_X"]))
        assert registry_mod.discover_toolkits() == found
        assert installed_toolkits() == {}


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

    def test_clashing_toolkits_are_left_out_not_fatal(self, installed, caplog):
        # One clashing pair (a third-party "one" with ONE_ vs ONE_DRIVE_) must
        # not take the router down: both are left out, the rest stays.
        installed(
            _kit("one", ["ONE_X"]),
            _kit("one_drive", ["ONE_DRIVE_X"]),
            _kit("mail", ["MAIL_SEND_EMAIL"]),
        )
        with caplog.at_level("WARNING", logger="aisquare.pipe"):
            assert list(catalogue()) == ["mail"]
            assert [q.connector for q in search_tools_across("x", limit=None)] == ["mail"]
            assert list(installed_toolkits()) == ["mail"]
        warnings = [r.getMessage() for r in caplog.records if "slug prefixes collide" in r.getMessage()]
        assert warnings == ["Left out toolkits whose slug prefixes collide (`pipe validate` fails on this): one (ONE_) and one_drive (ONE_DRIVE_)"]

    def test_a_clash_is_cached_like_any_discovery(self, monkeypatch, fresh_cache):
        calls = []

        def discover():
            calls.append(1)
            return {"github": _kit("github", ["GITHUB_X"]), "gh2": _kit("gh2", ["GITHUB_Y"], prefix="GITHUB_")}

        monkeypatch.setattr(registry_mod, "discover_toolkits", discover)
        assert catalogue() == {}
        assert catalogue() == {}
        search_tools_across("x")
        assert len(calls) == 1

    def test_strict_discovery_raises(self, installed):
        installed(_kit("github", ["GITHUB_X"]), _kit("gh2", ["GITHUB_Y"], prefix="GITHUB_"))
        with pytest.raises(ToolkitConflictError, match=r"gh2 \(GITHUB_\) and github \(GITHUB_\)"):
            installed_toolkits(strict=True)
        assert installed_toolkits() == {}

    def test_naming_a_left_out_toolkit_says_why(self, installed):
        installed(_kit("github", ["GITHUB_X"]), _kit("gh2", ["GITHUB_Y"], prefix="GITHUB_"))
        with pytest.raises(ToolkitNotFoundError, match=r"'github' not found.*slug prefixes collide: gh2"):
            search_tools_across("x", ["github"])

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

    def test_one_discovery_per_search(self, installed, monkeypatch):
        """A refresh landing mid-search can't make an installed toolkit
        unknown: the search reads the discovery once."""
        self._install(installed)
        cached = registry_mod._installed_toolkits
        first = cached()
        reads = []

        def racing():
            reads.append(1)
            return first if len(reads) == 1 else first._replace(toolkits={})

        racing.cache_clear = cached.cache_clear  # type: ignore[attr-defined]
        monkeypatch.setattr(registry_mod, "_installed_toolkits", racing)
        assert {q.connector for q in search_tools_across(None, limit=None)} == {"mail", "cal"}
        assert reads == [1]

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
        installed_toolkits()  # import the plugins first (see TestCatalogue)
        with no_network() as log:
            found = search_tools_across("list files", limit=10)
        assert log.attempts == []
        assert found and all(isinstance(q, QualifiedTool) for q in found)
