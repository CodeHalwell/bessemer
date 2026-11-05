# io_uring backend on the real server (Phase 5)

**Question:** with the io_uring backend wired into the single-thread worker
(`event_loop="bessemer"`, `io_backend="io_uring"`), how much CPU per request
does Bessemer save over the same worker on epoll?

**Answer:** on `/json`, 13.8 to 11.1 µs per request at 64 connections and
15.1 to 11.5 µs at 1024 with the server core saturated (−19% and −24%), and
87.1k to 90.0k req/s (−4% CPU) at 64 connections with oha on one core.

## Method

The spike's method ([io-uring-spike](../io-uring-spike/README.md)) applied to
the real server: [`benchmarks/profile/io_backend.sh`](../../../profile/io_backend.sh)
starts `python -m bessemer run bessemer_app:app --workers 1` from
`benchmarks/compare` with `BESSEMER_EVENT_LOOP=bessemer` and
`BESSEMER_IO_BACKEND` set to each backend, pins the worker to CPU 0, and runs
`oha` on CPU 3 alone or on CPUs 1-3. Each run is a 2 s warm-up then 10 s of
load, three rounds with the backends interleaved. CPU per request is the
worker's on-CPU time (summed `/proc/PID/task/*/schedstat`) over the measured
oha run divided by the responses oha received in it; user/sys from
`/proc/PID/stat`. Every response was a 200 and no run was rejected. The
server log confirmed `serving connections through io_uring` for the io_uring
runs.

Host: the same 4 vCPU VM as the spike, Linux 6.18, CPython 3.13, loopback.
Branch `claude/project-thread-uyd7pt` on top of `650d6eb`.

## Results

Medians of three rounds ([raw.tsv](raw.tsv)); µs is CPU per request.

| Connections | oha CPUs | Backend | req/s | server CPUs | µs/req | user | sys | p50 µs | p99 µs |
| ---: | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 64 | 3 | epoll | 87.1k | 0.97 | 11.19 | 6.83 | 4.40 | 680 | 1564 |
| 64 | 3 | io_uring | 90.0k | 0.97 | **10.75** | 6.18 | 4.35 | 629 | 1789 |
| 64 | 1-3 | epoll | 71.6k | 0.99 | 13.78 | 7.55 | 6.23 | 843 | 1728 |
| 64 | 1-3 | io_uring | **89.1k** | 0.99 | **11.11** | 6.22 | 4.88 | 667 | 1445 |
| 1024 | 3 | epoll | 71.3k | 0.97 | 13.40 | 8.34 | 5.01 | 13006 | 26938 |
| 1024 | 3 | io_uring | 81.7k | 0.94 | **11.45** | 7.31 | 4.22 | 11479 | 28540 |
| 1024 | 1-3 | epoll | 65.3k | 0.98 | 15.09 | 8.71 | 6.39 | 14868 | 24864 |
| 1024 | 1-3 | io_uring | **85.3k** | 0.98 | **11.50** | 6.73 | 4.78 | 11712 | 19194 |

## Reading

- **Bigger than the spike, and it grows with load.** The spike saved
  0.7-0.9 µs (11-15%) on a bare responder. Here, saturated, io_uring saves
  2.7 µs at 64 connections and 3.6 µs at 1024, and serves 24% and 31% more
  requests on one core. Both kernel and user time fall: kernel time by
  1.3-1.6 µs (most likely from fewer system calls: one `io_uring_enter` per pump turn instead
  of a `recv`, a `send` and epoll work per request), user time by 1.3-2.0 µs
  (likely no Tokio reactor registration, readiness tracking and waker churn per
  read and write).
- **Unlike the spike, Bessemer's server is never half idle here.** With oha on
  one core the worker still runs at 0.94-0.97 CPUs, because a request costs
  over twice as much as the spike's responder. So the "client-bound" rows are
  close to saturated too; at 64 connections the epoll worker is just less
  loaded in that configuration (11.2 against 13.8 µs), which is why the gap
  there is only 0.4 µs.
- **Epoll gets worse under saturation; io_uring barely does.** Epoll's cost
  rises from 11.2 to 13.8 µs at 64 connections and from 13.4 to 15.1 µs at
  1024 when oha gets three cores, while io_uring stays at 10.8-11.5 µs. Bigger
  batches amortise io_uring's one system call per turn, while epoll pays per
  socket.
- Latency follows throughput: p50 is 8-21% lower with io_uring. p99 is
  noisy on this shared VM in both directions.

These are single-host loopback numbers on a shared VM, like the spike's, and
not a formal M0 measurement.
