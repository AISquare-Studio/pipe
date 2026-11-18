#!/usr/bin/env python3
"""Print the lowest version of a dependency that the root extras allow.

``pip install -e ".[full,dev]"`` resolves the NEWEST version each extra
allows, but a host may already hold any version in the range, and pip keeps
it. A guard that holds at the newest version and not at the floor still
ships. So CI (validate.yml) installs the floor and runs, a second time, the
suites of the connectors whose extras name the dependency. The SQL guards
(sqlglot) are why: sqlglot 25.3 to 26.24 read BigQuery's ``/* */`` comments
as nested, and GoogleSQL doesn't nest them.

Every extra that names the dependency must give it the SAME specifier, so a
floor is raised once and holds for every connector that uses it.

Reads the root pyproject only (no install, no import, stdlib only):

    python scripts/dep_floor.py sqlglot               # sqlglot==<floor>; exit 1 on a problem
    python scripts/dep_floor.py sqlglot --connectors  # each connectors/<dir> whose extra names it; exit 1 if none
    python scripts/dep_floor.py sqlglot --root R      # another checkout
"""

from __future__ import annotations

import argparse
import re
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path

# name [extras] specifiers [; marker]: enough of PEP 508 for this pyproject.
_REQUIREMENT = re.compile(
    r"^\s*(?P<name>[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?)\s*(?:\[[^\]]*\])?"
    r"\s*(?P<spec>[^;@]*)"
)
_LOWER_BOUND = re.compile(r"^(?:>=|==|~=)\s*(?P<version>[0-9][0-9A-Za-z.]*)$")


class FloorError(Exception):
    """The extras give the dependency no single floor (the message says why)."""


@dataclass(frozen=True)
class Floor:
    dist: str
    version: str
    specifier: str
    extras: tuple[str, ...]  # the extras that name it, sorted

    @property
    def pin(self) -> str:
        return f"{self.dist}=={self.version}"


def canonical(name: str) -> str:
    """A distribution or extra name as pip compares them (PEP 503 / 685)."""
    return re.sub(r"[-_.]+", "-", name).lower()


def floor(root: Path, dist: str) -> Floor:
    """``dist``'s floor in ``root``'s extras; :class:`FloorError` when no
    extra names it, two extras disagree, or the specifier has no lower
    bound."""
    pyproject = root / "pyproject.toml"
    with pyproject.open("rb") as f:
        extras = tomllib.load(f).get("project", {}).get("optional-dependencies", {})
    specs: dict[str, str] = {}
    for extra, requirements in sorted(extras.items()):
        for requirement in requirements:
            match = _REQUIREMENT.match(requirement)
            if match and canonical(match["name"]) == canonical(dist):
                specs[extra] = re.sub(r"\s+", "", match["spec"])
    if not specs:
        raise FloorError(f"{pyproject}: no extra names {dist}")
    if len(set(specs.values())) > 1:
        listed = ", ".join(f"[{e}] {dist}{s}" for e, s in specs.items())
        raise FloorError(
            f"{pyproject}: the extras give {dist} different specifiers ({listed}); "
            "give every one the same, so its floor is raised once"
        )
    (specifier,) = set(specs.values())
    lower = [m["version"] for p in specifier.split(",") if (m := _LOWER_BOUND.match(p))]
    if len(lower) != 1:
        raise FloorError(
            f"{pyproject}: {dist}{specifier} needs exactly one lower bound (>=, == or ~=)"
        )
    return Floor(canonical(dist), lower[0], specifier, tuple(specs))


def connectors(root: Path, found: Floor) -> list[Path]:
    """The ``connectors/<dir>`` whose extra (named ``<dir>``) names the
    dependency, sorted."""
    named = {canonical(e) for e in found.extras}
    return [
        d
        for d in sorted((root / "connectors").iterdir())
        if d.is_dir() and canonical(d.name) in named
    ]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("dist", help="the dependency, e.g. sqlglot")
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(__file__).resolve().parent.parent,
        help="the pipe checkout (default: this script's repo)",
    )
    parser.add_argument(
        "--connectors",
        action="store_true",
        help="print each connector directory whose extra names it",
    )
    args = parser.parse_args(argv)
    root: Path = args.root.resolve()
    try:
        found = floor(root, args.dist)
    except FloorError as problem:
        print(f"dep_floor: {problem}", file=sys.stderr)
        return 1
    if args.connectors:
        paths = connectors(root, found)
        if not paths:
            # A floor step that finds no suite would pass having tested
            # nothing (review #37 n1).
            print(
                f"dep_floor: no connectors/<dir> is named like an extra that names "
                f"{found.dist} ({', '.join(sorted(found.extras))})",
                file=sys.stderr,
            )
            return 1
        for path in paths:
            print(path)
    else:
        print(found.pin)
    return 0


if __name__ == "__main__":
    sys.exit(main())
