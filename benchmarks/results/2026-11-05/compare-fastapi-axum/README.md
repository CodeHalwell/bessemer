# Bessemer vs FastAPI vs Axum (exploratory)

Same-host head-to-head of Bessemer `main` at `2386403` against FastAPI and a
plain Axum service, using four routes that are identical in all three apps
(`benchmarks/compare/`). Run with `run_compare.py`; `raw.json` holds every
sample.

- Host: 4-vCPU Intel Xeon VM, Linux 6.18. Servers were pinned with `taskset`
  to CPU 0 (1-core) or CPUs 0-2 (3-core). `oha` 1.16.0 ran on CPU 3 with 64
  keep-alive connections.
- Bessemer 1.0.0rc1 release wheel, CPython 3.13.18, `bessemer run --workers N`.
- FastAPI 0.121.3 on uvicorn 0.38.0 with `uvicorn[standard]` (uvloop,
  httptools), Pydantic 2.14.2, `--workers N`, access log off.
- Axum 0.8 on Tokio, `worker_threads = N`, release build with fat LTO
  (rustc 1.97).
- Three rounds. Each round runs Bessemer, then FastAPI, then Axum, and each
  sample is 10 seconds after a 3-second warm-up. The tables give the median
  requests per second, with the range across rounds in brackets. Every
  response was HTTP 200. oha reports the in-flight requests at the end of each
  timed run as aborted, which is expected.

Routes:

- `GET /json` returns `{"ok": true, "count": 42}`.
- `GET /items/7?q=abc` validates an integer path parameter and a string query.
- `POST /echo` validates and returns a Pydantic model (a typed struct in Axum)
  of 20 items.
- `GET /io` awaits a 1 ms sleep.

## One core

| Route | Bessemer | FastAPI | Axum | Bessemer / FastAPI | Bessemer / Axum |
| --- | ---: | ---: | ---: | ---: | ---: |
| `/json` | 80.5k (78.4-80.7) | 12.4k (12.3-12.5) | 132.3k (131.4-133.8) | 6.5x | 61% |
| `/items` | 63.3k (62.7-64.3) | 9.3k (9.1-9.3) | 137.9k (134.7-138.6) | 6.8x | 46% |
| `/echo` | 11.4k (11.4-11.8) | 7.0k (7.0-7.0) | 31.1k (31.0-31.5) | 1.6x | 37% |
| `/io` | 24.6k (24.2-24.9) | 11.0k (10.8-11.0) | 26.2k (26.0-26.7) | 2.2x | 94% |

Median latency on one core, p50 / p99 in ms:

| Route | Bessemer | FastAPI | Axum |
| --- | --- | --- | --- |
| `/json` | 0.76 / 1.45 | 5.08 / 7.39 | 0.12 / 12.17 |
| `/items` | 0.98 / 1.66 | 6.81 / 8.72 | 0.11 / 12.17 |
| `/echo` | 5.49 / 11.34 | 8.95 / 13.08 | 2.06 / 2.66 |
| `/io` | 2.57 / 3.81 | 5.55 / 10.24 | 2.63 / 3.33 |

## Three cores (three workers or threads)

| Route | Bessemer | FastAPI | Axum | Bessemer / FastAPI | Bessemer / Axum |
| --- | ---: | ---: | ---: | ---: | ---: |
| `/json` | 115.0k (109.9-116.6) | 42.9k (42.9-43.4) | 126.0k (124.8-128.8) | 2.7x | 91%* |
| `/items` | 112.2k (111.2-115.1) | 30.1k (29.7-30.2) | 129.4k (128.7-130.7) | 3.7x | 87%* |
| `/echo` | 36.3k (35.4-37.7) | 22.0k (21.8-22.0) | 58.6k (58.5-65.0) | 1.7x | 62% |
| `/io` | 37.2k (37.0-37.5) | 26.7k (26.6-28.5) | 27.0k (26.4-27.9) | 1.4x | see below |

\* Not a fair ratio. Axum on three cores (126k-129k) was no faster than on
one core, so the single client core is the ceiling at about 130k/s. Bessemer
and Axum `/json` and `/items` at three cores are bounded by `oha`, not the
server.

## Reading the results

- Bessemer beats FastAPI on every route: 6.5-6.8x on one core for routes
  where the framework dominates, and 1.6-1.7x where Pydantic validation of a
  20-item body dominates.
- On one core Bessemer reaches 46-61% of Axum on the small routes, and 37% on
  the validated body. There, Pydantic model validation and serialization in
  Python is most of the cost, against `serde` in Axum.
- `/io` measures timer behaviour more than the framework. Tokio's timer has
  millisecond granularity and rounds a 1 ms sleep up to the next tick, so
  Axum's requests wait close to 2 ms. asyncio sleeps closer to 1 ms. At 64
  connections that caps Axum near 27k/s on any core count. Treat `/io` as a
  Bessemer vs FastAPI comparison only.
- Axum's p99 of about 12 ms on the small routes appears in every round. It
  probably reflects connection fairness under a saturated client rather than
  slow requests, because its p50 is 0.11 ms. It was not investigated further.
- These are one host, short runs and a single client core. They are
  diagnostic evidence, not a formal performance result. A fairer three-core
  comparison needs the load generator on separate hardware.
