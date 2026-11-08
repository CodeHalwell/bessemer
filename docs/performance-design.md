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
| 6: settled JSON to the HTTP/1 writer | | | | | A native-encoded JSON result of up to 8 KiB (not HEAD, not 204/205/304) reaches the waiting request as a `JsonReply` (status, body, response byte permit) instead of a `Response` with a `HeaderMap` and a boxed body. On a kept-alive HTTP/1.1 connection with no CORS or compression configured, `write_json` formats the head straight into the output buffer; closing connections, HTTP/1.0, HTTP/2, middleware and every other response keep the general path, and the golden-bytes test (`tests/python/test_response_fast_path.py`) holds both to the same bytes. The application future is pinned on the stack in `exchange` (no 2.2 KB copy and allocation per request; the connection's future is that much larger), and the lane load counter is only kept when a worker runs several loops. Instructions per request at one connection, Rust loop (asyncio loop in brackets): `/json` 37,016 to 34,849 (−5.9%; 52,080 to 49,890), `/items/7?q=abc` 41,706 to 39,746 (−4.7%; 57,082 to 54,867), `POST /echo` 78,868 to 76,986 (−2.4%; 94,303 to 91,804). Not done: the reusable completion slot (the oneshot is still one `Arc` allocation per request) and a one-atomic byte budget; the budget still takes a semaphore permit. CPU time was not measured (shared machine). |
| 5: io_uring backend | | 11.1 | 89.1k | | `io_backend="io_uring"` on the single-thread worker: multishot accept and recv into a provided-buffer ring, sends batched into one `io_uring_enter` per pump turn, `SINGLE_ISSUER \| DEFER_TASKRUN`, epoll fallback. Server core saturated (oha on three cores), against the same worker on epoll: `/json` at 64 connections 13.8 to 11.1µs (71.6k to 89.1k req/s), at 1024 15.1 to 11.5µs (65.3k to 85.3k). With oha on one core, 11.2 to 10.8µs at 64 connections. See `benchmarks/results/2026-11-05/io-uring-backend`. Not measured with the profile script, so not directly comparable with the rows above. Accepts now use readiness rather than multishot accept, so workers sharing the listener split connections: with two workers, 172k against epoll's 162k req/s at 64 connections (`io-uring-workers`). |
| WI-3: single-pass JSON bodies | | | | | A small request body already in the read buffer after the head (`Content-Length` known) is copied out once into a one-frame `ReadyBody`, so the HTTP/1 handler creates no channels, no `StreamedBody`, no demand round-trip and no `pump_body` future; chunked bodies, bodies not yet fully received and `Expect: 100-continue` with an empty buffer keep the channel path. `buffer_body` returns a single frame as the same `Bytes` (no second copy) and `raw_body` is `Bytes` from there to `parse_model`. On a route whose only parameter is a plan-backed body model, the I/O side no longer runs `validate_json`: `parse_model` is the one JSON pass and returns `ParseError` for bad syntax (a typed mismatch stays `None`), and the loop answers the identical 422 `json_invalid`; every other route keeps `validate_json` where it was. `application/json` is recognised by a case-insensitive comparison and any other media type is parsed once per request. Instructions per request at one connection, `POST /echo` with the 185-byte order: Rust loop 79,392 to 68,466 (−10.9k, −13.8%; a first pair 79,056 to 68,844), asyncio 94,195 to 84,124 (−10.1k, −10.7%). `/json` 37,019 to 37,049 and `/items/7?q=abc` 41,735 to 41,849 on the Rust loop (within 0.3% over repeated runs), asyncio `/json` 52,068 to 51,673 and `/items/7?q=abc` 56,909 to 57,174. CPU time was not measured because the machine was shared. |
| WI-4: request head in one pass | | | | | The HTTP/1 parser counts Host, Transfer-Encoding and Content-Type headers in its single pass over the head and marks the request `head_checked` only when every check `protocol_rejection` would make passes (Transfer-Encoding with Content-Length never reaches it); any other head still goes through the full transport checks, so each 400 is answered byte for byte as before (golden test), and HTTP/2 and hyper upgrades keep them all. The media-type check matches the common spellings byte for byte before falling back to `mime`. Generated request ids are formatted in an inline buffer and owned by one `Bytes` allocation that carries its refcount (no promotion on first clone). Path parameters are a list with the route's shared names instead of a `HashMap<String, String>`, and int, float and bool inputs stay typed (`conversion::Argument`) rather than going through serde_json's arbitrary-precision `Number`; ints beyond i64 keep their digits (`Big`). Instructions per request at one connection (`CONNECTIONS=1`), base to candidate: Rust loop `/json` 36,968 to 36,393 (−575, −1.6%), `/items/7?q=abc` 41,729 to 40,020 (−1,709, −4.1%), POST `/echo` 78,833 to 78,194 (−639); asyncio `/json` 52,165 to 51,042, `/items/7?q=abc` 57,050 to 54,831, POST `/echo` 94,185 to 92,715. This is short of the planned −3,000 and −4,000: httparse and the header map's hashing are most of the 5.4k head cost and stay, and moving the Host check into the parse (about 440 instructions) only relocates it. CPU time at 64 connections was not measured here. |
| 1j: one clock reading per request, inert telemetry, one `epoll_wait` | | | | | The HTTP/1 head parse stamps the request with the one clock reading its deadline and telemetry durations start from; the third reading after the submit is gone. With metrics, the access log and tracing all off (the default) a request takes no metrics reference, no clock reading and no `finished` swaps (`RequestTelemetry::inert`, decided once in `Controls`). `Date` is cached for the wall-clock second it was formatted for and found with one monotonic reading, not a `SystemTime` per response; the window is measured on the monotonic clock, so after a suspend or a step of the wall clock the header can be up to one second out until the window expires. A response written without ever waiting leaves that reading for the connection's next header wait to start from (a response that waited, or streamed, leaves none), so the head wait takes no reading of its own; `read_head` also polls the socket before the header timer, so a request that is already readable never polls the timer. The header timer is armed once per connection in `serve` (an explicit `reset`, because a Tokio `Sleep` has no timer entry until polled or reset), and the deadline is checked first on every pass of the loop, so a peer that keeps the socket readable, such as a flood of blank lines, is cut off within the header timeout plus the 1 ms resolution. A header or send deadline already within Tokio's 1 ms timer resolution of the new one is not reset (it never fires more than 1 ms early; skipped renewals do not add up). The loop's coarse wait for a distant timer reuses one Tokio `Sleep`, moved in place when its deadline differs by 1 ms or more, instead of registering and removing one per wait. The loop's body runs as a spawned task instead of the `block_on` future: a connection task that wakes the `block_on` future makes Tokio poll its I/O driver once more before polling it, a second `epoll_wait` per request at one connection. `strace -c -f`, 10,000 `/json` requests: `epoll_wait` 20,244 to 10,010 at one connection (2.02 to 1.00 per request), 683 to 679 at 64 connections (unchanged); `recvfrom` and `sendto` unchanged. Instructions per request at one connection, 3,000 requests, base and this tree alternated on the same machine: Rust loop `/json` 37,040 to 35,984 (−1,056; repeat pairs gave −864 and −966; measured spread about −860 to −1,060), `/items/7?q=abc` 41,848 to 40,769 (−1,079), `POST /echo` 110,451 to 108,550 (−1,901); asyncio `/json` 52,049 to 51,910 (−139), `/items/7?q=abc` 57,009 to 56,351 (−658), `POST /echo` within the 124.3k to 125.3k spread of the base itself. The 1,000-instruction target on `/json` is not met on every run: the Rust loop's measured gain is about −860 to −1,060 instructions and the asyncio loop's about −140, which gains only from the clock readings and the timer reuse in `http1`, not from the loop changes; the orchestrator accepted this and waived the bar. Where the Rust loop has no native timer, a `call_later` timer can fire up to 1 ms later than requested, because its coarse wait reuses one Tokio `Sleep` moved only when the deadline differs by 1 ms or more. What remains per request, from callgrind on the Rust loop: nine clock readings, of which Tokio's time driver takes two, the invoker (`start_all`) two, `http1` two (head parse, `write_date`), `handle` two (submit check, the deadline check after the reply) and the loop's coarse wait one. A request's deadline now starts at its head parse, so time in `Controls::begin` (an awaited shared rate-limit check, JWT work) counts against `request_timeout`, and a submit that overruns it is answered 504 by the deadline queue's next sweep rather than synchronously. CPU time and the `[vdso]` share at 64 connections were not measured, as that benchmark was off limits on the shared machine. |
| WI-1: lean invoker call path | | | | | Handlers are called by keyword through vectorcall from a prebuilt name tuple (no kwargs dict), `Task.cancelled` comes from the cached type, the clock is read once per batch, the response body is the encoder's own `Vec` instead of a copy, and the request's context, control, pending, encoded and step state is a plain cell on GIL builds (a `Mutex` on free-threaded builds). The eager `asyncio.Task` keeps a reused options dict, now stored once per task and cleared at the end of the batch: `Task` has no vectorcall, so a keyword call builds a dict anyway and cost more than the dict it replaced. Instructions per request at one connection with a private vgdb prefix, same machine state for each pair. Rust loop: `/json` 37,035 to 36,630 (−1.1%), `/items/7?q=abc` 41,888 to 40,694 (−2.9%), `/items/7` 40,917 to 39,926, POST `/echo` 79,404 to 78,704. asyncio loop: `/json` 51,490 to 51,500 (unchanged), `/items/7?q=abc` 56,840 to 55,847, `/items/7` 56,165 to 54,964, POST `/echo` 93,520 to 92,931. An earlier, quieter pass gave Rust loop `/json` 36,994 to 36,715 and `/items/7?q=abc` 41,774 to 40,800; the asyncio and `/echo` counts move by tens of thousands between passes on a loaded machine, so only the pairs are comparable. The brief's target of −2,500 on `/json` and `/items` was not reached: the invoker's own work in these requests is about 1.2k self instructions, and the rest of its 8k is CPython (`Task` init 6.3k with the handler inside it, `Context` creation and teardown about 1k, the request copy about 0.9k). A request that omits an optional scalar already stays on the native path (the server fills the declared default into the arguments), so there was no fallback to remove. After review: the clock is read per job again (a handler that blocks the loop must not let later, expired jobs start), the vectorcall arity is checked in release builds, and the collector entry of the GIL-only cell refuses a thread that does not hold the interpreter lock. Re-measured on the rebased tree, 3,000 requests at one connection, base to this branch. Rust loop: `/json` 37,020 to 36,643, `/items/7?q=abc` 41,779 to 40,720, `/items/7` 40,956 to 39,946, POST `/echo` 110,032 to 110,122 (a different body from the earlier pair, so not comparable with it). asyncio loop: `/json` 52,132 to 51,595, `/items/7?q=abc` 57,046 to 56,129, `/items/7` 56,173 to 54,990, POST `/echo` 124,421 to 124,851. CPU time at 64 connections was not measured (the shared machine rules out CPU-time runs). |
| Integrated (this branch) | | 10.5 | 94.8k | 10.8 | Final pass on the integrated branch (`3651173`) against `main` (`7475503`) and Axum; every figure is in `benchmarks/results/2026-11-06/final-pass/`. Branch default is `event_loop="auto"`, which resolves to the Rust loop on Linux; main default is the asyncio loop. The µs/req, req/s and `/items` figures in the columns are the branch default at 64 connections (200 responses only; none were refused), server on one core, oha on three, median of two runs. Server µs CPU per request, `/json`, `/items/7?q=abc`, `POST /echo`, at 64 connections: main default 13.73, 14.39, 17.91; branch default 10.52, 10.82, 13.43; Axum 11.64, 11.83, 14.61 (branch against main −23%, −25%, −25%; against Axum −10%, −9%, −8%). At 1024 connections the default `max_in_flight` of 128 made the branch's Rust loop answer 503 to 31 to 34% of requests on every route in that pass, where main's Rust loop answered none (main's asyncio loop 14 to 17%): `/json` 200 responses per second 44.0k against 76.7k for main's Rust loop. The cause was the loop body running as a spawned task (1j): Tokio runs spawned tasks in FIFO order, so every ready connection submitted a request and took an admission permit before the loop ran any. The bridge now hurries the loop once 32 jobs wait for it (`HURRY_AT` in `src/bridge.rs`; `LoopCore::hurry` has the `block_on` future poll the body out of turn, which Tokio does within 61 tasks). Re-measured after that fix, default settings, two runs: 0 refused at 1024 connections, branch default 11.67, 11.97, 14.44 µs/req (85.0k, 82.9k, 68.7k req/s) against main's Rust loop 12.69 on `/json` in the same session; `/json` at 64 connections 10.58 µs/req, 94.4k req/s (unchanged). Hurrying cannot help below about 61: Tokio polls the `block_on` future only between batches of 61 tasks, and each connection task takes a permit and submits in one poll, so up to 61 requests are admitted between two of the loop's turns, on main as on the branch (burst of 512 requests while the loop is busy: 232 refused at `max_in_flight` 32, 104 at 48, 0 at 64, the same for main's wheel). A request that finds no permit while the Rust loop on its thread has jobs it has not taken now hurries the loop, yields and tries again, up to 32 times (`admit_after_local_loop` in `src/lib.rs`); a full server still refuses at once. The burst then refuses none at 32, 48, 64 and 128; `/json` at 1024 connections sheds 0% at `max_in_flight` 32 (main 5.7%, previous wheel 6.0%; 78.2k against 63.4k 200 responses/s for main), 64 and 128; `/json` at 64 connections 11.16, 10.92, 11.22 µs/req against 11.50, 10.87, 11.45 for the previous wheel in alternating runs (unchanged). With `BESSEMER_MAX_IN_FLIGHT=4096` nothing is refused: main default 20.88, 22.52, 23.62; branch default 11.34, 11.80, 14.57 (−46%, −48%, −38%); Axum (which refuses none at the default) 13.55, 14.81, 20.61, so the branch is 16%, 20% and 29% below Axum; `/json` 87.5k req/s against 47.2k for main default and 73.2k for Axum. Instructions per request at one connection, 5000 requests, main to branch: Rust loop `/json` 37,021 to 32,883 (−11.2%), `/items/7?q=abc` 41,817 to 36,064 (−13.8%), `POST /echo` 79,200 to 63,101 (−20.3%); asyncio loop `/json` 52,154 to 48,685 (−6.7%), `/items/7?q=abc` 56,992 to 51,773 (−9.2%), `POST /echo` 93,401 to 79,504 (−14.9%). Two workers on CPUs 0-1, `/json` at 1024 connections, default `max_in_flight`: main default 15.56µs and 122k req/s with p99 29.7 ms and 2 to 4% refused; branch default 10.22µs and 183k req/s with p99 12.9 ms and 2 to 3% refused (oha was the limit, 1.03 of its two cores); Axum with two threads 8.61µs and 213k req/s with p99 10.8 ms (oha was the limit here too). `[vdso]` is 1.64% of the loop thread's samples at 64 connections on `/json` (2.49% before). |
| WI-5: response_model lists in one pass | | | | | A plain `list[Model]` response annotation compiles into a model plan and a new native encoder validates each dict item directly against the model's fields and writes the response in one pass (instances of the model are written from their `__dict__`), in declaration field order, so the bytes are `TypeAdapter.dump_json`'s and FastAPI's (regression tests compare them byte for byte, for a list of dicts and a returned body model of a model whose declaration order is not alphabetical and whose last field carries a serialization alias); everything else keeps Pydantic's validate-then-dump path, so bytes and errors are unchanged. JSON-exact mutable field defaults (exact lists and dicts of JSON values) now qualify, rebuilt per instance the way Pydantic deep-copies them, with the default's bytes pre-encoded for the write side; `None` defaults write `null` under any field type; a float field accepts an exact int (written as Pydantic writes it, a float). Encoding a float value goes through a bounded (128-slot, per-thread) direct-mapped cache of zmij-formatted bytes in the encoder's scratch. Instance building avoids the per-model values template and sets `__dict__` with `PyObject_GenericSetDict`. Measured with a private-vgdb, PID-targeted variant of `instructions.sh` at one connection; callgrind's `summary:` includes inclusive call-context records that vary with where zero and dump land under concurrent agents, so the numbers are the dumps' self costs. Merged-main wheel to this wheel: `/json` (matrix) 30,450 to 30,621 (+0.6%), `/json` (compare app) 30,281 to 30,504 (+0.7%), `/items/7?q=abc` 33,224 to 33,676 (repeat pairs 33,364 to 33,572 and 33,579; about +1.1% on average, under the CI gate), `/models` (`response_model=list[Item]`, matrix Item) 192,752 to 69,616 (−64%), POST `/item` 96,250 to 38,962 (−60%), GET `/big` 431,156 to 421,642 (−2.2%), `POST /echo` (comparison Order) 188,343 to 184,889 (−1.8%). Against the pre-merge main (byte-sorted keys): `/models` 197,249 to 70,337, POST `/item` 98,370 to 38,679, GET `/big` 472,162 to 462,917, `POST /echo` 190,767 to 188,266; an earlier quiet-machine pass with the stock script gave /models 210,180 -> 79,432 and /item 105,855 -> 41,688 on the `summary:` basis. |
| WI-6: float cache placement, wrapped response models | | | | | The float format cache lives in its own thread-local (fixed 128-slot table, built once per thread) instead of the shared encoder scratch, which with main's lazy fallback (2376f1e) removes the per-response 6 KiB hazard from both directions; on `/big` it nets about −8.7k instructions per request against main's wheel in a same-session pair (zmij 44.2M to 15.5M per 3,000 requests, thread-local access 19.3M). Wrapped response-model shapes — `Item | None` (either union member order), `list[list[Item]]`, `list[Item | None]`, `dict[str, Item]` and compositions — compile into the plan's root and are encoded in one pass with the list encoder's reading style and critical-section snapshotting on free-threaded builds, byte-identical to `TypeAdapter.dump_json` (pinned by unit and HTTP-level tests, also behind middleware). Final-wheel same-session self-cost pairs, main wheel (75a64bf) to this wheel, one connection: `/json` (matrix) 30,312 to 30,508 (+0.6%), `/json` (compare) 30,241 to 30,388 (+0.5%), `/items/7?q=abc` 33,225 to 33,392 (+0.5%), `/models` 192,726 to 70,232 (−64%), `/big` 433,456 to 427,955 (−1.3%), POST `/item` 95,304 to 38,983 (−59%), `POST /echo` 187,168 to 184,882 (−1.2%); wrapped shapes against main's Pydantic fallback: `Order | None` 129,169 to 51,922 (−60%), `list[list[Item]]` 124,626 to 50,228 (−60%), `dict[str, Item]` 98,995 to 38,721 (−61%). |
| JSON insertion & field order | | | | | Native JSON encoder preserves `dict` insertion order and Pydantic model field declaration order (`Encoder::dict`, `ModelPlan` / `Encoder::model_fields`, and normalized `encode_json_limited`), writing entries in one pass without sorting `self.entries`; `Parser::object` also keeps document member order without sorting. Instructions per request at one connection (`BESSEMER_EVENT_LOOP=auto`, 3,000 requests, private `vgdb` prefix) from baseline `9b1f1c1`: `/json` 33,036 to 32,913 (−123, −0.4%), `/items/7?q=abc` 36,111 to 36,011 (−100, −0.3%), `POST /echo` (20-item `order.json`) 202,149 to 200,212 (−1,937, −1.0%). |

### 8 November 2026: synchronous completion handoff

The sync pool posts results directly to a loop-owned future whose cancellation
waits for actual completion, removing the concurrent-to-asyncio future chain and
shield wrapper. Sync handlers, dependencies and background tasks retain their
thread pool, copied ContextVars and cancellation/capacity lifetime.

| Matrix route (auto loop, one connection) | Before instructions/request | After | Change |
| --- | ---: | ---: | ---: |
| `/sync` | 211,426 | 157,784 | -25.37% |
| `/bg` | 275,278 | 220,809 | -19.79% |
| `/json` | 33,101 | 33,154 | +0.16% |

See the [measurement report](../benchmarks/results/2026-11-08/sync-completion/report.md)
for installed-wheel provenance, isolated Callgrind commands, intermediate
measurement and strace limitations. Post-send BackgroundTasks timing remains
open: buffered tasks still finish before the response is sent. This is a
user-space orchestration saving, not removal of the required thread hop or
formal performance acceptance.

### Native synchronous routes (8 November 2026)

Plain sync routes now share the native invoker's input binding, request/context
setup and result encoding. A native work item enters a copied Context on the
existing bounded executor and posts completion to the owning loop. Its internal
queue protocol avoids an unused concurrent Future; queued shutdown cancellation
still delivers completion. Non-native inputs/middleware and unusual results keep
the Python fallback. The private executor protocol covers CPython 3.12-3.15
(run with an optional worker context); this host exercises 3.13 only.

| Route | Before instructions/request | Native sync path | Change |
| --- | ---: | ---: | ---: |
| Matrix `/sync` | 158,515 | 99,426 | -37.28% |
| Matrix `/bg` | 221,948 | 222,010 | +0.03% |
| Matrix `/json` | 33,144 | 33,135 | -0.03% |
| Compare `/items/7?q=abc` | 36,164 | 36,053 | -0.31% |
| Compare POST `/echo` (20-item order) | 203,862 | 201,893 | -0.97% |

Installed CPython 3.13.15 release wheels, auto loop, one connection, 3,000
requests after 2,000 warmups, isolated Callgrind controls. Baseline includes the
closed-loop completion guard. A Python lean-pool wrapper measured 134,909 on
`/sync`; a native job using ordinary executor.submit measured 125,312. These
intermediates were superseded by the future-free executor work item. Background
timing is not changed in this step. The retained sync result is below 100k but
does not meet an interpretation of "well under" 100k or match the async cost.

### Post-send background work (8 November 2026)

Response completion now starts background tasks after delivery while a transport
guard holds admission through their completion and dependency cleanup. This
closes the previously documented timing gap for buffered/streamed responses,
HEAD, middleware responses and HTTP/2. Abandonment skips new tasks and unwinds
resources; task failures are logged without replacing an already-sent response.

Final installed-wheel instructions/request, against the pre-native-sync
baseline: `/sync` 158,515 -> 99,619 (-37.15%), `/bg` 221,948 -> 240,515
(+8.37%), `/json` 33,144 -> 33,189 (+0.14%), `/items/7?q=abc`
36,164 -> 36,124 (-0.11%), POST `/echo` 203,862 -> 203,828 (-0.02%).
The background CPU increase buys the required post-response lifetime accounting;
it is not reported as a performance improvement. Details, intermediate versions
and validation are in the [follow-up report](../benchmarks/results/2026-11-08/sync-completion/followup.md).

### 8 November 2026: dependencies, Request, exceptions and HTTP middleware

Yield-dependency cleanup runs inline in the request's task under the
invocation's deferred cancellation (not under middleware), dependencies resolve
without per-argument coroutines or per-call wrappers, `HTTPException` bodies are
encoded natively, the middleware `call_next` chain and raised errors leave no
reference cycles, and `Request`, its headers, query and URL views are built
without ABC checks or exception round trips. Details and the remaining gaps are
in the production-readiness record of the same date.

| Matrix route (auto loop, one connection) | main | This branch | Change |
| --- | ---: | ---: | ---: |
| `/json` | 33,189 | 33,162 | −0.1% |
| `/dep` | 109,047 | 97,157 | −10.9% |
| `/yield_dep` | 166,017 | 102,126 | −38.5% |
| `/request?q=1` | 147,643 | 127,717 | −13.5% |
| `/raise` | 93,853 | 55,167 | −41.2% |
| `/json` with one HTTP middleware | 150,489 | 114,852 | −23.7% |
| `/dep` with one HTTP middleware | 212,965 | 163,651 | −23.2% |
| `/yield_dep` with one HTTP middleware | 268,545 | 216,873 | −19.2% |

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
#### Second pass (lazy native request, flat dependency plans)

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

### 8 November 2026: HTTP middleware replies and streaming responses

Middleware JSON replies with extra headers use the HTTP/1 settled-JSON writer;
streams produce chunks with fewer calls, start their producer eagerly, batch a
sync iterator's first steps and go out in one write when ready. Details in the
production-readiness record of the same date.

| Route (auto loop, one connection) | Before | After | Change |
| --- | ---: | ---: | ---: |
| `/json` with one HTTP middleware | 92,282 | 91,095 | −1.3% |
| `/dep` with one HTTP middleware | 126,725 | 125,361 | −1.1% |
| `StreamingResponse`, async generator, 10 chunks | 241,021 | 175,880 | −27.0% |
| `StreamingResponse`, sync generator, 10 chunks | 486,714 | 328,985 | −32.4% |
| `EventSourceResponse`, 10 events | 725,102 | 428,820 | −40.9% |

### Adaptive admission (8 November 2026)

The default is `max_in_flight="auto"`, with delay-based shedding and a hard
4,096-request worker ceiling. Explicit integers preserve fixed admission.
Queue delay must stay above a timeout-scaled target over twenty target periods;
short bursts and healthy suspended requests do not alone trigger shedding.
Permits and byte budgets retain their existing lifetimes.

Private-prefix Callgrind, installed CPython 3.13 wheel, auto loop, c1,
2,000 warmups / 3,000 measured requests:

| Route | second merge baseline | latest main + auto admission |
| --- | ---: | ---: |
| `/json` | 33,022 | 33,130 (+0.33%) |
| `/sync` | 99,160 | 99,575 |
| `/bg` | 225,617 | 230,215 |

These include the later main parity merge (`a191932`). On the same candidate,
explicit fixed admission measured `/bg` at 229,678, so most of the `/bg`
difference is unrelated to the adaptive decision. Isolated throughput
qualification remains separate from shared-machine concurrency smoke results.

### Suspended-request completion (8 November 2026)

Timer-turn batching was reverted in `26f2c66`: no throughput improvement was
established to justify its extra epoll calls. After merging current main, use
the request's existing Call object as its real asyncio Task's completion
callback, avoiding a bound method and a redundant task-table lookup.
Private-prefix Callgrind c1 (2,000 warmups / 3,000 measured), installed wheel:
`/io` 61,192 → 60,421 (−1.26%); `/json` 33,257 → 33,292 (+0.11%). Real Task
identity, the lane tracking tables, cancellation and context semantics remain.

### 8 November 2026: stream setup, WebSockets and multipart uploads

| Route (auto loop, one connection) | Before | After | Change |
| --- | ---: | ---: | ---: |
| `StreamingResponse`, async generator, 10 chunks | 175,880 | 156,069 | −11.3% |
| `StreamingResponse`, sync generator, 10 chunks | 328,985 | 311,154 | −5.4% |
| `EventSourceResponse`, 10 events | 428,820 | 406,913 | −5.1% |
| WebSocket echo per message (text; FastAPI 85,148) | 32,106 | 27,683 | −13.8% |
| WebSocket connection (FastAPI 875,279) | 426,869 | 261,628 | −38.7% |
| multipart upload, 5 MiB | 8,004,905 | 6,281,816 | −21.5% |

### 8 November 2026: connection-state costs at high concurrency

`a839721` separates TLS state from plaintext connection tasks (13.1 KB →
7.1 KB); `151066a` separately pins connection futures to avoid task-output
state copies. Compare `/io`, installed CPython 3.13 release wheels, native
loop, fixed 4,096 admission, private vgdb prefix. No timer/epoll or Task/Future
semantic changes. All measured responses 200.

| Connections | Before instructions/request | After | Change |
| --- | ---: | ---: | ---: |
| 1 | 60,289 | 60,374 | +0.1% |
| 64 | 47,259 | 46,604 | -1.4% |
| 256 | 49,387 | 46,793 | -5.3% |
| 1024 | 57,500 | 48,232 | -16.1% |

The 12,000-request samples include fresh connection setup. At 60,000 requests,
c256 improves 47,243 → 46,799 (-0.9%) and c1024 improves 49,778 → 47,659
(-4.3%). Timer heap pop grows only 191 → 200 instructions/request and socket
readiness stays 444; connection copying grows 2,272 → 3,602 in the baseline.
Adaptive admission sheds under Callgrind, so its non-200-inclusive counts
cannot establish successful-request cost. This is not throughput acceptance;
see `benchmarks/results/2026-11-08/concurrency/report.md` for profiles,
rejected experiments, validation and remaining gaps.

After merging current main `78a8aef` in `dd7fb52` (which independently contains the
same connection-pinning change), direct current-main → merged counts are
c1 60,243 → 60,273; c64 46,939 → 46,903; c256 47,350 → 47,150;
c1024 50,024 → 49,222. The incremental TLS-state saving is 0.42% / 1.60%
at c256/c1024, plus 6 MiB less RSS with 1,024 open plaintext connections.
The earlier larger table includes pinning already landed in main. Final merged
installed-wheel full check passes 240 Rust, 991 Python (34 skipped), 23 harness
tests and all static checks; isolated throughput/p99 remains unqualified.

### 8 November 2026: HTTP/1 connection close

Native JSON replies on closing HTTP/1.1 connections use the direct writer.
Private-prefix Callgrind, compare `/json`, native loop, c1, 6,000 responses:

| Connection policy | Before instructions/request | After | Change |
| --- | ---: | ---: | ---: |
| One request per connection | 48,059 | 45,612 | -5.1% |
| Keep-alive control | 33,329 | 33,325 | unchanged |

Connection setup/close adds about 12,287 instructions above the keep-alive
path after the change. Kernel accept/close cost is outside Callgrind counts.
Wire-parity regression passed; full installed-wheel check follows this round.

### 8 November 2026: HTTP/2 stream task copies

Pin stream futures before submitting to Tokio. At 64 streams per connection,
`memcpy` falls from 6,889 to 4,659 instructions per request. Hyper's HPACK
header encoding still consumes about 8,400 instructions/stream inclusive.
Installed release wheel, `/json`, one connection, 6,000 successful responses:

| Protocol / concurrent streams | Before instructions/stream | After | Change |
| --- | ---: | ---: | ---: |
| h2c / 1 | 69,025 | 66,853 | -3.1% |
| h2c / 64 | 56,483 | 54,453 | -3.6% |
| TLS h2 / 64 | 59,291 | 57,269 | -3.4% |

TLS measurements verify the peer certificate; an earlier profile whose
certificate was rejected by oha was discarded. No protocol, stream-limit,
flow-control, ticket or cipher settings changed. Throughput remains for
isolated qualification; Callgrind latency/req/s is not meaningful.

TLS connection profiles use verified TLS 1.3, the same RSA-2048 key, 300
new connections, with explicit client assertions of 299 resumptions in the
resumed workload. Full connection + `/json` costs 11,164,534 → 11,161,627;
resumed 1,306,369 → 1,303,584 instructions. The small difference comes from
the closing writer; crypto dominates. Stateful tickets already work with
rustls's 256-entry cache and two tickets; no security settings changed.
At 128 concurrent h2c streams the stream pinning saving is 56,523 → 54,390
(-3.8%). Installed-wheel full check: 241 Rust (2 ignored), 992 Python
(34 skipped), 23 harness tests and static checks pass. Details, reproduction,
profile exclusions and remaining throughput qualification are in
`benchmarks/results/2026-11-08/transport/report.md`.

### 8 November 2026: native sync queue and bridge completions

Replace Python executor work items with Rust-owned worker threads sharing
one bounded-count queue. A thread retains its Python thread state across jobs
and parks detached from the interpreter. Lean results encode on that thread;
loop completions use the bridge's batched native wake. Matrix c1, 6,000 requests:
/sync 98,602 → 68,437 instructions/request (-30.6%); /bg 229,877 → 187,110
(-18.6%). Generic loop executor calls retain concurrent Future semantics;
internal calls avoid them. Further completion work and final controls follow.

Ordinary lean sync requests now avoid the request Task and asyncio Future:
the pool calls the bound handler in a copied Context, encodes eligible results,
and the loop settles the response directly in the bridge drain. Cancellation
still owns admission until the queue/running call completes; shutdown creates
waiters only for outstanding calls. Python response/error/awaitable fallback
and custom task factories retain Tasks. c1 /sync falls further from 68,437 to
42,922 instructions/request, versus baseline 98,602 (-56.5%). Full check and
concurrent/idle-spin measurements follow this checkpoint.

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

### 8 November 2026: PR #110 delivery fixes and regression budget

File delivery retains admission and in-flight accounting, ASGI files share
conditional/range selection with cancellation-safe reads, and HTTP/2 empty
responses defer background startup until headers flush. To offset ordinary
request costs, adaptive admission compares precomputed deadlines, optional
reply header maps are boxed, and rare invocation resources share one allocation.

### Instructions per request

Callgrind, one connection, 2000 requests; fails above +1%.

| Loop | Route | Base | Head | Change | |
| --- | --- | ---: | ---: | ---: | --- |
| asyncio | `/json` | 47,800 | 47,894 | +0.20% | ok |
| asyncio | `/items/7?q=abc` | 50,853 | 51,057 | +0.40% | ok |
| asyncio | `/echo` | 218,765 | 212,826 | -2.71% | faster |
| bessemer | `/json` | 32,027 | 32,141 | +0.36% | ok |
| bessemer | `/items/7?q=abc` | 35,032 | 35,225 | +0.55% | ok |
| bessemer | `/echo` | 202,588 | 197,655 | -2.43% | faster |


Baseline `362f71a`; head includes main `1558cdd`. CI driver, private vgdb
prefixes, CPython 3.13.15, 500 warmup/2,000 measured requests, c1; all six
comparisons pass the stricter 1% threshold. Installed-wheel full check passes
245 Rust tests (2 ignored), 1,035 Python tests (34 skipped), 34 harness tests
and static checks. No throughput claim; M0 remains open. Details:
`benchmarks/results/2026-11-08/review-delivery/report.md`.
