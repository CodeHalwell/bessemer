# 1.0.0rc2 performance sweep (9 November 2026)

Same-host run on a 4-vCPU cloud container (shared, not isolated), CPython
3.13.18. Servers on CPU 0 (1 worker) or CPUs 0-1 (2 workers), `oha` on CPUs
2-3. Bessemer is the release wheel of this branch (`rc2`) and of `main` at
`b4e4783` (`main`); FastAPI 0.162.2 on uvicorn 0.54.0 (no uvloop or
httptools, as in earlier runs); Axum from `benchmarks/compare/axum_app`.
Each figure is the best HTTP-200 req/s of the samples taken (4 s after a 1 s
warm-up). Treat differences under about 7% between `rc2` and `main` as
noise on this host: the interleaved A/B below shows that spread. Raw JSON
lines were not committed.

## FastAPI feature matrix (one core, 64 connections)

| Route | rc2 | main | FastAPI | rc2 / FastAPI |
|---|---:|---:|---:|---:|
| `/bg` | 33,379 | 31,894 | 2,629 | 12.7x |
| `/big` | 23,071 | 23,349 | 1,228 | 18.8x |
| `/cookie` | 93,483 | 96,689 | 4,632 | 20.2x |
| `/dep` | 58,076 | 58,664 | 3,975 | 14.6x |
| `/dict` | 61,624 | 67,485 | 3,803 | 16.2x |
| `/form` | 70,121 | 72,398 | 3,392 | 20.7x |
| `/header` | 96,592 | 91,763 | 4,621 | 20.9x |
| `/io` | 32,551 | 33,994 | 4,095 | 7.9x |
| `/item` | 87,623 | 92,940 | 4,020 | 21.8x |
| `/json` | 97,141 | 98,313 | 5,000 | 19.4x |
| `/models` | 73,013 | 74,817 | 4,728 | 15.4x |
| `/plain` | 73,041 | 77,195 | 4,890 | 14.9x |
| `/query?a=2&d=7` | 90,608 | 93,467 | 3,960 | 22.9x |
| `/request?q=1` | 44,119 | 46,113 | 4,376 | 10.1x |
| `/sync` | 71,905 | 72,597 | 3,647 | 19.7x |
| `/yield_dep` | 52,148 | 52,494 | 4,522 | 11.5x |
| middleware + `/dep` | 38,499 | 36,531 | 2,119 | 18.2x |
| middleware + `/json` | 52,797 | 54,065 | 2,498 | 21.1x |
| middleware + `/yield_dep` | 25,004 | 26,735 | 2,286 | 10.9x |

`/raise` returns 404 by design and is left out.

## Interleaved rc2 / main A/B (one core, 64 connections, three rounds, order alternating)

| Route | main (median) | rc2 (median) | Change |
|---|---:|---:|---:|
| `/json` | 97,045 | 95,422 | -1.7% |
| `/dict` | 65,616 | 68,996 | +5.2% |
| `/item` | 89,324 | 87,274 | -2.3% |
| `/plain` | 70,658 | 65,959 | -6.6% |
| `/request?q=1` | 44,997 | 44,232 | -1.7% |
| `/io` | 34,066 | 34,462 | +1.2% |
| `/cookie` | 89,753 | 90,232 | +0.5% |

`/plain` and `/json` run no code this branch changed; their spread is the
host's noise.

## Concurrency (`/json` and `/io`, which awaits `asyncio.sleep(0.001)`)

| Route | Workers | Conns | rc2 | main | Axum | FastAPI | rc2 p99 ms | Axum p99 ms | FastAPI p99 ms |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| `/io` | 1 | 64 | 35,657 | 35,388 | 22,955 | 4,532 | 2.6 | 3.6 | 19.2 |
| `/io` | 1 | 256 | 61,495 | 61,965 | 72,359 | 3,979 | 6.8 | 5.0 | 95.0 |
| `/io` | 1 | 1024 | 57,600 | 57,068 | 90,279 | 3,404 | 28.5 | 58.7 | 445.7 |
| `/io` | 2 | 64 | 42,846 | 44,253 | 22,980 | 1,383 | 2.2 | 3.6 | 52.4 |
| `/io` | 2 | 256 | 116,872 | 115,791 | 77,044 | 5,042 | 4.3 | 4.9 | 79.8 |
| `/io` | 2 | 1024 | 125,229 | 125,474 | 123,490 | 7,073 | 14.3 | 22.6 | 219.8 |
| `/json` | 1 | 64 | 91,439 | 89,831 | 105,134 | 5,160 | 1.4 | 1.3 | 19.2 |
| `/json` | 1 | 256 | 82,729 | 79,874 | 96,627 | 4,595 | 6.5 | 5.0 | 71.6 |
| `/json` | 1 | 1024 | 75,877 | 78,608 | 89,399 | 4,426 | 20.2 | 164.5 | 364.7 |
| `/json` | 2 | 64 | 195,357 | 192,719 | 181,682 | 1,427 | 3.6 | 0.9 | 51.9 |
| `/json` | 2 | 256 | 177,555 | 192,425 | 207,094 | 5,475 | 5.4 | 5.3 | 59.9 |
| `/json` | 2 | 1024 | 169,732 | 176,065 | 186,104 | 8,942 | 14.2 | 12.8 | 163.5 |

No sample had a non-200 response or a failed connection; the only `oha`
errors were its end-of-run aborts. With two workers and 64 connections the
two client CPUs limit `/json` for Bessemer and Axum alike. Axum's `/io` at 64
connections is capped by Tokio's millisecond timer rounding the 1 ms sleep
up (see `benchmarks/compare/README.md`). FastAPI's two-worker figures at 64
connections are below its one-worker ones in this run and were not
investigated; they do not affect the Bessemer comparison.
