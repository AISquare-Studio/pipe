"""The toolkit compliance suite against MockToolkit, and the toolkit contract
checks (testing/validation.py) against deliberately broken toolkits."""

from __future__ import annotations

import os
import socket
import unittest

import pytest

from aisquare.pipe import AuthType, OAuthSpec, Toolkit, ToolResult, ToolSpec
from aisquare.pipe.testing.compliance import toolkit_compliance_suite
from aisquare.pipe.testing.mock_connectors import MockSource, MockToolkit
from aisquare.pipe.testing.validation import (
    MAX_TOOL_DESCRIPTION,
    Severity,
    failures,
    run_toolkit_contract_checks,
)


class TestMockToolkitCompliance(toolkit_compliance_suite(MockToolkit)):
    pass


SCHEMA = {"type": "object", "properties": {"id": {"type": "string"}}, "required": ["id"]}


class Good(Toolkit):
    name = "good"
    version = "0.1.0"
    oauth = OAuthSpec(provider="good", authorize_url="https://a.example", token_url="https://t.example")

    def tools(self) -> list[ToolSpec]:
        return [
            ToolSpec("GOOD_GET", "Get a thing.", SCHEMA, read_only=True),
            ToolSpec("GOOD_LIST", "List things.", {"type": "object", "properties": {}}, read_only=True),
        ]

    def run_tool(self, tool, arguments, config):
        return ToolResult.ok({})


def failed_ids(cls: type, ep_name: str | None = None) -> set[str]:
    return {r.id for r in failures(run_toolkit_contract_checks(cls, ep_name))}


def test_good_toolkit_passes():
    assert failed_ids(Good, "good") == set()


def _with_tools(*tools: ToolSpec, **attrs) -> type:
    return type("Broken", (Good,), {"tools": lambda self: list(tools), **attrs})


def _forced_risk(spec: ToolSpec, risk: str) -> ToolSpec:
    """``spec`` with ``risk`` set past ``__post_init__``'s check."""
    object.__setattr__(spec, "risk", risk)
    return spec


@pytest.mark.parametrize(
    ("cls", "check_id"),
    [
        (_with_tools(ToolSpec("OTHER_GET", "x", SCHEMA, read_only=True)), "contract.toolkit.slugs"),
        (_with_tools(ToolSpec("good_get", "x", SCHEMA, read_only=True)), "contract.toolkit.slugs"),
        (
            _with_tools(
                ToolSpec("GOOD_GET", "x", SCHEMA, read_only=True),
                ToolSpec("GOOD_GET", "y", SCHEMA, read_only=True),
            ),
            "contract.toolkit.slugs",
        ),
        (_with_tools(), "contract.toolkit.tools"),
        (_with_tools(ToolSpec("GOOD_GET", "x", {"type": "string"}, read_only=True)), "contract.toolkit.input-schema"),
        (
            _with_tools(ToolSpec("GOOD_GET", "x", {"type": "object"}, read_only=True)),
            "contract.toolkit.input-schema",
        ),
        (
            _with_tools(
                ToolSpec(
                    "GOOD_GET",
                    "x",
                    {"type": "object", "properties": {}, "required": ["id"]},
                    read_only=True,
                )
            ),
            "contract.toolkit.input-schema",
        ),
        (_with_tools(ToolSpec("GOOD_GET", "  ", SCHEMA, read_only=True)), "contract.toolkit.descriptions"),
        (
            _with_tools(ToolSpec("GOOD_GET", "x" * (MAX_TOOL_DESCRIPTION + 1), SCHEMA, read_only=True)),
            "contract.toolkit.descriptions",
        ),
        (
            type("Broken", (Good,), {"oauth": OAuthSpec(provider="g", authorize_url="http://a", token_url="https://t")}),
            "contract.toolkit.oauth",
        ),
        (type("Broken", (Good,), {"name": "Not Valid"}), "contract.toolkit.name"),
        (type("Broken", (Good,), {"version": "one"}), "contract.version-semver"),
    ],
)
def test_broken_declarations_fail(cls, check_id):
    assert check_id in failed_ids(cls)


def test_tools_raising_fails():
    def tools(self):
        raise RuntimeError("no catalog")

    cls = type("Broken", (Good,), {"tools": tools})
    assert {"contract.toolkit.tools", "contract.toolkit.call-no-creds"} <= failed_ids(cls)


def test_call_tool_raising_fails():
    def call_tool(self, slug, arguments, config):
        raise KeyError(slug)

    cls = type("Broken", (Good,), {"call_tool": call_tool})
    assert {"contract.toolkit.call-unknown", "contract.toolkit.call-no-creds"} <= failed_ids(cls)


def test_call_tool_succeeding_without_creds_fails():
    cls = type("Broken", (Good,), {"validate_config": lambda self, config: True})
    ids = failed_ids(cls)
    assert "contract.toolkit.call-no-creds" in ids
    assert "contract.toolkit.validate-config-no-creds" in ids


def test_network_without_creds_fails():
    class Networked(Good):
        def call_tool(self, slug, arguments, config):
            # Dials out before checking credentials.
            socket.create_connection(("api.example.com", 443))
            return ToolResult.fail("unreachable")

    results = run_toolkit_contract_checks(Networked)
    failed = {r.id: r.message for r in failures(results)}
    assert "network" in failed["contract.toolkit.call-no-creds"]


def _listing(fn) -> type:
    return type("Listing", (Good,), {"available_tools": lambda self, config=None: fn(self, config)})


def _boom(self, config):
    raise KeyError("enabled_tiers")


def _dial(self, config):
    socket.create_connection(("api.example.com", 443))
    return self.tools()


@pytest.mark.parametrize(
    ("fn", "expected"),
    [
        (_boom, "available_tools(None) raised KeyError"),
        (_dial, "network in available_tools(None)"),
        (lambda self, config: tuple(self.tools()), "must return a list of ToolSpec"),
        (lambda self, config: [{"slug": "GOOD_GET"}], "must return a list of ToolSpec"),
        (lambda self, config: [ToolSpec("GOOD_NEW", "x", SCHEMA, read_only=True)], "outside the catalog: GOOD_NEW"),
        (lambda self, config: self.tools() if config is None else _boom(self, config), "available_tools({}) raised"),
    ],
)
def test_broken_available_tools_fails(fn, expected):
    failed = {r.id: r.message for r in failures(run_toolkit_contract_checks(_listing(fn)))}
    assert expected in failed["contract.toolkit.available-tools"]


def test_a_filtered_listing_passes():
    cls = _listing(lambda self, config: [t for t in self.tools() if t.slug == "GOOD_GET"])
    assert failed_ids(cls) == set()
    assert "contract.toolkit.available-tools" in {r.id for r in run_toolkit_contract_checks(Good)}


def test_network_in_init_short_circuits():
    class Dialer(Good):
        def __init__(self):
            socket.getaddrinfo("api.example.com", 443)

    assert failed_ids(Dialer) == {"contract.instantiate"}


def test_entry_point_name_must_equal_toolkit_name():
    assert "contract.toolkit.entry-point-name" in failed_ids(Good, "good-toolkit")
    assert "contract.toolkit.entry-point-name" not in failed_ids(Good, "good")


def test_non_toolkit_class_fails():
    assert failed_ids(MockSource) == {"contract.toolkit.base-class"}


def test_oauth_extras_may_not_set_flow_params():
    spec = OAuthSpec(
        provider="g",
        authorize_url="https://a.example",
        token_url="https://t.example",
        extra_authorize_params={"state": "fixed", "access_type": "offline"},
    )
    results = run_toolkit_contract_checks(type("Broken", (Good,), {"oauth": spec}))
    failed = {r.id: r.message for r in failures(results)}
    assert "state" in failed["contract.toolkit.oauth"]
    assert "access_type" not in failed["contract.toolkit.oauth"]


class _OpenKit(Good):
    """A no-auth toolkit: tools without required args really run."""

    auth_type = AuthType.NONE
    oauth = None

    def tools(self) -> list[ToolSpec]:
        return [
            ToolSpec("GOOD_GET", "Get a thing.", SCHEMA, read_only=True),
            ToolSpec("GOOD_PING", "Ping.", {"type": "object", "properties": {}}, read_only=True),
        ]


def test_auth_none_toolkit_passes_call_no_creds():
    assert failed_ids(_OpenKit) == set()


def test_auth_none_toolkit_still_probes_required_args():
    class Sloppy(_OpenKit):
        def call_tool(self, slug, arguments, config):
            if slug == "GOOD_GET":
                return ToolResult.ok({})  # skips argument validation
            return super().call_tool(slug, arguments, config)

    assert "contract.toolkit.call-no-creds" in failed_ids(Sloppy)


def test_non_canonical_error_code_fails():
    class Odd(Good):
        def call_tool(self, slug, arguments, config):
            return ToolResult(successful=False, error="nope", error_code="auth_failed")

    assert {"contract.toolkit.call-unknown", "contract.toolkit.call-no-creds"} <= failed_ids(Odd)


def test_oauth2_without_spec_is_a_warning_only():
    cls = type("NoSpec", (Good,), {"oauth": None})
    results = run_toolkit_contract_checks(cls)
    warning = next(r for r in results if r.id == "contract.toolkit.oauth-declared")
    assert not warning.passed and warning.severity is Severity.WARNING
    assert failures(results) == []


def test_generated_suite_fails_on_broken_toolkit():
    suite_cls = toolkit_compliance_suite(_with_tools(ToolSpec("OTHER_GET", "x", SCHEMA, read_only=True)))
    result = unittest.TestResult()
    unittest.TestLoader().loadTestsFromTestCase(suite_cls).run(result)
    failed = {test.id().rsplit(".", 1)[-1] for test, _ in result.failures}
    assert failed == {"test_tool_catalog"}
    assert suite_cls.__name__ == "BrokenCompliance"


# --------------------------------------------------------------------------
# Router metadata: card, category, card text, tool scopes/risk, slug prefixes
# --------------------------------------------------------------------------


class Described(Good):
    category = "files"
    keywords = ("files", "folders")
    examples = ("find my report", "share the budget folder")

    def tools(self) -> list[ToolSpec]:
        return [
            ToolSpec("GOOD_GET", "Get a thing.", SCHEMA, read_only=True, required_scopes=("files.read",)),
            ToolSpec(
                "GOOD_PUT",
                "Put a thing.",
                SCHEMA,
                read_only=False,
                risk="destructive",
                required_scopes=("https://www.googleapis.com/auth/drive", "read:org", "Files.ReadWrite.All"),
            ),
        ]


def test_a_toolkit_setting_no_new_fields_passes():
    # Good sets none of category/keywords/examples/required_scopes/risk.
    assert failed_ids(Good, "good") == set()
    assert Good.card()["category"] == "other"


def test_a_described_toolkit_passes():
    assert failed_ids(Described, "good") == set()


class _BadCard(Good):
    @classmethod
    def card(cls):
        return {"name": "good"}


class _CardRaises(Good):
    @classmethod
    def card(cls):
        raise RuntimeError("boom")


class _CardNotJSON(Good):
    @classmethod
    def card(cls):
        return {**super().card(), "keywords": [object()]}


class _CardWrongName(Good):
    @classmethod
    def card(cls):
        return {**super().card(), "name": "other"}


class _CardDials(Good):
    @classmethod
    def card(cls):
        socket.create_connection(("example.com", 443))
        return super().card()


@pytest.mark.parametrize(
    ("cls", "check_id"),
    [
        (_BadCard, "contract.toolkit.card"),
        (_CardRaises, "contract.toolkit.card"),
        (_CardNotJSON, "contract.toolkit.card"),
        (_CardWrongName, "contract.toolkit.card"),
        (_CardDials, "contract.toolkit.card"),
        (type("Cat", (Good,), {"category": "music"}), "contract.toolkit.category"),
        (type("Cat", (Good,), {"category": None}), "contract.toolkit.category"),
        (type("Kw", (Good,), {"keywords": "files"}), "contract.toolkit.card-text"),
        (type("Kw", (Good,), {"keywords": ("files", "")}), "contract.toolkit.card-text"),
        (type("Kw", (Good,), {"keywords": ("x" * 41,)}), "contract.toolkit.card-text"),
        (type("Ex", (Good,), {"examples": ("only one",)}), "contract.toolkit.card-text"),
        (type("Ex", (Good,), {"examples": ("a", "b", "c", "d", "e")}), "contract.toolkit.card-text"),
        (type("Ex", (Good,), {"examples": ("fine", "x" * 121)}), "contract.toolkit.card-text"),
        (type("Ex", (Good,), {"examples": "find my report"}), "contract.toolkit.card-text"),
        (
            _with_tools(ToolSpec("GOOD_GET", "x", SCHEMA, read_only=True, required_scopes=("files read",))),
            "contract.toolkit.tool-metadata",
        ),
        (
            _with_tools(ToolSpec("GOOD_GET", "x", SCHEMA, read_only=True, required_scopes=("a,b",))),
            "contract.toolkit.tool-metadata",
        ),
        (
            _with_tools(ToolSpec("GOOD_GET", "x", SCHEMA, read_only=True, required_scopes=("",))),
            "contract.toolkit.tool-metadata",
        ),
        (
            _with_tools(ToolSpec("GOOD_GET", "x", SCHEMA, read_only=True, required_scopes=(3,))),  # type: ignore[arg-type]
            "contract.toolkit.tool-metadata",
        ),
        (
            _with_tools(ToolSpec("GOOD_GET", "x", SCHEMA, read_only=True, required_scopes=("a", "a"))),
            "contract.toolkit.tool-metadata",
        ),
        # ToolSpec refuses these at construction; validate still catches a
        # spec whose risk was changed afterwards.
        (
            _with_tools(_forced_risk(ToolSpec("GOOD_GET", "x", SCHEMA, read_only=True), "write")),
            "contract.toolkit.tool-metadata",
        ),
        (
            _with_tools(_forced_risk(ToolSpec("GOOD_GET", "x", SCHEMA, read_only=False, destructive=True), "write")),
            "contract.toolkit.tool-metadata",
        ),
        (
            _with_tools(_forced_risk(ToolSpec("GOOD_GET", "x", SCHEMA, read_only=False), "read")),
            "contract.toolkit.tool-metadata",
        ),
    ],
)
def test_broken_router_metadata_fails(cls, check_id):
    assert check_id in failed_ids(cls)


def test_raising_the_risk_above_the_flags_passes():
    kit = _with_tools(ToolSpec("GOOD_SEND", "x", SCHEMA, read_only=False, risk="destructive"))
    assert "contract.toolkit.tool-metadata" not in failed_ids(kit)


def test_scopes_the_default_consent_does_not_request_warn():
    spec = OAuthSpec(
        provider="good", authorize_url="https://a.example", token_url="https://t.example", default_scopes=("files.read",)
    )
    kit = _with_tools(
        ToolSpec("GOOD_GET", "x", SCHEMA, read_only=True, required_scopes=("files.read",)),
        ToolSpec("GOOD_PUT", "x", SCHEMA, read_only=False, required_scopes=("files.write",)),
        oauth=spec,
    )
    (result,) = [r for r in run_toolkit_contract_checks(kit) if r.id == "contract.toolkit.scopes-requested"]
    assert (result.passed, result.severity) == (False, Severity.WARNING)
    assert "files.write" in result.message
    assert "contract.toolkit.scopes-requested" not in failed_ids(kit)  # a warning only

    implying = type("Implying", (kit,), {"scope_granted": lambda self, s, granted: s.startswith("files.")})
    (result,) = [r for r in run_toolkit_contract_checks(implying) if r.id == "contract.toolkit.scopes-requested"]
    assert result.passed


def _prefixed(kit_name: str, prefix: str) -> type:
    slug = f"{prefix}GET"
    return type(
        f"Kit_{kit_name}",
        (Good,),
        {
            "name": kit_name,
            "slug_prefix": prefix,
            "tools": lambda self: [ToolSpec(slug, "Get.", SCHEMA, read_only=True)],
        },
    )


@pytest.mark.parametrize(
    ("others", "clash"),
    [
        ({"other": ("other", "OTHER_")}, False),
        ({"twin": ("twin", "GOOD_")}, True),
        ({"short": ("short", "GO_")}, False),
        ({"goods": ("goods", "GOODS_")}, False),  # neither starts the other
        ({"inner": ("inner", "GOOD_X_")}, True),
    ],
)
def test_slug_prefix_must_be_unique_across_installed(others, clash):
    installed = {name: _prefixed(n, p) for name, (n, p) in others.items()}
    installed["good"] = Good
    results = run_toolkit_contract_checks(Good, "good", installed=installed)
    (result,) = [r for r in results if r.id == "contract.toolkit.slug-prefix-unique"]
    assert result.passed is not clash
    if clash:
        assert "GOOD_" in result.message


def test_slug_prefix_check_needs_the_installed_set():
    ids = {r.id for r in run_toolkit_contract_checks(Good, "good")}
    assert "contract.toolkit.slug-prefix-unique" not in ids


def test_generated_suite_runs_the_router_checks():
    suite = toolkit_compliance_suite(type("Cat", (Good,), {"category": "music"}))
    result = unittest.TextTestRunner(stream=open(os.devnull, "w")).run(  # noqa: SIM115
        unittest.defaultTestLoader.loadTestsFromTestCase(suite)
    )
    assert [test.id().rsplit(".", 1)[-1] for test, _ in result.failures] == ["test_category"]
