# RC6 CPU admission fix — final candidate benchmarks

Measured 2026-11-14 on the shared M5 Max; source-built candidate against source-built RC6 and pinned FastAPI 0.162.2.

242 attempts, 242 measurements, 242 individually eligible; 18,446,190 successful measured replies. Independent SQL counter/latency audit passed.

## Failures and correctness

| Variant | Measured statuses | Transport | Wrong lengths | Warmup statuses | Warmup transport |
|---|---:|---:|---:|---:|---:|
| candidate | 0 | 0 | 0 | 0 | 0 |
| rc6 | 0 | 0 | 0 | 0 | 0 |
| fastapi | 0 | 0 | 0 | 0 | 0 |

Lifecycle issues: 0. Any failed round withholds the entire paired comparison; medians marked † include an ineligible population.

## Throughput at 16 concurrent requests

| Python mode | Workload | Candidate req/s | RC6 req/s | FastAPI req/s | Candidate/RC6 paired | Candidate/FastAPI paired |
|---|---|---:|---:|---:|---|---|
| cp313 | cpu-inline | 657 | 655 | 615 | 0.998× (0.996–1.014) | 1.062× (1.051–1.082) |
| cp313 | cpu-sync | 658 | 659 | 615 | 0.985× (0.983–1.015) | 1.072× (1.070–1.087) |
| cp313 | cpu-offload-auto | 2,187 | 2,177 | 2,189 | 1.008× (0.984–1.027) | 0.999× (0.975–1.010) |
| cp314 | cpu-inline | 822 | 830 | 798 | 1.001× (0.976–1.008) | 1.034× (1.030–1.039) |
| cp314 | cpu-sync | 839 | 840 | 780 | 1.002× (0.997–1.005) | 1.075× (1.071–1.091) |
| cp314 | cpu-offload-auto | 2,720 | 2,773 | 2,768 | 0.977× (0.976–1.016) | 0.988× (0.979–0.994) |
| cp314t-gil1 | cpu-inline | 831 | 834 | 801 | 1.001× (0.996–1.003) | 1.029× (1.017–1.049) |
| cp314t-gil1 | cpu-sync | 835 | 835 | 761 | 0.998× (0.986–0.999) | 1.092× (1.073–1.110) |
| cp314t-gil1 | cpu-offload-auto | 2,804 | 2,738 | 2,829 | 1.024× (1.014–1.032) | 1.000× (0.978–1.019) |
| cp314t-gil0 | cpu-inline | 832 | 823 | 793 | 0.995× (0.991–1.021) | 1.042× (1.033–1.049) |
| cp314t-gil0 | cpu-sync | 2,794 | 2,837 | 2,658 | 1.024× (0.980–1.032) | 1.076× (1.051–1.086) |
| cp314t-gil0 | cpu-offload-auto | 2,750 | 2,775 | 2,786 | 0.991× (0.988–1.008) | 0.986× (0.978–1.007) |

## HTTP workloads and additional concurrency controls

| Phase | Python | Workload | Candidate req/s | RC6 req/s | FastAPI req/s | Candidate/RC6 paired | Candidate/FastAPI paired |
|---|---|---|---:|---:|---:|---|---|
| cpu-sync-c64 | cp313 | cpu-sync | 662 | 659 | 616 | 1.005× (1.004–1.008) | 1.076× (1.067–1.083) |
| cpu-sync-c64 | cp314 | cpu-sync | 837 | 839 | 775 | 0.999× (0.991–1.006) | 1.079× (1.073–1.106) |
| http-core | cp313 | json | 210,195 | 201,008 | 31,155 | 1.023× (1.019–1.049) | 6.546× (6.475–6.850) |
| http-core | cp313 | echo | 95,716 | 94,608 | 17,424 | 1.009× (1.003–1.012) | 5.504× (5.347–5.583) |
| http-core | cp313 | io | 55,876 | 56,517 | 26,180 | 0.997× (0.968–1.010) | 2.144× (2.126–2.166) |
| http-core | cp313 | fanout | 26,951 | 26,986 | 12,400 | 0.999× (0.989–1.001) | 2.174× (2.129–2.178) |
| http-core | cp313 | sync | 12,515 | 12,492 | 15,098 | 1.002× (0.993–1.008) | 0.829× (0.806–0.840) |
| http-core | cp313 | sync-stream | 26,257 | 26,727 | 2,263 | 0.984× (0.980–1.007) | 11.602× (11.488–11.929) |
| http-core | cp313 | stream | 22,192 | 21,607 | 8,271 | 1.020× (1.009–1.057) | 2.663× (2.637–2.765) |
| http-core | cp313 | bytes | 44,104 | 46,371 | 22,218 | 0.948× (0.918–0.970) | 1.985× (1.823–2.204) |
| http-core | cp313 | dependencies | 109,937 | 110,910 | 21,744 | 0.998× (0.991–1.023) | 5.133× (5.056–5.208) |
| http-models | cp313 | models_large | 42,807 | 42,649 | 6,730 | 1.004× (0.990–1.051) | 6.333× (6.306–6.360) |
| http-large-c16 | cp313 | echo-large | 3,180 | 3,176 | 1,037 | 1.001× (0.974–1.027) | 3.075× (2.998–3.081) |
| http-large-c64 | cp313 | echo-large | 3,102 | 3,078 | 1,027 | 1.003× (1.002–1.008) | 3.023× (2.985–3.024) |

The 256 KiB bytes response was 5.2% slower than RC6 by the paired median
(0.948×, range 0.918–0.970×). Standard 3.14 CPU-sync throughput was about
839 req/s in both builds.

## One-client controls

Eight separate one-round measurements check inline/synchronous CPU work with one outstanding request on standard 3.13/3.14, candidate and RC6. These are functional controls, excluded from three-round throughput comparisons.

8/8 controls eligible.

## Synthetic scheduler diagnostic

An owned worker follows a nominal 5 ms stop / 2 ms run cycle, with 16 CPU-sync clients. All 24 samples use pauses (six rounds across two builds and two modes); natural-load controls are in the main matrix. Actual signal timestamps, full response bodies, pause events and cleanup are retained. This is controlled scheduling diagnosis; its throughput does not establish server capacity.

| Mode | Pauses | Variant | Valid runs | HTTP 200 | HTTP 503 | Transport |
|---|---|---|---:|---:|---:|---:|
| cp313 | True | candidate | 6/6 | 2,537 | 0 | 0 |
| cp313 | True | rc6 | 6/6 | 2,749 | 2,036 | 0 |
| cp314 | True | candidate | 6/6 | 3,438 | 0 | 0 |
| cp314 | True | rc6 | 6/6 | 3,498 | 2,373 | 0 |

Observed non-200 bodies:

- HTTP 503: `{"detail":"Server event loop is overloaded"}` (4,409 replies)

## HTTP latency, CPU and memory

| Workload / concurrency | Candidate p99 ms | CPU us/success | Summed RSS MiB | Paired CPU/RC6 | Paired CPU/FastAPI |
|---|---:|---:|---:|---:|---:|
| json c64 | 0.467 | 4.69 | 85.5 | 0.981× | 0.152× |
| echo c64 | 0.981 | 10.35 | 85.8 | 0.992× | 0.182× |
| io c64 | 1.452 | 8.29 | 85.5 | 1.052× | 0.218× |
| fanout c64 | 6.591 | 36.85 | 87.1 | 1.003× | 0.459× |
| sync c64 | 6.222 | 44.89 | 87.9 | 1.004× | 0.565× |
| sync-stream c64 | 3.712 | 44.84 | 89.9 | 1.015× | 0.087× |
| stream c64 | 4.562 | 44.45 | 86.8 | 0.985× | 0.374× |
| bytes c64 | 2.149 | 22.34 | 85.8 | 1.048× | 0.502× |
| dependencies c64 | 0.916 | 9.00 | 85.6 | 1.001× | 0.193× |
| models_large c64 | 2.171 | 23.32 | 88.3 | 0.996× | 0.159× |
| echo-large c16 | 5.856 | 313.20 | 89.7 | 0.998× | 0.325× |
| echo-large c64 | 23.967 | 321.93 | 94.6 | 0.996× | 0.331× |

Summed RSS is a process-tree sum and can double-count shared pages. CPU ratios use paired rounds and the same error eligibility population as throughput.


## CPU workload resource cost

| Python mode | Workload | Candidate p99 ms | CPU us/success | Summed RSS MiB | Observed server CPU cores |
|---|---|---:|---:|---:|---:|
| cp313 | cpu-inline | 26.686 | 1,520.91 | 78.5 | 0.97 |
| cp313 | cpu-sync | 38.971 | 1,533.99 | 79.2 | 0.98 |
| cp313 | cpu-offload-auto | 8.302 | 2,011.52 | 227.2 | 4.27 |
| cp314 | cpu-inline | 21.671 | 1,215.64 | 83.6 | 0.97 |
| cp314 | cpu-sync | 30.795 | 1,205.66 | 84.5 | 0.98 |
| cp314 | cpu-offload-auto | 6.745 | 1,647.76 | 245.4 | 4.34 |
| cp314t-gil1 | cpu-inline | 21.904 | 1,202.31 | 97.3 | 0.97 |
| cp314t-gil1 | cpu-sync | 29.621 | 1,211.82 | 98.8 | 0.98 |
| cp314t-gil1 | cpu-offload-auto | 6.555 | 1,634.55 | 279.9 | 4.44 |
| cp314t-gil0 | cpu-inline | 21.597 | 1,200.21 | 97.3 | 0.97 |
| cp314t-gil0 | cpu-sync | 7.689 | 1,452.74 | 98.7 | 3.94 |
| cp314t-gil0 | cpu-offload-auto | 6.504 | 1,631.89 | 279.7 | 4.33 |

CPU slots are configured bounds, not OS isolation; observed CPU cores reflect this shared-host measurement window.


## Installed validation and causal regressions

- Full installed CPython 3.13 check passed: 315 Rust tests passed, 2 ignored; 1,187 Python tests ran, 25 skipped; 66 harness tests ran, 3 skipped.
- Focused installed suites: cp314: 169 passed / 2 skipped; cp314t-gil1: 150 passed / 21 skipped; cp314t-gil0: 150 passed / 21 skipped. Actual GIL states held before and after.
- The same 22 isolated adaptive-policy tests passed on the candidate; 9 failed on unchanged RC6 production code. Prefix/module hashes tie tested code to RC6 Git and candidate source.
- Prior candidate measurements are preserved separately and never pooled with this final build.


## Method and receipts

Rates are medians of three rounds; ratios are medians of within-round ratios. The eight c1 controls are reported separately. CPU cost includes drain/export monitoring; RSS sums process trees and can double-count shared memory. Individual warmup lengths are not recorded; warmup status/errors are checked. Full semantic/body probes run before and after each timed load, and measured SQL retains every status/length/latency.

Automatic CPU offload omits the Bessemer executor argument and checks the actual four-process backend. PYTHON_CPU_COUNT=4 sizes that pool; no affinity or CPU quota is imposed. The inline/sync reference app is frozen from main4965777. All measured Python routes are Tier 2. No broad 10×, formal M0, production billing, fixed-rate SLO, worker-scaling or long-soak qualification is implied.

The repository records [per-sample measurements](samples.csv) and the [summary with source and receipt hashes](summary.json). The full local run retained the following records.

- `benchmark-plan.json`: frozen source/build/environment and ordered plan.
- `installed-provenance.json`: installed/native/Python/wheel/source identity.
- `benchmark-results.json`, `samples.csv`, `analysis.json`: complete outcomes.
- `db/`, `logs/`: per-response SQL and server logs.
- `sql-audit.json`: independent SQL counter and percentile recomputation.
- `scheduler-diagnostics.json`: separate full-body synthetic scheduling controls.
