"""Layer A contract checks over every installed toolkit entry point
(environment-dependent, like test_connector_contracts.py)."""

from __future__ import annotations

import pytest

from aisquare.pipe.testing.validation import (
    Severity,
    discover_toolkit_entry_points,
    run_toolkit_contract_checks,
)

_DISCOVERED = discover_toolkit_entry_points()
_INSTALLED = {dc.ep_name: dc.cls for dc in _DISCOVERED if dc.cls is not None}


def test_mock_toolkit_is_registered():
    assert "mock" in {dc.ep_name for dc in _DISCOVERED}


def _require_installed(dc):
    """A toolkit whose aisquare-pipe[<name>] extra isn't installed is not
    installed (D8): skip it. Any other load failure fails."""
    if dc.cls is None and dc.missing_requirements:
        pytest.skip(f"not installed: its extra needs {', '.join(dc.missing_requirements)}")
    if dc.cls is None:
        pytest.fail(f"entry point {dc.ep_name} failed to load: {dc.load_error}")


@pytest.mark.parametrize("dc", _DISCOVERED, ids=lambda dc: dc.ep_name)
def test_contract(dc):
    _require_installed(dc)
    failures = [
        r
        for r in run_toolkit_contract_checks(dc.cls, dc.ep_name, installed=_INSTALLED)
        if not r.passed and r.severity is Severity.ERROR
    ]
    assert not failures, "; ".join(f"[{r.id}] {r.message}" for r in failures)


@pytest.mark.parametrize("dc", _DISCOVERED, ids=lambda dc: dc.ep_name)
def test_card_and_slug_prefix(dc):
    _require_installed(dc)
    ids = {r.id: r for r in run_toolkit_contract_checks(dc.cls, dc.ep_name, installed=_INSTALLED)}
    assert ids["contract.toolkit.card"].passed, ids["contract.toolkit.card"].message
    assert ids["contract.toolkit.slug-prefix-unique"].passed, ids["contract.toolkit.slug-prefix-unique"].message
