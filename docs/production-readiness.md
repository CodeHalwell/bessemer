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

## 8 November 2026: GIL-disabled CPython 3.14 qualification

Merged local main `75a64bf` and built/installed a release `cp314t` wheel in a
private CPython 3.14.7 free-threaded venv. With `PYTHON_GIL=0`, the interpreter
reported `Py_GIL_DISABLED=1` and the GIL stayed disabled after importing Bessemer
and pydantic-core and after each repeated stress batch.

Fixed two free-threaded model-list gaps: an absent exact-list guard could
segfault on non-list inputs, and model dictionaries previously declined native
encoding. Dictionaries and nested lists now retain safe snapshots under their
container critical sections; existing ordering/default/fallback tests and new
concurrent mutation stress pass. No race was reproduced in the native sync
pool, invocation completion, deferred background delivery, adaptive admission
or disconnect watch. New HTTP stress covers both event loops, one/two loop lanes,
ContextVars and per-request State, ordered background work and repeated receive
disconnects. Existing cancellation/admission/delivery checks were repeated.
The process-wide request-cycle GC measurement now runs in a subprocess with
its original workload and threshold, avoiding unrelated delayed garbage from
earlier suites.

Actual validation on the installed wheel: full native check passed with **242
Rust tests passed (2 ignored), 1,024 Python tests run (1,013 passed, 11 skipped),
and 23 benchmark-harness tests passed**, plus formatting, warnings-denied Clippy,
Ruff and shell syntax. The focused model/threading/GC set passed 43 tests; ten
repeated stress batches passed 250 test executions with the GIL disabled
throughout. The focused set also passed with `PYTHON_GIL=1` (one expected skip).
Skips cover unavailable Redis, Windows-only supervision and an unset forced-loop
configuration. This qualifies this Linux/interpreter combination, not all
free-threaded platforms or versions; no throughput/M0 performance acceptance is
claimed. Existing lean `current_request` metadata remains incomplete (template
path/empty path parameters without a Request argument), independent of threading.
See the [qualification report](../benchmarks/results/2026-11-08/free-threaded/report.md)
for commands, environment, findings and limits.

## 8 November 2026: reproducible feature and concurrency matrices

Merged main `73a1032`. The former local-only FastAPI comparison tooling now
lives under `benchmarks/compare`: the shared feature app, one-core feature
runner, Bessemer/FastAPI/Axum concurrency runner, Markdown summariser and
explicit same-user CPU-isolation helper. Paths, interpreters, executables,
ports and CPU lists are configurable; no local checkout paths are embedded.
The feature app preserves the dependency, validation, middleware, Request,
forms, exception and background workloads. The concurrency defaults cover
1/2/4 workers, 64/256/1024 connections and JSON/1 ms I/O routes. JSON lines
include HTTP-200 throughput, total throughput, latency, status/error counts
and CPU placement. Successful throughput is calculated from oha's elapsed
seconds, excluding non-200 and aborted/failed requests. Duration aborts remain
visible in the error distribution. Missing latency stays null, not zero.

Ten new harness tests cover configuration, payloads, metrics (including empty
samples), startup cleanup, caller-relative paths and affinity restoration with
thread-ID reuse/failure checks. Isolation was not applied to this shared host;
restoration is snapshot-based and explicitly does not restore threads created
after the snapshot. See [comparison instructions](../benchmarks/compare/README.md).

Installed CPython 3.13 wheel validation: full native check passed with **243
Rust tests passed (2 ignored), 1,024 Python tests run (990 passed, 34 skipped),
and 33 benchmark-harness tests passed**, plus formatting, warnings-denied
Clippy, Ruff and shell syntax checks. Functional smoke runs completed **68
feature samples** across both frameworks and MW=0/1, with the expected 200s
and intentional `/raise` 404s. The **54-cell concurrency grid** completed
without non-200s or transport failures apart from duration aborts; one short
0.2 s FastAPI I/O cell had no completed responses and correctly reported zero
OK throughput/null latency. Its 1 s recheck returned 200s and measured latency.
The summariser consumed these JSON lines successfully. These shared-host smoke
rates are not performance evidence; instruction counts were not remeasured
because this round changes tooling/tests, not the production request path.
The formal M0 gate remains open.

## 8 November 2026: shared-listener test under CPU contention

Hardened `workers_sharing_a_listener_split_the_connections` without changing
production listener behavior. Every worker announces readiness only after
polling accept and registering its waker. A 1 ms simulated request pause after
each accept gives an idle worker a scheduling opportunity on a contended CPU;
a persistent multishot accept would still reserve the backlog for its original
worker. Up to three fresh connection bursts tolerate an unlucky scheduling
split, while every successful attempt must accept all 300 connections and give
every worker a nonzero share. Workers have a bounded wait and are joined on
unsupported-io_uring exits. No minimum percentage or even split is asserted.

The barrier/retries alone reproduced deterministic starvation `[171, 0, 129]`
when pinned to one loaded CPU; the final test passed **30 consecutive runs**
with all workers and a separate short-lived busy loop pinned to one CPU. It
also passed in the full native check recorded above. This is regression-test
stability qualification, not a throughput improvement or a change to scheduling
semantics.

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
handler runs inside `Call`, a Rust coroutine (and a subclass of
`Invocation`, so it is the request's invocation too) that calls the handler on
the task's first step with a bare request copied from a per-route template set as
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

### Default event loop `auto` (6 November 2026)

Implemented scope: `event_loop` accepts `"auto"`, now the default for
`App.run`, `App._run`, `Worker`, the CLI and the settings. `"auto"` resolves to
the Rust loop on Linux and macOS when `bessemer._loop` imports and `loops` is 1,
and to asyncio otherwise, without a warning (`config.resolve_event_loop`).
`"bessemer"` is unchanged (warning and asyncio fallback off Linux and macOS,
and an `ImportError` if the loop is broken); `"asyncio"` is unchanged and is the
documented escape hatch. Free-threaded builds with `loops` above 1 keep the
asyncio layout under `"auto"`. `io_backend="io_uring"` applies to an
auto-resolved Rust loop; with auto resolving to asyncio it gives today's warning
and epoll fallback. The supervisor's `listening` line names the resolved loop.
Also in this change: the Rust loop times handles in debug mode and logs slow
ones on asyncio's logger in asyncio's format (one relaxed atomic load per
handle with debug off), and debug handle `repr` ends with `created at`.
Benchmark scripts that meant asyncio under the name `bessemer`
(`benchmarks/compare/servers.py`, `benchmarks/profile/instructions.sh` and
`profile_route.sh`) now say `asyncio` explicitly. The M0 qualification scripts
(`benchmarks/qualify_http.py` and others that start `bessemer run` without a
loop) were not changed and now run the default loop.

Evidence: the compatibility sweep of 6 November 2026 (Linux, CPython 3.13, one
worker; anyio, httpx, aiohttp, SQLAlchemy, redis, asyncpg, OpenTelemetry, TLS,
subprocesses, signals, cancellation) found the Rust loop identical to asyncio
except slow-callback warnings (closed here). CPython's asyncio tests: 2011 of
2011 on a rerun. Validation run for this change, Linux x86_64, CPython 3.13,
installed wheel: Python suite OK with the new default and with `BESSEMER_EVENT_LOOP=asyncio` (counts below); benchmark tests 21 OK; `cargo fmt --check`, `cargo
clippy --workspace --all-targets --locked -D warnings` and `cargo test
--workspace` clean (the loop crate's `workers_sharing_a_listener_split_the_connections`
failed once under load, `[2, 298, 0]`, and passed on three reruns). `ruff
check` and `ruff format --check` pass after rebasing onto the lint-fix commit. A CI job now forces `BESSEMER_EVENT_LOOP=asyncio`.

Review follow-up: `bessemer._loop` no longer touches
`asyncio.DefaultEventLoopPolicy` at import (deprecated in 3.14, removed in
3.16); `EventLoopPolicy` is built on first use with the warning silenced, and
`config.native_loop_available` treats any exception from the import as
unavailable, so `auto` falls back instead of failing under `-W error`. Checked
on a free-threaded CPython 3.14.6 build: `-W error` import, `auto` resolution
and a default `TestClient` request work. `tests/python/event_loop_env.py` makes
`BESSEMER_EVENT_LOOP` set the default for `App.run`, `App._run`, `Worker` and
`DEFAULTS` for the whole suite (loaded by `test_00_event_loop_env.py`; no library
change): a forced-asyncio run logged 60 supervisor `listening` lines with
`event_loop=asyncio` and a `bessemer` run 60 with `event_loop=bessemer`. The
supervisor logs the resolved loop without importing the loop module. Final
counts: 862 Python tests, OK, 28 skipped, in the default run and with
`BESSEMER_EVENT_LOOP=asyncio`; full `scripts/check.sh` passes.

Remaining gaps: macOS, Python 3.12 and 3.14, free-threaded builds, `loops` above
1 with `"bessemer"`, soak and descriptor growth, HTTP/2 clients and the
`test_runners` SIGINT tests under load (three timeouts in one loaded sweep run,
not reproduced) are not covered by this validation. Overload behaviour differs:
at 1,024 connections the asyncio worker answered 503 where the Rust loop did not.

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

### Python-side request overhead on the Rust loop

Implemented scope: internal changes that cut Python work per request, mostly
on `event_loop="bessemer"`; behaviour and the public API are unchanged.

- Native invoker: the bare `Request` is allocated with its type's `tp_alloc`
  (checked per call to be `object.__new__`'s; otherwise the Python route) and
  given a copy of the route's template dict. A lean `Call` reports whether
  its eager start finished, so `task.done()` is asked only on the `_invoke`
  path and for task factories. `Worker._accepting` is a property backed by a
  native `Switch` that the invoker reads per job, so a handler that stops
  accepting still stops the next job of its batch. The loop's task factory is
  read directly when `get_task_factory` is asyncio's own. The Rust loop
  answers `is_running` natively, which every eager `Task` asks.
- Body routes: a lean async route whose only non-scalar input is one Pydantic
  body model with a native body plan (no `Body()` validator, embed, body
  containers or `model_validate` override) now runs on the native invoker.
  The model is built from the raw body with the same plan
  `bridge::python_arguments` uses; anything the plan cannot build exactly
  (values Pydantic would coerce or reject, `null`, a missing body) takes
  `_invoke`. 415, 413 and invalid-JSON 422 responses are produced by the
  server before either path, so they are unchanged.
- Streaming on the Rust loop: when the stream pump runs on the loop's thread,
  a read returns buffered bytes or a bare future the producer fills, instead
  of a task and a concurrent future per chunk; a stream without background
  tasks or cleanup carries its end in the last batch. Callbacks queued from a
  Tokio task on the loop's own thread wake the loop through its driver instead
  of the self-pipe. Building a `Response` with a media type no longer raises
  and catches `KeyError` in `Headers.__contains__`, validates header values
  with one regex and remembers up to 512 short validated pairs, and
  `background_tasks` imports only when a response carries tasks. The asyncio
  worker and several loops per worker keep the old stream path; ASGI
  middleware still sees a plain end of stream.

Validation actually run: `BESSEMER_REQUIRE_NATIVE=1 ./scripts/check.sh`
passed against the installed wheel (714 Python tests, 146 Rust unit tests,
clippy and ruff). `NativeInvokerTests` and the HTTP streaming tests now also
run on the Rust loop. New tests cover request independence, a handler that
stops accepting mid-batch, an installed task factory, `is_running`, a
backpressure test for async and sync generators with a disconnect, and
`tests/python/test_native_body.py`, which compares 31 body cases (content
types, coerced, invalid, empty, `null`, too large, Optional, embedded,
validators) byte for byte against the Python path on both loops, and checks
`current_request`, `current_task` and exception handlers. Exploratory
callgrind counts at one connection on the Rust loop against a build of
`f37faf5`: `/json` 39,689 to 37,833, `/items/7` 43,620 to 41,688, `/echo`
with a one-item order 86,287 to 60,436 and with a twenty-item order 311,347
to 286,555 (most of the remainder is native body parsing and encoding). A
four-chunk stream fell from about 363k to 194k, and a sixteen-chunk stream
that awaits between chunks from about 890k to 585k (whole-run totals against
a build of #62's branch, since the counter reset is unreliable
for streams). Remaining gaps: `task.cancelled()` is still asked as a lean
task finishes; `asyncio.Task` construction itself (about 7k per request) is
unchanged; a generator that awaits between chunks still costs about 27k per
chunk, and a sync iterator one executor hop per chunk. This is exploratory
evidence; Python routes remain Tier 2.

Follow-up around the eager `Task`: a lean route's `Call` extends
`Invocation`, so each lean request allocates one object for both (the
worker's `_lean_response`/`_lean_failed` and the lane's dicts receive it; the
`_invoke` path keeps a plain `Invocation`); the reused eager task options
replace their `context` entry in place (None between calls) instead of
adding and deleting it, which had made the dict resize every few requests;
and the outbox keeps its buffer between drains. Callgrind at one connection
on the Rust loop against `149ad8a`: `/json` 37,723 to 36,906, `/items/7`
41,767 to 40,798. `NativeInvokerTests`, the event loop and streaming tests
pass on CPython 3.12, 3.13, 3.14 and 3.14t.

### io_uring socket I/O (phase 5)

Implemented scope: `io_backend` is a new, additive server setting (`App.run`,
`bessemer run --io-backend`, `BESSEMER_IO_BACKEND`, `[tool.bessemer]`) with
`"epoll"` (the default) and `"io_uring"`. With `"io_uring"`, a worker that
serves connections on the Rust loop (`event_loop="bessemer"`, one loop) accepts,
reads and writes its sockets through one io_uring per loop thread
(`crates/bessemer-loop/src/uring.rs`): multishot recv into a 4096 x 2 KiB provided-buffer ring (each buffer is copied
out and returned at once), and the sends queued during a pass of the runtime's
tasks submitted together with completion processing in one `io_uring_enter`.
The ring uses `SINGLE_ISSUER | DEFER_TASKRUN | TASKRUN_FLAG`; a pump task on
the loop's embedded runtime runs the submit/complete cycle and sleeps on an
eventfd registered with the ring (the ring descriptor itself was not reported
readable to epoll under `DEFER_TASKRUN` on Linux 6.18). The transport takes
either listener (`transport::Listener`) and the HTTP/1, HTTP/2, TLS and
WebSocket paths run unchanged over the io_uring stream. Recv pauses above
64 KiB of unread input, a write takes at most the room left under 256 KiB
unsent, and a dropped stream keeps sending what it wrote for up to 30 s, as
the kernel would for a closed epoll socket, while still holding its
connection slot, so `max_connections` and the graceful drain count it. If the ring cannot
be set up (seccomp `EPERM`, `kernel.io_uring_disabled`, a kernel before 6.1 or
without multishot recv or buffer rings), the worker logs the reason and
serves on epoll. With asyncio or several loops it logs a warning and serves on
epoll. `deploy/seccomp-io-uring.json` is Docker's default seccomp profile plus
the three io_uring system calls, documented in `docs/deployment.md`. The
default, the FastAPI-compatible API and existing settings are unchanged.

Validation actually run: `BESSEMER_REQUIRE_NATIVE=1 ./scripts/check.sh`
passed against the built wheel on top of #64 (697 Python tests, the Rust suites with 11 new io_uring tests, clippy and ruff). New Rust tests run the
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

Several workers. The first version accepted with a multishot `accept`. The
workers share the supervisor's listening socket, and the kernel gives each new
connection to the first waiting accept request, so with two workers one took
every connection (found by the current-main comparison, #67: 606 CPU ticks
against 15). The io_uring listener now accepts when the runtime's reactor
reports the socket readable, as the epoll path does, and keeps io_uring for
reads and writes; pending connections wait in the kernel backlog, and the
256-connection accept queue limit went with the multishot accept. A new Rust
test with three workers on one listener failed before the change (0, 0 and 300
of 300 connections) and passes after it. Measured with
`benchmarks/profile/io_backend.sh` (now with `WORKERS`), see
`benchmarks/results/2026-11-05/io-uring-workers`: two workers on two cores
split the load evenly (0.92 to 0.98 of a core each) and io_uring served 172k
against epoll's 162k req/s at 64 connections (11.1 against 11.9µs per request)
and 169k against 156k at 1024 (11.2 against 12.4µs). Three workers could not
be saturated with the client on the one remaining vCPU; there the two backends
were level and every worker got connections.

### Pre-release security assessment

Recorded in full in [security-assessment.md](security-assessment.md), with a
severity for every finding. Implemented: every accepted connection (HTTP/1.1,
the hyper upgrade hand-off and so WebSockets, and HTTP/2) is wrapped in
`http1::SendDeadline`, so a write, flush or shutdown that makes no progress
for the longest request timeout closes the connection; before, a client
that stopped reading held its slot and response bytes indefinitely (M-1).
Progress is any wake from the underlying stream, so buffered transports
(io_uring, TLS) draining to a slow reader are not cut off. Chunked bodies
with control characters in an extension or trailer are refused (L-3), and the
dev/CI `opentelemetry-api` pin is 1.45.1 (L-4). Validation: Rust tests for a
peer that stops reading, a slow reader that keeps draining, and a buffered
flush that stays pending for longer than the deadline while making progress;
the full Python suite (723 tests) on the built wheel; a randomised parser
and chunked-decoder fuzzer (`src/http1_fuzz.rs`, over 100 million iterations
clean); seventeen smuggling and framing probes against a running server; and
`cargo audit` and `pip-audit`. A probe with 40 pipelined 2 MB responses and a
client asleep for 8 s kept the connection open on the old build and closed it
3 s into the stall on the new one. Remaining gaps: HTTP/2 streams stalled by
flow control while the socket stays readable are not timed out (L-1);
dependency audits do not run in CI (L-5); key sets without `kid`s try every
key for an unknown token (L-6). No external audit or coverage-guided fuzzing
has been done.

At `e70b096`, a follow-up assessment built and installed a macOS arm64
CPython 3.13 wheel, passed 26 focused JWT/static/HTTP/2 tests and ran two
native probes. An ID-token-shaped, locally signed JWT passed a `Principal`-only
route under `JWTAuth.entra` when browser and API audiences shared one app ID;
an explicit scope check rejected it. One HTTP/2 peer held eight 4 MiB responses
without window updates, exhausted the default response budget and made later
requests return `503` until it disconnected. Code review also found writable
Python buffers aliased as immutable Rust slices in the opt-in Linux event loop,
and a conditional symlink replacement race when untrusted actors can write the
static directory. At that revision these were open, as were WebSocket Origin
responsibility, kidless-JWKS CPU amplification and missing scheduled audits. Fresh
RustSec and pinned Python lockfile audits found no known advisories. The current
image was not rebuilt or OS-scanned. Details and severity are in
[security-assessment.md](security-assessment.md).

On 6 November the candidate was changed to enforce an Entra access-token
profile, bound kidless key searches, expire stalled HTTP/2 connections, copy
mutable event-loop send buffers, pin and safely open static files, require an
explicit browser WebSocket origin allowlist, reject duplicate credential
headers and run dependency audits in CI. The rebuilt macOS wheel passed 30
focused tests and the ID-token and stalled HTTP/2 probes; 12 Rust JWT tests and
focused HTTP/2 and static-file tests passed. The mutable-buffer event-loop
regression passed on macOS. The Linux event-loop crate passed a cross-target
check and Clippy, but its new runtime regression was not run on Linux locally.
HTTP/2 closes the whole connection when any stream stalls; the
16 KiB progress granularity can also close very slow but moving downloads.

### Workers from app.run

Implemented scope: `App.run(workers=...)` is a new, additive argument. The
default, 1, serves from the calling process as before. 0 (one worker per usable
CPU, `cpu_quota_count`) and 2 to 256 run the existing `Supervisor` from
`app.run()`. The application is found as a module-level variable and handed to
the supervisor as `module:variable`; a script run as `python script.py` is
named by its file stem and its directory goes first on the workers'
`sys.path`, ahead of the working directory, and `python -m package.module` uses its module name. Server
settings are validated, TLS files and `upload_dir` checked and the app built in
the parent before any worker starts; `metrics_path` with more than one worker
is refused as with the CLI. A call to `app.run()` while a supervised worker
imports the application (a missing `if __name__ == "__main__":` guard) fails
with a configuration error (exit 78, not retried) naming the guard. Supervisor
recovery settings keep their defaults on this path, and `BESSEMER_*` and
`[tool.bessemer]` are not read.

Validation actually run: `tests/python/test_run_workers.py` covers the target
lookup (module, script, `-m`, a launcher's `__main__` alias, a script shadowed
by a sibling package, refused non-module app), argument checks, settings
handed to the supervisor, metrics refusal, a failed supervisor raising, and a
script started from another directory holding a same-named decoy module
serving requests from two worker
processes and exiting 0 on `SIGTERM`. Checked by hand: an unguarded script
fails at once with the guard message, and `workers=0` starts one worker per CPU
and stops cleanly on `SIGINT`.

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
[changelog](../CHANGELOG.md#100rc1---2026-11-05): `bessemer.__all__`, the
public FastAPI/Starlette-style submodules, CLI flags, `BESSEMER_*` settings
and `[tool.bessemer]` keys. The Rust event loop and the io_uring backend are
experimental: their setting names are stable, but their behaviour is not
covered. After the `auto` change the Rust loop is the default on Linux and
macOS with one loop per worker (`event_loop="asyncio"` opts out); io_uring
remains opt-in. The 1.0 line is defined as that API freeze on the qualified
Linux x86_64 runtime (CPython 3.12, 3.13 and free-threaded 3.14t), released
through the tag-driven [release process](releasing.md). It does not claim that
the gates below are closed.

The release workflow covers gate 2's clean remote CI on the exact tagged
revision itself: it re-runs the full suite against each built wheel before
publishing, and can be run by hand as a dry run. Gate 1 cannot be completed on
a single machine because it needs a separate load-generator host. Gates 3 to
5 are post-1.0 feature and platform work.

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
   Bearer/Basic/Digest, OAuth2 and OAuth2 bearers, OpenID Connect, API keys, `SecurityBase` and the password form) are
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
   name with Bessemer's; FastAPI's names and options for routers, routes
   (including `redirect_slashes`, `root_path` and `root_path_in_servers`),
   parameters (including `pydantic.Field(...)`, `default_factory`,
   `multiple_of`, `strict`, `discriminator`, `validation_alias`,
   `serialization_alias` and `Annotated[Model, Query()]` query models),
   responses (including `FileResponse` on the native static-file path with
   byte ranges and conditional requests), request data, exception handlers
   (including `ResponseValidationError`), middleware
   classes, `status`, `jsonable_encoder` and `StaticFiles` are implemented,
   as are `FastAPI` (an alias of `App`), Starlette path converters
   (`{name:path|int|float|uuid|str}`), `Body(embed=True)` with several
   body parameters per route, ASGI middleware, ASGI mounts and lifespan
   state. What remains is listed there, and fails at startup.
5. **Broader release assurance.** Protocol suites, FFI sanitizer/concurrency work,
   sustained deployment/failure evidence and Windows/macOS/arm64 release
   wheels remain open. Free-threaded 3.14t wheels are built and re-tested by
   the release workflow. No stateful production service is deployed.

## Continue from repository evidence

Start with this record, [the application API](application-api.md),
[supervision](supervision.md), [native controls](production-controls.md), Git
history and [the original specification](technical-specification.md). The
specification's embedded architecture/lifecycle/roadmap diagrams were not
recoverable from its plain-text export. Work recalled from 3 November was not
recovered; this is a reconstructed implementation with new evidence.

## 6 November 2026 concurrency and efficiency pass

Implemented locally, with the frozen public names/settings unchanged:

- The embedded Rust loop preserves sub-millisecond waits. One-shot kqueue
  timers on macOS and timerfd on Linux handle the final 10 ms before a timer
  deadline. More distant deadlines first wait through Tokio and return to the
  core for recomputation, avoiding native arm/disarm syscalls on every busy I/O
  iteration. Native timer cancellation, stale readiness and unavailable-native
  fallback are covered by focused Rust regressions. Standalone loops retain
  the existing poller's millisecond rounding.
- Response-model JSON key prefixes are escaped at plan compilation, with
  original key sorting preserved and byte limits checked before prefix copies.
  Validation-only JSON scans visit every value without allocating container
  vectors or sorting/deduplicating results that will be discarded.
- Native-invoker fallback carries an already parsed body into Python argument
  preparation, preserving Pydantic coercion/error handling while avoiding the
  repeated parse/model probe.
- Buffered Python byte responses retain immutable Python storage through
  budget-owned transport bytes. The payload and its byte reservation survive
  the Python response envelope and every transport clone. HEAD validates its
  response while avoiding retention of unsent payload bytes.
- WebSocket callbacks delivered on their owning loop avoid an extra thread-safe
  scheduling hop; cross-thread callbacks retain loop scheduling.
- An optional `ag-ui` extra and [example adapter](ag-ui.md) use the official
  SDK for typed input/events and standard HTTP SSE. The example's duplex
  WebSocket framing supports bounded live input, cancellation and repeated
  runs, with cleanup completed before terminal events. Its input/cancel control
  frames are explicitly application extensions. Browser origin allowlists are
  exact; native origin protections are unchanged.

Actual validation: the installed CPython 3.13 macOS wheel passed 748 Python
tests (14 platform/optional skips); 179 Rust tests passed (one ignored), including
the four new timer regressions and original M0 binary tests. Formatting,
warnings-denied workspace Clippy, Ruff and complexipy passed. The first
`BESSEMER_REQUIRE_NATIVE=1 ./scripts/check.sh` run reached three Linux-only
benchmark-harness failures on macOS (`/proc` and `taskset`). Those tests now have
platform guards, while their portable assertions still run; the focused harness
rerun passed 18 tests with three platform skips. Installed Python package files
match the source byte for byte. AG-UI SDK-covered CI jobs require the SDK/native
wheel rather than silently skipping, preserving the separate Pydantic 2.10.6
minimum job. The timer crate also passed a Linux x86_64 all-target cross-check;
Linux runtime execution was not performed here.

Recorded measurements are in `benchmarks/results/2026-11-06/concurrency-pass/`.
A 1 ms Rust-loop sleep fell from median 2.288 ms to 1.048 ms, and 20 ms from
22.066 ms to 20.097 ms. The broad matrix completed 195 samples and 6,992,391
responses/messages with no reported errors or unexpected HTTP statuses. Its
throughput results were mixed; closely paired JSON/dependency follow-ups changed
some conclusions. Ramped 1,024-connection I/O/fan-out checks passed on both
candidate modes and FastAPI; simultaneous connection-burst attempts produced
resets on all three. The initial timer tuning run, failed burst attempts,
controls, CPU costs, ranges and native hashes are retained separately. This
pass does not close formal M0, Linux timer runtime, sustained-load or
free-threaded scaling qualification.

Further candidates identified from current code, not implemented in this pass:
WebSocket sessions remain on the lifespan loop when HTTP uses several lanes;
upload spooling still crosses into the blocking pool for each incoming frame;
sync streaming still offloads each iterator step; ordinary asyncio Task
construction and native completion bookkeeping remain per request. These need
separate workload measurements and lifecycle/affinity checks before changing
their scheduling or buffering. Large native JSON results still copy the codec
scratch output into an owned Vec; investigate transfer above the retained-buffer
limit without sacrificing small-response scratch reuse.

## 6 November 2026 CPU and multicore pass

The local follow-on adds `App(executor=...)`, `App.run_cpu(...)` and
`bessemer.executors` with bounded thread/process pools and a custom executor
protocol. Actual GIL state selects the automatic backend. Spawned processes
handle pure-Python CPU work on GIL builds; threads share memory on GIL-disabled
builds. Lazy pools divide automatic CPU sizing across supervised workers;
mounted applications share the automatic pool. Saturation returns 503 with
`Retry-After`, and deadlines/cancellation retain capacity until work actually
finishes or a queued function is skipped. Worker shutdown waits for lane
finalizers before closing CPU executors. Python 3.14 inherited thread contexts
are cleared at pool-thread creation, with per-job contexts copied separately.

CPU entitlement now combines affinity, process CPU counts, cgroup v1/v2 quota
ancestors, delegated controller mount paths and cpuset intersections. The
additive `sync_workers` setting bounds synchronous handler threads through the
CLI, environment, TOML and `App.run`; zero preserves eight on GIL builds and
uses the available CPU share on a GIL-disabled build.

Stateless WebSocket sessions use the least-loaded existing worker lane when
`loops > 1`. Lifespan/state/app-scoped resources preserve first-loop affinity.
Submission and shutdown transitions reserve pending work together. Upload
spooling coalesces immediately available file frames into budgeted 64 KiB
writes, flushing before waiting for more network input. Staging buffers, files
and byte reservations stay owned by blocking writes through cancellation.
The deterministic 1 MiB fragmented-body regression reduces 149,797 writes to
16 for 7-byte frames and 257 to 16 for 4,093-byte frames; large-frame writes
remain at nine. This is a write-count result, not HTTP throughput acceptance.

The [multicore harness](../benchmarks/multicore/README.md) compares installed
GIL process topologies, a true 3.14t loop topology and FastAPI using CPU, sync,
I/O, fan-out, mixed and offload workloads. The separate
[GPU workspace](../benchmarks/gpu/README.md) adds a transfer-inclusive wgpu
numerical probe plus an isolated cuTile reference example. GPU dependencies do
not enter the runtime wheel. There is no bundled GPU executor, Ray/Dask backend
or Tokio-native CPU future; Python routes remain Tier 2.

Validation and measurement artifacts for this follow-on are recorded in
`benchmarks/results/2026-11-06/multicore-pass/`. Linux cgroup behavior is covered
with filesystem fixtures here; actual restricted-container and NVIDIA GPU
execution remain separate qualification work.

The optional Metal probe ran on the Apple M5 Max. With cached device/pipeline
and buffers, transfer-inclusive 1024×1024 FP32 multiplication took median
6.798 ms versus 19.351 ms for the single-thread SIMD Rust CPU reference (2.85×).
The 32×32 and 257×257 cases were slower on GPU. Cold device/pipeline setup was
121.306 ms; all warmup and measured outputs passed element-wise tolerance.
These numerical results do not measure HTTP or Python-handler acceleration.

The main multicore matrix completed 144 measured and 144 warmup samples, with
3,296,806 measured responses, zero errors/unexpected statuses and no remaining
tracked processes after 24 server sessions. On CPython 3.14t, one to four loops
raised inline CPU throughput from 716 to 2,255 requests/s (3.15×), synchronous
CPU from 718 to 2,496 (3.47×), and CPU offload from 724 to 2,509 (3.46×).
The GIL candidate's CPU throughput was broadly similar to the earlier local
baseline. Twenty-four of 72 two-round groups had at least 10% throughput spread.
These are unpinned process/loop configurations on an 18-CPU Mac, not enforced
vCPU quotas. The [report](../benchmarks/results/2026-11-06/multicore-pass/report.md)
retains workload/configuration medians, CPU costs, variability and build hashes.

Independent review then corrected custom executors returning normally after
timeout cancellation and inherited first-request contexts in idle synchronous
handler/default-executor threads on Python 3.14t. The final wheels use an empty
base thread context, preserving explicit per-call copies for sync handlers and
`asyncio.to_thread`. Original measured wheels/environments remain unchanged;
final-wheel provenance is in `manifest-final.json` alongside the main matrix.

Final installed-wheel validation passed `BESSEMER_REQUIRE_NATIVE=1
./scripts/check.sh` on CPython 3.13: 188 Rust tests passed (one ignored),
814 Python tests passed (11 skipped), and 18 benchmark tests passed (three
skipped), with formatting, Clippy and Ruff passing. The final GIL-disabled
3.14t wheel passed 213 focused tests (one skipped), including CPU pools,
request-context collection, cancellation/admission, uploads, WebSocket lanes,
native invocation and event-loop coverage. Actual GIL state stayed disabled
through imports and execution; both final installed Python packages matched
source byte for byte. New CPU/executor modules and focused tests passed
complexipy at 15. The first free-threaded check driver could not spawn from
stdin; its failed log is retained separately from the passing guarded-file run.

The final Python-only fixes were measured separately in 16 samples and 16
warmups: 47,679 measured responses, zero errors/unexpected statuses and eight
clean server-process trees. Final free-threaded offload scaled from 748 to
2,455 requests/s with one to four thread/loop slots (3.28×), and sync CPU from
768 to 2,411 (3.14×). The raw `final-scaling.json`, command, separate logs and
final build hashes are retained beside the original matrix.

## 6 November 2026 matched-resource cost pass

The follow-on [cost/capacity work](performance-cost.md) batches synchronous
stream iterator steps within 64 items, a 1 ms between-step quantum and the
remaining buffer allowance. The first batch stays one item; coalesced wakeups
publish ready bytes before a later step blocks. Pending thread bytes and loop
bytes share the reservation count. Empty/infinite iterators yield executor
capacity, and close waits for running iteration before finalizers. A
4,096-chunk fixture changed from 4,097 executor submissions to 65; a 16-chunk
fixture changed from 17 to two, with identical output. Native streaming creates
completion channels only for pending steps. All six immediate-step fixtures
changed from one channel per step to zero; the pending callback keeps one.

The new [efficiency harness](../benchmarks/efficiency/README.md) reuses existing
application/response contracts under identical local Linux CPU/memory limits,
with the load generator outside the server scope. Kernel readback confirmed two
CPU cores, cpuset 0–1, 512 MiB `memory.max`, 256 process slots and init at PID 1.
Docker also recorded a 1 GiB combined RAM/swap allowance. The diagnostic p99
target is 50 ms with explicit delivery/error conditions; these are short,
two-second samples on local OrbStack aarch64, not a production soak or the
user's deployment SLO.

The [report](../benchmarks/results/2026-11-06/efficiency-pass/report.md) records
80 samples and 1,261,517 completed responses with zero transport/status/body
length errors. Forty-five tested rates met the diagnostic objective; overload
and missed offered load correctly failed other points. All 12 server sessions
cleaned up normally with no remaining tracked descendants. The candidate Rust
loop used 1.16–3.41× less CPU per within-objective response than FastAPI at six
matched passing low-load points. PSS differences were modest. Repeated
sync-stream runs passed at 4,000 requests/s on the Rust loop with p99 10.0/5.5 ms;
FastAPI failed at 2,000 with p99 110.6/148.3 ms. Neither candidate loop passed
8,000 reliably, and the default loop had p99 outliers at 2,000/4,000. Their cause
remains to be profiled; sparse grids do not establish exact replica savings.

Final installed validation: 195 Rust tests passed (two ignored), 822 Python
tests passed (11 skipped) and 18 harness tests passed (three skipped) through
`BESSEMER_REQUIRE_NATIVE=1 ./scripts/check.sh` on macOS 3.13. The installed Linux
candidate passed 71 focused HTTP/lifecycle/streaming tests; GIL-disabled macOS
3.14t passed all 33 streaming tests. Final scoped Ruff/format and complexipy
checks passed. The initial format-only interrupted check is retained beside the
passing full check. Baseline/candidate Linux wheels, both macOS ABIs, installed
native/Python hashes, source snapshots, corrected latency records and resource
traces distinguish each measured build. Runtime names/dependencies and Tier 2
classification are unchanged. Native task/future execution and production
cost/performance acceptance remain open.

Final PR review also preserved cancellation of an AG-UI WebSocket session while
its explicitly cancelled run finishes cleanup, and restored conservative main
lane affinity for custom/dict/slots application-state containers. Rebuilt final
macOS 3.13 and GIL-disabled 3.14t wheels each passed 24 installed WebSocket/AG-UI
regressions with no skips. The whole-tree Ruff lint/format check passed.
The [review addendum](../benchmarks/results/2026-11-06/efficiency-pass/pr-final-validation.md)
separates those final checks from earlier full-suite and measured-build evidence.

## 6 November 2026 PR 102 CI repairs

The Linux worker failures came from multiprocessing registering the running
script as `__mp_main__`. Worker target lookup now excludes aliases of the live
`__main__` module before resolving the script file or `python -m` module name.
Focused regressions exercise the multiprocessing import in a fresh interpreter
and in the two-worker HTTP fixture. Lookup helpers also bring the modified
function's cognitive complexity from 31 to 13.

The Windows quota failure was a test fixture comparing `str(Path)` against a
POSIX path. Native `Path` equality now supplies the intended membership on
Windows. A separate temporary-proc fixture checks that the nested quota is
applied only when the process belongs to that group; CPU sizing is unchanged.

AG-UI send failure and receive disconnect could race to cancel the same agent
finalizer. The example now retains and awaits iterator cleanup in its original
context before propagating cancellation, and always closes the active agent
after receiver cleanup. Exhaustion after a terminal event is also protected;
extra events still become a generic agent error after cleanup. Regressions hold
cleanup open to check disconnects, repeated cancellation and ContextVar token
reset. Pre-terminal agent work remains cancellable.

Final focused validation against rebuilt installed wheels: Linux aarch64
CPython 3.13 passed all 51 tests; macOS arm64 CPython 3.13 and GIL-disabled
3.14t each ran 51 tests with one Linux-only skip. The installed application
source matched the checkout on all three environments. Seven release/API tests,
whole-tree Ruff lint/format and changed-function complexipy at 15 also passed.
The full check script and Windows runtime were not rerun locally for these
repairs. Remote CI at `7443014` still records the original failures until the
repairs are committed, pushed and checked on the new revision.

## 6 November 2026 post-merge review corrections

The model-plan compiler now excludes classes with custom construction or
finalization hooks, including nested and inherited hooks. Their request bodies
go through full JSON syntax validation before Pydantic constructs a model, so
malformed trailing bytes and malformed nested values cannot invoke application
constructors before a 422 response. Plain models keep the original one-pass
native parser. The lazy event-loop policy also restores its top-level qualified
name so the class can be pickled for multiprocessing spawn.

The final-pass profiling scripts now divide completed and successful response
counts by the measured `oha` duration. Derived tables were replayed from the
archived raw runs; the raw observations were not rewritten. In the checked
1,024-connection sample, 4,828 attempts had no response, so the HTTP 200
response rate is 45,124.53/s instead of the previously reported 45,451.64/s.
CPU cost per completed response uses the same corrected denominator. This is an
analysis correction, not a new performance run; the formal M0 gate remains open.

The benchmark correction passed two focused tests and replayed all 155
archived result/oha pairs with matching counts. No raw benchmark files changed.

Installed-wheel validation passed `BESSEMER_REQUIRE_NATIVE=1
./scripts/check.sh` on Homebrew CPython 3.13.18 after restoring the original
Rust parser: 221 Rust tests passed
(two ignored), 922 Python tests ran (12 skipped), and 21 benchmark-harness
tests ran (three skipped). Rust formatting, warnings-denied Clippy, Ruff
lint/format and shell syntax checks passed. The first full-check attempt in
a temporary uv-managed CPython 3.13.0 environment could not initialize the
embedded Python standard library (`encodings`) during Rust tests; the
Homebrew-interpreter rerun completed successfully. No Linux, Windows or
free-threaded wheel was rebuilt locally for these corrections.

The first PR head (`46183f5`) added a deferred Rust parse tree and failed the
instruction-count gate on the Pydantic echo route in Python 3.13, 3.14t and
3.15t (about 4% more instructions against a 2% limit). The follow-up restores
the prior Rust parser byte for byte for plain models and excludes custom model
constructors/finalizers from the fast-path plan instead. The performance
workflow's information-only throughput summary now reads the `samples` array
from its benchmark JSON, fixing a separate traceback in that step.
The final wheel moves the same custom-hook guard from the model compiler to its
node dispatcher without changing the parser. It passed 25 model fast-path
tests with current Pydantic and 25 with Pydantic 2.10.6, plus 31 event-loop
tests. Both asyncio and Bessemer loop HTTP repros returned 422 with zero
constructor calls for malformed JSON and 200 with one constructor call for
valid JSON. Current-head Linux, Windows, macOS and performance CI is pending.

## 7 November 2026 Pydantic and async performance pass

Model plans with immutable defaults now prepare a field dictionary once and
copy it for each instance, overwriting supplied fields. Models without defaults
retain their existing dictionary construction. Regressions cover declaration
order, separate instance dictionaries and field sets, omitted defaults and
missing required fields. Custom hooks and unsupported schemas retain their
existing fallback paths; the public API and dependencies are unchanged.

The Rust event loop now reuses a detached ready queue, taking the state mutex
twice per batch instead of once plus once per callback. Callbacks scheduled
during dispatch remain on the next tick, and unrun callbacks retain their order
after an interrupt. Suspended-request completion also removes one redundant
invocation-dictionary lookup. Cancellation and task-release regressions pass.

Rebuilt and installed macOS arm64 CPython 3.13.18 and 3.14t wheels. The final
3.13 wheel passed `BESSEMER_REQUIRE_NATIVE=1 ./scripts/check.sh`: 221 Rust tests
passed (two ignored), 926 Python tests ran (12 skipped), and 21 harness tests ran
(three skipped). Formatting, warnings-denied Clippy, Ruff and shell checks
passed. Pydantic 2.10.6 passed 28 model tests; 55 model/invoker tests passed on
3.14t with the GIL disabled throughout. Changed Python test functions also pass
complexipy at 15. This is local macOS validation; no new remote CI run was made.

The installed-wheel comparison used the previous `c2c33aa` build, the candidate
and FastAPI 0.162.0/Uvicorn 0.54.0 with uvloop 0.23.0 and httptools 0.9.0. All
used CPython 3.13.18 and Pydantic 2.14.3 on the active Apple M5 Max. One worker,
64 connections, three four-second samples per cell and one-second warmups gave
these median successful responses per second:

| Workload | Previous Bessemer | Candidate | FastAPI | Candidate / FastAPI |
|---|---:|---:|---:|---:|
| JSON | 101,532 | 114,185 | 18,114 | 6.30x |
| Path/query | 92,553 | 96,908 | 13,478 | 7.19x |
| 20-item Pydantic echo | 47,092 | 48,362 | 10,697 | 4.52x |
| 20-item Pydantic with defaults | 50,051 | 57,477 | 10,870 | 5.29x |
| 1 ms async wait | 45,925 | 46,326 | 16,549 | 2.80x |
| Cooperative async yield | 93,720 | 96,663 | 17,259 | 5.60x |
| Eight-task async fan-out | 26,348 | 27,029 | 10,446 | 2.59x |

The default-heavy payload improved 14.8% in HTTP throughput. At a matched 5,000
requests/s its estimated server CPU cost fell from 38.01 to 36.01 microseconds
per response (5.3%); FastAPI used 108.01. An alternating isolated codec probe
measured 28.9% less parse CPU and 17.8% less parse-plus-encode CPU for that
payload. Fully supplied controls remained broadly unchanged. Async HTTP gains
were small and within the local variation: the 256-connection wait test measured
91,543 previous / 90,756 candidate / 14,310 FastAPI responses/s with one client
thread, a 0.9% candidate decrease and 6.34x FastAPI.

Across all 99 samples, 15,264,961 responses completed with zero transport errors
or non-200 responses; warmups were also clean. All 27 server process groups
stopped, and no sample triggered the client-bound flag. Both Bessemer builds
used the default Rust loop and the harness admission/connection caps of 16,384.
These short, unpinned same-host measurements have substantial variation on some
routes and do not establish a general 10x claim. Python routes remain Tier 2
and the formal M0 performance gate remains open.

Report, raw observations, commands, source patch, wheel/native hashes and
environment records are retained locally under
`~/Library/Caches/bessemer-bench/speed-improvement-2026-11-07/`. The final tables
use only the `wheels-defaults-*` builds and final three-phase run; earlier
experimental builds and runs are separate from those results.

## 7 November 2026 async execution follow-up

Callback and timer-owner state now reuse the existing `Guarded` helper. GIL
builds avoid uncontended mutex operations; free-threaded builds retain mutexes.
Callback references are copied before entering Python, and callback/owner
objects are dropped after guarded access. Timer expiry, cancellation and close
retain their ownership and ordering. One internal loop-to-codec crate link
shares the helper without adding or updating an external dependency.

Handlers still run as real asyncio Tasks. Task factories, foreign awaitables,
task inspection, ContextVars and cancellation keep their existing execution
paths. The specification's separate native Task/Future driver is not implemented.
An Arc callback prototype and duplicate invocation-reference changes were
excluded after their probes failed to establish a consistent overall gain.

The retained installed macOS arm64 CPython 3.13.18 wheel passed the full check:
221 Rust tests passed (two ignored), 932 Python tests ran (12 skipped), and
21 harness tests ran (three skipped). Formatting, warnings-denied Clippy,
Ruff, shell checks and changed-test complexipy at 15 passed. The rebuilt
CPython 3.14t wheel ran 118 focused loop/invoker tests with one platform skip
and the GIL disabled before and after. New HTTP regressions cover stable task
identity/registration, an expired timeout followed by another await, and
concurrent sibling/child context isolation on both event loops.

An alternating probe on the exact retained wheel measured zero-delay timer CPU
at 283.14 versus 213.12 ns/operation (24.7% lower), callback batches at 188.29
versus 162.72 ns/operation (13.6% lower), and cooperative yields at 593.73 versus
539.09 ns/operation (9.2% lower). Standard CPython 3.13 handle object size fell
from 128 to 112 bytes (12.5%); that excludes referenced callback payloads.

The final three-way throughput comparison used the PR 105 wheel (`8acbac6`),
the retained patch and FastAPI 0.162.0/Uvicorn 0.54.0 with uvloop 0.23.0 and
httptools 0.9.0. All used CPython 3.13.18 and Pydantic 2.14.3 on the active,
unpinned Apple M5 Max. One worker, three four-second samples per cell and
one-second warmups gave these median successful responses per second:

| Workload | PR 105 baseline | Retained patch | FastAPI | Patch / FastAPI |
|---|---:|---:|---:|---:|
| 1 ms async wait, 64 connections | 40,794 | 48,034 | 17,046 | 2.82x |
| Cooperative async yield, 64 connections | 80,621 | 84,953 | 16,678 | 5.09x |
| Eight-task async fan-out, 64 connections | 27,492 | 27,913 | 9,575 | 2.92x |
| 1 ms async wait, 256 connections | 79,426 | 84,267 | 14,844 | 5.68x |

The 256-connection case uses one client thread; the 64-connection cases use
four. Both Bessemer builds use their default Rust loop with harness
admission/connection caps of 16,384. Responses are validated before load.
The complete table, sample ranges and latency results are in the retained report.
Variation was substantial on some routes; these local medians do not establish
sustained production capacity or a general 10x speedup.

A separate equal-load confirmation alternated baseline/candidate and
candidate/baseline over four six-second samples per cell at 5,000 requests/s.
Median CPU per response changed by -0.8% for JSON, about 0% for default-heavy
Pydantic, +0.6% for the async wait and +0.7% for fan-out. This did not establish
an overall HTTP CPU saving. The earlier combined prototype's fixed-rate CPU
regressions did not repeat in this isolated confirmation; its measurements are
kept separately and are not attributed to the retained patch.

The retained 104 samples completed 15,240,320 responses with zero transport
errors or non-200 responses; warmups were also clean. All 26 server process
groups stopped and no sample triggered the client-bound flag. Report, raw data,
commands, source patch, package/native hashes and build records are retained at
`~/Library/Caches/bessemer-bench/async-execution-2026-11-07/retained/`.
Before publication, the branch was fast-forwarded to main `adbb8bf` and both
macOS wheels were rebuilt. The full 3.13 check repeated the counts above, and
the 3.14t wheel repeated 118 focused tests with one platform skip and the GIL
disabled throughout. Runtime and regression-test source hashes still match
the retained benchmark patch; those performance measurements were not rerun
for this main update. Remote validation is tracked by the publication PR.
Python routes remain Tier 2 and the formal M0 gate remains open.

## 7 November 2026 async execution investigation

The retained changes improve benchmark reliability and compatibility coverage.
The native Task/Future prototype, the Task+Future constructor-option cache and
its narrower Future-only variant were rejected; runtime source is restored to
main `d34b102`. Task factories, cancellation and request execution remain on
the existing CPython Task path. The specified native Task/Future driver is open.

The prototype's eager Task cost was about twice CPython's, with 19–39% higher
CPU in suspended-task probes. Two factory cases also failed because
`_log_destroy_pending` was missing. The wider constructor cache then showed a
20.6% CPU regression in an alternating four-vCPU fan-out repeat.

The narrower Future-only cache reduced Future creation from 208.17 to 160.90
CPU ns/operation (22.7%) in an exact installed-wheel macOS CPython 3.13.18
baseline/candidate/candidate/baseline probe. Callback batches improved 8.5%
and callback/bookkeeping batches 10.6%. Its four-vCPU fan-out follow-up did not
reproduce the earlier regression, but the final balanced workload matrix found
an I/O SLO failure at 20,000 offered requests/s: main passed both rounds while
the Future-only candidate missed both (p99 208/137 ms, delivery 91.1%/93.2%).
The component improvement therefore was not retained as a runtime change.

The original three-way matrix and targeted repeats are separate from the final
balanced Future-only/main/FastAPI matrix. Each matrix covers JSON, Pydantic
validation, dependencies, async waits, 16-task fan-out, SSE and mixed CPU/I/O
under enforced 1/2/4-vCPU, 512 MiB and 256-PID limits, with the client outside
the cap. Two-second samples have substantial latency and CPU variation. Failed
SLO cells remain visible; CPU ratios require matched passing rounds. One-worker
Bessemer PSS was higher than FastAPI's, so CPU ratios alone do not establish
lower hosting cost.

The efficiency harness now includes its existing fan-out workload and an
explicit `baseline-rust` variant. Paired rounds reverse their order and rotate
between pairs; two no-server tests cover reversal and equal execution positions.
Four loop regressions cover loop binding, explicit task options/factories, close
and unclosed-loop GC. They pass on the immutable main CPython 3.13 and
GIL-disabled 3.14t wheels; the retained harness passes 23 tests (three skips).
Changed Python passes Ruff, formatting and complexipy at 15.

Experimental installed wheels also passed the full check: 221 Rust tests
(two ignored), 936 Python tests (12 skipped), 23 harness tests (three skipped),
formatting, warnings-denied Clippy, Ruff and shell checks. The experimental
GIL-disabled 3.14t wheel passed 122 focused loop/invoker tests (one platform
skip). Passing correctness checks did not override the performance rejection.

Profiles, rejected source/wheels, exact commands, source/native hashes and all
raw results remain under
`~/Library/Caches/bessemer-bench/async-next-2026-11-07/`. These diagnostics do not
establish production capacity, lower production cost or a general 10x claim.
The [investigation report](../benchmarks/results/2026-11-07/async-execution-investigation/report.md)
and its versioned summaries preserve the accepted and rejected scopes.

## 8 November 2026 compressed HEAD lengths

HEAD omits `Content-Length` when the corresponding GET could use native
compression, without compressing or iterating its body. Known lengths remain
for identity responses, bodies below the compression threshold and compression
opt-outs. The router preserves the omission through HTTP/1.1 and HTTP/2 writing.

The rebuilt, installed macOS arm64 release wheel passed 14 focused Python tests
on CPython 3.13.18 with Pydantic 2.10.6, including gzip/Brotli JSON, text and byte
responses, streaming HEAD, HTTP/1.1 connection reuse and HTTP/2 wire headers.
The new HTTP/1.1 and HTTP/2 regressions failed against the preceding wheel.
Eight compression and ten router Rust tests passed, along with warnings-denied
library Clippy, Rust formatting, Ruff and complexipy at 15 for the changed Python.
The full suite and cross-platform qualification were not rerun for this fix.

## 8 November 2026 response-model and list fast path

`response_model` routes now have the same native handling the body models had.
A plain `list[Model]` response annotation compiles into a model plan, and a new
native encoder validates every dict item straight against the model's fields
and writes the response in one pass, or writes an item that is already an
instance of the model from its `__dict__`; nothing is built into Pydantic
instances and dumped back to dicts. Anything the plan cannot accept (a
coercion, a missing required key, an extra key under `extra="forbid"`, a
subclass, a non-list, an invalid item anywhere in the list) refuses the whole
list and keeps Pydantic's validate-then-dump pipeline, so outputs and errors
are unchanged. On the Python side this also serves routes that run through
`_plain_response` (for example behind middleware) through the same one-pass
encoder.

For a list item's dict the encoder makes one walk checking every key is an
exact `str` (Pydantic's own dict reads compare text; a lookup on such a dict
runs no Python code), then looks each declared field up by its interned key
(`PyDict_GetItemWithError`) and writes it through its node, with absent fields
written from the field's default and undeclared keys ignored, as Pydantic
ignores extras. Nested models recurse the same way, so `list[Order]` accepts
plain dict trees. The per-field lookup replaced an earlier per-item slot walk
that a nested model's recursion would have clobbered.

Two model-plan extensions make the routes of the comparison matrix qualify.
First, a field default may be a JSON-exact mutable value: an exact list or
dict of exact JSON values (to depth 8). Pydantic deep-copies such a default
for every instance; the native builder rebuilds it structurally (new lists and
dicts, the same immutable leaves a deep copy shares), so per-instance
freshness, `model_fields_set`, mutation isolation and `model_dump()` are the
same; the encoder keeps the default's pre-encoded bytes so an absent field
writes without building anything. Defaults that do not fit their field keep
the model on Pydantic's path, and a default factory or tuple/object default
still rules a model out. Second, a `None` default now writes `null` under any
field type: Pydantic serialises a None-valued field as null with no warning
and validates none of its defaults, and this restores the fast path for
models that declare one (it had been lost when defaults became typed).
Encoding a float field also accepts an exact int, as Pydantic serialises it:
as a float (`2` written `2.0`), with no warning. Encoding a float value and
default now goes through a direct-mapped cache of the float's bit pattern in
the thread's scratch buffers, reusing the zmij-formatted bytes of a previous
encode of the same value; zmij's share of a /big response fell from about
14.7k to 5.2k instructions per request while the cache's own cost is about
5.5k, so the whole change is about -0.7% on /big and smaller elsewhere.

Building instances got cheaper: the values dict is built fresh in field order
instead of copied from a per-model template and re-set, and `__dict__` goes in
through `PyObject_GenericSetDict` instead of a `__dict__` attribute lookup
(the same path the `__dict__` slot descriptor's setter takes). Instances are
still indistinguishable: same `__dict__` key order, `__pydantic_fields_set__`,
`__pydantic_extra__` and `__pydantic_private__` as pydantic-core writes them.

Measurement caution for concurrent agents on this machine: callgrind's
`summary:` includes the inclusive records it writes for the call context open
at zero and dump time, which can add thousands of instructions per request
that vary with where those land (one run of this branch reported 40,635 for
/json where the honest number is 30,453). The per-request numbers below are
therefore the dumps' self costs, excluding those records, read back with a
parser; they repeat to well under 1%. The companion script
`benchmarks/profile/instructions-private.sh` zeroes and dumps one worker by
PID through a private vgdb prefix, so concurrent agents do not interfere, and
pauses before zero and dump so the call context is open in epoll.

Measured per request at one connection, CPython 3.13, one run with both sides
sequenced in the same quiet window (self cost, base main wheel -> this wheel):

After merging main (insertion-order dict keys and declaration-order model
fields), the list-of-model encoder writes fields in declaration order through
the merged per-field prefixes, and the key-order regression tests below
compare the response bytes byte for byte with `TypeAdapter.dump_json`, which
is what FastAPI emits (a model whose declaration order is not alphabetical
and whose last field carries a serialization alias pins the order in tests
for both a list of dicts and a returned body model). Key-order checks: one
walk in the list dict encoder requires every key to be an exact `str`
(Pydantic's own dict reads compare text; a lookup on such a dict runs no
Python code and the GIL keeps the dict unchanged), then each declared field
is looked up by its interned key and written through its node, with absent
fields written from the field's default bytes and undeclared keys ignored
like extras unless `extra="forbid"`; nested models recurse.

The float format cache is bounded and thread-local: a fixed direct-mapped
table of 128 slots (a `FloatSlot` is 48 bytes: 32-byte buffer, bit-pattern
tag, length), built once when a thread's scratch buffers are made and never
grown, so it costs about 6 KiB per worker thread on the GIL build. On
free-threaded Python the scratch buffers, and with them the cache, are
per-thread state (`thread_local!` with a same-buffer re-entrancy fallback),
so no table is shared across threads, and the cached bytes depend only on the
float's bits, so any table is correct for its thread. A Rust unit test pins
the table size and the byte-identity of repeated encodes.

Measured per request at one connection, CPython 3.13, main's wheel
(`/home/cmux/wheels/main`, insertion-order encoder) against this branch's,
both sides sequenced in one quiet window (self cost):

| Route | main | this branch | |
| --- | ---: | ---: | --- |
| GET /json (matrix) | 30,450 | 30,621 | +0.6% |
| GET /json (compare app) | 30,281 | 30,504 | +0.7% |
| GET /items/7?q=abc (compare app) | 33,224, 33,364 | 33,676, 33,572, 33,579 | +1.1% on average |
| GET /models, `response_model=list[Item]` | 192,752 | 69,616 | -64% |
| GET /big | 431,156 | 421,642 | -2.2% |
| POST /item (one `Item`, returned) | 96,250 | 38,962 | -60% |
| POST /echo (20-item `Order`, compare app) | 188,343 | 184,889 | -1.8% |

The small losses on modelless routes (up to about 450 instructions on /items,
under the 2% CI gate) come from code-layout shifts around the new encoder
state; they are the price of the /models and /item gains and of the float
cache. Against the pre-merge main (byte-sorted keys) the same branches were
measured in `/json` 30,310 to 30,453 / 30,191 to 30,413,
`/models` 197,249 to 70,337, `/big` 472,162 to 462,917, POST `/item`
98,370 to 38,679, POST `/echo` 190,767 to 188,266; an earlier quiet-machine
pass with the stock script gave /models 210,180 -> 79,432 and /item
105,855 -> 41,688 on the `summary:` basis.

The /models and /item figures were consistent in an earlier pass with the
stock script on a quiet machine: /models 210,180 -> 79,432 and /item
105,855 -> 41,688. /echo gains only the cheaper construction (it builds 21
instances; its parse, encode and base request path are unchanged this pass).
The full Python suite, the codec, loop and worker Rust tests, warnings-denied
Clippy and Ruff were rerun on this branch (counts below).
## 8 November 2026 synchronous completion handoff

Synchronous calls now post completion directly from the existing bounded pool to
a loop-owned future. That future declines cancellation until the pool call has
finished, replacing the concurrent-future chain and extra shield future while
retaining request capacity, resource-acquisition callbacks, copied ContextVars
and thread isolation. No Tokio thread waits for a Python future. The public API,
executor sizing and lifecycle are unchanged. New regressions cover sync
SystemExit delivery and repeated cancellation during resource acquisition.

Installed CPython 3.13.15 release wheels with the auto loop and one connection
measured `/sync` at 211,426 -> 157,784 instructions/request (-25.37%), `/bg` at
275,278 -> 220,809 (-19.79%), and `/json` at 33,101 -> 33,154 (+0.16%). The
[report](../benchmarks/results/2026-11-08/sync-completion/report.md) records
isolated Callgrind controls and scheduler-dependent strace results.

BackgroundTasks benefits from the sync optimization but its buffered tasks still
finish before the response is sent; the requested FastAPI post-send semantics
remain incomplete. A transport completion hook must retain admission through
background work and dependency cleanup across HTTP/1, HTTP/2, HEAD, disconnect,
shutdown and middleware response replacement. No detached workaround is added.
Native pool handoff, response encoding from pool threads, batching and spinning
workers were not implemented. Free-threaded correctness uses the existing pool
and loop synchronization; this host only validates GIL-enabled CPython 3.13.
Python routes stay Tier 2; the formal M0 performance gate remains open.

Validation: the built and installed release wheel passed the required full check:
235 Rust tests passed (2 ignored), 943 Python tests ran (34 skipped), and all 23
harness tests passed, with Rust formatting, warnings-denied Clippy, Ruff lint and
formatting, and shell syntax checks green. The final cancellation/exception
regressions passed a focused run; the repeated complete check passed those final
tests with the counts above. Logs and measurements are retained with the report.

### Closed-loop sync completion (8 November 2026)

Pool completion now checks whether its destination loop is closed and tolerates
closure racing with `call_soon_threadsafe`, matching asyncio executor chaining.
A closed loop cannot resume its request Task; completion is discarded rather
than failing inside an unobserved executor future. Returned, unstarted coroutine
objects are closed on that abandoned path. Normal Worker shutdown still drains
requests and the executor before closing the loop. An installed release wheel
passed six worker-thread tests, including already-closed and racing-close cases.

### Native synchronous invocation (8 November 2026)

Plain sync handlers can use native argument binding and result encoding while
running user code only in the existing bounded pool. A native work item owns a
copied Context, the invocation's cancellation controls and a loop-owned future.
Queued work observes cancellation before entering user code; running work holds
admission through completion. Cancellation closes an unstarted returned coroutine.
Non-native inputs and unusual return values preserve their Python fallbacks.
The executor's internal work-item protocol avoids an unused concurrent Future,
retains its shutdown locks and checks, and handles queued cancel_futures shutdown.

Isolated installed-wheel Callgrind measurements (CPython 3.13.15, auto loop,
one connection, 3,000 measured requests) changed `/sync` 158,515 -> 99,426,
`/bg` 221,948 -> 222,010, `/json` 33,144 -> 33,135, `/items/7?q=abc`
36,164 -> 36,053 and POST `/echo` 203,862 -> 201,893 instructions/request.
The latter two use the original compare app and 20-item order. Ninety-three
focused tests pass, including both native loops, binding/encoding bypass probes,
request contexts, deadlines and cancellation; a separate executor shutdown
protocol regression passes. Warnings-denied workspace Clippy passes. Full final
checks and post-send BackgroundTasks follow in the next step. Free-threaded and
other supported Python versions require CI qualification; Python remains Tier 2.

### Post-response background tasks (8 November 2026)

BackgroundTasks now starts after the transport sends a response, retaining
request admission and input/spool budgets through ordered background work and
dependency cleanup. HTTP/1 uses completion of response writing; HTTP/2 keeps the
completion guard through body/data-frame ownership. Discarded/failed responses
skip new tasks and unwind resources. Sync tasks remain on the bounded pool;
async tasks run on the request loop. Errors after sending are logged and do not
replace the delivered response. Streaming responses, HEAD, middleware replacement
and shutdown use the same guard. Source-only Worker.submit has no transport and
retains its prior synchronous completion semantics.

Final instruction counts before/after this follow-up: `/sync` 158,515 -> 99,619,
`/bg` 221,948 -> 240,515, `/json` 33,144 -> 33,189, `/items/7?q=abc`
36,164 -> 36,124 and POST `/echo` 203,862 -> 203,828. Post-send accounting
raises `/bg` CPU instructions by 8.37%; the timing bug fix is retained for
semantics, not claimed as a CPU win. Full measurements and intermediate results
are in the [follow-up report](../benchmarks/results/2026-11-08/sync-completion/followup.md).

Focused installed-wheel validation passed 132 background/interop tests (one skip),
then 19 delivery/API tests including middleware replacement and abandoned stream
cleanup. One old HTTP test in the first full run assumed background output was
already written at response receipt; it now waits for completion. The required
full check was repeated on the final installed wheel. Free-threaded and other
supported CPython versions still need CI qualification; native health stays
Tier 0, Python routes Tier 2, and the formal M0 gate remains open.

Final required installed-wheel check passed: 235 Rust tests (2 ignored),
954 Python tests run (34 skipped), all 23 harness tests, Rust formatting,
warnings-denied workspace Clippy, Ruff lint/format and shell syntax checks.
The final wheel/source match and exact wheel hash are retained in the report.

## 8 November 2026 JSON insertion and field order

Native JSON encoding now preserves Python `dict` insertion order and Pydantic
model field declaration order across `bessemer-codec` (`Encoder::dict` and
`ModelPlan` / `Encoder::model_fields`) and the normalized fallback encoder in
`src/conversion.rs`, matching `json.dumps(..., separators=(",", ":"))` and
FastAPI byte for byte across all routes in `/home/cmux/matrix/common.py`.
`Encoder::dict` on GIL builds and `Encoder::model_fields` when `fields_in_order`
succeeds write entries directly in one pass without pushing or sorting in the
scratch entry buffer, and `Parser::object` keeps document member order without
sorting.

Measured with Callgrind (`CONNECTIONS=1`, `BESSEMER_EVENT_LOOP=auto`, 3,000
requests, private `vgdb` prefix) against the baseline wheel (`9b1f1c1`) on
CPython 3.13.15 Linux x86_64:
- `GET /json`: 33,036 to 32,913 instructions/request (−123, −0.4%)
- `GET /items/7?q=abc`: 36,111 to 36,011 instructions/request (−100, −0.3%)
- `POST /echo` (20-item `order.json`): 202,149 to 200,212 instructions/request (−1,937, −1.0%)

235 Rust workspace tests (2 ignored) and 106 focused Python fast-path, parity,
response and native-body tests passed on the rebuilt installed wheel, along
with `cargo fmt --check`, warnings-denied Clippy and Ruff.

## 8 November 2026 dependency, Request, exception and HTTP middleware costs

FastAPI-style routes that leave the native lean path cost less per request,
with unchanged semantics (`use_cache`, teardown order, overrides, exception
handlers, middleware order and `call_next` streams):

- Yield-dependency cleanup runs in the request's own task when the request did
  not come through HTTP or ASGI middleware. Every cancellation of such a
  request goes through its invocation, which now defers an accepted
  cancellation while the cleanup runs (`_begin_cleanup`/`_end_cleanup` on the
  native `Invocation` and `_InvocationControl`) and raises it once the cleanup
  ends, as the shielded cleanup Task did. Middleware routes, streams and
  WebSockets keep the shielded Task, because middleware may cancel the task
  itself (for example `wait_for` around `call_next`).
- Dependencies are resolved without a coroutine per argument or a per-request
  lambda, plain async dependencies are awaited directly, and a yield
  dependency is entered without building an `asynccontextmanager` wrapper per
  request.
- `HTTPException` bodies use the native JSON encoder, the exception-handler
  lookup returns at once when no handlers exist, and a raised error no longer
  leaves an exception/traceback/frame cycle.
- The `call_next` chain no longer refers to itself (about 40 objects of cyclic
  garbage per middleware request before); call_next's JSON response gets its
  Content-Type without header revalidation.
- `Request` construction, `RequestHeaders`, `QueryParams` and `URL` skip ABC
  checks and KeyError/AttributeError round trips, and the native bridge interns
  request-metadata keys.
- `benchmarks/profile/instructions.sh` takes `APP`/`APP_DIR` and uses a private
  vgdb prefix, so concurrent callgrind runs no longer zero each other's counts.

Instructions per request (callgrind, auto loop, `CONNECTIONS=1`, private vgdb
prefix, installed CPython 3.13 release wheels; main is `362f71a`). Matrix app
`/home/cmux/matrix/common.py`; `MW=1` adds one `@app.middleware("http")`:

| Route | main | this branch | change |
| --- | ---: | ---: | ---: |
| `/json` | 33,189 | 33,162 | −0.1% |
| `/dep` | 109,047 | 97,157 | −10.9% |
| `/yield_dep` | 166,017 | 102,126 | −38.5% |
| `/request?q=1` | 147,643 | 127,717 | −13.5% |
| `/raise` | 93,853 | 55,167 | −41.2% |
| `/json`, `MW=1` | 150,489 | 114,852 | −23.7% |
| `/dep`, `MW=1` | 212,965 | 163,651 | −23.2% |
| `/yield_dep`, `MW=1` | 268,545 | 216,873 | −19.2% |
| compare app `/json` | 33,219 | 33,103 | −0.3% |
| compare app `/items/7?q=abc` | 36,061 | 36,062 | 0.0% |
| compare app `POST /echo` | 201,838 | 202,284 | +0.2% |

Not done: dependency routes still run through the Python `_invoke` task rather
than a native call; middleware routes still build full request metadata in Rust
and convert headers again in Python (a lazy native `Request` would avoid both);
middleware routes keep the shielded cleanup Task; general envelopes do not use
the HTTP/1 settled-JSON writer. An eager cleanup Task was tried first and
rejected: it must enter the request's Context, which is already entered.
Python routes stay Tier 2; the formal M0 performance gate remains open.

Validation: the installed release wheel of this branch (merged with main at
`035e0b3`) passed `BESSEMER_REQUIRE_NATIVE=1 ./scripts/check.sh`: 235 Rust tests
passed (2 ignored), 955 Python tests ran (34 skipped) and 23 harness tests
passed, with Rust formatting, warnings-denied Clippy, Ruff lint and formatting
and shell syntax green. New regressions: `tests/python/test_request_path_costs.py`
(header, query, URL and response-header behaviour; no cyclic garbage from
middleware, yield dependencies or raised errors; a deadline during inline
cleanup; a middleware `wait_for` that must still wait for cleanup) and a
cleanup-deadline route in `test_adversarial.py`. Request rates were not
measured (shared machine).

### Main integration (8 November 2026)

Merged local main at `5926da6` in `d9b3773`, retaining native synchronous
invocation and post-send background work alongside dependency/Request/middleware
optimizations, insertion-ordered JSON and FastAPI-ordered 422 members. Only the
two documentation files conflicted; both histories were retained.

Installed release wheel, isolated private-prefix Callgrind, auto loop, one
connection, 2,000 warmups and 3,000 measured requests: matrix `/sync` 99,619 ->
99,502, `/bg` 240,515 -> 240,967, `/json` 33,189 -> 33,006 instructions/request.
The required full check passed: 235 Rust tests (2 ignored), 966 Python tests
run (34 skipped), 23 harness tests, formatting, Clippy, Ruff and shell syntax.
Python routes remain Tier 2; the formal M0 gate remains open.

## 8 November 2026 second pass: lazy native request, flat dependency plans

Follow-up to the section above, same measurement method (the "this pass"
column is the installed wheel at `98d90d8`; merging main's error-order commits
afterwards does not touch these paths).

- Inline yield-dependency cleanup audit: deadlines, disconnects and worker or
  lane shutdown all cancel through the invocation and are deferred until the
  cleanup ends. `_cancel_background_tasks` only runs after `_shutdown` has
  gathered every request task (and cancelled the shielded cleanup Task as well
  before). The ASGI bridge and HTTP middleware may cancel the task themselves,
  so those requests keep the shielded Task. New tests cover a graceful
  `Worker` shutdown, a server shutdown and a client disconnect while cleanup
  awaits.
- `needs_request` routes get a native `RequestHead` instead of a metadata dict:
  it owns a clone of the request's `HeaderMap` and the other parts, `Request`
  reads method, path, scheme, query string and request id at once and converts
  headers (already lowercase), path parameters, cookies, body, client and
  client certificate on first use (`Request.__getattr__`). A `Request` built
  from a mapping (WebSockets, direct `Worker` use) is unchanged.
- Request-scoped dependency graphs are flattened when the worker starts into
  the resolver's call order (depth first, route and app dependencies first, a
  cached dependency once, an uncached one per use) and run as a loop; app-scoped
  dependencies, principals, Security scopes and WebSockets keep the resolver.
  Handlers that take only inputs, the Request and BackgroundTasks bind without
  a resolver.
- A worker result `(status, bytes, (), "json_bytes")` of at most 8 KiB, not
  HEAD, settles as a `JsonReply` through the HTTP/1 settled-JSON writer, as a
  natively encoded lean result does (byte-for-byte test against the lean path).
- Replacing the general path's `getattr(route, ..., None)` lookups was measured
  (59 instructions on `/dep`) and not kept.

| Route | main `362f71a` | first pass | this pass | change from main |
| --- | ---: | ---: | ---: | ---: |
| `/json` | 33,189 | 33,162 | 32,918 | −0.8% |
| `/dep` | 109,047 | 97,157 | 79,494 | −27.1% |
| `/yield_dep` | 166,017 | 102,126 | 89,086 | −46.3% |
| `/request?q=1` | 147,643 | 127,717 | 103,829 | −29.7% |
| `/raise` | 93,853 | 55,167 | 52,670 | −43.9% |
| `/json`, `MW=1` | 150,489 | 114,852 | 92,282 | −38.7% |
| `/dep`, `MW=1` | 212,965 | 163,651 | 126,725 | −40.5% |
| `/yield_dep`, `MW=1` | 268,545 | 216,873 | 185,939 | −30.8% |
| compare app `/json` | 33,219 | 33,103 | 32,886 | −1.0% |
| compare app `/items/7?q=abc` | 36,061 | 36,062 | 35,945 | −0.3% |
| compare app `POST /echo` | 201,838 | 202,284 | 199,300 | −1.3% |

Not done: dependency routes still run their Python orchestration (`_invoke`,
`_execute_route`, `AsyncExitStack`, `BackgroundTasks`) in the worker; a native
resolver in the invoker was not attempted. Under HTTP middleware the inner
`_invoke` still re-checks the route and filters native arguments, and
middleware responses with headers take `decode_response`. Python routes stay
Tier 2; the formal M0 performance gate remains open.

Validation: after merging main at `5926da6`, the installed release wheel passed
`BESSEMER_REQUIRE_NATIVE=1 ./scripts/check.sh`: 235 Rust tests passed
(2 ignored), 962 Python tests ran (34 skipped), 23 harness tests passed, with
formatting, warnings-denied Clippy, Ruff and shell syntax green. Before that
merge, the codec parity Rust test failed on main's `e787ada` (fixed on main by
`c395122`).

## 8 November 2026 HTTP middleware replies and streaming responses

Same measurement method as the two sections above (installed wheel at
`5a6c852`, auto loop, one connection, private vgdb prefix). Streaming routes
come from a private copy of the matrix app: `StreamingResponse` over an async
generator and over a sync generator, each yielding 10 small chunks, and an
`EventSourceResponse` over an async generator of 10 dict events.

- A JSON reply that an HTTP middleware changed only by headers now reaches the
  HTTP/1 settled-JSON writer as a `JsonReply` carrying its validated header map
  (written in map order, request-id and security headers replacing the
  application's). Envelopes with a `Date` header, an empty body, 204/205/304,
  HEAD, bodies over 8 KiB, closing or HTTP/1.0 connections and HTTP/2 keep
  `decode_response`. Filtering the native arguments once for the inner
  `_invoke` was measured (about 100 instructions) and dropped.
- Streaming bodies: exact `bytes` chunks need one type check; async generators
  are stepped with `async for` (other async iterators through their type's
  `__anext__`), and the buffer-space event is awaited only when clear. The
  runtime's first `next()` starts the producer task eagerly (it runs outside
  the stream's context), so chunks that are ready need no extra wake-ups. A
  sync iterator's first thread-pool batch takes up to 64 steps like later
  ones (still bounded by the 1 ms quantum and the 64 KiB allowance; a read
  can now carry several first chunks, and `test_sync_streams` was updated to
  state that bound as the read the runtime holds plus the buffer).
  `streaming::start` makes the first chunk request itself and the HTTP/1
  writer sends the head of a chunked body with whatever frames and end are
  already there, so a short stream goes out in one `send` instead of three.
- Server-sent events: one module-level JSON encoder instead of one per event,
  `jsonable` skipped for exact JSON data (the json encoder still writes it, so
  output is unchanged; tested against `json.dumps(jsonable(...))`), and the
  `_AsyncEvents` wrapper's coroutine skipped for async-generator sources.

| Route | before | after | change |
| --- | ---: | ---: | ---: |
| `/json` with one HTTP middleware | 92,282 | 91,095 | −1.3% |
| `/dep` with one HTTP middleware | 126,725 | 125,361 | −1.1% |
| `StreamingResponse`, async generator, 10 chunks | 241,021 | 175,880 | −27.0% |
| `StreamingResponse`, sync generator, 10 chunks | 486,714 | 328,985 | −32.4% |
| `EventSourceResponse`, 10 events | 725,102 | 428,820 | −40.9% |
| `/json` | 32,918 | 33,072 | +0.5% |
| `/dep` | 79,494 | 79,740 | +0.3% |
| compare app `/json` | 32,886 | 32,982 | +0.3% |
| compare app `/items/7?q=abc` | 35,945 | 36,359 | +1.2% |
| compare app `POST /echo` | 199,300 | 199,253 | 0.0% |

The `/json`, `/dep` and compare-app rows changed by under 1.2% with no code on
their paths changed; the main merge (`a191932`) came after these runs. Request
rates against FastAPI were not measured: the host load average stayed above 2.
Not done: per-request stream setup (two eager tasks, four `asyncio.Event`s and
the `_ResponseStream` object) and the user generator's own steps are most of
what remains; Python routes stay Tier 2 and the M0 gate remains open.

Validation: after merging main at `a191932`, the installed release wheel passed
`BESSEMER_REQUIRE_NATIVE=1 ./scripts/check.sh`: 235 Rust tests passed
(2 ignored), 969 Python tests ran (34 skipped), 23 harness tests passed, with
formatting, warnings-denied Clippy, Ruff and shell syntax green.

### 8 November 2026: adaptive admission candidate

Merged latest local main (`a191932`) in `b6ee362`; the guarded worker JSON
settlement path still retains post-delivery background work and admission.
Default `max_in_flight` now accepts `"auto"` with a 4,096-request worker
ceiling. Explicit integers retain fixed-cap semantics. Delay above the
timeout-scaled 0.1–5 ms target must persist for twenty target periods before
shedding new admissions for four target periods. Multi-loop dispatch avoids
congested lanes. Existing semaphore ownership and upload/response byte budgets
remain in place through cancellation, streaming and post-send work.

Installed release wheel full check passed: 238 Rust tests (2 ignored),
983 Python tests (34 skipped), 23 harness tests, formatting, Clippy with
warnings denied, Ruff and shell syntax. Focused admission tests also passed
on repeat. Private-prefix Callgrind c1: `/json` 33,022 → 33,130 (+0.33%),
`/sync` 99,160 → 99,575, `/bg` 225,617 → 230,215; these include the latest
main parity merge. The same candidate with fixed admission measured `/bg`
at 229,678. Container example defaults now select auto; no new image build
or container qualification was run. Isolated concurrency qualification and
the timer-path optimization remain open. Native health is
Tier 0, Python bridge routes Tier 2, and the formal M0 gate remains open.

Shared-machine concurrency smoke (one/four workers, c64/256/1024, compare
`/json` and `/io`) completed with zero candidate non-200 responses in all
twelve samples; the fixed128 baseline shed `/io` at high concurrency.
Transport errors were separately reported by oha. Builds and profiles
overlap these samples, so throughput is not qualified. Full table and
limitations: `benchmarks/results/2026-11-08/adaptive/report.md`.

### 8 November 2026: suspended-request lifecycle follow-up

Reverted the timer-turn batching change (`26f2c66`): its small instruction
saving did not establish throughput improvement and doubled standalone sleep
epoll calls. Merged current local main in `ca07722`, including the newer
response-path changes. Keep the original ready-turn/Tokio scheduling and
native 1 ms timerfd resolution while optimizing lifecycle work instead.

A lean request now registers its existing native Call as the asyncio Task's
done callback. This avoids a bound-method allocation and task-table lookup;
the real Task, both tracking tables, cancellation, completion ordering and
native response encoding remain. Focused installed invoker tests pass (53),
including a new user completion-callback ContextVars/current_request check on
both loop backends. Private-prefix Callgrind c1, 2,000 warmups / 3,000 measured:
`/io` 61,192 → 60,421; `/json` 33,257 → 33,292. The full installed check is in
progress with the separate Handle allocation candidate.

## 8 November 2026 FastAPI parity pass

Implemented scope (see [fastapi-parity.md](fastapi-parity.md) for the updated
matrix):

- `ResponseValidationError`: raised when `response_model` or return-annotation
  validation fails on the lean or general path, carrying `.errors()` (`loc`
  starting at `["response", ...]`) and `.body`. Routes without a registered
  `ResponseValidationError` exception handler still return `500`, and apps with
  `@app.exception_handler(ResponseValidationError)` intercept it cleanly.
- `bessemer.security`: added `SecurityBase`, `HTTPDigest`, `HTTPDigestCredentials`,
  `OAuth2`, `OAuth2Model`, `OAuthFlowsModel` and `OpenIdConnect`, exported from
  `bessemer.security` and wired into OpenAPI `securitySchemes`.
- `Parameter` and `pydantic.Field(...)` options: `Annotated[T, Field(...)]` on
  path, query and body parameters converts to the matching marker at startup;
  `default_factory`, `multiple_of`, `strict`, `discriminator`,
  `validation_alias` (`str` on non-body parameters; `str`, `AliasPath` or
  `AliasChoices` on body parameters) and `serialization_alias` are compiled at
  startup. Unsupported `Field` arguments (`max_digits`, `decimal_places`,
  `allow_inf_nan`, `alias_priority`, `union_mode`, `fail_fast`,
  `validate_default`) fail at route registration.
- `Annotated[Model, Query()]`: Pydantic `BaseModel` query parameters extract
  scalar and sequence fields from `request.query_params` (defaulting to empty
  lists for `list`/`tuple`/`set`/`frozenset` fields when absent), validate with
  `model_validate` (errors at `["query", ...]`), and document each field as an
  OpenAPI query parameter.
- `FileResponse` on the native static-file path: `_ResponseFile` (`PendingFile`
  in `src/static_files.rs`) carries the opened file and metadata to
  `serve_response_file`, which adds `Accept-Ranges: bytes` and serves `304 Not
  Modified` (`If-None-Match`, `If-Modified-Since`), `206 Partial Content` for
  single byte ranges (`Range`, `If-Range`), `416 Range Not Satisfiable`
  (`Content-Range: bytes */<size>`) and `HEAD` with `Content-Length` and empty
  body, streaming 64 KiB chunks natively in Rust (with ASGI fallback in
  `_asgi.py`).
- `redirect_slashes`: supported on `App` / `FastAPI`, `Router` / `APIRouter` and
  route decorators (`include_router` combines app, router and route settings),
  controlling the native router's `307 Temporary Redirect` between `/path` and
  `/path/` (preserving query strings).
- `root_path` and `root_path_in_servers`: supported on `App` / `FastAPI`,
  populating `request.scope["root_path"]`, prepending `root_path` in
  `request.base_url`, `request.url_for` and the `/docs` OpenAPI URL, and
  prepending `{"url": root_path}` to OpenAPI `servers` when
  `root_path_in_servers=True`.
- `Request.receive` and `Request.is_disconnected()`: `receive()` returns
  `{"type": "http.request", "body": ..., "more_body": False}` on the first call
  (or delegates to the ASGI middleware `receive`) and then waits on the native
  invocation's disconnect waiter (`_wait_disconnect`) to return
  `{"type": "http.disconnect"}`; `is_disconnected()` yields once and reads the
  native invocation's `_disconnected` flag (set via `DISCONNECT_BIT` on the
  bridge cancel queue when the client connection drops, without calling
  `task.uncancel()` or swallowing route-deadline, worker-shutdown or lane-close
  `CancelledError` cancellations, and still enforcing the route deadline if a
  disconnect-watching handler lingers after peer disconnect).
- `WebSocket` parity: `accept(subprotocol=..., headers=...)` validates and sends
  custom handshake response headers natively in Rust (`src/websocket.rs`);
  `send(message)` handles ASGI `websocket.accept`, `websocket.send` and
  `websocket.close` dicts; `receive_json(mode=...)` accepts `"text"` or
  `"binary"`; `scope`, `client_state`, `application_state` and
  `bessemer.websockets.WebSocketState` match Starlette/FastAPI.
- `app.router` and `APIRouter(lifespan=...)`: `app.router` returns `self`
  (`App` subclasses `Router`), and `Router` / `APIRouter` accepts `lifespan=`
  (and `.lifespan` / `.lifespan_context`), merging outer and included router
  lifespans in `include_router(...)` with FastAPI's entry/exit order and
  state dictionary precedence.

| Route | main `dd479d0` | after batch 2 | after batch 3 (`main` `d88b23e`) |
| --- | ---: | ---: | ---: |
| compare app `/json` | 32,982 | 33,179 | 33,616 |
| compare app `/items/7?q=abc` | 36,359 | 36,314 | 36,752 |
| compare app `POST /echo` | 199,253 | 199,189 | 200,219 |

(Note: `main` at `d88b23e` includes the adaptive admission and background
delivery merges from `01e7d52`.)

Validation: after merging `main` at `d88b23e`, the installed release wheel
passed `BESSEMER_REQUIRE_NATIVE=1 ./scripts/check.sh`: 241 Rust tests passed
(`bessemer-codec`: 15, `bessemer-ffi`: 3, `bessemer-loop`: 16, `_native` lib:
198 passed + 2 ignored, `bessemer-m0` bin: 3; 0 tests removed), 971 Python
tests ran (34 skipped) and 23 harness tests passed, with Rust formatting,
warnings-denied Clippy, Ruff lint and formatting and shell syntax green.

## 8 November 2026 stream setup, WebSockets and multipart uploads

Same measurement method (installed wheel at `1d93afc`, auto loop, one
connection, private vgdb prefix); WebSocket counts come from a raw-socket client
sending 32-byte messages on one connection (per message) or opening and closing
200 connections (per connection), with the server under callgrind.

- Streams: when the runtime pulls a stream whose iterator has ended and that
  has nothing to run afterwards, `next()` answers the last batch (or the end)
  itself instead of starting a read task; the native pump accepts the batch as
  a 1-tuple or `None`. The three per-stream `asyncio.Event`s are a slotted
  `_Signal` with the same semantics.
- WebSockets: receivers await an arrival future directly (a waiter list, woken
  on arrival, removed when a receive is cancelled), receive/send run in one
  coroutine frame, an async session handler is awaited without
  `_execute_plan`'s frame, and native delivery attaches to Python once. Each
  accepted connection's future is boxed before it is spawned (Tokio moved the
  multi-kilobyte future several times), and a session's read buffer is 16 KiB
  instead of a zero-filled 64 KiB (larger frames bypass it).
- Request bodies of several frames are copied once into an exact-size buffer
  (frames of 8 KiB or more kept as they arrive, smaller ones gathered) instead
  of into one growing buffer.

| Route | before | after | change |
| --- | ---: | ---: | ---: |
| `StreamingResponse`, async generator, 10 chunks | 175,880 | 156,069 | −11.3% |
| `StreamingResponse`, sync generator, 10 chunks | 328,985 | 311,154 | −5.4% |
| `EventSourceResponse`, 10 events | 428,820 | 406,913 | −5.1% |
| WebSocket echo, text, per message | 32,106 | 27,683 | −13.8% |
| WebSocket echo, bytes, per message | 32,490 | 28,148 | −13.4% |
| WebSocket connection (handshake, accept, close) | 426,869 | 261,628 | −38.7% |
| multipart upload, 10 KiB file and two fields | 128,068 | 127,883 | −0.1% |
| multipart upload, 5 MiB file and two fields | 8,004,905 | 6,281,816 | −21.5% |
| `/json` | 33,072 | 33,030 | −0.1% |
| `/dep` | 79,740 | 79,528 | −0.3% |

FastAPI (uvicorn 0.x with `websockets`) on the same WebSocket client: 85,148
(text) and 85,017 (bytes) instructions per message and 875,279 per connection.
FastAPI multipart was not measured (`python-multipart` is not installed here),
and request rates were not measured (the host load stayed above 2). In the
5 MiB upload the boundary search (`memchr` packed-pair, about 0.5 instructions
per byte on random data) and two copies (the contiguous body, then Python
`bytes`) are what remains; giving Python the body without its own copy is not
done. The 10 KiB upload's cost is spread across part-header parsing and
argument building. Python routes stay Tier 2; the M0 gate remains open.

Validation: after merging main at `52d6905`, the installed release wheel passed
`BESSEMER_REQUIRE_NATIVE=1 ./scripts/check.sh`: 239 Rust tests passed
(2 ignored), 988 Python tests ran (34 skipped), 23 harness tests passed, with
formatting, warnings-denied Clippy, Ruff and shell syntax green.

### 8 November 2026: high-concurrency connection-state cost

Merged local main through `52d6905`, retaining the Handle free-list revert.
Callgrind compare-app `/io` profiles at c256 and c1024 found connection-future
copies as the growing cost; timer heap, deadline FIFO and socket readiness
work per request stayed nearly flat. `a839721` allocates TLS state only on the
TLS branch, reducing plaintext connection task state from approximately
13.1 KB to 7.1 KB. `151066a` separately pins the connection future so task
completion need not copy that state. TLS adds one allocation per connection;
explicit connection pinning adds one per connection, none per request.
Existing tasks, cancellation, connection slots and shutdown watches retain
ownership. Added a stalled TLS handshake deadline/slot-release regression.
No timer batching, epoll change or GC free-list workaround was introduced.

Installed release-wheel private-prefix Callgrind, compare `/io`, 12,000
requests, fixed 4,096 admission to avoid instrumentation-induced shedding:
c1 60,289 → 60,374 (+0.1%), c64 47,259 → 46,604 (-1.4%),
c256 49,387 → 46,793 (-5.3%), c1024 57,500 → 48,232 (-16.1%).
All measured responses were 200. Longer 60,000-request samples amortizing
connection setup: c256 47,243 → 46,799 (-0.9%); c1024 49,778 → 47,659
(-4.3%). Adaptive Callgrind profiles include 503s and connection churn and
are not comparable successful-request counts. Initial Hyper/HTTP2 boxing
experiment did not help and was discarded. Details and limitations:
`benchmarks/results/2026-11-08/concurrency/report.md`.

TLS-only installed full check passed 238 Rust tests (2 ignored), 987 Python
tests (34 skipped), 23 harness tests and all static checks; the added focused
TLS suite passed 8 tests. Merged newer main `78a8aef` in `dd7fb52`, resolving
its independently landed connection pinning once. Final merged installed-wheel
check passed 240 Rust tests (2 ignored), 991 Python tests (34 skipped),
23 harness tests, formatting, warnings-denied Clippy, Ruff and shell syntax.
Against current main (already pinned), `/io` instructions c1/c64/c256/c1024:
60,243/46,939/47,350/50,024 → 60,273/46,903/47,150/49,222. Incremental saving
is 0.42% at c256 and 1.60% at c1024; 1,024 open plaintext connections save
6 MiB RSS. The earlier larger table includes pinning already in main.
These changes reduce connection setup/teardown and retained state; isolated throughput/p99
qualification and the larger suspended-request gap remain open. Python routes
stay Tier 2, native health Tier 0, and the formal M0 gate stays open.

### 8 November 2026: HTTP/1 closing JSON responses

HTTP/1.1 native JSON replies now use the direct writer when the connection
closes, avoiding general Response/Body/header construction. HTTP/1.0 and
responses with deferred background work retain their existing paths. A Rust
wire-parity regression covers closing responses, status, security/request-ID
headers and repeated cookies. Focused regression passed. Private-prefix
Callgrind `/json`, native loop, c1, 6,000 successful responses: closing
48,059 → 45,612 instructions/request (-5.1%); keep-alive 33,329 → 33,325.
Installed-wheel full validation will follow the transport profiling round.

### 8 November 2026: HTTP/2 stream task copies

Hyper stream tasks now pin their futures before Tokio submission, avoiding
copies of the larger inline task state at completion. The existing Tokio
scheduler, stream cancellation, send deadlines, rapid-reset limits and
post-send cleanup ownership remain intact. Private-prefix Callgrind `/json`,
6,000 responses, one connection, 64 concurrent streams: h2c 56,483 → 54,453
instructions/stream (-3.6%); verified TLS h2 59,291 → 57,269 (-3.4%). At one
stream, h2c 69,025 → 66,853 (-3.1%). Installed transport tests pass (12 Rust,
11 Python), including flow-control stall, extended CONNECT and shutdown;
full installed-wheel validation follows below.

### 8 November 2026: TLS connection profiling and transport validation

Added a private-prefix transport profiler for HTTP/1 keep-alive/close, h2c,
verified TLS h2, and full/resumed TLS 1.3 connections. It rejects oha samples
unless every request returns 200; TLS profiles explicitly verify ticket use
and response completeness. Rustls retains its defaults: stateful cache 256,
two TLS 1.3 tickets, no stateless ticket encryption or early data. A new
installed regression verifies repeated TLS 1.3 ticket resumption and ALPN.
No TLS security settings changed. Same RSA-2048 key, 300 connections:
full TLS + HTTP/1 close 11,164,534 → 11,161,627 instructions/request;
resumed 1,306,369 → 1,303,584. Signing accounts for about 89.5% of the full
handshake profile; cache consumption is only 0.39% of the resumed profile.
No speculative crypto/session-cache optimization was retained.

At 128 h2c streams, 56,523 → 54,390 instructions/stream (-3.8%). Final
rebuilt installed-wheel full check passes **241 Rust tests** (2 ignored),
**992 Python tests** (34 skipped) and **23 benchmark-harness tests**, plus
formatting, warnings-denied Clippy, Ruff and shell checks. The profiler's
subsequent response-completeness assertion passed full/resumed client smoke
checks and focused lint/format. Full report, paired counts, invalid-profile
exclusions and reproduction:
`benchmarks/results/2026-11-08/transport/report.md`. Isolated throughput/p99
against Axum/uvicorn is pending with the orchestrator. Python routes remain
Tier 2, native health Tier 0; the formal M0 performance gate remains open.

### 8 November 2026: native synchronous handler queue

A Rust-owned bounded-count thread pool now serves the shared handler executor,
including sync handlers, dependencies/background work and loop executor calls.
Threads retain their Python thread state, release the interpreter while parked,
and execute outside the queue lock. Generic executor callers retain concurrent
Future semantics; internal calls enqueue without one. Lean sync results encode
natively on the pool thread and return in batches through the native bridge,
replacing per-result call_soon_threadsafe. The bridge reader remains registered
through in-flight shutdown cleanup. One configured sync_workers budget remains
shared by all lanes and internal synchronous work. No spin is enabled.

Installed CPython 3.13/native-loop c1 Callgrind, 6,000 matrix requests:
/sync 98,602 → 68,437; /bg 229,877 → 187,110. Focused worker-thread suite
passes 7 tests. Further lean-sync completion work and full validation follow.

### 8 November 2026: direct lean synchronous completions

Ordinary lean sync routes now enqueue their bound call and copied Context
without an asyncio Future or request Task. The native pool encodes eligible
results and the loop settles the response directly from the bridge completion
batch. Exceptions, returned awaitables, streaming/background responses start
a Task in the original request context and use the existing worker boundary.
Custom task factories retain their Task-based path. Native invocations remain
registered for cancellation; queued work checks cancellation before starting,
and running/queued work retains admission through actual completion. Shutdown
allocates waiters only for outstanding native calls and keeps the reader alive
until they finish. Executor join handles also survive executor GC and are
joined before Python interpreter teardown.

Installed matrix c1 /sync: 98,602 baseline → 42,922 instructions/request
(-56.5%; native queue intermediate 68,437). Focused suites pass 31 invoker,
7 pool/thread, 8 sync-stream, 26 execution, 8 post-send background and 5 native
integration tests. Concurrent profiles, spin experiment and full check follow.

### 8 November 2026: native sync pool qualification

Final installed-wheel matrix c1 instructions/request (6,000 requests, 2,000
warmup, private vgdb prefixes): /sync 98,602 → 43,000 (-56.4%), /bg
229,877 → 187,724 (-18.3%), /json 33,336 → 33,649 (+0.94%). At c64
(12,000 requests), /sync 73,094 → 32,169 (-56.0%), /bg 207,527 →
165,037 (-20.5%), /json 27,916 → 28,017 (+0.36%). All measured responses
were 200. A detached 64-check spin before parking increased /sync instructions
4.7% at c1 and 1.0% at c64; removed without a demonstrated throughput gain.

Shutdown waiters now resist repeated cancellation until native calls finish;
explicit CancelledError bypasses application handlers, and fallback Task
scheduling failures settle and release the invocation. Regression tests cover
queued cancellation/admission, context restoration and native worker lifetime.
The file-response test now waits boundedly for post-send background deletion.
CPython 3.14t runtime qualification remains outstanding; no unsafe GC/GIL
workaround is used. Isolated one-core throughput was requested from the
orchestrator; shared-host rates do not establish acceptance. Full details:
`benchmarks/results/2026-11-08/native-sync/report.md`.

Final rebuilt/installed-wheel `BESSEMER_REQUIRE_NATIVE=1 ./scripts/check.sh`
passes: 241 Rust tests (2 ignored), 999 Python tests (34 skipped), 23 benchmark
harness tests, formatting/lint/source checks. This includes the post-send
cleanup assertion correction; the final full rerun exits 0.

### 8 November 2026: HTTP middleware call path

Merged local main `3c253e7` before this round. No-input async middleware routes
now call the handler inside the existing request boundary, retaining context
restoration, error/response handlers and cancellation. The outermost middleware
avoids a wrapper/adoption coroutine; only streams/files enter discarded-response
cleanup tracking. Parameterized/sync/dependency routes retain full dispatch.

Installed-wheel CPython 3.13.15/native-loop matrix c1, 6,000 requests following
2,000 warmup, private vgdb prefixes: `MW=1 /json` 91,798 → 80,761 instructions
(-12.0%). No-MW controls /sync 42,946, /bg 188,005, /json 33,571 remain within
0.23% of prior qualified counts. The supplied 68k /sync count was not reproduced
with this workload. Request creation/response wrappers and /bg's Python
post-send path remain. No shared-host throughput qualification is claimed;
Tier 0 health/Tier 2 Python labels and the open M0 gate remain unchanged.
Details: `benchmarks/results/2026-11-08/middleware/report.md`.

Final rebuilt and installed wheel passes `BESSEMER_REQUIRE_NATIVE=1
./scripts/check.sh`: 241 Rust tests (2 ignored), 1,001 Python tests (34 skipped),
23 benchmark harness tests and all static checks. Isolated one-core `MW=1 /json`
throughput comparison was requested and acknowledged; results are pending.

### 8 November 2026: compact JSON with delivery-owned background work

Compact JSON replies now carry the AfterResponse guard and admission through
successful delivery or abandonment. HTTP/1 starts work only after write_all;
write failures/drop skip tasks but retain admission through cleanup. General
HTTP/2 responses retain the same extension guard. Installed-wheel matrix c1
/bg 188,267 → 184,363 instructions/request (-2.1%), 6,000 requests after 2,000
warmup with private vgdb prefixes. Eight delivery tests pass on both loops;
a new native writer regression covers successful and failed writes. Further
pool handoff optimization and final full qualification follow.

### 8 November 2026: native generic sync completion batches

Internal synchronous work now uses a native pool item with its copied Context,
arguments, cancellation boundary and loop waiter. Results join the existing
bridge completion batch rather than allocating Python invoke/settle closures
and call_soon_threadsafe handles. The Python fallback remains for workers
without an attached native bridge. Cancellation still waits for actual thread
completion, including acquired-resource registration. Installed-wheel c1 /bg
184,363 → 175,655 instructions/request (-4.7%; baseline 188,267). Focused
suites pass 10 delivery, 26 execution, 10 worker/thread and 8 sync-stream tests.
Final full validation and other controls follow this checkpoint.

### 8 November 2026: native BackgroundTasks-only handler binding

Async handlers whose sole binding is BackgroundTasks now take native argument
binding and request setup, then the existing worker response/deferred-task
boundary. Dependency, middleware, Request, validation and other binding shapes
retain fallback. Route and response task collections merge in order, including
streams. Final c1 /bg after compact delivery and native generic completions:
188,267 baseline → 162,441 instructions (-13.7%; prior sample 161,625).
Controls /sync 43,062 and /json 33,420 stay close to prior 42,946/33,571.

Fixed integration issues found by qualification: clear native sync submitters
at lane detach to allow App/loop collection, and exclude no-body statuses from
compact replies even without headers. A 204 background response now joins the
lean/dependency wire-equality regression (GET and HEAD). Installed-wheel full
check passes 242 Rust tests (2 ignored), 1,004 Python (34 skipped), 23 harness
and static checks. Task callable classification is the next measured candidate.

### 8 November 2026: BackgroundTask callable classification and final qualification

Repeated inspect calls were the largest measured background-only cost. Exact
Python functions with an empty exact attribute dictionary now classify from
current code flags. No cache hides __code__ replacement; attributed/marked
functions, partials, methods, callable objects and dict subclasses retain the
existing inspect path. Three new tests cover these mutation/wrapper semantics.

Final installed-wheel matrix c1 /bg: 188,267 → 120,412 instructions/request
(-36.0%). Classification alone cuts 162,441 → 120,412 (-25.9%). Final controls
/sync 43,062 and /json 33,420 remain close to 42,946 and 33,571 baseline; they
do not call the changed classifier. Matched-response probes isolate the final
remaining path: /json 33,554; injected empty collection 43,771; one async task
87,644; one sync task 119,968. Post-send Task/waiter removal remains undone.

Final built/installed CPython 3.13.15 wheel passes the full required native
check: 242 Rust tests (2 ignored), 1,007 Python tests (34 skipped), 23 harness
tests and all static checks. Fourteen private-prefix profile status distributions
were inspected: all measured requests return 200. Actual 3.14t runtime and
isolated throughput remain unqualified here. Native health stays Tier 0, Python
routes Tier 2; the M0 gate stays open. Full stage counts, probe reproduction,
qualification corrections and gaps:
`benchmarks/results/2026-11-08/background/report.md`.

204 passed + 2 ignored, `bessemer-m0` bin: 3; 0 tests removed), 992 Python
tests ran (958 executed, 34 skipped) and 23 harness tests passed, with Rust
formatting, warnings-denied Clippy, Ruff lint and formatting and shell syntax
green.
