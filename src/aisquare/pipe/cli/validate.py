"""Orchestrator behind ``pipe validate`` — runs the validation layers and
renders the report.

Layers:
    A contract  — in-process, socket-guarded checks per installed connector
                  and toolkit entry point
    B hygiene   — repo-dir packaging/isolation rules (repo checkout only)
    C unit      — each connector's own pytest suite, one subprocess per
                  connector (their identically-named ``tests`` packages
                  cannot share one pytest process)
    D live      — opt-in (``--live``), only where tests/test_live.py exists

No layer ever requires credentials; live tests self-skip without their env
vars. Exit code 0 = no error-severity findings (warnings allowed), 1 = any
failure, 2 = usage error.
"""

from __future__ import annotations

import importlib
import json
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

import click

from aisquare.pipe.testing import hygiene as hyg
from aisquare.pipe.testing.validation import (
    CheckResult,
    DiscoveredConnector,
    Severity,
    discover_connector_entry_points,
    discover_toolkit_entry_points,
    run_contract_checks,
    run_toolkit_contract_checks,
)

FRAMEWORK_ROW = "framework"
DEFAULT_SUITE_TIMEOUT = 300

STATUS_PASS = "PASS"
STATUS_FAIL = "FAIL"
STATUS_WARN = "WARN"
STATUS_SKIP = "SKIP"
STATUS_NA = "—"


@dataclass
class SuiteResult:
    """Outcome of one subprocess pytest run."""

    target: str
    returncode: int
    summary: str
    output_tail: str
    duration: float
    timed_out: bool


@dataclass
class LayerReport:
    status: str = STATUS_NA
    checks: list[CheckResult] = field(default_factory=list)
    suite: SuiteResult | None = None


@dataclass
class ConnectorReport:
    name: str
    dist_name: str | None
    install_state: str
    entry_points: list[str] = field(default_factory=list)
    install_checks: list[CheckResult] = field(default_factory=list)
    contract: LayerReport = field(default_factory=LayerReport)
    hygiene: LayerReport = field(default_factory=LayerReport)
    unit: LayerReport = field(default_factory=LayerReport)
    live: LayerReport = field(default_factory=LayerReport)

    def error_failures(self) -> list[tuple[str, CheckResult]]:
        found: list[tuple[str, CheckResult]] = []
        for layer_name, layer in (
            ("install", LayerReport(checks=self.install_checks)),
            ("contract", self.contract),
            ("hygiene", self.hygiene),
        ):
            for check in layer.checks:
                if not check.passed and check.severity is Severity.ERROR:
                    found.append((layer_name, check))
        return found

    def warnings(self) -> list[CheckResult]:
        return [
            c
            for layer in (self.contract, self.hygiene)
            for c in layer.checks
            if not c.passed and c.severity is Severity.WARNING
        ]

    @property
    def failed(self) -> bool:
        if self.error_failures():
            return True
        for layer in (self.unit, self.live):
            if layer.status == STATUS_FAIL:
                return True
        return False


# --------------------------------------------------------------------------
# Layer runners
# --------------------------------------------------------------------------


def run_pytest_suite(
    cwd: Path, extra_args: list[str] | None = None, timeout: int = DEFAULT_SUITE_TIMEOUT
) -> SuiteResult:
    """Run one connector's (or the framework's) pytest suite in a subprocess.

    ``cwd`` pins the rootdir so the connector-local ``tests`` package wins;
    PYTHONPATH/PYTEST_ADDOPTS are stripped to stop host-env leakage.
    """
    import os

    cmd = [
        sys.executable,
        "-m",
        "pytest",
        "tests",
        "-q",
        "--no-header",
        "-p",
        "no:cacheprovider",
        *(extra_args or []),
    ]
    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", "PYTEST_ADDOPTS")}
    start = time.monotonic()
    try:
        proc = subprocess.run(
            cmd, cwd=cwd, env=env, capture_output=True, text=True, timeout=timeout
        )
    except subprocess.TimeoutExpired as e:
        tail = (e.stdout or "")[-2000:] if isinstance(e.stdout, str) else ""
        return SuiteResult(
            target=cwd.name,
            returncode=-1,
            summary=f"timed out after {timeout}s",
            output_tail=tail,
            duration=time.monotonic() - start,
            timed_out=True,
        )
    lines = [ln for ln in proc.stdout.strip().splitlines() if ln.strip()]
    summary = lines[-1] if lines else proc.stderr.strip().splitlines()[-1:] or ""
    if isinstance(summary, list):
        summary = summary[0] if summary else ""
    tail = "\n".join((proc.stdout + "\n" + proc.stderr).strip().splitlines()[-40:])
    return SuiteResult(
        target=cwd.name,
        returncode=proc.returncode,
        summary=summary,
        output_tail=tail,
        duration=time.monotonic() - start,
        timed_out=False,
    )


def _status_from_checks(checks: list[CheckResult]) -> str:
    if any(not c.passed and c.severity is Severity.ERROR for c in checks):
        return STATUS_FAIL
    if any(not c.passed and c.severity is Severity.WARNING for c in checks):
        return STATUS_WARN
    return STATUS_PASS


def _unit_status(suite: SuiteResult) -> tuple[str, str]:
    """(status, short label) for a suite result."""
    if suite.timed_out:
        return STATUS_FAIL, "timeout"
    if suite.returncode == 5:
        return STATUS_FAIL, "no tests collected"
    if suite.returncode == 0:
        passed = _extract_pass_count(suite.summary)
        return STATUS_PASS, f"({passed})" if passed else ""
    return STATUS_FAIL, suite.summary[:40]


def _extract_pass_count(summary: str) -> str:
    import re

    match = re.search(r"(\d+) passed", summary)
    return match.group(1) if match else ""


def _discover_entry_points() -> list[DiscoveredConnector]:
    """Connector + toolkit entry points (looked up at call time so tests can
    stub either discovery function on this module)."""
    return discover_connector_entry_points() + discover_toolkit_entry_points()


def _installed_toolkits(discovered: list[DiscoveredConnector]) -> dict[str, type]:
    """Every loaded toolkit entry point, name -> class (for the cross-toolkit
    slug-prefix check)."""
    return {d.ep_name: d.cls for d in discovered if d.is_toolkit and d.cls is not None}


def _contract_layer(
    entries: list[DiscoveredConnector],
    installed: dict[str, type],
) -> LayerReport:
    """Contract checks for ``entries`` (one connector's entry points);
    ``installed`` is every loaded toolkit, for the cross-toolkit checks."""
    checks: list[CheckResult] = []
    for d in entries:
        if d.cls is None:
            continue
        if d.is_toolkit:
            found = run_toolkit_contract_checks(d.cls, d.ep_name, installed=installed)
        else:
            found = run_contract_checks(d.cls, d.ep_name)
        for check in found:
            # Attribute each check to its entry point for the report.
            check.id = f"{check.id}@{d.ep_name}" if "@" not in check.id else check.id
            checks.append(check)
    if not checks:
        return LayerReport(status=STATUS_NA)
    return LayerReport(status=_status_from_checks(checks), checks=checks)


def _connector_key(d: DiscoveredConnector) -> str | None:
    """The connector an entry point belongs to: ``aisquare_pipe_<name>`` →
    ``<name>`` (with ``_`` as ``-``, as its directory is named). None for the
    framework's own entry points (the mocks) and other modules."""
    module = hyg.owner_module(d.ep_value)
    if not module.startswith("aisquare_pipe_") or module == "aisquare_pipe_":
        return None
    return module.removeprefix("aisquare_pipe_").replace("_", "-")


def _live_layer(cdir_path: Path, run_live: bool, timeout: int) -> LayerReport:
    live_file = cdir_path / "tests" / "test_live.py"
    if not live_file.is_file():
        return LayerReport(status=STATUS_NA)
    if not run_live:
        return LayerReport(status=STATUS_NA)
    suite = run_pytest_suite(cdir_path, ["tests/test_live.py", "-m", "live"], timeout)
    if suite.returncode == 0 and "skipped" in suite.summary and "passed" not in suite.summary:
        return LayerReport(status=f"{STATUS_SKIP} (no creds)", suite=suite)
    if suite.returncode in (0, 5):
        status = STATUS_PASS if suite.returncode == 0 else f"{STATUS_SKIP} (no creds)"
        return LayerReport(status=status, suite=suite)
    return LayerReport(status=STATUS_FAIL, suite=suite)


# --------------------------------------------------------------------------
# Orchestration
# --------------------------------------------------------------------------


def _install_repo(repo_root: Path, extras: str) -> tuple[bool, str]:
    proc = subprocess.run(
        [sys.executable, "-m", "pip", "install", "-e", f"{repo_root}[{extras}]", "--quiet"],
        capture_output=True,
        text=True,
    )
    return proc.returncode == 0, proc.stderr.strip().splitlines()[-1] if proc.stderr else ""


def install_missing_connectors(
    repo_root: Path, only: str | None = None, echo=click.echo
) -> int:
    """``pip install -e "<repo>[full,dev]"`` once when any connector is
    missing (its extra isn't installed), broken, stale (entry points declared
    but not installed), or the installed ``aisquare-pipe`` is another
    checkout or another version. With ``only``, that connector's extra
    instead of ``[full]``. Returns the number of installs attempted (0 or 1).

    The caller must re-exec validation in a fresh interpreter afterwards:
    editable installs register their import hooks via ``.pth`` files, which
    only run at interpreter startup.
    """
    cdirs = hyg.list_connector_dirs(repo_root)
    if only is not None:
        cdirs = [c for c in cdirs if c.dir_name == only]
    discovered = _discover_entry_points()
    needs = [
        c.dir_name
        for c in cdirs
        if hyg.classify_install_state(c, hyg.load_dist_info(c, discovered)) != hyg.INSTALL_OK
    ]
    stale = bool(hyg.check_framework_install(repo_root))
    if not needs and not stale:
        return 0
    extras = f"{only},dev" if only is not None else "full,dev"
    why = ", ".join(needs) if needs else f"stale {hyg.FRAMEWORK_DIST} metadata"
    echo(f"Installing {hyg.FRAMEWORK_DIST}[{extras}] (pip install -e {repo_root}) for: {why} ...")
    ok, error = _install_repo(repo_root, extras)
    if not ok:
        echo(f"  install failed: {error}", err=True)
    importlib.invalidate_caches()
    return 1


def validate_repo(
    repo_root: Path,
    only: str | None = None,
    *,
    live: bool = False,
    skip_tests: bool = False,
    timeout: int = DEFAULT_SUITE_TIMEOUT,
    echo=click.echo,
) -> list[ConnectorReport]:
    """Validate connectors in a repo checkout (layers A–D + framework row)."""
    cdirs = hyg.list_connector_dirs(repo_root)
    if only is not None:
        cdirs = [c for c in cdirs if c.dir_name == only]

    discovered = _discover_entry_points()
    installed = _installed_toolkits(discovered)
    reports: list[ConnectorReport] = []

    for index, cdir in enumerate(cdirs, start=1):
        dist = hyg.load_dist_info(cdir, discovered)
        state = hyg.classify_install_state(cdir, dist)
        loaded = hyg.load_entry_point_classes(cdir, discovered)
        own = [d for d in discovered if hyg.owner_module(d.ep_value) == cdir.module_name]

        report = ConnectorReport(
            name=cdir.dir_name,
            dist_name=cdir.dist_name,
            install_state=state,
            entry_points=[d.ep_name for d in own],
            install_checks=hyg.check_install_state(cdir, dist),
        )

        hygiene_checks = hyg.run_hygiene_checks(cdir, repo_root, dist, loaded)
        if state != hyg.INSTALL_OK:
            # The broken install is attributed once (install column); drop
            # the duplicate entry-point-resolve finding from hygiene.
            hygiene_checks = [
                c for c in hygiene_checks if c.id != "hygiene.entry-points-resolve"
            ]
        report.hygiene = LayerReport(
            status=_status_from_checks(hygiene_checks), checks=hygiene_checks
        )

        if state == hyg.INSTALL_OK:
            report.contract = _contract_layer(own, installed)
            if not skip_tests:
                echo(f"[{index}/{len(cdirs)}] {cdir.dir_name}: running unit suite ...")
                report.unit = _unit_report(cdir.path, timeout)
            report.live = _live_layer(cdir.path, live, timeout)

        reports.append(report)

    reports.append(
        _framework_report(repo_root, discovered, skip_tests=skip_tests, timeout=timeout, echo=echo)
    )
    return reports


def _unit_report(path: Path, timeout: int) -> LayerReport:
    suite = run_pytest_suite(path, timeout=timeout)
    status, _ = _unit_status(suite)
    return LayerReport(status=status, suite=suite)


def _framework_report(
    repo_root: Path,
    discovered: list[DiscoveredConnector],
    *,
    skip_tests: bool,
    timeout: int,
    echo=click.echo,
) -> ConnectorReport:
    own = [
        d for d in discovered if d.dist_name == hyg.FRAMEWORK_DIST and _connector_key(d) is None
    ]
    report = ConnectorReport(
        name=FRAMEWORK_ROW,
        dist_name=hyg.FRAMEWORK_DIST,
        install_state=hyg.INSTALL_OK,
        entry_points=[d.ep_name for d in own],
    )
    report.contract = _contract_layer(own, _installed_toolkits(discovered))
    framework_checks = hyg.check_framework_install(repo_root)
    if framework_checks:
        report.hygiene = LayerReport(
            status=_status_from_checks(framework_checks), checks=framework_checks
        )
    if not skip_tests:
        echo("[framework] running root test suite ...")
        report.unit = _unit_report(repo_root, timeout)
    return report


def validate_installed_only(
    *, live: bool = False, echo=click.echo
) -> list[ConnectorReport]:
    """Outside a repo checkout: Layer A only, one row per connector (the
    connectors inside ``aisquare-pipe`` by module, any other plugin by its
    distribution). A connector whose extra isn't installed is reported as
    not installed, not as a failure: the host chose not to install it."""
    discovered = _discover_entry_points()
    installed = _installed_toolkits(discovered)
    groups: dict[str, list[DiscoveredConnector]] = {}
    for d in discovered:
        key = _connector_key(d) or d.dist_name or "(unknown dist)"
        groups.setdefault(key, []).append(d)

    reports: list[ConnectorReport] = []
    for key in sorted(groups):
        eps = groups[key]
        dist_name = eps[0].dist_name or key
        not_installed = [d for d in eps if d.missing_requirements]
        report = ConnectorReport(
            name=key.removeprefix("aisquare-pipe-") or key,
            dist_name=f"{dist_name}[{key}]" if _connector_key(eps[0]) else dist_name,
            install_state=hyg.INSTALL_MISSING if not_installed else hyg.INSTALL_OK,
            entry_points=[d.ep_name for d in eps],
        )
        load_failures = [
            CheckResult(
                f"contract.entry-point-load@{d.ep_name}",
                False,
                message=f"entry point failed to load: {d.load_error}",
            )
            for d in eps
            if d.load_error and not d.missing_requirements
        ]
        report.contract = _contract_layer(eps, installed)
        if load_failures:
            report.contract.checks.extend(load_failures)
            report.contract.status = STATUS_FAIL
        reports.append(report)
    return reports


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------


def _unit_cell(layer: LayerReport) -> str:
    if layer.status == STATUS_NA or layer.suite is None:
        return layer.status
    status, label = _unit_status(layer.suite)
    return f"{status} {label}".strip() if status == STATUS_PASS else status


def render_table(reports: list[ConnectorReport], echo=click.echo) -> None:
    echo(f"\n{'Connector':<15} {'Install':<9} {'Contract':<10} {'Hygiene':<9} {'Unit-tests':<14} {'Live':<16}")
    echo("-" * 75)
    for r in reports:
        install = r.install_state if r.name != FRAMEWORK_ROW else "ok"
        echo(
            f"{r.name:<15} {install:<9} {r.contract.status:<10} "
            f"{r.hygiene.status if r.name != FRAMEWORK_ROW else STATUS_NA:<9} "
            f"{_unit_cell(r.unit):<14} {r.live.status:<16}"
        )

    failures = [(r, layer_name, c) for r in reports for layer_name, c in r.error_failures()]
    suite_failures = [
        (r, report)
        for r in reports
        for report in (r.unit, r.live)
        if report.status == STATUS_FAIL and report.suite is not None
    ]
    warnings = [(r, c) for r in reports for c in r.warnings()]

    if failures or suite_failures:
        echo("\nFAILURES")
        for r, _layer_name, check in failures:
            echo(f"  {r.name}")
            echo(f"    [{check.id}] {check.message}")
        for r, report in suite_failures:
            assert report.suite is not None
            echo(f"  {r.name} — {report.suite.summary}")
            for line in report.suite.output_tail.splitlines()[-15:]:
                echo(f"    {line}")

    if warnings:
        echo("\nWARNINGS")
        for r, check in warnings:
            echo(f"  {r.name:<12} [{check.id}] {check.message}")

    passed = sum(1 for r in reports if not r.failed)
    failed = sum(1 for r in reports if r.failed)
    echo(
        f"\n{passed} passed, {failed} failed, {len(warnings)} warnings — "
        f"exit {1 if failed else 0}"
    )


def _layer_json(layer: LayerReport) -> dict:
    payload: dict = {"status": layer.status.lower().replace("—", "not-applicable")}
    if layer.checks:
        payload["checks"] = [
            {
                "id": c.id,
                "passed": c.passed,
                "severity": c.severity.value,
                "message": c.message,
            }
            for c in layer.checks
        ]
    if layer.suite is not None:
        payload.update(
            returncode=layer.suite.returncode,
            summary=layer.suite.summary,
            duration=round(layer.suite.duration, 2),
        )
    return payload


def render_json(reports: list[ConnectorReport], repo_root: Path | None, echo=click.echo) -> None:
    failed = sum(1 for r in reports if r.failed)
    payload = {
        "repo_root": str(repo_root) if repo_root else None,
        "ok": failed == 0,
        "connectors": [
            {
                "name": r.name,
                "dist": r.dist_name,
                "install_state": r.install_state,
                "entry_points": r.entry_points,
                "install_checks": [
                    {"id": c.id, "passed": c.passed, "message": c.message}
                    for c in r.install_checks
                ],
                "layers": {
                    "contract": _layer_json(r.contract),
                    "hygiene": _layer_json(r.hygiene),
                    "unit": _layer_json(r.unit),
                    "live": _layer_json(r.live),
                },
            }
            for r in reports
        ],
        "summary": {
            "passed": len(reports) - failed,
            "failed": failed,
            "warnings": sum(len(r.warnings()) for r in reports),
            "exit_code": 1 if failed else 0,
        },
    }
    echo(json.dumps(payload, indent=2))


def exit_code(reports: list[ConnectorReport]) -> int:
    return 1 if any(r.failed for r in reports) else 0
