# Native synchronous handler pool, 8 November 2026

Baseline: local main `ca33f4e`, merged before implementation. CPython 3.13.15
installed release wheels, matrix `common:app`, native loop, one worker, fixed
4,096 admission to avoid instrumentation-induced shedding. Private vgdb prefixes
and PID-scoped controls throughout; all reported requests return 200. No main
checkout or other agent worktree/venv was modified.

## Implemented

- `b9e13fb`: Rust-owned worker threads and a shared queue replace Python
  ThreadPoolExecutor work items. One configured `sync_workers` count serves
  all lanes, handlers, synchronous dependencies/background work and default
  asyncio executor calls. Each worker retains its Python thread state and
  detaches while parked. No queue lock is held while executing Python or
  joining threads. Internal work avoids concurrent Future allocation; generic
  executor callers retain their concurrent Future contract.
- Lean sync handlers run with copied Context, bound native arguments and
  `current_request`. Eligible results encode natively on the pool thread.
  Completed batches use the native bridge's reader wake, replacing per-result
  `call_soon_threadsafe` handles.
- `0f0984f`: ordinary lean sync requests also avoid the request Task and
  asyncio Future. The loop settles the encoded response directly from the
  completion batch. Python responses, returned awaitables and exceptions
  create a Task in the original request context and use the existing response
  and exception boundaries. Custom task factories keep their Task-based path.
- Final parity/cleanup fixes preserve explicit `CancelledError` behavior,
  release admission if scheduling a fallback Task fails, and protect native
  shutdown waiters from repeated cancellation. Only shutdown allocates these
  waiters. Registered native invocations remain cancellable before start and
  keep admission until queued/running calls actually finish. The bridge reader
  stays registered during shutdown cleanup, then detaches.
- Native thread join handles survive executor garbage collection and are
  joined before interpreter teardown. A worker cannot join itself. Queued
  generic Futures cancel synchronously outside the queue lock; capacity-owning
  internal calls remain queued until their cancellation check executes.

The public API, sync worker count policy, post-send BackgroundTasks guards,
response byte budgets and HTTP behavior are unchanged. There is no GC free-list
workaround or timer/epoll batching change. Native health stays Tier 0; Python
application routes remain Tier 2.

## Instruction counts

c1 samples use 6,000 requests, following the harness's 2,000-request warmup.
The intermediate stages show where the saving comes from:

| Route | Baseline | Native queue + bridge | Direct completion |
| --- | ---: | ---: | ---: |
| `/sync` | 98,602 | 68,437 | 42,922 |
| `/bg` | 229,877 | 187,110 | 187,867 |
| `/json` | 33,336 | 33,324 | 33,504 |

Final rebuilt-wheel c1 counts, including shutdown/exception fixes:

| Route | Baseline | Final | Change |
| --- | ---: | ---: | ---: |
| `/sync` | 98,602 | 43,000 | -56.4% |
| `/bg` | 229,877 | 187,724 | -18.3% |
| `/json` | 33,336 | 33,649 | +0.94% |

The small async control cost increase is roughly 0.9%, with larger
invocation/control state and completion bookkeeping; the native pool does no
work on `/json`.
The matrix `/bg` handler is async with a sync background task, and retains the
full Python request/deferred-response path; it benefits from the new pool but
does not qualify for the direct lean sync completion path.

c64 samples use 12,000 requests, one worker and the same fixed admission:

| Route | Baseline instructions/request | Native pool/direct | Change |
| --- | ---: | ---: | ---: |
| `/sync` | 73,094 | 32,169 | -56.0% |
| `/bg` | 207,527 | 165,037 | -20.5% |
| `/json` | 27,916 | 28,017 | +0.36% |

These concurrency counts precede the final exception/shutdown edge-case fixes;
the ordinary fast path is unchanged. All are installed-wheel worker user-space
instructions, not kernel time, latency or throughput measurements. Thread/GIL
scheduling still has a cost that instruction counts do not capture.

## Rejected spin experiment

A private constructor argument enabled 64 brief spin/yield-to-queue checks
before a worker parked on its condition variable. The worker was detached
from Python while spinning. Same executable, default parking vs spin:

| `/sync` | Parking | Spin 64 | Change |
| --- | ---: | ---: | ---: |
| c1 | 42,922 | 44,952 | +4.7% |
| c64 | 32,169 | 32,493 | +1.0% |

The c1 parking sample has 6,000 requests; the spin sample 12,000, so connection
setup is slightly more amortized in the spin sample. Both c64 samples have
12,000 requests. Spin increased instructions without demonstrated isolated
throughput benefit. The argument and spin code are removed; the final pool
parks without spinning. No benchmark-only production setting is retained.

## Reproduction / artifacts

```sh
PYTHON=/home/cmux/venvs/codex/bin/python OHA=$HOME/.cargo/bin/oha \
  BESSEMER_EVENT_LOOP=auto PROFILE_MAX_IN_FLIGHT=4096 PORT=18101 \
  CONNECTIONS=1 OUT=/tmp/my-private-sync-profile \
  benchmarks/profile/instructions_matrix.sh /sync 6000
```

Repeat with `/bg`, `/json`; use `CONNECTIONS=64` and 12,000 requests for the
concurrency check. The private immutable baseline wheel was unpacked into
`/tmp/codex-native-sync-baselinepkg`; baseline profiles set PYTHONPATH to it.
Raw ignored artifacts: `raw/before-*`, `pool-*`, `direct-*`, `park-c64-*`,
`spin64-*`, `final-*` and `qualified-*`. Warmup and measured worker counts are
separated with PID-scoped Callgrind zero/dump. Client status distributions were
inspected explicitly, including every concurrency/spin sample.

## Validation and remaining qualification

New regressions cover no Python Future on ordinary native sync calls, restored
loop Context/current_request for returned awaitables and exceptions, queued
cancellation retaining admission, explicit CancelledError bypassing application
handlers, shared executor capacity/context isolation, self-join protection and
repeated shutdown cancellation. Existing post-send background, native timeout,
stream cleanup and multi-loop tests remain in the full check.

The first full runs found three tests inspecting ThreadPoolExecutor's private
Python thread set. Their ContextVar/current_request values and weak-reference
collection checks passed. The live-thread assertion now checks native worker
liveness when the native pool is present; the original Python fallback assertion
is retained. All three focused context tests pass with idle native threads alive.

All measurements/tests here use CPython 3.13.15. Free-threaded ownership uses
PyO3 attach/detach, owned Py handles, Rust queue locks and the existing Guarded
invocation state; no unsafe GC or GIL assumptions were added. Actual 3.14t
runtime qualification is not claimed from this environment.

An isolated one-core /sync, /bg and /json throughput run through `0f0984f` was
requested from the orchestrator. No shared-host req/s or Callgrind latency is
used as performance acceptance. The formal M0 gate remains open.

A full run exposed a file-response test assuming background unlink completed
when the client received the response. It now waits boundedly for cleanup,
preserving the required post-send ordering rather than moving work before send.

Final rebuilt/installed-wheel `BESSEMER_REQUIRE_NATIVE=1 ./scripts/check.sh`
passes: 241 Rust tests (2 ignored), 999 Python tests (34 skipped), 23 benchmark
harness tests, formatting/lint/source checks. This includes the post-send
cleanup assertion correction; the final full rerun exits 0.
