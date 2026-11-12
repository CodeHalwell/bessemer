# GC collection accounting: local validation and benchmark

12 November 2026. Base: published RC5 (`69add39`). The candidate retires body
allowances when a collection resets the young allocation count. Old request
completion cannot subtract a grant issued after that collection.

## Correctness checks

The installed release wheel passed `BESSEMER_REQUIRE_NATIVE=1 ./scripts/check.sh`:
295 Rust tests passed (2 ignored), 1,151 Python tests passed (24 skipped), and
63 harness tests passed (3 skipped), plus format, Clippy and Ruff checks.
Six separate runs of the 21-case GC module each passed 20 tests with one skip.
Python 3.12.16/Pydantic 2.10.6 compatibility tests also passed 20 with one skip;
GIL-disabled 3.14.7 passed its three applicable cases with 18 GIL-only skips.
The compatibility wheels were unoptimised; the measured 3.13 wheel was release-built.

The original eight isolated probes now observe `1,1,1` young collections for
three allocation waves, with both allowance modes and both loops, after
explicit or automatic collection. The complete body remains live throughout.
The collection/overlap regressions fail against published RC5.

## Matched throughput comparison

Local source-built fix wheel versus retained published RC5/RC4 wheels and FastAPI. One worker, 64 connections; balanced rotated order. Large typed echo: four eight-second rounds; controls: three four-second rounds. One-second warmup per sample.

| Workload / reference | Paired throughput ratio, fix/reference | Paired CPU/success ratio |
| --- | ---: | ---: |
| echo-large:rc5-pypi | 1.0757 | 0.9856 |
| echo-large:rc4-pypi | 1.5789 | 0.6637 |
| echo-large:fastapi-uvloop | Withheld | Withheld |
| json:rc5-pypi | 1.0691 | 0.9669 |
| json:fastapi-uvloop | 4.5353 | 0.2068 |
| echo:rc5-pypi | 0.9629 | 0.9849 |
| echo:fastapi-uvloop | 4.5721 | 0.2094 |

Ratios are medians of within-round pairs, not ratios of group medians.

| Workload / variant | Median RPS | RPS range | Median host CPU | Errors |
| --- | ---: | ---: | ---: | ---: |
| echo-large:fix-default | 1,165 | 1,076–1,191 | 89.8% | 0 |
| echo-large:rc5-pypi | 1,092 | 969–1,123 | 92.7% | 0 |
| echo-large:rc4-pypi | 750 | 669–789 | 89.6% | 0 |
| echo-large:fastapi-uvloop | 280 | 252–303 | 91.6% | 229 |
| json:fix-default | 35,929 | 32,047–36,477 | 95.7% | 0 |
| json:rc5-pypi | 34,120 | 29,217–42,471 | 95.8% | 0 |
| json:fastapi-uvloop | 7,922 | 7,407–8,536 | 96.1% | 0 |
| echo:fix-default | 23,747 | 21,619–24,704 | 92.7% | 0 |
| echo:rc5-pypi | 23,775 | 22,453–26,343 | 93.5% | 0 |
| echo:fastapi-uvloop | 5,069 | 4,728–5,466 | 96.8% | 0 |

Retained 34 samples, 1,675,038 successful measured responses, 229 measured transport errors, 0 warmup transport errors and 0 cleanup failures.

Execution-valid paired ratios remain observations on a shared host. Check host-pressure differences and repeat ranges before attributing gains or losses. Erroneous/client-bound/unclean planned comparisons are withheld as a whole.

Candidate is a locally source-built wheel; rc5-pypi and rc4-pypi are actual published wheels.
Measured status/length and warmup status/error checks are distinct; warmup per-response lengths were not recorded.
Closed-loop throughput samples do not establish fixed-offered-rate SLO or production cost savings.
CPU per success includes drain/export overhead; RSS can double-count shared pages and USS may be unavailable.
Shared unpinned Mac; no causal attribution for noisy control differences or general FastAPI capacity claim.

## Reproduction and receipts

- [All sample metrics](samples.csv), [paired analysis](summary.json), and
  [native/build/test/probe fingerprints](receipts.json) are retained beside this report.
- Actual published RC5 and RC4 wheels were reused unchanged. The candidate is
  a separately installed source-built wheel with the same version string.
  Python 3.13.18 and all dependencies match, including Pydantic 2.14.3,
  FastAPI 0.162.2, Uvicorn 0.54.0, uvloop 0.23.0 and httptools 0.9.0.
- The app is the unchanged `benchmarks/concurrency/app.py` from RC4. The large
  request contains 1,000 items; the small typed echo contains 20. Oha 1.16.0
  uses two client threads, drains deadline requests, records every measured
  status/length in SQLite, and validates semantic bodies before/after load.
- A frozen Latin-rotated plan runs large-body FIX/RC5/RC4/FastAPI for four
  rounds of 8 seconds, then JSON and small echo FIX/RC5/FastAPI for three
  rounds of 4 seconds; every sample has a one-second warmup. All use one
  worker and 64 connections. Bessemer uses its default Rust loop;
  FastAPI uses uvloop/httptools. Builds and tests finished before timed load.
- The runner is the same RC4 assessment harness copied byte-for-byte, wrapped
  by `run_fix.py`. Full plans, scripts, raw JSON, SQLite, logs, wheel files
  and independent SQL/arithmetic checks are retained locally at
  `~/Library/Caches/bessemer-bench/gc-fix-2026-11-12/`. From that directory:

  ```sh
  # For a fresh run, preserve existing outputs and use a new run directory.
  venv-fix/bin/python run_fix.py large
  venv-fix/bin/python run_fix.py controls
  venv-fix/bin/python analyse_fix.py
  ```

The source-built candidate has no matched compiler-only control against the
release wheels.

No formal M0 gate, fixed-offered-load production cost acceptance, Mac worker
scaling or free-threaded performance comparison was run for this correction.

## Provenance

The probe and harness scripts whose hashes are in `receipts.json`
(`run_full.py`, `full_server.py`, `feature_app.py`, `feature_cases.py`,
`checksum_work.py` and the collection probes) are not in this repository, so
the probe results and the throughput figures above cannot be re-run from it.
The repository's own reproduction of the fault is the five regression tests
in `tests/python/test_gc_allowance.py`, which fail against the published
1.0.0rc5 wheel.
