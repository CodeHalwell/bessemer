# Bessemer working context

Read `docs/production-readiness.md` and `README.md` before continuing implementation.
The original user-supplied requirements are in `docs/technical-specification.md`.
The November 2026 recovery and production-foundation work is recorded in Git;
do not depend on a previous conversation being available.

The current runtime is a 1.0 release candidate compatibility implementation with a POSIX
process supervisor and a qualified Linux worker-lifecycle path.
The supplied container uses Tini above Bessemer so adopted application
descendants are reaped; the Bessemer supervisor owns its tracked worker leaders.
Keep init-aware process-role and orphan-reaping checks in container qualification.
Keep native health at Tier 0 and label Python bridge routes Tier 2 until the
specified fast path and measurements support a different classification.
Preserve the M0 binary and benchmark harness. The formal M0 performance gate
remains open; local smoke tests do not establish performance acceptance.

Build/install the wheel before running integration tests, then run
`BESSEMER_REQUIRE_NATIVE=1 ./scripts/check.sh`. Tests must exercise the installed
package as well as source checks. Keep cancellation cleanup within the request
capacity accounting, and do not block Tokio threads on Python future results.

Record implemented scope, remaining gaps and actual validation in the readiness
document as work progresses. Keep incomplete features explicit rather than
silently accepting unsupported configuration or API arguments.

The 1.x public API is frozen: names in `bessemer.__all__`, CLI flags,
`BESSEMER_*` settings and `[tool.bessemer]` keys. Only add to it in 1.x;
`tests/python/test_release_version.py` pins `__all__`. Bump versions with
`scripts/release_version.py` passing and follow `docs/releasing.md`.
