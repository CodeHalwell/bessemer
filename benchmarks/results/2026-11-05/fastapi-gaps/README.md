# FastAPI gaps: hot-path check

Do path converters, `Body(embed=True)` and the `FastAPI` alias
([parity](../../../../docs/fastapi-parity.md)) slow down routes that do not
use them? Measured on the same host with the `benchmarks/compare` harness:
one worker pinned to CPU 0, `oha` with 64 connections pinned to CPU 3, 3 s
warm-up then 10 s per route. The baseline is `main` at `04bbc82`; the branch
adds the three changes, with its own native extension build. Runs alternated
baseline, branch, three times.

| Route | Baseline req/s (median of 3) | Branch req/s (median of 3) | Difference |
| --- | ---: | ---: | ---: |
| `GET /json` | 84,332 | 82,875 | -1.7% |
| `GET /items/7?q=abc` | 66,063 | 66,148 | +0.1% |
| `POST /echo` (Pydantic) | 18,492 | 18,480 | -0.1% |
| `GET /io` (1 ms sleep) | 24,810 | 25,079 | +1.1% |

Run-to-run spread within each build was up to 4%, so every difference is
within noise. To check the largest one, both servers were started side by
side on CPU 0 and loaded alternately on `/json`, six 8 s runs each:

| Build | Runs (req/s) | Median |
| --- | --- | ---: |
| Baseline | 84,136 · 85,088 · 83,193 · 84,338 · 84,707 · 84,097 | 84,237 |
| Branch | 83,948 · 84,886 · 84,691 · 85,271 · 85,439 · 83,396 | 84,789 |

The branch sits inside the baseline's range. p99 latency stayed within
run-to-run variation on every route (`raw.json`).

Exploratory, single host, short runs; not a formal performance gate.
