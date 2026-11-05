# io_uring spike (Phase 5)

**Question:** how much CPU per request does io_uring save over epoll for a
keep-alive HTTP/1 responder, and can Bessemer's deployments use it at all?

**Answer:** about 0.7 µs per request (11-13%) once the server core is busy,
and nothing at light load. Docker's and containerd's default seccomp
profiles block io_uring, so an io_uring backend can only ever be an optional
fast path with epoll as the default. **Go, but as Phase 5 proper after the
Rust event loop (Phases 3/4) lands, not before.**

## Setup

[`crates/uring-spike`](../../../../crates/uring-spike) is a standalone crate
(not in the workspace, not wired into the server). It answers `GET /json`
with the same bytes on three backends, sharing the parser and response code
(`httparse`, cached `date` header) so only the I/O path differs:

- `tokio`: current_thread runtime, a task per connection (the shape of the
  server's I/O today).
- `mio`: a hand-rolled edge-triggered epoll loop, the leanest readiness design.
- `uring`: multishot accept straight into the fixed (registered) file table,
  multishot recv from a 4096 x 2 KiB provided-buffer ring, all sends of a loop
  iteration submitted in one `io_uring_enter`, ring built with
  `SINGLE_ISSUER | DEFER_TASKRUN`. `uring-plain` is the same without those
  two flags.
- `axum`: [`benchmarks/compare/axum_app`](../../../compare/axum_app) with one
  worker thread, as a reference point.

Host: 4 vCPU Intel Xeon VM, Linux 6.18, loopback. The server is pinned to
CPU 0. `oha` runs on CPU 3 alone, or on CPUs 1-3 to push the server to a full
core. Each run is a 2 s warm-up then 10 s of load, three rounds interleaved.
CPU per request is the server's on-CPU time over a 5 s window (summed
`/proc/PID/task/*/schedstat`, what perf's task-clock counts) divided by the
requests in that window. `perf` is not available in this container, so there
is no kernel symbol breakdown. Reproduce with
`AXUM=... crates/uring-spike/measure.sh raw.tsv`; every response was a 200.

## Results

Medians of three rounds ([raw.tsv](raw.tsv)). µs is CPU per request; user/sys
split from `/proc/PID/stat`.

Server saturated (oha on CPUs 1-3, server at 1.00 CPU):

| Connections | Backend | req/s | µs/req | user | sys | p50 µs | p99 µs |
| ---: | --- | ---: | ---: | ---: | ---: | ---: | ---: |
| 64 | tokio | 154.7k | 6.45 | 0.50 | 5.94 | 405 | 604 |
| 64 | mio | 156.1k | 6.35 | 0.40 | 5.95 | 404 | 651 |
| 64 | uring | **176.7k** | **5.64** | 0.12 | 5.51 | 357 | 593 |
| 64 | uring-plain | 168.3k | 5.88 | 0.12 | 5.77 | 378 | 597 |
| 64 | axum | 84.8k | 11.79 | 3.28 | 8.54 | 765 | 1069 |
| 1024 | tokio | 163.7k | 6.12 | 0.72 | 5.35 | 6276 | 8243 |
| 1024 | mio | 180.7k | 5.52 | 0.43 | 5.15 | 5642 | 9072 |
| 1024 | uring | **208.0k** | **4.78** | 0.14 | 4.63 | 4855 | 8027 |
| 1024 | uring-plain | 217.3k | 4.46 | 0.11 | 4.35 | 4588 | 7201 |
| 1024 | axum | 69.2k | 14.45 | 4.18 | 10.30 | 3092 | 171246 |

Client-bound (oha on CPU 3 only, server about half busy):

| Connections | Backend | req/s | server CPUs | µs/req | user | sys |
| ---: | --- | ---: | ---: | ---: | ---: | ---: |
| 64 | tokio | 106.8k | 0.55 | 5.11 | 0.63 | 4.37 |
| 64 | mio | 111.1k | 0.53 | 4.74 | 0.43 | 4.42 |
| 64 | uring | 109.3k | 0.53 | 4.83 | 0.26 | 4.57 |
| 64 | uring-plain | 62.3k | 0.50 | 8.01 | 0.29 | 7.65 |
| 64 | axum | 125.6k | 0.82 | 6.54 | 2.45 | 4.09 |
| 1024 | tokio | 110.9k | 0.53 | 4.80 | 0.61 | 4.20 |
| 1024 | mio | 112.4k | 0.51 | 4.67 | 0.47 | 4.24 |
| 1024 | uring | 114.1k | 0.49 | 4.25 | 0.24 | 4.03 |
| 1024 | uring-plain | 57.3k | 0.50 | 8.73 | 0.24 | 8.48 |
| 1024 | axum | 115.4k | 0.83 | 7.24 | 2.85 | 4.55 |

## Reading

- **The saving is real and in line with the design estimate.** With the
  server core full, io_uring costs 0.71 µs less per request than epoll at 64
  connections and 0.74 µs less at 1024, and serves 13% and 15% more requests
  on one core. The design doc predicted 0.5 to 0.8 µs.
- **It only shows under load.** With the server half idle the backends are
  within noise of each other (except at 1024 connections, about 0.4 µs). The
  benefit comes from amortising syscalls over bigger batches, which only
  happen when completions pile up.
- **DEFER_TASKRUN is not optional.** Without `SINGLE_ISSUER | DEFER_TASKRUN`
  the ring is slightly ahead when saturated but costs 8-9 µs per request and
  serves half the requests at light load, because completions are run as task
  work interrupting the thread rather than in batches when it asks for them.
  An integration must use the flags (Linux 6.1+) or not use io_uring.
- **The kernel is most of the cost of serving a request.** Even the lean
  responders spend 4-6 µs per request in the kernel against 0.1-0.7 µs in user
  space. Bessemer is at 12.8 µs today, so io_uring is worth about 5% now; it
  becomes worth 10-15% once Phases 1-4 have removed the user-space overhead.
- **tokio vs a hand-rolled loop** is worth 0.1-0.6 µs; io_uring adds a
  further 0.7 µs on top of the hand-rolled epoll loop.
- **Axum as a reference.** Client-bound, Axum measures 6.5 µs at 64
  connections, matching the 6.6 µs baseline taken with perf, which supports
  the measurement method. Saturated, Axum's cost per request rises to
  12-14 µs, with a 171 ms p99 at 1024 connections. That was not investigated;
  it does not affect the comparison between the spike's backends.

## Can deployments use it?

| Environment | io_uring |
| --- | --- |
| This cloud container (no seccomp filter, `kernel.io_uring_disabled=0`) | available |
| Docker 29.9 default seccomp profile | **blocked** (`io_uring_setup`: EPERM) |
| containerd 2.4 default profile (`ctr run --seccomp`, what Kubernetes `RuntimeDefault` uses) | **blocked** (EPERM) |
| Docker with `--security-opt seccomp=unconfined` | available |

`uring-spike probe` reports this for any environment. The project's own
[`deploy/kubernetes.yaml`](../../../../deploy/kubernetes.yaml) sets
`seccompProfile: RuntimeDefault`, so the reference deployment would run on
the epoll fallback. Using io_uring there needs a custom seccomp profile that
allows `io_uring_setup`, `io_uring_enter` and `io_uring_register`.

## Recommendation

Go, with conditions:

1. Build it as part of the Rust event loop (Phases 3/4), not into today's
   tokio server: the saving depends on owning the loop and batching
   submissions, which tokio does not offer.
2. Feature-detect at start-up (setup with `SINGLE_ISSUER | DEFER_TASKRUN`,
   probe for multishot accept/recv and buffer rings) and fall back to epoll on
   any failure, including EPERM from seccomp. Epoll stays the default path in
   containers and must stay fully tested.
3. Ship a seccomp profile and document the opt-in for deployments that want
   the extra 11-15% per core.
4. Do it after Phases 1-4. Before them, 0.7 µs of 12.8 µs is not the best use
   of effort.
