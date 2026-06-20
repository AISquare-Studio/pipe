# Releasing

This repo is a monorepo of independently published PyPI packages:

| Package | Path |
|---|---|
| `aisquare-pipe` | `.` |
| `aisquare-pipe-local` | `connectors/local` |
| `aisquare-pipe-dropbox` | `connectors/dropbox` |
| `aisquare-pipe-onedrive` | `connectors/onedrive` |
| `aisquare-pipe-salesforce` | `connectors/salesforce` |
| `aisquare-pipe-docusign` | `connectors/docusign` |
| `aisquare-pipe-composio` | `connectors/composio` |
| `aisquare-pipe-gateway` | `connectors/gateway` |
| `aisquare-pipe-n8n` | `connectors/n8n` |

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
(`workflow_dispatch`). This is the way to do the first publish of the current
`0.1.x` / `0.2.x` versions once the trusted publishers above are registered.
