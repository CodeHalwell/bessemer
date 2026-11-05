# Bessemer vs FastAPI vs Axum on current main (exploratory)

A rerun of [compare-fastapi-axum](../compare-fastapi-axum/README.md) on `main`
at `407eac1`, now with Bessemer's opt-in Rust event loop and io_uring backend.
`raw.json` holds every sample.

- Host: the same 4-vCPU Intel Xeon VM, Linux 6.18, loopback. These are
  same-machine figures, so treat them as a guide rather than a formal result.
- Bessemer 1.0.0rc1 built from `407eac1` (`maturin build --release`),
  CPython 3.13.18, `bessemer run --workers N`. Setups:
  - **default**: asyncio loop, epoll.
  - **Rust loop**: `BESSEMER_EVENT_LOOP=bessemer`.
  - **Rust loop + io_uring**: also `BESSEMER_IO_BACKEND=io_uring` (the log
    confirmed `serving connections through io_uring`).
- FastAPI 0.162.0 on uvicorn 0.54.0 (`uvicorn[standard]`), Pydantic 2.14.3,
  access log off. Axum 0.8.9 on Tokio, fat LTO.
- `oha` 1.16.0, 64 keep-alive connections. Three rounds with all five servers
  interleaved in each; each sample is 10 s after a 3 s warm-up. Tables give
  the median, with the range across rounds. Every response was HTTP 200.
- Configs (`run_compare.py --configs`): **1-core** (server on CPU 0, oha on
  CPU 3), **2-core** (two workers or threads on CPUs 0-1, oha on CPUs 2-3),
  **3-core** (CPUs 0-2, oha on CPU 3).

Routes are as before: `GET /json`, `GET /items/7?q=abc` (int path + str
query) and `POST /echo` (validate and return a 20-item Pydantic model; a
`serde` struct through `Json<T>` in Axum).

### 1-core

| Route | Setup | req/s (range) | % of Axum | x FastAPI |
| --- | --- | ---: | ---: | ---: |
| `/json` | Bessemer default | 99.4k (97.0-101.7) | 86% | 8.7x |
| `/json` | Rust loop | 115.7k (114.8-118.7) | 100% | 10.1x |
| `/json` | Rust loop + io_uring | 138.5k (120.2-139.1) | 120% | 12.1x |
| `/json` | FastAPI | 11.4k (11.2-11.6) | 10% | 1.0x |
| `/json` | Axum | 115.2k (107.9-119.7) | 100% | 10.1x |
| `/items` | Bessemer default | 90.4k (88.0-92.7) | 70% | 9.8x |
| `/items` | Rust loop | 107.2k (106.1-114.1) | 83% | 11.6x |
| `/items` | Rust loop + io_uring | 109.0k (106.0-111.1) | 84% | 11.8x |
| `/items` | FastAPI | 9.2k (9.0-9.3) | 7% | 1.0x |
| `/items` | Axum | 129.9k (124.9-133.0) | 100% | 14.1x |
| `/echo` | Bessemer default | 32.2k (30.6-32.2) | 110% | 4.4x |
| `/echo` | Rust loop | 37.5k (37.1-38.2) | 128% | 5.1x |
| `/echo` | Rust loop + io_uring | 36.3k (35.0-37.9) | 124% | 5.0x |
| `/echo` | FastAPI | 7.3k (7.1-7.5) | 25% | 1.0x |
| `/echo` | Axum | 29.3k (28.6-30.2) | 100% | 4.0x |

### 2-core

| Route | Setup | req/s (range) | % of Axum | x FastAPI |
| --- | --- | ---: | ---: | ---: |
| `/json` | Bessemer default | 155.1k (138.5-170.5) | 77% | 6.7x |
| `/json` | Rust loop | 211.5k (211.3-215.5) | 105% | 9.2x |
| `/json` | Rust loop + io_uring | 110.6k (107.5-112.7) | 55% | 4.8x |
| `/json` | FastAPI | 23.1k (22.9-23.7) | 11% | 1.0x |
| `/json` | Axum | 202.4k (201.0-214.7) | 100% | 8.8x |
| `/items` | Bessemer default | 133.0k (127.7-150.4) | 69% | 7.0x |
| `/items` | Rust loop | 197.8k (195.6-203.9) | 103% | 10.5x |
| `/items` | Rust loop + io_uring | 101.8k (101.5-112.7) | 53% | 5.4x |
| `/items` | FastAPI | 18.9k (18.5-19.3) | 10% | 1.0x |
| `/items` | Axum | 191.8k (186.6-193.2) | 100% | 10.2x |
| `/echo` | Bessemer default | 59.9k (58.3-60.0) | 211% | 4.0x |
| `/echo` | Rust loop | 70.0k (69.5-71.4) | 247% | 4.7x |
| `/echo` | Rust loop + io_uring | 34.7k (33.1-36.6) | 122% | 2.3x |
| `/echo` | FastAPI | 15.0k (14.1-15.1) | 53% | 1.0x |
| `/echo` | Axum | 28.4k (22.0-32.9) | 100% | 1.9x |

### 3-core

| Route | Setup | req/s (range) | % of Axum | x FastAPI |
| --- | --- | ---: | ---: | ---: |
| `/json` | Bessemer default | 106.8k (105.7-109.3) | 85% | 2.7x |
| `/json` | Rust loop | 99.3k (92.6-108.4) | 79% | 2.5x |
| `/json` | Rust loop + io_uring | 121.7k (116.2-127.3) | 97% | 3.1x |
| `/json` | FastAPI | 39.8k (39.0-39.9) | 32% | 1.0x |
| `/json` | Axum | 125.7k (125.2-130.4) | 100% | 3.2x |
| `/items` | Bessemer default | 110.2k (109.0-111.1) | 89% | 3.5x |
| `/items` | Rust loop | 96.7k (96.1-97.6) | 78% | 3.1x |
| `/items` | Rust loop + io_uring | 109.4k (105.1-110.0) | 88% | 3.5x |
| `/items` | FastAPI | 31.2k (30.6-31.6) | 25% | 1.0x |
| `/items` | Axum | 124.5k (122.4-127.4) | 100% | 4.0x |
| `/echo` | Bessemer default | 87.7k (86.4-89.9) | 128% | 3.7x |
| `/echo` | Rust loop | 110.3k (107.3-110.8) | 161% | 4.6x |
| `/echo` | Rust loop + io_uring | 36.1k (35.0-37.6) | 53% | 1.5x |
| `/echo` | FastAPI | 23.9k (23.8-24.2) | 35% | 1.0x |
| `/echo` | Axum | 68.7k (62.5-70.4) | 100% | 2.9x |

## Reading the results

- **One core.** Against the run from this afternoon (`2386403`), the default
  path is up from 80.5k to 99.4k on `/json`. The Rust loop draws level with
  Axum on `/json` and reaches 83% on `/items`; io_uring adds more on `/json`.
  oha on one core tops out at roughly 130-140k, and Axum's `/json` rounds
  (108-120k, with its usual 12 ms p99) sit just under that, so read the
  one-core `/json` comparison as "level with Axum", not "20% faster".
- **Two cores is the cleanest multi-core view**, as oha has two cores of its
  own. The Rust loop with two workers serves 211.5k on `/json` and 197.8k on
  `/items`, 103-105% of Axum with two Tokio threads. The default loop is at
  69-77%.
- **`/echo` now beats Axum.** Responses were byte-identical. Axum's
  `Json<Order>` costs about 33 µs a request on one core: a raw-bytes echo on
  the same app does 114k/s and a hand-written `serde_json` parse and
  serialise does 42k/s, so the cost is in `serde` and `Json<T>` under load,
  not in the harness. TCP_NODELAY on Axum made no difference. Axum's two-core
  `/echo` was noisy (22-33k) and below its one-core rate, so the two-core
  `/echo` ratios overstate the lead; the three-core 161% is the better guide.
- **io_uring does not scale past one worker.** With two or three workers, the
  io_uring setup serves at single-worker speed. During a two-worker run, one
  worker used 606 CPU ticks and the other 15: one worker's multishot accept
  takes every connection on the shared listener. On a single worker, io_uring
  helps; with more, use the Rust loop on epoll until accept is balanced.
- **Three cores is client-bound** for the small routes: Axum is no faster
  than on one core (about 125k), so `/json` and `/items` ratios there say
  little. The Rust loop's 12 ms p99 there matches Axum's and probably reflects
  connection fairness under a saturated client.
- FastAPI is 8.7-12x behind on the small routes and 4-5x on `/echo` on one
  core.
