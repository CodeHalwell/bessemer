# bessemer-loop micro-benchmarks

Python-level benchmarks that compare the Rust event loop (`bessemer._loop`) with
stock `asyncio` and `uvloop` on the same workloads. Each script measures one
area of the loop; `run_all.py` runs them all and prints a comparison table.

| Script | Cases |
|---|---|
| `bench_call_soon.py` | self-rescheduling callback chain, 100 concurrent chains, 1000-callback batches, `call_soon_threadsafe` chain |
| `bench_timers.py` | `call_later` with mixed 0..200 µs delays and half cancelled, zero-delay timers, far-future `call_at` + cancel (heap churn), self re-arming timer |
| `bench_futures.py` | `create_future` + `set_result` + `await` (already done, and resolved by `call_soon`), `add_done_callback` |
| `bench_tasks.py` | `create_task` + `gather` in batches of 1000, `create_task` + `await` one at a time, two-task ping-pong over futures (task switches/s), 10 tasks yielding with `sleep(0)` |
| `bench_sockets.py` | 64 B and 16 KiB echo round trips: `sock_recv`/`sock_sendall` over a `socketpair`, streams (`start_server`/`open_connection`) and a `Protocol` over loopback TCP |
| `bench_executor.py` | `run_in_executor(None, f)` with one call in flight and with 32 in flight |

## Running

Python 3.12+ is required; `uvloop` is optional, and `bessemer` is used once
`import bessemer._loop` works (the loop is part of the `bessemer` wheel).

```sh
cd crates/bessemer-loop/bench

# Every loop that imports, all benchmarks (about a minute per loop).
python3 run_all.py

# Pin to one CPU and keep the results.
python3 run_all.py --cpu 2 --json results.json --markdown results.md

# A subset.
python3 run_all.py --loop asyncio --loop bessemer --bench sockets --bench timers

# CI smoke test: tiny op counts, one run each, a few seconds in total.
# Exits non-zero if any benchmark fails on any loop.
python3 run_all.py --quick

# A single script, with a readable report.
python3 bench_tasks.py --loop uvloop
python3 bench_tasks.py --loop bessemer --case ping_pong --repeat 10
```

`run_all.py` options: `--loop` and `--bench` (repeatable), `--repeat N` (timed
runs per case, default 5), `--scale X` (multiply every op count), `--quick`,
`--cpu N`, `--json PATH`, `--markdown PATH`, `--timeout S` (per subprocess)
and `--python EXE`. A loop named with `--loop` that cannot be imported is an
error; without `--loop`, unavailable loops are skipped with a note.

Each script accepts `--loop {asyncio,uvloop,bessemer}`, `--repeat`, `--scale`,
`--quick`, `--cpu`, `--case NAME` (repeatable) and `--json`.

## Method

- Every (benchmark, loop) pair runs in a fresh subprocess; loops are
  interleaved per benchmark so drift affects them alike.
- Each case gets a new loop from the loop factory, one warm-up run at a fifth of
  the op count, then `--repeat` timed runs via `loop.run_until_complete`.
  `gc.collect()` runs before each timed run; the collector stays enabled.
- Wall time comes from `time.perf_counter`, CPU time from `time.process_time`
  (the whole process, so executor worker threads count).
- Reported: median ops/s, ns/op (wall and CPU), and spread, which is the
  min..max range of per-run ops/s as a percentage of the median. Treat a
  difference smaller than the spread as noise.
- Relative speed is `loop ops/s ÷ asyncio ops/s` (higher is better); the
  geometric mean across all cases is shown at the bottom of the table.
- Op counts are fixed per case, so every loop does identical work, and sized
  so that stock asyncio takes roughly 0.1 to 0.4 s per run.
- With `--cpu`, every process is pinned with `os.sched_setaffinity`, and so are
  the executor's worker threads. This favours repeatable numbers over executor
  parallelism.
- The socket benchmarks run client and server in the same loop and process,
  so a round trip costs two receives and two sends on the loop.

## Adding a case

Write `async def workload(n) -> int` that does `n` operations on the running
loop and returns how many it did, then add a `Case(name, ops, workload, unit)`
to the script's `CASES` list. A new script must also be added to `BENCHMARKS`
in `run_all.py`.
