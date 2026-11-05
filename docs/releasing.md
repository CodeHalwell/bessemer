# Releasing

Releases are cut from `main` by pushing a version tag. The
[release workflow](../.github/workflows/release.yml) then builds, re-tests and
publishes the exact artifacts it tested.

## One-time setup

1. **Licence.** Add a licence file and the `license` field in
   `pyproject.toml` before the first upload. PyPI shows the package as having
   no licence otherwise.
2. **PyPI account.** Create an account at <https://pypi.org/account/register/>
   and turn on two-factor authentication, which PyPI requires before
   publishing.
3. **PyPI Trusted Publisher.** At
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
4. **GitHub `pypi` environment.** Under Settings, Environments, create an
   environment named `pypi`. Add yourself as a required reviewer and limit
   deployment to tags matching `v*`. A tag push then waits for your approval
   before anything is uploaded.
5. **Private vulnerability reporting.** Turn it on under Settings, Security,
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
which only run for a pushed tag.

## Cutting a release

1. Update the version in every declaration. `scripts/release_version.py`
   lists them and fails when they disagree; the test suite runs it.
   Python uses PEP 440 (`1.0.0rc1`), Cargo the SemVer spelling (`1.0.0-rc.1`).
2. Add a `## [X.Y.Z] - YYYY-MM-DD` section to [CHANGELOG.md](../CHANGELOG.md).
   Its body becomes the GitHub release notes.
3. Update [production readiness](production-readiness.md) with the candidate's
   evidence and merge. Confirm the CI workflows are green on that commit, and
   run the dry run on `main`.
4. Tag that exact commit and push the tag:

   ```sh
   git tag -a v1.0.0 -m "Bessemer 1.0.0"
   git push origin v1.0.0
   ```

On a tag, the workflow:
1. Checks the tag against the package version.
2. Builds an sdist and manylinux 2.28 x86_64 wheels for CPython 3.12, 3.13
   and free-threaded 3.14t.
3. Installs each wheel and runs the full check suite against it.
4. Waits for approval in the `pypi` environment.
5. Uploads to PyPI with attestations.
6. Creates the GitHub release with the artifacts, `SHA256SUMS` and the
   changelog section.

Versions with an `a`, `b` or `rc` suffix are marked as pre-releases.

Only Linux x86_64 is built, because it is the only qualified platform. Add
other targets to the matrix once they have their own qualification evidence.
