"""Compliance test suite for connector validation."""

from __future__ import annotations

import re
import unittest
from collections.abc import Iterator

from aisquare.pipe.core.connector import AuthType, SinkConnector, SourceConnector
from aisquare.pipe.core.envelope import DataEnvelope, MetaField, PushResult


def _is_valid_semver(version: str) -> bool:
    """Check if a string looks like a semver version."""
    return bool(re.match(r"^\d+\.\d+\.\d+", version))


def _is_valid_mime(mime: str) -> bool:
    """Check if a string looks like a MIME type (contains a /)."""
    return "/" in mime


def connector_compliance_suite(connector_class: type) -> type:
    """Generate a test class that validates any connector against the spec.

    Usage::

        class TestMyConnector(connector_compliance_suite(MyConnector)):
            pass
    """

    is_source = issubclass(connector_class, SourceConnector)
    is_sink = issubclass(connector_class, SinkConnector)

    class ComplianceTests(unittest.TestCase):
        """Auto-generated compliance tests."""

        connector_cls = connector_class

        def _make_instance(self):
            return self.connector_cls()

        def test_has_name(self):
            inst = self._make_instance()
            self.assertIsInstance(inst.name, str)
            self.assertTrue(len(inst.name) > 0, "name must be non-empty")

        def test_has_version(self):
            inst = self._make_instance()
            self.assertIsInstance(inst.version, str)
            self.assertTrue(
                _is_valid_semver(inst.version),
                f"version '{inst.version}' is not valid semver",
            )

        def test_has_auth_type(self):
            inst = self._make_instance()
            self.assertIsInstance(inst.auth_type, AuthType)

        def test_metadata_spec_values(self):
            inst = self._make_instance()
            if hasattr(inst, "metadata_spec") and inst.metadata_spec:
                for key, val in inst.metadata_spec.items():
                    self.assertIsInstance(
                        val,
                        MetaField,
                        f"metadata_spec['{key}'] must be a MetaField",
                    )

        if is_source:

            def test_has_output_types(self):
                inst = self._make_instance()
                self.assertIsInstance(inst.output_types, list)
                self.assertTrue(
                    len(inst.output_types) > 0,
                    "output_types must be non-empty",
                )
                for t in inst.output_types:
                    self.assertIsInstance(t, str)
                    self.assertTrue(
                        _is_valid_mime(t),
                        f"output_type '{t}' is not a valid MIME type",
                    )

            def test_pull_returns_iterator(self):
                inst = self._make_instance()
                result = inst.pull({})
                self.assertTrue(
                    isinstance(result, Iterator) or hasattr(result, "__next__"),
                    "pull() must return an iterator/generator",
                )

            def test_validate_config_implemented(self):
                inst = self._make_instance()
                result = inst.validate_config({})
                self.assertIsInstance(result, bool)

        if is_sink:

            def test_has_input_types(self):
                inst = self._make_instance()
                self.assertIsInstance(inst.input_types, list)
                self.assertTrue(
                    len(inst.input_types) > 0,
                    "input_types must be non-empty",
                )
                for t in inst.input_types:
                    self.assertIsInstance(t, str)
                    self.assertTrue(
                        _is_valid_mime(t),
                        f"input_type '{t}' is not a valid MIME type",
                    )

            def test_push_returns_push_result(self):
                inst = self._make_instance()
                envelope = DataEnvelope(
                    content_type="text/plain",
                    data="test",
                    source_id="compliance-test",
                )
                result = inst.push(envelope, {})
                self.assertIsInstance(result, PushResult)

            def test_sink_validate_config_implemented(self):
                inst = self._make_instance()
                result = inst.validate_config({})
                self.assertIsInstance(result, bool)

    ComplianceTests.__name__ = f"{connector_class.__name__}Compliance"
    ComplianceTests.__qualname__ = f"{connector_class.__name__}Compliance"
    return ComplianceTests
