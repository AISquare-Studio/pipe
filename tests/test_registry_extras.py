"""A connector whose ``aisquare-pipe[<name>]`` extra isn't installed counts as
not installed: left out of discovery with one info line, never a warning. Any
other load failure is a broken plugin, a warning, even where the extra is
missing too.

Builds a real (temporary) distribution on sys.path, so the metadata the
registry reads is parsed exactly as an installed wheel's would be.
"""

from __future__ import annotations

import logging
import sys
import textwrap
from collections.abc import Sequence
from importlib.metadata import distributions
from pathlib import Path

import pytest

from aisquare.pipe.core import registry
from aisquare.pipe.testing import validation

DIST = "aisquare-pipe-registry-probe"


def _requires(extra: str, reqs: list[str]) -> str:
    """Requires-Dist lines as a wheel writes them: ``req; extra == "x"``, or
    ``req; (marker) and extra == "x"`` for a ``req; marker`` entry."""
    lines = []
    for r in reqs:
        req, _, marker = r.partition(";")
        cond = (
            f'({marker.strip()}) and extra == "{extra}"'
            if marker
            else f'extra == "{extra}"'
        )
        lines.append(f"Requires-Dist: {req.strip()}; {cond}\n")
    return f"Provides-Extra: {extra}\n" + "".join(lines)


def _install(
    tmp_path: Path,
    extra_reqs: list[str],
    module_body: str,
    *,
    other_reqs: Sequence[str] = (),
) -> None:
    """A distribution named DIST with one connector module, aisquare_pipe_probe,
    whose [probe] extra requires ``extra_reqs``. Its [other] extra (another
    connector's) requires ``other_reqs``."""
    pkg = tmp_path / "aisquare_pipe_probe"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    (pkg / "connector.py").write_text(textwrap.dedent(module_body))
    (pkg / "toolkit.py").write_text(
        "from aisquare_pipe_probe.connector import ProbeSource as ProbeToolkit\n"
    )
    info = tmp_path / "aisquare_pipe_registry_probe-0.5.0.dist-info"
    info.mkdir()
    (info / "METADATA").write_text(
        f"Metadata-Version: 2.1\nName: {DIST}\nVersion: 0.5.0\n"
        + _requires("probe", extra_reqs)
        + _requires("other", list(other_reqs))
        + _requires("bundle", [f"{DIST}[probe]"])
    )
    (info / "entry_points.txt").write_text(
        "[aisquare_pipe.connectors]\nprobe-source = aisquare_pipe_probe.connector:ProbeSource\n\n"
        "[aisquare_pipe.toolkits]\nprobe = aisquare_pipe_probe.toolkit:ProbeToolkit\n"
    )


def _forget_probe_modules() -> None:
    for name in [m for m in sys.modules if m.startswith("aisquare_pipe_probe")]:
        del sys.modules[name]


@pytest.fixture
def probe(tmp_path, monkeypatch):
    """Point the registry's entry points at the probe distribution only."""
    _forget_probe_modules()  # each test writes its own aisquare_pipe_probe
    monkeypatch.syspath_prepend(str(tmp_path))

    def entry_points(*, group):
        (dist,) = [
            d for d in distributions(path=[str(tmp_path)]) if d.metadata["Name"] == DIST
        ]
        return [ep for ep in dist.entry_points if ep.group == group]

    monkeypatch.setattr(registry, "entry_points", entry_points)
    monkeypatch.setattr(validation, "entry_points", entry_points)
    yield tmp_path
    _forget_probe_modules()


def _records(caplog, level):
    return [
        r.getMessage()
        for r in caplog.records
        if r.name == "aisquare.pipe" and r.levelno == level
    ]


def test_a_connector_whose_extra_is_missing_is_not_installed(probe, caplog):
    _install(probe, ["probe-sdk-not-installed>=1"], "import probe_sdk_not_installed\n")
    caplog.set_level(logging.DEBUG, logger="aisquare.pipe")

    assert registry.discover_connectors() == {}
    assert registry.discover_toolkits() == {}
    assert _records(caplog, logging.WARNING) == []
    assert _records(caplog, logging.INFO) == [
        (
            "Connector probe-source is not installed: it needs aisquare-pipe[probe] "
            "(missing probe-sdk-not-installed)"
        ),
        (
            "Toolkit probe is not installed: it needs aisquare-pipe[probe] "
            "(missing probe-sdk-not-installed)"
        ),
    ]


def test_with_its_extra_installed_the_connector_loads(probe, caplog):
    # pytest is installed in every environment that runs this test.
    _install(probe, ["pytest>=7"], "import pytest\n\nclass ProbeSource:\n    pass\n")
    caplog.set_level(logging.DEBUG, logger="aisquare.pipe")

    assert list(registry.discover_connectors()) == ["probe-source"]
    assert caplog.records == []


def test_an_import_error_with_the_extra_installed_is_a_broken_plugin(probe, caplog):
    """The SDK is there, so the failure is the connector's own: a warning."""
    _install(probe, ["pytest>=7"], "from pytest import no_such_name\n")
    caplog.set_level(logging.DEBUG, logger="aisquare.pipe")

    assert registry.discover_connectors() == {}
    assert _records(caplog, logging.INFO) == []
    (warning,) = _records(caplog, logging.WARNING)
    assert warning.startswith(
        "Failed to load connector probe-source: cannot import name 'no_such_name'"
    )


def test_a_connector_with_no_third_party_deps_that_fails_to_import_is_a_warning(
    probe, caplog
):
    _install(probe, [], "import aisquare_pipe_probe_missing_own_module\n")
    caplog.set_level(logging.DEBUG, logger="aisquare.pipe")

    assert registry.discover_connectors() == {}
    assert (
        len(_records(caplog, logging.WARNING)) == 1
        and _records(caplog, logging.INFO) == []
    )


def test_any_other_load_error_is_still_a_warning(probe, caplog):
    _install(probe, ["probe-sdk-not-installed>=1"], "raise RuntimeError('boom')\n")
    caplog.set_level(logging.DEBUG, logger="aisquare.pipe")

    assert registry.discover_connectors() == {}
    assert _records(caplog, logging.WARNING) == [
        "Failed to load connector probe-source: boom"
    ]


def test_a_missing_requirement_of_another_extra_does_not_count(probe, caplog):
    """[probe] is fully installed; [other], another connector's extra, is not.
    The probe's import of a module it never declared is its own bug."""
    _install(
        probe,
        ["pytest>=7"],
        "import probe_undeclared_sdk\n",
        other_reqs=["probe-other-sdk-not-installed>=1"],
    )
    caplog.set_level(logging.DEBUG, logger="aisquare.pipe")

    assert registry.discover_connectors() == {}
    assert _records(caplog, logging.INFO) == []
    assert _records(caplog, logging.WARNING) == [
        "Failed to load connector probe-source: No module named 'probe_undeclared_sdk'"
    ]


@pytest.mark.parametrize(
    ("body", "error"),
    [
        # The upgrade trap: `pip uninstall aisquare-pipe-<x>` deleted a file
        # that aisquare-pipe also ships.
        (
            "import aisquare_pipe_probe.constants\n",
            "No module named 'aisquare_pipe_probe.constants'",
        ),
        (
            "import aisquare.pipe.no_such_module\n",
            "No module named 'aisquare.pipe.no_such_module'",
        ),
        # A name missing from a module that is there: never "not installed".
        ("from pytest import no_such_name\n", "cannot import name 'no_such_name'"),
    ],
)
def test_a_broken_install_is_a_warning_even_without_the_extra(
    probe, caplog, body, error
):
    _install(probe, ["probe-sdk-not-installed>=1"], body)
    caplog.set_level(logging.DEBUG, logger="aisquare.pipe")

    assert registry.discover_connectors() == {}
    assert _records(caplog, logging.INFO) == []
    (warning,) = _records(caplog, logging.WARNING)
    assert warning.startswith(f"Failed to load connector probe-source: {error}")


#: Guards that re-raise a missing third-party module as an error with a
#: remediation message and no module name. Python chains the original the way
#: a traceback prints it.
GUARDS = {
    # graphify's engine guard: an explicit __cause__.
    "raise from": """\
        try:
            import probe_sdk_not_installed
        except ImportError as exc:
            raise ModuleNotFoundError("install the engine extra") from exc
        """,
    # Raised while handling it: an implicit __context__.
    "raised in except": """\
        try:
            import probe_sdk_not_installed
        except ImportError:
            raise ImportError("install the engine extra")
        """,
    # A __cause__ whose own __context__ is the missing module.
    "raise from a guard raised in except": """\
        try:
            try:
                import probe_sdk_not_installed
            except ImportError:
                raise ImportError("the probe SDK is missing")
        except ImportError as exc:
            raise ImportError("install the engine extra") from exc
        """,
}


@pytest.mark.parametrize("guard", list(GUARDS))
def test_an_import_error_raised_from_a_missing_third_party_module_is_not_installed(
    probe, caplog, guard
):
    _install(probe, ["probe-sdk-not-installed>=1"], GUARDS[guard])
    caplog.set_level(logging.DEBUG, logger="aisquare.pipe")

    assert registry.discover_connectors() == {}
    assert _records(caplog, logging.WARNING) == []
    assert _records(caplog, logging.INFO) == [
        (
            "Connector probe-source is not installed: it needs aisquare-pipe[probe] "
            "(missing probe-sdk-not-installed)"
        )
    ]


def test_a_guard_raised_from_none_is_a_warning(probe, caplog):
    """``from None`` suppresses the context: the traceback doesn't print it,
    and the registry doesn't follow it. The guard's own message is the
    warning, so it still says what to install."""
    _install(
        probe,
        ["probe-sdk-not-installed>=1"],
        """\
        try:
            import probe_sdk_not_installed
        except ImportError:
            raise ImportError("install the engine extra") from None
        """,
    )
    caplog.set_level(logging.DEBUG, logger="aisquare.pipe")

    assert registry.discover_connectors() == {}
    assert _records(caplog, logging.INFO) == []
    assert _records(caplog, logging.WARNING) == [
        "Failed to load connector probe-source: install the engine extra"
    ]


def test_a_cycle_in_the_chain_ends_the_walk():
    first, second = ImportError("first"), ImportError("second")
    first.__context__, second.__context__ = second, first

    assert registry._missing_module(first) is None


@pytest.mark.parametrize(
    ("body", "error", "not_installed"),
    [
        ("import probe_sdk_not_installed\n", "ModuleNotFoundError: ", True),
        (GUARDS["raised in except"], "ImportError: install the engine extra", True),
        ("import aisquare_pipe_probe.constants\n", "ModuleNotFoundError: ", False),
    ],
)
def test_pipe_validate_reads_load_failures_the_same_way(
    probe, body, error, not_installed
):
    """`pipe validate` reports a broken install as a load failure, not as an
    extra that isn't installed."""
    _install(probe, ["probe-sdk-not-installed>=1"], body)

    (found,) = validation.discover_connector_entry_points()

    assert found.cls is None and found.load_error.startswith(error)
    assert found.missing_requirements == (
        ("probe-sdk-not-installed",) if not_installed else ()
    )


@pytest.mark.parametrize(
    ("marker", "counts"),
    [('python_version < "3"', False), ('python_version >= "3"', True)],
)
def test_a_requirement_whose_marker_excludes_this_python_does_not_count(
    probe, marker, counts
):
    _install(
        probe,
        ["pytest>=7", f"probe-sdk-not-installed>=1; {marker}"],
        "import probe_sdk_not_installed\n",
    )
    (ep,) = registry.entry_points(group="aisquare_pipe.connectors")

    assert registry.missing_requirements(ep) == (
        ["probe-sdk-not-installed"] if counts else []
    )


@pytest.mark.parametrize(
    ("module", "extra"),
    [
        ("aisquare_pipe_dropbox.connector", "dropbox"),
        ("aisquare_pipe_googlecalendar", "googlecalendar"),
        ("aisquare.pipe.testing.mock_connectors", None),
        ("aisquare_pipe_", None),
        ("other_pkg.module", None),
    ],
)
def test_connector_extra_follows_the_module_name(module, extra):
    assert registry.connector_extra(module) == extra
