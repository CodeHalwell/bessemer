# Rust-side performance design

Status: plan, November 2026. Phases land as separate pull requests; each one
records its measured result in the table at the end.

The goal is for a Bessemer route that does little work in Python to cost
close to what the same route costs in Axum, while keeping the FastAPI-compatible
API in [fastapi-parity.md](fastapi-parity.md) and the frozen 1.x surface
(`bessemer.__all__`, CLI flags, `BESSEMER_*` settings) exactly as they are.
Everything here is internal.

## How to measure

Requests per second on one server core is a poor metric on the 4-vCPU
development host, because a single `oha` core tops out at about 130k/s. Axum
on one core only reached 0.84 to 0.90 CPUs in the runs below, so its req/s is
the client's limit, not Axum's. The primary metric is therefore **CPU time per
request**: the server's `task-clock` divided by the requests it completed in the
same window. It is independent of the client ceiling and adds up across
threads.

[`benchmarks/profile/profile_route.sh`](../benchmarks/profile/profile_route.sh)
does this. It pins the server to CPU 0 and `oha` (64 connections) to CPU 3,
measures with `perf stat`, records a `perf` profile, and prints req/s and
µs CPU per request. `PYTHONPERFSUPPORT=1` names Python frames in the profile.
Every phase reports both metrics, and the comparison routes in
`benchmarks/compare/` stay the reference workload.

CPU time still varies by about ±7% between runs on a shared VM, which hides
changes of a few percent. For those,
[`benchmarks/profile/instructions.sh`](../benchmarks/profile/instructions.sh)
runs the worker under Valgrind's callgrind and counts user-space instructions
for a fixed number of requests. It repeats to about 0.1% and leaves a
per-function profile for `callgrind_annotate`, but it does not see kernel
time.

CI applies the same measure on every supported Python (3.12, 3.13, 3.14 and
3.14t, plus pre-release 3.15 and 3.15t, which report without gating):
the `Performance` workflow builds the base commit and the candidate in one
job and runs
[`benchmarks/profile/instructions_compare.py`](../benchmarks/profile/instructions_compare.py)
on `/json`, `/items/7?q=abc` and the Pydantic `/echo` route, on both the
asyncio and Rust loops. A route that costs more than 2% more instructions per
request fails the job. Req/s from `benchmarks/compare/run_compare.py` goes in
the job summary for information only.

## Where the time goes today

Measured on `main` at `04bbc82`, CPython 3.13, one worker, `GET /json`
(`{"ok": true, "count": 42}`) and `GET /items/7?q=abc`.

| | Bessemer `/json` | Axum `/json` | Bessemer `/items` | Axum `/items` |
| --- | ---: | ---: | ---: | ---: |
| req/s on one core | 77.4k | 131k (client-bound) | 60.2k | 135k (client-bound) |
| CPU per request | **12.8 µs** | **6.6 µs** | **16.5 µs** | **6.7 µs** |

Split of the `/json` 12.8 µs by thread and by code:

| Where | Bessemer | Axum | Gap |
| --- | ---: | ---: | ---: |
| Kernel (TCP over loopback, syscalls, scheduling) | 4.9 µs | 4.8 µs | 0.1 µs |
| Rust on the I/O thread (incl. allocator and libc) | 4.1 µs | 1.9 µs | 2.2 µs |
| Python thread (interpreter, asyncio, Bessemer's worker) | 3.8 µs | — | 3.8 µs |

What the profile says:

1. **The kernel is not the gap.** Both servers spend about 4.8 µs per request
   in the kernel, most of it in `sendto`, which on loopback also runs the
   client's receive path. Syscalls are already batched: about one `sendto`,
   1.25 `recvfrom` and 0.27 `epoll_wait` per request, and almost no futex or
   wake-up writes while the server is busy. io_uring can trim the syscall and
   epoll share of this, not the TCP work itself.
2. **The Python thread is the largest single cost, and almost none of it is
   the handler.** In a profile with Python frames named, the handler body
   (`json_route`) is about 2% of the Python thread's time. The rest is
   Bessemer's own orchestration: `_Lane.drain`, building `_NativeInvocation`
   and its `contextvars.Context`, `Request._bare`, `_plain_response`,
   `_json_response`, `_finish` and `ticket.complete`, plus asyncio's
   `_run_once`. On `/items` the Python thread grows to about 5.8 µs because
   arguments are converted to JSON values in Rust, rebuilt as Python objects
   and validated again in Python.
3. **The Rust request path does about twice Axum's work.** The largest items:
   `Dispatcher::call` and `handle` (0.6 µs together), the Tokio scheduler
   (0.8 µs, multi-thread scheduler with one worker), allocation (mimalloc
   0.6 µs, `memmove` 0.6 µs), building `HeaderMap`s and `Extensions` for the
   request and the response (0.5 µs), the per-request `timeout_at` timer and
   `Sleep::reset`, and several `Instant::now()` calls (0.2 µs in the vDSO).
   On `/items`, query parsing into a `HashMap<String, String>` and path
   parameters into another add 0.3 µs of allocation and drop.

## Phases

Each phase is independently shippable, keeps the full test suite green and
leaves behaviour unchanged. Expected gains are CPU per request on `/json` on
one core unless stated, and assume the earlier phases have landed.

### Phase 0: measurement (this change)

The profiling script above, this document, and the baseline numbers. Each
later PR runs the script before and after and records the result here.

### Phase 1: lean native request path (I/O thread)

Remove the fixed Rust work that does not apply to the route being served.

- **Compiled route plans.** Resolve every parameter's source to an enum and
  precompute per-route flags (`needs_form`, `body_count`, reads cookies, reads
  headers, has a native body plan) when the route table is built, instead of
  string comparisons over `route.parameters` on every request.
- **No maps for absent inputs.** Parse the query string only for declared
  keys, into a small vector; decode path parameters into a small vector
  rather than a `HashMap<String, String>`. Routes without parameters allocate
  nothing for them.
- **No per-request timer.** Replace `tokio::time::timeout_at` around each
  request with the connection's existing header `Sleep`, reset to the request
  deadline while a request is in flight. One timer per connection, reset in
  place, and the deadline checks that already exist stay as they are.
- **One clock read per request.** Read `Instant::now()` once when the head is
  parsed and pass it down.
- **Lean request and response objects.** Pass the peer address and TLS state
  as arguments instead of `Extensions` entries (each insert allocates a map).
  For responses the worker already encodes to bytes (`json_bytes`, `bytes`),
  carry status, content type and body straight to the HTTP/1 writer, which
  formats the head into the connection's reused output buffer. Controls that
  only add headers (request id, security headers, CORS) write them into the
  same buffer; compression, metrics scrapes, streams and HTTP/2 keep the full
  `Response` path.
- **Reused completion slot.** Replace the per-request `oneshot` channel with
  a completion slot owned by the connection and reused for each request.

Expected: Rust on the I/O thread from about 4.1 to 2.5 µs, so `/json` from
12.8 to about 11.2 µs (+14% req/s on one core). `/items` gains a further
0.3 µs from the parameter changes.

### Phase 2: native invoker (Python thread)

Move the per-request orchestration on the loop thread from Python into Rust,
keeping every observable behaviour: each handler runs in its own `Task` with
its own `contextvars.Context`, `asyncio.current_task()` works,
`current_request` is set, exceptions go through the same handlers, and
cancellation works as today.

- **Drain in Rust.** `Bridge.drain` creates the context, the coroutine and
  the eager `Task` itself, instead of returning tuples for `_Lane.drain` to
  unpack. `_NativeInvocation` becomes a native class, with the cancellation
  flag a plain atomic.
- **Cheaper request objects.** Build the bare `Request` from a per-route
  template in Rust, so `Request._bare` no longer runs Python code per request.
- **Native result encoding.** For lean routes, encode the handler's return
  value (exact built-in JSON values, or an exact declared Pydantic model)
  straight from the completed task in Rust, without `_plain_response`,
  `_json_response` and the response tuple. Anything else falls back to the
  Python path.
- **Batched completions.** Collect the responses finished in one loop
  iteration and publish them together after it, so the I/O thread is woken
  once per batch rather than once per response.
- **Typed scalar arguments.** Convert path, query, header and cookie scalars
  to Python `int`, `float`, `bool` or `str` directly in Rust, after the native
  checks that already run, and skip the second validation in Python when the
  native conversion already produced the exact annotated type. Today these go
  through `serde_json::Value` and `json_to_python`, then `_validate_arguments`.

Expected: the Python thread on `/json` from 3.8 to about 2.0 µs, and on
`/items` from 5.8 to about 2.8 µs. Together with phase 1, `/json` about
9.4 µs (+36% over today) and `/items` about 11 µs (+50%).

### Phase 3: thread-per-core runtime

- Run one Tokio `current_thread` runtime per core instead of a multi-thread
  runtime, with an `SO_REUSEPORT` listener each (or accept distribution where
  the kernel does not balance well). This removes the work-stealing atomics
  and the scheduler's share of the profile.
- Keep each request on the core that accepted it, from parse through the
  Python handoff to the write, and pin each core's Python loop thread next to
  its I/O thread.
- On free-threaded builds this composes with the existing `loops` setting:
  one I/O runtime and one asyncio loop per core in one process.

Expected: about 0.5 µs per request from the scheduler, more under
multi-core load where today's cross-core wake-ups and cache traffic grow.

A warning from the phase 1 work: simply dropping to one Tokio worker thread
when the process is pinned to one CPU halved `/json` throughput (77k to 39k
req/s). Today the hand-off from the Python thread relies on a second, often
idle, Tokio worker to absorb wake-ups. Thread-per-core therefore has to come
with the hand-off redesign (phase 2's batched completions, then phase 4),
not before it.

### Phase 4: a Rust asyncio event loop on the I/O thread

The remaining structural cost is the hop between two threads on every
request: the wake-up socket, asyncio's selector, `_run_once` and
`Handle._run`, and the context switches when either side goes idle. Phase 4
implements an asyncio event loop in Rust (the `asyncio.AbstractEventLoop`
API: `call_soon`, `call_at`, futures and tasks, readers and writers, sockets,
`run_in_executor`, signals) on the same per-core reactor that serves HTTP, in
the manner of uvloop. Requests are then scheduled onto the loop directly as
callbacks on the I/O thread, with no queue, socket pair or second thread.

- Optional at first (`loop = "bessemer"`), with the standard loop as the
  default until it passes the qualification suite and the relevant parts of
  CPython's asyncio tests, then the default with the standard loop as a
  fallback.
- Handlers that use third-party asyncio libraries (database drivers, HTTP
  clients, TLS) are the compatibility risk; they are the acceptance tests.

Expected: about 1.5 to 2 µs per request from the removed hop and the Python
loop overhead. With phases 1 to 3, `/json` about 7.5 µs, within about 15% of
Axum's 6.6 µs.

#### What phase 4a measured, and the revised plan

Moving the worker onto the Rust loop (4a) removes asyncio's Python
`_run_once` work: at one connection the loop thread's user time per request
roughly halves (about 11 to 6µs). It does not touch the larger cost, which is
the hand-off between threads. Per-thread CPU at one connection on `/json`
(server pinned to one core, from `/proc`):

| Thread | user µs/req | system µs/req | context switches/req |
| --- | ---: | ---: | ---: |
| asyncio loop (Python) | 11.0 | 2.2 | 1.6 |
| Tokio worker 1 | 4.0 | 8.0 | 1.4 |
| Tokio worker 2 | 4.0 | 8.0 | 1.4 |

At 64 connections the same request costs about 11µs in all, because work
arrives in batches and threads rarely sleep. A single Tokio worker, or a
current-thread runtime, doubles CPU per request at 64 connections (10.7 to
22µs): with the Python thread on the same core, each request then ping-pongs
between the two threads with two context switches.

So the next step is a single-thread worker (4b): the HTTP connections and the
Python loop on one thread, as uvicorn with uvloop does, with one worker
process per core for parallelism.

- The Rust loop's reactor is replaced by a Tokio current-thread runtime on
  the loop thread: readers and writers become `AsyncFd` interests, timers
  Tokio sleeps, and `call_soon_threadsafe` a wake of that runtime. The
  ready queue and `Handle` stay as they are.
- `transport::run` is spawned on the same runtime. A request reaches the
  native invoker by a direct call on the same thread, and its response by a
  local wake, with no queue, socket pair or futex.
- The GIL is released while the thread waits for I/O, so sync handlers on
  the executor and other Python threads still run.
- The current two-thread layout stays the default until 4b measures better
  at both one and many connections.

Expected: at one connection, about 30 to 15µs per request, from removing two
of the three thread wake-ups; at 64 connections, about 10.7 to 8µs.


#### What phase 4b measured

4b is in, as the behaviour of the `event_loop="bessemer"` setting (public and
opt-in on Linux since it was added; asyncio elsewhere) with one loop per worker. Several loops per worker keep the separate
two-thread runtime.

- The loop owns a Tokio current-thread runtime (`crates/bessemer-loop/src/driver.rs`).
  Its epoll descriptor is registered with that runtime, so the loop waits in
  Tokio rather than in `epoll_wait`, and the server's connection tasks run on
  the loop's thread between its callbacks. The loop keeps its own epoll
  reactor for Python's readers and writers; replacing it with `AsyncFd`
  interests was not needed.
- Python callbacks run inside the runtime's `block_on`, so a task they wake
  (a response sent on a oneshot) is queued locally, with no driver wake-up.
- The bridge wakes the loop with a flag and a `Notify` rather than a byte on
  its socket pair, and the loop runs the bridge's reader as if epoll had
  reported it. That removes a `sendto` and a `recvfrom` per request.
- The loop drains its epoll descriptor only when Tokio reports it readable,
  and lets ready tasks run without the scheduler's extra driver poll.
- A callback queued from a Tokio task on the loop's thread (`call_soon` or
  `call_soon_threadsafe`) wakes the runtime through its `Notify`, not the
  self-pipe.
- Streaming responses: the pump runs on the loop's thread, so a read returns
  buffered chunks as bytes, or a bare future that the producer settles with
  the next chunk, instead of a task, a concurrent future and a pipe wake-up per
  read; the last batch carries the end when nothing runs after it. At one
  connection, a 4-chunk stream fell from about 363k to 194k user-space
  instructions per request, and each chunk the generator awaits between from
  about 36k to 27k.

System calls per request at one connection (strace): asyncio worker 8
(two `sendto`, two `recvfrom`, three `epoll_wait`, a futex pair and a
`write` per hand-off); 4b 4 (one `sendto`, one `recvfrom`, two `epoll_wait`).
Per-thread time at one connection is now 12.7µs user and 13.1µs system on the
one thread, with one voluntary context switch per request. Most of the system
time is the loopback TCP send, which runs the receiver's side in the sender's
context.

The expected 15µs at one connection was not reached: about 26µs, of which 13µs
is the kernel. At 64 connections it is about 9.4µs against an expected 8µs.
Making the option public is a later decision; it stays internal for now.

### Phase 5: io_uring I/O backend (Linux)

With its own per-core loop (phases 3 and 4), Bessemer can drive the sockets
through io_uring: multishot `recv` into a provided-buffer
ring, `send` submitted in batches once per loop iteration, and registered
file descriptors. Accepts stay on readiness, so that workers sharing a
listener split its connections. Everything else keeps working on epoll.

- Feature-detected at start-up, with epoll as the fallback. Many container
  runtimes block io_uring in their default seccomp profile, so the fallback
  is a supported path, not a corner case.
- A standalone spike first: a minimal responder for the `/json` workload on
  epoll and on io_uring, on the same host, to measure the real kernel saving
  before integrating.

Expected: the syscall entry, `epoll_wait` and `fdget` share of the 4.8 µs
kernel time, roughly 0.5 to 0.8 µs per request on keep-alive loopback
traffic, and more with many connections. This is the one phase that can take
Bessemer below Axum on Tokio.

### Phase 6: allocation and copy discipline

Runs alongside the others, profile-led.

- Write a Python `bytes` body without copying it into a `Vec`: keep the
  object alive until the write completes and release references in a batch on
  the loop thread (or freely on free-threaded builds).
- `writev` the response head and a large body together; keep `sendfile` for
  static files.
- Keep request heads, jobs and completion slots in per-connection or
  per-core pools, and drop the `Box<Parsed>` allocation per head.
- `MSG_ZEROCOPY` only for large bodies (tens of KB and up), where it pays.

Expected: most of the 1.2 µs of `memmove` and allocator time on small
routes, and larger savings on large bodies.

## Target

| Phase | `/json` CPU per request | vs today |
| --- | ---: | ---: |
| Today (`04bbc82`) | 12.8 µs | — |
| 1: lean request path | ~11.2 µs | +14% |
| 2: native invoker | ~9.4 µs | +36% |
| 3: thread-per-core | ~8.9 µs | +44% |
| 4: Rust asyncio loop | ~7.5 µs | +70% |
| 5: io_uring | ~6.8 µs | +88% |
| Axum on Tokio | 6.6 µs | |

These are estimates from the profile, not promises. Each phase replaces its
estimate with a measurement when it lands.

## Working in parallel

- Phases 1 and 6 change the HTTP/1 connection, router and request handler
  (`src/http1.rs`, `src/router.rs`, `handle` in `src/lib.rs`) and are done
  together.
- Phase 2 changes the loop side (`src/bridge.rs`, `src/conversion.rs`,
  `python/bessemer/worker.py`). Its interface with phase 1 is `Pending` and
  `decode_response`; the response envelope from Python stays as it is until
  both have landed.
- Phase 5 starts as a standalone spike (a new crate under `crates/`) and
  does not touch the server until it has numbers.
- Phases 3 and 4 start as a standalone event-loop prototype under
  `crates/`, measured against the standard loop, and are integrated after
  phases 1 and 2.

## Results

| Phase | PR | `/json` µs/req | `/json` req/s (1 core) | `/items` µs/req | Notes |
| --- | --- | ---: | ---: | ---: | --- |
| Baseline | — | 12.8 | 77.4k | 16.5 | `04bbc82`, CPython 3.13 |
| 1a: fewer copies and allocations | #53 | | | | Instructions per request: `/json` 57,959 to 55,615 (−4.0%), `/items` 79,154 to 76,375 (−3.5%), `/healthz` (Rust only) 21,392 to 19,177 (−10.4%). CPU time is within run-to-run noise. |
| 1b: query parsing without maps | #55 | | | | Instructions per request: `/items` 76,111 to 74,165 (−2.6%), `/json` 55,615 to 54,323 (−2.3%). |
| 1c: no content-length header build | #56 | | | | Instructions per request: `/json` 54,346 to 53,573 (−1.4%), `/items` 73,956 to 73,175 (−1.1%). The remaining Rust-side work is spread thinly: header maps about 4k and allocation about 2.6k of about 24k native instructions per `/json` request. The next larger step, typed route plans, changes the argument hand-off to Python and waits for phase 2. |
| 2: native invoker | #54 | 11.5 | 86.4k | 13.4 | From that PR's own measurement. Instructions per request at 8 connections: `/json` 53,573 to about 40,000, `/items` 73,175 to about 45,300. |
| 1d: arguments as a list | #57 | | | | Instructions per request at one connection: `/items` 63,444 to 62,952 (−0.8%), `/json` unchanged within 0.3%. At 8 connections the count moves about 1% between runs with the loop's batch sizes, so comparisons from here use `CONNECTIONS=1`. At one connection each request is its own batch and costs about 18k more instructions than at 8 (`/json` 58.5k against 40k): the loop wake-up, asyncio's `_run_once` and Tokio parking. Running the worker on the phase 4 spike loop cuts that to 47.4k on `/json` (−19%) and 52.2k on `/items` (−17%). |
| 4a: Rust loop in the wheel (internal) | | | | | Workers on `bessemer._loop` (`event_loop="bessemer"`, not yet a public setting). CPU per request on `/json` at one connection 37.3 to 31.1µs (−17%, three alternating pairs, 16.2k to 17.6k req/s); at 64 connections unchanged at about 10.7µs. The full Python suite (681 tests) passes with every worker on it, as do CPython's asyncio tests (2011 of 2011) and the loop tests on 3.14t. |
| 4b: single-thread worker (internal) | | 9.4 | 105k | 10.4 | With `event_loop="bessemer"` and one loop, the HTTP server runs in a Tokio runtime embedded in the loop, on the loop's thread. µs/req and req/s are at 64 connections, the mean of three alternating runs against asyncio (10.7µs and 92k req/s on `/json`, 11.6µs on `/items`). At one connection, 36.3 to 25.7µs on `/json` (−29%) and 37.9 to 27.2µs on `/items` (−28%). Four system calls per request at one connection, down from eight. The full Python suite passes with it as the default loop. |
| 4c: no per-iteration buffer zeroing | | | | | The loop zeroed its 256-entry epoll buffer (3 KB) on every iteration, about 3,000 instructions a request on the single-thread worker. The buffer is now made once per run, or left uninitialised where `epoll_wait` fills it. Instructions per request at one connection on the Rust loop: `/items` 51,091 to 47,797 (−6.4%), `/json` 43,093 after. |
| 1e: compiled route plans | | | | | Each parameter's source and type are enums resolved when routes load, header parameters keep a parsed `HeaderName`, and each route carries its flags (reads cookies, has files, reads a form, body count) instead of scanning its parameters by string on every request. Instructions per request at one connection on the Rust loop: `/items` 47,797 to 47,737, `/json` 43,093 to 42,957 (both within 0.3%). The gain is small, as phase 1 expected for these routes; it mainly removes per-request work that grows with the parameter count. |
| 1f: batch buffers and fewer per-request maps | | | | | The invoker swaps its job and cancel buffers with the bridge's instead of taking fresh ones (a reallocation on every batch), keeps the eager-task options dict instead of building one per batch, matches cancellations in a short sorted list instead of a hashed set, and inserts the processing deadline into a response's extensions only when it may be compressed (an extensions map per response otherwise). Instructions per request at one connection on the Rust loop: `/json` 43,039 to 41,488 (−3.6%). |
| 1g: no per-request timer | | | | | Each server keeps one FIFO of request deadlines per distinct timeout (deadlines of one timeout are monotone, so no heap) and one sweeper task that wakes expired requests, instead of a Tokio timer registered and removed per request (`src/deadline.rs`). A request registers only once it first waits, and reuses the finished slot at the front of its FIFO, so steady traffic neither allocates nor touches the timer wheel. Not the connection's header `Sleep` this section first proposed: HTTP/2 also reaches `handle` and has no such timer. Instructions per request at one connection on the Rust loop (two runs each): `/json` 41,343 and 41,421 to 40,705 and 40,774 (−1.55%); `/items/42?q=y` 45,977 to 45,208 (−1.7%). The 404 `/items` never reaches the timer (18,253 to 18,249). |
| 1h: response headers written directly | | | | | On HTTP/1 the request id and the three static security headers no longer go into the response's `HeaderMap`: `Controls::finish` records them for the writer, which skips any value the application set for those names (as `insert` would have replaced it) and appends ours to the head. HTTP/2, upgrades and every other caller still get them in the map. Instructions per request at one connection on the Rust loop: `/json` 41,716 to 40,405 (−3.1%; a repeat pair gave 41,735 to 40,350), `/items/42?q=y` 46,334 to 45,108 (−2.6%). `HeaderMap` insertion and hashing on `/json` fell from about 1,350 to about 690, and `write_parts` self cost from 1,137 to 646. CORS headers still go through the map. |
| 1i: leaner request head | | | | | The HTTP/1 parser resolves the sixteen most common header names, when sent in lowercase, by one fixed-length comparison instead of `HeaderName::from_bytes` (other spellings still go through it), fills the request's header map in place, and builds the request with its empty body so it is not split and rebuilt; the parsed head stays boxed until the exchange. `Controls::begin`'s result shrinks from 456 to 232 bytes (a rejection response and the tracing span are boxed, both rare), so its several moves per request copy half as much. Instructions per request at one connection on the Rust loop, with a private vgdb prefix: `/json` 41,482 to 40,506–40,550 (−2.3%), `/items/42?q=y` 46,009 to 45,221 (−1.7%). |
| 1g to 1i together | | | | | Measured on one tree with a private callgrind instance per run: `/json` 41,716 to 38,828 (−6.9%), `/items/42?q=y` 46,334 to 43,357 (−6.4%) against `main` at `89b25bd`. |
| 5: io_uring backend | | 11.1 | 89.1k | | `io_backend="io_uring"` on the single-thread worker: multishot accept and recv into a provided-buffer ring, sends batched into one `io_uring_enter` per pump turn, `SINGLE_ISSUER \| DEFER_TASKRUN`, epoll fallback. Server core saturated (oha on three cores), against the same worker on epoll: `/json` at 64 connections 13.8 to 11.1µs (71.6k to 89.1k req/s), at 1024 15.1 to 11.5µs (65.3k to 85.3k). With oha on one core, 11.2 to 10.8µs at 64 connections. See `benchmarks/results/2026-11-05/io-uring-backend`. Not measured with the profile script, so not directly comparable with the rows above. Accepts now use readiness rather than multishot accept, so workers sharing the listener split connections: with two workers, 172k against epoll's 162k req/s at 64 connections (`io-uring-workers`). |
