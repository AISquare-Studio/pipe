"""A connector whose ``aisquare-pipe[<name>]`` extra isn't installed counts as
not installed: left out of discovery with one info line, never a warning.

Builds a real (temporary) distribution on sys.path, so the metadata the
registry reads is parsed exactly as an installed wheel's would be.
"""

from __future__ import annotations

import logging
import sys
import textwrap
from importlib.metadata import distributions
from pathlib import Path

import pytest

from aisquare.pipe.core import registry

DIST = "aisquare-pipe-registry-probe"


def _install(tmp_path: Path, extra_reqs: list[str], module_body: str) -> None:
    """A distribution named DIST with one connector module, aisquare_pipe_probe,
    whose [probe] extra requires ``extra_reqs``."""
    pkg = tmp_path / "aisquare_pipe_probe"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    (pkg / "connector.py").write_text(textwrap.dedent(module_body))
    (pkg / "toolkit.py").write_text(
        "from aisquare_pipe_probe.connector import ProbeSource as ProbeToolkit\n"
    )
    info = tmp_path / "aisquare_pipe_registry_probe-0.5.0.dist-info"
    info.mkdir()
    requires = "".join(f'Requires-Dist: {r}; extra == "probe"\n' for r in extra_reqs)
    (info / "METADATA").write_text(
        f"Metadata-Version: 2.1\nName: {DIST}\nVersion: 0.5.0\n"
        f"Provides-Extra: probe\n{requires}"
        f'Provides-Extra: bundle\nRequires-Dist: {DIST}[probe]; extra == "bundle"\n'
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
    yield tmp_path
    _forget_probe_modules()


def _records(caplog, level):
    return [
        r.getMessage()
        for r in caplog.records
        if r.name == "aisquare.pipe" and r.levelno == level
    ]


def test_a_connector_whose_extra_is_missing_is_not_installed(probe, caplog):
    _install(
        probe,
        ["aisquare-pipe-probe-sdk-not-installed>=1"],
        "import aisquare_pipe_probe_sdk_not_installed\n",
    )
    caplog.set_level(logging.DEBUG, logger="aisquare.pipe")

    assert registry.discover_connectors() == {}
    assert registry.discover_toolkits() == {}
    assert _records(caplog, logging.WARNING) == []
    assert _records(caplog, logging.INFO) == [
        (
            "Connector probe-source is not installed: it needs aisquare-pipe[probe] "
            "(missing aisquare-pipe-probe-sdk-not-installed)"
        ),
        (
            "Toolkit probe is not installed: it needs aisquare-pipe[probe] "
            "(missing aisquare-pipe-probe-sdk-not-installed)"
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
    _install(
        probe,
        ["aisquare-pipe-probe-sdk-not-installed>=1"],
        "raise RuntimeError('boom')\n",
    )
    caplog.set_level(logging.DEBUG, logger="aisquare.pipe")

    assert registry.discover_connectors() == {}
    assert _records(caplog, logging.WARNING) == [
        "Failed to load connector probe-source: boom"
    ]


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
