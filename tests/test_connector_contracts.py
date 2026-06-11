"""Layer A contract checks over every installed connector entry point.

Coverage is environment-dependent by design: a venv with only the framework
installed validates the mock connectors; a full dev venv validates every
loadable connector. A broken editable install fails here loudly — that is
intended surfacing, and the message carries the load error.
"""

from __future__ import annotations

import pytest

from aisquare.pipe.testing.validation import (
    Severity,
    discover_connector_entry_points,
    run_contract_checks,
)

_DISCOVERED = discover_connector_entry_points()


@pytest.mark.parametrize("dc", _DISCOVERED, ids=lambda dc: dc.ep_name)
def test_contract(dc):
    if dc.cls is None:
        pytest.fail(f"entry point {dc.ep_name} failed to load: {dc.load_error}")
    failures = [
        r
        for r in run_contract_checks(dc.cls, dc.ep_name)
        if not r.passed and r.severity is Severity.ERROR
    ]
    assert not failures, "; ".join(f"[{r.id}] {r.message}" for r in failures)
