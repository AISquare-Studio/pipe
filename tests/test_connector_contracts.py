"""Layer A contract checks over every installed connector entry point.

Coverage is environment-dependent by design: a venv with only the framework
installed validates the mock connectors and every connector that needs no
extra; a full dev venv (``.[full,dev]``) validates every connector. A
connector whose ``aisquare-pipe[<name>]`` extra isn't installed is not
installed (D8) and skips, naming the extra. Any other load failure fails here
loudly — that is intended surfacing, and the message carries the load error.
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
    if dc.cls is None and dc.missing_requirements:
        pytest.skip(f"not installed: its extra needs {', '.join(dc.missing_requirements)}")
    if dc.cls is None:
        pytest.fail(f"entry point {dc.ep_name} failed to load: {dc.load_error}")
    failures = [
        r
        for r in run_contract_checks(dc.cls, dc.ep_name)
        if not r.passed and r.severity is Severity.ERROR
    ]
    assert not failures, "; ".join(f"[{r.id}] {r.message}" for r in failures)
