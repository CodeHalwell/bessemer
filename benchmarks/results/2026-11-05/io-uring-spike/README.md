# io_uring spike (Phase 5)

**Question:** how much CPU per request does io_uring save over epoll for a
keep-alive HTTP/1 responder, and can Bessemer's deployments use it at all?

**Answer:** 0.7-0.9 µs per request (11-15%) once the server core is busy,
and little at light load. Docker's and containerd's default seccomp
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
requests the server answered in that same window (its own `GET /stats`
counter, read at both ends; for Axum, oha's average rate). Runs with any
failed request are rejected. `perf` is not available in this container, so there
is no kernel symbol breakdown. Reproduce with
`AXUM=... crates/uring-spike/measure.sh raw.tsv`; every response was a 200.

## Results

Medians of three rounds ([raw.tsv](raw.tsv)). µs is CPU per request; user/sys
split from `/proc/PID/stat`.

Server saturated (oha on CPUs 1-3, server at a full CPU):

| Connections | Backend | req/s | µs/req | user | sys | p50 µs | p99 µs |
| ---: | --- | ---: | ---: | ---: | ---: | ---: | ---: |
| 64 | tokio | 156.8k | 6.37 | 0.49 | 5.94 | 404 | 617 |
| 64 | mio | 154.1k | 6.40 | 0.36 | 6.04 | 412 | 637 |
| 64 | uring | **173.4k** | **5.70** | 0.13 | 5.57 | 364 | 586 |
| 64 | uring-plain | 173.9k | 5.71 | 0.09 | 5.61 | 363 | 606 |
| 64 | axum | 85.6k | 11.63 | 3.09 | 8.46 | 759 | 1034 |
| 1024 | tokio | 169.9k | 5.82 | 0.65 | 5.24 | 6034 | 7230 |
| 1024 | mio | 172.9k | 5.78 | 0.40 | 5.32 | 5843 | 9976 |
| 1024 | uring | **200.8k** | **4.90** | 0.13 | 4.77 | 5048 | 7982 |
| 1024 | uring-plain | 203.1k | 4.80 | 0.14 | 4.66 | 4869 | 8832 |
| 1024 | axum | 69.8k | 14.72 | 4.21 | 10.49 | 3074 | 172668 |

Client-bound (oha on CPU 3 only, server about half busy):

| Connections | Backend | req/s | server CPUs | µs/req | user | sys |
| ---: | --- | ---: | ---: | ---: | ---: | ---: |
| 64 | tokio | 110.7k | 0.53 | 4.80 | 0.62 | 4.18 |
| 64 | mio | 111.7k | 0.51 | 4.63 | 0.35 | 4.27 |
| 64 | uring | 110.7k | 0.51 | 4.53 | 0.29 | 4.25 |
| 64 | uring-plain | 61.4k | 0.49 | 8.00 | 0.33 | 7.67 |
| 64 | axum | 127.8k | 0.85 | 6.61 | 2.50 | 4.17 |
| 1024 | tokio | 107.9k | 0.54 | 4.89 | 0.63 | 4.27 |
| 1024 | mio | 104.7k | 0.52 | 4.90 | 0.48 | 4.38 |
| 1024 | uring | 104.2k | 0.49 | 4.63 | 0.29 | 4.33 |
| 1024 | uring-plain | 53.9k | 0.49 | 9.04 | 0.36 | 8.69 |
| 1024 | axum | 112.1k | 0.83 | 7.31 | 2.86 | 4.59 |

## Reading

- **The saving is real and in line with the design estimate.** With the
  server core full, io_uring costs 0.70 µs less per request than epoll at 64
  connections and 0.88 µs less at 1024, and serves 13% and 16% more requests
  on one core. The design doc predicted 0.5 to 0.8 µs.
- **It mostly shows under load.** With the server half idle io_uring is
  only 0.1-0.3 µs ahead of epoll, close to the noise between rounds. The
  benefit comes from amortising syscalls over bigger batches, which only
  happen when completions pile up.
- **DEFER_TASKRUN is not optional.** Without `SINGLE_ISSUER | DEFER_TASKRUN`
  the ring matches the default when saturated but costs 8-9 µs per request and
  serves half the requests at light load, because completions are run as task
  work interrupting the thread rather than in batches when it asks for them.
  An integration must use the flags (Linux 6.1+) or not use io_uring.
- **The kernel is most of the cost of serving a request.** Even the lean
  responders spend 4-6 µs per request in the kernel against 0.1-0.7 µs in user
  space. Bessemer is at 12.8 µs today, so io_uring is worth about 5% now; it
  becomes worth 10-15% once Phases 1-4 have removed the user-space overhead.
- **tokio vs a hand-rolled loop** is worth up to 0.3 µs, within noise when
  saturated; io_uring saves a further 0.7-0.9 µs on top of the epoll loop.
- **Axum as a reference.** Client-bound, Axum measures 6.6 µs at 64
  connections, matching the 6.6 µs baseline taken with perf, which supports
  the measurement method. Saturated, Axum's cost per request rises to
  12-15 µs, with a 173 ms p99 at 1024 connections. That was not investigated;
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
   the extra 13-16% per core.
4. Do it after Phases 1-4. Before them, under 1 µs of 12.8 µs is not the best use
   of effort.
