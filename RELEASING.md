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

Every pull request runs the same workflow as a dry run: plan, build and check,
with no upload. To dry-run a branch by hand, use **Actions → publish → Run
workflow**, pick the branch, and leave **dry-run** ticked (the default).
Locally:

```bash
python scripts/release_packages.py plan --pypi            # what a publish would upload
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

Done already, and never needed again for a new connector:

- **PyPI:** a trusted publisher on the `aisquare-pipe` project with Owner
  `AISquare-Studio`, Repository `pipe`, Workflow `publish.yml`, Environment
  `pypi`. The same publisher already covers the ten per-connector projects
  that get a shim (see below). Each one has passed PyPI's upload-permission
  check in a run of this workflow: on 2026-06-29 (run 28394718052) for the
  older eight, and on 2026-07-10 for github and graphify, which this workflow
  created.
- **GitHub:** the `pypi` environment (Settings → Environments). Optionally,
  add required reviewers. If you restrict its deployment refs, allow the
  release branch you publish from and tags `v*`.

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

1. Only after `aisquare-pipe` 0.5.0 is on PyPI. `plan` fails a real shim run
   until it is, and warns in a dry run.
2. Leave **dry-run** ticked first. The run builds and checks all ten shims:
   each has no code, requires exactly `aisquare-pipe[<name>]>=0.5.0`, and
   passes `twine check --strict`.
3. Then run it again with **dry-run** unticked. Each project already exists,
   with this workflow as its publisher, so no registration is needed. `verify`
   checks the ten uploads.

**Upgrading in place.** pip removes the old per-connector release's files
when it installs the shim. They are the same paths `aisquare-pipe` 0.5.0 has
just installed, so in an environment that had, say, `aisquare-pipe-onedrive`
0.1.0, `pip install -U aisquare-pipe-onedrive aisquare-pipe` leaves
`aisquare_pipe_onedrive` broken. Afterwards, run
`pip install --force-reinstall --no-deps aisquare-pipe`, or use a fresh
environment. Fresh installs (Docker image builds, CI) are not affected. The
shim's README on PyPI says the same.

## The 0.5.0 release

0.5.0 is the first single-distribution release, and 0.4.0 is skipped: PyPI
goes from `aisquare-pipe` 0.1.1 straight to 0.5.0. It is cut from
`release/pipe-router`, which carries 0.5.0 (the tool router, the search
index, the native toolkits and this packaging).

1. Dry-run it: Run workflow on `release/pipe-router` with **dry-run**
   ticked. The summary should say `aisquare-pipe` 0.5.0 "would upload".
2. Publish: a GitHub Release tagged `v0.5.0` with target `release/pipe-router`.
3. Wait for `verify` to go green: `aisquare-pipe` 0.5.0 is on PyPI.
4. Shims: "The shims" above, dry run first.
5. Relock pipe-as-service against `aisquare-pipe[full,toolkits]==0.5.0`. Its
   per-connector requirements must become extras first: `scripts/release_packages.py
   coverage` (below) names each one.

Check a consumer's requirements against what a branch publishes:

```bash
python scripts/release_packages.py coverage --pipe-root . \
    --consumer-root ../pipe-as-service --consumer-ref origin/release/pipe-router
```
