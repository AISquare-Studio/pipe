#!/usr/bin/env python3
"""The packages this monorepo publishes to PyPI, and the checks around publishing them.

``.github/workflows/publish.yml`` takes its build matrix from ``plan``, so no
package can be left out of a release. A package is the root ``pyproject.toml``
(``aisquare-pipe``) plus every ``connectors/*/pyproject.toml``.

Subcommands:

    plan        List every package and check the set: each version is static
                and valid, each ``aisquare-pipe*`` requirement between the
                packages is satisfied by a version declared here, and on a
                release the tag is ``v<aisquare-pipe version>``. With ``--pypi``,
                look each version up on PyPI and decide what to upload. Writes
                GitHub Actions outputs and a step summary when run in Actions.
    check-dist  A built dist directory holds exactly one wheel and one sdist,
                both named for the package's declared name and version.
    verify      After an upload: each planned upload is on PyPI, with the
                sha256 of the file this run built.
    coverage    Every ``aisquare-pipe*`` requirement of a consumer
                (pipe-as-service's pyproject.toml / requirements.in /
                requirements.lock), followed through extras and the packages'
                own dependencies, is a package here, at a declared version
                that satisfies it.

Examples:

    python scripts/release_packages.py plan
    python scripts/release_packages.py plan --pypi --event release --tag v0.4.0
    python scripts/release_packages.py check-dist --path connectors/slack dist/
    python scripts/release_packages.py coverage \\
        --pipe-root . --pipe-ref origin/release/pipe-router \\
        --consumer-root ../pipe-as-service --consumer-ref origin/release/pipe-router

Needs ``packaging`` (pytest, build and twine all depend on it). Reads files
only: nothing is imported from the packages and nothing is uploaded.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import tomllib
import urllib.error
import urllib.request
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
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

FAMILY = "aisquare-pipe"  # every distribution here is this or aisquare-pipe-<name>
PYPI = "https://pypi.org"
USER_AGENT = "aisquare-pipe-release-check (+https://github.com/AISquare-Studio/pipe)"

# A pip-compile lock line: ``name==version \`` (extras are stripped by --strip-extras,
# but tolerate them). Comments and hash lines are indented and never match.
_LOCK_PIN = re.compile(
    r"^(?P<name>[A-Za-z0-9][A-Za-z0-9._-]*)(?:\[[^\]]*\])?==(?P<version>[^\s\\;]+)"
)


class ReleaseError(Exception):
    """The package set can't be read or released as it stands."""


# --------------------------------------------------------------------------- trees


class Tree:
    """Read-only view of a checkout: a directory on disk or a git ref."""

    def read(self, rel: str) -> str | None:
        raise NotImplementedError

    def connector_dirs(self) -> list[str]:
        """``connectors/<name>`` for each connector dir holding a pyproject.toml."""
        raise NotImplementedError

    def describe(self) -> str:
        raise NotImplementedError


@dataclass(frozen=True)
class FsTree(Tree):
    root: Path

    def read(self, rel: str) -> str | None:
        path = self.root / rel
        return path.read_text(encoding="utf-8") if path.is_file() else None

    def connector_dirs(self) -> list[str]:
        base = self.root / "connectors"
        if not base.is_dir():
            return []
        return sorted(
            f"connectors/{d.name}"
            for d in base.iterdir()
            if (d / "pyproject.toml").is_file()
        )

    def describe(self) -> str:
        return str(self.root)


@dataclass(frozen=True)
class GitTree(Tree):
    repo: Path
    ref: str

    def _git(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", "-C", str(self.repo), *args],
            capture_output=True,
            text=True,
            check=False,
        )

    def read(self, rel: str) -> str | None:
        done = self._git("show", f"{self.ref}:{rel}")
        return done.stdout if done.returncode == 0 else None

    def connector_dirs(self) -> list[str]:
        done = self._git("ls-tree", "-r", "--name-only", self.ref, "--", "connectors")
        if done.returncode != 0:
            raise ReleaseError(
                f"git ls-tree {self.ref} in {self.repo}: {done.stderr.strip()}"
            )
        found = (
            re.fullmatch(r"(connectors/[^/]+)/pyproject\.toml", line)
            for line in done.stdout.splitlines()
        )
        return sorted(m.group(1) for m in found if m)

    def describe(self) -> str:
        return f"{self.repo}@{self.ref}"


def make_tree(root: str, ref: str | None) -> Tree:
    return GitTree(Path(root), ref) if ref else FsTree(Path(root))


# ------------------------------------------------------------------------ packages


@dataclass(frozen=True)
class Package:
    path: str  # "." or "connectors/<name>", relative to the repo root
    name: str  # the distribution name as its pyproject spells it
    version: str
    dependencies: tuple[str, ...] = ()
    extras: dict[str, tuple[str, ...]] = field(default_factory=dict)

    @property
    def key(self) -> str:
        return canonicalize_name(self.name)

    @property
    def is_framework(self) -> bool:
        return self.path == "."

    def matrix_entry(self) -> dict[str, str]:
        return {"path": self.path, "name": self.name, "version": self.version}


def _load_package(tree: Tree, path: str) -> Package:
    rel = "pyproject.toml" if path == "." else f"{path}/pyproject.toml"
    text = tree.read(rel)
    if text is None:
        raise ReleaseError(f"{rel}: not found in {tree.describe()}")
    try:
        project = tomllib.loads(text).get("project", {})
    except tomllib.TOMLDecodeError as exc:
        raise ReleaseError(f"{rel}: {exc}") from exc
    name, version = project.get("name"), project.get("version")
    if "version" in project.get("dynamic", []) or not isinstance(version, str):
        raise ReleaseError(
            f"{rel}: [project] version must be a static string; the publish run reads it "
            "from here to decide what to upload"
        )
    if not isinstance(name, str):
        raise ReleaseError(f"{rel}: [project] has no name")
    try:
        Version(version)
    except InvalidVersion as exc:
        raise ReleaseError(
            f"{rel}: version {version!r} is not a PEP 440 version"
        ) from exc
    key = canonicalize_name(name)
    if key != FAMILY and not key.startswith(f"{FAMILY}-"):
        raise ReleaseError(f"{rel}: {name!r} is not {FAMILY} or {FAMILY}-<name>")
    extras = {
        str(extra): tuple(reqs)
        for extra, reqs in project.get("optional-dependencies", {}).items()
    }
    return Package(
        path=path,
        name=name,
        version=version,
        dependencies=tuple(project.get("dependencies", [])),
        extras=extras,
    )


def discover(tree: Tree) -> list[Package]:
    """Every package in the tree: the framework first, then connectors by directory."""
    packages = [_load_package(tree, ".")]
    packages += [_load_package(tree, d) for d in tree.connector_dirs()]
    seen: dict[str, str] = {}
    for pkg in packages:
        if pkg.key in seen:
            raise ReleaseError(
                f"{pkg.path} and {seen[pkg.key]} both declare {pkg.name}"
            )
        seen[pkg.key] = pkg.path
    if not packages[0].is_framework or packages[0].key != FAMILY:
        raise ReleaseError(
            f"the root pyproject.toml must be {FAMILY}, not {packages[0].name}"
        )
    return packages


def _family_requirement(spec: str) -> Requirement | None:
    """The requirement, if it names an aisquare-pipe* distribution (markers ignored)."""
    try:
        req = Requirement(spec)
    except InvalidRequirement:
        return None
    key = canonicalize_name(req.name)
    return req if key == FAMILY or key.startswith(f"{FAMILY}-") else None


def _satisfies(req: Requirement, version: str) -> bool:
    return req.specifier.contains(Version(version), prereleases=True)


def internal_problems(packages: Sequence[Package]) -> list[str]:
    """Each aisquare-pipe* requirement a package declares (dependencies and extras)
    must name a package here, at a declared version that satisfies it. Otherwise
    this set, uploaded as it stands, could not be installed together."""
    by_key = {p.key: p for p in packages}
    problems = []
    for pkg in packages:
        declared = [("dependencies", s) for s in pkg.dependencies]
        declared += [
            (f"[{extra}]", s) for extra, specs in pkg.extras.items() for s in specs
        ]
        for where, spec in declared:
            req = _family_requirement(spec)
            if req is None:
                continue
            target = by_key.get(canonicalize_name(req.name))
            if target is None:
                problems.append(
                    f"{pkg.name} {where} requires {spec!r}: no package here is named {req.name}"
                )
            elif not _satisfies(req, target.version):
                problems.append(
                    f"{pkg.name} {where} requires {spec!r}, but {target.path} declares "
                    f"{target.name} {target.version}"
                )
            for extra in req.extras:
                if target is not None and extra not in target.extras:
                    problems.append(
                        f"{pkg.name} {where} requires {spec!r}: {target.name} has no [{extra}] extra"
                    )
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
NEW_PROJECT = "new project"  # the project doesn't exist: the upload needs a pending trusted publisher
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


def pypi_state(pkg: Package, fetch: Fetch) -> PypiState:
    status, body = fetch(f"{PYPI}/pypi/{pkg.key}/{pkg.version}/json")
    if status == 200:
        files = {
            u["filename"]: u["digests"]["sha256"]
            for u in json.loads(body).get("urls", [])
        }
        if not files:
            raise ReleaseError(
                f"{pkg.name} {pkg.version} is registered on PyPI with no files. PyPI never "
                "accepts a filename twice, so this version can't be uploaded: bump it"
            )
        return PypiState(PUBLISHED, files)
    if status != 404:
        raise ReleaseError(f"PyPI lookup of {pkg.name} {pkg.version}: HTTP {status}")
    status, _ = fetch(f"{PYPI}/pypi/{pkg.key}/json")
    if status == 404:
        return PypiState(NEW_PROJECT)
    if status == 200:
        return PypiState(NEW_VERSION)
    raise ReleaseError(f"PyPI lookup of {pkg.name}: HTTP {status}")


# --------------------------------------------------------------------------- plan


@dataclass
class Plan:
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
            "packages": json.dumps([p.matrix_entry() for p in self.packages]),
            "upload": json.dumps(self.upload and bool(uploads)),
            "connectors": json.dumps(
                [p.matrix_entry() for p in uploads if not p.is_framework]
            ),
            "framework": json.dumps(any(p.is_framework for p in uploads)),
            "uploads": json.dumps([p.matrix_entry() for p in uploads]),
        }

    def summary(self) -> str:
        mode = "publish" if self.upload else "dry run (nothing is uploaded)"
        lines = [
            f"### PyPI {mode}",
            "",
            "| Package | Path | Version | On PyPI | This run |",
            "|---|---|---|---|---|",
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
                on_pypi = (
                    "no (new project: needs a pending trusted publisher)"
                    if st.state == NEW_PROJECT
                    else "no"
                )
                action = "**upload**" if self.upload else "would upload"
            lines.append(
                f"| `{p.name}` | `{p.path}` | {p.version} | {on_pypi} | {action} |"
            )
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
) -> Plan:
    """What a publish run does. ``fetch`` is None to skip the PyPI lookups."""
    packages = discover(tree)
    upload = event == "release" or (event == "workflow_dispatch" and not dry_run)
    plan = Plan(packages=packages, upload=upload)
    plan.problems += internal_problems(packages)
    framework = packages[0]
    if event == "release" and tag != f"v{framework.version}":
        plan.problems.append(
            f"release tag {tag!r} does not match {framework.name} {framework.version} in "
            f"pyproject.toml: tag the release v{framework.version}, or bump pyproject.toml "
            "first (publish connectors alone with Run workflow, dry-run unticked)"
        )
    if upload and fetch is None:
        plan.problems.append("a publish run must look versions up on PyPI (--pypi)")
    if fetch is None:
        return plan
    for pkg in packages:
        try:
            plan.states[pkg.key] = pypi_state(pkg, fetch)
        except ReleaseError as exc:
            if upload:
                plan.problems.append(str(exc))
            else:
                plan.states[pkg.key] = PypiState(UNKNOWN, detail=str(exc))
                plan.warnings.append(str(exc))
    if upload and not plan.problems and not plan.to_upload():
        plan.warnings.append(
            "every package's version is already on PyPI: nothing to upload"
        )
    return plan


# ------------------------------------------------------------------ dist + verify


def dist_problems(pkg: Package, dist: Path) -> list[str]:
    """The dist dir holds one wheel and one sdist, named for the declared name and version."""
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
                f"{fname} is {name} {version}, but {pkg.path}/pyproject.toml declares {pkg.name} {pkg.version}"
            )
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
        str, tuple[Package, dict[str, str]]
    ] = {}  # key -> (package, {filename: sha256})
    for entry in uploads:
        pkg = Package(path=entry["path"], name=entry["name"], version=entry["version"])
        local_dir = dist_root / f"dist-{pkg.name}"
        local = (
            {f.name: sha256(f) for f in sorted(local_dir.iterdir()) if f.is_file()}
            if local_dir.is_dir()
            else {}
        )
        if local:
            built[pkg.key] = (pkg, local)
        else:
            problems.append(f"{pkg.name} {pkg.version}: no built files in {local_dir}")
    on_pypi: dict[
        str, dict[str, str]
    ] = {}  # key -> {filename: sha256} as PyPI lists them
    pending = set(built)
    deadline = clock() + timeout
    while True:
        for key in sorted(pending):
            pkg, local = built[key]
            try:
                on_pypi[key] = pypi_state(pkg, fetch).files
            except ReleaseError:
                on_pypi[key] = {}
            if set(local) <= set(on_pypi[key]):
                pending.discard(key)
        if not pending or clock() >= deadline:
            break
        sleep(interval)
    for key, (pkg, local) in built.items():
        missing = sorted(set(local) - set(on_pypi.get(key, {})))
        if missing:
            problems.append(
                f"{pkg.name} {pkg.version}: not on PyPI after the upload: {', '.join(missing)}"
            )
        for fname, digest in sorted(local.items()):
            remote = on_pypi.get(key, {}).get(fname)
            if remote is not None and remote != digest:
                problems.append(
                    f"{pkg.name} {pkg.version}: PyPI's {fname} has sha256 {remote}, not this "
                    f"run's {digest} (an older upload of the same version?)"
                )
    return problems


# ------------------------------------------------------------------------ coverage


@dataclass(frozen=True)
class Need:
    requirement: str  # as the consumer (or a package's metadata) wrote it
    source: str  # where it came from, e.g. "requirements.in" or "aisquare-pipe[full]"
    package: str  # the package path that provides it, "" if none
    declared: str  # that package's declared version
    verdict: (
        str  # "ok", "ok (pin below declared: relock after publishing)" or "FAIL: ..."
    )


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
    req_in = tree.read("requirements.in")
    for line in (req_in or "").splitlines():
        line = line.split("#", 1)[0].strip()
        if line and not line.startswith("-"):
            found.append(("requirements.in", line))
    lock = tree.read("requirements.lock")
    for line in (lock or "").splitlines():
        m = _LOCK_PIN.match(line)
        if m:
            found.append(("requirements.lock", f"{m['name']}=={m['version']}"))
    return own, found


def coverage(pipe: Tree, consumer: Tree) -> list[Need]:
    """Every aisquare-pipe* requirement of the consumer, followed through the
    extras it asks for and each package's own aisquare-pipe* dependencies."""
    packages = {p.key: p for p in discover(pipe)}
    own, found = consumer_requirements(consumer)
    own_key = canonicalize_name(own) if own else ""
    queue: list[tuple[str, str]] = [(src, spec) for src, spec in found]
    seen: set[tuple[str, str]] = set()
    needs: list[Need] = []
    while queue:
        source, spec = queue.pop(0)
        req = _family_requirement(spec)
        if req is None or canonicalize_name(req.name) == own_key:
            continue  # not ours, or the consumer's self-reference (e.g. svc[toolkits])
        if (source, spec) in seen:
            continue
        seen.add((source, spec))
        pkg = packages.get(canonicalize_name(req.name))
        if pkg is None:
            needs.append(
                Need(
                    spec,
                    source,
                    "",
                    "",
                    f"FAIL: no package named {req.name} in {pipe.describe()}",
                )
            )
            continue
        pins = list(req.specifier)
        if _satisfies(req, pkg.version):
            verdict = "ok"
        elif (
            pins
            and all(s.operator in ("==", "===") for s in pins)
            and all(Version(s.version) <= Version(pkg.version) for s in pins)
        ):
            verdict = "ok (pinned below the declared version: relock after publishing)"
        else:
            verdict = f"FAIL: needs {req.specifier}, {pkg.path} declares {pkg.version}"
        needs.append(Need(spec, source, pkg.path, pkg.version, verdict))
        for extra in sorted(req.extras):
            if extra not in pkg.extras:
                needs.append(
                    Need(
                        spec,
                        source,
                        pkg.path,
                        pkg.version,
                        f"FAIL: {pkg.name} has no [{extra}] extra",
                    )
                )
                continue
            queue += [(f"{pkg.name}[{extra}]", s) for s in pkg.extras[extra]]
        queue += [(f"{pkg.name} dependencies", s) for s in pkg.dependencies]
    return needs


# ----------------------------------------------------------------------------- CLI


def _write_github(name: str, text: str) -> None:
    target = os.environ.get(name)
    if target:
        with open(target, "a", encoding="utf-8") as fh:
            fh.write(text)


def _cmd_plan(args: argparse.Namespace) -> int:
    plan = make_plan(
        make_tree(args.root, None),
        event=args.event,
        tag=args.tag,
        dry_run=args.dry_run,
        fetch=http_fetch if args.pypi else None,
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


def _cmd_check_dist(args: argparse.Namespace) -> int:
    pkg = next(
        (p for p in discover(make_tree(args.root, None)) if p.path == args.path), None
    )
    if pkg is None:
        print(f"::error::{args.path} is not a package directory here", file=sys.stderr)
        return 1
    problems = dist_problems(pkg, Path(args.dist))
    for p in problems:
        print(f"::error::{p}")
    if not problems:
        print(
            f"{pkg.name} {pkg.version}: "
            + ", ".join(sorted(f.name for f in Path(args.dist).iterdir()))
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
    for n in needs:
        print(
            f"{n.requirement:<{width}}  {n.source:<{src_width}}  {n.package or '-':<26} {n.declared or '-':<8} {n.verdict}"
        )
    failed = [n for n in needs if n.verdict.startswith("FAIL")]
    print(f"\n{len(needs)} requirement(s), {len(failed)} failing")
    return 1 if failed or not needs else 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser(
        "plan", help="list the packages and decide what a publish run uploads"
    )
    p.add_argument("--root", default=".")
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

    p = sub.add_parser("check-dist", help="check one package's built dist directory")
    p.add_argument("--root", default=".")
    p.add_argument(
        "--path", required=True, help='the package path, "." or connectors/<name>'
    )
    p.add_argument("dist")
    p.set_defaults(func=_cmd_check_dist)

    p = sub.add_parser(
        "verify", help="check each upload landed on PyPI with this run's sha256"
    )
    p.add_argument(
        "--uploads", required=True, help="the plan's `uploads` output (JSON)"
    )
    p.add_argument("--dist-root", required=True, help="holds dist-<name>/ per package")
    p.add_argument("--timeout", type=float, default=300.0)
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
