# Production readiness checkpoint — 4 November 2026

This is a new implementation checkpoint based on the surviving M0 code and the
original specification. Work recalled from 3 November was not recovered. This
document records the current implementation and the next gates; it does not
claim the framework is ready for general production use.

## Implemented first slice

- Installable Python/Rust wheel and `bessemer run module:app` CLI.
- `App`, `Router`, HTTP method decorators, router prefixes and native health routes.
- Startup compilation and rejection of unsupported signatures and ambiguous routes.
- Scalar path/query arguments (`str`, signed 64-bit `int`, finite `float`, `bool`),
  defaults, JSON object/array bodies and optional Pydantic v2 body validation.
- Rust HTTP routing and JSON conversion, including invalid-return protection.
- A persistent asyncio worker with lifespan startup/shutdown on the same loop.
- Async handlers with actual suspension; sync handlers use eight executor threads.
- Configurable body limit, in-flight limit, route/global timeouts and graceful drain.
- 413 on body overflow (including chunked input), 422 on invalid inputs, 503 with
  `Retry-After` at capacity and 504 on a handler deadline. Internal errors return
  generic 500 JSON responses. HEAD and bodyless statuses do not send a body.
- Installed-wheel HTTP tests, CI wheel artifacts, and a non-root example container.

Python routes currently use the compatibility bridge and are reported as Tier 2.
Native health routes are Tier 0. No Tier 1 throughput claim is made for this bridge.
The coroutine callback design avoids waiting on Python futures on Tokio reactor
threads, but the handoff is not yet the M1 batching design in the specification.

## Operational behavior

The initial server is a single Python asyncio worker with Rust networking. Run
replicas under a container or process supervisor. It does not yet supervise,
respawn or recycle its own processes or provide a free-threaded worker topology.

The in-flight limit bounds admitted Python requests, including requests whose
timeout response has already been sent but whose Python work is still cleaning
up. Native health checks remain available when Python request capacity is full.

Async timeout cancellation waits for actual coroutine completion before releasing
capacity. Python cannot forcibly interrupt a running sync handler. Its capacity
slot remains held until its thread returns. Blocking the asyncio loop itself can
also prevent cooperative cancellation; offload blocking work to sync handlers.

SIGINT/SIGTERM stops acceptance and drains connections. `graceful_timeout` is a
drain/cleanup interval, not a guaranteed maximum process lifetime: the native
drain and Python cleanup are separate phases, and non-cooperative thread work
requires an external supervisor's hard termination deadline.

Use a trusted ingress for TLS, header/connection read timeouts and deployment
access controls. This slice does not implement proxy-header trust, auth, CORS,
rate limiting, response caching, compression, OpenAPI or distributed telemetry.
`app.health` reports process health; add application-specific readiness checks
before exposing a real service. No stateful production service has been deployed.

Pydantic integration uses the worker's compatibility path. Pydantic is optional
and must be installed by the application when its models are used. JSON scalar
arguments and model support are a deliberately limited initial surface; this
slice is not yet a FastAPI compatibility implementation. Unsupported signatures
fail at startup. No raw Request, dependency injection or response headers API is
provided yet.

## Verified at this checkpoint

On CPython 3.12, the installed wheel passed 5 Rust tests, 22 Python API/worker/CLI
tests (including real Pydantic v2 validation), 6 real HTTP/lifecycle tests and
9 benchmark-harness tests: 42 tests in total. Rust formatting, Clippy with
warnings denied, Ruff checks/formatting and shell syntax checks also passed.

The pinned Docker build passed, and its example container served native health,
an awaited Python route and JSON creation, rejected an invalid typed path,
became healthy as UID 999, and exited cleanly on SIGTERM. CI is configured to
build and test installed Linux wheels on CPython 3.12 and 3.13; remote CI results
are tracked on the pull request separately from these local checks.

## Remaining gates

1. **Measure M0 formally.** Preserve the original stop criterion below 40% of
   Axum and the 60% Tier 1 target. Use separate server/load machines, a dedicated
   pinned server core, 30-second warmups, five alternating 60-second runs and
   at most 3% sample spread. Fixed-rate latency and free-threaded scaling remain
   unmeasured. Unit tests and local smoke runs do not satisfy this gate.
2. **Complete the framework request path.** Extract the Rust core from bindings,
   implement the specified coroutine batching/native fast path, typed validation,
   dependencies, response models, OpenAPI and compatibility coverage.
3. **Complete worker operation.** Supervision, CPU-quota sizing, process/thread
   topology, hard shutdown guarantees, crash recovery and reload behavior.
4. **Complete production controls.** Auth/proxy trust, native pipeline features,
   telemetry, protocol/FFI/concurrency testing, soak tests, security tooling and
   supported-platform wheel verification.
5. **Qualify a release.** Run the full benchmark suite and deployment/load/failure
   tests on representative infrastructure before calling it production ready.

The plain-text specification includes placeholders for architecture, lifecycle
and roadmap diagrams. Those diagrams were not recovered; the text requirements
are preserved in `technical-specification.md`.

## Continuing the work

Start with this document, `README.md`, the specification and Git history. Update
this checkpoint with implemented changes and actual validation results as the
build progresses. Store code and decisions in the repository rather than only
in chat. Existing M0 commands remain available as `bessemer-m0`.
