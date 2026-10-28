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


def test_mock_toolkit_is_registered():
    assert "mock" in {dc.ep_name for dc in _DISCOVERED}


@pytest.mark.parametrize("dc", _DISCOVERED, ids=lambda dc: dc.ep_name)
def test_contract(dc):
    if dc.cls is None:
        pytest.fail(f"entry point {dc.ep_name} failed to load: {dc.load_error}")
    failures = [
        r
        for r in run_toolkit_contract_checks(dc.cls, dc.ep_name)
        if not r.passed and r.severity is Severity.ERROR
    ]
    assert not failures, "; ".join(f"[{r.id}] {r.message}" for r in failures)
