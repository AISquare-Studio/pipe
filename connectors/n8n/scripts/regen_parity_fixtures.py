#!/usr/bin/env python3
"""Regenerate the frozen parity references under
``connectors/n8n/tests/parity/expected/``.

Runs the SDK's ``gateway.connectors.n8n.execution_to_spans`` against each
fixture in ``connectors/n8n/tests/parity/fixtures.py``, wraps the
(trace_id, spans) result into a TraceBatch dict (matching ``pipe`` shape),
and writes it as JSON.

The frozen references are the source of truth for the always-on parity
test ``tests.parity.test_sdk_parity::test_pipe_shaper_matches_frozen_reference``
inside the n8n connector.

Usage (from the repo root):
    python connectors/n8n/scripts/regen_parity_fixtures.py
    AISQUARE_SDK_ROOT=/path/to/sdk python connectors/n8n/scripts/regen_parity_fixtures.py
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path


def main() -> int:
    # connector_root = connectors/n8n; repo_root = pipe checkout
    connector_root = Path(__file__).resolve().parents[1]
    repo_root = connector_root.parents[1]
    default_sdk = repo_root.parent / "AISquare-Explainability-SDK"
    sdk_root = Path(os.environ.get("AISQUARE_SDK_ROOT", str(default_sdk)))
    if not (sdk_root / "gateway" / "connectors" / "n8n.py").exists():
        print(
            f"error: SDK shaper not found at {sdk_root}/gateway/connectors/n8n.py",
            file=sys.stderr,
        )
        print(
            "       set AISQUARE_SDK_ROOT to the SDK checkout directory.",
            file=sys.stderr,
        )
        return 1

    sys.path.insert(0, str(sdk_root))
    sys.path.insert(0, str(connector_root / "tests"))

    from gateway.connectors.n8n import execution_to_spans  # type: ignore  # noqa: E402
    from parity.fixtures import all_fixtures  # type: ignore  # noqa: E402

    expected_dir = connector_root / "tests" / "parity" / "expected"
    expected_dir.mkdir(parents=True, exist_ok=True)

    written = 0
    for name, execution in all_fixtures().items():
        trace_id, spans = execution_to_spans(execution)
        trace_batch = {"trace_id": trace_id, "spans": spans}
        out_path = expected_dir / f"{name}.json"
        out_path.write_text(
            json.dumps(trace_batch, indent=2, sort_keys=True, default=str)
        )
        print(f"wrote {out_path.relative_to(repo_root)}")
        written += 1

    print(
        f"\nregenerated {written} frozen reference(s) in "
        f"{expected_dir.relative_to(repo_root)}/"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
