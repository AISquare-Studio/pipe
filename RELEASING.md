# Releasing

This repo publishes **one** PyPI distribution, `aisquare-pipe`. It carries the
framework and every connector under `connectors/` (D8): each connector is a
top-level module (`aisquare_pipe_<name>`) inside it, and its third-party
dependencies are the `aisquare-pipe[<name>]` extra. No connector is ever its
own PyPI project, so adding one never needs a new project or a new trusted
publisher.

Publishing is automated by [`.github/workflows/publish.yml`](.github/workflows/publish.yml)
using **PyPI Trusted Publishing (OIDC)**: no API tokens are stored anywhere.
`scripts/release_packages.py` makes every decision; the workflow wires its
steps together.

## What a publish run does

1. **plan** reads the version from the root `pyproject.toml` and checks the
   metadata. Every `aisquare-pipe[...]` reference must name an existing extra,
   nothing may depend on an `aisquare-pipe-*` package, every connector must
   have an extra, and `[full]` must pull in every one. On a GitHub Release
   the tag must be `v<version>`. It then looks the version up on PyPI.
2. **build** builds the wheel and sdist and checks them:
   - both carry the planned name and version;
   - the wheel's RECORD holds every module under `src/` and every
     `connectors/<dir>/src/`;
   - `twine check --strict` passes.
3. **publish** uploads, unless that version is already on PyPI: then it is
   skipped, and the run summary says so. The upload doesn't use
   `skip-existing`, so a rejected upload fails the run.
4. **verify** checks each file on PyPI against the sha256 of the file the
   run built.

`plan` refuses to upload anything whose PyPI project doesn't exist yet. This
workflow never creates a project.

## Dry run

Every pull request runs the same workflow as a dry run of everything a
publish could upload: plan, then build and check the distribution and all ten
shims (below), with no upload. To dry-run a branch by hand, use **Actions →
publish → Run workflow**, pick the branch and the target, and leave
**dry-run** ticked (the default). Locally:

```bash
python scripts/release_packages.py plan --pypi            # a PR's plan: the distribution and the shims
python scripts/release_packages.py build --path . --outdir dist
python scripts/release_packages.py check-dist --path . dist
twine check --strict dist/*
```

## Versioning

`aisquare-pipe` has one version: `version` in the root `pyproject.toml`.
Bump it for every release that changes anything, the framework or any
connector. A version already on PyPI is never re-published.

Each connector class still carries a `version` attribute. It is that
connector's own contract version (toolkit cards and the search index key on
it), and it is independent of the distribution's version. `pipe validate`'s
`hygiene.version-sync` checks that all of one connector's classes agree on
it.

## One-time setup

A new connector never needs any of this. Each PyPI project this workflow
uploads to needs a **trusted publisher** with exactly these values (PyPI →
Your projects → the project → Manage → Publishing, or
`https://pypi.org/manage/project/<project>/settings/publishing/`; only a
project Owner sees the page):

| Field on PyPI's "Add a new publisher" → GitHub form | Value |
|---|---|
| Owner | `AISquare-Studio` |
| Repository name | `pipe` |
| Workflow name | `publish.yml` |
| Environment name | `pypi` |

- **`aisquare-pipe`:** in place. Its 0.1.1 files (2026-06-29) carry a
  provenance attestation from this publisher.
- **The ten shim projects** (see "The shims"): seven are proven the same way,
  since their last upload carries an attestation from this publisher:
  `aisquare-pipe-composio`, `-gateway`, `-github`, `-graphify`, `-n8n`,
  `-onedrive` and `-salesforce`. Three are **not**: `aisquare-pipe-docusign`,
  `aisquare-pipe-dropbox` and `aisquare-pipe-local`. Their only release,
  0.1.0, was uploaded by hand on 2026-05-26, before this workflow existed,
  and has no attestation. A green job of theirs in run 28394718052 proves
  nothing: that workflow uploaded with `skip-existing`, which skipped their
  already-published 0.1.0 before PyPI checked any permission. Check them
  before the shim run (step 1 of "The shims").
- **GitHub:** the `pypi` environment (Settings → Environments → `pypi`).
  Recommended, since a Run workflow with **dry-run** unticked otherwise
  publishes from any branch:
  - **Deployment branches and tags** → Selected branches and tags → add the
    branch rules `main` and `release/*` and the tag rule `v*`. A release runs
    on its tag, and a Run workflow on its branch.
  - **Required reviewers:** yourself, so no upload starts until you approve
    it.

  Pull requests never reach the environment: only the two upload jobs use it.

A provenance attestation shows the publisher was registered when that file
was uploaded. The Publishing page is the only check that it still is.

## Cutting a release

1. Bump `version` in the root `pyproject.toml` and update `CHANGELOG.md`.
2. Open a PR and merge it. The PR's publish dry run must be green.
3. **Create a GitHub Release** whose tag is `v<version>`, e.g. `v0.5.0` when
   the root `pyproject.toml` says `0.5.0` (Releases → Draft a new release).
   Its target is the branch that carries that version. Publishing the release
   triggers the workflow.

   A tag that doesn't match the version fails the run before anything is
   built. History: `v0.2.0` (2026-07-10) was cut while the root still said
   `0.1.1`. Its framework job rebuilt 0.1.1, `skip-existing` skipped it, and
   the job went green with nothing uploaded.

Run workflow with **dry-run** unticked publishes the same way without a
release (for a re-run, or to publish from a branch without tagging).

## The shims (once, after 0.5.0)

Before 0.5.0, ten connectors were their own PyPI projects:
`aisquare-pipe-composio`, `-docusign`, `-dropbox`, `-gateway`, `-github`,
`-graphify`, `-local`, `-n8n`, `-onedrive` and `-salesforce`. Each gets one
last release, **0.5.0**. It has no code and one requirement,
`aisquare-pipe[<name>]>=0.5.0`, so `pip install aisquare-pipe-<name>` keeps
working and pulls in the connector from `aisquare-pipe`. The four connectors
that never reached PyPI (googledrive, slack, gmail, googlecalendar) get none.

Publish them with **Run workflow, target `shims`**:

1. **Check the publisher on `aisquare-pipe-docusign`, `aisquare-pipe-dropbox`
   and `aisquare-pipe-local`** (see "One-time setup"). For each, open
   `https://pypi.org/manage/project/<project>/settings/publishing/`.
   - If "Manage current publishers" lists GitHub, `AISquare-Studio/pipe`,
     `publish.yml`, environment `pypi`, there is nothing to do.
   - Otherwise, under "Add a new publisher" → GitHub, enter Owner
     `AISquare-Studio`, Repository name `pipe`, Workflow name `publish.yml`,
     Environment name `pypi`, then **Add**.

   No dry run can see a missing publisher. Without it, step 4 uploads seven
   shims, fails these three with HTTP 403, and `verify` fails. Adding the
   publisher and re-running then uploads only the three: the seven already on
   PyPI are skipped.
2. Only after `aisquare-pipe` 0.5.0 is on PyPI. `plan` fails a real shim run
   until it is, and warns in a dry run.
3. Leave **dry-run** ticked first. The run builds and checks all ten shims:
   each has no code, requires exactly `aisquare-pipe[<name>]>=0.5.0`, and
   passes `twine check --strict`. Every pull request builds and checks them
   too.
4. Then run it again with **dry-run** unticked. `verify` checks the ten
   uploads.

**Upgrading in place** (the user-facing version is README.md, "Upgrading from
`aisquare-pipe-<x>`"). pip removes the old per-connector release's files when
it installs the shim. They are the same paths `aisquare-pipe` 0.5.0 has just
installed, so in an environment that had, say, `aisquare-pipe-onedrive`
0.1.0, `pip install -U aisquare-pipe-onedrive aisquare-pipe` leaves
`aisquare_pipe_onedrive` broken. So does a later `pip uninstall
aisquare-pipe-onedrive` after `aisquare-pipe[onedrive]` was installed over
0.1.0. Uninstall the old package first; if it's too late, run `pip install
--force-reinstall --no-deps aisquare-pipe`, or use a fresh environment.
Discovery logs the breakage as a warning (`Failed to load toolkit onedrive:
No module named 'aisquare_pipe_onedrive.constants'`), while `pip check` sees
nothing wrong. Fresh installs (Docker image builds, CI) are not affected. The
shim's README on PyPI says the same.

## The 0.5.0 release

0.5.0 is the first single-distribution release, and 0.4.0 is skipped: PyPI
goes from `aisquare-pipe` 0.1.1 straight to 0.5.0. Cut it from
**`release/rc-2026-11`**, once `feat/single-distribution` (this packaging) is
merged into it. That branch then carries 0.5.0 whole: the tool router, the
search index, the native toolkits and this packaging. `release/pipe-router`
is already in it, without this packaging, so don't publish from
`release/pipe-router`.

**Never run the workflow on a branch without this packaging.** `main`,
`release/pipe-router` and `release/rc-2026-11` still carry the old
`publish.yml` until this packaging reaches them, and it has no dry run: Run
workflow, or a release, uploads at once. It would upload an `aisquare-pipe`
0.5.0 with no connectors inside, and PyPI never takes a version twice, so
0.5.0 would be used up. Check the branch first: its `publish.yml` must have
the `dry-run` input.

```bash
git fetch origin
git show origin/release/rc-2026-11:.github/workflows/publish.yml | grep -c 'dry-run:'   # 1, not 0
```

Required reviewers on the `pypi` environment (see "One-time setup") also
stop the old workflow: its upload job waits for your approval.

1. Dry-run it: Run workflow on `release/rc-2026-11` with **dry-run**
   ticked. The summary should say `aisquare-pipe` 0.5.0 "would upload", and
   the build job's check should say its RECORD holds every module of the
   checkout.
2. Publish: a GitHub Release tagged `v0.5.0` with target `release/rc-2026-11`.
3. Wait for `verify` to go green: `aisquare-pipe` 0.5.0 is on PyPI.
4. Shims: "The shims" above, publisher check and dry run first.
5. Relock pipe-as-service against `aisquare-pipe[full,toolkits]==0.5.0`. Its
   per-connector requirements must become extras first: `scripts/release_packages.py
   coverage` (below) names each one.

Check a consumer's requirements against what a branch publishes:

```bash
python scripts/release_packages.py coverage --pipe-root . \
    --consumer-root ../pipe-as-service --consumer-ref origin/release/pipe-router
```
