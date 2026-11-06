# PyPI Bessemer 1.0.0rc1 vs FastAPI on macOS

Measured on 6 November 2026 on an Apple M5 Max, macOS 27.2 arm64, CPython
3.13.18. Bessemer was installed from the published macOS arm64 CPython 3.13
wheel, whose SHA-256 is `fd946d1dc157e8c602f7d422efd152683342ef437baffad61be678e13aeeabbb`.
The compared versions were Bessemer 1.0.0rc1, FastAPI 0.162.0, Uvicorn 0.54.0,
Pydantic 2.14.3, uvloop 0.23.0 and httptools 0.9.0. The complete installed
versions are in [installed-versions.txt](installed-versions.txt).

## Throughput

Requests per second, median of three four-second samples, with the minimum and
maximum in brackets. Each sample had a one-second warmup and 64 persistent
HTTP/1.1 connections. Both servers used one worker. Bessemer (Rust loop) is the
opt-in `BESSEMER_EVENT_LOOP=bessemer` variant.

| Route | Bessemer, default | Bessemer, Rust loop | FastAPI + Uvicorn | Default / FastAPI |
| --- | ---: | ---: | ---: | ---: |
| JSON | 102,513 [101,565–107,086] | 60,405 [56,831–67,474] | 18,103 [15,619–19,244] | 5.66× |
| Path and query | 97,226 [88,633–103,346] | 68,863 [44,451–75,162] | 13,361 [11,895–14,232] | 7.28× |
| Pydantic echo | 71,514 [65,280–77,694] | 31,709 [28,981–32,540] | 9,735 [8,462–10,724] | 7.35× |
| 1 ms async sleep | 48,250 [47,414–49,182] | 17,307 [16,643–18,392] | 11,726 [11,439–12,139] | 4.11× |

Median estimated server CPU use, in cores, from process-tree CPU time divided
by elapsed time:

| Route | Bessemer, default | Bessemer, Rust loop | FastAPI + Uvicorn |
| --- | ---: | ---: | ---: |
| JSON | 2.04 | 0.80 | 0.87 |
| Path and query | 2.06 | 0.85 | 0.88 |
| Pydantic echo | 2.26 | 0.86 | 0.88 |
| 1 ms async sleep | 1.53 | 0.35 | 0.86 |

The 36 throughput samples returned 6,547,076 HTTP 200 responses, with zero
other responses and zero load-generator errors. Full per-sample data and
latency distributions are in [raw.json](raw.json).

## Latency at 5,000 requests/second

Each route below was driven at the same target rate for three three-second
samples, with one-second warmups and `oha --latency-correction`. Values are
medians of the individual samples' p50 and p99 latencies, in milliseconds.
Ranges show the lowest and highest sample p99, rather than a combined p99.

| Route | Bessemer default p50 / p99 [p99 range] | Bessemer Rust loop p50 / p99 [p99 range] | FastAPI p50 / p99 [p99 range] |
| --- | ---: | ---: | ---: |
| JSON | 0.150 / 0.691 [0.542–0.761] | 0.187 / 0.851 [0.815–0.864] | 0.440 / 1.673 [1.615–1.864] |
| Pydantic echo | 0.235 / 0.802 [0.715–4.685] | 0.295 / 1.034 [0.999–2.535] | 0.610 / 2.818 [2.351–40.073] |

All 18 fixed-rate samples sustained approximately 5,000 requests/second and
returned 269,909 HTTP 200 responses with zero reported errors. See
[fixed/raw.json](fixed/raw.json) for every sample.

## Method and limits

The benchmark apps and payload are copies of the repository's
`benchmarks/compare` reference. The macOS adapters use those same routes and
server commands, alternate framework order between rounds, and check all four
response bodies before each run. They wait for in-flight requests at the end
of each timed sample; the Linux reference runner aborts those requests at the
deadline. `oha` 1.16.0 used two worker threads. Its macOS arm64 binary's SHA-256
is `7dea53ecb8342a7a067e1976fd0aef44ac33d9cc6b1e65c53637ffb932da63c4`.

The Mac ran client and server on the same host without CPU affinity. Its cores
have different performance classes and other applications were active. One
Bessemer worker uses two Tokio I/O threads as well as its Python event loop;
one Uvicorn worker largely uses one core. The measured CPU column makes that
difference visible. The Rust-loop path/query throughput ranged from 44,451 to
75,162 requests/second, and one FastAPI echo p99 reached 40 ms. Treat these as
exploratory local measurements, rather than a formal single-core or Linux
production qualification.

## Playground

The PyPI wheel and comparison packages are installed in
`~/Library/Caches/bessemer-bench/pypi-1.0.0rc1/venv`. The
[playground.py](playground.py) app exercises typed path and query parameters,
a dependency, a Pydantic body, health and OpenAPI. It returned 200 for valid
requests and 422 for invalid path and body input.

From this directory, start it with:

```sh
~/Library/Caches/bessemer-bench/pypi-1.0.0rc1/venv/bin/python -m bessemer run playground:app --host 127.0.0.1 --port 8765 --workers 1
```

Then try `curl 'http://127.0.0.1:8765/items/42?active=true'`, or POST JSON such
as `{"name":"demo","quantity":2}` to `/items`. OpenAPI is at `/openapi.json`.

The measured runners are [macos_compare.py](macos_compare.py) and
[macos_fixed_rate.py](macos_fixed_rate.py). For a repeat, pass the virtual
environment's Python as `--python`, an `oha` 1.16.0 binary as `--oha`, and a
new results path as `--out`. The run settings are retained in both raw files.
