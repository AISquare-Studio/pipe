"""Repo-hygiene checks for connector directories (Layer B of ``pipe validate``).

Operates on a repo checkout's ``connectors/<dir>/`` trees and codifies the
connector-PR review rubric: packaging rules, scaffold completeness, code
isolation, and install-state. Pure functions over paths + injected dist
info, so tests can drive them against ``tmp_path`` fakes.

Every connector ships inside the one ``aisquare-pipe`` distribution (D8):
the root ``pyproject.toml`` packages ``connectors/<dir>/src``, declares the
connector's entry points, and holds its third-party deps as the
``aisquare-pipe[<dir>]`` extra. ``connectors/<dir>/pyproject.toml`` holds
only the tool settings its test suite runs with (pytest, ruff).
"""

from __future__ import annotations

import ast
import json
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import url2pathname

from aisquare.pipe.testing.validation import (
    CheckResult,
    DiscoveredConnector,
    Severity,
    no_network,
)

FRAMEWORK_DIST = "aisquare-pipe"
ENTRY_POINT_GROUP = "aisquare_pipe.connectors"
TOOLKIT_ENTRY_POINT_GROUP = "aisquare_pipe.toolkits"
CONVERTER_ENTRY_POINT_GROUP = "aisquare_pipe.converters"
ENTRY_POINT_GROUPS = (ENTRY_POINT_GROUP, TOOLKIT_ENTRY_POINT_GROUP, CONVERTER_ENTRY_POINT_GROUP)
#: The bundle every connector's extra belongs to.
FULL_EXTRA = "full"
#: How the repo is installed for development (and by ``--install``).
DEV_INSTALL = 'pip install -e ".[full,dev]"'

INSTALL_OK = "ok"
INSTALL_BROKEN = "broken"
INSTALL_MISSING = "missing"

_SELF_EXTRAS = re.compile(r"^\s*aisquare-pipe\s*\[([^\]]*)\]", re.IGNORECASE)


def canonical(name: str) -> str:
    """A distribution or extra name as pip compares them (PEP 503 / 685)."""
    return re.sub(r"[-_.]+", "-", name).lower()


@dataclass
class RootProject:
    """The packaging facts of the root ``pyproject.toml`` (``aisquare-pipe``)."""

    data: dict
    error: str | None = None

    @classmethod
    def load(cls, repo_root: Path) -> RootProject:
        try:
            with (repo_root / "pyproject.toml").open("rb") as f:
                return cls(tomllib.load(f))
        except (OSError, tomllib.TOMLDecodeError) as e:
            return cls({}, error=str(e))

    @property
    def project(self) -> dict:
        return self.data.get("project", {})

    @property
    def version(self) -> str | None:
        return self.project.get("version")

    @property
    def extras(self) -> dict[str, list[str]]:
        """Extras by canonical name."""
        raw = self.project.get("optional-dependencies", {}) or {}
        return {canonical(k): list(v or []) for k, v in raw.items()}

    @property
    def package_dirs(self) -> list[str]:
        """``[tool.setuptools.packages.find] where``, as written."""
        find = self.data.get("tool", {}).get("setuptools", {}).get("packages", {})
        where = find.get("find", {}).get("where", []) if isinstance(find, dict) else []
        return [str(w).rstrip("/") for w in where]

    def entry_points(self, group: str) -> dict[str, str]:
        return dict(self.project.get("entry-points", {}).get(group, {}) or {})

    def bundle(self, extra: str) -> set[str]:
        """Every extra ``extra`` pulls in through ``aisquare-pipe[...]``
        self-references, itself included (canonical names)."""
        seen: set[str] = set()
        todo = [canonical(extra)]
        while todo:
            name = todo.pop()
            if name in seen:
                continue
            seen.add(name)
            for req in self.extras.get(name, []):
                m = _SELF_EXTRAS.match(req)
                if m:
                    todo += [canonical(e) for e in m.group(1).split(",") if e.strip()]
        return seen


@dataclass
class ConnectorDir:
    """A ``connectors/<dir>/`` tree in the repo checkout."""

    dir_name: str
    path: Path
    #: ``connectors/<dir>/pyproject.toml``: tool settings only.
    pyproject: dict | None
    pyproject_error: str | None
    root: RootProject = field(default_factory=lambda: RootProject({}))

    @property
    def safe_name(self) -> str:
        return self.dir_name.replace("-", "_")

    @property
    def module_name(self) -> str:
        return f"aisquare_pipe_{self.safe_name}"

    @property
    def extra(self) -> str:
        """The extra carrying this connector's third-party deps."""
        return canonical(self.dir_name)

    @property
    def requirement(self) -> str:
        """How to install it: ``aisquare-pipe[<dir>]``."""
        return f"{FRAMEWORK_DIST}[{self.dir_name}]"

    @property
    def dist_name(self) -> str:
        """The distribution it ships in, with its extra (a report label)."""
        return self.requirement

    def entry_points(self, group: str) -> dict[str, str]:
        """This connector's entry points in ``group``, as the root pyproject
        declares them: those whose module is ``aisquare_pipe_<dir>``."""
        return {
            name: value
            for name, value in self.root.entry_points(group).items()
            if value.split(":", 1)[0].split(".", 1)[0] == self.module_name
        }

    def declared_entry_points(self) -> set[str]:
        """Its connector and toolkit entry points (the groups ``pipe validate``
        loads)."""
        return {
            name
            for group in (ENTRY_POINT_GROUP, TOOLKIT_ENTRY_POINT_GROUP)
            for name in self.entry_points(group)
        }

    @property
    def registers_toolkit(self) -> bool:
        return bool(self.entry_points(TOOLKIT_ENTRY_POINT_GROUP))

    @property
    def registers_connector(self) -> bool:
        """True when connector entry points are registered — or when nothing
        is, so a bare directory is still held to the connector scaffold."""
        return bool(self.entry_points(ENTRY_POINT_GROUP)) or not self.registers_toolkit


@dataclass
class DistInfo:
    """Installed facts for one connector (injectable in tests): the framework
    distribution it ships in, and how its own entry points loaded."""

    found: bool
    version: str | None = None
    editable_path: Path | None = None
    ep_load_errors: dict[str, str] = field(default_factory=dict)
    #: Requirements of the connector's extra that aren't installed.
    missing_requirements: list[str] = field(default_factory=list)
    #: Its entry points the installed metadata lists (None: not looked up).
    installed_entry_points: set[str] | None = None


def find_repo_root(start: Path | None = None) -> Path | None:
    """Walk up from ``start`` (default cwd) to the aisquare-pipe repo root:
    the directory holding a ``connectors/`` dir and a pyproject whose
    project name is ``aisquare-pipe``."""
    current = (start or Path.cwd()).resolve()
    for candidate in (current, *current.parents):
        pyproject = candidate / "pyproject.toml"
        if not pyproject.is_file() or not (candidate / "connectors").is_dir():
            continue
        try:
            with pyproject.open("rb") as f:
                data = tomllib.load(f)
        except (OSError, tomllib.TOMLDecodeError):
            continue
        if data.get("project", {}).get("name") == FRAMEWORK_DIST:
            return candidate
    return None


def list_connector_dirs(repo_root: Path) -> list[ConnectorDir]:
    """Every ``connectors/<dir>/`` (each is, or should be, a connector),
    sorted by name."""
    root = RootProject.load(repo_root)
    dirs: list[ConnectorDir] = []
    connectors = repo_root / "connectors"
    for path in sorted(connectors.iterdir()):
        if not path.is_dir() or path.name.startswith((".", "_")):
            continue
        pyproject_path = path / "pyproject.toml"
        pyproject: dict | None = None
        error: str | None = None
        if pyproject_path.is_file():
            try:
                with pyproject_path.open("rb") as f:
                    pyproject = tomllib.load(f)
            except (OSError, tomllib.TOMLDecodeError) as e:
                error = str(e)
        else:
            error = "pyproject.toml not found (it holds the suite's pytest settings)"
        dirs.append(
            ConnectorDir(
                dir_name=path.name,
                path=path,
                pyproject=pyproject,
                pyproject_error=error,
                root=root,
            )
        )
    return dirs


def owner_module(ep_value: str) -> str:
    """The top-level module an entry point's value lives in."""
    return ep_value.split(":", 1)[0].split(".", 1)[0]


def _file_url_to_path(url: str) -> Path:
    """Convert a ``file://`` URL from ``direct_url.json`` into a real path.

    Stripping the scheme textually is wrong on Windows: pip writes
    ``file:///N:/repo``, whose path component is ``/N:/repo`` — a leading
    slash that makes the result compare unequal to every real directory, so
    every editable connector looks like a broken install. It also leaves
    percent-escapes (``%20`` for a space) in place on all platforms.
    ``url2pathname`` handles both, and is a no-op for ordinary POSIX paths.
    """
    return Path(url2pathname(urlparse(url).path))


def _framework_dist_facts() -> tuple[bool, str | None, Path | None]:
    """(installed, version, checkout path) of the installed ``aisquare-pipe``.
    The checkout path comes from ``direct_url.json`` and is set only for an
    install from a directory (``pip install [-e] <repo>``), not from a wheel
    or an index."""
    from importlib.metadata import PackageNotFoundError, distribution

    try:
        dist = distribution(FRAMEWORK_DIST)
    except PackageNotFoundError:
        return False, None, None
    editable_path: Path | None = None
    try:
        raw = dist.read_text("direct_url.json")
        if raw:
            info = json.loads(raw)
            url = info.get("url", "")
            if "dir_info" in info and url.startswith("file://"):
                editable_path = _file_url_to_path(url)
    except Exception:  # noqa: BLE001 — absent/odd metadata is not an error
        editable_path = None
    return True, dist.version, editable_path


def load_dist_info(cdir: ConnectorDir, discovered: list[DiscoveredConnector]) -> DistInfo:
    """Installed facts for one connector: the framework distribution's
    version and checkout, and how this connector's entry points loaded. An
    entry point that failed only because the connector's extra isn't
    installed is a missing requirement, not a load error."""
    found, version, editable_path = _framework_dist_facts()
    if not found:
        return DistInfo(found=False)
    own = [d for d in discovered if owner_module(d.ep_value) == cdir.module_name]
    missing = sorted({req for d in own for req in d.missing_requirements})
    ep_load_errors = {
        d.ep_name: d.load_error for d in own if d.load_error and not d.missing_requirements
    }
    return DistInfo(
        found=True,
        version=version,
        editable_path=editable_path,
        ep_load_errors=ep_load_errors,
        missing_requirements=missing,
        installed_entry_points={d.ep_name for d in own},
    )


def stale_entry_points(cdir: ConnectorDir, dist: DistInfo) -> list[str]:
    """Entry points the root pyproject declares for this connector that the
    installed metadata doesn't list: the install predates them."""
    if dist.installed_entry_points is None:
        return []
    return sorted(cdir.declared_entry_points() - dist.installed_entry_points)


def classify_install_state(cdir: ConnectorDir, dist: DistInfo) -> str:
    if not dist.found or dist.missing_requirements:
        return INSTALL_MISSING
    if dist.ep_load_errors or stale_entry_points(cdir, dist):
        return INSTALL_BROKEN
    if dist.editable_path is not None:
        try:
            if dist.editable_path.resolve() != cdir.path.resolve().parents[1]:
                return INSTALL_BROKEN
        except (OSError, IndexError):
            return INSTALL_BROKEN
    return INSTALL_OK


# --------------------------------------------------------------------------
# Individual hygiene rules
# --------------------------------------------------------------------------


def _result(check_id: str, ok: bool, message: str, severity: Severity = Severity.ERROR) -> CheckResult:
    return CheckResult(check_id, ok, severity=severity, message="" if ok else message)


def _iter_src_files(cdir: ConnectorDir) -> list[Path]:
    src = cdir.path / "src"
    return sorted(src.rglob("*.py")) if src.is_dir() else []


def _check_pyproject_rules(cdir: ConnectorDir) -> list[CheckResult]:
    """The connector's own pyproject holds tool settings only; the root
    pyproject packages it, declares its entry points and its extra."""
    rel = f"connectors/{cdir.dir_name}"
    results = [
        _result(
            "hygiene.pyproject-parses",
            cdir.pyproject is not None,
            f"{rel}/pyproject.toml: {cdir.pyproject_error}",
        )
    ]
    if cdir.pyproject is not None:
        tables = sorted(t for t in ("project", "build-system") if t in cdir.pyproject)
        results.append(
            _result(
                "hygiene.not-a-package",
                not tables,
                f"{rel}/pyproject.toml has [{'], ['.join(tables)}]: connectors ship inside "
                f"{FRAMEWORK_DIST}, packaged by the root pyproject.toml. Keep only [tool.*] here",
            )
        )
    root = cdir.root
    results.append(
        _result(
            "hygiene.packaged",
            f"{rel}/src" in root.package_dirs,
            f'root pyproject.toml: [tool.setuptools.packages.find] where lacks "{rel}/src", '
            f"so {cdir.module_name} is not in the {FRAMEWORK_DIST} wheel",
        )
    )
    results.append(
        _result(
            "hygiene.extra-declared",
            cdir.extra in root.extras,
            f"root pyproject.toml: no [project.optional-dependencies] {cdir.dir_name} = [...] "
            f"(its third-party deps; [] when it has none), so {cdir.requirement} does not exist",
        )
    )
    eps = cdir.entry_points(ENTRY_POINT_GROUP) or cdir.entry_points(TOOLKIT_ENTRY_POINT_GROUP)
    results.append(
        _result(
            "hygiene.entry-points-registered",
            bool(eps),
            f'root pyproject.toml: no [project.entry-points."{ENTRY_POINT_GROUP}"] (or '
            f'"{TOOLKIT_ENTRY_POINT_GROUP}") entry pointing into {cdir.module_name}',
        )
    )
    return results


def _check_scaffold_files(cdir: ConnectorDir) -> list[CheckResult]:
    module_dir = cdir.path / "src" / cdir.module_name
    missing: list[str] = []
    required = ["README.md", f"src/{cdir.module_name}/__init__.py"]
    if cdir.registers_connector:
        required.append(f"src/{cdir.module_name}/connector.py")
    required.append("tests")
    for rel in required:
        if not (cdir.path / rel).exists():
            missing.append(rel)

    compliance_text = ""
    compliance_file = cdir.path / "tests" / "test_compliance.py"
    if compliance_file.is_file():
        try:
            compliance_text = compliance_file.read_text(encoding="utf-8")
        except OSError:
            compliance_text = ""
    suites = []
    if cdir.registers_connector:
        suites.append("connector_compliance_suite(")
    if cdir.registers_toolkit:
        suites.append("toolkit_compliance_suite(")
    for suite in suites:
        if suite not in compliance_text:
            missing.append(f"tests/test_compliance.py (with {suite}...))")

    return [
        _result(
            "hygiene.scaffold-files",
            not missing,
            f"missing required files: {', '.join(missing)}",
        ),
        _result(
            "hygiene.module-name",
            module_dir.is_dir(),
            f"src/{cdir.module_name}/ not found — module name must match the directory name",
        ),
    ]


def _check_source_code(cdir: ConnectorDir) -> list[CheckResult]:
    cross_imports: list[str] = []
    bare_excepts: list[str] = []
    own_module = cdir.module_name

    for py_file in _iter_src_files(cdir):
        try:
            tree = ast.parse(py_file.read_text(encoding="utf-8"))
        except (OSError, SyntaxError) as e:
            return [
                _result(
                    "hygiene.no-cross-connector-imports",
                    False,
                    f"{py_file.relative_to(cdir.path)} cannot be parsed: {e}",
                )
            ]
        rel = str(py_file.relative_to(cdir.path))
        for node in ast.walk(tree):
            modules: list[str] = []
            lineno = getattr(node, "lineno", 0)
            if isinstance(node, ast.Import):
                modules = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                modules = [node.module]
            for module in modules:
                root = module.split(".", 1)[0]
                if root.startswith("aisquare_pipe_") and root != own_module:
                    cross_imports.append(f"{rel}:{lineno} imports {root}")
            if isinstance(node, ast.ExceptHandler) and node.type is None:
                bare_excepts.append(f"{rel}:{lineno}")

    return [
        _result(
            "hygiene.no-cross-connector-imports",
            not cross_imports,
            "connectors compose via the framework, never each other: " + "; ".join(cross_imports),
        ),
        _result(
            "hygiene.no-bare-except",
            not bare_excepts,
            "bare `except:` at " + ", ".join(bare_excepts),
        ),
    ]


_CLASS_VERSION_RE = re.compile(r"^\s+version\s*=\s*[\"']([^\"']+)[\"']", re.MULTILINE)


def _class_versions(cdir: ConnectorDir, loaded_classes: dict[str, type]) -> set[str]:
    """Connector-class versions: from loaded entry-point classes when
    importable, else a regex sweep over the source files."""
    own = [
        cls
        for cls in loaded_classes.values()
        if cls.__module__.split(".", 1)[0] == cdir.module_name
    ]
    if own:
        return {str(getattr(cls, "version", "")) for cls in own}
    versions: set[str] = set()
    for py_file in _iter_src_files(cdir):
        try:
            versions.update(_CLASS_VERSION_RE.findall(py_file.read_text(encoding="utf-8")))
        except OSError:
            continue
    return versions


def _check_versions(
    cdir: ConnectorDir, dist: DistInfo, loaded_classes: dict[str, type]
) -> list[CheckResult]:
    """A connector's classes carry its own contract version (the toolkit card
    and search index key on it), independent of the distribution's: they
    must declare one, and all of one connector's classes the same one."""
    class_versions = {v for v in _class_versions(cdir, loaded_classes) if v}
    results = [
        _result(
            "hygiene.version-set",
            bool(class_versions),
            f"no connector class in {cdir.module_name} declares a version",
            severity=Severity.WARNING,
        )
    ]
    if class_versions:
        results.append(
            _result(
                "hygiene.version-sync",
                len(class_versions) == 1,
                f"{cdir.module_name}'s classes declare different versions {sorted(class_versions)}: "
                "one connector, one version",
            )
        )
    return results


def _check_tier(cdir: ConnectorDir) -> list[CheckResult]:
    return [
        _result(
            "hygiene.tier-listed",
            cdir.extra in cdir.root.bundle(FULL_EXTRA),
            f"[{cdir.dir_name}] is not in the root pyproject's [{FULL_EXTRA}] extra, which "
            "must pull in every connector's deps",
        )
    ]


def _check_entry_points_resolve(cdir: ConnectorDir, dist: DistInfo) -> list[CheckResult]:
    if not dist.found:
        return []
    errors = [f"{name}: {err}" for name, err in sorted(dist.ep_load_errors.items())]
    return [
        _result(
            "hygiene.entry-points-resolve",
            not errors,
            "entry points fail to load — " + "; ".join(errors),
        )
    ]


def _check_live_markers(cdir: ConnectorDir) -> list[CheckResult]:
    if not (cdir.path / "tests" / "test_live.py").is_file():
        return []
    pytest_opts = (cdir.pyproject or {}).get("tool", {}).get("pytest", {}).get(
        "ini_options", {}
    )
    markers = pytest_opts.get("markers", []) or []
    has_marker = any(str(m).split(":")[0].strip() == "live" for m in markers)
    addopts = str(pytest_opts.get("addopts", ""))
    results = [
        _result(
            "hygiene.live-marker-registered",
            has_marker,
            'tests/test_live.py exists but pyproject does not register the "live" marker '
            "([tool.pytest.ini_options] markers)",
        ),
        _result(
            "hygiene.live-deselected-by-default",
            "not live" in addopts,
            "tests/test_live.py exists but addopts does not deselect it "
            "(add: addopts = \"-m 'not live'\")",
            severity=Severity.WARNING,
        ),
    ]
    return results


def check_install_state(cdir: ConnectorDir, dist: DistInfo) -> list[CheckResult]:
    state = classify_install_state(cdir, dist)
    remediation = f"Fix: {DEV_INSTALL}  (or re-run with --install)"
    if state == INSTALL_MISSING:
        if not dist.found:
            detail = f"{FRAMEWORK_DIST} is not installed"
        else:
            detail = (
                f"not installed: {cdir.requirement} is missing "
                + ", ".join(dist.missing_requirements)
            )
        return [_result("hygiene.install-state", False, f"{detail}. {remediation}")]
    if state == INSTALL_BROKEN:
        stale = stale_entry_points(cdir, dist)
        if dist.ep_load_errors:
            detail = "entry points fail to load"
        elif stale:
            detail = (
                f"entry points {', '.join(stale)} are declared but not installed "
                "(the install predates them)"
            )
        else:
            detail = f"{FRAMEWORK_DIST} is installed from {dist.editable_path}, not this checkout"
        return [_result("hygiene.install-state", False, f"{detail}. {remediation}")]
    return [_result("hygiene.install-state", True, "")]


def run_hygiene_checks(
    cdir: ConnectorDir,
    repo_root: Path,
    dist: DistInfo,
    loaded_classes: dict[str, type] | None = None,
) -> list[CheckResult]:
    """All Layer B checks for one connector directory.

    ``loaded_classes`` maps entry-point name → class for successfully loaded
    entry points (used for the version checks); pass ``{}``/None when
    nothing is importable — the check falls back to source scanning.
    """
    loaded_classes = loaded_classes or {}
    results: list[CheckResult] = []
    results += _check_pyproject_rules(cdir)
    results += _check_scaffold_files(cdir)
    results += _check_source_code(cdir)
    results += _check_versions(cdir, dist, loaded_classes)
    results += _check_tier(cdir)
    results += _check_entry_points_resolve(cdir, dist)
    results += _check_live_markers(cdir)
    return results


def check_framework_install(repo_root: Path) -> list[CheckResult]:
    """The installed ``aisquare-pipe`` matches this checkout's pyproject
    version (stale install metadata otherwise)."""
    root = RootProject.load(repo_root)
    found, version, _ = _framework_dist_facts()
    if not found or not root.version or version == root.version:
        return []
    return [
        CheckResult(
            "hygiene.dist-version-fresh",
            False,
            severity=Severity.WARNING,
            message=(
                f"installed {FRAMEWORK_DIST} {version} != pyproject {root.version} "
                f"(stale install metadata — re-run {DEV_INSTALL})"
            ),
        )
    ]


def load_entry_point_classes(
    cdir: ConnectorDir, discovered: list[DiscoveredConnector]
) -> dict[str, type]:
    """Successfully loaded classes of one connector, importable without
    network (guard enforced here so hygiene callers get it for free)."""
    classes: dict[str, type] = {}
    for d in discovered:
        if owner_module(d.ep_value) == cdir.module_name and d.cls is not None:
            with no_network():
                classes[d.ep_name] = d.cls
    return classes
