# Where Bessemer's CPU per request goes, against Axum (2026-11-06, `main` 7475503)

All figures are µs of server CPU per request on one pinned core (CPU 0), unless stated. Sources: the reprofile matrix and its supplements in this directory (`README.md`, `raw/`), the `perf` profiles in `raw/perf/`, py-spy in `raw/pyspy/`, callgrind in `raw/callgrind/`, plus three small checks run for this analysis (syscall counts with `strace -c`, and a user/system CPU split at one connection; see "Extra measurements").

## Summary

| `/json`, µs CPU/request | 1 conn | 64 conn, saturated | 1024 conn | p99 at 1024 (ms) |
|---|--:|--:|--:|--:|
| Axum, 1 worker thread | 19.63 | 11.62 | 13.26 | 169.0 |
| Bessemer, Rust loop (epoll) | 29.83 (+52%) | 12.23 (+5%) | 13.55 (+2%) | 20.8 |
| Bessemer, Rust loop + io_uring | 31.41 (+60%) | 10.19 (−12%) | 10.35 (−22%) | 16.3 |
| Bessemer, asyncio loop (the default) | 44.80 (+128%) | 13.72 (+18%) | 23.84 (+80%)¹ | 52.2¹ |

¹ `BESSEMER_MAX_IN_FLIGHT=4096`. With the default of 128 the asyncio loop answers 14–16% of requests with 503 at 1024 connections (24.61 µs, p99 63.7 ms).

1. **With the server saturated, the Rust loop is already within 2–10% of Axum, and io_uring beats it.** On the Rust loop the remaining gap is entirely in user space. Bessemer's kernel time is lower than Axum's (6.1 against 7.7 µs on `/json`), but its user-space time is higher (6.2 against 4.0 µs).
2. **The default configuration is where the gap is.** The asyncio loop costs 1.5–2.6 µs/request more than the Rust loop at 64 connections, and 10–14 µs more at 1024 connections. The extra cost comes from the two-thread layout: the cross-thread wake-ups add about 0.5 `sendto`, 0.5 `recvfrom` and 0.5 `epoll_wait` per request, and at 1024 connections about 0.5 context switches per request on a shared core. Closing that gap means changing a default, so it is for the owner (see "Default changes").
3. **The one-connection (latency) gap is user-space CPU, not syscalls.** The Rust loop uses 13.8 µs user and 14.7 µs system per request; Axum uses 6.0 µs user and 13.8 µs system. Per request the loop makes 2 `epoll_wait` calls to Axum's 1. The rest of the difference is a large instruction footprint (37k user instructions/request), which runs cold after every idle period.
4. **Of the ~6.2 µs of user space on the Rust loop at 64 connections, about 4.45 µs is the Rust HTTP stack** (parse, header map, Tokio, timers and clock reads, futures, response write). That is already more than Axum's entire user space (4.0 µs). About 1.2 µs is per-request Python objects that Bessemer creates (Request, Context, Task, Call). Only 0.17 µs is the handler itself. No Bessemer Python bytecode runs on these routes.

## Method

**Totals.** Totals at 64 connections are the medians of supplement A (`raw/supp-oha-cpus1-3-at64-tables.md`), where oha has three cores, the server is at 1.00 CPU and no cell is client-limited. I did not use the 64-connection matrix cells with oha on one core for gaps. Axum and the faster Bessemer cells there are client-limited and cost 1–3.6 µs less per request than when saturated (axum 8.03 against 11.62). Both input reviews used those cells as their baseline (see "Corrections to the inputs"). Totals at 1024 connections are the matrix medians (oha on CPUs 1-3, not client-limited).

**Kernel against user.** The split comes from the per-thread DSO shares of the step-3 `perf record` runs (one run each, `cpu-clock` at 4999 Hz, 64 connections, oha on CPUs 1-3). Kernel time includes the libc syscall wrappers. On this VM there is no PMU, and about 16% of samples land on `_raw_spin_unlock_irqrestore`. That is skid of kernel work done with interrupts off, so it is counted as kernel, not as a lock.

**User space by category.** Each user-space perf symbol is mapped to a category. Generic leaves (memcpy, mimalloc, `_PyObject_Malloc/Free`, `dict_dealloc`, refcounting and so on) are charged to their callers in proportion to the call-edge costs in the callgrind profile of the same loop and route. A Python dict freed by Bessemer's Request teardown therefore counts as orchestration, not "CPython". The categories:

| category | what is in it |
|---|---|
| kernel | `[kernel.kallsyms]` plus libc syscall wrappers |
| Rust HTTP parse/route/serialise | `_native` HTTP/1 (`http1`, `transport`, `router`, `controls`, `budget`, `telemetry`, `handle` in `lib.rs`), Tokio (I/O driver, current-thread scheduler, timers, sync), `http`/`httparse`, vDSO `clock_gettime`, the share of memmove/malloc charged to them, and `bessemer_codec` (JSON encode; on `/echo` also the body parse and model build, including the CPython objects it creates) |
| pyo3 crossing + GIL | pyo3 trampolines, `AttachGuard`, `take_gil`/`drop_gil`, `PyThreadState_Attach/Detach`, `PyGILState_*` |
| Python handler body | CPython eval and generator frames (`_PyEval_EvalFrameDefault`, `gen_send_ex`, …) on the Rust loop, where the handler is the only bytecode. On asyncio the same 0.17 µs is assumed, and the rest of the bytecode is put under asyncio |
| Bessemer orchestration | `_native::invoker`/`bridge`/`conversion`, the C `_asyncio` Task (`Task.__init__`, eager start, `swap_current_task`, dealloc), `contextvars` (Context, HAMT, Token), Request creation and teardown, and pyo3's deferred decref pool. There is **no Bessemer Python bytecode** on these three routes: py-spy shows no Bessemer frame above 0.1%, and the Python review confirms it by reading `_native_routes`. This category is the Python *objects* that Bessemer's Rust code builds per request |
| asyncio / loop | the Rust loop: `bessemer_loop` core. asyncio: Python bytecode of `BaseEventLoop._run_once`, `Handle._run`, `get_debug`, `is_running`, plus the `select` module and the multi-thread Tokio scheduler of the two-worker I/O runtime |

**Rows without a profile:**
- `/items`: no perf profile. Its split is the `/json` split plus the measured difference in total (+0.81 µs on the Rust loop), spread in the proportions of the callgrind instruction difference between `/items` and `/json`. That difference is 63% Rust HTTP (path and query parsing, `HashMap`, `serde_json::Value`), 17% orchestration, 8% pyo3 and 4% handler.
- io_uring: no perf profile. User space is assumed equal to the epoll Rust loop, because the HTTP, Python and codec code is shared. The kernel figure is therefore "total minus that". It also absorbs any difference in the io_uring driver's own user-space cost.
- Axum on `/items` and `/echo`: the kernel time is taken equal to Axum `/json` (7.65 µs), and the rest is user space.

**Uncertainty:**
- Totals: ±7% run to run (two runs per cell; the two runs of every 64-connection supplement cell agree within 2.5%).
- Category splits: each is one perf run of about 4 s (about 20k samples, so ±0.1 µs of sampling error), plus up to about ±0.3 µs from the mapping. The kernel/user boundary is the least certain, at about ±0.5 µs. Example: the Rust loop's kernel time is 6.1 µs on `/json` but 5.6 µs on `/echo`, although both make the same syscalls.
- The 1024-connection rows are not profiled. Only the increase over 64 connections is shown, and it is not split.

## CPU per request by category

### GET /json, 64 connections, server saturated

| configuration | total | kernel | Rust HTTP parse/route/serialise | pyo3 crossing + GIL | Python handler body | Bessemer orchestration | asyncio / loop | gap to Axum |
|---|--:|--:|--:|--:|--:|--:|--:|--:|
| Axum (1 thread) | **11.62** | 7.65 | 3.97 | – | – | – | – | |
| Rust loop (epoll) | **12.23** | 6.08 | 4.45 | 0.22 | 0.17 | 1.18 | 0.12 | +0.61 (+5%) |
| Rust loop + io_uring | **10.19** | 4.04 | 4.45 | 0.22 | 0.17 | 1.18 | 0.12 | −1.43 (−12%) |
| asyncio loop (default) | **13.72** | 7.12 | 4.34 | 0.16 | 0.17 | 1.01 | 0.91 | +2.10 (+18%) |

### GET /items/7?q=abc, 64 connections, server saturated

| configuration | total | kernel | Rust HTTP parse/route/serialise | pyo3 crossing + GIL | Python handler body | Bessemer orchestration | asyncio / loop | gap to Axum |
|---|--:|--:|--:|--:|--:|--:|--:|--:|
| Axum (1 thread) | **11.84** | 7.65 | 4.19 | – | – | – | – | |
| Rust loop (epoll) | **13.04** | 6.14 | 4.96 | 0.29 | 0.20 | 1.31 | 0.14 | +1.20 (+10%) |
| Rust loop + io_uring | **10.88** | 3.98 | 4.96 | 0.29 | 0.20 | 1.31 | 0.14 | −0.96 (−8%) |
| asyncio loop (default) | **14.51** | 7.17 | 4.84 | 0.22 | 0.20 | 1.14 | 0.93 | +2.67 (+23%) |

### POST /echo (185-byte Order), 64 connections, server saturated

| configuration | total | kernel | Rust HTTP parse/route/serialise | pyo3 crossing + GIL | Python handler body | Bessemer orchestration | asyncio / loop | gap to Axum |
|---|--:|--:|--:|--:|--:|--:|--:|--:|
| Axum (1 thread) | **15.60** | 7.65 | 7.95 | – | – | – | – | |
| Rust loop (epoll) | **16.49** | 5.58 | 8.37² | 0.55 | 0.17 | 1.53 | 0.28 | +0.89 (+6%) |
| Rust loop + io_uring | **16.19** | 5.28 | 8.37² | 0.55 | 0.17 | 1.53 | 0.28 | +0.59 (+4%) |
| asyncio loop (default) | **18.17** | 6.56 | 8.43² | 0.46 | 0.17 | 1.42 | 1.13 | +2.57 (+16%) |

² Of which `bessemer_codec` (body parse into the model, including the CPython objects it builds, and response encode) is 2.33 µs, and the HTTP layer is 6.0 µs. The HTTP layer on `/echo` is 1.5 µs more than on `/json`:
- the second JSON scan (`validate_json`, 6.0k instructions);
- the body channel and the extra Tokio wake (Tokio scheduler/sync goes from 0.60 to 1.08 µs);
- three `mime` parses of the same content type (in `transport.rs`, `forms.rs` and `lib.rs`).

### 1024 connections (oha on CPUs 1-3; not profiled, so only the increase over 64 connections is shown)

| route | configuration | µs CPU/req | increase over 64 conn | gap to Axum | p50 ms | p99 ms | p99.9 ms |
|---|---|--:|--:|--:|--:|--:|--:|
| `/json` | Axum | **13.26** | +1.64 | | 2.82 | 169.0 | 174.9 |
| | Rust loop | **13.55** | +1.32 | +0.29 (+2%) | 13.40 | 20.8 | 27.7 |
| | Rust loop + io_uring | **10.35** | +0.16 | −2.91 (−22%) | 10.46 | 16.3 | 26.8 |
| | asyncio, max_in_flight 4096 | **23.84** | +10.12 | +10.58 (+80%) | 26.65 | 52.2 | 65.0 |
| | asyncio, default (15% 503) | 24.61 | | | 27.96 | 63.7 | 75.7 |
| `/items` | Axum | **14.84** | +3.00 | | 3.18 | 170.9 | 179.2 |
| | Rust loop | **14.00** | +0.96 | −0.84 (−6%) | 14.05 | 21.5 | 28.4 |
| | Rust loop + io_uring | **11.27** | +0.39 | −3.57 (−24%) | 11.45 | 16.3 | 21.2 |
| | asyncio, max_in_flight 4096 | **28.82** | +14.31 | +13.98 (+94%) | 32.35 | 63.2 | 78.9 |
| | asyncio, default (14% 503) | 28.29 | | | 32.19 | 74.1 | 95.8 |
| `/echo` | Axum | **20.92** | +5.32 | | 4.47 | 174.7 | 188.7 |
| | Rust loop | **18.72** | +2.23 | −2.20 (−11%) | 18.74 | 26.1 | 38.7 |
| | Rust loop + io_uring | **15.42** | −0.77 | −5.50 (−26%) | 15.75 | 24.7 | 36.2 |
| | asyncio, max_in_flight 4096 | **29.38** | +11.21 | +8.46 (+40%) | 33.56 | 72.5 | 107.5 |
| | asyncio, default (16% 503) | 28.18 | | | 14.76 | 107.3 | 131.4 |

- At 1024 connections Axum's p99 (169–175 ms) is 7–10 times Bessemer's Rust loop (21–26 ms), although its p50 is lower. Axum ran one worker thread here, and its multi-thread scheduler serves connections unevenly. This is a property of this Axum configuration, not a Bessemer advantage that would hold against Axum with more threads.
- The asyncio increase at 1024 (+10 to +14 µs) is not profiled. The likely cause: three threads (the Python loop thread and two Tokio workers) share one pinned core, at 0.46–0.58 context switches per request, against 0.001 on the Rust loop. Treat that as a hypothesis.

### One connection (latency path)

| `/json`, 1 conn | µs CPU/req | user µs/req³ | system µs/req³ | ctx sw/req | `epoll_wait`/req⁴ | p50 ms | p99 ms |
|---|--:|--:|--:|--:|--:|--:|--:|
| Axum | 19.63 | 6.0 | 13.8 | 1.0 | 1.0 | 0.041 | 0.08 |
| Rust loop | 29.83 | 13.8 | 14.7 | 1.0 | 2.0 | 0.050 | 0.10 |
| Rust loop + io_uring | 31.41 | – | – | 1.0 | – | 0.052 | 0.10 |
| asyncio | 44.80 | – | – | 4.4 | – | 0.061 | 0.12 |

³ From `/proc/<pid>/stat` utime/stime over 10 s (10 ms ticks, about 180k requests, one run). ⁴ `strace -c` over 10,000 requests.

The 10 µs gap at one connection is about 8 µs of user space and 1 µs of kernel. The Rust loop executes 37k user instructions per request (callgrind), against 13.8 µs of user time. That is about 0.37 ns per instruction, against about 0.2 ns at 64 connections, which is consistent with cold caches after every idle period. Fewer instructions help here more than anywhere else. So does removing the second `epoll_wait` and the GIL detach/attach per loop iteration.

## User-space instructions per request (callgrind, 1 connection)

These are exact counts that repeat to about 0.1%, but they are taken at one connection, so per-iteration loop costs are not amortised. Generic leaves are charged to their callers as above. Scripts: `scripts/cgparse.py`, `scripts/classify.py`.

| loop / route | total | Rust HTTP, Tokio, timers | codec (parse/model/encode) | pyo3 + GIL | Bessemer orchestration | Python bytecode | loop core / asyncio runtime | syscall wrappers + other |
|---|--:|--:|--:|--:|--:|--:|--:|--:|
| Rust loop `/json` | 37,074 | 20,696 | 1,701 | 2,497 | 8,393 | 1,541 | 1,818 | 427 |
| Rust loop `/items` | 41,973 | 23,700 | 1,759 | 2,906 | 9,203 | 1,726 | 1,937 | 743 |
| Rust loop `/echo` | 79,021 | 30,124 | 27,582 | 4,938 | 10,761 | 1,216 | 1,978 | 2,420 |
| asyncio `/json` | 52,135 | 19,233 | 1,700 | 2,220 | 8,172 | 16,011 | 3,979 | 819 |
| asyncio `/items` | 57,041 | 22,314 | 1,758 | 2,544 | 8,942 | 16,234 | 3,982 | 1,266 |
| asyncio `/echo` | 93,683 | 28,081 | 27,617 | 4,645 | 10,622 | 15,539 | 4,060 | 3,120 |

The largest items inside the Rust HTTP bucket on Rust loop `/json` (instructions per request):

| item | instructions | notes |
|---|--:|---|
| request head parse and `HeaderMap` build | 5,385 | `http1::build`, `httparse`, `try_append2`, `hash_elem_using`, `get_all` |
| `handle` (`lib.rs`) | 2,927 | |
| timers and clock reads | 2,187 | `Timespec::now`/`sub_timespec`, `clock_gettime`, Tokio timer wheel, `Sleep::poll`. In perf at 64 connections, vDSO `clock_gettime` alone is 2.5% of all CPU (0.30 µs), against 0.5% for Axum |
| Tokio scheduler and sync | 2,162 | |
| serve loop | 1,933 | |
| `memcpy` of futures and buffers | about 2,000 | counted inside the buckets above. Callers: `start_all`, `http1::serve`, `handle`, `Dispatcher::call`, `ProtocolChecked::call` |
| response write | 1,196 | |
| router | 1,187 | |
| transport checks | 860 | |
| controls | 775 | |

On `/echo`:
- the extra scan `validate_json` is 6,040 instructions (7.6% of the request);
- `from_utf8` inside the codec is 2,760;
- the three `mime` parses are 960.

## Multi-core (2 workers or threads on CPUs 0-1, oha on CPUs 2-3, `/json`, 1024 connections)

| server | req/s | server CPUs | µs CPU/req | p99 ms | p99.9 ms | non-200 |
|---|--:|--:|--:|--:|--:|--:|
| Axum, 2 threads | 204,758 | 1.84 | 9.00 | 10.5 | 21.7 | 0 |
| Bessemer Rust loop, `--workers 2` | 172,336 | 1.96 | 11.38 | 12.0 | 22.6 | 0 |
| Bessemer asyncio (default), `--workers 2` | 114,873 | 1.96 | 17.08 | 36.3 | 581.7 | 3–4% |
| Bessemer asyncio, max_in_flight 4096 | 149,595 | 1.96 | 13.13 | 16.7 | 33.0 | 0 |

- Axum and the Rust loop are both client-limited here (oha at 0.95–1.05 of its two cores), so their req/s are lower bounds.
- Axum used 1.84 of 2 CPUs, so its 9.00 µs is an unsaturated figure, like the 8.03 µs in the one-core matrix. It is not comparable with Bessemer's 11.38.
- Per process, the Rust loop scales from 1 to 2 workers with no loss of CPU per request (13.55 → 11.38 µs, with the client limiting).
- The default asyncio layout loses 3–4% of requests to 503 and has a p99.9 above 0.5 s.

## Extra measurements made for this analysis

The `strace -c` counts are in `raw/strace-syscall-counts.txt`. Each count is one run, with the server pinned to CPU 0.

| | `sendto`/`writev` | `recvfrom` | `epoll_wait` | per request at |
|---|--:|--:|--:|---|
| Rust loop | 1.00 | 1.00 | 2.01 | 1 conn |
| Axum | 1.00 (`writev`) | 1.00 | 1.00 | 1 conn |
| Rust loop | 1.00 | 1.00 | 0.036 | 64 conn |
| asyncio | 1.47 | 1.48 | 0.51 | 64 conn |
| Axum | 1.00 (`writev`) | 1.00 | 0.017 | 64 conn |

At 64 connections the Rust loop's syscalls are already minimal. The asyncio layout's extra ~0.5 send, recv and `epoll_wait` per request are the bridge wake-ups between the Tokio threads and the Python thread. The bridge already coalesces them (`bridge.rs` `submit`, `was_empty`), so the batches average about 2 jobs.

## Corrections to the inputs

**Benchmark report (Sonnet).** The numbers check out against `raw/*-all-runs.csv`; I spot-checked every `/json` cell and every supplement A cell. Two caveats:
1. The 64-connection rows of the main matrix should not be used for gaps, because Axum there is client-limited and unsaturated. The report says so, but its first tables still lead with them.
2. "CPU/request rises when oha has three cores" is unexplained for every server. It is the reason the saturated column is the right one, but it means that absolute µs/request depend on how hard the client pushes. Compare servers only within one client setup.

**Rust-path review (Opus):**
- "The syscalls are already at the minimum." True at 64 connections. At 1 connection the Rust loop makes **2** `epoll_wait` calls per request to Axum's 1.
- "Against roughly 9.4 µs per request at 64 connections (Linux, 4b)": that is an old phase-4b figure. The current saturated figure is 12.23 µs, of which 6.1 µs is kernel.
- "with io_uring, 2 workers reached 172k": wrong. The 172k is the epoll Rust loop (step 6 never ran io_uring with two workers).
- On (d), the "lock-step" effect: on Linux the Rust loop's single thread runs at 0.99–1.00 CPU whenever the client is not the limit (supplement A, `raw/thread-split-64c-*`). This is a pure single-thread ceiling, and the macOS lock-step effect does not reproduce here. Capping jobs per drain is not justified by this data.
- Item 12, the double JSON scan, is underestimated. At 0.05–0.15 µs it is 6.0k instructions, about 0.4–0.8 µs at the IPC measured on `/echo`. It is the largest single item on `/echo`.
- Item 6, clock reads, is supported: vDSO `clock_gettime` is 2.5% of all CPU on the Rust loop, against 0.5% on Axum.
- Item 8, future boxing, is supported by about 2k `memcpy` instructions per request, whose callers are the boxed-future layers.

**Python-path review (Opus):**
- Its baselines (Rust loop 10.0, asyncio 11.4, Axum 8.0 µs) are the client-limited cells; use 12.23, 13.72 and 11.62.
- Its claim that no Bessemer Python bytecode runs on these routes is confirmed by py-spy and callgrind. On the Rust loop all Python bytecode is 1.5k instructions per request. On asyncio it is 16k, almost all of it asyncio's own `_run_once` machinery.
- Its "pure handler inline" item (B3) only helps handlers that call nothing. It is correct and safe as designed, but it is a benchmark-shaped win: real handlers call functions, so `co_names` is not empty. I rank it last, as an optional follow-on.

## What would close the gap

The ranked plan with full briefs is in the structured output of this analysis. In short:
- Five independent items (invoker trims, single-pass request bodies, clock consolidation, head-parse and protocol checks, the response fast path) remove an estimated 1.2–2.0 µs per request in user space on every route.
- That would take the Rust loop to about 10.5–11 µs on `/json`, below Axum's 11.6, and io_uring to about 8.5–9 µs.
- It would take the asyncio default to about 12–12.5 µs. The asyncio layout itself (about 1.5–2.6 µs at 64 connections, about 10 µs at 1024) only goes away by changing the default event loop, which is the owner's decision.
