# Changelog

All notable changes to Bessemer are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and versions follow
[Semantic Versioning](https://semver.org/) (PEP 440 spelling for the Python
package). Detailed test evidence for each candidate lives in
[production readiness](docs/production-readiness.md) and
[qualification results](docs/qualification-results.md).

## [Unreleased]

### Added

- `app.run(workers=...)` serves through the same process supervisor as
  `bessemer run`: 0 for one worker per usable CPU, or 2 to 256. The default
  stays at 1, serving from the calling process. Workers import the application
  by module and variable name, so the call belongs under
  `if __name__ == "__main__":`; an unguarded call fails with an error that
  says so.

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
- Licensed under MIT OR Apache-2.0, with both licence files shipped in the
  wheel and sdist.
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

### Security

- A response write that makes no progress for as long as the longest request
  timeout now closes its connection, on HTTP/1.1, HTTP/2 and upgraded
  connections. Before, a client that stopped reading held its connection slot
  and its share of the response byte budget for as long as it liked, so a few
  hundred such connections could make a worker refuse everyone else.
- Chunked request bodies with a control character (such as a bare CR) in a
  chunk extension or trailer field are refused with an error instead of the
  extension being ignored, so intermediaries that read a bare CR as a line end
  cannot disagree with Bessemer about where the chunk ends.
- The development and CI pin of `opentelemetry-api` moves to 1.45.1
  (CVE-2026-114603, quadratic `tracestate` parsing). Bessemer itself only
  passes `tracestate` values of up to 512 bytes to the API, so it was not
  exposed through its own propagation.

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
