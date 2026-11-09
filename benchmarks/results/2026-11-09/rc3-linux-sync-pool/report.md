# Adaptive sync pool on Linux — 9 November 2026

PR #112 made the default GIL sync pool grow from eight to sixteen threads when
work queues up and the process has CPU to spare. Its own evidence came from a
shared macOS host, where a CPU-bound route looked slower and grew to eleven
threads. This run checks the same question on Linux before 1.0.0rc3.

## Method

- Build: `main` at `be2aa3e` (PR #112 merged), release wheel, CPython 3.13.18,
  Linux x86_64, 4 vCPUs, 15 GiB, no pinning.
- One worker, default settings otherwise. **fixed8** passes
  `--sync-workers 8` (the rc2 pool); **adaptive** leaves it at the default.
  Both variants run the same wheel, so only the pool policy differs.
- Load: oha 1.16.0, 64 connections, 1 s warm-up then 4 s measured, three
  rounds with the variant order alternating. Fixed offers use latency
  correction; `closed` means as fast as the server answers.
- Routes (`def` handlers): `sync` sleeps 1 ms; `cpu-sync` runs the multicore
  `checksum(50_000)`; `mixed-sync` runs `checksum(10_000)` then sleeps 2 ms.
- CPU is the server process tree's user+system time over the measured window.
  Threads is the number of `bessemer-handler` threads at the end.
- The contended runs add three busy-looping Python processes on the host.

Raw rows are in `results.json`. Medians of three:

## Idle host

| Route | Offer | Variant | Successful/s | p99 ms (3 rounds) | CPU s / million | Threads |
|---|---|---|---:|---|---:|---|
| sync | 5,000/s | fixed8 | 4,998.5 | 5.4, 9.0, 3.8 | 106.5 | 8, 8, 8 |
| sync | 5,000/s | adaptive | 4,997.8 | 8.0, 4.7, 4.7 | 115.6 | 9, 8, 9 |
| sync | 10,000/s | fixed8 | 7,082.5 | 1,157, 1,140, 1,184 | 78.4 | 8, 8, 8 |
| sync | 10,000/s | adaptive | 9,998.5 | 94.1, 9.2, 13.1 | 86.0 | 16, 14, 15 |
| cpu-sync | closed | fixed8 | 271.0 | 347, 350, 376 | 3,782 | 8, 8, 8 |
| cpu-sync | closed | adaptive | 273.8 | 348, 357, 371 | 3,772 | 8, 8, 8 |
| mixed-sync | closed | fixed8 | 1,253.8 | 69, 69, 65 | 802 | 8, 8, 8 |
| mixed-sync | closed | adaptive | 1,274.3 | 85, 71, 64 | 795 | 9, 8, 8 |

## Contended host (three spinning processes)

| Route | Variant | Successful/s | p99 ms (3 rounds) | CPU s / million | Threads |
|---|---|---:|---|---:|---|
| cpu-sync | fixed8 | 291.3 | 341, 347, 332 | 3,373 | 8, 8, 8 |
| cpu-sync | adaptive | 284.3 | 390, 339, 347 | 3,478 | 9, 9, 8 |
| mixed-sync | fixed8 | 1,384.0 | 79, 69, 81 | 721 | 8, 8, 8 |
| mixed-sync | adaptive | 1,439.5 | 74, 66, 70 | 705 | 8, 8, 8 |

## Decision

The adaptive pool stays the default for 1.0.0rc3, with no change to its
growth rule:

- Blocking handlers: at 10,000/s the fixed pool tops out near 7,100/s with
  p99 above a second; the adaptive pool serves the full offer. CPU per
  request rises about 8 to 10% at both offers.
- CPU-bound handlers: no growth and no measurable change on an idle host.
  Under host contention the pool grew by at most one thread and served about
  2% fewer requests, within the round-to-round spread. This does not
  reproduce the macOS result's eleven threads or 12% loss.
- Mixed handlers: no regression on either host.

Apps that want the old pool set `sync_workers=8`. These are short, same-host
diagnostics on a small VM, not the formal M0 gate.
