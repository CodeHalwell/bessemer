# io_uring with several workers

**Question:** does the io_uring backend scale across workers? The
[current-main comparison](https://github.com/CodeHalwell/bessemer/pull/67)
found that with two io_uring workers one took every connection (606 CPU ticks
against 15), so two workers ran at one worker's speed.

**Cause:** all workers accept from one listening socket that the supervisor
owns. Each worker's multishot `accept` is a request waiting on that socket,
and the kernel gives every new connection to the first waiting request, so
the worker that armed first took them all. A new Rust test reproduces it:
three workers sharing a listener accepted 0, 0 and 300 of 300 connections.

**Fix:** the io_uring listener now accepts when the runtime's reactor reports
the socket readable, as the epoll path does, and keeps io_uring for reads and
writes. Every idle worker is woken and races for each connection, and a busy
worker leaves it to the others. The same test now gives every worker a share.
The multishot accept and its 256-connection queue limit are gone; pending
connections now wait in the kernel's backlog, as on epoll.

**Answer:** with two workers the connections split evenly (each worker
0.92–0.98 of a core) and io_uring is ahead of epoll on both connection counts:
162k to 172k req/s at 64 connections (11.9 to 11.1 µs per request) and 156k
to 169k at 1024 (12.4 to 11.2 µs). Two io_uring workers serve 1.8 times one.
Three workers could not be saturated on this 4 vCPU VM (see below).

## Method

[`benchmarks/profile/io_backend.sh`](../../../profile/io_backend.sh), the
method of [io-uring-backend](../io-uring-backend/README.md), now with
`WORKERS` and a CPU list in `SERVER_CPU`. It runs
`python -m bessemer run bessemer_app:app --workers N` with
`BESSEMER_EVENT_LOOP=bessemer` and `BESSEMER_IO_BACKEND` set to each backend,
pinned to the server CPUs, and oha on the rest. Each run is a 2 s warm-up and
10 s of `/json`, three rounds with the backends interleaved. CPU is summed
over the workers; `worker_cpus` in the raw files is each worker's CPU over
the run. Every response was a 200, and every io_uring worker logged
`serving connections through io_uring`.

Host: 4 vCPU VM, Linux 6.18, CPython 3.13, loopback. Branch
`claude/project-thread-uyd7pt` on top of `407eac1`.

## Results

Medians of three rounds; µs is CPU per request summed over workers.

| Workers (CPUs) | oha CPUs | Connections | Backend | req/s | server CPUs | µs/req | p50 µs | p99 µs |
| --- | --- | ---: | --- | ---: | ---: | ---: | ---: | ---: |
| 1 (0) | 1-3 | 64 | epoll | 77.4k | 0.99 | 12.83 | 798 | 1368 |
| 1 (0) | 1-3 | 64 | io_uring | **94.8k** | 0.99 | **10.42** | 633 | 1257 |
| 1 (0) | 1-3 | 1024 | epoll | 66.1k | 0.98 | 14.89 | 14580 | 25758 |
| 1 (0) | 1-3 | 1024 | io_uring | **84.6k** | 0.98 | **11.57** | 11489 | 19017 |
| 2 (0-1) | 2-3 | 64 | epoll | 161.7k | 1.93 | 11.93 | 366 | 1304 |
| 2 (0-1) | 2-3 | 64 | io_uring | **172.0k** | 1.92 | **11.09** | 326 | 1569 |
| 2 (0-1) | 2-3 | 1024 | epoll | 155.8k | 1.94 | 12.43 | 6386 | 14057 |
| 2 (0-1) | 2-3 | 1024 | io_uring | **169.0k** | 1.90 | **11.24** | 5822 | 14229 |
| 3 (0-2) | 3 | 64 | epoll | 81.7k | 1.22 | 14.63 | 205 | 12348 |
| 3 (0-2) | 3 | 64 | io_uring | 78.1k | 1.22 | 15.58 | 196 | 12424 |
| 3 (0-2) | 3 | 1024 | epoll | 78.1k | 1.21 | 15.53 | 15044 | 29055 |
| 3 (0-2) | 3 | 1024 | io_uring | 76.0k | 1.19 | 15.66 | 15150 | 29735 |

Raw runs: [1 worker](raw-1-worker.tsv), [2 workers](raw-2-workers.tsv),
[3 workers](raw-3-workers.tsv).

Two workers: per-worker CPU was 0.92–0.98 in every run of both backends, so
neither backend left a worker idle. The io_uring saving per request (7–10%)
is smaller than with one worker (19–22%) in this setup. This was not
investigated further; the client has fewer CPUs per server core here (two
for two workers, against three for one), which may account for some of it.

Three workers: with three server CPUs, oha has only CPU 3 and is the limit.
The workers used about 1.2 CPUs between them, and both backends served about
the same. The split between workers varied from run to run on both
backends (for example 0.12/0.41/0.71 on io_uring and 0.23/0.27/0.74 on
epoll). Keep-alive connections are assigned once, at connect time, to
whichever worker wins the accept, and here the workers were mostly idle.
Every worker got connections in every run. A fair three-worker figure needs
a larger machine or a separate client host.

## Limits

One VM shared by client and server, loopback only, one route. Figures are a
guide, not a guarantee.
