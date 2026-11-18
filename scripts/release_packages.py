#!/usr/bin/env python3
"""What this repo publishes to PyPI, and the checks around publishing it.

From 0.5.0 the repo publishes ONE distribution, ``aisquare-pipe``, which
carries the framework and every connector (D8). The ten connectors that were
once their own PyPI projects get one last *shim* release each: no code, just
a dependency on ``aisquare-pipe[<name>]``, so ``pip install aisquare-pipe-<name>``
keeps working. ``.github/workflows/publish.yml`` drives everything from here.

Subcommands:

    plan         What a publish run builds and uploads. ``--target aisquare-pipe``
                 (the default): the distribution; it checks the version is
                 static and valid, the extras that name each other exist, and
                 on a release that the tag is ``v<version>``. ``--target shims``:
                 the ten shim projects. A pull request (``--event
                 pull_request``, the default) plans both, as a dry run. With
                 ``--pypi``, look each version up on PyPI. Writes GitHub
                 Actions outputs and a step summary.
    build        Build one planned package (the distribution, or a shim) into
                 a dist directory.
    check-dist   A built dist directory holds exactly one wheel and one sdist
                 of the package at its version. For the distribution, the
                 wheel's RECORD holds every module under ``src/`` and every
                 ``connectors/<dir>/src/``. For a shim, the wheel has no code
                 and requires exactly ``aisquare-pipe[<name>]>=<floor>``.
    verify       After an upload: each planned upload is on PyPI, with the
                 sha256 of the file this run built.
    coverage     Every ``aisquare-pipe*`` requirement of a consumer
                 (pipe-as-service's pyproject.toml / requirements.in /
                 requirements.lock) is met by this tree: the distribution at
                 its version with the extras asked for, or a shim.

Examples:

    python scripts/release_packages.py plan
    python scripts/release_packages.py plan --pypi --event release --tag v0.5.0
    python scripts/release_packages.py plan --pypi --target shims --event workflow_dispatch
    python scripts/release_packages.py build --path . --outdir dist/
    python scripts/release_packages.py check-dist --path . dist/
    python scripts/release_packages.py coverage \\
        --pipe-root . --consumer-root ../pipe-as-service --consumer-ref origin/release/pipe-router

Needs ``packaging`` (pytest, build and twine all depend on it), and ``build``
for the build subcommand. Nothing here uploads anything.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import tomllib
import urllib.error
import urllib.request
import zipfile
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from email.parser import Parser
from pathlib import Path

from packaging.requirements import InvalidRequirement, Requirement
from packaging.utils import (
    InvalidSdistFilename,
    InvalidWheelFilename,
    canonicalize_name,
    parse_sdist_filename,
    parse_wheel_filename,
)
from packaging.version import InvalidVersion, Version

FRAMEWORK = "aisquare-pipe"
PYPI = "https://pypi.org"
USER_AGENT = "aisquare-pipe-release-check (+https://github.com/AISquare-Studio/pipe)"

TARGET_FRAMEWORK = "aisquare-pipe"
TARGET_SHIMS = "shims"
#: A pull request's dry run: the distribution and every shim, so each PR
#: builds and checks everything a publish run could upload.
TARGET_ALL = "all"

#: The connectors that were their own PyPI projects (aisquare-pipe-<name>)
#: before 0.5.0. Fixed: no other connector ever gets a PyPI project.
SHIM_PROJECTS = (
    "composio",
    "docusign",
    "dropbox",
    "gateway",
    "github",
    "graphify",
    "local",
    "n8n",
    "onedrive",
    "salesforce",
)
#: Each shim's version (above every per-connector release on PyPI, the
#: highest being aisquare-pipe-n8n 0.2.1) and the aisquare-pipe it requires.
SHIM_VERSION = "0.5.0"
SHIM_FLOOR = "0.5.0"

# A pip-compile lock line: ``name==version \``. Comments and hash lines are
# indented and never match.
_LOCK_PIN = re.compile(
    r"^(?P<name>[A-Za-z0-9][A-Za-z0-9._-]*)(?:\[[^\]]*\])?==(?P<version>[^\s\\;]+)"
)


class ReleaseError(Exception):
    """The package can't be read or released as it stands."""


# --------------------------------------------------------------------------- trees


class Tree:
    """Read-only view of a checkout: a directory on disk or a git ref."""

    def read(self, rel: str) -> str | None:
        raise NotImplementedError

    def describe(self) -> str:
        raise NotImplementedError


@dataclass(frozen=True)
class FsTree(Tree):
    root: Path

    def read(self, rel: str) -> str | None:
        path = self.root / rel
        return path.read_text(encoding="utf-8") if path.is_file() else None

    def describe(self) -> str:
        return str(self.root)


@dataclass(frozen=True)
class GitTree(Tree):
    repo: Path
    ref: str

    def read(self, rel: str) -> str | None:
        done = subprocess.run(
            ["git", "-C", str(self.repo), "show", f"{self.ref}:{rel}"],
            capture_output=True,
            text=True,
            check=False,
        )
        return done.stdout if done.returncode == 0 else None

    def describe(self) -> str:
        return f"{self.repo}@{self.ref}"


def make_tree(root: str, ref: str | None) -> Tree:
    return GitTree(Path(root), ref) if ref else FsTree(Path(root))


# ------------------------------------------------------------------------ packages


@dataclass(frozen=True)
class Package:
    path: str  # "." for the distribution, "shims/<name>" for a shim
    name: str
    version: str
    dependencies: tuple[str, ...] = ()
    extras: dict[str, tuple[str, ...]] = field(default_factory=dict)

    @property
    def key(self) -> str:
        return canonicalize_name(self.name)

    @property
    def is_framework(self) -> bool:
        return self.path == "."

    @property
    def is_shim(self) -> bool:
        return self.path.startswith("shims/")

    @property
    def shim_of(self) -> str:
        """The connector a shim stands for (``shims/<name>`` → ``<name>``)."""
        return self.path.removeprefix("shims/")

    def matrix_entry(self) -> dict[str, str]:
        return {"path": self.path, "name": self.name, "version": self.version}


def framework_package(tree: Tree) -> Package:
    """The distribution, from the root pyproject."""
    text = tree.read("pyproject.toml")
    if text is None:
        raise ReleaseError(f"pyproject.toml: not found in {tree.describe()}")
    try:
        project = tomllib.loads(text).get("project", {})
    except tomllib.TOMLDecodeError as exc:
        raise ReleaseError(f"pyproject.toml: {exc}") from exc
    name, version = project.get("name"), project.get("version")
    if "version" in project.get("dynamic", []) or not isinstance(version, str):
        raise ReleaseError(
            "pyproject.toml: [project] version must be a static string; the publish run "
            "reads it from here to decide what to upload"
        )
    if not isinstance(name, str) or canonicalize_name(name) != FRAMEWORK:
        raise ReleaseError(f"the root pyproject.toml must be {FRAMEWORK}, not {name!r}")
    try:
        Version(version)
    except InvalidVersion as exc:
        raise ReleaseError(
            f"pyproject.toml: version {version!r} is not a PEP 440 version"
        ) from exc
    extras = {
        canonicalize_name(str(extra)): tuple(reqs)
        for extra, reqs in project.get("optional-dependencies", {}).items()
    }
    return Package(
        path=".",
        name=name,
        version=version,
        dependencies=tuple(project.get("dependencies", [])),
        extras=extras,
    )


def shim_packages() -> list[Package]:
    return [
        Package(
            path=f"shims/{name}",
            name=f"{FRAMEWORK}-{name}",
            version=SHIM_VERSION,
            dependencies=(f"{FRAMEWORK}[{name}]>={SHIM_FLOOR}",),
        )
        for name in SHIM_PROJECTS
    ]


def packages_for(tree: Tree, target: str) -> list[Package]:
    if target == TARGET_FRAMEWORK:
        return [framework_package(tree)]
    if target == TARGET_SHIMS:
        return shim_packages()
    if target == TARGET_ALL:
        return [framework_package(tree), *shim_packages()]
    raise ReleaseError(
        f"unknown target {target!r} ({TARGET_FRAMEWORK}, {TARGET_SHIMS} or {TARGET_ALL})"
    )


def _family_requirement(spec: str) -> Requirement | None:
    """The requirement, if it names an aisquare-pipe* distribution (markers ignored)."""
    try:
        req = Requirement(spec)
    except InvalidRequirement:
        return None
    key = canonicalize_name(req.name)
    return req if key == FRAMEWORK or key.startswith(f"{FRAMEWORK}-") else None


def _satisfies(req: Requirement, version: str) -> bool:
    return req.specifier.contains(Version(version), prereleases=True)


def connector_dirs(root: Path) -> list[str]:
    """Every ``connectors/<dir>/`` with a ``src/`` (the connectors the wheel carries)."""
    base = root / "connectors"
    if not base.is_dir():
        return []
    return sorted(
        d.name
        for d in base.iterdir()
        if (d / "src").is_dir() and not d.name.startswith((".", "_"))
    )


def bundle(framework: Package, extra: str) -> set[str]:
    """Every extra ``extra`` pulls in through ``aisquare-pipe[...]`` references,
    itself included (canonical names)."""
    seen: set[str] = set()
    todo = [canonicalize_name(extra)]
    while todo:
        name = todo.pop()
        if name in seen:
            continue
        seen.add(name)
        for spec in framework.extras.get(name, ()):
            req = _family_requirement(spec)
            if req is not None and canonicalize_name(req.name) == FRAMEWORK:
                todo += [canonicalize_name(e) for e in req.extras]
    return seen


def internal_problems(framework: Package, tree: Tree | None = None) -> list[str]:
    """Problems in the distribution's own metadata that would make it
    uninstallable, or not what it claims:

    - an ``aisquare-pipe[...]`` reference (``[full]`` → ``[popular]``) names an
      extra that doesn't exist, or a version this one isn't;
    - a dependency on another ``aisquare-pipe-*`` distribution (every connector
      ships inside, so it would pull in an old per-connector copy);
    - given a working tree: a ``connectors/<dir>/`` with no ``[<dir>]`` extra,
      or a ``[full]`` that doesn't pull in every connector's extra.
    """
    problems = []
    declared = [("dependencies", s) for s in framework.dependencies]
    declared += [
        (f"[{extra}]", s) for extra, specs in framework.extras.items() for s in specs
    ]
    for where, spec in declared:
        req = _family_requirement(spec)
        if req is None:
            continue
        if canonicalize_name(req.name) != FRAMEWORK:
            problems.append(
                f"{framework.name} {where} requires {spec!r}: connectors ship inside "
                f"{FRAMEWORK}; require {FRAMEWORK}[<name>] instead"
            )
            continue
        if not _satisfies(req, framework.version):
            problems.append(
                f"{framework.name} {where} requires {spec!r}, but this is {framework.version}"
            )
        for extra in req.extras:
            if canonicalize_name(extra) not in framework.extras:
                problems.append(
                    f"{framework.name} {where} requires {spec!r}: there is no [{extra}] extra"
                )
    if isinstance(tree, FsTree):
        dirs = connector_dirs(tree.root)
        missing = [d for d in dirs if canonicalize_name(d) not in framework.extras]
        problems += [
            f"connectors/{d}/ has no [{d}] extra in the root pyproject.toml"
            for d in missing
        ]
        full = bundle(framework, "full")
        problems += [
            f"[full] doesn't pull in [{d}]: it must carry every connector's deps"
            for d in dirs
            if d not in missing and canonicalize_name(d) not in full
        ]
    return problems


# --------------------------------------------------------------------------- PyPI

Fetch = Callable[[str], tuple[int, bytes]]


def http_fetch(
    url: str, *, attempts: int = 4, timeout: float = 20.0
) -> tuple[int, bytes]:
    """GET ``url``; (status, body). Retries network errors and 5xx/429, returns 404 as is."""
    last = ""
    for attempt in range(attempts):
        request = urllib.request.Request(
            url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"}
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as exc:
            if exc.code < 500 and exc.code != 429:
                return exc.code, exc.read()
            last = f"HTTP {exc.code}"
        except (urllib.error.URLError, TimeoutError) as exc:
            last = str(exc)
        time.sleep(2**attempt)
    raise ReleaseError(f"GET {url} failed after {attempts} attempts: {last}")


# A package version's state on PyPI.
NEW_PROJECT = "new project"  # the project doesn't exist
NEW_VERSION = "new version"  # the project exists, this version doesn't
PUBLISHED = "published"  # this exact version is on PyPI: nothing to upload
UNKNOWN = "unknown"  # the lookup failed (dry runs only; a real run stops instead)


@dataclass(frozen=True)
class PypiState:
    state: str
    files: dict[str, str] = field(
        default_factory=dict
    )  # filename -> sha256, when PUBLISHED
    detail: str = ""


def pypi_state(name: str, version: str, fetch: Fetch) -> PypiState:
    key = canonicalize_name(name)
    status, body = fetch(f"{PYPI}/pypi/{key}/{version}/json")
    if status == 200:
        files = {
            u["filename"]: u["digests"]["sha256"]
            for u in json.loads(body).get("urls", [])
        }
        if not files:
            raise ReleaseError(
                f"{name} {version} is registered on PyPI with no files. PyPI never "
                "accepts a filename twice, so this version can't be uploaded: bump it"
            )
        return PypiState(PUBLISHED, files)
    if status != 404:
        raise ReleaseError(f"PyPI lookup of {name} {version}: HTTP {status}")
    status, _ = fetch(f"{PYPI}/pypi/{key}/json")
    if status == 404:
        return PypiState(NEW_PROJECT)
    if status == 200:
        return PypiState(NEW_VERSION)
    raise ReleaseError(f"PyPI lookup of {name}: HTTP {status}")


# --------------------------------------------------------------------------- plan


@dataclass
class Plan:
    target: str
    packages: list[Package]
    upload: bool  # a real publish run (not a dry run)
    states: dict[str, PypiState] = field(default_factory=dict)  # by package key
    problems: list[str] = field(default_factory=list)  # any problem fails the run
    warnings: list[str] = field(default_factory=list)

    def to_upload(self) -> list[Package]:
        return [
            p
            for p in self.packages
            if self.states.get(p.key, PypiState(UNKNOWN)).state
            in (NEW_PROJECT, NEW_VERSION)
        ]

    def outputs(self) -> dict[str, str]:
        uploads = self.to_upload() if self.upload else []
        return {
            "target": self.target,
            "packages": json.dumps([p.matrix_entry() for p in self.packages]),
            "upload": json.dumps(self.upload and bool(uploads)),
            "framework": json.dumps(any(p.is_framework for p in uploads)),
            "shims": json.dumps([p.matrix_entry() for p in uploads if p.is_shim]),
            "uploads": json.dumps([p.matrix_entry() for p in uploads]),
        }

    def summary(self) -> str:
        mode = "publish" if self.upload else "dry run (nothing is uploaded)"
        lines = [
            f"### PyPI {mode}: {self.target}",
            "",
            "| Package | Version | On PyPI | This run |",
            "|---|---|---|---|",
        ]
        for p in self.packages:
            st = self.states.get(p.key)
            if st is None:
                on_pypi, action = "not checked", "build + check"
            elif st.state == PUBLISHED:
                on_pypi, action = "yes", "skip: this version is already on PyPI"
            elif st.state == UNKNOWN:
                on_pypi, action = f"lookup failed ({st.detail})", "build + check"
            else:
                on_pypi = "no (new project)" if st.state == NEW_PROJECT else "no"
                action = "**upload**" if self.upload else "would upload"
            lines.append(f"| `{p.name}` | {p.version} | {on_pypi} | {action} |")
        for title, items in (
            ("Problems (the run fails)", self.problems),
            ("Warnings", self.warnings),
        ):
            if items:
                lines += ["", f"**{title}**", ""] + [f"- {i}" for i in items]
        return "\n".join(lines) + "\n"


def make_plan(
    tree: Tree,
    *,
    event: str,
    tag: str = "",
    dry_run: bool = True,
    fetch: Fetch | None = None,
    target: str = TARGET_FRAMEWORK,
) -> Plan:
    """What a publish run does. ``fetch`` is None to skip the PyPI lookups."""
    if event == "release":
        target = (
            TARGET_FRAMEWORK  # a GitHub Release only ever publishes the distribution
        )
    elif event == "pull_request":
        target = TARGET_ALL  # a PR uploads nothing, and checks everything
    packages = packages_for(tree, target)
    upload = event == "release" or (event == "workflow_dispatch" and not dry_run)
    plan = Plan(target=target, packages=packages, upload=upload)
    if target in (TARGET_FRAMEWORK, TARGET_ALL):
        framework = packages[0]
        plan.problems += internal_problems(framework, tree)
        if event == "release" and tag != f"v{framework.version}":
            plan.problems.append(
                f"release tag {tag!r} does not match {framework.name} {framework.version} in "
                f"pyproject.toml: tag the release v{framework.version}, or bump pyproject.toml first"
            )
    if upload and fetch is None:
        plan.problems.append("a publish run must look versions up on PyPI (--pypi)")
    if fetch is None:
        return plan
    for pkg in packages:
        try:
            plan.states[pkg.key] = pypi_state(pkg.name, pkg.version, fetch)
        except ReleaseError as exc:
            if upload:
                plan.problems.append(str(exc))
            else:
                plan.states[pkg.key] = PypiState(UNKNOWN, detail=str(exc))
                plan.warnings.append(str(exc))
    for pkg in packages:
        if plan.states.get(pkg.key, PypiState(UNKNOWN)).state == NEW_PROJECT:
            # Never create a project: its trusted publisher wouldn't exist.
            plan.problems.append(
                f"{pkg.name} is not on PyPI at all, and this workflow never creates a project"
            )
    if target == TARGET_SHIMS:
        # Only a shim run can upload a shim; a PR's shims are a build check.
        _check_shim_floor(plan, fetch)
    if upload and not plan.problems and not plan.to_upload():
        plan.warnings.append("every version is already on PyPI: nothing to upload")
    return plan


def _check_shim_floor(plan: Plan, fetch: Fetch) -> None:
    """Shims upload only once the aisquare-pipe they require is on PyPI:
    before that, installing a shim could not resolve."""
    try:
        floor = pypi_state(FRAMEWORK, SHIM_FLOOR, fetch)
    except ReleaseError as exc:
        floor = PypiState(UNKNOWN, detail=str(exc))
    if floor.state != PUBLISHED:
        message = (
            f"{FRAMEWORK} {SHIM_FLOOR} is not on PyPI yet ({floor.state}), and every shim "
            f"requires it: publish {FRAMEWORK} first"
        )
        (plan.problems if plan.upload else plan.warnings).append(message)


# ----------------------------------------------------------------------- shims


def shim_files(name: str) -> dict[str, str]:
    """The source files of the shim for connector ``name``."""
    project = f"{FRAMEWORK}-{name}"
    module = f"aisquare_pipe_{name.replace('-', '_')}"
    readme = (
        f"# {project}\n\n"
        f"**This package has moved.** From {FRAMEWORK} {SHIM_FLOOR}, the `{name}` connector "
        f"ships inside [{FRAMEWORK}](https://pypi.org/project/{FRAMEWORK}/), with its "
        f"third-party dependencies as the `[{name}]` extra:\n\n"
        f'```bash\npip install "{FRAMEWORK}[{name}]"\n```\n\n'
        f"This release has no code. It only requires `{FRAMEWORK}[{name}]>={SHIM_FLOOR}`, so "
        f"existing `pip install {project}` lines keep working in a fresh environment. The module is still "
        f"`{module}`.\n\n"
        f"## Upgrading from an older {project}\n\n"
        f"The old release and {FRAMEWORK} install the same files, and removing the old "
        f"release removes them. Uninstall it first, then install the extra:\n\n"
        f'```bash\npip uninstall -y {project}\npip install "{FRAMEWORK}[{name}]>={SHIM_FLOOR}"\n```\n\n'
        f"If you already upgraded in place, never `pip uninstall {project}` without running "
        f"`pip install --force-reinstall --no-deps {FRAMEWORK}` afterwards. The same command "
        f"repairs an environment where `{module}` fails to import. A fresh environment is "
        f"never affected.\n"
    )
    pyproject = f'''[build-system]
requires = ["setuptools>=77.0"]
build-backend = "setuptools.build_meta"

[project]
name = "{project}"
version = "{SHIM_VERSION}"
description = "Moved: the {name} connector now ships inside {FRAMEWORK} (install {FRAMEWORK}[{name}])"
readme = "README.md"
license = "Apache-2.0"
requires-python = ">=3.11"
authors = [{{name = "AISquare Studio"}}]
classifiers = [
    "Development Status :: 7 - Inactive",
    "Programming Language :: Python :: 3",
]
dependencies = ["{FRAMEWORK}[{name}]>={SHIM_FLOOR}"]

[project.urls]
Homepage = "https://github.com/AISquare-Studio/pipe"
Repository = "https://github.com/AISquare-Studio/pipe"

[tool.setuptools]
packages = []
py-modules = []
'''
    return {"pyproject.toml": pyproject, "README.md": readme}


def build(path: str, outdir: Path, root: Path) -> list[Path]:
    """Build the distribution (``.``) or a shim (``shims/<name>``) into
    ``outdir`` with ``python -m build``; the files built."""
    outdir.mkdir(parents=True, exist_ok=True)
    before = set(outdir.iterdir())
    if path == ".":
        _run_build(root, outdir)
    elif path.startswith("shims/") and path.removeprefix("shims/") in SHIM_PROJECTS:
        with tempfile.TemporaryDirectory(prefix="aisquare-pipe-shim-") as tmp:
            for rel, text in shim_files(path.removeprefix("shims/")).items():
                (Path(tmp) / rel).write_text(text, encoding="utf-8")
            _run_build(Path(tmp), outdir)
    else:
        raise ReleaseError(
            f"{path!r} is not a package here ('.' or shims/<one of {', '.join(SHIM_PROJECTS)}>)"
        )
    return sorted(set(outdir.iterdir()) - before)


def _run_build(src: Path, outdir: Path) -> None:
    done = subprocess.run(
        [sys.executable, "-m", "build", "--outdir", str(outdir), str(src)],
        capture_output=True,
        text=True,
        check=False,
    )
    if done.returncode != 0:
        raise ReleaseError(
            f"python -m build {src} failed:\n{done.stdout[-2000:]}\n{done.stderr[-2000:]}"
        )


# ------------------------------------------------------------------ dist checks


def dist_problems(pkg: Package, dist: Path) -> list[str]:
    """The dist dir holds one wheel and one sdist, named for the package and version."""
    files = (
        sorted(f.name for f in dist.iterdir() if f.is_file()) if dist.is_dir() else []
    )
    wheels = [f for f in files if f.endswith(".whl")]
    sdists = [f for f in files if f.endswith(".tar.gz")]
    problems = []
    if len(wheels) != 1 or len(sdists) != 1 or len(files) != 2:
        problems.append(
            f"{dist}: expected one wheel and one sdist of {pkg.name}, found {files}"
        )
    want = (pkg.key, Version(pkg.version))
    for fname in wheels + sdists:
        try:
            name, version = (
                parse_wheel_filename(fname)[:2]
                if fname.endswith(".whl")
                else parse_sdist_filename(fname)
            )
        except (InvalidWheelFilename, InvalidSdistFilename) as exc:
            problems.append(f"{fname}: {exc}")
            continue
        if (canonicalize_name(name), version) != want:
            problems.append(
                f"{fname} is {name} {version}, but the package is {pkg.name} {pkg.version}"
            )
    return problems


def wheel_record(wheel: Path) -> list[str]:
    """The paths a wheel's RECORD lists."""
    with zipfile.ZipFile(wheel) as zf:
        record = next(
            (n for n in zf.namelist() if n.endswith(".dist-info/RECORD")), None
        )
        if record is None:
            raise ReleaseError(f"{wheel.name} has no RECORD")
        rows = csv.reader(io.StringIO(zf.read(record).decode("utf-8")))
        return [row[0] for row in rows if row]


def wheel_metadata(wheel: Path) -> dict[str, list[str]]:
    """A wheel's METADATA headers (each header's values, in order)."""
    with zipfile.ZipFile(wheel) as zf:
        name = next(
            (n for n in zf.namelist() if n.endswith(".dist-info/METADATA")), None
        )
        if name is None:
            raise ReleaseError(f"{wheel.name} has no METADATA")
        message = Parser().parsestr(zf.read(name).decode("utf-8"), headersonly=True)
    found: dict[str, list[str]] = {}
    for key, value in message.items():
        found.setdefault(key, []).append(value)
    return found


def expected_modules(root: Path) -> set[str]:
    """Every ``.py`` file the distribution must carry, as a wheel path:
    ``src/<...>`` and each ``connectors/<dir>/src/<...>``."""
    paths: set[str] = set()
    for base in [
        root / "src",
        *(root / "connectors" / d / "src" for d in connector_dirs(root)),
    ]:
        for py in base.rglob("*.py"):
            if "__pycache__" not in py.parts:
                paths.add(py.relative_to(base).as_posix())
    return paths


def framework_wheel_problems(wheel: Path, root: Path) -> list[str]:
    """The wheel's RECORD holds every module of the checkout, and no connector
    module that isn't in ``connectors/``."""
    recorded = set(wheel_record(wheel))
    expected = expected_modules(root)
    problems = [f"{wheel.name}: RECORD lacks {p}" for p in sorted(expected - recorded)]
    dirs = {f"aisquare_pipe_{d.replace('-', '_')}" for d in connector_dirs(root)}
    tops = {p.split("/", 1)[0] for p in recorded if p.endswith(".py")}
    problems += [
        f"{wheel.name}: carries {top}, which no connectors/<dir>/src provides"
        for top in sorted(tops)
        if top.startswith("aisquare_pipe_") and top not in dirs
    ]
    return problems


def shim_wheel_problems(pkg: Package, wheel: Path) -> list[str]:
    """A shim wheel has no code, and requires exactly ``aisquare-pipe[<name>]>=<floor>``."""
    problems = []
    code = [p for p in wheel_record(wheel) if ".dist-info/" not in p]
    if code:
        problems.append(
            f"{wheel.name} carries files besides its metadata: {', '.join(code)}"
        )
    requires = wheel_metadata(wheel).get("Requires-Dist", [])
    want = Requirement(f"{FRAMEWORK}[{pkg.shim_of}]>={SHIM_FLOOR}")
    got = [Requirement(r) for r in requires]
    same = len(got) == 1 and (
        canonicalize_name(got[0].name),
        got[0].extras,
        got[0].specifier,
        got[0].marker,
    ) == (canonicalize_name(want.name), want.extras, want.specifier, None)
    if not same:
        problems.append(
            f"{wheel.name} requires {requires}, not exactly [{str(want)!r}]"
        )
    return problems


def check_dist(pkg: Package, dist: Path, root: Path) -> list[str]:
    problems = dist_problems(pkg, dist)
    wheels = sorted(dist.glob("*.whl")) if dist.is_dir() else []
    if len(wheels) == 1:
        if pkg.is_framework:
            problems += framework_wheel_problems(wheels[0], root)
        elif pkg.is_shim:
            problems += shim_wheel_problems(pkg, wheels[0])
    return problems


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_problems(
    uploads: Iterable[dict[str, str]],
    dist_root: Path,
    fetch: Fetch,
    *,
    timeout: float = 180.0,
    interval: float = 15.0,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> list[str]:
    """Each upload's files are on PyPI with the sha256 of what this run built.

    ``dist_root/dist-<name>/`` holds each package's built files (the artifact
    layout publish.yml downloads). PyPI's JSON API can trail an upload by a
    little, so this polls whatever is still missing until ``timeout``."""
    problems: list[str] = []
    built: dict[
        str, tuple[dict[str, str], dict[str, str]]
    ] = {}  # key -> (entry, {file: sha256})
    for entry in uploads:
        local_dir = dist_root / f"dist-{entry['name']}"
        local = (
            {f.name: sha256(f) for f in sorted(local_dir.iterdir()) if f.is_file()}
            if local_dir.is_dir()
            else {}
        )
        if local:
            built[canonicalize_name(entry["name"])] = (entry, local)
        else:
            problems.append(
                f"{entry['name']} {entry['version']}: no built files in {local_dir}"
            )
    on_pypi: dict[str, dict[str, str]] = {}
    pending = set(built)
    deadline = clock() + timeout
    while True:
        for key in sorted(pending):
            entry, local = built[key]
            try:
                on_pypi[key] = pypi_state(entry["name"], entry["version"], fetch).files
            except ReleaseError:
                on_pypi[key] = {}
            if set(local) <= set(on_pypi[key]):
                pending.discard(key)
        if not pending or clock() >= deadline:
            break
        sleep(interval)
    for key, (entry, local) in built.items():
        missing = sorted(set(local) - set(on_pypi.get(key, {})))
        if missing:
            problems.append(
                f"{entry['name']} {entry['version']}: not on PyPI after the upload: {', '.join(missing)}"
            )
        for fname, digest in sorted(local.items()):
            remote = on_pypi.get(key, {}).get(fname)
            if remote is not None and remote != digest:
                problems.append(
                    f"{entry['name']} {entry['version']}: PyPI's {fname} has sha256 {remote}, not this "
                    f"run's {digest} (an older upload of the same version?)"
                )
    return problems


# ------------------------------------------------------------------------ coverage


@dataclass(frozen=True)
class Need:
    requirement: str  # as the consumer wrote it
    source: str  # where it came from, e.g. "requirements.in"
    provided_by: str  # what meets it: "aisquare-pipe 0.5.0", "shim aisquare-pipe-github 0.5.0", ""
    verdict: str  # "ok", "ok (...)" or "FAIL: ..."


def consumer_requirements(tree: Tree) -> tuple[str, list[tuple[str, str]]]:
    """(the consumer's own project name, [(source file, requirement)])."""
    found: list[tuple[str, str]] = []
    text = tree.read("pyproject.toml")
    if text is None:
        raise ReleaseError(f"no pyproject.toml in {tree.describe()}")
    project = tomllib.loads(text).get("project", {})
    own = project.get("name", "")
    found += [
        ("pyproject.toml dependencies", s) for s in project.get("dependencies", [])
    ]
    for extra, specs in project.get("optional-dependencies", {}).items():
        found += [(f"pyproject.toml [{extra}]", s) for s in specs]
    for line in (tree.read("requirements.in") or "").splitlines():
        line = line.split("#", 1)[0].strip()
        if line and not line.startswith("-"):
            found.append(("requirements.in", line))
    for line in (tree.read("requirements.lock") or "").splitlines():
        m = _LOCK_PIN.match(line)
        if m:
            found.append(("requirements.lock", f"{m['name']}=={m['version']}"))
    return own, found


def coverage(pipe: Tree, consumer: Tree) -> list[Need]:
    """Every aisquare-pipe* requirement of the consumer against what this tree
    publishes: ``aisquare-pipe`` at its version (with the extras asked for),
    or one of the shims. A lock pin below what is published is the lock
    before its relock (reported, not a failure)."""
    framework = framework_package(pipe)
    shims = {p.key: p for p in shim_packages()}
    own, found = consumer_requirements(consumer)
    own_key = canonicalize_name(own) if own else ""
    needs: list[Need] = []
    for source, spec in found:
        req = _family_requirement(spec)
        if req is None or canonicalize_name(req.name) == own_key:
            continue  # not ours, or the consumer's own extras
        key = canonicalize_name(req.name)
        pins = list(req.specifier)
        pinned = bool(pins) and all(s.operator in ("==", "===") for s in pins)
        if key == FRAMEWORK:
            unknown = sorted(
                e for e in req.extras if canonicalize_name(e) not in framework.extras
            )
            if unknown:
                verdict = f"FAIL: {FRAMEWORK} has no [{', '.join(unknown)}] extra"
            elif _satisfies(req, framework.version):
                verdict = "ok"
            elif pinned and all(
                Version(s.version) <= Version(framework.version) for s in pins
            ):
                verdict = (
                    "ok (pinned below the published version: relock after publishing)"
                )
            else:
                verdict = f"FAIL: needs {req.specifier}, this tree publishes {framework.version}"
            needs.append(
                Need(spec, source, f"{FRAMEWORK} {framework.version}", verdict)
            )
            continue
        connector = key.removeprefix(f"{FRAMEWORK}-")
        shim = shims.get(key)
        if shim is None:
            verdict = (
                f"FAIL: {req.name} is not a distribution (it never reached PyPI, and from 0.5.0 "
                f"it ships inside {FRAMEWORK}): require {FRAMEWORK}[{connector}]"
            )
            needs.append(Need(spec, source, "", verdict))
        elif pinned:
            verdict = (
                f"ok (a pin of the old per-connector release: the relock must drop it, since "
                f"{FRAMEWORK}[{connector}] replaces it)"
            )
            needs.append(
                Need(spec, source, f"shim {shim.name} {shim.version}", verdict)
            )
        elif _satisfies(req, shim.version):
            verdict = f"ok via the shim; replace with {FRAMEWORK}[{connector}]>={framework.version}"
            needs.append(
                Need(spec, source, f"shim {shim.name} {shim.version}", verdict)
            )
        else:
            verdict = f"FAIL: needs {req.specifier}; its last release is the shim {shim.version}"
            needs.append(Need(spec, source, "", verdict))
    return needs


# ----------------------------------------------------------------------------- CLI


def _write_github(name: str, text: str) -> None:
    target = os.environ.get(name)
    if target:
        with open(target, "a", encoding="utf-8") as fh:
            fh.write(text)


def _package(args: argparse.Namespace) -> Package:
    if args.path == ".":
        return framework_package(FsTree(Path(args.root)))
    for pkg in shim_packages():
        if pkg.path == args.path:
            return pkg
    raise ReleaseError(f"{args.path!r} is not a package here ('.' or shims/<name>)")


def _cmd_plan(args: argparse.Namespace) -> int:
    plan = make_plan(
        make_tree(args.root, None),
        event=args.event,
        tag=args.tag,
        dry_run=args.dry_run,
        fetch=http_fetch if args.pypi else None,
        target=args.target,
    )
    outputs = plan.outputs()
    _write_github("GITHUB_OUTPUT", "".join(f"{k}={v}\n" for k, v in outputs.items()))
    summary = plan.summary()
    _write_github("GITHUB_STEP_SUMMARY", summary)
    print(summary)
    for w in plan.warnings:
        print(f"::warning::{w}")
    for p in plan.problems:
        print(f"::error::{p}")
    if args.json:
        print(json.dumps(outputs, indent=2))
    return 1 if plan.problems else 0


def _cmd_build(args: argparse.Namespace) -> int:
    pkg = _package(args)
    files = build(pkg.path, Path(args.outdir), Path(args.root))
    print(f"built {pkg.name} {pkg.version}: " + ", ".join(f.name for f in files))
    return 0


def _cmd_check_dist(args: argparse.Namespace) -> int:
    pkg = _package(args)
    dist = Path(args.dist)
    problems = check_dist(pkg, dist, Path(args.root))
    for p in problems:
        print(f"::error::{p}")
    if not problems:
        what = (
            f"its RECORD holds all {len(expected_modules(Path(args.root)))} modules of the checkout"
            if pkg.is_framework
            else f"no code; requires {pkg.dependencies[0]}"
        )
        print(
            f"{pkg.name} {pkg.version}: "
            + ", ".join(sorted(f.name for f in dist.iterdir()))
            + f" ({what})"
        )
    return 1 if problems else 0


def _cmd_verify(args: argparse.Namespace) -> int:
    uploads = json.loads(args.uploads)
    problems = verify_problems(
        uploads, Path(args.dist_root), http_fetch, timeout=args.timeout
    )
    lines = ["### Uploads verified on PyPI", ""]
    lines += (
        [f"- `{u['name']}` {u['version']}" for u in uploads] if not problems else []
    )
    lines += [f"- :x: {p}" for p in problems]
    _write_github("GITHUB_STEP_SUMMARY", "\n".join(lines) + "\n")
    for p in problems:
        print(f"::error::{p}")
    if not problems:
        print(f"verified {len(uploads)} upload(s) on PyPI")
    return 1 if problems else 0


def _cmd_coverage(args: argparse.Namespace) -> int:
    pipe = make_tree(args.pipe_root, args.pipe_ref)
    consumer = make_tree(args.consumer_root, args.consumer_ref)
    needs = coverage(pipe, consumer)
    print(f"pipe:     {pipe.describe()}\nconsumer: {consumer.describe()}\n")
    width = max((len(n.requirement) for n in needs), default=10)
    src_width = max((len(n.source) for n in needs), default=10)
    by_width = max((len(n.provided_by) for n in needs), default=10)
    for n in needs:
        print(
            f"{n.requirement:<{width}}  {n.source:<{src_width}}  "
            f"{n.provided_by or '-':<{by_width}}  {n.verdict}"
        )
    failed = [n for n in needs if n.verdict.startswith("FAIL")]
    print(f"\n{len(needs)} requirement(s), {len(failed)} failing")
    return 1 if failed or not needs else 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("plan", help="decide what a publish run builds and uploads")
    p.add_argument("--root", default=".")
    p.add_argument(
        "--target", default=TARGET_FRAMEWORK, choices=[TARGET_FRAMEWORK, TARGET_SHIMS]
    )
    p.add_argument("--event", default="pull_request", help="github.event_name")
    p.add_argument("--tag", default="", help="the release tag (release events)")
    p.add_argument(
        "--dry-run",
        default="true",
        type=lambda s: s.lower() == "true",
        help="true/false",
    )
    p.add_argument("--pypi", action="store_true", help="look each version up on PyPI")
    p.add_argument("--json", action="store_true", help="also print the outputs as JSON")
    p.set_defaults(func=_cmd_plan)

    p = sub.add_parser("build", help="build one package into a dist directory")
    p.add_argument("--root", default=".")
    p.add_argument("--path", required=True, help='"." or shims/<name>')
    p.add_argument("--outdir", required=True)
    p.set_defaults(func=_cmd_build)

    p = sub.add_parser("check-dist", help="check one package's built dist directory")
    p.add_argument("--root", default=".")
    p.add_argument("--path", required=True, help='"." or shims/<name>')
    p.add_argument("dist")
    p.set_defaults(func=_cmd_check_dist)

    p = sub.add_parser(
        "verify", help="check each upload landed on PyPI with this run's sha256"
    )
    p.add_argument(
        "--uploads", required=True, help="the plan's `uploads` output (JSON)"
    )
    p.add_argument("--dist-root", required=True, help="holds dist-<name>/ per package")
    p.add_argument("--timeout", type=float, default=180.0)
    p.set_defaults(func=_cmd_verify)

    p = sub.add_parser(
        "coverage",
        help="check a consumer's aisquare-pipe* requirements against a pipe tree",
    )
    p.add_argument("--pipe-root", default=".")
    p.add_argument(
        "--pipe-ref", help="read pipe from this git ref instead of the working tree"
    )
    p.add_argument("--consumer-root", required=True)
    p.add_argument("--consumer-ref", help="read the consumer from this git ref")
    p.set_defaults(func=_cmd_coverage)

    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except ReleaseError as exc:
        print(f"::error::{exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
