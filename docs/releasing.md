# Releasing

Releases are cut from `main` by merging a version bump. When a push to `main`
carries a package version that has no `v` tag yet, the
[release workflow](../.github/workflows/release.yml) builds, re-tests and
publishes the exact artifacts it tested, then creates the tag and the GitHub
release on that commit. Pushing a version tag by hand still works.

## One-time setup

1. **Two-factor authentication** on the PyPI account, which PyPI requires
   before publishing.
2. **PyPI Trusted Publisher.** At
   <https://pypi.org/manage/account/publishing/>, add a *pending* publisher.
   A pending publisher is needed because the project does not exist yet:
   - PyPI project name: `bessemer`
   - Owner: `CodeHalwell`
   - Repository: `bessemer`
   - Workflow: `release.yml`
   - Environment: `pypi`

   No API token is needed. The workflow authenticates with a short-lived
   OIDC token, and PyPI turns the pending publisher into a normal one on the
   first upload.
3. **GitHub `pypi` environment.** Under Settings, Environments, create an
   environment named `pypi`. Add yourself as a required reviewer and limit
   deployment to the `main` branch and to tags matching `v*`. Both are needed:
   an automatic release runs on `main`, a hand-pushed tag runs on the tag.
   Either one then waits for your approval before anything is uploaded.
4. **Private vulnerability reporting.** Turn it on under Settings, Security,
   so the process in [SECURITY.md](../SECURITY.md) works.

crates.io is not used. Every Rust crate in this repository is an internal part
of the Python extension (`publish = false`), so there is no public Rust API to
publish. Publishing one would commit its Rust interface to SemVer separately
from the Python package. If you want to reserve the crate names, a crates.io
account with a scoped API token is enough. It is not part of this workflow.

## Dry run

Run the workflow by hand from the Actions tab (Release, Run workflow) on any
branch. It also runs this way on pull requests that change the workflow,
`pyproject.toml`, the Cargo manifests or `scripts/release_version.py`.

A dry run checks the version declarations, builds the sdist, then builds and
imports it from source in a clean environment. It builds every wheel and runs
the full suite against each. It stops before the PyPI and GitHub release jobs,
which only run for a new version on `main` or a pushed tag.

A push to `main` whose version is already tagged stops after the version check,
so ordinary merges build nothing here.

## Cutting a release

1. Update the version in every declaration. `scripts/release_version.py`
   lists them and fails when they disagree; the test suite runs it.
   Python uses PEP 440 (`1.0.0rc1`), Cargo the SemVer spelling (`1.0.0-rc.1`).
2. Add a `## [X.Y.Z] - YYYY-MM-DD` section to [CHANGELOG.md](../CHANGELOG.md).
   Its body becomes the GitHub release notes.
3. Update [production readiness](production-readiness.md) with the candidate's
   evidence. The pull request's release dry run must pass.
4. Merge. The push to `main` starts the release; approve the `pypi`
   deployment when it asks.

On a new version, the workflow:
1. Checks every version declaration agrees (and, for a hand-pushed tag, that
   the tag matches) and that the tag does not exist yet.
2. Builds an sdist and manylinux 2.28 x86_64 wheels for CPython 3.12, 3.13
   and free-threaded 3.14t.
3. Installs each wheel and runs the full check suite against it.
4. Waits for approval in the `pypi` environment.
5. Uploads to PyPI with attestations.
6. Creates the `vX.Y.Z` tag on the tested commit and the GitHub release with
   the artifacts, `SHA256SUMS` and the changelog section. The tag is created
   with the workflow's own token, so it does not start a second release run.

If the GitHub release step fails after the PyPI upload, use "Re-run failed
jobs" on that run: it reuses the same commit and the artifacts already on
PyPI. Do not push again to retry; a new run for an already uploaded version
fails at the PyPI step rather than tagging a different commit.

There is no tag to push. A hand-pushed tag is only needed to release a commit
that is not the tip of `main`; push it before the version reaches `main`, and
the `main` run then finds the tag and builds nothing.

Versions with an `a`, `b` or `rc` suffix are marked as pre-releases.

Wheels are built for Linux x86_64 (manylinux 2.28; CPython 3.12, 3.13 and
3.14t), macOS arm64 and x86_64, and Windows x86_64 (CPython 3.12 and 3.13).
Each wheel is installed and tested on its own platform before anything is
published: the Linux wheels with `scripts/check.sh`, the macOS and Windows
wheels with the Python test suite. Linux is the only platform with formal
qualification evidence.
