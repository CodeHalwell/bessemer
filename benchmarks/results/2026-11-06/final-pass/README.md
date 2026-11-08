# Final before/after benchmarks, 2026-11-06

CPU per request, latency and instruction counts of `main` ("before") against this branch ("after") and the Axum comparison binary.
Every number below comes from a run made on this machine while nothing else ran. Runs were strictly sequential.
Raw outputs were in `raw/` and are no longer in the working tree; they remain in Git
history at commit 7cc4b95 (`git show 7cc4b95:benchmarks/results/2026-11-06/final-pass/raw/...`). The scripts are in `scripts/`.

## Environment

| | |
|---|---|
| before | `main` at `7475503386b11ca2a041931c668b6f4b8657e364`; venv `/home/user/venv` (`bessemer 1.0.0rc1`, wheel built from main, not modified) |
| after | branch `claude/project-thread-8mp3j6` at `365117326cd12b00b2d47e1954ab8470391b61bc`; venv `/home/user/venvs/integrated` (`bessemer 1.0.0rc1`, wheel from that head) |
| Python | CPython 3.13.18 in both venvs, pydantic 2.14.3 |
| kernel / CPU | Linux 6.18.44-fc-v242, 4 vCPU Intel Xeon @ 2.10 GHz (shared VM) |
| tools | oha 1.16.0, perf 6.8.1 (software events only), valgrind 3.22.0 |
| Axum | `benchmarks/compare/axum_app/target/release/axum-compare PORT THREADS` (built 16:02), 1 thread except in the 2-worker run |
| app | `benchmarks/compare/bessemer_app.py` for both; order body `scripts/order.json` (185 bytes) |
| settings | defaults (`max_in_flight` 128, `max_connections` 1024, `workers` 1) plus `BESSEMER_EVENT_LOOP` / `BESSEMER_IO_BACKEND` as listed |

Load: `uptime` was 0.09 / 0.34 / 0.52 just before the matrix started and 2.19 / 2.22 / 2.01 before the 2-worker run (the one-minute
average then still contained the previous benchmark's own load; nothing else was running). `raw/matrix/*/*/*/*/loadavg.txt` has `/proc/loadavg`
read just before each of the 108 matrix runs: one-minute values 0.09 to 3.16 (median 1.90), five-minute maximum 2.24. These values are the
benchmarks' own trailing load (oha + server use about 2 to 4 CPUs while a run lasts), not other work. Same files for the supplement
(`raw/supp-max-in-flight-4096`, 1.48 to 3.06), the 2-worker runs (`raw/multicore`, 2.19 to 4.53), callgrind (`raw/callgrind-loadavg-*.txt`,
1.02 to 4.96; callgrind itself is a deterministic count, load does not matter) and perf (`raw/perf/branch-rust-json/loadavg.txt`, 1.01).
`raw/uptime-before-*.txt` has the `uptime` line before each phase.

## Method

Same collection method as `../reprofile/`; the archived `result.json` files preserve the original profiling output, and the
aggregators apply the response-count correction described below.
Per run: a fresh server pinned to CPU 0 with `taskset`, 3 s warm-up, one 10 s window. oha ran on **CPUs 1-3** (`taskset -c 1-3`) for
every connection count (the reprofile used CPU 3 alone for 1 and 64 connections, where Axum and the Rust loop were client-limited).
CPU per completed response = server `task-clock` (`perf stat -p` over the worker process) divided by completed responses per second
times the `perf stat` window. The response rate is the sum of oha's HTTP status counts divided by its measured duration. The two
measurement windows overlap but have slightly different lengths, so the response count within the `perf stat` window is estimated
from oha's average rate. Context switches use the same denominator. p50 / p99 / p99.9 come from oha's JSON. A run is flagged CLIENT-LIMITED when oha used 0.95 or more of its cores; **none of the 108 matrix runs
was** (highest oha utilisation 0.75), so every matrix cell had the server saturated or (1 connection) latency-bound.
Every cell ran twice: round 1 in the order main-asyncio, main-rust, branch-default, branch-asyncio, branch-rust-iouring, axum; round 2 in
the reverse order. Tables show the median of the two runs (the mean) and `+-` is half the difference between the two runs.

* **Loop selection.** Every Bessemer config passes the loop explicitly through `BESSEMER_EVENT_LOOP`: `asyncio`, `bessemer` (the Rust loop), or `auto` for branch-default.
  The branch's `server.log` prints `event_loop=bessemer` for branch-default (so auto resolved to the Rust loop) and for branch-rust-iouring (plus
  "serving connections through io_uring"), and `event_loop=asyncio` for branch-asyncio, in all runs. Main's log does not print the loop at all; for main the
  thread list in `pids.txt` confirms it (asyncio: two `tokio-rt-worker` threads next to the Python thread; Rust loop: none) and so do the context switches per request (4.4 against 1.0 at 1 connection).
* **req/s** is HTTP 200 responses per second (200 status count / oha duration). **non-200** is the share of completed responses that were not 200 (all were 503). **us CPU/req** divides by all
  completed responses, 503s included, so a cell with 503s is not comparable with one without. oha's deadline aborts and cancelled operations are excluded from response counts and remain visible in its error distribution.
* `max_in_flight` is the default 128 everywhere except in the supplement below.

### Response-count correction

The original `result.json` summaries used oha's attempts per second as the response rate. The raw `oha.json` and `result.json`
files remain unchanged. `scripts/aggregate.py` and `scripts/aggregate_extra.py` now replay the raw counts to regenerate the tables,
CSV files and headlines; `scripts/profile_route.sh` uses completed response counts for future runs. For example,
`raw/matrix/branch-rust-iouring/items/c1024/run1` made 670,846 attempts, received 666,018 responses, and recorded 4,828 errors
over 10.026232196 seconds. Its 200 response rate is 45,124.53/s (previously reported as 45,451.64/s), and server CPU per completed
response is 14.40 microseconds (previously 14.30 per attempt). No benchmark was rerun for this correction.

### Commands

```
export PATH=$PATH:/usr/local/bin:~/.cargo/bin
cd /home/user/bessemer/benchmarks/results/2026-11-06/final-pass
python3 scripts/run_matrix.py                                  # 108 runs -> raw/matrix/<config>/<route>/c<N>/run<R>/
SUBDIR=supp-max-in-flight-4096 ONLY_CONNS=1024 ONLY_CONFIGS=main-asyncio,main-rust,branch-default,branch-asyncio,branch-rust-iouring \
  SERVER_EXTRA="BESSEMER_MAX_IN_FLIGHT=4096" python3 scripts/run_matrix.py     # 30 runs
bash scripts/run_multicore.sh                                  # 2 workers / 2 Axum threads on CPUs 0-1, oha CPUs 2-3
bash scripts/run_callgrind.sh                                  # benchmarks/profile/instructions.sh, CONNECTIONS=1, 5000 requests
bash scripts/run_perf.sh                                       # PERF_RECORD=1, PYTHONPERFSUPPORT=1, 64 conn, oha CPUs 1-3
python3 scripts/aggregate.py; python3 scripts/aggregate.py supp-max-in-flight-4096; python3 scripts/aggregate_extra.py
```

Each cell of the matrix is
`PYTHON=<venv>/bin/python OHA=~/.cargo/bin/oha CONNECTIONS=N SERVER_CPU=0 CLIENT_CPU=1-3 BESSEMER_EVENT_LOOP=<loop> [BESSEMER_IO_BACKEND=io_uring] [METHOD=POST BODY_FILE=scripts/order.json] bash scripts/profile_route.sh bessemer|axum ROUTE OUT`.

## Read this first: the branch's Rust loop sheds 31-34% of completed responses at 1024 connections; main's does not

**Fixed since:** see "After the in-flight fix" at the end; the cause was the loop body running as a FIFO task (perf/wi-2), and the branch now refuses none at 1024 connections. The tables below are the measurements from before the fix.

At 1024 connections with the default `max_in_flight` of 128, **branch-default and branch-rust-iouring answered 503 to 31-34% of completed responses on every route,
in both runs; main-rust answered 0%** (main-asyncio 14-17%, branch-asyncio 16-17%). The branch therefore delivers fewer 200s per second than main's Rust loop at 1024
connections (/json: 43,982 against 76,611 200 responses/s; /items 42,112 against 72,146; /echo 37,212 against 53,107), and its us CPU/req
at 1024 (/json 14.82, /items 15.48) is **higher** than main-rust's (12.93, 13.77), beyond the two-run spread. This is a behaviour change at the default setting, not a CPU cost:
with `BESSEMER_MAX_IN_FLIGHT=4096` (supplement) the branch has no 503s and is cheaper than main-rust (11.35 against 12.68 us on /json). I did not investigate why the
branch's loop admits more than 128 requests at once; it is the one regression found. The 1024-connection cells of the three tables below are all affected by shedding for every config except main-rust and axum.
The headline table therefore also has a 1024-connection variant with shedding removed.

## Headline: us CPU per request (median of 2 runs), oha on CPUs 1-3

% vs axum: positive = more CPU per request than Axum. % change vs main default: negative = cheaper than main's default (asyncio) loop.
Branch default = `auto`, which resolved to the Rust loop (epoll).

| route | conns | main default (asyncio) us/req | branch default (rust) us/req | axum us/req | branch vs axum | main default vs axum | branch vs main default | notes |
|---|--:|--:|--:|--:|--:|--:|--:|---|
| GET /json | 64 | 13.73 | 10.52 | 11.64 | -10% | +18% | -23% |  |
| GET /json | 1024 | 27.59 | 14.82 | 13.56 | +9% | +103% | -46% | main: non-200; branch: non-200 |
| GET /items/7?q=abc | 64 | 14.40 | 10.82 | 11.83 | -9% | +22% | -25% |  |
| GET /items/7?q=abc | 1024 | 26.51 | 15.48 | 14.83 | +4% | +79% | -42% | main: non-200; branch: non-200 |
| POST /echo | 64 | 17.91 | 13.43 | 14.61 | -8% | +23% | -25% |  |
| POST /echo | 1024 | 27.67 | 17.59 | 20.65 | -15% | +34% | -36% | main: non-200; branch: non-200 |

1024 connections with `max_in_flight` 4096 for the two Bessemer configs (supplement, 0 non-200; Axum from the main matrix, 0 non-200):

| route | conns | main default (asyncio, max_in_flight 4096) us/req | branch default (rust, max_in_flight 4096) us/req | axum us/req | branch vs axum | main default vs axum | branch vs main default |
|---|--:|--:|--:|--:|--:|--:|--:|
| GET /json | 1024 | 20.93 | 11.35 | 13.56 | -16% | +54% | -46% |
| GET /items/7?q=abc | 1024 | 22.57 | 11.82 | 14.83 | -20% | +52% | -48% |
| POST /echo | 1024 | 23.67 | 14.59 | 20.65 | -29% | +15% | -38% |

## Matrix: per route, median of 2 runs

Columns per connection count: HTTP 200 responses/s, server CPUs used, us CPU/req (+- half the two-run difference), context switches/req, p50 / p99 / p99.9, share of non-200 (503) responses.
Rows with `non-200` have cheap shed responses in req/s and CPU/req.
`CLIENT-LIMITED` appears nowhere. Per-run values: `matrix-all-runs.csv`.

#### GET /json

| config | 1: req/s (200) | CPUs | us CPU/req (+-) | ctx sw/req | p50 / p99 / p99.9 ms | non-200 | flags | 64: req/s (200) | CPUs | us CPU/req (+-) | ctx sw/req | p50 / p99 / p99.9 ms | non-200 | flags | 1024: req/s (200) | CPUs | us CPU/req (+-) | ctx sw/req | p50 / p99 / p99.9 ms | non-200 | flags |
|---|--:|--:|--:|--:|--:|--:|---|--:|--:|--:|--:|--:|--:|---|--:|--:|--:|--:|--:|--:|---|
| main-asyncio | 14,722 | 0.65 | **43.95** (+-0.35) | 4.410 | 0.061 / 0.11 / 0.3 | 0 |  | 72,475 | 0.99 | **13.73** (+-0.40) | 0.127 | 0.833 / 1.81 / 2.8 | 0 |  | 30,534 | 0.98 | **27.59** (+-0.11) | 0.573 | 30.868 / 74.26 / 105.0 | 14.2% | non-200 |
| main-rust | 17,928 | 0.52 | **29.18** (+-1.01) | 1.001 | 0.049 / 0.10 / 0.2 | 0 |  | 81,485 | 1.00 | **12.25** (+-0.18) | 0.001 | 0.758 / 1.44 / 2.6 | 0 |  | 76,611 | 0.99 | **12.93** (+-0.03) | 0.001 | 12.956 / 19.56 / 25.8 | 0 |  |
| branch-default | 18,524 | 0.50 | **26.93** (+-0.59) | 1.001 | 0.048 / 0.09 / 0.3 | 0 |  | 94,835 | 1.00 | **10.52** (+-0.00) | 0.001 | 0.664 / 1.13 / 2.3 | 0 |  | 43,982 | 0.99 | **14.82** (+-0.24) | 0.002 | 8.188 / 40.38 / 48.7 | 34.0% | non-200 |
| branch-asyncio | 14,955 | 0.65 | **43.34** (+-0.41) | 4.408 | 0.060 / 0.11 / 0.3 | 0 |  | 78,288 | 0.99 | **12.70** (+-0.02) | 0.128 | 0.759 / 1.84 / 3.0 | 0 |  | 35,746 | 0.98 | **22.78** (+-0.55) | 0.567 | 25.596 / 60.16 / 73.3 | 17.1% | non-200 |
| branch-rust-iouring | 18,190 | 0.51 | **28.15** (+-0.07) | 1.000 | 0.050 / 0.09 / 0.3 | 0 |  | 103,238 | 1.00 | **9.67** (+-0.05) | 0.001 | 0.602 / 1.03 / 1.8 | 0 |  | 47,323 | 0.94 | **13.70** (+-0.31) | 0.006 | 14.579 / 43.31 / 69.4 | 31.2% | non-200 |
| axum | 21,695 | 0.43 | **19.73** (+-0.29) | 0.999 | 0.041 / 0.08 / 0.2 | 0 |  | 85,814 | 1.00 | **11.64** (+-0.17) | 0.000 | 0.749 / 1.19 / 1.8 | 0 |  | 73,105 | 0.99 | **13.56** (+-0.03) | 0.001 | 2.876 / 169.72 / 176.1 | 0 |  |

#### GET /items/7?q=abc

| config | 1: req/s (200) | CPUs | us CPU/req (+-) | ctx sw/req | p50 / p99 / p99.9 ms | non-200 | flags | 64: req/s (200) | CPUs | us CPU/req (+-) | ctx sw/req | p50 / p99 / p99.9 ms | non-200 | flags | 1024: req/s (200) | CPUs | us CPU/req (+-) | ctx sw/req | p50 / p99 / p99.9 ms | non-200 | flags |
|---|--:|--:|--:|--:|--:|--:|---|--:|--:|--:|--:|--:|--:|---|--:|--:|--:|--:|--:|--:|---|
| main-asyncio | 14,186 | 0.66 | **46.77** (+-0.18) | 4.404 | 0.063 / 0.12 / 0.3 | 0 |  | 69,092 | 0.99 | **14.40** (+-0.06) | 0.115 | 0.865 / 1.93 / 3.1 | 0 |  | 31,768 | 0.98 | **26.51** (+-0.81) | 0.578 | 30.222 / 69.05 / 91.2 | 14.3% | non-200 |
| main-rust | 17,396 | 0.53 | **30.61** (+-0.96) | 1.001 | 0.051 / 0.10 / 0.3 | 0 |  | 78,037 | 1.00 | **12.76** (+-0.15) | 0.001 | 0.789 / 1.36 / 3.0 | 0 |  | 72,146 | 0.99 | **13.77** (+-0.27) | 0.001 | 13.881 / 18.56 / 28.2 | 0 |  |
| branch-default | 18,404 | 0.51 | **27.61** (+-0.34) | 1.001 | 0.049 / 0.09 / 0.2 | 0 |  | 92,158 | 1.00 | **10.82** (+-0.09) | 0.001 | 0.684 / 1.13 / 1.9 | 0 |  | 42,112 | 0.99 | **15.48** (+-0.32) | 0.003 | 8.510 / 43.46 / 57.2 | 34.0% | non-200 |
| branch-asyncio | 14,961 | 0.65 | **43.26** (+-0.00) | 4.415 | 0.060 / 0.11 / 0.3 | 0 |  | 79,807 | 0.99 | **12.47** (+-0.27) | 0.117 | 0.752 / 1.66 / 2.6 | 0 |  | 35,017 | 0.98 | **23.63** (+-0.02) | 0.566 | 26.503 / 62.00 / 77.2 | 15.8% | non-200 |
| branch-rust-iouring | 17,555 | 0.52 | **29.58** (+-0.27) | 1.000 | 0.051 / 0.09 / 0.3 | 0 |  | 97,782 | 1.00 | **10.20** (+-0.02) | 0.001 | 0.637 / 1.05 / 2.1 | 0 |  | 45,176 | 0.96 | **14.45** (+-0.05) | 0.006 | 14.999 / 41.00 / 68.2 | 31.7% | non-200 |
| axum | 21,645 | 0.44 | **20.22** (+-0.47) | 0.999 | 0.041 / 0.08 / 0.2 | 0 |  | 84,399 | 1.00 | **11.83** (+-0.04) | 0.000 | 0.768 / 1.09 / 1.9 | 0 |  | 67,018 | 0.99 | **14.83** (+-0.05) | 0.001 | 3.175 / 170.71 / 178.6 | 0 |  |

#### POST /echo

| config | 1: req/s (200) | CPUs | us CPU/req (+-) | ctx sw/req | p50 / p99 / p99.9 ms | non-200 | flags | 64: req/s (200) | CPUs | us CPU/req (+-) | ctx sw/req | p50 / p99 / p99.9 ms | non-200 | flags | 1024: req/s (200) | CPUs | us CPU/req (+-) | ctx sw/req | p50 / p99 / p99.9 ms | non-200 | flags |
|---|--:|--:|--:|--:|--:|--:|---|--:|--:|--:|--:|--:|--:|---|--:|--:|--:|--:|--:|--:|---|
| main-asyncio | 12,263 | 0.68 | **55.28** (+-0.05) | 4.086 | 0.073 / 0.14 / 0.4 | 0 |  | 55,592 | 1.00 | **17.91** (+-0.09) | 0.100 | 1.073 / 2.32 / 3.5 | 0 |  | 29,700 | 0.98 | **27.67** (+-0.58) | 0.372 | 14.129 / 105.71 / 126.2 | 16.5% | non-200 |
| main-rust | 15,313 | 0.59 | **38.21** (+-0.28) | 1.001 | 0.058 / 0.11 / 0.3 | 0 |  | 61,354 | 1.00 | **16.26** (+-0.05) | 0.001 | 0.977 / 1.89 / 3.5 | 0 |  | 53,107 | 0.99 | **18.69** (+-0.36) | 0.001 | 18.517 / 30.56 / 38.1 | 0 |  |
| branch-default | 16,260 | 0.55 | **33.98** (+-0.15) | 1.001 | 0.055 / 0.10 / 0.3 | 0 |  | 74,255 | 1.00 | **13.43** (+-0.19) | 0.001 | 0.849 / 1.43 / 2.3 | 0 |  | 37,212 | 0.99 | **17.59** (+-0.33) | 0.003 | 9.734 / 49.83 / 61.5 | 33.9% | non-200 |
| branch-asyncio | 13,864 | 0.70 | **50.79** (+-0.42) | 5.108 | 0.066 / 0.12 / 0.3 | 0 |  | 68,826 | 1.00 | **14.48** (+-0.35) | 0.085 | 0.887 / 1.70 / 3.0 | 0 |  | 35,696 | 0.99 | **23.05** (+-0.75) | 0.313 | 23.963 / 54.55 / 64.9 | 16.7% | non-200 |
| branch-rust-iouring | 15,629 | 0.56 | **35.98** (+-0.88) | 1.000 | 0.057 / 0.10 / 0.3 | 0 |  | 76,236 | 1.00 | **13.08** (+-0.02) | 0.001 | 0.819 / 1.32 / 2.3 | 0 |  | 38,365 | 0.96 | **16.66** (+-0.10) | 0.006 | 17.225 / 46.11 / 84.9 | 33.3% | non-200 |
| axum | 19,295 | 0.49 | **25.54** (+-0.09) | 1.000 | 0.047 / 0.08 / 0.2 | 0 |  | 68,371 | 1.00 | **14.61** (+-0.10) | 0.000 | 0.909 / 1.65 / 2.8 | 0 |  | 48,080 | 0.99 | **20.65** (+-0.14) | 0.001 | 4.409 / 172.11 / 181.8 | 0 |  |

## Supplement: 1024 connections with `BESSEMER_MAX_IN_FLIGHT=4096` (no shedding; `supp-max-in-flight-4096-tables.md`)

Axum is not repeated (it never sheds; see the matrix).

#### GET /json

| config | 1024: req/s (200) | CPUs | us CPU/req (+-) | ctx sw/req | p50 / p99 / p99.9 ms | non-200 | flags |
|---|--:|--:|--:|--:|--:|--:|---|
| main-asyncio | 47,098 | 0.99 | **20.93** (+-0.04) | 0.405 | 22.793 / 43.84 / 57.8 | 0 |  |
| main-rust | 78,165 | 0.99 | **12.68** (+-0.10) | 0.001 | 12.779 / 17.97 / 22.9 | 0 |  |
| branch-default | 87,449 | 0.99 | **11.35** (+-0.15) | 0.001 | 11.197 / 18.27 / 28.0 | 0 |  |
| branch-asyncio | 52,359 | 0.99 | **18.82** (+-0.19) | 0.395 | 20.245 / 38.81 / 48.8 | 0 |  |
| branch-rust-iouring | 108,382 | 0.99 | **9.15** (+-0.11) | 0.000 | 9.281 / 13.55 / 25.0 | 0 |  |

#### GET /items/7?q=abc

| config | 1024: req/s (200) | CPUs | us CPU/req (+-) | ctx sw/req | p50 / p99 / p99.9 ms | non-200 | flags |
|---|--:|--:|--:|--:|--:|--:|---|
| main-asyncio | 43,685 | 0.99 | **22.57** (+-0.15) | 0.400 | 24.782 / 46.62 / 59.4 | 0 |  |
| main-rust | 70,796 | 0.99 | **14.01** (+-0.04) | 0.001 | 13.993 / 20.01 / 31.5 | 0 |  |
| branch-default | 83,919 | 0.99 | **11.82** (+-0.06) | 0.001 | 11.831 / 18.38 / 27.4 | 0 |  |
| branch-asyncio | 49,096 | 0.98 | **20.06** (+-0.30) | 0.401 | 21.729 / 40.89 / 51.5 | 0 |  |
| branch-rust-iouring | 103,066 | 0.99 | **9.64** (+-0.08) | 0.001 | 9.799 / 14.71 / 25.1 | 0 |  |

#### POST /echo

| config | 1024: req/s (200) | CPUs | us CPU/req (+-) | ctx sw/req | p50 / p99 / p99.9 ms | non-200 | flags |
|---|--:|--:|--:|--:|--:|--:|---|
| main-asyncio | 41,803 | 0.99 | **23.67** (+-0.37) | 0.207 | 26.335 / 46.76 / 57.7 | 0 |  |
| main-rust | 53,721 | 0.99 | **18.48** (+-0.35) | 0.001 | 18.448 / 28.76 / 34.7 | 0 |  |
| branch-default | 67,941 | 0.99 | **14.59** (+-0.01) | 0.001 | 14.384 / 23.61 / 44.8 | 0 |  |
| branch-asyncio | 50,608 | 0.99 | **19.58** (+-0.25) | 0.135 | 19.578 / 38.33 / 49.3 | 0 |  |
| branch-rust-iouring | 78,669 | 0.99 | **12.62** (+-0.12) | 0.001 | 12.959 / 17.72 / 37.3 | 0 |  |

## Two workers: /json, 1024 connections, servers on CPUs 0-1, oha on CPUs 2-3 (`multicore-table.md`)

Main default and branch default use `--workers 2` (`WORKERS=2`), Axum uses 2 threads. Two runs each, order main, branch, axum then axum, branch, main.
Branch and Axum are **CLIENT-LIMITED** here (oha at 1.02-1.04 of its two cores): their req/s are lower bounds, and Axum's server used 1.83 of 2 CPUs.

| config (2 workers / threads on CPUs 0-1; oha on CPUs 2-3) | run | req/s (200) | server CPUs | us CPU/req | ctx sw/req | p50 ms | p99 ms | p99.9 ms | oha util (2 cores) | non-200 |
|---|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|
| main-default | 1 | 125,613 | 1.96 | 15.34 | 0.501 | 2.29 | 29.80 | 120.5 | 0.79 | 22492 (1.8%) |
| main-default | 2 | 119,065 | 1.97 | 15.80 | 0.526 | 2.75 | 29.54 | 271.1 | 0.81 | 52804 (4.2%) |
| **main-default median** | | **122,339** | 1.96 | **15.57** | 0.513 | 2.52 | **29.67** | **195.8** | 0.80 | 37648 |
| branch-default | 1 | 182,282 | 1.94 | 10.28 | 0.003 | 5.45 | 12.64 | 20.8 | 1.03 CLIENT-LIMITED | 60073 (3.2%) |
| branch-default | 2 | 182,904 | 1.90 | 10.18 | 0.006 | 5.88 | 13.07 | 20.9 | 1.03 CLIENT-LIMITED | 42689 (2.3%) |
| **branch-default median** | | **182,593** | 1.92 | **10.23** | 0.005 | 5.67 | **12.85** | **20.8** | 1.03 | 51381 |
| axum | 1 | 214,983 | 1.85 | 8.60 | 0.024 | 6.15 | 10.47 | 27.5 | 1.04 CLIENT-LIMITED | 0 (0.0%) |
| axum | 2 | 210,374 | 1.81 | 8.62 | 0.025 | 6.14 | 11.04 | 57.8 | 1.02 CLIENT-LIMITED | 0 (0.0%) |
| **axum median** | | **212,679** | 1.83 | **8.61** | 0.024 | 6.14 | **10.75** | **42.7** | 1.03 | 0 |

## Instructions per request (callgrind, `CONNECTIONS=1`, 5000 requests, one run each; `callgrind-table.md`)

`benchmarks/profile/instructions.sh` from this branch (it honours `BESSEMER_EVENT_LOOP` and passes `--event-loop`); the same 5000 requests for every config.
User-space instructions only. The script documents 0.1% repeatability; not repeated. The script takes the largest numbered dump of the worker as the measured window.
**No dump was discarded**: all 12 selections gave counts in the expected range, and the two main columns reproduce the reprofile's (asyncio /json 52,154 against 52,071; Rust loop /json 37,021 against 37,021;
/items 56,992 and 41,817 against 56,975 and 41,919; /echo 93,401 and 79,200 against 93,603 and 78,953). Raw dumps: `raw/callgrind/<config>-<route>/`; the summary lines are in `raw/callgrind.log`.

| route | main asyncio | main rust | branch asyncio | branch rust | branch rust vs main rust | branch asyncio vs main asyncio |
|---|--:|--:|--:|--:|--:|--:|
| GET /json | 52,154 | 37,021 | 48,685 | 32,883 | -11.2% | -6.7% |
| GET /items/7?q=abc | 56,992 | 41,817 | 51,773 | 36,064 | -13.8% | -9.2% |
| POST /echo | 93,401 | 79,200 | 79,504 | 63,101 | -20.3% | -14.9% |

## perf record: [vdso] share (64 connections, branch Rust loop, /json)

`scripts/run_perf.sh`: `PYTHONPERFSUPPORT=1`, `perf record -F 4999 -e cpu-clock -g` for 4 s in a separate load phase after the measured window (oha on CPUs 1-3, one run);
19,923 samples. The measured window of that run: 92,070 completed responses/s, 10.84 us CPU/response. Share of the loop thread's samples by the leaf frame's DSO (made from `perf script`, the same way as the reprofile's `perf-samples-per-tid-dso.txt`):

```
  9948  49.94% of thread 12357/12359 dso=[kernel.kallsyms]
  6573  33.00% of thread 12357/12359 dso=/home/user/venvs/integrated/lib/python3.13/site-packages/bessemer/_native.cpython-313-x86_64-linux-gnu.so
  1939   9.73% of thread 12357/12359 dso=/usr/local/bin/python3.13
  1041   5.23% of thread 12357/12359 dso=/usr/lib/x86_64-linux-gnu/libc.so.6
   327   1.64% of thread 12357/12359 dso=[vdso]
    88   0.44% of thread 12357/12359 dso=/usr/lib/x86_64-linux-gnu/ld-linux-x86-64.so.2
     3   0.02% of thread 12357/12359 dso=/tmp/perf-12357.map
     3  75.00% of thread 12357/12357 dso=[kernel.kallsyms]
     1  25.00% of thread 12357/12357 dso=/home/user/venvs/integrated/lib/python3.13/site-packages/bessemer/_native.cpython-313-x86_64-linux-gnu.so
```

**[vdso] is 1.64% of the loop thread's samples (327 of 19,923), against 2.49% in the reprofile** (main's Rust loop, same method). `perf.data` and the `perf-*.map` files were dropped
after the reports were extracted; `raw/perf/branch-rust-json/` keeps the reports, `result.json`, `oha.json` and the logs.

## Files

* `headline.md`, `headline-1024-no-shedding.md`, `matrix-tables.md`, `matrix-all-runs.csv`: matrix results.
* `supp-max-in-flight-4096-*`, `multicore-table.md`, `after-inflight-fix-table.md`, `callgrind-table.md`: the other results above.
* `raw/matrix/<config>/<route>/c<N>/run<R>/`: `result.json`, `oha.json`, `stat.txt`, `server.log`, `pids.txt`, `loadavg.txt`, per-thread CPU snapshots. Also `raw/*.log` for the runners' output.
* `scripts/`: runners and aggregators (`threads.py` is the reprofile's; `profile_route.sh` has the response-count correction).

## After the in-flight fix (1024 connections, default `max_in_flight` 128)

**Cause.** perf/wi-2 (`e787976`, the change in `38f7028`) made the Rust loop's body a task spawned on its runtime instead of the
runtime's `block_on` future (`crates/bessemer-loop/src/core.rs`, `run_driven`). Tokio's current-thread scheduler polls a woken `block_on`
future at least every `event_interval` (61) tasks, but runs spawned tasks strictly in FIFO order. The first request a connection hands to
the bridge wakes the loop (`src/bridge.rs`, `Shared::submit`), and the loop task then waits behind every connection task already queued:
with 1024 ready connections each of them parses a request, takes an admission permit (`src/lib.rs`, `handle`: `state.capacity.try_acquire_owned()`)
and submits it before the loop starts any. Everything past the 128th is answered 503, the shed connections send again at once, and the cycle
repeats. The permit's lifetime did not change (taken in `handle`, released when the Python task completes, on main and the branch alike), so
main was not undercounting: main's loop really never had more than about 61 requests waiting, because Tokio polled it every 61 tasks.

**Evidence.** Quick 5 s runs (server on CPU 0, oha on CPUs 1-3, `/json`, 1024 connections, Rust loop) on the per-item venvs: wi-1, wi-3, wi-4,
wi-5 and default-loop 0% 503; **wi-2 34.1%**; integrated 34.0%; main 0%. Sweeping `BESSEMER_MAX_IN_FLIGHT` (3 s runs) brackets the peak
number admitted at once: main sheds 2.0% at 32, 0.4% at 48, 0% at 64 (peak about 48 to 64); the branch sheds 20.7% at 256, 5.7% at 512,
1.7% at 768, 0% at 1024 (peak near the connection count). Reverting only the spawn (loop body back as the `block_on` future) removed the
503s but cost 8% at 64 connections (11.27 against 10.45 us/req in the original quick-run summaries, two runs each), so the spawn stays.

**Fix.** The bridge now hurries a local Rust loop when 32 jobs are queued for it (`HURRY_AT` in `src/bridge.rs`): `LoopCore::hurry` wakes the
`block_on` future, which polls the loop body out of turn (with the body task's waker, so its own wake-ups still go to the task). Tokio polls that future
within 61 tasks, so at most about 32 + 61 requests wait for the loop. With fewer than 32 queued (64 connections and below) nothing changes.
After the fix, `max_in_flight` 64 sheds 4.5% and 96 none (quick runs), so the peak is now between 64 and 96.
`tests/python/test_event_loop.py::BurstAdmissionOnTheRustLoopTests` sends one request on each of 512 connections while the loop's thread is busy:
before the fix **384 of 512 were refused** (512 minus 128: every connection was admitted before the loop ran), after it none (8 of 8 runs), and main's wheel refuses none.

**Re-measured** (same method as the matrix, `raw/after-inflight-fix/`, two runs each in the order json, items, echo at 1024, json at 64, main-rust json at 1024, then again;
`/proc/loadavg` one-minute 0.14 to 1.58). Branch default is `auto`, which resolved to `event_loop=bessemer` in every server log; wheel `/home/user/wheels/fix503`.

| config | route | conns | req/s (200) | us CPU/req (+-) | p50 / p99 / p99.9 ms | non-200 |
|---|---|--:|--:|--:|--:|--:|
| branch-default (fixed) | GET /json | 1024 | 84,913 | **11.68** (+-0.03) | 11.71 / 18.48 / 25.0 | 0 |
| branch-default (fixed) | GET /items/7?q=abc | 1024 | 82,842 | **11.98** (+-0.11) | 12.06 / 18.02 / 25.3 | 0 |
| branch-default (fixed) | POST /echo | 1024 | 68,648 | **14.46** (+-0.04) | 14.61 / 19.01 / 24.7 | 0 |
| branch-default (fixed) | GET /json | 64 | 94,398 | **10.58** (+-0.15) | 0.67 / 1.12 / 1.9 | 0 |
| main-rust (same session) | GET /json | 1024 | 78,211 | **12.70** (+-0.03) | 12.78 / 18.55 / 29.9 | 0 |

Against the matrix above at 1024 connections: /json 43,982 to 84,913 200 responses/s and 34.0% to 0 refused; /items 42,112 to 82,842; /echo 37,212 to 68,648.
The branch is now cheaper than main's Rust loop at 1024 connections (/json 11.68 against 12.70 us/req in this session; matrix main-rust 12.93, /items 13.77, /echo 18.69)
and close to its own `max_in_flight` 4096 figures (11.35, 11.82, 14.59). /json at 64 connections is unchanged (10.58 against 10.52). io_uring (one quick 4 s run each): 29.5% refused before and 0% after.

### Limits below Tokio's cadence: requests wait for the loop instead of being refused

**Why hurrying was not enough.** Tracing the burst test (512 connections, loop thread busy for 0.5 s, then one request on each) at
`max_in_flight` 40, where the threshold is 1, showed the hurry working as designed: the first job hurries, the `block_on` future is polled
after exactly 61 connection tasks, and the loop takes the batch. But those 61 tasks are 40 admissions and 21 refusals: a current-thread
runtime checks its `block_on` future only between batches of `event_interval` tasks, and each connection task parses, takes a permit
and submits in one poll, so however early the loop is hurried up to 61 requests can be admitted between two of its turns. That is also main's limit. Measured
here, main's wheel (`/home/user/venv`, the loop body as the `block_on` future) refuses exactly what the previous branch wheel does in the
burst: 232 of 512 at 32, 168 at 40, 104 at 48, 40 at 56, 0 at 64 and 128 (eight or nine windows of 61, each refusing 61 minus the limit;
the same in each of three runs at 40 and 56). The earlier statement that main refuses none applies at 64 and above only.

**Fix.** A request that finds no permit, on a thread whose Rust loop has jobs it has not taken yet, now hurries the loop, yields
(`tokio::task::yield_now`, which on a current-thread runtime defers the task until after the next driver poll, so behind the loop's
out-of-turn poll) and tries again, up to 32 times (`admit_after_local_loop` in `src/lib.rs`, `Lanes::hurry_local_backlog` in
`src/bridge.rs`). The permits it lacks are then held by requests the loop cannot start until the connection tasks yield, so the wait is
short; when they are held by requests the loop has already started (a genuinely full server), or no Rust loop runs on that thread, the
request is refused at once as before. Only the refusal path changed; an admitted request takes the same code as before. In the burst at
`max_in_flight` 16 on 1000 connections no request needed more than seven yields.

**Burst test** (`BurstAdmissionOnTheRustLoopTests`, now at 32, 48, 64 and 128; refused of 512):

| wheel | 32 | 48 | 64 | 128 |
|---|--:|--:|--:|--:|
| before the hurry (the integrated wheel benchmarked above) | 480 | 464 | 448 | 384 |
| hurry at 32 (`232cc54`, `/home/user/venvs/fix503`) | 232 | 104 | 0 | 0 |
| hurry scaled with the limit (`53eeaa9`) | 232 | 104 | 0 | 0 |
| main (`/home/user/venv`) | 232 | 104 | 0 | 0 |
| this fix (`/home/user/wheels/burst`) | **0** | **0** | **0** | **0** |

The four tests passed in 5 of 5 runs with this fix.

**Sustained load**, `/json` at 1024 connections, oha on CPUs 1-3 for 6 s after a 2 s warm-up, server on CPU 0, one run each:

| `max_in_flight` | main: 200/s, non-200, p99 | previous (`53eeaa9`) | this fix |
|--:|--:|--:|--:|
| 32 | 63.4k, 5.7%, 33.2 ms | 63.9k, 6.0%, 31.7 ms | 78.2k, **0%**, 18.5 ms |
| 64 | 75.3k, 0%, 18.3 ms | 82.2k, 0%, 19.8 ms | 79.9k, 0%, 17.0 ms |
| 128 | 77.5k, 0%, 18.8 ms | 83.5k, 0%, 19.6 ms | 81.9k, 0%, 25.7 ms |

**64 connections, `/json`, default settings** (`benchmarks/profile/profile_route.sh`, `BESSEMER_EVENT_LOOP=bessemer`, alternating runs):
previous wheel 11.50, 10.87, 11.45 us/req; this fix 11.16, 10.92, 11.22 (medians 11.45 and 11.16, within the run-to-run spread);
no request was refused in any run. The CPU gain of the spawned loop body at 64 connections is kept.
