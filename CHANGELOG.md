# Changelog

All notable changes to Bessemer are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and versions follow
[Semantic Versioning](https://semver.org/) (PEP 440 spelling for the Python
package). Detailed test evidence for each candidate lives in
[production readiness](docs/production-readiness.md) and
[qualification results](docs/qualification-results.md).

## [Unreleased]

### Added

- `FastAPI`, an alias of `App`, so `from bessemer import FastAPI` ports
  unchanged.
- Starlette path converters in route paths: `{name:path}`, `{name:int}`,
  `{name:float}`, `{name:uuid}` and `{name:str}`. Values that do not match
  are a 404, checked natively; unknown converters and ambiguous routes fail at
  startup.
- `Body(embed=True)` and several body parameters per route, embedded under
  their names in one JSON object as in FastAPI, with a `Body_<operationId>`
  OpenAPI component.
- Standard ASGI middleware through `app.add_middleware` (Starlette's
  `SessionMiddleware` and `AuthenticationMiddleware`, for example), with
  `request.scope`, `request.session`, `request.auth` and `request.user`.
  Apps without ASGI middleware keep the native fast path.
- `app.mount` takes any ASGI app, served under its prefix with Starlette's
  `path`/`root_path` and streamed responses.
- A lifespan may yield a state mapping, which seeds each request's
  `request.state`, as in Starlette. `request.state` is now created on first
  use.

### Changed

- Plain Pydantic body models are built natively from request bodies whose
  values already have their fields' exact JSON types, and returned instances of
  the declared response model are encoded natively. Other bodies and values
  keep Pydantic's path, so results, errors and response bytes are unchanged.
  The 20-item `/echo` comparison route runs about 43% faster on one core.
- Such a body, when it is the route's only body parameter, is parsed straight
  from the request bytes into the model instead of through an intermediate JSON
  value. Invalid JSON gets the same 422 as before. The `/echo` comparison route
  runs about 13% faster again on one core and about 20% on three.
- Less fixed work per request: async routes without inputs or middleware
  skip checks that cannot apply, and the native handler future is no longer
  copied through every enclosing future. The `/json` comparison route runs
  about 9% faster on one core. Behaviour is unchanged.
- A native invoker starts requests on the event loop thread: the context, the
  invocation handle and the eager task for each request are created in Rust,
  and responses finished in one batch are handed back together. Async routes
  that take only scalar path, query, header and cookie inputs, without
  middleware or request metadata, also get their request object, handler call
  and JSON result encoding in Rust, and numeric and length bounds on those
  inputs are checked natively instead of by a second validation in Python.
  CPU per request on one core falls from about 12.8 to 11.5µs on `/json` and
  from 16.0 to 13.4µs on `/items/{id}` (Python thread 3.5 to 2.4µs and 5.5 to
  2.7µs), about 12% and 20% more requests/s. Behaviour is unchanged.
- Less Rust work per request: each connection's application future is boxed
  once instead of being copied (it was tens of kilobytes) on every request,
  the Host and request-target checks no longer allocate, request ids share
  their bytes, and response heads reuse a per-connection buffer. User-space
  instructions per request fall about 4% on `/json` and 10% on a native-only
  route. Behaviour is unchanged.
- Query strings are decoded once into a list of pairs that borrow the request
  target, instead of building two maps per request, and the request timeout's
  future is boxed. A repeated key still binds its last value. User-space
  instructions per request fall a further 2.6% on `/items/{id}`.

## [1.0.0rc1] - 2026-11-04

Release candidate for 1.0. The public API below is frozen for the 1.x series:
later 1.x releases only add to it, and breaking changes wait for 2.0.

### API stability

- Stable: every name in `bessemer.__all__`, the `bessemer run` and
  `bessemer routes` CLI flags, `BESSEMER_*` environment settings and
  `[tool.bessemer]` keys, and documented response, error and OpenAPI shapes.
- Not covered: `bessemer._native`, other underscore-prefixed modules, worker
  and supervisor internals, Prometheus metric names, access-log fields, and
  the M0 benchmark binary.

### Added

- WebSockets, implemented natively: `WebSocket`, `WebSocketDisconnect`, with
  dependency unwinding on disconnect and bounded outbound watermarks.
- Streaming responses and server-sent events: `StreamingResponse` and
  `EventSourceResponse`, with cleanup on cancellation and close.
- Native JWT bearer authentication: `JWTAuth` with static or remote,
  auto-refreshing JWKS, a single-tenant Microsoft Entra ID preset
  (`JWTAuth.entra`), `Principal` and `require_scope`.
- Tag-driven release workflow: sdist plus manylinux 2.28 x86_64 wheels for
  CPython 3.12 and 3.13, each re-tested as installed, published through PyPI
  Trusted Publishing with attestations and a GitHub release with checksums.
- `scripts/release_version.py`, which fails when any version declaration,
  the tag or the changelog disagree.
- `SECURITY.md` with private vulnerability reporting, and release docs.
- Package metadata for PyPI (readme, project links).

### Changed

- Faster request path. Native Tokio threads reuse one CPython thread state,
  requests reach the asyncio worker through a native batched queue without
  taking the GIL on Tokio threads, and handlers start eagerly. JSON is encoded
  and decoded directly between Python objects and bytes by the new
  `bessemer-codec` crate, with output unchanged.
- Ordinary HTTP/1.1 requests use a purpose-built connection loop instead of
  hyper's, with the same limits, statuses and keep-alive rules. A request with
  both `Transfer-Encoding` and `Content-Length` is now refused with 400.
- The native extension uses mimalloc as its Rust allocator, and release
  builds use fat LTO with one codegen unit.
- Development status is now Beta. Qualification evidence is no longer shipped
  in the source distribution (it stays in the repository).

### Known limitations

These are tracked in [production readiness](docs/production-readiness.md) and
are not blockers for the 1.0 API freeze:

- Formal performance acceptance (gate 1) needs a dedicated load-generator
  host and has not been run. Same-host diagnostics (one-core `/json` about
  56k/s) are recorded in the readiness doc but are not acceptance results.
- Python routes are still classed Tier 2: the batched handoff is in, but the
  Tier 1 classification needs gate 1's measurements. Free-threaded CPython and
  formal multicore scaling evidence are post-1.0 work, as are multi-tenant Entra ID, distributed rate limiting and caching,
  compression and static files, native I/O clients and OpenTelemetry.
- Only Linux x86_64 with CPython 3.12 and 3.13 is built and qualified.

## [0.2.0a3] - 2026-11-04

First supervised application runtime. Python application routes run on the
Tier 2 bridge; the Tier 1 fast path and formal performance acceptance are open.

### Added

- Installable Rust/Python wheel with validated configuration and a supervised
  `bessemer` CLI (`run`, `routes`), resolving CLI flags, `BESSEMER_*`
  environment variables and `[tool.bessemer]` settings.
- `App`/`Router` registration with startup route and signature compilation:
  typed path, query, header, cookie and body inputs with constraints,
  dependency DAGs with per-request caching, app-scoped resources, sync, async
  and yield dependencies, and lifespan.
- JSON, text, HTML and redirect responses, repeated cookies, response models,
  filtered serialization, exception handlers, `Request`, state and
  request-scoped background tasks. Optional Pydantic 2.10.6+ validation.
- OpenAPI 3.1 generated once at startup, served natively, with optional docs.
- Native routing, JSON parsing and serialization, body and response limits,
  per-worker aggregate payload budgets, admission control, absolute processing
  deadlines and cancellation of queued sync work.
- Bounded HTTP/1 transport: connection, header-read, header count and buffer,
  and request-target limits with strict Host and framing checks.
- Re-exec worker supervisor sharing one listener, with readiness and event-loop
  heartbeats, cgroup CPU sizing, crash backoff, rolling reload, request,
  lifetime and RSS recycling, and TERM/drain/KILL shutdown with Linux
  process-group cleanup.
- Native CORS, environment-sourced API keys, allowed hosts, trusted proxy
  client IPs, rate limits, request IDs, security headers, protected Prometheus
  metrics and redacted access logs.
- Container image on pinned Debian Trixie with verified Tini as PID 1,
  qualified at 512 MiB and 256 MiB, plus an authenticated production example.
- Qualification tooling: installed-wheel HTTP and process-failure tests,
  seeded generated-input campaigns, extended soak and security scans.

### Known limitations

- Formal M0 performance acceptance is not met; local measurements were
  load-generator constrained and outside the 3% spread ceiling.
- The supervisor is POSIX-only; only Linux x86_64 with CPython 3.12 and 3.13
  is qualified.
- JWT/JWKS, distributed rate limiting, compression, streaming, WebSockets and
  OpenTelemetry are not implemented.

## [0.1.0] - 2026-11-02

### Added

- M0 performance spike comparing an Axum JSON handler with a Rust-driven
  Python async handler, plus the paired benchmark harness.
