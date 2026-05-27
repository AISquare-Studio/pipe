"""Parity harness — confirms the pipe shaper produces the same TraceBatch
as the in-gateway shaper in the AISquare Explainability SDK.

Strategy:

1. **Frozen reference** (always runs): each fixture has a stored
   ``expected/<name>.json`` file. The pipe shaper output is compared
   byte-for-byte against it. Frozen references are regenerated via
   ``connectors/n8n/scripts/regen_parity_fixtures.py`` against the SDK
   source whenever the SDK shaper changes — that script is the bridge
   that keeps the two in sync.

2. **Live SDK comparison** (runs only when the SDK is sibling-checked
   out at ``../AISquare-Explainability-SDK``): imports the SDK shaper
   and compares its output against the pipe shaper's. Skipped in
   environments without the SDK.

If both checks pass: a trace produced by the pipe-n8n container is
byte-equivalent to one produced by the legacy in-gateway poller, so the
gateway's structural worker / FE / policy engine see identical data.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

import pytest

from aisquare_pipe_n8n.spans import execution_to_trace_batch

from .fixtures import all_fixtures

EXPECTED_DIR = Path(__file__).parent / "expected"


# ─── Frozen reference: always runs ───────────────────────────────────────


@pytest.mark.parametrize("name,execution", list(all_fixtures().items()))
def test_pipe_shaper_matches_frozen_reference(
    name: str, execution: dict[str, Any]
) -> None:
    """Pipe shaper output must match the checked-in frozen reference."""
    expected_path = EXPECTED_DIR / f"{name}.json"
    if not expected_path.exists():
        pytest.skip(
            f"Frozen reference {expected_path} missing — regenerate via "
            "`python connectors/n8n/scripts/regen_parity_fixtures.py`"
        )

    expected = json.loads(expected_path.read_text())
    _, actual = execution_to_trace_batch(execution)

    # Normalise to JSON-round-tripped dicts so dict ordering doesn't trip the
    # assertion — semantically the gateway treats spans as a set keyed by
    # span_id.
    actual_norm = json.loads(json.dumps(actual, sort_keys=True))
    expected_norm = json.loads(json.dumps(expected, sort_keys=True))
    assert actual_norm == expected_norm, (
        f"shape drift in fixture {name}; "
        "regenerate via connectors/n8n/scripts/regen_parity_fixtures.py if the SDK changed, "
        "or fix the pipe shaper if it diverged."
    )


# ─── Live SDK comparison: opt-in ─────────────────────────────────────────


def _try_import_sdk_shaper():
    """Try to import the SDK's execution_to_spans. Returns the function or
    None when the SDK isn't reachable.

    Layout assumed by the default sibling-checkout path:
        <workspace>/pipe/connectors/n8n/tests/parity/test_sdk_parity.py
        <workspace>/AISquare-Explainability-SDK/
    """
    # parents[4] is the pipe repo root; its parent is the workspace that holds
    # the sibling SDK checkout.
    sibling_default = (
        Path(__file__).resolve().parents[4].parent / "AISquare-Explainability-SDK"
    )
    sdk_root = os.environ.get("AISQUARE_SDK_ROOT", str(sibling_default))
    sdk_root_path = Path(sdk_root)
    if not (sdk_root_path / "gateway" / "connectors" / "n8n.py").exists():
        return None
    if str(sdk_root_path) not in sys.path:
        sys.path.insert(0, str(sdk_root_path))
    try:
        # The SDK module is `gateway.connectors.n8n` — direct import works
        # once the SDK root is on sys.path. We import lazily because some
        # SDK transitive deps (httpx etc.) may not be installed.
        from gateway.connectors.n8n import execution_to_spans  # type: ignore
        return execution_to_spans
    except Exception:
        return None


SDK_SHAPER = _try_import_sdk_shaper()


@pytest.mark.skipif(
    SDK_SHAPER is None,
    reason=(
        "AISquare-Explainability-SDK not importable. Set AISQUARE_SDK_ROOT or "
        "checkout the SDK alongside `pipe/` to enable the live parity check."
    ),
)
@pytest.mark.parametrize("name,execution", list(all_fixtures().items()))
def test_pipe_shaper_matches_sdk_shaper(
    name: str, execution: dict[str, Any]
) -> None:
    """Pipe shaper output must match the live SDK shaper byte-for-byte."""
    sdk_trace_id, sdk_spans = SDK_SHAPER(execution)  # type: ignore[misc]
    pipe_trace_id, pipe_batch = execution_to_trace_batch(execution)

    assert sdk_trace_id == pipe_trace_id, name
    assert pipe_trace_id == pipe_batch["trace_id"], name

    pipe_spans = pipe_batch["spans"]
    # Sort by span_id so dict iteration order doesn't matter.
    sdk_sorted = sorted(sdk_spans, key=lambda s: s["span_id"])
    pipe_sorted = sorted(pipe_spans, key=lambda s: s["span_id"])

    # Round-trip through JSON to drop any non-portable types and to normalise
    # internal dict ordering on attribute sub-maps.
    sdk_norm = json.loads(json.dumps(sdk_sorted, sort_keys=True, default=str))
    pipe_norm = json.loads(json.dumps(pipe_sorted, sort_keys=True, default=str))
    assert sdk_norm == pipe_norm, f"divergence on fixture {name}"
