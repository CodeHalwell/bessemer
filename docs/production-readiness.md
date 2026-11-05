# Production readiness — tested checkpoints

Bessemer now has a bounded application runtime and process supervisor. It is a
1.0 release candidate for its frozen public API (see [1.0 release scope](#10-release-scope)):
the original specification is broader than this implementation, and formal
performance acceptance is still open.

The six-hour engineering push started on 4 November 2026 at 09:48 UTC. This
record tracks tested checkpoints rather than treating newly written code as
production proof. The specification and decisions live in Git so continuation
does not depend on a conversation remaining available.

## Implemented core

- Installable Rust/Python wheel, validated configuration and supervised CLI.
- App/Router registration, startup route/signature compilation, typed path/query,
  header/cookie/body inputs and constraints, dependency DAGs with request caching,
  app-scoped resources, sync/async/yield dependencies and lifespan.
- Explicit JSON/text/HTML/redirect responses, repeated cookies, safe headers,
  optional Pydantic input and response validation, filtered serialization,
  exception handlers, Request/State/context and request-scoped background work.
- OpenAPI 3.1 generated once, native schema delivery and optional bundled docs.
- Native routing/JSON, bounded body/response copying, admission limits,
  request deadlines, cancellation and native health/static replies.
- Per-worker retained request/response payload budgets, separately configurable
  from individual body/response limits and held through cleanup/transport ownership.
- Explicit HTTP/1 connection/header-read/count/buffer/target bounds, strict Host
  and framing checks, exact JSON integers, and canonical cookie parsing.
- Re-exec workers sharing a parent-owned listener, readiness and loop heartbeats,
  cgroup CPU sizing, bounded crash backoff, rolling reload, request/lifetime/RSS
  recycling, and TERM/drain/KILL shutdown with Linux process-group cleanup.
- Native CORS, env-configured API keys, allowed hosts, trusted proxy client IPs,
  bounded rate limits, request IDs, security headers, protected Prometheus
  metrics and bounded redacted access logging.
- Installed-wheel HTTP tests, actual process failure tests, reproducible Linux
  subreaper tests, CI wheel artifacts, container packaging and qualification tools.

Python application routes remain Tier 2. Native health, static documentation and
native control rejections do not execute a Python handler. The specified Tier 1
batching/native coroutine path and its performance targets are not qualified.

## Verified checkpoints

At 10:45 UTC on CPython 3.12, the rebuilt installed wheel passed **248 tests**:
23 Rust workspace tests, 216 Python tests and 9 original benchmark-harness tests.
The Python set includes actual HTTP framing/limits, dependency and ContextVar
cleanup, public API/example behavior, native controls, real worker crashes,
stalls, recycling, reload and hard shutdown. Rust formatting, warnings-denied
Clippy, Ruff formatting/lint and shell syntax checks passed.

The qualified Pydantic minimum is 2.10.6: 97 source compatibility tests passed
with that version and its pydantic-core 2.27.2. The same source suite passed with
current Pydantic; without Pydantic, 87 passed and 10 optional-feature tests
appropriately skipped. CI explicitly installs the minimum on Python 3.12 and
current supported v2 on Python 3.13 so those tests cannot silently disappear.

Pinned dependency scanners found no known advisories in the then-current
Cargo.lock, qualified minimum/current optional Python runtime closures, and
maturin 1.9.6. Scanner versions, database revision and exact dependency snapshots
are retained with the security reports. This does not replace an application
security review or guarantee absence of unknown vulnerabilities.

Independent generated-input testing and long-running qualification continue.
See [qualification evidence](qualification-results.md) for exact artifacts,
failures, workload provenance, older-wheel versus candidate distinctions and
completed runs. Property testing found a floating-point response-budget boundary issue; its
shrunk input is retained and the correction passed both boundary regressions
and the repeated generated-input campaign.

At 11:25 UTC, the refreshed 0.2.0a1 installed wheel passed **262 tests**:
25 Rust workspace tests, 228 Python tests and 9 original harness tests, with
formatting, Clippy, Ruff and shell checks passing. This includes the subsequent
JSON duplicate-value, MIME/transfer-coding, Latin-1 header, admission-publication
and multiprocess log atomicity corrections. The separate seeded campaign
passed 21,000 generated cases on its recorded immutable wheel; artifact hashes
and regression inputs distinguish that campaign from later revisions.

At 11:55 UTC, the rebuilt 0.2.0a2 installed wheel passed **270 tests**:
25 Rust workspace tests, 236 Python tests and 9 original harness tests. Remote
CI passed for commit `ac8b397` on Python 3.12 with Pydantic 2.10.6 and Python
3.13 with Pydantic 2.14.2. This checkpoint separates startup and shutdown
budgets and preserves the heartbeat reader during normal application cleanup.
Its immutable container images passed the restricted runtime scenarios at
512 MiB and 256 MiB and the authenticated production example checks. See
[container evidence](deployment.md#qualification) for the exact
image identities and limitations; this does not qualify subsequent source.

The initial OS scan reported installed Debian package advisories, including
SQLite with FTS5. Component-level review found several other flagged components
absent, but that does not make the image advisory-free. Python distribution and
Cargo advisory results have separate scopes from OS packages and the CPython
interpreter. The runtime now uses pinned Debian Trixie after isolated base-change
tests. Exact final-image scans report zero critical matches and 45 high package
findings representing nine unique CVEs, including an unresolved SQLite FTS5
denial-of-service advisory. Component triage and remaining exposure are retained
with the image records; these results do not establish an advisory-free image.

Exploratory same-host framework measurements show a material performance cost:
the a2 one-worker JSON median was 27,508 requests/second against the foundation's
55,392, while the two-worker median was 51,291 with a 28.75% sample spread.
Fixed-rate latency also deteriorated near saturation. These measurements do not
establish accepted scaling, a deployment SLO or formal M0 performance acceptance.

At 12:32 UTC, the rebuilt 0.2.0a3 installed wheel passed **302 tests**:
37 Rust workspace tests, 256 Python tests and 9 original harness tests, plus
formatting, warnings-denied Clippy, Ruff and shell checks. Every installed Python
package file matched the source byte for byte. Its wheel SHA-256 is
`5737cc939d90152e5316a387a21a9b84d8af650d7d1370361ff3cb6fc6cd5790`.
This candidate adds aggregate payload budgets, strict absolute processing
deadlines, cancellation of queued sync calls before user code starts, and
parent-enforced self-drain deadlines even with heartbeat monitoring disabled.
It avoids scheduling a cleanup task for an empty internal exit stack; nonempty
resource cleanup retains its existing cancellation shielding and context.

The same immutable a3 wheel passed 21,000 generated cases across seven seeded
HTTP properties (19,341 distinct inputs) in an isolated dependency-free core
environment. Optional Pydantic behavior is covered separately by the installed
HTTP/API suite and CI. Its exact 66-dependency Cargo lock, optional Python runtime
closures and separate build-tool closure had no known advisories in the recorded
scanner databases. The same wheel completed its two-hour, two-worker extended soak at 14:34 UTC:
720,003 expected responses, zero unexpected HTTP/log errors, verified timeout
cleanup and both normal lifespan finalizers. Server and qualifier exited zero;
shutdown took 0.766 seconds without forced termination. Exact results are
retained in the qualification record; this does not replace the 24-hour gate.

Remote CI for commit `342e0ac` passed **309 tests per Python version**:
37 Rust, 263 Python and 9 original harness tests. The seven additional Python
tests validate full-stream log redaction/error guards and delivered-work checks
in the production-image soak harness. Tested combinations were CPython 3.12.15
with Pydantic 2.10.6 and CPython 3.13.17 with Pydantic 2.14.2. The separate
container workflow passed both builds, all 512/256 MiB runtime scenarios and
the authenticated production example checks. It retained the tested image
binaries and qualification records as downloadable CI artifacts.

The recorded no-init a3 local and remote image build inputs match source digest
`6e1c4f077e4f75238484c1fb0ec2192cb3ca67c0b5904819ec8108029a4f4742`.
Exact IDs, inventories, audit scopes and CI artifacts are linked from
[deployment](deployment.md) and [CI evidence](../deploy/qualification/ci/342e0ac.json).
Those images are historical: a later process test reproduced adopted
grandchildren remaining as zombies when Bessemer itself was container PID 1.
The replacement image uses pinned, archive-verified Tini above Bessemer; its
source digest is `8fa1e5d46b617d851220c61bb4cc001e49cc55f159694f0d972284115dfee80d`.
Its native extension bytes match the preceding a3 image, with Tini the only
installed package addition. Init-aware lifecycle/orphan tests and fresh
exact-image audits are recorded separately; the no-init image's two-hour
demonstration does not qualify its replacement's timed run.
The init-enabled checkpoint [`f4f7eae`](../deploy/qualification/ci/f4f7eae.json)
is green in both remote CI workflows:
**315 tests per Python version** (37 Rust, 269 Python and 9 original harness
tests), plus both image builds and all five runtime scenarios at each of
512/256 MiB. The new orphan test requires complete child absence after a worker
crash while init and the supervisor remain alive; zombie state cannot pass.
The replacement image's planned 90-minute run was stopped early at the user's
request to limit usage. Partial observations are retained separately; the full
90-minute image soak is incomplete and is not an acceptance result.
The final wheel's one-core JSON median was 35,583 requests/second with 2.86%
spread; its single corrected-latency point at 13,847 requests/second had p99
2.605 ms. These short diagnostics leave formal performance acceptance open.

A first framework-overhead pass removed three per-request costs. Native Tokio
threads now keep one CPython thread state instead of recreating it for each
request. Results made only of exact built-in JSON values are encoded natively
once. Anything else still goes through `jsonable`, with its existing behaviour
and errors. Routes skip resolver, validation and request-view setup they do not
need. A same-host interleaved A/B on a different, slower machine put one-core
`/json` at 18.3k/s for main, 26.4k/s for the foundation and 29.2k/s for this
change. Awaited I/O rose 20% but stayed about 7% below the foundation.
This is diagnostic evidence only; see
`benchmarks/results/2026-11-04/framework-overhead-pass1/`.

The JSON codec was then rewritten as the `bessemer-codec` crate, the audited
unsafe boundary for direct CPython object access. Responses made of exact
built-in JSON values are written straight from Python objects to bytes, and
request bodies are parsed in a single pass straight to Python objects (or to a
`Value` without the GIL for bound body parameters). The previous path built a
`serde_json::Value` in both directions and re-parsed nested request values once
per level. Output and errors are unchanged: sorted keys, `serde_json`'s number
formatting, 64-level nesting, 4096-digit integers, finite floats and
last-key-wins duplicates are checked by differential fuzzing against the old
implementation. Single-core codec timings fell 2-5x (100-item list encode
57.9 to 11.2 microseconds, decode 86.8 to 24.0). A same-host A/B with the server
and `oha` on separate pinned cores, five alternating 10-second runs, gave
`/items` (100 objects) 7.6k to 14.1k/s, a 20-object JSON echo 10.1k to 13.0k/s
and small `/json` 17.0k to 17.4k/s (within noise). Spreads reached 5.9%, above
the formal 3% ceiling, so this is diagnostic evidence only; see
`benchmarks/results/2026-11-04/codec-rewrite/`.

The HTTP layer pass replaced hyper's connection loop for ordinary requests with
`src/http1.rs`, a purpose-built HTTP/1.1 loop: `httparse` heads parsed from one
reused buffer into zero-copy header values, request bodies framed on demand,
and the response head and an in-memory body written together (large single
bodies with one vectored write). It keeps hyper's limits, error statuses,
header deadline, keep-alive, `Expect: 100-continue`, HEAD/204/304 rules and
disconnect cancellation; requests that upgrade are replayed byte for byte to
hyper. One deliberate tightening: `Transfer-Encoding` with `Content-Length` is
now refused with 400 instead of being read as chunked. The controls middleware
became a hand-written Tower layer, generated request IDs no longer read the
clock or call `getpid` per request, and the release profile uses fat LTO with
one codegen unit. In memory the controls cost fell from about 2.7 to 1.4
microseconds per request. On the same host, with three alternating 10-second
runs, the native `/healthz` route rose from 51.6k to 79.7k/s; Python-handled
routes were within noise (`/json` 8.0k to 8.5k/s), because handler dispatch
dominates them. The host was busy and spreads reached 16%, so this is
diagnostic evidence only; see `benchmarks/results/2026-11-04/http-layer/`.

Requests now reach the asyncio worker through a native batched queue
(`src/bridge.rs`), so Tokio threads no longer take the GIL on the request path.
The first request into an empty queue writes one wake-up byte to a socket pair.
The loop watches that socket, then drains the whole batch on its own thread and
builds the Python arguments there. Completion publishes through a native
ticket, without `concurrent.futures`. Deadlines that expire and cancellations
that arrive while a job is still queued are answered without starting Python
work. Cancellation of a running task travels through the same queue. Admission
is still held until the handler and its cleanup have actually finished. Each
request starts in a fresh, empty `contextvars` context. On the same host and
harness, one-core `/json` reached 39.0k/s, against 30.3k/s for the previous
pass and 18.4k/s for main; awaited I/O reached 17.1k/s. This is diagnostic
evidence only; see `benchmarks/results/2026-11-04/native-handoff/`.
`Worker.submit` remains available for direct, thread-safe use.

The native extension now uses mimalloc as its Rust global allocator. Python's
own allocator is unchanged. On the same host and harness, one-core `/json`
rose 12% to 45.6k/s and `/items/7?q=abc` rose 22% to 37.0k/s against the
batched handoff; awaited I/O rose 3%. This is diagnostic evidence only; see
`benchmarks/results/2026-11-04/allocator/`.

Handlers taken from the native queue now start eagerly
(`asyncio.Task(..., eager_start=True)`). Each still runs in its own Task and
fresh context, but a handler that returns without suspending is answered
inside the drain, without a loop iteration or a done callback. Handlers that
suspend continue as ordinary Tasks with the same cancellation and shutdown
tracking. A task factory installed by the application still creates every
task. On the same host and harness, one-core `/json` rose 24% to 55.9k/s and
`/items/7?q=abc` 26% to 45.8k/s; awaited I/O rose 2%. This is diagnostic
evidence only; see `benchmarks/results/2026-11-04/eager-start/`.

Async handlers that take only native inputs now run on a lean worker path. When
a route has no dependencies, before-hooks, background tasks or principal, the
worker calls the handler directly, without an `AsyncExitStack`,
`BackgroundTasks` or resolver. A request with no metadata is built without
parsing. Streaming results still get an exit stack and background tasks. The
Python cost per `/json` request fell from 4.3 to 2.5 microseconds. On the same
host and harness, one-core `/json` rose 25% to 73.6k/s and `/items/7?q=abc`
26% to 58.1k/s; awaited I/O rose 7%. This is diagnostic evidence only; see
`benchmarks/results/2026-11-04/lean-path/`.

Requests are now routed natively (`src/router.rs`) instead of through the axum
`Router` and tower layers. One `matchit` lookup selects the route, and the
native controls run around the handler directly. The native handler also
borrows the request head instead of cloning headers and request strings. The
404 and 405 fallbacks, `Allow`, `HEAD` handling, percent-decoding and telemetry
templates are unchanged. On the same host and harness, one-core `/json` rose
18% to 85.6k/s and `/items/7?q=abc` 19% to 67.8k/s against the lean worker
path. This is diagnostic evidence only; see
`benchmarks/results/2026-11-04/native-router/`.

Requests are now routed natively (`src/router.rs`) instead of through the axum
`Router` and tower layers. One `matchit` lookup selects the route, and the
native controls run around the handler directly. The native handler also
borrows the request head instead of cloning headers and request strings. The
404 and 405 fallbacks, `Allow`, `HEAD` handling, percent-decoding and telemetry
templates are unchanged. On the same host and harness, one-core `/json` rose
18% to 85.6k/s and `/items/7?q=abc` 19% to 67.8k/s against the lean worker
path. This is diagnostic evidence only; see
`benchmarks/results/2026-11-04/native-router/`.

### Native Pydantic model fast path

Implemented scope: request bodies bound to a plain Pydantic model are built
into instances natively from the parsed JSON when every value already has its
field's exact JSON type, and a returned instance of exactly the declared
response model is encoded natively. Eligibility is read from pydantic-core's
schema with an allow-list of keys: fields of `int`, `float`, `str`, `bool`,
lists, optionals and nested plain models, immutable defaults, single-string
aliases, `extra` of `ignore` or `forbid`. Validators, constraints, custom
serializers, computed fields, private attributes, recursion, plans nested past
64 levels, `Body()` length constraints, `response_model_*` options and
validation-changing config keep the Pydantic path. Bodies that need coercion
or fail validation, and values Pydantic must serialize, always fall back, so
coercions, 422 details and response bytes are unchanged.

The unsafe code lives in `crates/bessemer-codec/src/model.rs`: instances are
created through `tp_new` and `PyObject_GenericSetAttr` as pydantic-core does,
and the encoder reuses the codec's container rules, holding strong references
to field values on free-threaded builds.

Validation: `tests/python/test_model_fast_path.py` compares 3,000 random
bodies field by field against Pydantic, checks encoder bytes against
`dump_python` plus the plain encoder, encodes while another thread mutates the
instance, and checks 422 details through the real server. It passed with
Pydantic 2.14.2 on CPython 3.13, Pydantic 2.10.6 on CPython 3.13, and on
CPython 3.14t with `PYTHON_GIL=0`, alongside the full `scripts/check.sh` run.
On the #41 same-host comparison, the 20-item `/echo` route rose from about
10.9k to 15.6k requests/s on one core. This is exploratory evidence, not a
formal performance gate; Python routes remain Tier 2.

When the route has a single body parameter with a plan and does not take the
`Request`, the body is no longer parsed into a JSON value on the Tokio thread.
There it is only checked with the same parser and limits, so invalid JSON
still gets the same `json_invalid` 422 before any other parameter is checked.
The bytes travel with the job, and the loop thread parses them straight into
the model (`parse_model` in `model.rs`). A value without its field's exact
type, a repeated field key, an extra key under `extra="forbid"` or a missing
required field makes it give up, and the body is then parsed and handled
exactly as before. `ParseTests` in the same test file checks, on 3,000 randomly
formatted bodies and 3,000 randomly corrupted ones, that it builds the same
instance as the value path whenever it builds one, never refuses a body the
value path builds except for repeated keys, and refuses exactly the input the
value path refuses; HTTP tests cover the order of checks, depth limits and
repeated keys. On the same comparison the `/echo` route rose from a median of
about 16.1k to 18.3k requests/s on one core, and from about 52-57k to 66-69k
on three.

### Per-request overhead

Implemented scope: two internal changes with no behaviour change. The native
handler future is boxed inside the dispatcher, because at several kilobytes it
was copied into every enclosing connection future; the profile's `memmove`
share halved. An async handler that takes no inputs, runs no middleware and
receives no native arguments (no principal or trace) now runs on a shorter
branch of the lean path that skips the checks which cannot apply. It builds the
same bare request, keeps it current for the handler and for exception
handlers, and answers every exception through the same handling as the other
paths, now shared in `Worker._invocation_failed`.

Validation: `WorkerFastPathTests` covers results, the current request and
`HTTPException`, handled and unhandled errors on such routes, and the full
Python suite, `cargo clippy` and the native tests passed. A `perf` profile of
the #41 `/json` route on one core put Bessemer at about 14µs of CPU per
request against Axum's 6.6µs: about 4.3µs Rust, 4.2µs Python and 5.6µs kernel,
against 2.1µs user and 4.5µs kernel for Axum. On the same-host comparison,
alternating with a build of main over five runs, `/json` rose from a median of
about 71.6k to 77.9k requests/s and `/items/{id}` by about 5%; `/echo` is
unchanged. Skipping the wake-up socket write while the loop is busy cut
syscalls but gave no reliable throughput gain on one core, so it was not
kept. This is exploratory evidence; Python routes remain Tier 2.

### Native invoker (performance design phase 2)

The per-request orchestration on the event loop thread moved from Python into
Rust (`src/invoker.rs`). `Invoker.drain`, the loop's reader callback, creates
each request's `contextvars.Context`, its `Invocation` (the native
`_InvocationControl`: cancellation completes only after the work does and is
declined while a sync call runs) and its eagerly started `asyncio.Task`; the
responses settled during one drain are published together afterwards. A
route qualifies for the lean native path when `_invoke` would call its async
handler with the native inputs unchanged: scalar path, query, header and
cookie inputs only, no dependencies, middleware, request metadata (routes of
an app with exception handlers always get metadata), principal or body. Its
handler runs inside `Call`, a Rust coroutine that calls the handler on the
task's first step with a bare request copied from a per-route template set as
`current_request` in the task's own context, then encodes an exact built-in
JSON value or exact declared model natively. Any other result, stream,
response with background tasks or exception goes to the worker's
`_plain_response`, `_lean_response` or `_lean_failed`, which keep the Python
path's handling (exception handlers run while the exception is being
handled). Bounds (`ge`, `gt`, `le`, `lt`, `min_length`, `max_length`) without a
pattern or converter are checked natively; a value outside them, or one that
cannot be compared exactly, takes `_invoke` and gets the same 422. Everything
else takes `_invoke` exactly as before. `Call` exposes the `cr_*` attributes,
so `inspect.getcoroutinestate` (used by anyio) works on `Task.get_coro()`.

Validation: `tests/python/test_native_invoker.py` covers route selection,
inputs and bounds, the task and request seen by handlers, exceptions
(including `BaseException` and errors raised when the handler is called),
streams, background tasks, response models, HEAD and 204, cancellation of a
suspended handler on disconnect, coroutine introspection and that a stopped
client leaves nothing alive. The full Python suite, `cargo test`, `cargo
clippy` and ruff pass. CPU per request on one core (server and `oha` pinned to
separate cores, 64 connections, three alternating runs against a build of
`04bbc82`, measured per thread from `/proc`): `/json` 12.8 to 11.5µs (76.8k to
86.4k requests/s) and `/items/{id}` 16.0 to 13.4µs (62.0k to 74.3k); the
Python thread's share fell from 3.5 to 2.4µs and from 5.5 to 2.7µs. At 64
connections a drain takes about 20 requests. This is exploratory evidence;
Python routes remain Tier 2.
### Rust request path copies and allocations

Implemented scope: internal changes with no behaviour change, the first part of
phase 1 in [performance-design.md](performance-design.md). Each HTTP/1
connection boxes the application's future once per request instead of holding
it inline: the connection future was about 40 KB and was copied on every
request. The rarely used asynchronous steps (shared rate limiting, metrics
scrapes, compression, uploads, static files, WebSockets) are boxed so they no
longer enlarge every request's future. Host and request-target checks validate
in place without allocating (the Host rules are unchanged and now have a table
test), request ids reuse the client's bytes or move a new id into the header
without a copy, the peer address is passed as an argument instead of a request
extension, header maps are sized for the headers added later, and response
heads are formatted into a buffer reused per connection.

Validation: the full Python suite, the native tests and `cargo clippy` passed.
Under callgrind (`benchmarks/profile/instructions.sh`, 5,000 requests),
user-space instructions per request fell from 57,959 to 55,615 on `/json`,
79,154 to 76,375 on `/items/{id}` and 21,392 to 19,177 on the native-only
`/healthz`. Wall-clock throughput differences are inside this host's ±7%
run-to-run noise. This is exploratory evidence; Python routes remain Tier 2.

### Query parsing without maps

Implemented scope: an internal change with no behaviour change, the second part
of phase 1 in [performance-design.md](performance-design.md). The request query
is decoded once with `form_urlencoded` into pairs that borrow the request
target unless they needed decoding, and scalar query parameters search those
pairs from the end, so a repeated key still binds its last value. Decoding is
lossy, as it was through `serde_urlencoded`, which never failed for string
pairs, so the unreachable "Invalid query string" rejection was removed. The
request timeout's future is boxed so the handler's state is not copied into
it.

Validation: the full Python suite, the native tests and `cargo clippy` passed,
with a new HTTP test that a repeated, percent-encoded query key binds its last
decoded value. Under callgrind, user-space instructions per request fell from
76,111 to 74,165 on `/items/{id}` and from 55,615 to 54,323 on `/json`. This is
exploratory evidence; Python routes remain Tier 2.

### Content-length from the size hint

Implemented scope: an internal change with no change on the wire, the third
part of phase 1 in [performance-design.md](performance-design.md). The router
sets `content-length` only on `HEAD` responses, whose body it drops; other
responses keep their exact size hint, from which the HTTP/1 writer and hyper
write the same header in the same place. This saves formatting the length into
a shared header value (two allocations) and a header map insert per request.
Integer parameters without a `+` sign or leading zeros are parsed in place
rather than through a normalised copy.

Validation: the full Python suite, the native tests and `cargo clippy` passed,
with unit tests for the `HEAD` and `GET` cases and for integer normalisation.
One run hit an existing race in
`test_worker_group_descendants_are_killed_after_leader_exit`, where two writes
to the captured output interleave; it passed on three reruns and is unrelated
to this change. Under callgrind, user-space instructions per request fell from
54,346 to 53,573 on `/json` and from 73,956 to 73,175 on `/items/{id}`. This
is exploratory evidence; Python routes remain Tier 2.

### Request arguments as a list

Implemented scope: an internal change with no behaviour change, the typed
route plan step of phase 1 in [performance-design.md](performance-design.md).
`bridge::Arguments` replaces the `serde_json::Map` that carried a request's
bound inputs to the loop thread. It is a list in parameter order whose keys
share the route's parameter names (`Arc<str>`), so binding an input no longer
copies its name or builds a B-tree. A name bound twice keeps one entry with
the last value, as the map did, so the native invoker's input count check is
unchanged. `benchmarks/profile/instructions.sh` gained `CONNECTIONS`, since
batching on the loop makes counts at several connections move by about 1%.

Validation: the full Python suite, the native tests (with a unit test for
repeated names) and `cargo clippy` passed. Under callgrind at one connection,
user-space instructions per request fell from 63,444 to 62,952 on
`/items/{id}`; `/json`, which has no inputs, is unchanged within 0.3%. This is
exploratory evidence; Python routes remain Tier 2.

### Rust event loop in the wheel

Implemented scope: the phase 4 spike's event loop moved from a standalone
crate and wheel into the server's build. `crates/bessemer-loop` is now a
workspace library whose classes register into `bessemer._native`; its Python
side is `bessemer._loop` and its behaviour tests are
`tests/python/test_event_loop.py`. `App._run` and `Worker` accept
`event_loop="bessemer"` (default `"asyncio"`), which builds every lane's loop
from `bessemer._loop.Loop`. No public setting, CLI flag or `App.run` argument
was added, so the 1.x API is unchanged. The loop is Linux only and is not
compiled elsewhere.

Validation: `BESSEMER_REQUIRE_NATIVE=1 ./scripts/check.sh` passed (681 Python
tests, the native tests, clippy and ruff, now including the loop's harness).
The whole Python suite also passed with the default loop switched to the Rust
loop in the installed package, and the loop tests passed on free-threaded
3.14t. CPython's asyncio suite passed 2011 of 2011 against the built wheel. CPU
per request on `/json` at one connection fell from 37.3 to 31.1µs (three
alternating pairs; the server and `oha` pinned to separate cores); at 64
connections it was unchanged at about 10.7µs. This is exploratory evidence;
the loop is not used unless that internal option is passed.

### Single-thread worker on the Rust loop

Implemented scope: with `event_loop="bessemer"` and one loop per worker, the
HTTP server no longer has its own runtime. `serve` takes the loop
(`event_loop=`, internal) and spawns the server on a Tokio current-thread
runtime embedded in the loop (`crates/bessemer-loop/src/driver.rs`), so
connections and Python callbacks share the loop's thread. The loop runs its
callbacks inside that runtime and waits in it, with the GIL released. The
bridge wakes the loop directly (`Bridge.set_local_wake`) instead of through its
socket pair. `serve` returns an error if the loop closes before the server
stops, rather than waiting for ever. Several loops per worker, and the default
asyncio loop, are unchanged. No public setting was added.

Validation: `BESSEMER_REQUIRE_NATIVE=1 ./scripts/check.sh` passed (682 Python
tests). The whole Python suite also passed with the default loop switched to
the Rust loop, which serves connections this way. The loop and fast-path tests
passed on free-threaded 3.14t. A new test checks that no server runtime
threads exist while a one-loop worker serves. CPU per request (three
alternating runs; server and `oha` pinned to separate cores): `/json` at one
connection 36.3 to 25.7µs, at 64 connections 10.7 to 9.4µs (92k to 105k req/s);
`/items` 37.9 to 27.2µs and 11.6 to 10.4µs. This is exploratory evidence; the
path is used only with the internal option.

### Event loop setting

Implemented scope: `event_loop` is a public server setting (`App.run`,
`bessemer run --event-loop`, `BESSEMER_EVENT_LOOP`, `[tool.bessemer]`), with
`"asyncio"` (the default) and `"bessemer"`. `"bessemer"` runs the Rust loop
described above, and with one loop per worker the single-thread worker. On
platforms other than Linux the worker logs a warning and uses asyncio
(`worker._resolve_event_loop`). Other values are rejected at settings time.
This is an addition only; no existing setting changed.

Validation actually run: `BESSEMER_REQUIRE_NATIVE=1 ./scripts/check.sh` passed
against the built wheel (687 Python tests), with new tests for the default, the environment variable, the
CLI flag reaching the supervisor, rejection of unknown values, the non-Linux
fallback (platform mocked) and `App.run` passing the value on. `bessemer run` with
`BESSEMER_EVENT_LOOP=bessemer` served a handler on `bessemer._loop`, and on
asyncio without it. The earlier
whole-suite run with the Rust loop as the default still stands as the
stability evidence. Remaining gaps: the fallback has been exercised only with a
mocked platform, not on macOS or Windows; making the Rust loop the Linux
default waits for more soak time.

### Loop buffer and compiled route plans

Implemented scope: the Rust loop no longer zeroes its 3 KB epoll buffer on
every iteration (made once per run in the driven loop, left uninitialised
where `epoll_wait` fills it). Routes are compiled once into typed parameter
sources and kinds, parsed header names and per-route flags, replacing string
comparisons over the parameter list on each request. Behaviour, error bodies
and the public API are unchanged.

Validation actually run: `BESSEMER_REQUIRE_NATIVE=1 ./scripts/check.sh`
passed (684 Python tests, 137 Rust unit tests including new ones for route
plans and source/kind parsing, clippy and ruff), and the loop and fast-path
tests passed against the built wheel. CPython's asyncio suite against the
wheel runs in CI's event-loop job, not locally. Measurements are exploratory
callgrind instruction counts at one connection on the Rust loop (`/items`
51,091 to 47,737, −6.6%; `/json` 42,957 after), not a formal performance
acceptance run. Remaining gaps: the per-request timer, header maps and the
response path are unchanged (phase 1's lean request objects).

### Batch buffers and per-request maps

Implemented scope: the invoker reuses its job and cancellation buffers
across batches (swapped with the bridge's under its lock), keeps one eager
task options dict, matches cancellations in a sorted list, and the processing
deadline is stored in a response's extensions only when the response may be
compressed, the one reader of it. Behaviour and the public API are unchanged.

Validation actually run: `BESSEMER_REQUIRE_NATIVE=1 ./scripts/check.sh`
passed (687 Python tests, 137 Rust unit tests, clippy and ruff). Measurement
is an exploratory callgrind count at one connection on the Rust loop
(`/json` 43,039 to 41,488 instructions per request), not a formal performance
run.

### Request deadline queue

Implemented scope: request deadlines no longer use a Tokio timer per
request. Each server has a deadline queue (`src/deadline.rs`) with one FIFO
per distinct timeout (the server's `request_timeout` and each per-route
`timeout`) and one sweeper task, started on the first request that waits, on
whichever runtime serves it (the loop's embedded single-thread runtime or the
default multi-thread one). A request registers when it first returns pending,
reusing the finished slot at the front of its FIFO; the sweeper sleeps until
the earliest deadline (and never longer than the shortest timeout while
requests arrive), marks expired requests and wakes them. A registration whose
deadline is earlier than the sweeper's armed time wakes it; after an idle
period the sweeper parks without a timer. Everything around the deadline is
unchanged: the request is polled before its deadline, a late ready response is
still a 504 "Request timed out", dropping a request still cancels its Python
work, a timed-out incomplete body still answers `Connection: close`, the
existing deadline checks before and after dispatch remain, and the streaming
per-chunk timeout and WebSocket timeout still use Tokio timers.

Validation actually run: `BESSEMER_REQUIRE_NATIVE=1 ./scripts/check.sh`
passed (687 Python tests, 142 Rust unit tests, clippy and ruff). New Rust tests cover a deadline firing on the
current-thread and multi-thread runtimes, a shorter timeout registered behind a
longer one, slot reuse for sequential requests, and compaction and silence of
dropped requests. An ad hoc end-to-end run against the installed wheel on both
`event_loop="bessemer"` and `"asyncio"` saw 504 at the per-route (0.3 s) and
server (0.5 s) timeouts within 3 ms, the handler's cancellation, `Connection:
close` on a timed-out incomplete body, and normal responses afterwards.
Measurement is an exploratory callgrind count at one connection on the Rust
loop (`/json` about 41,380 to 40,740, −1.55%; `/items/42` 45,977 to 45,208),
not a formal performance run. Remaining gaps: the sweeper wakes at least once
per shortest timeout while requests keep arriving (every 10 ms for a 10 ms
route timeout); a FIFO keeps finished entries behind a long-running request
until compaction (bounded at twice the live count, at least 64); timer
starvation behaves as before (a late wake still fires, only later).
### Response headers written directly on HTTP/1

Implemented scope: when the native HTTP/1 server writes a response, the
`x-request-id` and security headers (`x-content-type-options`,
`x-frame-options`, `referrer-policy`) are passed from `Controls::finish` to
the writer as `DirectHeaders` instead of being inserted into the response's
`HeaderMap`. The writer drops any value the application gave for those names
and writes ours, so the wire output matches `insert` semantics. HTTP/2,
upgraded connections served by hyper, and Rust callers that read the
`Response` keep the headers in the map. A 1xx final response still becomes a
bare 500. CORS headers still use the map. The public API is unchanged.

Validation actually run: `BESSEMER_REQUIRE_NATIVE=1 ./scripts/check.sh`
passed (689 Python tests, 138 Rust unit tests, clippy and ruff). Two earlier
runs each failed once in `test_abandoned_handshake_holds_its_slot_through_cleanup`,
a WebSocket test (served by hyper, not this writer) with a 5-second deadline
on a loaded shared machine; it then passed in 5 of 5 runs of its class, 7 of 8
runs of its module and a full-suite run on each of the new and baseline wheels,
so it is recorded as a timing flake, not a regression. New tests cover the HTTP/1 writer with an application
overriding `x-frame-options` (twice) and `x-request-id`, the 1xx case, and an
end-to-end check that HTTP/1.1 and HTTP/2 (curl, prior knowledge) both carry
exactly one value of each control header with the application's override
replaced. Measurement is an exploratory callgrind count at one connection on
the Rust loop (`/json` 41,716 to 40,405, `/items/42?q=y` 46,334 to 45,108
instructions per request), not a formal performance run. Remaining gaps: the
header order on HTTP/1 differs (control headers now follow the application's
headers instead of taking an overridden header's place); CORS and other
controls still go through the map.
### Leaner request head

Implemented scope: the HTTP/1 parser maps the sixteen most common header
names, sent in lowercase, straight to their standard `HeaderName` by one
fixed-length comparison; any other spelling (including Title-Case) still goes
through `HeaderName::from_bytes`, so names, values, framing and the smuggling
checks are unchanged. The header map is filled in place inside the request,
the request is built with an empty body (replaced when the head frames one),
and the parsed head stays boxed until the exchange. `Controls::begin`'s
result boxes its rare rejection response and the request's tracing span, so
it shrinks from 456 to 232 bytes. The `Request` handlers and Python see and
the public API are unchanged.

Validation actually run: `BESSEMER_REQUIRE_NATIVE=1 ./scripts/check.sh` passed
(687 Python tests, 143 Rust unit tests, clippy and ruff). New Rust tests check
that every standard name, in lower, upper and Title-Case, and every one-byte
change of each common name, resolves to what `HeaderName::from_bytes` returns
or defers to it, and that lowercase and Title-Case heads parse to identical
header maps and framing. Measurement is an exploratory callgrind count at one
connection on the Rust loop with a private vgdb prefix (`/json` 41,482 to
about 40,530; `/items/42?q=y` 46,009 to 45,221), not a formal performance run.
Remaining gaps: Title-Case names (as curl and browsers send over HTTP/1.1)
take the old path; header value validation, the header map inserts and the
moves of the request and handler futures in `Dispatcher::call` and `handle`
are unchanged; a traced request makes one more small allocation for its span.

### io_uring socket I/O (phase 5)

Implemented scope: `io_backend` is a new, additive server setting (`App.run`,
`bessemer run --io-backend`, `BESSEMER_IO_BACKEND`, `[tool.bessemer]`) with
`"epoll"` (the default) and `"io_uring"`. With `"io_uring"`, a worker that
serves connections on the Rust loop (`event_loop="bessemer"`, one loop) accepts,
reads and writes its sockets through one io_uring per loop thread
(`crates/bessemer-loop/src/uring.rs`): multishot accept on the listener,
multishot recv into a 4096 x 2 KiB provided-buffer ring (each buffer is copied
out and returned at once), and the sends queued during a pass of the runtime's
tasks submitted together with completion processing in one `io_uring_enter`.
The ring uses `SINGLE_ISSUER | DEFER_TASKRUN | TASKRUN_FLAG`; a pump task on
the loop's embedded runtime runs the submit/complete cycle and sleeps on an
eventfd registered with the ring (the ring descriptor itself was not reported
readable to epoll under `DEFER_TASKRUN` on Linux 6.18). The transport takes
either listener (`transport::Listener`) and the HTTP/1, HTTP/2, TLS and
WebSocket paths run unchanged over the io_uring stream. Recv pauses above
64 KiB of unread input and writes wait above 256 KiB unsent. If the ring cannot
be set up (seccomp `EPERM`, `kernel.io_uring_disabled`, a kernel before 6.1 or
without multishot accept/recv or buffer rings), the worker logs the reason and
serves on epoll. With asyncio or several loops it logs a warning and serves on
epoll. `deploy/seccomp-io-uring.json` is Docker's default seccomp profile plus
the three io_uring system calls, documented in `docs/deployment.md`. The
default, the FastAPI-compatible API and existing settings are unchanged.

Validation actually run: `BESSEMER_REQUIRE_NATIVE=1 ./scripts/check.sh`
passed against the built wheel on top of #62 (696 Python tests, the Rust suites with 7 new io_uring tests, clippy and ruff). New Rust tests run the
io_uring listener and stream directly: 64 concurrent echo connections, 4 MiB
each way (pausing recv and partial sends), dropping streams with flushed and
unflushed writes, replacing the listener on one thread, and a seccomp filter
returning `EPERM` for `io_uring_setup` handing the listener back for epoll. New
Python tests serve requests, a 900 KB request body, a 3 MB response, a stream,
a WebSocket, 32 concurrent keep-alive clients and abandoned half-requests on
`io_backend="io_uring"`, and check that a ring exists. The whole Python suite
(695 tests) also passed with the defaults switched to the Rust loop on
io_uring, except the one test that asserts the default has server runtime
threads. Measured with the spike's method
(`benchmarks/results/2026-11-05/io-uring-backend`): `/json` CPU per request
with the server core saturated fell from 13.8 to 11.1µs at 64 connections and
from 15.1 to 11.5µs at 1024 (72k to 89k and 65k to 85k req/s on one core).
This is exploratory, single-host evidence. Remaining gaps: registered (fixed)
file descriptors, zero-copy sends and `sendfile` through the ring are not used;
the fallback was tested with a seccomp filter in-process, not in a Docker
container; the provided-buffer memory (8 MiB) is left allocated when a loop
thread exits; no soak run yet.

## Operational contracts

The CLI process supervisor is POSIX. Linux has the qualified parent-death and
zombie-safe process-group cleanup path. Each worker owns one asyncio loop and a
bounded sync pool. Limits apply per worker unless explicitly documented. A
worker replacement overlaps at most one old draining worker in its slot.
Deliberately detached descendants remain the container/cgroup's responsibility.
The supplied container runs Tini as PID 1 to forward signals and reap adopted
descendants. Bessemer supervises its tracked worker leaders. A custom image that
runs the CLI as PID 1 needs an init/subreaper for application grandchildren.

A request capacity slot remains held through actual sync work and cancellation
cleanup. The supervisor can terminate a non-cooperative process after its drain
deadline. Generator dependency cleanup and BackgroundTasks currently complete
before the response is sent; this favors bounded resource lifetime over the
specification's future post-send optimization. Native pre-handler errors do not
currently invoke Python custom exception handlers.
Yielding cleanup can keep all request slots occupied while the heartbeat stays
healthy. The default recycling limits are disabled, so native health alone does
not guarantee automatic recovery. See [stuck work](supervision.md#request-deadlines-and-stuck-work)
for cooperative cleanup, lifetime recycling and functional-probe policies.

JSON integers preserve precision up to 4096 digits; floats must be finite. JSON
nesting is capped at 64 levels. Route literals must be ASCII URI text, with
explicit percent encoding for non-ASCII literals; parameter values support
Unicode. Cookie parsing uses strict RFC 6265 octets and rejects malformed or
duplicate names. Validation errors omit raw input values and arbitrary validator
messages; reported locations can retain submitted dictionary keys.
Explicit HTTPException details remain application output.

Default application request/response bounds are 1 MiB/4 MiB. Normalization and
native encoding are bounded; application code and external model serialization
can allocate before the framework sees a result. Application response headers
are capped at 64 fields and 16 KiB. The default request transport uses at most
1024 connections, 64 headers, 32 KiB header buffer, a 10-second header deadline
and an 8192-byte normalized request target. The target limit applies to
`http::Uri`, which may strip fragments; the full raw request line/header block
is still bounded by the header-buffer limit. See configuration docs for ranges.

Aggregate retained request and response payload budgets each default to 32 MiB
per worker, are capped at 1 GiB and cannot be smaller than the corresponding
individual size limit. Incoming actual body chunks stay charged through Python
completion/cancellation cleanup; response bytes remain charged through transport
ownership, including native static schema/docs responses. Small native health
and error replies are exempt; metrics retains its separate 4 MiB single-scrape
guard. This is logical payload accounting, not a physical heap guarantee:
application/model allocations, temporary serialization, headers, startup storage
and kernel buffers remain outside the budget. Independent worker/replacement
budgets can overlap. See [application API](application-api.md) for configuration.
Processing deadlines cover body reading, validation, submission, handler/cleanup
and encoding to response readiness, excluding final socket drain. Synchronous
GIL-held work cannot be preempted, and timed-out cleanup can continue while
holding request resources. Admission `503` and incomplete-body `504` close the
connection; a handler `504` after a consumed body preserves keepalive.

API keys and metrics tokens are read from named environment variables; no key
values are exported in route/schema configuration. Host allowlists, proxy trust
and health exemptions are explicit. Rate limits are process-local unless a
Redis store shares them across workers and replicas. Metrics
require one worker until cross-process aggregation exists, use fixed/bounded
labels and a 4 MiB scrape cap, report omitted series, and admit only one scrape
body at a time. Access logs are best-effort through a bounded queue and never
include raw URLs, query strings, request bodies or authorization headers.
Metrics duration/in-flight accounting ends when the response is ready and
excludes socket drain; response byte reservations can persist through that drain.

Native TLS termination uses rustls with PEM files read once at startup
(`--tls-cert`, `--tls-key`), TLS 1.3 minimum by default and 1.2 by
`--tls-min 1.2`. Mutual TLS verifies client certificates against
`--tls-client-ca` (required by default, or `--tls-client-auth optional`) and
exposes the verified leaf as `request.client_certificate`. `--tls-client-crl`
adds PEM certificate revocation lists: a revoked client certificate is
refused, and so is one whose issuer has no CRL in the file or whose CRL is
past its next update (as OpenSSL does). Only the client's own certificate is
checked against them; intermediates are trusted by being in the CA bundle,
and client OCSP checks are not supported. `--tls-ocsp-response` staples a DER
OCSP response for the server certificate to every handshake; Bessemer does not
fetch or validate it, so a job (for example `openssl ocsp` or the CA's
tooling) must refresh the file before it expires, and the reload then picks
it up. The certificate, key, client CA, CRL and OCSP files are re-read every
`--tls-reload-interval` seconds (60 by default, 0 disables); new connections
use changed files once they form a valid configuration, while open
connections keep the one they started with. A change that fails to load is
logged and the working certificate stays in use. The TLS handshake
shares the header deadline and connection limit, and a connection over capacity is closed
rather than answered in plaintext. Use ingress for deployment access controls
and infrastructure policy.

HTTP/2 is served by default (`--http auto`) through hyper: negotiated by ALPN
over TLS, and by prior knowledge (the client preface) on plaintext, so an
ingress can speak h2c to workers on the same port as HTTP/1.1. Each connection
allows 100 concurrent streams, applies the header-size and header-count
limits, caps client resets before the server reads a stream (rapid-reset
protection) and is closed with GOAWAY after a header-timeout period with no
open streams. Application admission stays bounded by the in-flight limits.
WebSockets also open over HTTP/2 by extended CONNECT (RFC 8441, advertised in
SETTINGS): each session is one stream, counts toward the 100-stream limit,
keeps the connection from being closed as idle, and receives the same close
on shutdown as an HTTP/1.1 session. A connection admits at most 100 WebSocket
handlers at once, each counted from the handshake until the handler finishes,
so refused handshakes whose handlers are still cleaning up cannot accumulate;
beyond that a handshake gets 503. `--http 1.1` turns HTTP/2 off. h2spec
runs in CI over TLS and plaintext (`scripts/h2spec.sh`).
Health reports process reachability; application readiness must check its own
resources. The included production example demonstrates that separation.

## 1.0 release scope

`1.0.0rc1` freezes the public API described in the
[changelog](../CHANGELOG.md#100rc1---2026-11-04): `bessemer.__all__`, CLI
flags, `BESSEMER_*` settings and `[tool.bessemer]` keys. The 1.0 line is
defined as that API freeze on the qualified Linux x86_64 / CPython 3.12-3.13
runtime above, released through the tag-driven [release process](releasing.md).
It is not a claim that the gates below are closed. Gate 2's clean remote CI on
the exact tagged revision is enforced by the release workflow itself, which
re-runs the full suite against each built wheel before publishing.
Gate 1 cannot be completed on a single machine: it needs a separate
load-generator host. Gates 3 to 5 are post-1.0 feature and platform work.

## Remaining release and specification gates

1. **Formal performance acceptance.** Dedicated server/load machines, pinned
   server cores, five 60-second samples after 30-second warmups, <=3% spread,
   fixed-rate latency and representative workload comparisons. The local M0
   run exceeded the spread ceiling and was load-generator constrained. It
   establishes neither the 60% target nor the 40% stop threshold result.
2. **Candidate qualification.** Finish immutable candidate soak/failure/fuzz
   runs, compare resource use and shutdown under load, validate the final Docker
   image and complete clean remote CI for the exact source revision.
3. **Fast path and scaling.** Complete the Rust-driven/batched Tier 1 execution
   path and multicore performance evidence. Free-threaded CPython 3.14t is
   built and fully tested in CI, the native encoder is safe against concurrent
   mutation there, and sync handlers scale across cores in one worker
   ([API](application-api.md#free-threaded-python)). Async handlers can run on
   several event loops per worker (`loops`), which nearly doubled a Pydantic
   route on three cores in one process.
4. **Broader framework surface.** Native JWT verification with static and
   remote, auto-refreshing JWKS is implemented
   ([controls](production-controls.md#jwt-bearer-authentication)), with a
   Entra ID preset for single tenants and explicit multi-tenant lists (up to
   1024 tenants); accepting any tenant without a list is deliberately out of
   scope. Streaming responses and
   server-sent events are implemented ([API](application-api.md#streaming-responses-and-server-sent-events)).
   WebSockets are implemented natively ([API](application-api.md#websockets)),
   with opt-in `permessage-deflate`.
   Native gzip and Brotli response compression, including streamed responses,
   is implemented ([controls](production-controls.md#response-compression)).
   Native static files with conditional, range and precompressed responses are
   implemented ([API](application-api.md#static-files)). FastAPI-style
   `@app.middleware("http")` wraps Python routes without re-encoding their
   bodies ([API](application-api.md#http-middleware)). Route names with
   `url_path_for`/`url_for` and the `response_model_*` serialization options
   are implemented ([API](application-api.md#route-names-and-urls)), as is
   mounting a Bessemer sub-application
   ([API](application-api.md#mounting-applications)), as are mounted ASGI
   apps, standard ASGI middleware and lifespan state, which run off the
   native fast path ([parity](fastapi-parity.md#asgi-middleware)). FastAPI's security dependencies (HTTP
   Bearer/Basic, OAuth2 bearers, API keys and the password form) are
   implemented with OpenAPI entries ([API](application-api.md#security-schemes)),
   as are `Security()` scopes and `SecurityScopes`.
   OpenTelemetry tracing (W3C Trace Context propagation and native OTLP/HTTP
   JSON span export, [controls](production-controls.md#opentelemetry-tracing))
   is implemented; OTLP metrics and logs, gRPC/protobuf export and WebSocket
   spans are not. Shared rate limits through Redis are implemented
   ([controls](production-controls.md#shared-limits-through-redis)); Redis
   Cluster/Sentinel and a distributed cache are not. Native I/O clients, extension
   ABI and FastAPI migration/parity remain incomplete. Toward
   FastAPI parity, an in-process `TestClient` over the real native server and
   `dependency_overrides` are implemented ([API](application-api.md#testing)),
   as are natively decoded forms and file uploads
   ([API](application-api.md#forms-and-file-uploads)), as are WebSocket test
   sessions ([API](application-api.md#testing)), as are disk-spooled uploads
   larger than `max_body_size` (`max_upload_size`,
   [API](application-api.md#forms-and-file-uploads)); spooling covers
   `multipart/form-data` file routes only. A
   [parity assessment](fastapi-parity.md) compares every public FastAPI
   name with Bessemer's; FastAPI's names and options for routers, routes,
   parameters, responses, request data, exception handlers, middleware
   classes, `status`, `jsonable_encoder` and `StaticFiles` are implemented,
   as are `FastAPI` (an alias of `App`), Starlette path converters
   (`{name:path|int|float|uuid|str}`), `Body(embed=True)` with several
   body parameters per route, ASGI middleware, ASGI mounts and lifespan
   state. What remains is listed there, and fails at startup.
5. **Broader release assurance.** Protocol suites, FFI sanitizer/concurrency work,
   sustained deployment/failure evidence and Windows/macOS/arm64 and
   free-threaded release wheels remain open. No stateful production service is deployed.

## Continue from repository evidence

Start with this record, [the application API](application-api.md),
[supervision](supervision.md), [native controls](production-controls.md), Git
history and [the original specification](technical-specification.md). The
specification's embedded architecture/lifecycle/roadmap diagrams were not
recoverable from its plain-text export. Work recalled from 3 November was not
recovered; this is a reconstructed implementation with new evidence.
