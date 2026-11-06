# Concurrency and efficiency pass — 6 November 2026

Local macOS arm64 CPython 3.13.18 comparison against the immutable published
1.0.0rc1 wheel. Both environments use Pydantic 2.14.3, FastAPI 0.162.0 and
Uvicorn 0.54.0. The host is an Apple M5 Max with 64 GiB RAM. Source changes are
local on `perf/concurrency-2026-11-06`; the candidate retains the rc1 package
version and is distinguished by its native-library SHA-256 in every data file.
Candidate native SHA-256:
`29612ea6f2fd5eeaa1a0e29ba657575b6e5142783aaec193f82c3800d56d1a7e`.
Candidate wheel SHA-256:
`d05c83d3122c9e5d3ab7dc2f9d05e7883dc053f567f73bf60b737c50e36ff457`.

## Implemented

- High-resolution embedded Rust-loop timers, with a coarse wait until the last
  10 ms to avoid native arm/disarm syscalls on every busy I/O iteration.
- Precompiled escaped model output keys and byte-budget checks before copies.
- Allocation-free container traversal for validation-only JSON scans.
- Parsed request-body reuse when the native invoker falls back to Python.
- Budget-owned immutable Python response bytes without a transport copy.
- Same-loop WebSocket callback delivery without thread-safe rescheduling.
- An optional official-SDK AG-UI example: standard SSE plus application-framed
  duplex WebSockets, bounded live input, cancellation and cleanup before terminal
  output. Browser acceptance covered live input, repeated runs and cancellation.

## Evidence

- [Broad results](summary.md): three rounds, 13 capabilities, five variants,
  64 HTTP connections and 32 duplex WebSocket sessions. All 195 measured samples
  completed: 6,992,391 responses/messages, zero reported errors and zero
  unexpected HTTP statuses. The invalid-body workload expects 422. Fourteen
  samples were flagged as client-limited.
- [Broad raw data](raw.json): per-sample throughput, p50/p99, CPU cost,
  load-generator output, installed versions and native hashes.
- [Paired follow-up](paired-rust/raw.json): four alternating rounds of rc1 and
  candidate Rust-loop JSON/dependency routes, after the broad run showed mixed
  results. Candidate medians were 50,524 versus 40,388 requests/s for JSON and
  22,174 versus 18,467 for dependencies. CPU costs were 18.1 versus 22.6 µs and
  41.6 versus 46.8 µs respectively. The broad run's dependency slowdown did not
  reproduce in this follow-up.
- [Timer-tuning A/B](timer-tuning/raw.json): the final native-timer window
  recovered JSON throughput while improving the 1 ms I/O workload.
- [Ramped capacity check](capacity/raw.json): all three variants established
  1,024 persistent connections with 16 setup requests at a time. Candidate
  default completed 80,653 I/O/fan-out requests, candidate Rust loop 64,130,
  and FastAPI 22,800, with zero setup/request errors. Each phase ran for two
  seconds; this is a capacity check with a Python client, not a server-throughput
  qualification.
- [Burst setup attempts](high-concurrency/): `oha -c 1024` reset connections on
  the published Rust loop, both candidate modes and FastAPI. The harness rejected
  those runs, including warmup errors; they are not successful performance
  samples. The gradual-start check above separates this observed connection-burst
  limit from sustained application concurrency.
- `timers-before.json` / `timers-after.json`: isolated served sleep measurements.
- `model-before.json` / `model-after.json`: native model encode/parse/control
  timings across 1, 20 and 100 items, with plain and escaped field aliases.
- [Isolated timing tables](isolated.md): final timer and model measurements,
  including the unchanged control's variation. A 1 ms Rust-loop sleep fell
  from 2.288 ms to 1.048 ms; a 20 ms sleep from 22.066 ms to 20.097 ms.
- [Live-input browser acceptance](ag-ui-live-input.png) and
  [cancellation acceptance](ag-ui-cancel.png).

`pre-timer-tuning/` is the interrupted diagnostic pass that exposed excessive
timer syscalls; it is not the final candidate comparison. Its files are retained
to distinguish that earlier native build. The broader matrix has mixed gains
and slowdowns, and the paired follow-up changed some conclusions. These short,
unpinned same-host measurements do not establish uniform speedups or Linux
production/scaling acceptance. CPU use differs between Bessemer's runtime modes;
use CPU per response alongside throughput. WebSocket results can be limited by
the Python load generator.

## Reproduce

The baseline interpreter is
`~/Library/Caches/bessemer-bench/pypi-1.0.0rc1/venv/bin/python`.
The candidate interpreter and wheel are under
`~/Library/Caches/bessemer-bench/concurrency-2026-11-06/`.
The `oha` 1.16.0 binary used was `/tmp/bessemer-bench-tools/oha`.
Servers used one worker; Bessemer admission/connection limits were raised to
16,384, including the ramped capacity check. “Default” identifies the asyncio
event loop rather than the stock admission configuration.
See [runner instructions](../../../concurrency/README.md) and the `settings`
object in each raw file for exact arguments. Run timers and model benchmarks
sequentially with each interpreter while builds/tests are idle.

## Validation

`validation.log` records the installed-wheel check: 179 Rust passes (one ignored),
748 Python passes (14 skips), formatting, warnings-denied workspace Clippy and
Ruff. That invocation then failed three Linux-only harness tests on macOS.
Platform guards were added without changing the Linux runner; the focused rerun
in `validation-harness.log` passed 18 tests with three skips. `complexity.log`
records complexipy success at maximum complexity 15 for the new/changed Python
functions. Installed Python package files were byte-identical to the checkout.
The loop crate passed Linux x86_64 all-target compilation, not Linux execution.
The 17 AG-UI tests include native SSE/WebSocket lifecycle and exact-origin
allow/deny cases on both macOS loop modes.

## Targets identified during this pass

Later [multicore](../multicore-pass/report.md) and
[efficiency](../efficiency-pass/report.md) work implements stateless WebSocket
lane distribution, coalesced upload writes and bounded synchronous stream
batches. The list below records the original profiling targets.

- WebSocket sessions stay on the lifespan loop when HTTP uses several lanes
  (`src/lib.rs::native_lanes`, `Worker.submit`, `_invoke_websocket`). Session
  sharding needs lane-local resource/cleanup design before enabling parallel
  free-threaded WebSocket handlers.
- Upload spooling uses one blocking write per incoming frame
  (`src/uploads.rs::spool`). Bounded coalescing and reusable buffers could reduce
  blocking-pool hops while retaining upload/cancellation budget ownership.
- Sync streaming offloads each iterator step. Blind batching would delay an
  available chunk behind a blocking next step; retain that latency boundary.
- Ordinary asyncio Task allocation, completion bookkeeping and wide-model field
  lookup remain profiling targets. This pass retains Task/context/cancellation
  compatibility and makes no new Tier 1 claim.
- Native JSON results still copy the codec scratch buffer into an owned Vec.
  Large results could transfer ownership above the scratch-retention limit;
  small results currently reuse the scratch allocation.
