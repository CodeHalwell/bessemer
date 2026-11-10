# Changelog

All notable changes to Bessemer are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and versions follow
[Semantic Versioning](https://semver.org/) (PEP 440 spelling for the Python
package). Detailed test evidence for each candidate lives in
[production readiness](docs/production-readiness.md) and
[qualification results](docs/qualification-results.md).

## [Unreleased]

Fixes from a whole-project review of 1.0.0rc3, and a request path that does
less work per request. The 1.x API freeze holds; `App(strict_content_type=)`
is an addition. The changes to default behaviour are listed first.

### Changed

- A JSON body must say that it is JSON. A request body sent with no
  `Content-Type` to a route with a body parameter is refused with `415`, as
  one with the wrong type already was, instead of being read as JSON. A
  browser can post such a body to another site without a CORS preflight,
  and FastAPI refuses it by default for the same reason. Pass
  `App(strict_content_type=False)` to keep reading it. Routes that read
  `await request.body()` themselves are not affected.
- Cookies never refuse a request. A pair that is not well-formed RFC 6265 is
  left out and the first cookie with a name wins; before, one malformed or
  repeated cookie answered `400` on every route that reads cookies or takes
  a `Request`, and on every WebSocket handshake. A browser sends every
  cookie in scope, including those other applications set on the domain.
- Rate limits count an IPv6 client by its /64 rather than its full address.
- A WebSocket session keeps at most about `max_body_size` bytes of received
  messages, plus one message, waiting for its handler (and at most 64
  messages, as before); it then stops reading the socket until the handler
  consumes.
- A static mount serves at most `max_connections` responses at once per
  worker and answers `503` with `Retry-After` beyond that. HTTP/1.1 clients
  cannot reach the bound; it stops HTTP/2 streams from each holding a file
  open.
- A request that declares a `Content-Length` over `max_body_size` is refused
  with `413` at once, before `100 Continue`, instead of when the excess
  arrives.
- During shutdown new connections are refused immediately instead of
  waiting in the listen backlog until the drain ends.
- A request holds its `max_in_flight` slot for its work, not while the
  server waits for its body. A request that finds no slot free is still
  refused at once with `503`, before any of its body is read. One that is
  admitted gives the slot up while its body is outstanding and takes one
  again when the body is in, so it can now be answered `503` after sending
  its body if every slot has been taken meanwhile. A spooled upload does the
  same each time it waits for its client, but waits its turn for a slot
  rather than being refused part way. Before, a client that declared a body
  and sent none held a slot until the request timeout, and a handful of them
  made every Python route answer `503`.
- A client must take a response it is behind on at a minimum rate, 240
  bytes a second by default (`min_send_rate`, below). The send deadline was
  only an idle timeout: a client that took a little, just inside each
  timeout, could hold a response and its share of the response byte budget
  for as long as it liked. Now the time response writes spend blocked may
  not exceed the longest request timeout plus the time the bytes taken
  would need at that rate; past it the connection is closed. On HTTP/2 the
  same holds for each stream. Time the application spends producing a
  response is not counted. A consumer that deliberately reads a stream more
  slowly than that for longer than the timeout is now disconnected; set the
  rate to `0` for the old behaviour.
- A static or `FileResponse` file small enough to be compressed whole (up to
  1 MiB) is charged to the response byte budget while it is in memory, as
  other buffered responses are. When the budget cannot take it, it is
  compressed a chunk at a time and sent with chunked framing, as larger
  files already were, instead of being refused. Before, each such response
  held up to 1 MiB that no budget counted.
- Generated `X-Request-ID` values start with a prefix chosen at random per
  worker instead of the worker's start time and process id.
- A `SIGHUP` received during a rolling reload starts one more cycle when the
  current one ends instead of being dropped.
- `HTTPException` and request-validation errors keep their own responses
  when the application registers a handler for a base class such as
  `Exception` or `ValueError`: the 404 stays a 404 and an invalid body stays
  a 422, as in FastAPI. A handler for the exception's own class or status
  code applies as before, and a handler for `Exception` (or status 500)
  remains the catch-all for everything else.
- An exception that no handler takes is raised from `call_next` in
  `@app.middleware("http")`, and into ASGI middleware, after the route's
  dependencies have been torn down, as in Starlette; it used to arrive as a
  500 response. Code after `await call_next(request)` therefore no longer
  runs for such an error unless it is in `finally`, and a middleware that
  commits after `call_next` no longer commits a failed request. If no
  middleware answers it, the catch-all handler or the built-in 500 does.
- Error logging. A handled exception no longer writes an ERROR line (it
  wrote "Unhandled ..." even when a handler answered). An unhandled one
  writes the same line followed by where it was raised: each frame's file,
  line and function and the exception class names, including chained
  causes. Exception messages and source lines are still never logged.
- `Depends(obj.method)` written in several places is one dependency: it
  runs once per request and makes one `scope="app"` resource, as in
  FastAPI. Each occurrence used to be a separate dependency because a
  bound method is a new object every time it is read.
- `include_router()` refuses an `App` that carries exception handlers,
  middleware, mounted apps, dependency overrides or server options, which
  it used to drop silently, and points to `mount()`. An `App` with only
  routes and a lifespan is still included like a router.
- Assigning `JSONResponse.body` raises `AttributeError` (it was ignored),
  and `TestClient.cookies.set()` rejects a `domain` or `path` it cannot
  honour and unknown keywords (they were swallowed).
- `request.scope` no longer contains the internal `"bessemer.call"` entry
  once a request under ASGI middleware has finished.

- Requests cost less. Under Callgrind, against 1.0.0rc3 and including the
  fixes below, a request takes 8.5% fewer user-space instructions on `/json`
  and 7.7% fewer on `/items/{id}` with the default Rust event loop (32,505
  to 29,751 and 35,566 to 32,830), about 5% fewer with asyncio's loop, and
  about 5% fewer for a 20-item Pydantic body on either. Nothing changes on
  the wire. The savings come from resolving common header names in any
  letter case (clients and proxies send them Title-Case), remembering a
  connection's validated `Host`, borrowing rather than moving the request
  through dispatch, one stop-signal wait per connection, smaller queued
  jobs, and matching a model's next expected key in place. These are
  instruction counts, not throughput; the formal performance gate is still
  open.

### Added

- `App(strict_content_type=True)`.
- `min_send_rate` (`--min-send-rate`, `BESSEMER_MIN_SEND_RATE`,
  `[tool.bessemer] min_send_rate`, `App.run(min_send_rate=)`): the bytes a
  second a client must take of a response it is behind on, once it has been
  behind for the longest request timeout. Default 240; `0` sets no floor.
- Linux x86_64 wheels for the regular CPython 3.14 build, tested with the
  same suite as the others.
- `py.typed`: the package's annotations are visible to type checkers.
- A warning at startup for a `BESSEMER_*` environment variable that sets
  nothing but is a slip from a setting (`BESSEMER_WORKER`), naming the
  setting it resembles.

### Fixed

- **Unbounded memory growth on CPython 3.12 and free-threaded 3.14.** Object
  keys parsed by `await request.json()` and `WebSocket.receive_json()` were
  interned, and interned strings are immortal on those interpreters, so a
  client sending fresh keys grew the worker for good (about 3.75 MiB per
  659 KiB request on 3.12). Keys are no longer interned.
- **A worker exited when it ran out of file descriptors.** Any `accept`
  error ended the server loop and dropped the requests in flight. The
  worker now keeps serving, retries with a growing pause and logs once.
- **An error in a lifespan's teardown stopped the whole service.** Exit
  status 78 was treated as a permanent configuration failure even from a
  worker that had been serving, so the first routine recycle of a worker
  whose teardown raised `AttributeError`, `TypeError`, `ValueError` or
  `ImportError` shut every worker down.
- **An open redirect on a static mount at `/`.** `GET //evil.example/%ff`
  was answered `307 Location: //evil.example/%ff/`. An undecodable path is
  now `404`, and no redirect is sent for a path a browser would read as
  protocol-relative (`//host` or `/\host`).
- A replacement worker that could not be spawned, or a killed worker that
  was slow to reap, no longer shuts the other workers down.
- `http.HTTPStatus` and other `IntEnum` status codes are accepted by
  `HTTPException`, the response classes, `RedirectResponse`, route
  `status_code=` and `exception_handler()`; they raised and so answered 500.
- A response built from another response's headers keeps repeated fields:
  `Response(headers=other.headers)` kept one of several `Set-Cookie`
  values, and so did an `HTTPException` with two `WWW-Authenticate` headers
  that went through the default handler.
- A `JSONResponse` subclass that overrides `render()` has its override
  called, and `JSONResponse(...).body` returns the encoded bytes rather
  than `b""`. (`render()` on other response classes is still not called.)
- An exception handler that returns a `StreamingResponse` has it delivered;
  it answered 500.
- A request under ASGI middleware is freed when it ends instead of waiting
  for the cycle collector.
- Windows: `bessemer run` from a virtual environment starts workers on the
  real interpreter rather than through the environment's launcher, whose
  child was not the supervisor's child. Not yet confirmed on Windows; the
  portability workflow now runs the Windows supervisor tests from a
  virtual environment.
- HTTP/1: a request refused for conflicting framing always gets its `400`
  (it was closed silently when its body contained two spaces); an HTTP/1.0
  keep-alive request for a body of unknown length is no longer told
  `connection: keep-alive` on a response that ends by closing; a streamed
  body that fails is reported to after-response work as not delivered.
- One unusable key in a fetched JWKS no longer rejects the whole set and
  stalls key rotation.
- The access-log writer no longer stops for good after one failed write.
- On the Rust event loop, `sock_recv` with an enormous size returns what is
  available instead of aborting the process, and a socket left in blocking
  mode no longer blocks the loop.
- Source installs and CI now use the same maturin (1.16.0).

## [1.0.0rc3] - 2026-11-09

Third release candidate for 1.0. The 1.x API freeze still holds. One default
changes: the size of the sync handler pool on Linux and macOS.

### Changed

- With `sync_workers` left at its default (`0`), a GIL build of CPython on
  Linux or macOS now starts up to eight sync handler threads as before and can
  grow the pool to sixteen. A thread is added only after four or more jobs have
  been queued for 10 ms and the process used less than 0.75 of a CPU core over
  the last 20 ms, at most one thread per 20 ms; extra threads stay parked until
  shutdown. Blocking handlers gain the most: a 1 ms blocking handler on one
  Linux worker now serves an offered 10,000 requests/s (p99 9 to 94 ms)
  instead of about 7,000/s (p99 about 1.15 s). The cost is about 8 to 10%
  more process CPU per request on that workload. CPU-bound `def` handlers do
  not grow the pool on an idle host and were within noise of the fixed pool
  (at most one extra thread and about 2% fewer requests under host CPU
  contention). Set `sync_workers=8` (`--sync-workers 8`,
  `BESSEMER_SYNC_WORKERS=8`) to keep the previous fixed pool. Windows,
  free-threaded builds and any explicit `sync_workers` value are unchanged.
- An HTTP/1 connection's read buffer starts at 2 KiB instead of 8 KiB and
  reuses its consumed space before growing, which cut idle keep-alive memory
  by about 31% per connection (18.3 to 12.5 KiB) in the PR #112 measurement.
  Large heads and bodies still grow the buffer in 8 KiB steps.
- Releases are cut by merging a version bump: a push to `main` whose package
  version has no tag yet builds, tests and (after approval) publishes it, then
  tags the commit and creates the GitHub release. See `docs/releasing.md`.

### Fixed

- The Callgrind CPU-clock probe in the performance workflow no longer writes
  a profile into the uploaded results artifact.
- The adaptive pool growth test allows five seconds instead of two, so a busy
  CI runner does not fail it.

## [1.0.0rc2] - 2026-11-09

Second release candidate for 1.0. The 1.x API freeze from 1.0.0rc1 still
holds; this candidate only adds to it. The default-behaviour changes below
are listed first because they can change what an existing app sees.

### Changed

- `max_in_flight` defaults to `"auto"` (`App.run`, `bessemer run
  --max-in-flight`, `BESSEMER_MAX_IN_FLIGHT`, `[tool.bessemer]`). Each worker
  admits up to 4,096 requests and sheds load (503) only while event-loop
  queueing delay stays above a target derived from `request_timeout`, instead
  of refusing everything above a fixed 128. Suspended handlers count against
  memory, not loop time, so a slow-upstream workload can hold many more open
  requests than before. An integer keeps the previous fixed-cap meaning.
- `redirect_slashes` is on by default, as in FastAPI: a request that differs
  from a route only by a trailing slash gets a `307` to the other spelling
  (keeping `root_path` and the query string) instead of a `404`. Set
  `redirect_slashes=False` on the app or router to keep the old answer.
- `BackgroundTasks` run after the response has been delivered, as in FastAPI,
  and the request keeps its admission slot until they finish.
- JSON responses keep `dict` insertion order and Pydantic field declaration
  order, and validation errors list `type`, `loc`, `msg` in FastAPI's order.
- Once a handler has called `Request.receive()` or `is_disconnected()`, a
  client disconnect sets the disconnected flag and the request is cancelled
  one second later, so the handler can react first. Handlers that never look
  are cancelled at once, as before.
- Plain `def` handlers run on a native thread pool, with arguments bound and
  results encoded natively. ContextVars, `current_request`, cancellation
  before start, capacity accounting and exception handlers behave as before.
  Pool threads start as work needs them and are named from the executor's
  prefix (`bessemer-handler-N`, `bessemer-cpu-N`); they are not
  `threading.Thread` objects.
- The default event loop is now `auto`. On Linux and macOS, with one loop per
  worker (`loops=1`) and the native loop (`bessemer._loop`) importable, workers run
  Bessemer's Rust event loop and serve connections on its thread; in every
  other case (Windows, `loops` above 1, no native loop) they run asyncio's
  loop, silently. Set `event_loop="asyncio"`, `--event-loop asyncio`,
  `BESSEMER_EVENT_LOOP=asyncio` or `event_loop = "asyncio"` under
  `[tool.bessemer]` to keep the previous behaviour. `BESSEMER_EVENT_LOOP`,
  `[tool.bessemer]` and `--event-loop` reach only `bessemer run`; `app.run()`
  takes the `event_loop=` argument, and `TestClient` needs
  `server_options={"event_loop": "asyncio"}`. `event_loop="bessemer"`
  behaves as before, including the warning and asyncio fallback off Linux and
  macOS. This supersedes the 1.0.0rc1 note that the Rust loop is opt-in and
  off by default; its setting name and the `asyncio` escape hatch stay
  stable. `bessemer run` now logs the resolved loop on its `listening` line.
  Handles returned by the Rust loop's `call_soon`, `call_later` and `call_at`
  are not `asyncio.Handle` or `asyncio.TimerHandle` instances (as with
  uvloop); code that checks for those types needs `event_loop="asyncio"`.
- `io_backend="io_uring"` now takes effect without also setting
  `event_loop="bessemer"`, because the default loop resolves to it on Linux.

### Added

- FastAPI parity: 233 of FastAPI's 258 `docs_src` tutorial modules now build
  with only the import changed (the rest fail loudly at startup; see
  `docs/fastapi-parity.md`). New or completed:
  - `ResponseValidationError`, and `OAuth2`, `OpenIdConnect`, `HTTPDigest`
    and `SecurityBase` security classes;
  - `pydantic.Field` metadata and options on parameters, Pydantic models as
    `Query()`/`Header()`/`Cookie()`/`Form()` parameters, Pydantic functional
    validators in `Annotated`, `time`, `timedelta` and `Decimal` parameters,
    `strict=True`, and the `response: Response` parameter;
  - a native `FileResponse` with byte ranges and conditional requests;
  - `redirect_slashes`, `root_path` and `root_path_in_servers`;
  - `Request.receive()` and `Request.is_disconnected()`;
  - `WebSocket.accept(headers=)`, raw ASGI WebSocket messages and
    `WebSocketState`;
  - `app.router`, router lifespans, `bessemer.exception_handlers` and the
    `bessemer.middleware.cors`/`gzip`/`trustedhost`/`httpsredirect`
    submodules with `HTTPSRedirectMiddleware`.
- `max_in_flight` accepts `"auto"`.
- `benchmarks/compare/` gains a reproducible FastAPI feature matrix, a
  Bessemer/FastAPI/Axum concurrency runner, a CPU-isolation helper and a
  summariser.
- `event_loop` accepts `"auto"` (`App.run`, `bessemer run --event-loop`,
  `BESSEMER_EVENT_LOOP`, `[tool.bessemer]`).
- With `loop.set_debug(True)`, the Rust event loop logs callbacks that run for
  `loop.slow_callback_duration` or longer on asyncio's logger, as asyncio's loop
  does (`Executing <Handle ...> took 0.123 seconds`). Its handles' `repr` now
  ends with `created at file:line` in debug mode, as asyncio's does.

### Fixed

- `redirect_slashes` never answers `//host`-style paths with a
  protocol-relative `Location` (an open redirect found in review).
- An explicit JSON `null` for an optional body with functional validators
  (for example `Annotated[int | None, Body(), AfterValidator(check)]`) runs
  the validators; an absent body keeps its default without running them.
- `HTTPSRedirectMiddleware` keeps `root_path` in its `Location`, including
  for routes of a mounted application.
- A buffered request body of alternating large and tiny frames no longer
  reserves several times the memory the request byte budget charged for it.
- A garbage-collected executor's native thread pool is released once its
  threads have exited.
- A `HEAD` request to a route whose `GET` response is JSON or bytes now gets
  the `Content-Length` the `GET` response has, instead of `0`. When native
  compression could change that length, HEAD omits it without compressing
  or iterating the body.
- Models with custom construction or finalization hooks use Pydantic's
  JSON-validation path, so malformed JSON cannot invoke those hooks.
- The lazy `EventLoopPolicy` class retains its pickleable module identity.
- The archived final-pass benchmark tables now count completed responses when
  calculating response rates and CPU cost per response.
- The CI performance workflow now renders throughput rows from its benchmark
  result JSON.

### Notes

- Synchronous routes do not run in anyio worker threads, on either loop, so
  `anyio.from_thread.run()` and `anyio.from_thread.run_sync()` called from a
  `def` route need an explicit token: save `anyio.lowlevel.current_token()` in
  async code and pass `token=`. This is documented in the application API
  guide; behaviour is unchanged.

## [1.0.0rc1] - 2026-11-06

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
- `app.run(workers=...)` serves through the same process supervisor as
  `bessemer run`: 0 for one worker per usable CPU, or 2 to 256. The default
  stays at 1, serving from the calling process. Workers import the application
  by module and variable name, so the call belongs under
  `if __name__ == "__main__":`; an unguarded call fails with an error that
  says so.
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
- macOS (Apple Silicon and Intel) and Windows x86_64 wheels for CPython 3.12
  and 3.13, each built and tested with the Python suite before release. The
  supervisor runs several workers on macOS and Windows as on Linux. On
  Windows, workers share the listening socket through an explicit handle list,
  each runs one I/O thread, and each runs in a job object so a crashed
  supervisor takes its workers with it. The supervisor stops a Windows worker
  gracefully by closing its standard input, so graceful stop works without a
  console, under a service or `pythonw`.

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

- Entra JWT authentication requires delegated scopes or an app-only token with
  `idtyp=app` and roles. Configure the `idtyp` optional access-token claim for
  app-only clients. Key sets without `kid` are limited to eight signing keys.
- WebSocket handshakes with `Origin` require an explicit CORS origin allowlist;
  a wildcard does not allow them. Duplicate credential headers are rejected.
- Stalled HTTP/2 responses close their connection after the send timeout. Static
  file access uses pinned directory handles and rejects non-regular files.
- The experimental Rust event loop snapshots mutable send buffers. Dependency
  auditing runs in CI and weekly, with Dependabot updates enabled.
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
- Formal qualification (soak, latency and cleanup evidence) covers Linux
  x86_64 only. macOS and Windows pass the test suite but are not qualified.
  Linux arm64 wheels are not built, and free-threaded 3.14t wheels are
  Linux-only.
- `--max-rss` is Linux-only, and SIGHUP rolling reloads are POSIX-only, so
  Windows has neither. The strongest process cleanup guarantees (pidfd, parent-death signals,
  subreaper) are Linux-only.
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
