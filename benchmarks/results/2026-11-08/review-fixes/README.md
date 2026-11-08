# Review fixes and model fast path, 2026-11-08

`main` is c7d630d; `branch` is this change (HEAD Content-Length fix and the
model fast-path changes). Both are release wheels built from the tree for
CPython 3.13 on the same 4-CPU cloud container, with the default event loop
(`auto`, so the Rust loop) and one worker.

## Instructions per request

`benchmarks/profile/instructions.sh` (callgrind, `CONNECTIONS=1`,
`BESSEMER_EVENT_LOOP=auto`), on `benchmarks/compare/bessemer_app.py`.
`/echo` posts the 20-item order (`id`, `name`, `price`, `tags` per item).

| Route | main | branch | change |
| --- | ---: | ---: | ---: |
| `GET /json` | 33,351 | 33,400 | +0.1% |
| `GET /items/7?q=hello` | 36,369 | 36,388 | +0.1% |
| `POST /echo` (3 runs each) | 212,126, 210,744, 209,512 | 203,254, 202,730, 201,697 | -3.9% (means) |

## Throughput

Server pinned to CPU 0 (`taskset -c 0`), `oha` 1.16 on CPUs 2-3, 2 s warm-up
then 8 s per sample, `--max-in-flight 4096`. Three rounds, `main` and
`branch` interleaved in each round. Median (min to max) of the three; every
response was 200. Samples are in `throughput.jsonl`.

| Route | Connections | main req/s | branch req/s | change | p99 main / branch (ms) |
| --- | ---: | ---: | ---: | ---: | ---: |
| `/json` | 1 | 21,932 (21,880-22,041) | 21,798 (21,482-22,362) | -0.6% | 0.07 / 0.08 |
| `/json` | 64 | 111,015 (110,618-113,162) | 111,354 (89,151-115,414) | +0.3% | 0.99 / 1.00 |
| `/items` | 1 | 21,352 (21,293-21,678) | 21,153 (20,947-21,279) | -0.9% | 0.08 / 0.08 |
| `/items` | 64 | 105,857 (105,628-107,388) | 104,516 (103,009-106,647) | -1.3% | 1.03 / 1.13 |
| `/echo` | 1 | 14,534 (14,027-14,764) | 14,826 (14,573-15,134) | +2.0% | 0.11 / 0.11 |
| `/echo` | 64 | 42,047 (40,882-43,809) | 43,389 (41,860-43,804) | +3.2% | 2.80 / 2.89 |

The `/json` and `/items` differences are within run-to-run noise on this
host, as the unchanged instruction counts confirm; `/echo` moves with its
instruction count. The client shares the host, so these are comparisons
between the two builds, not absolute capacity figures.
