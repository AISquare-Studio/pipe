"""scripts/release_packages.py: what publish.yml builds, skips, uploads and verifies.

Hermetic: PyPI is a fake ``fetch``; nothing touches the network.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "release_packages.py"
PUBLISH_YML = REPO_ROOT / ".github" / "workflows" / "publish.yml"


def _load_script():
    spec = importlib.util.spec_from_file_location("release_packages", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses resolve their module by name
    spec.loader.exec_module(module)
    return module


rp = _load_script()


# ---------------------------------------------------------------- fixtures


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(text))


def _pyproject(
    name: str,
    version: str,
    deps: list[str] | tuple[str, ...] = (),
    extras: dict[str, list[str]] | None = None,
) -> str:
    out = f'[project]\nname = "{name}"\nversion = "{version}"\ndependencies = {json.dumps(list(deps))}\n'
    if extras:
        out += "\n[project.optional-dependencies]\n"
        out += "".join(f"{k} = {json.dumps(v)}\n" for k, v in extras.items())
    return out


def _repo(
    root: Path,
    framework: str = "0.5.0",
    connectors: dict[str, tuple[str, list[str]]] | None = None,
    extras=None,
) -> Path:
    """A pipe-shaped tree: the root package plus connectors/<dir>."""
    connectors = (
        connectors
        if connectors is not None
        else {
            "gmail": ("0.2.0", ["aisquare-pipe>=0.5.0"]),
            "slack": ("0.2.0", ["aisquare-pipe>=0.5.0", "requests>=2.31"]),
            "local": ("0.1.0", ["aisquare-pipe>=0.1.0"]),
        }
    )
    extras = (
        extras
        if extras is not None
        else {
            "dev": ["pytest>=7.0"],
            "popular": ["aisquare-pipe-local"],
            "full": [
                "aisquare-pipe[popular]",
                "aisquare-pipe-slack",
                "aisquare-pipe-gmail",
            ],
            "toolkits": ["aisquare-pipe-gmail>=0.2.0", "aisquare-pipe-slack>=0.2.0"],
        }
    )
    _write(
        root / "pyproject.toml",
        _pyproject("aisquare-pipe", framework, ["click>=8.0"], extras),
    )
    for d, (version, deps) in connectors.items():
        _write(
            root / "connectors" / d / "pyproject.toml",
            _pyproject(f"aisquare-pipe-{d}", version, deps),
        )
    return root


class FakePypi:
    """PyPI's JSON API: ``published`` maps (name, version) -> {filename: sha256};
    ``projects`` lists projects that exist without the version asked for."""

    def __init__(self, published=None, projects=(), status=None):
        self.published = published or {}
        self.projects = set(projects) | {n for n, _ in self.published}
        self.status = status  # force this status on every call
        self.calls: list[str] = []

    def __call__(self, url: str) -> tuple[int, bytes]:
        self.calls.append(url)
        if self.status is not None:
            return self.status, b""
        parts = url.removeprefix(f"{rp.PYPI}/pypi/").removesuffix("/json").split("/")
        if len(parts) == 2:
            files = self.published.get((parts[0], parts[1]))
            if files is None:
                return 404, b""
            urls = [{"filename": f, "digests": {"sha256": h}} for f, h in files.items()]
            return 200, json.dumps({"urls": urls}).encode()
        return (200 if parts[0] in self.projects else 404), b"{}"


# --------------------------------------------------------------- discovery


def test_discover_lists_the_framework_first_then_every_connector_dir(tmp_path):
    pkgs = rp.discover(rp.FsTree(_repo(tmp_path)))
    assert [(p.path, p.name, p.version) for p in pkgs] == [
        (".", "aisquare-pipe", "0.5.0"),
        ("connectors/gmail", "aisquare-pipe-gmail", "0.2.0"),
        ("connectors/local", "aisquare-pipe-local", "0.1.0"),
        ("connectors/slack", "aisquare-pipe-slack", "0.2.0"),
    ]


def test_a_connector_dir_without_pyproject_is_not_a_package(tmp_path):
    _repo(tmp_path)
    (tmp_path / "connectors" / "notes").mkdir()
    assert "connectors/notes" not in [p.path for p in rp.discover(rp.FsTree(tmp_path))]


@pytest.mark.parametrize(
    ("pyproject", "message"),
    [
        (
            '[project]\nname = "aisquare-pipe-x"\ndynamic = ["version"]\n',
            "static string",
        ),
        ('[project]\nname = "aisquare-pipe-x"\nversion = "soon"\n', "PEP 440"),
        (
            '[project]\nname = "other-thing"\nversion = "0.1.0"\n',
            "is not aisquare-pipe",
        ),
        (
            '[project]\nname = "aisquare_pipe.gmail"\nversion = "0.1.0"\n',
            "both declare",
        ),
    ],
)
def test_discover_rejects_a_package_publish_yml_could_not_release(
    tmp_path, pyproject, message
):
    _repo(tmp_path)
    _write(tmp_path / "connectors" / "x" / "pyproject.toml", pyproject)
    with pytest.raises(rp.ReleaseError, match=message):
        rp.discover(rp.FsTree(tmp_path))


def test_git_tree_reads_the_same_packages_as_the_working_tree(tmp_path):
    repo = _repo(tmp_path / "repo")
    (repo / "connectors" / "notes").mkdir()
    (repo / "connectors" / "notes" / "README.md").write_text("no pyproject\n")
    git = [
        "git",
        "-C",
        str(repo),
        "-c",
        "user.name=t",
        "-c",
        "user.email=t@example.invalid",
        "-c",
        "commit.gpgsign=false",
    ]
    subprocess.run([*git, "init", "-q"], check=True)
    subprocess.run([*git, "add", "-A"], check=True)
    subprocess.run([*git, "commit", "-qm", "init"], check=True)
    assert rp.discover(rp.GitTree(repo, "HEAD")) == rp.discover(rp.FsTree(repo))


# ------------------------------------------------------------- consistency


def test_this_repo_is_a_consistent_package_set():
    """The packages here, uploaded as they stand, could be installed together."""
    pkgs = rp.discover(rp.FsTree(REPO_ROOT))
    assert pkgs[0].path == "."
    on_disk = sorted(
        p.parent.relative_to(REPO_ROOT).as_posix()
        for p in REPO_ROOT.glob("connectors/*/pyproject.toml")
    )
    assert [p.path for p in pkgs[1:]] == on_disk
    assert rp.internal_problems(pkgs) == []


def test_internal_problems_flag_requirements_the_set_cannot_satisfy(tmp_path):
    _repo(
        tmp_path,
        connectors={
            "gmail": ("0.2.0", ["aisquare-pipe>=0.6.0"]),  # framework is 0.5.0
            "slack": (
                "0.1.0",
                ["aisquare-pipe>=0.5.0"],
            ),  # [toolkits] floors it at 0.2.0
            "local": ("0.1.0", ["aisquare-pipe-ghost>=0.1"]),  # no such package here
        },
    )
    problems = "\n".join(rp.internal_problems(rp.discover(rp.FsTree(tmp_path))))
    assert (
        "aisquare-pipe-gmail dependencies requires 'aisquare-pipe>=0.6.0'" in problems
    )
    assert "aisquare-pipe [toolkits] requires 'aisquare-pipe-slack>=0.2.0'" in problems
    assert "no package here is named aisquare-pipe-ghost" in problems
    assert (
        "click" not in problems and "pytest" not in problems
    )  # third-party: not ours to check


# -------------------------------------------------------------------- plan


def test_pr_dry_run_builds_everything_and_uploads_nothing(tmp_path):
    pypi = FakePypi(
        published={("aisquare-pipe-local", "0.1.0"): {"x.whl": "0"}},
        projects={"aisquare-pipe"},
    )
    plan = rp.make_plan(rp.FsTree(_repo(tmp_path)), event="pull_request", fetch=pypi)
    out = plan.outputs()
    assert plan.problems == []
    assert [p["path"] for p in json.loads(out["packages"])] == [
        ".",
        "connectors/gmail",
        "connectors/local",
        "connectors/slack",
    ]
    assert (
        out["upload"] == "false"
        and out["uploads"] == "[]"
        and out["connectors"] == "[]"
    )
    assert "would upload" in plan.summary() and "**upload**" not in plan.summary()


def test_publish_uploads_only_versions_missing_from_pypi_and_says_what_it_skipped(
    tmp_path,
):
    pypi = FakePypi(
        published={("aisquare-pipe-local", "0.1.0"): {"x.whl": "0"}},
        projects={"aisquare-pipe", "aisquare-pipe-gmail"},
    )
    plan = rp.make_plan(
        rp.FsTree(_repo(tmp_path)), event="release", tag="v0.5.0", fetch=pypi
    )
    out = plan.outputs()
    assert plan.problems == []
    assert out["upload"] == "true" and out["framework"] == "true"
    assert [c["name"] for c in json.loads(out["connectors"])] == [
        "aisquare-pipe-gmail",
        "aisquare-pipe-slack",
    ]
    assert [u["name"] for u in json.loads(out["uploads"])] == [
        "aisquare-pipe",
        "aisquare-pipe-gmail",
        "aisquare-pipe-slack",
    ]
    summary = plan.summary()
    assert (
        "| `aisquare-pipe-local` | `connectors/local` | 0.1.0 | yes | skip: this version is already on PyPI |"
        in summary
    )
    assert "no (new project: needs a pending trusted publisher)" in summary  # slack


def test_a_release_tag_that_is_not_the_framework_version_fails_the_plan(tmp_path):
    """v0.2.0 (2026-07-10), a connector-only release: the tag said 0.2.0 and
    pyproject.toml 0.1.1, and the aisquare-pipe job went green on a silent skip."""
    pypi = FakePypi(
        published={("aisquare-pipe", "0.1.1"): {"aisquare_pipe-0.1.1.tar.gz": "0"}}
    )
    plan = rp.make_plan(
        rp.FsTree(_repo(tmp_path, framework="0.1.1", connectors={}, extras={})),
        event="release",
        tag="v0.2.0",
        fetch=pypi,
    )
    assert plan.problems == [
        (
            "release tag 'v0.2.0' does not match aisquare-pipe 0.1.1 in pyproject.toml: "
            "tag the release v0.1.1, or bump pyproject.toml first (publish connectors "
            "alone with Run workflow, dry-run unticked)"
        )
    ]


def test_manual_run_is_a_dry_run_unless_asked_otherwise(tmp_path):
    tree = rp.FsTree(_repo(tmp_path))
    assert not rp.make_plan(
        tree, event="workflow_dispatch", dry_run=True, fetch=FakePypi()
    ).upload
    assert rp.make_plan(
        tree, event="workflow_dispatch", dry_run=False, fetch=FakePypi()
    ).upload


def test_a_failed_lookup_stops_a_publish_but_only_warns_in_a_dry_run(tmp_path):
    tree = rp.FsTree(_repo(tmp_path))
    publish = rp.make_plan(
        tree, event="workflow_dispatch", dry_run=False, fetch=FakePypi(status=403)
    )
    assert publish.problems and all("HTTP 403" in p for p in publish.problems)
    dry = rp.make_plan(tree, event="pull_request", fetch=FakePypi(status=403))
    assert dry.problems == [] and len(dry.warnings) == 4
    assert json.loads(dry.outputs()["uploads"]) == []


def test_a_publish_must_look_versions_up(tmp_path):
    plan = rp.make_plan(
        rp.FsTree(_repo(tmp_path)), event="workflow_dispatch", dry_run=False, fetch=None
    )
    assert plan.problems == ["a publish run must look versions up on PyPI (--pypi)"]


def test_a_version_on_pypi_with_no_files_is_a_problem(tmp_path):
    pypi = FakePypi(published={("aisquare-pipe", "0.5.0"): {}})
    plan = rp.make_plan(
        rp.FsTree(_repo(tmp_path)), event="release", tag="v0.5.0", fetch=pypi
    )
    assert any("registered on PyPI with no files" in p for p in plan.problems)


def test_plan_cli_writes_github_outputs_and_fails_on_a_problem(tmp_path, monkeypatch):
    _repo(tmp_path / "repo")
    output, summary = tmp_path / "out", tmp_path / "summary"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    assert rp.main(["plan", "--root", str(tmp_path / "repo")]) == 0
    written = dict(line.split("=", 1) for line in output.read_text().splitlines())
    assert set(written) == {"packages", "upload", "connectors", "framework", "uploads"}
    assert len(json.loads(written["packages"])) == 4
    assert "### PyPI dry run" in summary.read_text()
    assert (
        rp.main(
            [
                "plan",
                "--root",
                str(tmp_path / "repo"),
                "--event",
                "release",
                "--tag",
                "v9",
            ]
        )
        == 1
    )


# --------------------------------------------------------------- check-dist


def _dist(tmp_path: Path, *names: str) -> Path:
    dist = tmp_path / "dist"
    dist.mkdir(exist_ok=True)
    for n in names:
        (dist / n).write_bytes(n.encode())
    return dist


def test_check_dist_accepts_the_declared_name_and_version(tmp_path):
    pkg = rp.Package(
        path="connectors/gmail", name="aisquare-pipe-gmail", version="0.2.0"
    )
    dist = _dist(
        tmp_path,
        "aisquare_pipe_gmail-0.2.0-py3-none-any.whl",
        "aisquare_pipe_gmail-0.2.0.tar.gz",
    )
    assert rp.dist_problems(pkg, dist) == []


def test_check_dist_rejects_a_build_at_another_version(tmp_path):
    pkg = rp.Package(path=".", name="aisquare-pipe", version="0.2.0")
    dist = _dist(
        tmp_path, "aisquare_pipe-0.1.1-py3-none-any.whl", "aisquare_pipe-0.1.1.tar.gz"
    )
    problems = rp.dist_problems(pkg, dist)
    assert len(problems) == 2 and all(
        "declares aisquare-pipe 0.2.0" in p for p in problems
    )


def test_check_dist_wants_exactly_one_wheel_and_one_sdist(tmp_path):
    pkg = rp.Package(path=".", name="aisquare-pipe", version="0.5.0")
    assert rp.dist_problems(
        pkg, _dist(tmp_path, "aisquare_pipe-0.5.0-py3-none-any.whl")
    )
    assert rp.dist_problems(pkg, tmp_path / "missing")


# ------------------------------------------------------------------- verify


def _built(root: Path, name: str, files: dict[str, bytes]) -> dict[str, str]:
    d = root / f"dist-{name}"
    d.mkdir(parents=True)
    for fname, data in files.items():
        (d / fname).write_bytes(data)
    return {fname: hashlib.sha256(data).hexdigest() for fname, data in files.items()}


UPLOAD = {"path": "connectors/gmail", "name": "aisquare-pipe-gmail", "version": "0.2.0"}
FILES = {
    "aisquare_pipe_gmail-0.2.0-py3-none-any.whl": b"wheel",
    "aisquare_pipe_gmail-0.2.0.tar.gz": b"sdist",
}


def test_verify_passes_when_pypi_has_this_runs_files(tmp_path):
    digests = _built(tmp_path, "aisquare-pipe-gmail", FILES)
    pypi = FakePypi(published={("aisquare-pipe-gmail", "0.2.0"): digests})
    assert rp.verify_problems([UPLOAD], tmp_path, pypi, timeout=0) == []


def test_verify_fails_an_upload_that_never_landed(tmp_path):
    _built(tmp_path, "aisquare-pipe-gmail", FILES)
    problems = rp.verify_problems([UPLOAD], tmp_path, FakePypi(), timeout=0)
    assert problems == [
        (
            "aisquare-pipe-gmail 0.2.0: not on PyPI after the upload: "
            "aisquare_pipe_gmail-0.2.0-py3-none-any.whl, aisquare_pipe_gmail-0.2.0.tar.gz"
        )
    ]


def test_verify_fails_when_pypi_holds_different_bytes(tmp_path):
    digests = _built(tmp_path, "aisquare-pipe-gmail", FILES)
    digests["aisquare_pipe_gmail-0.2.0.tar.gz"] = "f" * 64
    problems = rp.verify_problems(
        [UPLOAD],
        tmp_path,
        FakePypi(published={("aisquare-pipe-gmail", "0.2.0"): digests}),
        timeout=0,
    )
    assert (
        len(problems) == 1
        and "aisquare_pipe_gmail-0.2.0.tar.gz has sha256 ffff" in problems[0]
    )


def test_verify_polls_until_pypi_catches_up(tmp_path):
    digests = _built(tmp_path, "aisquare-pipe-gmail", FILES)
    pypi = FakePypi()
    now = [0.0]

    def sleep(seconds):
        now[0] += seconds
        pypi.published[("aisquare-pipe-gmail", "0.2.0")] = (
            digests  # lands after the first look
        )

    problems = rp.verify_problems(
        [UPLOAD],
        tmp_path,
        pypi,
        timeout=60,
        interval=5,
        sleep=sleep,
        clock=lambda: now[0],
    )
    assert problems == [] and now[0] == 5


# ----------------------------------------------------------------- coverage


def _consumer(
    root: Path, pyproject: str, requirements_in: str = "", lock: str = ""
) -> Path:
    _write(root / "pyproject.toml", pyproject)
    if requirements_in:
        _write(root / "requirements.in", requirements_in)
    if lock:
        _write(root / "requirements.lock", lock)
    return root


SERVICE = """\
    [project]
    name = "aisquare-pipe-service"
    version = "0.1.0"
    dependencies = ["aisquare-pipe>=0.5.0", "fastapi>=0.110"]

    [project.optional-dependencies]
    gmail = ["aisquare-pipe-gmail>=0.2.0"]
    toolkits = ["aisquare-pipe[toolkits]>=0.5.0", "aisquare-pipe-service[gmail]"]
    """
LOCK = """\
    aisquare-pipe==0.1.1 \\
        --hash=sha256:00
        # via
        #   aisquare-pipe-local
    aisquare-pipe-local==0.1.0 \\
        --hash=sha256:11
    fastapi==0.115.0 \\
        --hash=sha256:22
    """


def test_coverage_follows_extras_and_dependencies_through_the_pipe_tree(tmp_path):
    pipe = rp.FsTree(_repo(tmp_path / "pipe"))
    consumer = rp.FsTree(
        _consumer(
            tmp_path / "svc",
            SERVICE,
            "aisquare-pipe[full,toolkits]>=0.5.0  # the image\n-c constraints.txt\n",
            LOCK,
        )
    )
    needs = rp.coverage(pipe, consumer)
    by = {(n.requirement, n.source): n for n in needs}
    assert all(not n.verdict.startswith("FAIL") for n in needs)
    # through [full] -> [popular] and [toolkits], to every connector here
    assert {n.package for n in needs} == {
        ".",
        "connectors/gmail",
        "connectors/local",
        "connectors/slack",
    }
    assert by[("aisquare-pipe-local", "aisquare-pipe[popular]")].verdict == "ok"
    assert (
        by[("aisquare-pipe>=0.5.0", "aisquare-pipe-gmail dependencies")].verdict == "ok"
    )
    # a lock pin below what the branch declares is the stale lock, relocked after publishing
    assert by[("aisquare-pipe==0.1.1", "requirements.lock")].verdict.startswith(
        "ok (pinned below"
    )
    # the consumer's own extras and third-party pins are not checked
    assert not any(
        "aisquare-pipe-service" in n.requirement or "fastapi" in n.requirement
        for n in needs
    )


def test_coverage_fails_a_missing_package_a_floor_above_the_branch_and_a_pin_above_it(
    tmp_path,
):
    pipe = rp.FsTree(_repo(tmp_path / "pipe"))
    consumer = rp.FsTree(
        _consumer(
            tmp_path / "svc",
            SERVICE.replace("aisquare-pipe-gmail>=0.2.0", "aisquare-pipe-gmail>=0.3.0"),
            "aisquare-pipe-googlecalendar>=0.2.0\naisquare-pipe[nope]\n",
            "aisquare-pipe-slack==0.9.0 \\\n    --hash=sha256:00\n",
        )
    )
    failed = {
        n.requirement: n.verdict
        for n in rp.coverage(pipe, consumer)
        if n.verdict.startswith("FAIL")
    }
    assert failed == {
        "aisquare-pipe-gmail>=0.3.0": "FAIL: needs >=0.3.0, connectors/gmail declares 0.2.0",
        "aisquare-pipe-googlecalendar>=0.2.0": f"FAIL: no package named aisquare-pipe-googlecalendar in {pipe.describe()}",
        "aisquare-pipe[nope]": "FAIL: aisquare-pipe has no [nope] extra",
        "aisquare-pipe-slack==0.9.0": "FAIL: needs ==0.9.0, connectors/slack declares 0.2.0",
    }


def test_coverage_cli_exits_nonzero_on_a_failure(tmp_path, capsys):
    _repo(tmp_path / "pipe")
    _consumer(tmp_path / "svc", SERVICE.replace(">=0.5.0", ">=0.6.0"))
    assert (
        rp.main(
            [
                "coverage",
                "--pipe-root",
                str(tmp_path / "pipe"),
                "--consumer-root",
                str(tmp_path / "svc"),
            ]
        )
        == 1
    )
    assert "failing" in capsys.readouterr().out


# ---------------------------------------------------------------- publish.yml


def test_publish_workflow_takes_its_matrix_from_the_plan_and_never_skips_silently():
    """The two ways the 2026-07-10 runs went wrong: a hand-kept matrix, and
    skip-existing turning an upload of an old version into a green job."""
    text = PUBLISH_YML.read_text()
    assert "skip-existing" not in text.replace("without skip-existing", "")
    assert "scripts/release_packages.py plan --pypi" in text
    assert "fromJSON(needs.plan.outputs.packages)" in text
    assert "fromJSON(needs.plan.outputs.connectors)" in text
    assert "release_packages.py verify" in text
    assert "connectors/" not in text.split("jobs:", 1)[1].replace("connectors/*", "")
