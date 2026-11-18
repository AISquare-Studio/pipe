"""scripts/release_packages.py: what publish.yml builds, skips, uploads and verifies.

Hermetic: PyPI is a fake ``fetch``, wheels are built by hand as zip files, and
nothing touches the network.
"""

from __future__ import annotations

import csv
import hashlib
import importlib.util
import io
import json
import sys
import textwrap
import tomllib
import zipfile
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


ROOT_PYPROJECT = """\
    [project]
    name = "aisquare-pipe"
    version = "{version}"
    dependencies = ["click>=8.0", "requests>=2.31"]

    [project.optional-dependencies]
    dev = ["pytest>=7.0"]
    gmail = []
    slack = []
    dropbox = ["dropbox>=12.0"]
    popular = ["aisquare-pipe[dropbox]"]
    full = ["aisquare-pipe[popular,gmail,slack]"]
    toolkits = ["aisquare-pipe[gmail,slack]"]
    """


def _repo(
    root: Path,
    version: str = "0.5.0",
    pyproject: str | None = None,
    dirs: tuple[str, ...] = ("dropbox", "gmail", "slack"),
) -> Path:
    """A pipe-shaped tree: the root distribution and connectors/<dir>/src."""
    _write(
        root / "pyproject.toml",
        pyproject if pyproject is not None else ROOT_PYPROJECT.format(version=version),
    )
    _write(root / "src" / "aisquare" / "pipe" / "__init__.py", "")
    for d in dirs:
        _write(
            root / "connectors" / d / "src" / f"aisquare_pipe_{d}" / "__init__.py", ""
        )
        _write(
            root / "connectors" / d / "src" / f"aisquare_pipe_{d}" / "connector.py", ""
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


SHIM_NAMES = [f"aisquare-pipe-{n}" for n in rp.SHIM_PROJECTS]


def _wheel(
    path: Path, files: dict[str, str], metadata: str = "Name: x\nVersion: 0\n"
) -> Path:
    """A wheel at ``path`` holding ``files`` plus a dist-info with METADATA and RECORD."""
    dist_info = f"{path.name.split('-')[0]}-{path.name.split('-')[1]}.dist-info"
    entries = {**files, f"{dist_info}/METADATA": metadata}
    record = io.StringIO()
    writer = csv.writer(record)
    for name in entries:
        writer.writerow([name, "sha256=x", "1"])
    writer.writerow([f"{dist_info}/RECORD", "", ""])
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as zf:
        for name, text in entries.items():
            zf.writestr(name, text)
        zf.writestr(f"{dist_info}/RECORD", record.getvalue())
    return path


# --------------------------------------------------------------- the package


def test_the_distribution_is_read_from_the_root_pyproject(tmp_path):
    pkg = rp.framework_package(rp.FsTree(_repo(tmp_path)))
    assert (pkg.path, pkg.name, pkg.version) == (".", "aisquare-pipe", "0.5.0")
    assert set(pkg.extras) >= {"gmail", "slack", "dropbox", "full", "toolkits"}


@pytest.mark.parametrize(
    ("pyproject", "message"),
    [
        ('[project]\nname = "aisquare-pipe"\ndynamic = ["version"]\n', "static string"),
        ('[project]\nname = "aisquare-pipe"\nversion = "soon"\n', "PEP 440"),
        (
            '[project]\nname = "aisquare-pipe-slack"\nversion = "0.1.0"\n',
            "must be aisquare-pipe",
        ),
    ],
)
def test_a_distribution_publish_yml_could_not_release_is_rejected(
    tmp_path, pyproject, message
):
    with pytest.raises(rp.ReleaseError, match=message):
        rp.framework_package(rp.FsTree(_repo(tmp_path, pyproject=pyproject)))


def test_the_shims_are_the_ten_projects_that_existed_before_0_5_0():
    shims = rp.shim_packages()
    assert [p.name for p in shims] == SHIM_NAMES
    assert {p.version for p in shims} == {"0.5.0"}
    assert shims[0].dependencies == ("aisquare-pipe[composio]>=0.5.0",)
    # Never-published connectors get no shim: they never had a PyPI project.
    assert not {
        "aisquare-pipe-slack",
        "aisquare-pipe-gmail",
        "aisquare-pipe-googledrive",
    } & set(SHIM_NAMES)


def test_git_tree_reads_the_same_distribution_as_the_working_tree(tmp_path):
    import subprocess

    repo = _repo(tmp_path / "repo")
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
    assert rp.framework_package(rp.GitTree(repo, "HEAD")) == rp.framework_package(
        rp.FsTree(repo)
    )


# ------------------------------------------------------------- consistency


def test_this_repo_is_a_consistent_distribution():
    """Every extra the root names exists, nothing depends on an old per-connector
    package, and every connectors/<dir>/ has an extra that [full] pulls in."""
    framework = rp.framework_package(rp.FsTree(REPO_ROOT))
    assert rp.internal_problems(framework, rp.FsTree(REPO_ROOT)) == []
    assert set(rp.connector_dirs(REPO_ROOT)) <= rp.bundle(framework, "full")


def test_internal_problems_flag_metadata_that_would_not_install(tmp_path):
    pyproject = ROOT_PYPROJECT.format(version="0.5.0").replace(
        'full = ["aisquare-pipe[popular,gmail,slack]"]',
        'full = ["aisquare-pipe[popular,gmail,nope]", "aisquare-pipe-slack>=0.2.0", "aisquare-pipe[gmail]>=0.6"]',
    )
    root = _repo(
        tmp_path, pyproject=pyproject, dirs=("dropbox", "gmail", "slack", "orphan")
    )
    problems = "\n".join(
        rp.internal_problems(rp.framework_package(rp.FsTree(root)), rp.FsTree(root))
    )
    assert "there is no [nope] extra" in problems
    assert (
        "requires 'aisquare-pipe-slack>=0.2.0': connectors ship inside aisquare-pipe"
        in problems
    )
    assert "requires 'aisquare-pipe[gmail]>=0.6', but this is 0.5.0" in problems
    assert "connectors/orphan/ has no [orphan] extra" in problems
    assert "[full] doesn't pull in [slack]" in problems


# -------------------------------------------------------------------- plan


@pytest.mark.parametrize("target", ["aisquare-pipe", "shims"])
def test_pr_dry_run_builds_the_distribution_and_every_shim_and_uploads_nothing(
    tmp_path, target
):
    """Whatever the target input says: a PR checks everything a publish
    could build, and the shims' floor (aisquare-pipe 0.5.0 on PyPI) is no
    concern of a PR's."""
    plan = rp.make_plan(
        rp.FsTree(_repo(tmp_path)),
        event="pull_request",
        fetch=_pypi_with_shim_projects(),
        target=target,
    )
    out = plan.outputs()
    assert (plan.problems, plan.warnings) == ([], [])
    assert json.loads(out["packages"]) == [
        {"path": ".", "name": "aisquare-pipe", "version": "0.5.0"}
    ] + [
        {"path": f"shims/{n}", "name": f"aisquare-pipe-{n}", "version": "0.5.0"}
        for n in rp.SHIM_PROJECTS
    ]
    assert (
        out["upload"],
        out["uploads"],
        out["shims"],
        out["framework"],
        out["target"],
    ) == (
        "false",
        "[]",
        "[]",
        "false",
        "all",
    )
    summary = plan.summary()
    assert summary.count("would upload") == 11 and "**upload**" not in summary


def test_a_pr_dry_run_fails_when_a_shim_project_is_gone(tmp_path):
    plan = rp.make_plan(
        rp.FsTree(_repo(tmp_path)),
        event="pull_request",
        fetch=FakePypi(projects={"aisquare-pipe", *SHIM_NAMES[:-1]}),
    )
    assert plan.problems == [
        "aisquare-pipe-salesforce is not on PyPI at all, and this workflow never creates a project"
    ]


def test_a_release_uploads_the_new_version(tmp_path):
    plan = rp.make_plan(
        rp.FsTree(_repo(tmp_path)),
        event="release",
        tag="v0.5.0",
        fetch=FakePypi(projects={"aisquare-pipe"}),
    )
    out = plan.outputs()
    assert plan.problems == []
    assert (out["upload"], out["framework"]) == ("true", "true")
    assert [u["name"] for u in json.loads(out["uploads"])] == ["aisquare-pipe"]


def test_a_version_already_on_pypi_is_skipped_out_loud(tmp_path):
    pypi = FakePypi(
        published={("aisquare-pipe", "0.5.0"): {"aisquare_pipe-0.5.0.tar.gz": "0"}}
    )
    plan = rp.make_plan(
        rp.FsTree(_repo(tmp_path)), event="release", tag="v0.5.0", fetch=pypi
    )
    assert plan.outputs()["upload"] == "false"
    assert (
        "| `aisquare-pipe` | 0.5.0 | yes | skip: this version is already on PyPI |"
        in plan.summary()
    )
    assert plan.warnings == ["every version is already on PyPI: nothing to upload"]


def test_a_release_tag_that_is_not_the_version_fails_the_plan(tmp_path):
    """v0.2.0 (2026-07-10), a connector-only release: the tag said 0.2.0 and
    pyproject.toml 0.1.1, and the aisquare-pipe job went green on a silent skip."""
    pypi = FakePypi(
        published={("aisquare-pipe", "0.1.1"): {"aisquare_pipe-0.1.1.tar.gz": "0"}}
    )
    plan = rp.make_plan(
        rp.FsTree(_repo(tmp_path, version="0.1.1")),
        event="release",
        tag="v0.2.0",
        fetch=pypi,
    )
    assert plan.problems == [
        (
            "release tag 'v0.2.0' does not match aisquare-pipe 0.1.1 in pyproject.toml: "
            "tag the release v0.1.1, or bump pyproject.toml first"
        )
    ]


def test_the_plan_never_creates_a_project(tmp_path):
    plan = rp.make_plan(
        rp.FsTree(_repo(tmp_path)), event="release", tag="v0.5.0", fetch=FakePypi()
    )
    assert plan.problems == [
        "aisquare-pipe is not on PyPI at all, and this workflow never creates a project"
    ]


def test_manual_run_is_a_dry_run_unless_asked_otherwise(tmp_path):
    tree = rp.FsTree(_repo(tmp_path))
    pypi = FakePypi(projects={"aisquare-pipe"})
    assert not rp.make_plan(
        tree, event="workflow_dispatch", dry_run=True, fetch=pypi
    ).upload
    assert rp.make_plan(
        tree, event="workflow_dispatch", dry_run=False, fetch=pypi
    ).upload


def test_a_failed_lookup_stops_a_publish_but_only_warns_in_a_dry_run(tmp_path):
    tree = rp.FsTree(_repo(tmp_path))
    publish = rp.make_plan(
        tree, event="workflow_dispatch", dry_run=False, fetch=FakePypi(status=403)
    )
    assert publish.problems and all("HTTP 403" in p for p in publish.problems)
    dry = rp.make_plan(tree, event="pull_request", fetch=FakePypi(status=403))
    assert dry.problems == [] and len(dry.warnings) == 1 + len(rp.SHIM_PROJECTS)
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


# ------------------------------------------------------------------ shims


def _pypi_with_shim_projects(**kw) -> FakePypi:
    return FakePypi(projects={"aisquare-pipe", *SHIM_NAMES}, **kw)


def test_the_shim_run_uploads_every_shim_once_aisquare_pipe_is_out(tmp_path):
    pypi = _pypi_with_shim_projects(
        published={("aisquare-pipe", "0.5.0"): {"aisquare_pipe-0.5.0.tar.gz": "0"}}
    )
    plan = rp.make_plan(
        rp.FsTree(_repo(tmp_path)),
        event="workflow_dispatch",
        dry_run=False,
        fetch=pypi,
        target="shims",
    )
    out = plan.outputs()
    assert plan.problems == []
    assert out["target"] == "shims" and out["framework"] == "false"
    assert [s["name"] for s in json.loads(out["shims"])] == SHIM_NAMES
    assert json.loads(out["shims"])[0]["path"] == "shims/composio"


def test_shims_wait_for_the_aisquare_pipe_they_require(tmp_path):
    tree = rp.FsTree(_repo(tmp_path))
    publish = rp.make_plan(
        tree,
        event="workflow_dispatch",
        dry_run=False,
        fetch=_pypi_with_shim_projects(),
        target="shims",
    )
    assert publish.problems == [
        (
            "aisquare-pipe 0.5.0 is not on PyPI yet (new version), and every shim requires it: "
            "publish aisquare-pipe first"
        )
    ]
    dry = rp.make_plan(
        tree,
        event="workflow_dispatch",
        fetch=_pypi_with_shim_projects(),
        target="shims",
    )
    assert dry.problems == [] and "publish aisquare-pipe first" in dry.warnings[0]


def test_a_shim_never_creates_a_project(tmp_path):
    pypi = FakePypi(
        projects={"aisquare-pipe", *SHIM_NAMES[1:]},
        published={("aisquare-pipe", "0.5.0"): {"x": "0"}},
    )
    plan = rp.make_plan(
        rp.FsTree(_repo(tmp_path)),
        event="workflow_dispatch",
        dry_run=False,
        fetch=pypi,
        target="shims",
    )
    assert plan.problems == [
        "aisquare-pipe-composio is not on PyPI at all, and this workflow never creates a project"
    ]


def test_a_release_only_ever_publishes_the_distribution(tmp_path):
    plan = rp.make_plan(
        rp.FsTree(_repo(tmp_path)),
        event="release",
        tag="v0.5.0",
        fetch=FakePypi(projects={"aisquare-pipe"}),
        target="shims",
    )
    assert plan.target == "aisquare-pipe" and [p.name for p in plan.packages] == [
        "aisquare-pipe"
    ]


def test_shim_sources_have_no_code_and_one_requirement():
    files = rp.shim_files("onedrive")
    data = tomllib.loads(files["pyproject.toml"])
    assert (data["project"]["name"], data["project"]["version"]) == (
        "aisquare-pipe-onedrive",
        "0.5.0",
    )
    assert data["project"]["dependencies"] == ["aisquare-pipe[onedrive]>=0.5.0"]
    assert data["tool"]["setuptools"] == {"packages": [], "py-modules": []}
    assert 'pip install "aisquare-pipe[onedrive]"' in files["README.md"]
    assert "--force-reinstall --no-deps aisquare-pipe" in files["README.md"]
    # Uninstall-first comes before the install of the extra.
    readme = files["README.md"]
    upgrade = readme.index("pip uninstall -y aisquare-pipe-onedrive\n")
    assert readme.index('pip install "aisquare-pipe[onedrive]>=0.5.0"') > upgrade


def _readme_in_place_upgrade() -> str:
    """README.md's "If you already upgraded in place" text: the repair
    command and its two cases, up to the paragraph after the list."""
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    start = readme.index("**If you already upgraded in place**")
    return readme[start : readme.index("\n\n", readme.index("\n- ", start)) + 1]


def test_readme_splits_the_two_in_place_upgrades():
    """`pip install -U aisquare-pipe-<x>` breaks the connector at once; the
    extra installed over the old package breaks only at a later uninstall."""
    text = _readme_in_place_upgrade()
    assert "pip install --force-reinstall --no-deps aisquare-pipe\n" in text
    immediate, later = (line for line in text.splitlines() if line.startswith("- "))
    assert immediate.startswith("- `pip install -U aisquare-pipe-<x>`")
    assert "at once" in immediate and "right after" in immediate
    assert immediate.index("at once") < immediate.index("right after")
    assert later.startswith(
        "- If you installed `aisquare-pipe[<x>]` over the old package"
    )
    assert "never `pip uninstall aisquare-pipe-<x>`" in later and "afterwards" in later


@pytest.mark.parametrize("name", sorted(rp.SHIM_PROJECTS))
def test_each_shim_readme_has_the_readme_s_in_place_upgrade_text(name):
    """The shim README (PyPI's page for aisquare-pipe-<x>) says what
    README.md (aisquare-pipe's page) says, for that connector."""
    expected = _readme_in_place_upgrade().replace("<x>", name)
    assert expected in rp.shim_files(name)["README.md"]


def test_plan_cli_writes_github_outputs_and_fails_on_a_problem(tmp_path, monkeypatch):
    _repo(tmp_path / "repo")
    output, summary = tmp_path / "out", tmp_path / "summary"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    assert rp.main(["plan", "--root", str(tmp_path / "repo")]) == 0
    written = dict(line.split("=", 1) for line in output.read_text().splitlines())
    assert set(written) == {
        "target",
        "packages",
        "upload",
        "framework",
        "shims",
        "uploads",
    }
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


# --------------------------------------------------------------- dist checks


def _dist(tmp_path: Path, *names: str) -> Path:
    dist = tmp_path / "dist"
    dist.mkdir(exist_ok=True)
    for n in names:
        (dist / n).write_bytes(n.encode())
    return dist


def test_check_dist_accepts_the_declared_name_and_version(tmp_path):
    pkg = rp.Package(path=".", name="aisquare-pipe", version="0.5.0")
    dist = _dist(
        tmp_path, "aisquare_pipe-0.5.0-py3-none-any.whl", "aisquare_pipe-0.5.0.tar.gz"
    )
    assert rp.dist_problems(pkg, dist) == []


def test_check_dist_rejects_a_build_at_another_version(tmp_path):
    pkg = rp.Package(path=".", name="aisquare-pipe", version="0.2.0")
    dist = _dist(
        tmp_path, "aisquare_pipe-0.1.1-py3-none-any.whl", "aisquare_pipe-0.1.1.tar.gz"
    )
    problems = rp.dist_problems(pkg, dist)
    assert len(problems) == 2 and all(
        "the package is aisquare-pipe 0.2.0" in p for p in problems
    )


def test_check_dist_wants_exactly_one_wheel_and_one_sdist(tmp_path):
    pkg = rp.Package(path=".", name="aisquare-pipe", version="0.5.0")
    assert rp.dist_problems(
        pkg, _dist(tmp_path, "aisquare_pipe-0.5.0-py3-none-any.whl")
    )
    assert rp.dist_problems(pkg, tmp_path / "missing")


def _framework_wheel(tmp_path: Path, root: Path, drop: str = "", add: str = "") -> Path:
    files = dict.fromkeys(rp.expected_modules(root), "")
    files.pop(drop, None)
    if add:
        files[add] = ""
    return _wheel(tmp_path / "w" / "aisquare_pipe-0.5.0-py3-none-any.whl", files)


def test_the_wheel_record_must_hold_every_module_of_the_checkout(tmp_path):
    root = _repo(tmp_path / "repo")
    assert rp.expected_modules(root) == {
        "aisquare/pipe/__init__.py",
        *(
            f"aisquare_pipe_{d}/{f}"
            for d in ("dropbox", "gmail", "slack")
            for f in ("__init__.py", "connector.py")
        ),
    }
    assert rp.framework_wheel_problems(_framework_wheel(tmp_path, root), root) == []
    wheel = _framework_wheel(tmp_path, root, drop="aisquare_pipe_gmail/connector.py")
    assert rp.framework_wheel_problems(wheel, root) == [
        "aisquare_pipe-0.5.0-py3-none-any.whl: RECORD lacks aisquare_pipe_gmail/connector.py"
    ]


def test_a_connector_module_without_a_directory_is_a_problem(tmp_path):
    root = _repo(tmp_path / "repo")
    wheel = _framework_wheel(tmp_path, root, add="aisquare_pipe_ghost/__init__.py")
    assert rp.framework_wheel_problems(wheel, root) == [
        "aisquare_pipe-0.5.0-py3-none-any.whl: carries aisquare_pipe_ghost, which no connectors/<dir>/src provides"
    ]


SHIM = rp.shim_packages()[rp.SHIM_PROJECTS.index("onedrive")]


def _shim_wheel(
    tmp_path: Path, requires: list[str], files: dict[str, str] | None = None
) -> Path:
    metadata = "Metadata-Version: 2.4\nName: aisquare-pipe-onedrive\nVersion: 0.5.0\n"
    metadata += "".join(f"Requires-Dist: {r}\n" for r in requires)
    return _wheel(
        tmp_path / "s" / "aisquare_pipe_onedrive-0.5.0-py3-none-any.whl",
        files or {},
        metadata,
    )


def test_a_shim_wheel_has_no_code_and_one_requirement(tmp_path):
    assert (
        rp.shim_wheel_problems(
            SHIM, _shim_wheel(tmp_path, ["aisquare-pipe[onedrive]>=0.5.0"])
        )
        == []
    )


@pytest.mark.parametrize(
    ("requires", "files", "says"),
    [
        (
            ["aisquare-pipe[onedrive]>=0.5.0"],
            {"aisquare_pipe_onedrive/__init__.py": ""},
            "carries files besides",
        ),
        (["aisquare-pipe>=0.5.0"], None, "not exactly"),
        (["aisquare-pipe[onedrive]>=0.4.0"], None, "not exactly"),
        (["aisquare-pipe[onedrive]>=0.5.0", "requests"], None, "not exactly"),
        (['aisquare-pipe[onedrive]>=0.5.0; python_version < "3"'], None, "not exactly"),
    ],
)
def test_a_wrong_shim_wheel_is_a_problem(tmp_path, requires, files, says):
    problems = rp.shim_wheel_problems(SHIM, _shim_wheel(tmp_path, requires, files))
    assert len(problems) == 1 and says in problems[0]


# ------------------------------------------------------------------- verify


def _built(root: Path, name: str, files: dict[str, bytes]) -> dict[str, str]:
    d = root / f"dist-{name}"
    d.mkdir(parents=True)
    for fname, data in files.items():
        (d / fname).write_bytes(data)
    return {fname: hashlib.sha256(data).hexdigest() for fname, data in files.items()}


UPLOAD = {"path": ".", "name": "aisquare-pipe", "version": "0.5.0"}
FILES = {
    "aisquare_pipe-0.5.0-py3-none-any.whl": b"wheel",
    "aisquare_pipe-0.5.0.tar.gz": b"sdist",
}


def test_verify_passes_when_pypi_has_this_runs_files(tmp_path):
    digests = _built(tmp_path, "aisquare-pipe", FILES)
    pypi = FakePypi(published={("aisquare-pipe", "0.5.0"): digests})
    assert rp.verify_problems([UPLOAD], tmp_path, pypi, timeout=0) == []


def test_verify_fails_an_upload_that_never_landed(tmp_path):
    _built(tmp_path, "aisquare-pipe", FILES)
    problems = rp.verify_problems([UPLOAD], tmp_path, FakePypi(), timeout=0)
    assert problems == [
        (
            "aisquare-pipe 0.5.0: not on PyPI after the upload: "
            "aisquare_pipe-0.5.0-py3-none-any.whl, aisquare_pipe-0.5.0.tar.gz"
        )
    ]


def test_verify_fails_when_pypi_holds_different_bytes(tmp_path):
    digests = _built(tmp_path, "aisquare-pipe", FILES)
    digests["aisquare_pipe-0.5.0.tar.gz"] = "f" * 64
    pypi = FakePypi(published={("aisquare-pipe", "0.5.0"): digests})
    problems = rp.verify_problems([UPLOAD], tmp_path, pypi, timeout=0)
    assert (
        len(problems) == 1
        and "aisquare_pipe-0.5.0.tar.gz has sha256 ffff" in problems[0]
    )


def test_verify_polls_until_pypi_catches_up(tmp_path):
    digests = _built(tmp_path, "aisquare-pipe", FILES)
    pypi = FakePypi()
    now = [0.0]

    def sleep(seconds):
        now[0] += seconds
        pypi.published[("aisquare-pipe", "0.5.0")] = (
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
    gmail = ["aisquare-pipe[gmail]>=0.5.0"]
    toolkits = ["aisquare-pipe[toolkits]>=0.5.0", "aisquare-pipe-service[gmail]"]
    """
LOCK = """\
    aisquare-pipe==0.1.1 \\
        --hash=sha256:00
    aisquare-pipe-onedrive==0.1.0 \\
        --hash=sha256:11
    fastapi==0.115.0 \\
        --hash=sha256:22
    """


def test_coverage_of_a_consumer_written_for_one_distribution(tmp_path):
    pipe = rp.FsTree(_repo(tmp_path / "pipe"))
    consumer = rp.FsTree(
        _consumer(
            tmp_path / "svc",
            SERVICE,
            "aisquare-pipe[full,toolkits]>=0.5.0  # image\n-c c.txt\n",
            LOCK,
        )
    )
    by = {(n.requirement, n.source): n for n in rp.coverage(pipe, consumer)}
    assert not any(n.verdict.startswith("FAIL") for n in by.values())
    assert (
        by[("aisquare-pipe[full,toolkits]>=0.5.0", "requirements.in")].verdict == "ok"
    )
    assert (
        by[("aisquare-pipe[gmail]>=0.5.0", "pyproject.toml [gmail]")].provided_by
        == "aisquare-pipe 0.5.0"
    )
    # The lock before its relock: an old framework pin, and an old per-connector one.
    assert by[("aisquare-pipe==0.1.1", "requirements.lock")].verdict.startswith(
        "ok (pinned below"
    )
    assert (
        "the relock must drop it"
        in by[("aisquare-pipe-onedrive==0.1.0", "requirements.lock")].verdict
    )
    # The consumer's own extras and third-party pins are not checked.
    assert not any("aisquare-pipe-service" in r or "fastapi" in r for r, _ in by)


def test_coverage_of_a_consumer_still_written_for_per_connector_packages(tmp_path):
    """pipe-as-service's release/pipe-router before its D8 change."""
    pipe = rp.FsTree(_repo(tmp_path / "pipe"))
    consumer = rp.FsTree(
        _consumer(
            tmp_path / "svc",
            SERVICE.replace(
                "aisquare-pipe[gmail]>=0.5.0", "aisquare-pipe-gmail>=0.2.0"
            ),
            "aisquare-pipe-github>=0.3.0\naisquare-pipe-github>=0.6.0\naisquare-pipe[nope]\naisquare-pipe>=0.6.0\n",
        )
    )
    verdicts = {n.requirement: n.verdict for n in rp.coverage(pipe, consumer)}
    assert verdicts["aisquare-pipe-gmail>=0.2.0"].startswith(
        "FAIL: aisquare-pipe-gmail is not a distribution"
    )
    assert "require aisquare-pipe[gmail]" in verdicts["aisquare-pipe-gmail>=0.2.0"]
    assert (
        verdicts["aisquare-pipe-github>=0.3.0"]
        == "ok via the shim; replace with aisquare-pipe[github]>=0.5.0"
    )
    assert (
        verdicts["aisquare-pipe-github>=0.6.0"]
        == "FAIL: needs >=0.6.0; its last release is the shim 0.5.0"
    )
    assert verdicts["aisquare-pipe[nope]"] == "FAIL: aisquare-pipe has no [nope] extra"
    assert (
        verdicts["aisquare-pipe>=0.6.0"]
        == "FAIL: needs >=0.6.0, this tree publishes 0.5.0"
    )


def test_coverage_cli_exits_nonzero_on_a_failure(tmp_path, capsys):
    _repo(tmp_path / "pipe")
    _consumer(tmp_path / "svc", SERVICE.replace(">=0.5.0", ">=0.6.0"))
    args = [
        "coverage",
        "--pipe-root",
        str(tmp_path / "pipe"),
        "--consumer-root",
        str(tmp_path / "svc"),
    ]
    assert rp.main(args) == 1
    assert "failing" in capsys.readouterr().out


# ---------------------------------------------------------------- publish.yml


def test_publish_workflow_is_plan_driven_and_never_skips_silently():
    """The two ways the 2026-07-10 runs went wrong: a hand-kept matrix, and
    skip-existing turning an upload of an old version into a green job. Plus
    the D8 shape: one distribution, and the shims on their own manual run."""
    text = PUBLISH_YML.read_text()
    assert "skip-existing" not in text.replace("without skip-existing", "")
    assert "release_packages.py plan --pypi" in text
    assert "fromJSON(needs.plan.outputs.packages)" in text
    assert "fromJSON(needs.plan.outputs.shims)" in text
    assert "release_packages.py check-dist" in text and "twine check --strict" in text
    assert "release_packages.py verify" in text
    jobs = [
        line
        for line in text.split("jobs:", 1)[1].splitlines()
        if not line.strip().startswith("#")
    ]
    assert not any("connectors/" in line for line in jobs)  # no hand-kept package list
