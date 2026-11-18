"""[snowflake] shares the sqlglot floor of every SQL-guard extra, so CI runs
connectors/snowflake's suite at it (scripts/dep_floor.py, validate.yml)."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "dep_floor.py"


def _load_script():
    spec = importlib.util.spec_from_file_location("dep_floor_snowflake", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses resolve their module by name
    spec.loader.exec_module(module)
    return module


df = _load_script()


def test_snowflake_names_sqlglot_at_the_shared_floor_and_ci_runs_its_suite():
    """One specifier across the extras (``floor`` raises otherwise), at 26.25
    or later; the Snowflake suite is among those CI reruns at the floor."""
    found = df.floor(REPO_ROOT, "sqlglot")
    assert tuple(int(p) for p in found.version.split(".")[:2]) >= (26, 25)
    assert "snowflake" in found.extras
    assert "snowflake" in [p.name for p in df.connectors(REPO_ROOT, found)]
