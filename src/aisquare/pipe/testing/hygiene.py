"""Repo-hygiene checks for connector packages (Layer B of ``pipe validate``).

Operates on a repo checkout's ``connectors/<dir>/`` trees and codifies the
connector-PR review rubric: packaging rules, scaffold completeness, code
isolation, and install-state. Pure functions over paths + injected dist
info, so tests can drive them against ``tmp_path`` fakes.
"""

from __future__ import annotations

import ast
import json
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from aisquare.pipe.testing.validation import (
    CheckResult,
    DiscoveredConnector,
    Severity,
    no_network,
)

FRAMEWORK_DIST = "aisquare-pipe"
ENTRY_POINT_GROUP = "aisquare_pipe.connectors"

INSTALL_OK = "ok"
INSTALL_BROKEN = "broken"
INSTALL_MISSING = "missing"


@dataclass
class ConnectorDir:
    """A ``connectors/<dir>/`` tree in the repo checkout."""

    dir_name: str
    path: Path
    dist_name: str
    pyproject: dict | None
    pyproject_error: str | None

    @property
    def safe_name(self) -> str:
        return self.dir_name.replace("-", "_")

    @property
    def module_name(self) -> str:
        return f"aisquare_pipe_{self.safe_name}"


@dataclass
class DistInfo:
    """Installed-distribution facts for one connector (injectable in tests)."""

    found: bool
    version: str | None = None
    editable_path: Path | None = None
    ep_load_errors: dict[str, str] = field(default_factory=dict)


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
    """Every ``connectors/<dir>/`` containing (or expected to contain) a
    connector package, sorted by name."""
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
            error = "pyproject.toml not found"
        dirs.append(
            ConnectorDir(
                dir_name=path.name,
                path=path,
                dist_name=f"aisquare-pipe-{path.name}",
                pyproject=pyproject,
                pyproject_error=error,
            )
        )
    return dirs


def load_dist_info(
    dist_name: str, discovered: list[DiscoveredConnector]
) -> DistInfo:
    """Installed-dist facts: version, editable target (``direct_url.json``),
    and any entry-point load errors attributed to this dist."""
    from importlib.metadata import PackageNotFoundError, distribution

    try:
        dist = distribution(dist_name)
    except PackageNotFoundError:
        return DistInfo(found=False)

    editable_path: Path | None = None
    try:
        raw = dist.read_text("direct_url.json")
        if raw:
            url = json.loads(raw).get("url", "")
            if url.startswith("file://"):
                editable_path = Path(url.removeprefix("file://"))
    except Exception:  # noqa: BLE001 — absent/odd metadata is not an error
        editable_path = None

    ep_load_errors = {
        d.ep_name: d.load_error
        for d in discovered
        if d.dist_name == dist_name and d.load_error
    }
    return DistInfo(
        found=True,
        version=dist.version,
        editable_path=editable_path,
        ep_load_errors=ep_load_errors,
    )


def classify_install_state(cdir: ConnectorDir, dist: DistInfo) -> str:
    if not dist.found:
        return INSTALL_MISSING
    if dist.ep_load_errors:
        return INSTALL_BROKEN
    if dist.editable_path is not None:
        try:
            if dist.editable_path.resolve() != cdir.path.resolve():
                return INSTALL_BROKEN
        except OSError:
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
    results = [
        _result(
            "hygiene.pyproject-parses",
            cdir.pyproject is not None,
            f"connectors/{cdir.dir_name}/pyproject.toml: {cdir.pyproject_error}",
        )
    ]
    if cdir.pyproject is None:
        return results
    project = cdir.pyproject.get("project", {})

    results.append(
        _result(
            "hygiene.package-name",
            project.get("name") == cdir.dist_name,
            f"[project].name is {project.get('name')!r}, must be {cdir.dist_name!r}",
        )
    )
    results.append(
        _result(
            "hygiene.requires-python",
            bool(project.get("requires-python")),
            "[project].requires-python is not set (framework baseline: >=3.11)",
        )
    )
    deps = project.get("dependencies", []) or []
    has_framework_dep = any(
        re.match(r"^aisquare-pipe\s*(\[[^]]*\])?\s*(>=|==|~=)", dep) for dep in deps
    )
    results.append(
        _result(
            "hygiene.framework-dep",
            has_framework_dep,
            "dependencies must include a versioned requirement on aisquare-pipe (e.g. aisquare-pipe>=0.1.0)",
        )
    )
    eps = project.get("entry-points", {}).get(ENTRY_POINT_GROUP, {})
    results.append(
        _result(
            "hygiene.entry-points-registered",
            bool(eps),
            f'[project.entry-points."{ENTRY_POINT_GROUP}"] is missing or empty',
        )
    )
    return results


def _check_scaffold_files(cdir: ConnectorDir) -> list[CheckResult]:
    module_dir = cdir.path / "src" / cdir.module_name
    missing: list[str] = []
    for rel in (
        "README.md",
        f"src/{cdir.module_name}/__init__.py",
        f"src/{cdir.module_name}/connector.py",
        "tests",
    ):
        if not (cdir.path / rel).exists():
            missing.append(rel)

    compliance_ok = False
    compliance_file = cdir.path / "tests" / "test_compliance.py"
    if compliance_file.is_file():
        try:
            compliance_ok = "connector_compliance_suite(" in compliance_file.read_text(
                encoding="utf-8"
            )
        except OSError:
            compliance_ok = False
    if not compliance_ok:
        missing.append("tests/test_compliance.py (with connector_compliance_suite(...))")

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
    results: list[CheckResult] = []
    pyproject_version = (
        (cdir.pyproject or {}).get("project", {}).get("version")
        if cdir.pyproject
        else None
    )
    results.append(
        _result(
            "hygiene.version-set",
            bool(pyproject_version),
            "[project].version is not set",
            severity=Severity.WARNING,
        )
    )
    if pyproject_version:
        class_versions = _class_versions(cdir, loaded_classes)
        mismatched = {v for v in class_versions if v and v != pyproject_version}
        if class_versions:
            results.append(
                _result(
                    "hygiene.version-sync",
                    not mismatched,
                    f"connector class version(s) {sorted(mismatched)} != pyproject version {pyproject_version!r}",
                )
            )
        if dist.found and dist.version and dist.version != pyproject_version:
            results.append(
                CheckResult(
                    "hygiene.dist-version-fresh",
                    False,
                    severity=Severity.WARNING,
                    message=(
                        f"installed dist {dist.version} != pyproject {pyproject_version} "
                        f"(stale editable metadata — re-run pip install -e connectors/{cdir.dir_name})"
                    ),
                )
            )
    return results


def _check_tier(cdir: ConnectorDir, repo_root: Path) -> list[CheckResult]:
    try:
        with (repo_root / "pyproject.toml").open("rb") as f:
            root = tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError):
        return []
    extras = root.get("project", {}).get("optional-dependencies", {})
    listed = any(
        any(req.split("[")[0].strip() == cdir.dist_name for req in extras.get(tier, []))
        for tier in ("popular", "full")
    )
    return [
        _result(
            "hygiene.tier-listed",
            listed,
            f"{cdir.dist_name} is in neither [popular] nor [full] extras of the root pyproject "
            "(fine for experimental connectors; add it when it should ship with the bundles)",
            severity=Severity.WARNING,
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
    remediation = f"Fix: pip install -e connectors/{cdir.dir_name}  (or re-run with --install)"
    if state == INSTALL_MISSING:
        return [
            _result(
                "hygiene.install-state",
                False,
                f"{cdir.dist_name} is not installed. {remediation}",
            )
        ]
    if state == INSTALL_BROKEN:
        if dist.ep_load_errors:
            detail = "entry points fail to load"
        else:
            detail = f"editable install points at {dist.editable_path} (repo moved?)"
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
    entry points (used for the version-sync check); pass ``{}``/None when
    nothing is importable — the check falls back to source scanning.
    """
    loaded_classes = loaded_classes or {}
    results: list[CheckResult] = []
    results += _check_pyproject_rules(cdir)
    results += _check_scaffold_files(cdir)
    results += _check_source_code(cdir)
    results += _check_versions(cdir, dist, loaded_classes)
    results += _check_tier(cdir, repo_root)
    results += _check_entry_points_resolve(cdir, dist)
    results += _check_live_markers(cdir)
    return results


def load_entry_point_classes_for_dist(
    dist_name: str, discovered: list[DiscoveredConnector]
) -> dict[str, type]:
    """Successfully loaded classes belonging to one dist, importable without
    network (guard enforced here so hygiene callers get it for free)."""
    classes: dict[str, type] = {}
    for d in discovered:
        if d.dist_name == dist_name and d.cls is not None:
            with no_network():
                classes[d.ep_name] = d.cls
    return classes
