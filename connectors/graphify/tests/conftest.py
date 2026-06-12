"""Shared fixtures: a recording fake `graphify` executable + a fake checkout.

The stub records every invocation (argv + the env vars that matter + cwd) to
``calls.jsonl`` beside itself and mimics the REAL per-command artifact
contract of graphifyy 0.8.36 — this is load-bearing: an earlier stub wrote
GRAPH_REPORT.md on every command and masked a production bug where `extract`
never writes the report (only `cluster-only`/`update` do).

  extract       -> writes graph.json ONLY; prints the telemetry line
  cluster-only  -> requires graph.json; writes GRAPH_REPORT.md
  update        -> writes BOTH artifacts (the keyless AST path)

Behavior is steered by *control files* touched next to the stub (the engine
scrubs the subprocess env, so env-var steering wouldn't reach it):

  FAIL_EXTRACT      -> `extract` exits 1 with a billing-style stderr
  FAIL_CLUSTER      -> `cluster-only` exits 1
  FAIL_UPDATE       -> `update` exits 1
  CLUSTER_NO_REPORT -> `cluster-only` exits 0 but writes nothing (drift sim)
  NO_ARTIFACTS      -> exit 0 but write nothing (sanity-gate trigger)
  BAD_JSON          -> write an unparseable graph.json
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

_STUB = '''#!/usr/bin/env python3
import json, os, sys
here = os.path.dirname(os.path.abspath(__file__))
interesting = ("PATH", "HOME", "GOOGLE_API_KEY", "AWS_REGION")
env = {k: v for k, v in os.environ.items() if k.endswith("_API_KEY") or k in interesting}
with open(os.path.join(here, "calls.jsonl"), "a") as fh:
    fh.write(json.dumps({"argv": sys.argv[1:], "env": env, "cwd": os.getcwd()}) + "\\n")
if "--version" in sys.argv:
    print("graphify 0.8.36-fake")
    raise SystemExit(0)
cmd = sys.argv[1] if len(sys.argv) > 1 else ""
out = os.path.join(os.getcwd(), "graphify-out")

def write_graph_json():
    os.makedirs(out, exist_ok=True)
    with open(os.path.join(out, "graph.json"), "w") as fh:
        if os.path.exists(os.path.join(here, "BAD_JSON")):
            fh.write("{not json")
        else:
            fh.write(json.dumps({"nodes": [{}] * 12, "links": [{}] * 34}))

def write_report():
    os.makedirs(out, exist_ok=True)
    with open(os.path.join(out, "GRAPH_REPORT.md"), "w") as fh:
        fh.write("# Graph report\\n\\nNodes: 12\\nEdges: 34\\nCommunities: 3\\n\\n(fake)\\n")

if os.path.exists(os.path.join(here, "NO_ARTIFACTS")):
    raise SystemExit(0)
if cmd == "extract":
    if os.path.exists(os.path.join(here, "FAIL_EXTRACT")):
        sys.stderr.write("enriched boom: credit balance is too low\\n")
        raise SystemExit(1)
    write_graph_json()
    with open(os.path.join(out, ".graphify_analysis.json"), "w") as fh:
        fh.write(json.dumps({"tokens": {"input": 9000, "output": 400}}))
    print("[graphify extract] wrote graph.json - 12 nodes, 34 edges (no clustering)")
    print("[graphify extract] tokens: 1,234 in / 567 out, est. cost: $0.0123")
    print("next: run graphify cluster-only . to generate GRAPH_REPORT.md")
elif cmd in ("cluster-only", "label"):
    if os.path.exists(os.path.join(here, "FAIL_CLUSTER")):
        sys.stderr.write("cluster boom\\n")
        raise SystemExit(1)
    if not os.path.exists(os.path.join(here, "CLUSTER_NO_REPORT")):
        write_report()
    print("Done - 3 communities. GRAPH_REPORT.md and graph.json updated.")
elif cmd == "update":
    if os.path.exists(os.path.join(here, "FAIL_UPDATE")):
        sys.stderr.write("update boom\\n")
        raise SystemExit(1)
    write_graph_json()
    write_report()
    print("ok")
else:
    print("ok")
'''


class FakeGraphify:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.bin = root / "graphify"
        self.bin.write_text(_STUB)
        self.bin.chmod(self.bin.stat().st_mode | stat.S_IEXEC)

    def touch(self, control: str) -> None:
        (self.root / control).write_text("")

    def calls(self) -> list[dict]:
        path = self.root / "calls.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


@pytest.fixture()
def stub(tmp_path) -> FakeGraphify:
    return FakeGraphify(tmp_path / "fakebin")


@pytest.fixture(autouse=True)
def _mkdir_fakebin(tmp_path):
    (tmp_path / "fakebin").mkdir(exist_ok=True)
    yield


@pytest.fixture()
def checkout(tmp_path) -> str:
    src = tmp_path / "checkout"
    src.mkdir()
    (src / "app.py").write_text("def main():\n    return 42\n")
    return str(src)


@pytest.fixture()
def scrub_canary(monkeypatch):
    """Plant ambient keys that MUST NOT leak into the stub's env."""
    monkeypatch.setenv("GOOGLE_API_KEY", "ambient-google")
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "ambient-anthropic")
    yield
