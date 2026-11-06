# Bessemer vs Axum reprofile, 2026-11-06

CPU time per request, latency and profiles of `main` (7475503) against the Axum comparison binary.
Everything here was measured on this tree; no number is estimated or extrapolated. Nothing else ran on the host during the
measurements and no two benchmarks ever overlapped.

## Environment

| | |
|---|---|
| git | `7475503386b11ca2a041931c668b6f4b8657e364` (main), branch `claude/project-thread-8mp3j6`, working tree clean apart from this directory |
| Bessemer | wheel `bessemer 1.0.0rc1` installed in `/home/user/venv` (the release wheel of the current tree, as provided; not rebuilt here) |
| Python | CPython 3.13.18 (`/usr/local/bin/python3.13`), pydantic 2.14.3 |
| kernel | Linux 6.18.44-fc-v242, 4 vCPU Intel Xeon @ 2.10 GHz (shared VM), clocksource tsc, TCP congestion control bbr |
| tools | oha 1.16.0, perf 6.8.1 (software events only), valgrind 3.22.0, py-spy 0.4.3 |
| Axum | `benchmarks/compare/axum_app/target/release/axum-compare PORT THREADS` (built 16:02), tokio multi-thread runtime |
| settings | defaults, except `BESSEMER_EVENT_LOOP` / `BESSEMER_IO_BACKEND` as listed. Default `max_in_flight` = 128, `max_connections` = 1024 |
| order body | `scripts/order.json`: an Order with 3 items (id, name, price, tags), 185 bytes |

## Method

`scripts/profile_route.sh` is a copy of `benchmarks/profile/profile_route.sh` (original untouched) with: `METHOD` / `BODY_FILE` /
`CONTENT_TYPE` (same convention as `instructions.sh`); oha p50/p99/p99.9 and status codes from oha's JSON; `CLIENT_CPU` / `SERVER_CPU`
as CPU lists; `WORKERS` / `AXUM_THREADS`; oha's own CPU time (`perf stat` on oha) to detect a client-limited run; per-thread CPU
from `/proc/<pid>/task/*/stat` taken before and after the same 10 s window; optional `PERF_RECORD=1`, which runs `perf record` in a
*separate* load phase after the measured window (so the numbers are never perturbed by recording).

Per run: server started pinned with `taskset`, 3 s warm-up, then one 10 s window. µs CPU per request = server `task-clock`
(`perf stat -p` over the bessemer worker process(es), or the axum process, 10 s) / (oha req/s * 10 s). Context switches come from the same `perf stat`.
Every run starts a fresh server. Every cell was run twice, in two rounds (round 1 over all cells, then round 2), and the tables show the
median of the two (= their mean). The raw per-run values are in `raw/*-tables.md` and `raw/*-all-runs.csv`.

* Server pinned to CPU 0. oha on CPU 3 for 1 and 64 connections (as the script does), and on CPUs 1-3 (`taskset -c 1-3`) for the
  1024-connection runs so the client is not the limit there.
* A cell is marked **CLIENT-LIMITED** when oha used >= 0.95 of its cores (oha task-clock / its runtime / cores).
  With oha on one core this happens at 64 connections for axum (all routes) and for Bessemer's Rust loop and io_uring on `/json`.
* The matrix runs did **not** use `PYTHONPERFSUPPORT`; the profile runs (step 3) did.
* oha always reports a few hundred "aborted due to deadline" errors when its 10 s timer ends with requests in flight. That is not a server
  error and is not counted as a failure. Non-200 responses are (see 503 note).

### Commands

```
export PATH=$PATH:/usr/local/bin:~/.cargo/bin
cd /home/user/bessemer/benchmarks/results/2026-11-06/reprofile
python3 scripts/run_matrix.py                       # step 2, raw/matrix/<server>/<route>/c<N>/run<R>/ (72 runs)
SUBDIR=supp-oha-cpus1-3-at64 CLIENT64=1-3 ONLY_CONNS=64 python3 scripts/run_matrix.py          # supplement: 64 conn, oha on CPUs 1-3
SUBDIR=supp-asyncio-max-in-flight-4096 ONLY_SERVERS=bessemer-asyncio ONLY_CONNS=1024 \
  SERVER_EXTRA="BESSEMER_MAX_IN_FLIGHT=4096" python3 scripts/run_matrix.py                     # supplement: no 503 shedding
bash scripts/run_perf.sh                            # step 3: PYTHONPERFSUPPORT=1, PERF_RECORD=1, 64 conn, oha CPUs 1-3, perf.data kept
bash scripts/run_pyspy.sh                           # step 4: py-spy record --native --rate 2000 -d 8 -f raw -p <worker>
bash scripts/run_pyspy_nonnative.sh                 # step 4 supplement: same without --native
bash scripts/run_callgrind.sh                       # step 5: benchmarks/profile/instructions.sh, CONNECTIONS=1, 5000 requests
bash scripts/run_multicore.sh                       # step 6: --workers 2 / axum 2 threads on CPUs 0-1, oha CPUs 2-3, /json, 1024 conn
```

Inside the matrix, each cell is (servers: `bessemer-asyncio` = `BESSEMER_EVENT_LOOP=asyncio`, `bessemer-rust` = `BESSEMER_EVENT_LOOP=bessemer`,
`bessemer-rust-iouring` = `BESSEMER_EVENT_LOOP=bessemer BESSEMER_IO_BACKEND=io_uring`, `axum`; io_uring was accepted: every run's `server.log` says
"serving connections through io_uring"):
`PYTHON=/home/user/venv/bin/python OHA=~/.cargo/bin/oha CONNECTIONS=N SERVER_CPU=0 CLIENT_CPU=3|1-3 [METHOD=POST BODY_FILE=scripts/order.json] bash scripts/profile_route.sh bessemer|axum ROUTE OUT`

## Read this first: the result depends on whether the server or the client is the bottleneck

At 64 connections with oha on a single core, Axum and the faster Bessemer configurations are client-limited, so the server is not
saturated (axum `/json` used 0.78 CPUs) and CPU per request is lower than when the server is the bottleneck. With the server saturated
(supplement, oha on CPUs 1-3), every server costs more per request and the gaps change:

| `/json`, 64 conn, µs CPU/req (median of 2) | oha on CPU 3 (matrix) | oha on CPUs 1-3 (server at 1.00 CPU) |
|---|--:|--:|
| axum | 8.03 (0.78 CPUs, client-limited) | 11.62 |
| bessemer rust loop | 9.93 (client-limited) | 12.23 |
| bessemer rust loop + io_uring | 9.33 (client-limited) | 10.19 |
| bessemer asyncio loop | 11.39 | 13.72 |

Use the saturated column (and the 1024-connection rows) for "cost when busy", and keep both in mind before splitting the gap. I did not
investigate why CPU/request rises when oha has three cores; it is observed for every server, including Axum.
`PYTHONPERFSUPPORT=1` + `perf record` did not visibly change CPU/request (step 3 runs, oha on CPUs 1-3: asyncio `/json` 13.86, rust 12.22, axum 11.44; one run each).

## Step 2 results (medians of two runs; `raw/matrix-tables.md` has both runs)

#### GET /json

| server | conns | req/s | CPUs | µs CPU/req | ctx sw/req | p50 ms | p99 ms | p99.9 ms | oha cores / util | flags |
|---|--:|--:|--:|--:|--:|--:|--:|--:|--:|---|
| bessemer-asyncio | 1 | 14,658 | 0.66 | **44.80** | 4.407 | 0.061 | 0.12 | 0.4 | 1 / 0.38 |  |
| bessemer-asyncio | 64 | 86,374 | 0.98 | **11.39** | 0.177 | 0.689 | 1.80 | 6.3 | 1 / 0.87 |  |
| bessemer-asyncio | 1024 | 39,948 | 0.98 | **24.61** | 0.561 | 27.962 | 63.65 | 75.7 | 3 / 0.33 | 15% 503 |
| bessemer-rust | 1 | 17,409 | 0.52 | **29.83** | 1.000 | 0.050 | 0.10 | 0.3 | 1 / 0.45 |  |
| bessemer-rust | 64 | 98,937 | 0.98 | **9.93** | 0.003 | 0.602 | 1.54 | 12.1 | 1 / 1.00 | CLIENT-LIMITED |
| bessemer-rust | 1024 | 73,303 | 0.99 | **13.55** | 0.001 | 13.400 | 20.75 | 27.7 | 3 / 0.35 |  |
| bessemer-rust-iouring | 1 | 16,797 | 0.53 | **31.41** | 1.001 | 0.052 | 0.10 | 0.3 | 1 / 0.44 |  |
| bessemer-rust-iouring | 64 | 104,422 | 0.97 | **9.33** | 0.005 | 0.547 | 1.92 | 10.8 | 1 / 1.00 | CLIENT-LIMITED |
| bessemer-rust-iouring | 1024 | 95,933 | 0.99 | **10.35** | 0.001 | 10.464 | 16.30 | 26.8 | 3 / 0.39 |  |
| axum | 1 | 21,869 | 0.43 | **19.63** | 1.000 | 0.041 | 0.08 | 0.2 | 1 / 0.53 |  |
| axum | 64 | 97,171 | 0.78 | **8.03** | 0.118 | 0.165 | 12.23 | 16.5 | 1 / 1.03 | CLIENT-LIMITED |
| axum | 1024 | 74,957 | 0.99 | **13.26** | 0.001 | 2.819 | 168.97 | 174.9 | 3 / 0.43 |  |

#### GET /items/7?q=abc

| server | conns | req/s | CPUs | µs CPU/req | ctx sw/req | p50 ms | p99 ms | p99.9 ms | oha cores / util | flags |
|---|--:|--:|--:|--:|--:|--:|--:|--:|--:|---|
| bessemer-asyncio | 1 | 14,165 | 0.66 | **46.65** | 4.404 | 0.063 | 0.12 | 0.4 | 1 / 0.37 |  |
| bessemer-asyncio | 64 | 79,958 | 0.99 | **12.35** | 0.139 | 0.761 | 1.58 | 3.2 | 1 / 0.80 |  |
| bessemer-asyncio | 1024 | 34,719 | 0.98 | **28.29** | 0.584 | 32.192 | 74.08 | 95.8 | 3 / 0.31 | 14% 503 |
| bessemer-rust | 1 | 16,453 | 0.53 | **32.57** | 1.001 | 0.052 | 0.11 | 0.3 | 1 / 0.44 |  |
| bessemer-rust | 64 | 89,595 | 0.99 | **11.01** | 0.002 | 0.669 | 1.67 | 10.8 | 1 / 0.94 |  |
| bessemer-rust | 1024 | 70,840 | 0.99 | **14.00** | 0.001 | 14.047 | 21.46 | 28.4 | 3 / 0.34 |  |
| bessemer-rust-iouring | 1 | 16,450 | 0.54 | **32.61** | 1.000 | 0.054 | 0.10 | 0.3 | 1 / 0.43 |  |
| bessemer-rust-iouring | 64 | 94,157 | 0.98 | **10.38** | 0.004 | 0.616 | 2.07 | 9.6 | 1 / 0.93 |  |
| bessemer-rust-iouring | 1024 | 88,050 | 0.99 | **11.27** | 0.001 | 11.446 | 16.32 | 21.2 | 3 / 0.36 |  |
| axum | 1 | 21,033 | 0.43 | **20.66** | 1.001 | 0.042 | 0.08 | 0.3 | 1 / 0.52 |  |
| axum | 64 | 103,874 | 0.84 | **8.11** | 0.084 | 0.154 | 12.22 | 18.7 | 1 / 1.05 | CLIENT-LIMITED |
| axum | 1024 | 66,979 | 0.99 | **14.84** | 0.001 | 3.184 | 170.85 | 179.2 | 3 / 0.40 |  |

#### POST /echo

| server | conns | req/s | CPUs | µs CPU/req | ctx sw/req | p50 ms | p99 ms | p99.9 ms | oha cores / util | flags |
|---|--:|--:|--:|--:|--:|--:|--:|--:|--:|---|
| bessemer-asyncio | 1 | 11,939 | 0.68 | **57.15** | 4.083 | 0.075 | 0.15 | 0.5 | 1 / 0.32 |  |
| bessemer-asyncio | 64 | 59,278 | 0.99 | **16.66** | 0.138 | 1.038 | 2.07 | 3.8 | 1 / 0.66 |  |
| bessemer-asyncio | 1024 | 34,948 | 0.98 | **28.18** | 0.358 | 14.762 | 107.27 | 131.4 | 3 / 0.32 | 16% 503 |
| bessemer-rust | 1 | 14,883 | 0.58 | **38.93** | 1.001 | 0.059 | 0.11 | 0.3 | 1 / 0.40 |  |
| bessemer-rust | 64 | 63,316 | 0.99 | **15.70** | 0.001 | 0.947 | 1.98 | 3.8 | 1 / 0.75 |  |
| bessemer-rust | 1024 | 53,026 | 0.99 | **18.72** | 0.001 | 18.742 | 26.11 | 38.7 | 3 / 0.28 |  |
| bessemer-rust-iouring | 1 | 14,260 | 0.59 | **41.07** | 1.000 | 0.061 | 0.13 | 0.4 | 1 / 0.39 |  |
| bessemer-rust-iouring | 64 | 65,653 | 0.99 | **15.12** | 0.001 | 0.960 | 1.95 | 3.3 | 1 / 0.74 |  |
| bessemer-rust-iouring | 1024 | 64,289 | 0.99 | **15.42** | 0.001 | 15.751 | 24.68 | 36.2 | 3 / 0.28 |  |
| axum | 1 | 18,890 | 0.49 | **26.20** | 1.000 | 0.047 | 0.09 | 0.2 | 1 / 0.48 |  |
| axum | 64 | 91,723 | 0.99 | **10.75** | 0.003 | 0.599 | 2.34 | 9.6 | 1 / 1.01 | CLIENT-LIMITED |
| axum | 1024 | 47,457 | 0.99 | **20.92** | 0.002 | 4.474 | 174.66 | 188.7 | 3 / 0.31 |  |

Flags and caveats for this matrix:
* **CLIENT-LIMITED**: oha >= 0.95 of its one core (axum 64 conn on all routes; rust loop and io_uring `/json` 64 conn). `/items` at 64 conn on the rust loop
  and io_uring was 0.91-0.95 (just under the threshold; run 1 of each was 0.95). Their req/s is a lower bound for the server and CPU/req was measured with the server below saturation.
  The supplement below repeats every 64-connection cell with oha on CPUs 1-3.
* **bessemer-asyncio at 1024 connections returns 503s** (14-16% of responses): the default `max_in_flight` of 128 sheds load. Its req/s and CPU/req count those cheap
  503s, so they are not comparable with Axum or the Rust loop (no 503s there). The supplement below reruns it with `BESSEMER_MAX_IN_FLIGHT=4096` (0 non-200).
* 1-connection rows: CPUs is well below 1 because the server is idle between requests; CPU/req there includes wake-ups (asyncio 4.4 context switches/request, rust loop and axum 1.0).
* Axum at 1024 connections: p50 2.8 ms but p99 169 ms (one worker thread, uneven service across connections). Axum had 1 worker thread in every cell except step 6.
* No run failed; every cell has 2 runs. Only `perf.data` of the step 3 runs is kept.

### Supplement A: 64 connections with oha on CPUs 1-3 (server is the bottleneck; `raw/supp-oha-cpus1-3-at64-tables.md`)

#### GET /json

| server | conns | req/s | CPUs | µs CPU/req | ctx sw/req | p50 ms | p99 ms | p99.9 ms | oha cores / util | flags |
|---|--:|--:|--:|--:|--:|--:|--:|--:|--:|---|
| bessemer-asyncio | 64 | 72,458 | 0.99 | **13.72** | 0.129 | 0.828 | 1.87 | 3.1 | 3 / 0.36 |  |
| bessemer-rust | 64 | 81,432 | 1.00 | **12.23** | 0.001 | 0.763 | 1.27 | 2.5 | 3 / 0.38 |  |
| bessemer-rust-iouring | 64 | 97,901 | 1.00 | **10.19** | 0.001 | 0.614 | 1.18 | 2.9 | 3 / 0.41 |  |
| axum | 64 | 85,840 | 1.00 | **11.62** | 0.000 | 0.745 | 1.19 | 1.7 | 3 / 0.43 |  |

#### GET /items/7?q=abc

| server | conns | req/s | CPUs | µs CPU/req | ctx sw/req | p50 ms | p99 ms | p99.9 ms | oha cores / util | flags |
|---|--:|--:|--:|--:|--:|--:|--:|--:|--:|---|
| bessemer-asyncio | 64 | 68,517 | 0.99 | **14.51** | 0.111 | 0.864 | 2.04 | 3.1 | 3 / 0.34 |  |
| bessemer-rust | 64 | 76,466 | 1.00 | **13.04** | 0.001 | 0.803 | 1.43 | 3.3 | 3 / 0.36 |  |
| bessemer-rust-iouring | 64 | 91,536 | 1.00 | **10.88** | 0.001 | 0.656 | 1.29 | 3.2 | 3 / 0.40 |  |
| axum | 64 | 84,362 | 1.00 | **11.84** | 0.000 | 0.765 | 1.16 | 2.0 | 3 / 0.42 |  |

#### POST /echo

| server | conns | req/s | CPUs | µs CPU/req | ctx sw/req | p50 ms | p99 ms | p99.9 ms | oha cores / util | flags |
|---|--:|--:|--:|--:|--:|--:|--:|--:|--:|---|
| bessemer-asyncio | 64 | 54,800 | 1.00 | **18.17** | 0.100 | 1.090 | 2.31 | 3.6 | 3 / 0.28 |  |
| bessemer-rust | 64 | 60,496 | 1.00 | **16.49** | 0.001 | 0.989 | 2.06 | 3.6 | 3 / 0.30 |  |
| bessemer-rust-iouring | 64 | 61,617 | 1.00 | **16.19** | 0.001 | 0.991 | 2.03 | 4.0 | 3 / 0.31 |  |
| axum | 64 | 64,003 | 1.00 | **15.60** | 0.000 | 0.943 | 1.82 | 3.2 | 3 / 0.33 |  |

### Supplement B: bessemer-asyncio at 1024 connections with `BESSEMER_MAX_IN_FLIGHT=4096` (`raw/supp-asyncio-max-in-flight-4096-tables.md`)

#### GET /json

| server | conns | req/s | CPUs | µs CPU/req | ctx sw/req | p50 ms | p99 ms | p99.9 ms | oha cores / util | flags |
|---|--:|--:|--:|--:|--:|--:|--:|--:|--:|---|
| bessemer-asyncio | 1024 | 41,267 | 0.98 | **23.84** | 0.464 | 26.649 | 52.16 | 65.0 | 3 / 0.30 |  |

#### GET /items/7?q=abc

| server | conns | req/s | CPUs | µs CPU/req | ctx sw/req | p50 ms | p99 ms | p99.9 ms | oha cores / util | flags |
|---|--:|--:|--:|--:|--:|--:|--:|--:|--:|---|
| bessemer-asyncio | 1024 | 34,239 | 0.98 | **28.82** | 0.494 | 32.347 | 63.22 | 78.9 | 3 / 0.26 |  |

#### POST /echo

| server | conns | req/s | CPUs | µs CPU/req | ctx sw/req | p50 ms | p99 ms | p99.9 ms | oha cores / util | flags |
|---|--:|--:|--:|--:|--:|--:|--:|--:|--:|---|
| bessemer-asyncio | 1024 | 33,619 | 0.99 | **29.38** | 0.292 | 33.560 | 72.52 | 107.5 | 3 / 0.26 |  |

## Step 3: per-thread split at 64 connections (µs CPU per request per thread)

From `/proc/<pid>/task/*/stat` over the 10 s window (10 ms tick resolution), medians of the two matrix runs (clean, oha on CPU 3):

| server / route | total (perf stat) | Python loop thread | Tokio worker threads | other |
|---|--:|--:|--:|--:|
| asyncio `/json` | 11.39 | 2.64 (0.23 CPUs) | 4.38 + 4.37 = 8.75 (2 workers, 0.38 CPUs each) | ~0 |
| asyncio `/items` | 12.35 | 2.91 | 4.72 + 4.71 = 9.43 | ~0 |
| asyncio `/echo` | 16.66 | 5.62 | 5.53 + 5.51 = 11.0 | ~0 |
| rust loop `/json` | 9.93 | 9.89 (one thread does Python, Tokio and the kernel work) | none | ~0 |
| rust loop `/items` | 11.01 | 10.92 | none | ~0 |
| rust loop `/echo` | 15.70 | 15.58 | none | ~0 |
| rust + io_uring `/json` | 9.33 | 9.25 | none | ~0 |
| axum `/json` | 8.03 | n/a | 8.05 (single Tokio worker) | ~0 |

(The asyncio rows include both Tokio workers because the worker runs a 2-thread multi-thread runtime next to the Python thread; all threads share CPU 0.)
These rows come from `raw/thread-split-64c-matrix.md`, which lists both runs. The step 3 profile runs (`raw/thread-split-64c-perfruns.md`) give the same split.
The supervisor process is not measured; the worker's main thread is idle.

The perf profiles are in `raw/perf/<server>-<route>/` (asyncio and rust loop on `/json` and `/echo`, axum `/json`; one run each, oha on CPUs 1-3, 4 s of `perf record -F 4999 -e cpu-clock -g`):
* `perf-top60-dso-sym.txt`: `perf report --no-children --sort dso,sym` top 60 lines (Rust symbols demangled with llvm-cxxfilt; the `-full.demangled.txt` file has all rows).
* `perf-samples-per-tid-dso.txt` and `perf-samples-per-tid.txt`: samples per thread and per DSO per thread (`perf report --sort tid` printed nothing on this perf build, so this was made from `perf script`). `perf-report-comm-dso.txt` is the requested `--sort comm,dso` report;
  **for the rust loop its `comm` column is misleading** (the loop thread appears as both `python` and `tokio-rt-worker`); use the per-tid files.
* `perf-children-python-frames.txt`: inclusive `py::` frames; only partly resolved, because callchains do not unwind cleanly through the interpreter, so use py-spy for Python-level detail.
* In every profile the largest single symbol is the kernel's `_raw_spin_unlock_irqrestore` (about 16-17% of samples). This VM has no PMU and `cpu-clock` samples land when interrupts are re-enabled, so
  that row is sampling skid of kernel work that ran with interrupts off, not a real function. Do not read it as a lock.
* Thread split by DSO (perf samples), `/json`: asyncio, Python thread 52% python3.13 / 31% `_native` / 11% kernel; each Tokio worker 59-60% kernel / 34-35% `_native` / 4% libc. Rust loop, one thread: 48% kernel / 36% `_native` / 8% python3.13 / 5% libc / 2.5% vdso.

## Step 4: py-spy (`raw/pyspy/<loop>-<route>/`)

`py-spy record --native --rate 2000 -d 8 -f raw -p <worker>` (blocking sampler, 64 connections, oha on CPUs 1-3, py-spy on CPU 2) gives `folded.txt`; `top30-python-frames.txt` has the top 30.
The throughput during sampling was 33k-51k req/s (py-spy stops the process to sample), so use the stacks, not the rates.
`--native` also samples threads that are blocked (supervisor main thread in a futex wait, parked Tokio workers), so `top30-python-frames.txt` keeps only stacks rooted at `Thread._bootstrap`, the Python loop thread, and reports shares of its wall-clock samples.
`folded-nonnative.txt` / `top30-python-frames-nonnative.txt` are a second set without `--native` (py-spy's default idle filter).

What they show (shares of the loop thread's samples):
* asyncio `/json`: `epoll_wait` (idle) 62%; `recv` 8%; the handler `json_route` is 1.2% (115 of 9,602 samples); `Handle._run` (33%) is the innermost Python frame above the native callbacks and `_run_once` is 1.7%; the other Python-level frames are each under 0.4%.
* rust loop `/json`: 98.9% of samples sit under `BaseEventLoop.run_until_complete` with a native leaf: `send` 48.7%, `recv` 23.5%, `epoll_wait` 1.4%; `json_route` 1.1%. `/echo`: `send` 30.7%, `recv` 19.2%, the handler `echo` 0.5%, and the codec symbols `bessemer_codec::Encoder::node/model`, `Parser::typed/typed_model/string/number/value`, `model::assemble`, `from_utf8` are each 0.4-1.1%.
* Python interpreter frames inside `python3.13` show as unresolved addresses (the binary is stripped), so the "leaf is Python" table is empty with `--native`; the `--native` "innermost Python frame" table and the non-native files carry the Python-level view.
* On the rust loop, `run_until_complete` (98.9% / 99.5%) is the innermost Python frame for everything native beneath it, and the handler is the only other Python frame above 0.1%.

## Step 5: instructions per request (`raw/callgrind/<loop>-<route>/`)

`benchmarks/profile/instructions.sh` (unmodified), `CONNECTIONS=1`, 5000 requests, one run each (the script documents ~0.1% repeatability; not repeated here):

| route | asyncio loop | rust loop |
|---|--:|--:|
| `/json` | 52,071 | 37,021 |
| `/items/7?q=abc` | 56,975 | 41,919 |
| POST `/echo` | 93,603 | 78,953 |

* `top50-self-cost.txt` in each directory is the top 50 functions by self instructions, parsed directly from the callgrind file (summed self cost matches the file's `summary:` within 0.15%).
* `callgrind_annotate-inclusive-no-top50.txt` is the requested `callgrind_annotate --inclusive=no` output. **It is not reliable here**: callgrind_annotate 3.22 reports self costs above 1000% of the total for the recursive `_PyEval_EvalFrameDefault'2` entries on these profiles and then lists inclusive-looking rows, so use `top50-self-cost.txt`.
  The raw `callgrind.out.*` files are kept (the largest numbered `.1` dump of the worker is the measured window).

## Step 6: multi-core sanity (`raw/multicore/`)

`/json`, 1024 connections, servers pinned to CPUs 0-1 (bessemer `--workers 2`, axum `AXUM_THREADS=2`), oha on CPUs 2-3, two runs each. Bessemer's supervisor and both workers are pinned to CPUs 0-1.

| server (2 workers/threads on CPUs 0-1; oha on CPUs 2-3) | run | req/s | server CPUs | us CPU/req | ctx sw/req | p50 ms | p99 ms | p99.9 ms | oha util (2 cores) | non-200 |
|---|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|
| bessemer-rust | 1 | 174,527 | 1.97 | 11.26 | 0.002 | 5.96 | 12.39 | 23.3 | 0.95 CLIENT-LIMITED | 0.0% |
| bessemer-rust | 2 | 170,145 | 1.96 | 11.49 | 0.002 | 5.74 | 11.65 | 21.8 | 0.96 CLIENT-LIMITED | 0.0% |
| **bessemer-rust median** | | **172,336** | 1.96 | **11.38** | 0.002 | 5.85 | **12.02** | 22.6 | 0.96 | |
| bessemer-asyncio | 1 | 111,247 | 1.96 | 17.58 | 0.723 | 2.59 | 38.73 | 648.2 | 0.79 | 2.8% |
| bessemer-asyncio | 2 | 118,499 | 1.97 | 16.58 | 0.512 | 2.75 | 33.95 | 515.3 | 0.82 | 3.8% |
| **bessemer-asyncio median** | | **114,873** | 1.96 | **17.08** | 0.618 | 2.67 | **36.34** | 581.7 | 0.80 | |
| axum | 1 | 205,414 | 1.84 | 8.97 | 0.027 | 6.26 | 10.50 | 19.5 | 1.05 CLIENT-LIMITED | 0.0% |
| axum | 2 | 204,103 | 1.84 | 9.04 | 0.026 | 6.26 | 10.52 | 23.9 | 1.04 CLIENT-LIMITED | 0.0% |
| **axum median** | | **204,758** | 1.84 | **9.00** | 0.027 | 6.26 | **10.51** | 21.7 | 1.04 | |
| bessemer-asyncio-max-in-flight-4096 | 1 | 146,603 | 1.96 | 13.35 | 0.214 | 6.11 | 18.26 | 40.0 | 0.84 | 0.0% |
| bessemer-asyncio-max-in-flight-4096 | 2 | 152,587 | 1.97 | 12.91 | 0.193 | 6.12 | 15.09 | 26.1 | 0.87 | 0.0% |
| **bessemer-asyncio-max-in-flight-4096 median** | | **149,595** | 1.96 | **13.13** | 0.203 | 6.12 | **16.68** | 33.0 | 0.86 | |

* Rust loop and Axum are **CLIENT-LIMITED** here (oha at 0.95-1.05 of its two cores); their req/s are lower bounds and Axum's server used only 1.84 of 2 CPUs.
* Default asyncio loses 3-4% of responses to 503 (`max_in_flight` 128 per worker) and has p99.9 of 515-648 ms; the 4096 row has no 503s.

## Where to look (analyst reading list)

* This README, then `raw/matrix-tables.md` (both runs of every cell) and `raw/matrix-all-runs.csv`.
* perf: `raw/perf/*/perf-top60-dso-sym.txt`, `raw/perf/*/perf-samples-per-tid-dso.txt`, `raw/perf/*/perf.data` (+ `perf-<pid>.map` for Python frame names).
* Per-thread CPU per request: `raw/thread-split-64c-matrix.md`, `raw/thread-split-64c-perfruns.md`.
* py-spy: `raw/pyspy/*/top30-python-frames.txt`, `raw/pyspy/*/folded.txt`, `raw/pyspy/*/folded-nonnative.txt`.
* callgrind: `raw/callgrind/*/top50-self-cost.txt`, `raw/callgrind/*/callgrind.out.*`.
* Multi-core: `raw/multicore-table.md`, `raw/multicore/*/run*/result.json`.
* Scripts: `scripts/` (profile_route.sh, order.json, run_*.sh, run_matrix.py, aggregate.py, threads.py, callgrind_self.py, pyspy_top*.py).
