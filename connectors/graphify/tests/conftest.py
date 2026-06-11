"""Shared fixtures: a recording fake `graphify` executable + a fake checkout.

The stub records every invocation (argv + the env vars that matter + cwd) to
``calls.jsonl`` beside itself, and writes a canned ``graphify-out/`` into the
cwd. Behavior is steered by *control files* touched next to the stub (the
engine scrubs the subprocess env, so env-var steering wouldn't reach it):

  FAIL_EXTRACT  -> `extract` exits 1 with a billing-style stderr
  FAIL_UPDATE   -> `update` exits 1
  NO_ARTIFACTS  -> exit 0 but write nothing (sanity-gate trigger)
  BAD_JSON      -> write an unparseable graph.json
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
if cmd == "extract" and os.path.exists(os.path.join(here, "FAIL_EXTRACT")):
    sys.stderr.write("enriched boom: credit balance is too low\\n")
    raise SystemExit(1)
if cmd == "update" and os.path.exists(os.path.join(here, "FAIL_UPDATE")):
    sys.stderr.write("update boom\\n")
    raise SystemExit(1)
if os.path.exists(os.path.join(here, "NO_ARTIFACTS")):
    raise SystemExit(0)
out = os.path.join(os.getcwd(), "graphify-out")
os.makedirs(out, exist_ok=True)
with open(os.path.join(out, "GRAPH_REPORT.md"), "w") as fh:
    fh.write("# Graph report\\n\\nNodes: 12\\nEdges: 34\\nCommunities: 3\\n\\n(fake)\\n")
with open(os.path.join(out, "graph.json"), "w") as fh:
    if os.path.exists(os.path.join(here, "BAD_JSON")):
        fh.write("{not json")
    else:
        fh.write(json.dumps({"nodes": [{}] * 12, "links": [{}] * 34}))
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
