# Releasing

This repo is a monorepo of independently published PyPI packages:

| Package | Path |
|---|---|
| `aisquare-pipe` | `.` |
| `aisquare-pipe-local` | `connectors/local` |
| `aisquare-pipe-dropbox` | `connectors/dropbox` |
| `aisquare-pipe-onedrive` | `connectors/onedrive` |
| `aisquare-pipe-googledrive` | `connectors/googledrive` |
| `aisquare-pipe-salesforce` | `connectors/salesforce` |
| `aisquare-pipe-docusign` | `connectors/docusign` |
| `aisquare-pipe-composio` | `connectors/composio` |
| `aisquare-pipe-gateway` | `connectors/gateway` |
| `aisquare-pipe-n8n` | `connectors/n8n` |
| `aisquare-pipe-github` | `connectors/github` |
| `aisquare-pipe-graphify` | `connectors/graphify` |
| `aisquare-pipe-slack` | `connectors/slack` |

Publishing is automated by [`.github/workflows/publish.yml`](.github/workflows/publish.yml)
using **PyPI Trusted Publishing (OIDC)** — no API tokens are stored anywhere.

## Versioning

Each package carries its own version, declared in two places that must match
(enforced by `pipe validate`'s `hygiene.version-sync` check):

- `version` in the package's `pyproject.toml`
- the `version` attribute on its connector class

The workflow has no package list of its own. `scripts/release_packages.py plan`
reads the root `pyproject.toml` and every `connectors/*/pyproject.toml`, so a
new connector directory is built, checked and published with no edit to the
workflow. It still needs its PyPI publisher ("One-time setup") before the
first run that uploads it.

Every run considers **every** package, each at the version declared in its
`pyproject.toml`. The `plan` job looks each version up on PyPI first:

- **Already on PyPI:** skipped, and the run summary lists it as skipped.
  A skipped package is not re-published even if its code changed after that
  version was uploaded. Bump its version to ship the change.
- **Not on PyPI:** uploaded. The upload doesn't use `skip-existing`, so a
  rejected upload fails the run. After the uploads, the `verify` job checks
  each file on PyPI against the sha256 of the file the run built.

So bumping only the packages that changed is enough. Keep all versions equal
if you prefer strict lockstep; the workflow supports either model.

The set must also install together. `plan` fails when a package's
`aisquare-pipe*` requirement (a dependency, or an entry in an extra such as
`[full]`) names no package here, or names one whose declared version doesn't
satisfy it. Connectors upload before `aisquare-pipe`, and `aisquare-pipe`
uploads only once every connector upload has succeeded. That way its
`[popular]` / `[full]` extras never name a release that isn't on PyPI.

## Dry run

Every pull request runs the same workflow as a dry run. It builds each
package's wheel and sdist, checks that both carry the declared name and
version, and runs `twine check --strict`. Nothing is uploaded. To dry-run a
branch by hand, use **Actions → publish → Run workflow** and leave **dry-run**
ticked (the default). Locally:

```bash
python scripts/release_packages.py plan --pypi   # what a publish would upload
python -m build --outdir dist connectors/slack && twine check --strict dist/*
```

## One-time setup

Trusted Publishing must be configured once per project. For projects that don't
exist on PyPI yet, use a **pending publisher**:

1. Go to <https://pypi.org/manage/account/publishing/>.
2. Add a pending publisher for **each** package name in the table above with:
   - **Owner:** `AISquare-Studio`
   - **Repository:** `pipe`
   - **Workflow:** `publish.yml`
   - **Environment:** `pypi`
3. In GitHub: **Settings → Environments → New environment** named `pypi`
   (optionally add required reviewers to gate publishes).

The run summary marks each package whose project is new on PyPI. Register its
pending publisher **before** the run that first uploads it. Without one, the
upload is rejected. The publisher that the existing projects share matches
every job, so the token is still minted, but a token can't create a new
project ("Non-user identities cannot create new projects"). Every new project
from `aisquare-pipe-onedrive` (2026-06-20) to `aisquare-pipe-graphify`
(2026-07-10) failed this way on its first run.

PyPI creates a new project when a job mints its token. It uses the first
pending publisher that matches the repository, workflow and environment, and
that one may belong to another package. So a run that creates several
projects at once can still refuse a job whose own project doesn't exist yet.
**Re-run failed jobs**: by then every project exists.

## Cutting a release

1. Bump `version` in the `pyproject.toml` (and the matching connector class
   attribute) for each package you're releasing; update `CHANGELOG.md`.
2. Open a PR and merge to `main`. The PR's publish dry run must be green.
3. **Create a GitHub Release** with the tag `v<aisquare-pipe version>`, e.g.
   `v0.4.0` when the root `pyproject.toml` says `0.4.0`
   (Releases → Draft a new release). Publishing the release triggers the
   workflow, which uploads every package whose version isn't on PyPI yet. A
   tag that doesn't match the root version fails the run before anything is
   built. `v0.2.0` (2026-07-10) was a connector-only release: the root still
   said `0.1.1`. Its `aisquare-pipe` job rebuilt 0.1.1, `skip-existing`
   skipped it, and the job went green. Nothing in the run said that no
   framework was uploaded.

To publish connectors without a new `aisquare-pipe` (a connector-only
release), or to upload what a failed run left behind, run it manually:
**Actions → publish → Run workflow**, pick the branch, and untick **dry-run**.
