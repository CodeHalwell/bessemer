# Changelog

All notable changes to Bessemer are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and versions follow
[Semantic Versioning](https://semver.org/) (PEP 440 spelling for the Python
package). Detailed test evidence for each candidate lives in
[production readiness](docs/production-readiness.md) and
[qualification results](docs/qualification-results.md).

## [Unreleased]

### Added

- `event_loop="bessemer"` (`bessemer run --event-loop bessemer`,
  `BESSEMER_EVENT_LOOP`, `[tool.bessemer] event_loop`) runs workers on
  Bessemer's own event loop, written in Rust, on Linux. With one loop per
  worker the HTTP connections are served on the loop's thread, cutting CPU per
  request by roughly 12 to 29%. Elsewhere it falls back to asyncio with a
  warning. The default stays asyncio.
- `io_backend="io_uring"` (`bessemer run --io-backend io_uring`,
  `BESSEMER_IO_BACKEND`, `[tool.bessemer] io_backend`) does a worker's socket
  I/O through io_uring instead of epoll when it serves connections on
  Bessemer's loop (`event_loop="bessemer"` with one loop). Off by default and
  Linux only; it needs kernel 6.1 or later and a seccomp profile that allows
  io_uring. Otherwise the worker logs a warning and uses epoll. With several
  workers, connections are spread across them, as on epoll.

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
- Responses other than `HEAD` no longer build a `content-length` header value
  before writing it, since both servers write the length from the body's
  exact size, and canonical integer parameters are parsed without a copy.
  The bytes on the wire are unchanged.
- A request's bound inputs travel to the event loop as a list in parameter
  order that shares the route's parameter names, instead of a JSON map with a
  copied key per input. Behaviour is unchanged.
- The Rust asyncio event loop (`crates/bessemer-loop`) is built into the
  wheel as `bessemer._loop`, and workers can run on it through an internal
  option. It is not a public setting yet. CI runs CPython's asyncio tests
  against it from the built wheel.
- With that internal option and one loop per worker, the HTTP server runs on
  the loop's own thread in a Tokio runtime embedded in the loop, with no
  hand-off between threads per request. CPU per request fell by about 28% at
  one connection and 12% at 64 connections in the comparison benchmarks.

## [1.0.0rc1] - 2026-11-05

Release candidate for 1.0. The public API below is frozen for the 1.x series:
later 1.x releases only add to it, and breaking changes wait for 2.0.

### API stability

- Stable: every name in `bessemer.__all__`; the public, non-underscore
  submodules that mirror FastAPI and Starlette (`bessemer.security`,
  `bessemer.status`, `bessemer.testclient`, `bessemer.staticfiles`,
  `bessemer.encoders`, `bessemer.responses`, `bessemer.websockets` and the
  like); the `bessemer run` and `bessemer routes` CLI flags; `BESSEMER_*`
  environment settings and `[tool.bessemer]` keys; and documented response,
  error and OpenAPI shapes.
- Experimental, outside the guarantee: the Rust event loop
  (`event_loop="bessemer"`) and the io_uring backend
  (`io_backend="io_uring"`). Both are opt-in and off by default. Their setting
  names are stable, but their behaviour and fallbacks may change in 1.x.
- Not covered: `bessemer._native`, `bessemer._loop` and other
  underscore-prefixed modules, worker and supervisor internals, Prometheus
  metric names, access-log fields, and the M0 benchmark binary.

### Added

- FastAPI-compatible API, with a [parity assessment](docs/fastapi-parity.md)
  covering every public FastAPI name. This includes:
  - `FastAPI` (an alias of `App`), `APIRouter`, route names with
    `url_path_for`, and the `response_model_*` options;
  - Starlette path converters and `Body(embed=True)`;
  - security dependencies with `Security()` scopes and `SecurityScopes`;
  - `status`, `jsonable_encoder` and `@app.middleware("http")`;
  - mounted Bessemer and ASGI apps, standard ASGI middleware and lifespan
    state.

  Anything that is not supported fails at startup.
- `TestClient` over the real native server, including WebSocket sessions,
  and `dependency_overrides`.
- Forms and file uploads (`Form`, `File`, `UploadFile`), decoded natively.
  Large multipart uploads spool to disk.
- WebSockets, implemented natively: `WebSocket`, `WebSocketDisconnect` and
  `WebSocketException`. Optional `permessage-deflate` is supported, and
  WebSockets also work over HTTP/2.
- Streaming responses and server-sent events (`StreamingResponse`,
  `EventSourceResponse`).
- HTTP/2 alongside HTTP/1.1, with h2spec conformance checked in CI.
- Native TLS through rustls. This covers mutual TLS, client certificate
  revocation lists, OCSP stapling, and reloading rotated certificates without
  a restart.
- Native gzip and Brotli response compression (`Compression`) and static
  files with conditional, range and precompressed responses.
- Native JWT bearer authentication: `JWTAuth`, with static or
  auto-refreshing remote JWKS, single- and multi-tenant Microsoft Entra ID
  presets, `Principal` and `require_scope`.
- OpenTelemetry tracing (`OpenTelemetry`): W3C Trace Context propagation and
  native OTLP/HTTP span export.
- Rate limits shared across workers and replicas through Redis.
- Several asyncio event loops per worker (`loops`), and supported free-threaded
  CPython 3.14t, where sync handlers scale across cores in one worker.
- Experimental, opt-in: a Rust asyncio event loop
  (`event_loop="bessemer"`, Linux), and io_uring socket I/O on it
  (`io_backend="io_uring"`). io_uring falls back to epoll when the kernel or
  seccomp policy refuses it.
- Tag-driven release workflow: an sdist plus manylinux 2.28 x86_64 wheels for
  CPython 3.12, 3.13 and 3.14t. Each wheel is re-tested as installed, then
  published through PyPI Trusted Publishing with attestations, and a GitHub
  release is created with checksums. Running the workflow by hand is a dry
  run that stops before publishing.
- `scripts/release_version.py`, which fails when any version declaration, the
  tag or the changelog disagree.
- `SECURITY.md` with private vulnerability reporting, release docs, and PyPI
  package metadata.

### Changed

- Much faster request path:
  - Requests are routed natively and started on the event-loop thread from
    a native batched queue.
  - Plain Pydantic models are built from JSON bodies and encoded natively.
  - JSON goes directly between Python objects and bytes through the
    `bessemer-codec` crate.
  - Fixed per-request work and allocations have been cut throughout.

  On a 4-vCPU development VM, one core served `/json` at about 99k req/s by
  default and 116k on the Rust loop. FastAPI on uvicorn served 11k, and Axum
  115k ([comparison](benchmarks/results/2026-11-05/compare-current-main/README.md)).
  These are same-host figures, not a formal benchmark.
- Ordinary HTTP/1.1 requests use a purpose-built connection loop instead of
  hyper's, with the same limits, statuses and keep-alive rules. A request with
  both `Transfer-Encoding` and `Content-Length` is now refused with 400.
- The native extension uses mimalloc as its Rust allocator, and release
  builds use fat LTO with one codegen unit.
- The supervisor promotes a replacement worker only after its control channel
  is confirmed. It also no longer counts a normal exit that races its pipe
  closing as a failure.
- Development status is now Beta. Qualification evidence is no longer shipped
  in the source distribution (it stays in the repository).

### Known limitations

These are tracked in [production readiness](docs/production-readiness.md) and
do not block the 1.0 API freeze:

- Formal performance acceptance (gate 1) needs a dedicated load-generator
  host and has not been run. Every published number is a same-host
  diagnostic.
- Only Linux x86_64 wheels are built and qualified (CPython 3.12, 3.13 and
  3.14t). macOS, Windows and arm64 are not.
- The CLI supervisor is POSIX-only.
- Native I/O clients, an extension ABI, OTLP metrics and logs, gRPC export,
  Redis Cluster/Sentinel and a distributed cache are not implemented.
  [FastAPI parity](docs/fastapi-parity.md) lists the remaining gaps.

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
