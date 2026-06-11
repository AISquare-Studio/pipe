"""Compliance test suite for connector validation.

The assertion logic lives in :mod:`aisquare.pipe.testing.validation` (pure
check functions shared with ``pipe validate``); this module wraps it in the
unittest API that connector test suites subclass. Method names are stable —
connector suites and CI reference them — and new strictness only ever lands
in new method names.
"""

from __future__ import annotations

import unittest

from aisquare.pipe.core.connector import SinkConnector, SourceConnector
from aisquare.pipe.testing import validation
from aisquare.pipe.testing.validation import CheckResult, failures


def _assert_checks(case: unittest.TestCase, results: list[CheckResult]) -> None:
    failed = failures(results)
    if failed:
        case.fail("; ".join(f"[{r.id}] {r.message}" for r in failed))


def connector_compliance_suite(connector_class: type) -> type:
    """Generate a test class that validates any connector against the spec.

    Usage::

        class TestMyConnector(connector_compliance_suite(MyConnector)):
            pass

    The suite is credential-free by design: behavioral checks run under a
    socket guard, so a connector that needs network (or credentials) to
    answer ``validate_config({})`` / ``pull({})`` / ``push(..., {})`` fails.
    """

    is_source = issubclass(connector_class, SourceConnector)
    is_sink = issubclass(connector_class, SinkConnector)

    class ComplianceTests(unittest.TestCase):
        """Auto-generated compliance tests."""

        connector_cls = connector_class

        def test_instantiation(self):
            _assert_checks(self, validation.check_instantiation(self.connector_cls))

        def test_has_name(self):
            _assert_checks(self, validation.check_name(self.connector_cls))

        def test_has_version(self):
            _assert_checks(self, validation.check_version(self.connector_cls))

        def test_has_auth_type(self):
            _assert_checks(self, validation.check_auth_type(self.connector_cls))

        def test_metadata_spec_values(self):
            _assert_checks(self, validation.check_metadata_spec(self.connector_cls))

        if is_source:

            def test_has_output_types(self):
                _assert_checks(self, validation.check_output_types(self.connector_cls))

            def test_pull_returns_iterator(self):
                results = [
                    r
                    for r in validation.check_pull_contract(self.connector_cls)
                    if r.id == "contract.source.pull-iterator"
                ]
                _assert_checks(self, results)

            def test_pull_no_creds(self):
                _assert_checks(self, validation.check_pull_contract(self.connector_cls))

            def test_validate_config_implemented(self):
                # Historical name; now also enforces the no-creds rules.
                _assert_checks(
                    self, validation.check_source_validate_config(self.connector_cls)
                )

        if is_sink:

            def test_has_input_types(self):
                _assert_checks(self, validation.check_input_types(self.connector_cls))

            def test_push_returns_push_result(self):
                _assert_checks(self, validation.check_push_contract(self.connector_cls))

            def test_sink_validate_config_implemented(self):
                _assert_checks(
                    self, validation.check_sink_validate_config(self.connector_cls)
                )

    ComplianceTests.__name__ = f"{connector_class.__name__}Compliance"
    ComplianceTests.__qualname__ = f"{connector_class.__name__}Compliance"
    return ComplianceTests
