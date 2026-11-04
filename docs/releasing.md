# Releasing

Releases are cut from `main` by pushing a version tag. The
[release workflow](../.github/workflows/release.yml) then builds, re-tests and
publishes the exact artifacts it tested.

## One-time setup

1. On PyPI, add a Trusted Publisher for project `bessemer`: owner
   `CodeHalwell`, repository `bessemer`, workflow `release.yml`, environment
   `pypi`. For the first upload use a *pending* publisher, since the project
   does not exist yet.
2. In the GitHub repository settings, create the `pypi` environment and add
   yourself as a required reviewer, so a tag push waits for approval before
   anything is uploaded.
3. Enable private vulnerability reporting (Security, Advisories) so the
   process in [SECURITY.md](../SECURITY.md) works.

## Cutting a release

1. Update the version in every declaration. `scripts/release_version.py`
   lists them and fails when they disagree; the test suite runs it.
   Python uses PEP 440 (`1.0.0rc1`), Cargo the SemVer spelling (`1.0.0-rc.1`).
2. Add a `## [X.Y.Z] - YYYY-MM-DD` section to [CHANGELOG.md](../CHANGELOG.md).
   Its body becomes the GitHub release notes.
3. Update [production readiness](production-readiness.md) with the candidate's
   evidence, merge, and confirm both CI workflows are green on that commit.
4. Tag that exact commit and push the tag:

   ```sh
   git tag -a v1.0.0 -m "Bessemer 1.0.0"
   git push origin v1.0.0
   ```

The workflow checks the tag against the package version, builds an sdist and
manylinux 2.28 x86_64 wheels for CPython 3.12 and 3.13, installs each wheel and
runs the full check suite against it, then waits for `pypi` environment
approval. After upload (with attestations) it creates the GitHub release with
the artifacts, `SHA256SUMS` and the changelog section. Versions with an `a`,
`b` or `rc` suffix are marked as pre-releases.

Only Linux x86_64 is built because it is the only qualified platform. Add other
targets to the matrix once they have their own qualification evidence.
