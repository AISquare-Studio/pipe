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
| `aisquare-pipe-gmail` | `connectors/gmail` |
| `aisquare-pipe-googlecalendar` | `connectors/googlecalendar` |

Publishing is automated by [`.github/workflows/publish.yml`](.github/workflows/publish.yml)
using **PyPI Trusted Publishing (OIDC)** — no API tokens are stored anywhere.

## Versioning

Each package carries its own version, declared in two places that must match
(enforced by `pipe validate`'s `hygiene.version-sync` check):

- `version` in the package's `pyproject.toml`
- the `version` attribute on its connector class

The release workflow publishes **every** package on each release, each at the
version declared in its `pyproject.toml`, and **skips any version already on
PyPI** (`skip-existing: true`). So bumping only the packages that changed is
enough — unchanged packages are simply skipped. Keep all versions equal if you
prefer strict lockstep; the workflow supports either model.

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

## Cutting a release

1. Bump `version` in the `pyproject.toml` (and the matching connector class
   attribute) for each package you're releasing; update `CHANGELOG.md`.
2. Open a PR and merge to `main`.
3. **Create a GitHub Release** with a tag like `vX.Y.Z`
   (Releases → Draft a new release). Publishing the release triggers the
   workflow, which builds and uploads all packages.

You can also run it manually: **Actions → publish → Run workflow**
(`workflow_dispatch`). Either way, every package whose project is new on PyPI
needs its pending trusted publisher registered first ("One-time setup").

## The 0.4.0 release (native connectors)

PyPI has `aisquare-pipe` 0.1.0 and 0.1.1 only (checked 2026-10-31): the
0.3.0 release was never published. Four projects in this run have never been
on PyPI at all:

| New on PyPI | Version |
|---|---|
| `aisquare-pipe-googledrive` | 0.3.0 |
| `aisquare-pipe-slack` | 0.1.0 |
| `aisquare-pipe-gmail` | 0.1.0 |
| `aisquare-pipe-googlecalendar` | 0.1.0 |

1. **Before tagging**, register a pending trusted publisher for each of the
   four ("One-time setup", step 2). This is the maintainer's step on
   pypi.org. The workflow runs with `fail-fast: false`, so a package with no
   publisher fails alone while the others upload. `aisquare-pipe-googledrive`
   is in `[popular]`, and so in `[full]`: if it is missing,
   `pip install "aisquare-pipe[popular]"` / `"aisquare-pipe[full]"` 0.4.0
   cannot resolve.
2. Publish (a GitHub Release `v0.4.0`, or Run workflow on `main`).
3. **After the run, before relocking pipe-as-service**, confirm all 8
   expected uploads on the simple index
   (`https://pypi.org/simple/<project>/`, or
   `pip index versions <project>`). Every other package was already on PyPI
   at its version when checked on 2026-10-31, so the run skips it.

   | Project | Version |
   |---|---|
   | `aisquare-pipe` | 0.4.0 |
   | `aisquare-pipe-github` | 0.3.0 |
   | `aisquare-pipe-googledrive` | 0.3.0 |
   | `aisquare-pipe-onedrive` | 0.3.0 |
   | `aisquare-pipe-slack` | 0.1.0 |
   | `aisquare-pipe-gmail` | 0.1.0 |
   | `aisquare-pipe-googlecalendar` | 0.1.0 |
   | `aisquare-pipe-graphify` | 0.2.1 |

   If one is missing, fix its publisher and run the workflow again:
   `skip-existing` makes the rerun upload only what is missing.
