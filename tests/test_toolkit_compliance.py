"""The toolkit compliance suite against MockToolkit, and the toolkit contract
checks (testing/validation.py) against deliberately broken toolkits."""

from __future__ import annotations

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
        auth_type = AuthType.NONE

        def run_tool(self, tool, arguments, config):
            socket.create_connection(("api.example.com", 443))
            return ToolResult.ok({})

    results = run_toolkit_contract_checks(Networked)
    failed = {r.id: r.message for r in failures(results)}
    assert "network" in failed["contract.toolkit.call-no-creds"]


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
