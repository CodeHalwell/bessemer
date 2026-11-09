# Workload efficiency follow-up — 9 November 2026

The retained changes improve the tested blocking-I/O capacity and reduce
incremental idle-connection RSS. These runs establish no general CPU-work or
four-worker scaling improvement.

## Retained changes

- HTTP/1 starts with a 2 KiB read buffer, reclaims consumed space, and preserves
  8 KiB growth for larger traffic. Read-ahead allocates only when needed;
  pending handlers can use existing spare space. Regressions cover repeated
  and medium heads, shared bytes, large heads, pipelining and EOF.
- Implicit Linux/macOS GIL sync pools start lazily up to eight threads, with a
  sixteen-thread ceiling. Growth needs four queued jobs for 10 ms plus a
  checked process-CPU window below 0.75 cores. Windows/unavailable-clock and
  source-only defaults stay eight; explicit limits and free-threaded CPU-share
  sizing stay fixed. Extra threads remain parked until shutdown. The process
  utilisation gate does not classify individual handlers.
- A checked-in worker-balance diagnostic captures socket ownership, per-worker
  active CPU, expected HTTP responses and process cleanup. Listener code is
  unchanged.

## Build and method

Base: `4fac5e267eef990b005d7e1362e166007a813658`. Candidate is an unreleased local wheel
with the existing `1.0.0rc2` metadata. Published RC2 remains a separate immutable
PyPI installation; dependency pins and CPython 3.13.18 match. Hardware is the
shared Apple M5 Max, macOS 27.2 arm64, with no affinity or CPU quota applied.
These are paired short diagnostics, not formal M0 or production acceptance.

Candidate wheel SHA256: `d47a2fa10de8b57d3d120ec90f3af300d08f4edab4fda30c706d874abc7ea949`. Installed native SHA256:
`e5e43d606d0d30d3b3766c6c187c84d38f11025912f17cec2e5aa0e761b59361`. Full compiled-source hashes and package
versions are in `receipt.json`; original metadata is retained in the cache.

HTTP phases use 64 connections, one worker except the explicit four-worker
JSON cells, two alternating-order repeats, 1 s warm-up and 3 s measurement.
Fixed offers use oha 1.16.0 scheduling-latency correction and per-request SQLite
status/body-length validation. The diagnostic gate is p99 ≤50 ms, failure
≤0.1%, delivery ≥99%, ≤1% slow successes and no detected client saturation.
Pre/post probes check exact bodies. Fixed-rate p99 is not pooled with the
uncorrected closed-load CPU/scaling cases. Medians of two samples are
summaries, not confidence intervals or capacity limits.

## Blocking handlers

At an offered 10,000/s for the existing 1 ms blocking sleep:

| Build | Successful responses/s | Corrected p99 ms | CPU s / million successes |
|---|---:|---:|---:|
| rc2 | 6,323.6 | 1,095.275 | 61.05 |
| candidate | 9,990.2 | 6.606 | 73.21 |

Candidate passed both repeats (p99 5.757–7.456 ms); RC2 failed both delivery/
latency gates. The offered rate is a tested point, not an exact maximum.

At 5,000/s both builds passed:

| Build | Successful responses/s | Corrected p99 ms | CPU s / million successes |
|---|---:|---:|---:|
| rc2 | 4,988.6 | 7.834 | 69.53 |
| candidate | 4,996.6 | 6.688 | 81.64 |

Measured CPU per success increased at this moderate offer. No broad CPU-cost
saving is claimed. In the separate same-process 5k → 10k → 5k sequence,
candidate passed all phases; RC2 failed the burst. Candidate post-burst p99
was 2.056 ms, CPU was 54.20 s/million versus 49.05 before the burst, and its
sixteen pool threads remained alive. Only one such sequence per build ran.

## HTTP request paths

All twelve JSON/echo/large-header samples passed the fixed-offer gate with
zero status, transport or body-length failures.

| Workload / offer | RC2 corrected p99 ms | Candidate corrected p99 ms | RC2 CPU s/million | Candidate CPU s/million |
|---|---:|---:|---:|---:|
| json / 30,000/s | 6.422 | 1.118 | 10.00 | 9.52 |
| echo / 8,000/s | 0.705 | 0.857 | 22.57 | 22.89 |
| large-header / 10,000/s | 1.859 | 0.815 | 13.58 | 14.40 |

The header case adds 4 KiB of request padding. CPU and latency differences
were mixed; these cells demonstrate compatibility at the tested offers.

## Connection memory

Four sessions used 0/256/1,024 body-validated idle keep-alive connections and
2 s sampling per level. Every target was reached and all measurement/close
errors were empty.

| Build | Incremental RSS at 1,024 connections, MiB (range) | KiB per added connection (median) |
|---|---:|---:|
| rc2 | 18.273 (18.250–18.297) | 18.273 |
| candidate | 12.531 (12.297–12.766) | 12.531 |

The median incremental saving was about 5.74 KiB per socket (31%). Summed RSS
includes shared pages and changed with host memory compression; candidate
zero-socket RSS ranged 54.4–74.8 MiB. USS was unavailable and remains null.
This is a small-request idle-connection result, not a total-instance or billed
memory reduction. Large-request buffers still grow.

## CPU and worker scaling remain open

The closed-load checksum route was overloaded in all four samples:

| Build | Successful responses/s | Uncorrected p99 ms | CPU s / million successes |
|---|---:|---:|---:|
| rc2 | 342.4 | 323.544 | 2,825.92 |
| candidate | 300.1 | 376.615 | 3,212.63 |

RC2 returned 1,221 HTTP 503s; candidate returned 1,175. Candidate used 8/11 pool
threads in the two samples: a process utilisation gate can permit growth
under scheduler contention. CPU-heavy work showed no improvement. Host load
also differed between paired samples; the recorded rates do not isolate a
source-level regression. CPU-heavy applications can retain explicit fixed
thread limits and use the existing process CPU offload API.

Four-worker/64-connection JSON returned only checked 200s. RC2 rates were
170,490/160,071 per second; candidate rates were 169,058/68,171. The slow
candidate sample coincided with 99.8% host CPU utilisation, versus 62.7–77.9%
in the other samples. This repeat did not resolve the scaling plateau.

The earlier five-second published-RC2/256-connection probe observed socket
ownership 25/47/120/64 with worker CPU 0.356/0.552/0.908/0.670 cores. This is
observed imbalance, not proof of the plateau's sole cause. No listener
scheduling change is retained. Next work is isolated per-worker profiling
and listener distribution/reload qualification.

## Validation and receipts

- Installed final release wheel: 67 focused Python tests passed.
- Rust: 39 HTTP/1 tests and three synthetic pool-policy tests passed.
- Benchmark diagnostic: 11 focused tests passed; real CLI smoke receipts are
  retained separately with startup/validation failures visible.
- Rust formatting, warnings-denied Clippy, Ruff, diff checks and new-function
  complexity checks passed. Legacy CLI/Worker methods already above the
  complexity threshold were not broadly refactored in this slice.
- Full check.sh, Linux, Windows and a GIL-disabled build were not run in this
  slice. The API compatibility and fixed sizing behaviours have local tests.

The retained comparison contains **34 HTTP load samples**, **2,552,863 expected
responses**, **2,396 overload 503s**, **zero transport errors** and **zero wrong
body lengths**. Four memory sessions supply twelve idle levels. Counts exclude
prototype experiments and rejected diagnostic receipts. All comparison server
process trees exited without forced kills or survivors.

`results.csv` contains per-sample outcomes. `receipt.json` contains immutable
source/wheel/result identities. Full oha JSON, SQL, memory samples, commands,
prototype/rejected-run receipts, logs and frozen drivers remain under:

`~/Library/Caches/bessemer-bench/workload-efficiency-2026-11-09/`

The cache's `short-*` and `final-*` files are earlier prototypes; only
`retained-*` files describe this retained wheel. The first prototype's
backlog-only policy was replaced after sync-CPU latency worsened. The retained
memory implementation also includes the later medium-header ownership fix.

The final diagnostic CLI smoke passed on the retained installed wheel:
556,454 expected responses, zero status/body/transport failures, socket
ownership 28/69/51/108, and no surviving processes or forced kills. See
`worker-balance.json`. Earlier rejected diagnostic attempts exposed venv
symlink, import-path and SQLite Path conversion bugs; all have focused
regressions. The shared SQLite parser now explicitly closes its connection;
the recorded comparison query/formulas are unchanged.
