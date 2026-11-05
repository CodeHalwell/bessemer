# Free-threaded CPython (exploratory)

The same source tree was built for CPython 3.13.18 (GIL) and the free-threaded
CPython 3.14.6 build, both installed from release wheels. On a 4-vCPU VM, one
worker (`--workers 1`, default eight sync handler threads) was pinned to CPUs
0-2 with `taskset`, and `oha` (32 connections, 3-second warm-up, three 8-second
runs) to CPU 3. `run.sh` and `ftapp.py` are the exact script and application.
Every response was HTTP 200.

| Route | CPython 3.13 (GIL) | CPython 3.14t (free-threaded) |
| --- | ---: | ---: |
| `/cpu` (sync, 20,000-step Python loop) | 1,383 to 1,426/s | 4,455 to 4,552/s |
| `/json` (async, no work) | 127.6k to 135.3k/s | 131.0k to 136.3k/s |

What this shows:

- Without the GIL, CPU-bound sync handlers run in parallel on the worker's
  handler threads: 3.2 times the GIL build's throughput on three cores, in
  one process.
- Async handlers share one event loop per worker, so `/json` is unchanged.
  Spreading async work across cores in one process needs several event loops
  per worker, which is not implemented yet.

These are single-host, short runs: diagnostic evidence only, not a formal
scaling result.
