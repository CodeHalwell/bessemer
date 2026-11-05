# FastAPI parity: hot-path check

Does the FastAPI-compatibility work ([parity](../../../../docs/fastapi-parity.md))
slow down routes that do not use it? Measured on the same host with the
`benchmarks/compare` harness: one worker pinned to CPU 0, `oha` with 64
connections pinned to CPU 3, 3 s warm-up then 10 s per route. The baseline
is `main` at `7c2f7f6`; the branch is the same native extension with the
parity Python changes. Runs alternated baseline, branch, three times.

| Route | Baseline req/s (median of 3) | Branch req/s (median of 3) | Difference |
| --- | ---: | ---: | ---: |
| `GET /json` | 81,955 | 79,904 | -2.5% |
| `GET /items/7?q=abc` | 62,521 | 62,964 | +0.7% |
| `POST /echo` (Pydantic) | 11,527 | 11,769 | +2.1% |
| `GET /io` (1 ms sleep) | 24,678 | 24,642 | -0.1% |

Run-to-run spread within each build was up to 4%, so every difference is
within noise. To check the largest one, both servers were started side by
side on CPU 0 and loaded alternately on `/json`, six 8 s runs each:

| Build | Runs (req/s) | Median |
| --- | --- | ---: |
| Baseline | 81,717 · 81,338 · 85,835 · 84,123 · 86,920 · 85,167 | 84,645 |
| Branch | 83,586 · 84,030 · 82,093 · 83,548 · 84,385 · 84,991 | 83,808 |

The branch sits inside the baseline's range. p99 latency was unchanged on
every route (`raw.json`). The non-zero error counts in `raw.json` are the 64
requests in flight when each `oha` run stops, in both builds.

Exploratory, single host, short runs; not a formal performance gate.
