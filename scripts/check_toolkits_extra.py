#!/usr/bin/env python3
"""Check that the root ``[toolkits]`` extra lists every toolkit package, and nothing else.

A toolkit package is a ``connectors/<name>/`` package whose ``pyproject.toml``
registers at least one ``aisquare_pipe.toolkits`` entry point. The extra is
how a host (pipe-as-service) installs every toolkit without naming them one by
one, so a toolkit missing from it never reaches the host, and an entry that is
not a toolkit installs a package for nothing.

Each entry must be ``<package>>=<floor>``: a bare name lets pip settle on an
old release that predates the package's toolkit (aisquare-pipe-github 0.1.0
registers none). The floor may not exceed the package's version in this repo,
or the extra could not be satisfied from it.

Every toolkit entry-point name must match ``TOOLKIT_NAME_RULE`` (no hyphens):
it is the connector id a host routes on, and pipe-as-service could not serve
a hyphenated one.

Reads the pyproject files only (no install, no import, stdlib only):

    python scripts/check_toolkits_extra.py           # check; exit 1 on a problem
    python scripts/check_toolkits_extra.py --dirs    # print each toolkit package's dir
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
TOOLKIT_GROUP = "aisquare_pipe.toolkits"
# aisquare.pipe.core.tools.TOOLKIT_NAME_RULE, copied: this script runs before
# anything is installed. tests/test_check_toolkits_extra.py keeps them equal.
TOOLKIT_NAME_RULE = r"^[a-z][a-z0-9_]{0,63}$"

# PEP 508 name, optional extras, then the version specifiers (no markers, no URLs).
_REQUIREMENT = re.compile(
    r"^\s*(?P<name>[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?)\s*"
    r"(?:\[[^\]]*\])?\s*(?P<specs>[^;@]*)$"
)
_FLOOR = re.compile(r"^>=\s*(?P<version>\d+(?:\.\d+)*)$")


@dataclass(frozen=True)
class ToolkitPackage:
    name: str  # the distribution name, as its pyproject spells it
    version: str
    path: Path
    toolkits: tuple[str, ...]  # its aisquare_pipe.toolkits entry-point names


def canonical(name: str) -> str:
    """A distribution name as pip compares them (PEP 503)."""
    return re.sub(r"[-_.]+", "-", name).lower()


def _release(version: str) -> tuple[int, ...]:
    """The numeric release part of a version, trailing zeros dropped
    (``0.3`` == ``0.3.0``; ``0.4.0rc1`` gives ``(0, 4)``)."""
    head = re.match(r"\d+(?:\.\d+)*", version)
    parts = [int(p) for p in head.group().split(".")] if head else []
    while len(parts) > 1 and parts[-1] == 0:
        parts.pop()
    return tuple(parts)


def _load(path: Path) -> dict:
    with path.open("rb") as f:
        return tomllib.load(f)


def toolkit_packages(root: Path) -> list[ToolkitPackage]:
    """Every ``connectors/*`` package registering an ``aisquare_pipe.toolkits``
    entry point, sorted by directory."""
    found: list[ToolkitPackage] = []
    for pyproject in sorted((root / "connectors").glob("*/pyproject.toml")):
        project = _load(pyproject).get("project", {})
        toolkits = project.get("entry-points", {}).get(TOOLKIT_GROUP) or {}
        if toolkits:
            found.append(
                ToolkitPackage(
                    name=str(project.get("name", "")),
                    version=str(project.get("version", "")),
                    path=pyproject.parent,
                    toolkits=tuple(sorted(toolkits)),
                )
            )
    return found


def check(root: Path) -> list[str]:
    """The problems with ``root``'s ``[toolkits]`` extra; empty when it is right."""
    pyproject = root / "pyproject.toml"
    extras = _load(pyproject).get("project", {}).get("optional-dependencies", {})
    if EXTRA not in extras:
        return [f"{pyproject}: no [project.optional-dependencies] {EXTRA} extra"]

    packages = {canonical(p.name): p for p in toolkit_packages(root)}
    problems: list[str] = []
    for package in packages.values():
        for name in package.toolkits:
            if re.fullmatch(TOOLKIT_NAME_RULE, name) is None:
                problems.append(
                    f"{package.path.relative_to(root)}: toolkit entry point {name!r} must match "
                    f"{TOOLKIT_NAME_RULE} (no hyphens: hosts route a toolkit by this name)"
                )
    listed: set[str] = set()
    for entry in extras[EXTRA]:
        match = _REQUIREMENT.match(entry)
        if match is None:
            problems.append(f"{EXTRA} entry {entry!r}: not a plain requirement")
            continue
        key = canonical(match["name"])
        if key in listed:
            problems.append(f"{EXTRA} entry {entry!r}: listed twice")
            continue
        listed.add(key)
        package = packages.get(key)
        if package is None:
            problems.append(
                f"{EXTRA} entry {entry!r}: no package under connectors/ registers an "
                f"{TOOLKIT_GROUP} entry point under that name; remove it"
            )
            continue
        floor = _FLOOR.match(match["specs"].strip())
        if floor is None:
            problems.append(
                f"{EXTRA} entry {entry!r}: write it as '{package.name}>=<floor>', the first "
                "release that ships its toolkit"
            )
        elif _release(floor["version"]) > _release(package.version):
            problems.append(
                f"{EXTRA} entry {entry!r}: floor {floor['version']} is above "
                f"{package.name} {package.version} in {package.path.relative_to(root)}"
            )
    for key, package in sorted(packages.items()):
        if key not in listed:
            problems.append(
                f"{package.path.relative_to(root)} registers toolkit(s) "
                f"{', '.join(package.toolkits)} but {package.name} is not in the {EXTRA} "
                f"extra; add '{package.name}>={package.version}'"
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
        help="print each toolkit package's directory and exit",
    )
    args = parser.parse_args(argv)
    root: Path = args.root.resolve()

    if args.dirs:
        for package in toolkit_packages(root):
            print(package.path)
        return 0

    problems = check(root)
    if problems:
        print(f"check_toolkits_extra: {len(problems)} problem(s)", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        return 1
    names = ", ".join(p.name for p in toolkit_packages(root))
    print(f"check_toolkits_extra: ok, [{EXTRA}] lists every toolkit package ({names})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
