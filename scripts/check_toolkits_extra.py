#!/usr/bin/env python3
"""Check that the root ``[toolkits]`` extra pulls in every toolkit connector, and nothing else.

Every connector ships inside the one ``aisquare-pipe`` distribution (D8), so
``[toolkits]`` is a self-reference, ``aisquare-pipe[github,gmail,...]``: the
extras of every connector that registers an ``aisquare_pipe.toolkits`` entry
point in the root ``pyproject.toml``. A host (pipe-as-service) installs it to
get every toolkit's deps without naming them one by one. A toolkit missing
from it can still be imported but may lack its deps, and an entry that has no
toolkit installs deps for nothing.

A toolkit connector is ``connectors/<dir>/``, module ``aisquare_pipe_<dir>``,
with at least one ``aisquare_pipe.toolkits`` entry point pointing into that
module. Its extra is named ``<dir>``.

Every toolkit entry-point name must match ``TOOLKIT_NAME_RULE`` (no hyphens):
it is the connector id a host routes on, and pipe-as-service could not serve
a hyphenated one.

Reads the pyproject files only (no install, no import, stdlib only):

    python scripts/check_toolkits_extra.py           # check; exit 1 on a problem
    python scripts/check_toolkits_extra.py --dirs    # print each toolkit connector's dir
    python scripts/check_toolkits_extra.py --root R  # check another checkout
"""

from __future__ import annotations

import argparse
import re
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path

EXTRA = "toolkits"
FRAMEWORK = "aisquare-pipe"
TOOLKIT_GROUP = "aisquare_pipe.toolkits"
MODULE_PREFIX = "aisquare_pipe_"
# aisquare.pipe.core.tools.TOOLKIT_NAME_RULE, copied: this script runs before
# anything is installed. tests/test_check_toolkits_extra.py keeps them equal.
TOOLKIT_NAME_RULE = r"^[a-z][a-z0-9_]{0,63}$"

# A requirement on this distribution's own extras: aisquare-pipe[a,b] (any
# version spec after it is allowed but pointless).
_SELF_REFERENCE = re.compile(
    r"^\s*(?P<name>[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?)\s*\[(?P<extras>[^\]]*)\]\s*(?P<rest>[^;@]*)$"
)


@dataclass(frozen=True)
class ToolkitConnector:
    dir_name: str  # connectors/<dir_name>; its extra has the same name
    path: Path
    toolkits: tuple[str, ...]  # its aisquare_pipe.toolkits entry-point names

    @property
    def extra(self) -> str:
        return canonical(self.dir_name)


def canonical(name: str) -> str:
    """A distribution or extra name as pip compares them (PEP 503 / 685)."""
    return re.sub(r"[-_.]+", "-", name).lower()


def _load(path: Path) -> dict:
    with path.open("rb") as f:
        return tomllib.load(f)


def toolkit_connectors(root: Path) -> list[ToolkitConnector]:
    """Every connector with an ``aisquare_pipe.toolkits`` entry point in the
    root pyproject, sorted by directory. An entry point whose module isn't
    ``aisquare_pipe_<dir>`` for some ``connectors/<dir>/`` (the framework's
    mock, say) belongs to no connector."""
    project = _load(root / "pyproject.toml").get("project", {})
    entry_points = project.get("entry-points", {}).get(TOOLKIT_GROUP) or {}
    dirs = {
        f"{MODULE_PREFIX}{d.name.replace('-', '_')}": d
        for d in sorted((root / "connectors").iterdir())
        if d.is_dir() and not d.name.startswith((".", "_"))
    }
    names: dict[str, list[str]] = {}
    for name, value in entry_points.items():
        module = str(value).split(":", 1)[0].split(".", 1)[0]
        if module in dirs:
            names.setdefault(module, []).append(name)
    return [
        ToolkitConnector(dir_name=dirs[m].name, path=dirs[m], toolkits=tuple(sorted(n)))
        for m, n in sorted(names.items(), key=lambda kv: dirs[kv[0]].name)
    ]


def check(root: Path) -> list[str]:
    """The problems with ``root``'s ``[toolkits]`` extra; empty when it is right."""
    pyproject = root / "pyproject.toml"
    project = _load(pyproject).get("project", {})
    extras = {
        canonical(k): v for k, v in project.get("optional-dependencies", {}).items()
    }
    if EXTRA not in extras:
        return [f"{pyproject}: no [project.optional-dependencies] {EXTRA} extra"]

    connectors = {c.extra: c for c in toolkit_connectors(root)}
    problems: list[str] = []
    for connector in connectors.values():
        for name in connector.toolkits:
            if re.fullmatch(TOOLKIT_NAME_RULE, name) is None:
                problems.append(
                    f"connectors/{connector.dir_name}: toolkit entry point {name!r} must match "
                    f"{TOOLKIT_NAME_RULE} (no hyphens: hosts route a toolkit by this name)"
                )
    listed: set[str] = set()
    for entry in extras[EXTRA]:
        match = _SELF_REFERENCE.match(entry)
        if match is None or canonical(match["name"]) != FRAMEWORK:
            problems.append(
                f"{EXTRA} entry {entry!r}: write the toolkits as {FRAMEWORK}[<dir>,...]; a "
                "toolkit's third-party deps belong in its own [<dir>] extra"
            )
            continue
        for raw in match["extras"].split(","):
            key = canonical(raw.strip())
            if not key:
                continue
            if key in listed:
                problems.append(
                    f"{EXTRA} entry {entry!r}: [{raw.strip()}] is listed twice"
                )
                continue
            listed.add(key)
            if key not in extras:
                problems.append(
                    f"{EXTRA} entry {entry!r}: {FRAMEWORK} has no [{raw.strip()}] extra"
                )
            elif key not in connectors:
                problems.append(
                    f"{EXTRA} entry {entry!r}: [{raw.strip()}] is not a toolkit connector (no "
                    f"{TOOLKIT_GROUP} entry point points into its module); remove it"
                )
    for key, connector in sorted(connectors.items()):
        if key not in listed:
            problems.append(
                f"connectors/{connector.dir_name} registers toolkit(s) "
                f"{', '.join(connector.toolkits)} but [{connector.dir_name}] is not in the "
                f"{EXTRA} extra; add it to {FRAMEWORK}[...]"
            )
        if connector.extra not in extras:
            problems.append(
                f"connectors/{connector.dir_name} has no [{connector.dir_name}] extra in the "
                "root pyproject (its third-party deps; [] when it has none)"
            )
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(__file__).resolve().parent.parent,
        help="the pipe checkout (default: this script's repo)",
    )
    parser.add_argument(
        "--dirs",
        action="store_true",
        help="print each toolkit connector's directory and exit",
    )
    args = parser.parse_args(argv)
    root: Path = args.root.resolve()

    if args.dirs:
        for connector in toolkit_connectors(root):
            print(connector.path)
        return 0

    problems = check(root)
    if problems:
        print(f"check_toolkits_extra: {len(problems)} problem(s)", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        return 1
    names = ", ".join(c.dir_name for c in toolkit_connectors(root))
    print(
        f"check_toolkits_extra: ok, [{EXTRA}] pulls in every toolkit connector ({names})"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
