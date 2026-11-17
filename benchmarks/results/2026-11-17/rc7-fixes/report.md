# RC7 cancellation fix and benchmark qualification

Measured 2026-11-17 on the shared M5 Max: one server worker, two load-generator threads, matched source-built candidate and RC7, and FastAPI 0.162.2. This is a local shared-host comparison.

Across the main study and separate generic follow-up, 314 natural-load samples recorded 17,761,710 successful HTTP 200 replies. All Bessemer samples qualified; the three FastAPI high-concurrency upload samples had 16 timeouts and their comparison is withheld.

On CPython 3.13 the candidate measured 120,031 req/s for JSON, 65,331 for typed echo and 33,297 for 256-model responses. Within-round median ratios against FastAPI were 6.257×, 5.346× and 7.312× respectively. Performance versus RC7 was mixed: the 3.13 generic dependency case was 0.939× RC7; the FT GIL-off Request case was 1.180× RC7. Repeat ranges appear below.

Free-threading benefited CPU-bound sync work (2.567× standard 3.14 within the candidate), while the short generic Request/dependency cases measured 0.782×/0.608× standard 3.14. These results describe different workloads, not a general benefit from disabling the GIL.

## Changes tested

- Queued synchronous handlers and synchronous dependencies check whether their response is still awaited before starting. Lean routes, dependencies, Request injection and middleware use the same gate; cancellation keeps request capacity until cleanup finishes.
- The comparison harness drains deadline requests, retains status/error and warmup observations, reports successful replies per elapsed second, and withholds comparisons with failed or missing observations, client saturation or incomplete rounds. Missing CPU observations stay unavailable, stale results are replaced, and malformed rounds or inconsistent rate counters cannot qualify.

260 planned attempts, 260 measurements, 257 individually eligible; 13,541,989 successful measured replies. The main population contains 252 three-round jobs plus eight separate one-client controls. The independent SQL counter/latency audit passed all 260 measurements.

## Failures and qualification

| Variant | Measured/attempted | Measured HTTP statuses | Transport errors | Wrong lengths | Warmup non-200 | Warmup transport | Measured error reasons |
|---|---|---|---|---|---|---|---|
| Candidate | 88/88 | {"200": 6030042} | 0 | 0 | 0 | 0 | {} |
| RC7 | 88/88 | {"200": 5966117} | 0 | 0 | 0 | 0 | {} |
| FastAPI | 84/84 | {"200": 1545830} | 16 | 0 | 0 | 0 | {"timeout": 16} |

Lifecycle issues: 0. Any failed or missing round withholds the entire paired population, including its CPU comparison. † marks an unqualified diagnostic median; it is not used for a speedup claim.

| Phase | Python | Variant | Workload | Round | Measured failures | Warmup failures | Warmup issues |
|---|---|---|---|---|---|---|---|
| http-large-c64 | cp313 | fastapi | echo-large | 1 | 8 | 0 | — |
| http-large-c64 | cp313 | fastapi | echo-large | 2 | 3 | 0 | — |
| http-large-c64 | cp313 | fastapi | echo-large | 3 | 5 | 0 | — |

## HTTP and generic sync workloads — CPython 3.13

Each framework cell gives successful req/s / p99 ms. Paired ratios include the three-round range.

| Python | Workload | Candidate req/s / p99 | RC7 req/s / p99 | FastAPI req/s / p99 | Candidate/RC7 paired | Candidate/FastAPI paired |
|---|---|---|---|---|---|---|
| cp313 | json c64 | 120,031 / 1.220 | 127,898 / 1.279 | 18,720 / 6.969 | 1.011× (0.938–1.120) | 6.257× (5.692–7.076) |
| cp313 | echo c64 | 65,331 / 2.068 | 59,826 / 2.598 | 11,175 / 10.943 | 1.081× (1.018–1.092) | 5.346× (5.102–5.846) |
| cp313 | io c64 | 44,993 / 2.374 | 42,076 / 2.748 | 17,976 / 10.364 | 1.029× (1.022–1.088) | 2.409× (2.356–3.559) |
| cp313 | fanout c64 | 19,046 / 9.551 | 18,111 / 9.887 | 8,371 / 18.589 | 1.080× (0.887–1.111) | 2.272× (2.038–2.275) |
| cp313 | sync c64 | 12,857 / 5.907 | 12,754 / 6.443 | 8,187 / 14.714 | 0.999× (0.998–1.014) | 1.570× (1.395–1.878) |
| cp313 | sync-stream c64 | 12,991 / 10.092 | 13,744 / 8.083 | 1,573 / 66.718 | 0.968× (0.945–1.184) | 9.628× (8.257–9.706) |
| cp313 | stream c64 | 12,037 / 8.947 | 12,121 / 9.134 | 4,056 / 30.137 | 0.993× (0.957–1.190) | 2.967× (2.555–3.104) |
| cp313 | bytes c64 | 18,587 / 7.576 | 20,065 / 6.198 | 10,186 / 12.920 | 1.020× (0.926–1.067) | 2.116× (1.813–2.171) |
| cp313 | dependencies c64 | 67,670 / 2.113 | 64,642 / 2.175 | 16,747 / 11.527 | 0.994× (0.832–1.192) | 4.265× (3.210–5.799) |
| cp313 | models_large c64 | 33,297 / 3.374 | 32,542 / 3.415 | 4,310 / 26.486 | 1.023× (0.963–1.025) | 7.312× (7.083–7.726) |
| cp313 | echo-large c16 | 2,166 / 12.863 | 2,371 / 9.864 | 752 / 45.637 | 0.976× (0.914–0.999) | 2.996× (2.881–3.307) |
| cp313 | echo-large c64 | 2,555 / 35.694 | 2,580 / 34.851 | 758† / 156.949† | 0.987× (0.920–0.990) | withheld (transport_errors) |
| cp313 | generic-request c64 | 32,899 / 5.344 | 32,206 / 5.355 | 4,815 / 30.820 | 1.046× (0.899–1.112) | 6.809× (6.608–7.557) |
| cp313 | generic-dependency c64 | 35,887 / 5.678 | 40,241 / 3.838 | 4,850 / 29.461 | 0.939× (0.828–0.980) | 7.399× (7.299–7.996) |

The generic sync population uses four handler slots for each contender: one synchronous Request-taking handler and one async handler with a synchronous dependency. The other HTTP workloads keep the frozen reference app defaults.

## CPU workloads — four Python modes, c16

Each framework cell gives successful req/s / p99 ms. Paired ratios include the three-round range.

| Python | Workload | Candidate req/s / p99 | RC7 req/s / p99 | FastAPI req/s / p99 | Candidate/RC7 paired | Candidate/FastAPI paired |
|---|---|---|---|---|---|---|
| cp313 | cpu-inline c16 | 554 / 37.388 | 459 / 52.992 | 512 / 64.420 | 1.208× (0.972–1.224) | 1.083× (1.055–1.209) |
| cp314 | cpu-inline c16 | 714 / 31.330 | 682 / 30.985 | 646 / 51.996 | 1.026× (1.000–1.047) | 1.060× (1.058–1.224) |
| cp314t-gil1 | cpu-inline c16 | 696 / 32.965 | 722 / 30.213 | 650 / 51.312 | 0.963× (0.901–0.992) | 1.070× (1.059–1.076) |
| cp314t-gil0 | cpu-inline c16 | 713 / 28.414 | 716 / 31.927 | 660 / 48.683 | 1.005× (0.995–1.020) | 1.073× (1.060–1.112) |
| cp313 | cpu-sync c16 | 556 / 49.572 | 547 / 48.797 | 525 / 42.517 | 1.017× (1.011–1.018) | 1.085× (1.005–1.090) |
| cp314 | cpu-sync c16 | 666 / 44.334 | 685 / 39.636 | 593 / 40.371 | 0.972× (0.863–1.044) | 1.124× (1.122–1.146) |
| cp314t-gil1 | cpu-sync c16 | 694 / 41.936 | 696 / 40.915 | 611 / 37.722 | 0.996× (0.984–0.999) | 1.164× (1.060–1.165) |
| cp314t-gil0 | cpu-sync c16 | 1,742 / 13.238 | 1,988 / 12.096 | 1,654 / 14.654 | 0.879× (0.876–1.015) | 1.107× (1.053–1.354) |
| cp313 | cpu-offload-auto c16 | 1,702 / 20.421 | 1,718 / 19.384 | 1,749 / 16.738 | 0.951× (0.934–1.031) | 0.959× (0.917–1.102) |
| cp314 | cpu-offload-auto c16 | 2,092 / 16.739 | 2,291 / 11.868 | 2,318 / 9.663 | 0.963× (0.913–0.976) | 0.966× (0.902–0.970) |
| cp314t-gil1 | cpu-offload-auto c16 | 2,182 / 17.077 | 2,411 / 10.517 | 2,046 / 18.056 | 0.945× (0.905–0.996) | 0.974× (0.957–1.066) |
| cp314t-gil0 | cpu-offload-auto c16 | 2,306 / 12.689 | 2,359 / 9.765 | 2,316 / 12.425 | 1.016× (0.945–1.044) | 1.032× (0.996–1.038) |

## Additional CPU-sync c64 controls

Each framework cell gives successful req/s / p99 ms. Paired ratios include the three-round range.

| Python | Workload | Candidate req/s / p99 | RC7 req/s / p99 | FastAPI req/s / p99 | Candidate/RC7 paired | Candidate/FastAPI paired |
|---|---|---|---|---|---|---|
| cp313 | cpu-sync c64 | 564 / 181.686 | 562 / 174.488 | 503 / 155.148 | 1.003× (0.997–1.033) | 1.128× (1.028–1.152) |
| cp314 | cpu-sync c64 | 722 / 150.133 | 720 / 151.606 | 658 / 125.366 | 1.003× (0.994–1.004) | 1.123× (1.093–1.214) |

## HTTP and generic sync resource cost

Framework cells give CPU µs/success / summed RSS MiB. CPU ratios use the same qualified rounds as throughput.

| Python | Workload | Candidate CPU / RSS | RC7 CPU / RSS | FastAPI CPU / RSS | Candidate/RC7 CPU paired | Candidate/FastAPI CPU paired |
|---|---|---|---|---|---|---|
| cp313 | json c64 | 7.70 / 86.2 | 7.20 / 86.3 | 48.38 / 54.7 | 0.990× (0.881–1.069) | 0.161× (0.142–0.177) |
| cp313 | echo c64 | 14.03 / 86.9 | 14.91 / 87.0 | 82.52 / 54.9 | 0.941× (0.937–0.987) | 0.187× (0.170–0.196) |
| cp313 | io c64 | 12.28 / 86.2 | 13.32 / 86.6 | 52.14 / 55.5 | 0.940× (0.922–0.964) | 0.234× (0.170–0.241) |
| cp313 | fanout c64 | 49.01 / 88.1 | 52.28 / 88.1 | 112.54 / 57.4 | 0.924× (0.906–1.112) | 0.440× (0.435–0.486) |
| cp313 | sync c64 | 39.97 / 89.0 | 38.11 / 88.8 | 126.57 / 58.1 | 1.064× (0.927–1.107) | 0.316× (0.289–0.319) |
| cp313 | sync-stream c64 | 77.86 / 90.9 | 75.59 / 90.8 | 676.72 / 59.3 | 1.027× (0.941–1.030) | 0.103× (0.102–0.115) |
| cp313 | stream c64 | 74.14 / 87.7 | 74.20 / 87.9 | 210.97 / 57.5 | 0.999× (0.851–1.021) | 0.351× (0.324–0.393) |
| cp313 | bytes c64 | 40.37 / 86.2 | 38.47 / 86.4 | 77.17 / 54.9 | 0.988× (0.939–1.057) | 0.471× (0.469–0.523) |
| cp313 | dependencies c64 | 13.52 / 86.5 | 14.15 / 86.7 | 56.62 / 55.7 | 1.007× (0.847–1.183) | 0.226× (0.169–0.296) |
| cp313 | models_large c64 | 28.23 / 89.9 | 28.89 / 89.8 | 210.31 / 55.2 | 1.036× (0.977–1.046) | 0.138× (0.134–0.144) |
| cp313 | echo-large c16 | 407.84 / 90.7 | 382.50 / 91.1 | 1,211.63 / 58.3 | 1.012× (1.002–1.066) | 0.329× (0.308–0.337) |
| cp313 | echo-large c64 | 366.30 / 97.2 | 361.43 / 96.8 | 1,192.08† / 66.4† | 1.013× (1.013–1.063) | withheld (transport_errors) |
| cp313 | generic-request c64 | 33.34 / 81.7 | 33.53 / 81.3 | 181.61 / 56.9 | 1.003× (0.967–1.082) | 0.187× (0.180–0.187) |
| cp313 | generic-dependency c64 | 28.26 / 81.3 | 27.09 / 80.8 | 178.79 / 56.5 | 1.027× (0.975–1.077) | 0.149× (0.148–0.158) |

CPU includes sampler drain/export time. RSS sums process trees and can count shared pages more than once; unavailable USS is not reported as zero.

## CPU workload resource cost

Framework cells give CPU µs/success / summed RSS MiB. CPU ratios use the same qualified rounds as throughput.

| Python | Workload | Candidate CPU / RSS | RC7 CPU / RSS | FastAPI CPU / RSS | Candidate/RC7 CPU paired | Candidate/FastAPI CPU paired |
|---|---|---|---|---|---|---|
| cp313 | cpu-inline c16 | 1,739.22 / 79.6 | 1,979.20 / 78.6 | 1,873.64 / 72.0 | 0.870× (0.862–1.021) | 0.930× (0.845–0.952) |
| cp314 | cpu-inline c16 | 1,341.49 / 84.9 | 1,382.61 / 84.9 | 1,460.84 / 81.4 | 0.974× (0.967–0.998) | 0.942× (0.839–0.949) |
| cp314t-gil1 | cpu-inline c16 | 1,362.70 / 97.0 | 1,338.42 / 96.9 | 1,446.13 / 95.0 | 1.024× (1.007–1.067) | 0.942× (0.936–0.947) |
| cp314t-gil0 | cpu-inline c16 | 1,353.08 / 96.7 | 1,359.94 / 96.9 | 1,432.39 / 95.1 | 0.995× (0.977–0.998) | 0.934× (0.919–0.945) |
| cp313 | cpu-sync c16 | 1,727.97 / 80.2 | 1,774.26 / 80.6 | 1,841.38 / 72.7 | 0.986× (0.974–0.992) | 0.926× (0.925–0.990) |
| cp314 | cpu-sync c16 | 1,422.87 / 86.2 | 1,396.95 / 86.2 | 1,588.32 / 81.5 | 1.000× (0.970–1.116) | 0.902× (0.896–0.956) |
| cp314t-gil1 | cpu-sync c16 | 1,373.07 / 98.2 | 1,377.89 / 98.7 | 1,548.68 / 98.5 | 0.999× (0.997–1.008) | 0.885× (0.885–0.947) |
| cp314t-gil0 | cpu-sync c16 | 2,185.11 / 98.1 | 1,963.04 / 98.5 | 2,313.29 / 98.5 | 1.113× (0.991–1.160) | 0.945× (0.757–1.013) |
| cp313 | cpu-offload-auto c16 | 2,434.34 / 230.8 | 2,440.24 / 231.0 | 2,495.15 / 151.8 | 1.017× (0.998–1.056) | 1.006× (0.924–1.041) |
| cp314 | cpu-offload-auto c16 | 1,978.25 / 250.3 | 1,922.30 / 250.4 | 1,957.06 / 184.6 | 1.034× (1.028–1.053) | 1.011× (1.004–1.019) |
| cp314t-gil1 | cpu-offload-auto c16 | 1,946.90 / 280.7 | 1,881.06 / 281.0 | 2,058.89 / 211.1 | 1.024× (0.996–1.035) | 0.992× (0.946–1.011) |
| cp314t-gil0 | cpu-offload-auto c16 | 1,852.24 / 280.7 | 1,879.79 / 280.7 | 1,970.41 / 211.0 | 0.985× (0.963–1.015) | 0.940× (0.938–0.948) |
| cp313 | cpu-sync c64 | 1,723.78 / 81.3 | 1,733.78 / 81.2 | 1,892.71 / 73.4 | 0.996× (0.979–1.001) | 0.904× (0.893–0.967) |
| cp314 | cpu-sync c64 | 1,338.67 / 86.6 | 1,340.28 / 87.2 | 1,470.81 / 82.8 | 0.999× (0.998–1.008) | 0.905× (0.853–0.916) |

CPU includes sampler drain/export time. RSS sums process trees and can count shared pages more than once; unavailable USS is not reported as zero.

## CPU-sync mode scaling

Paired throughput ratios within each framework, from the CPU-sync c16 population.

| Variant | Modes | Paired throughput ratio (range) |
|---|---|---|
| Candidate | cp314t-gil1/cp314 | 0.972× (0.972–1.244) |
| Candidate | cp314t-gil0/cp314 | 2.567× (2.441–3.776) |
| Candidate | cp314t-gil0/cp314t-gil1 | 2.640× (2.512–3.035) |
| RC7 | cp314t-gil1/cp314 | 1.018× (0.960–1.074) |
| RC7 | cp314t-gil0/cp314 | 2.908× (2.838–3.210) |
| RC7 | cp314t-gil0/cp314t-gil1 | 2.955× (2.855–2.990) |
| FastAPI | cp314t-gil1/cp314 | 1.031× (0.937–1.224) |
| FastAPI | cp314t-gil0/cp314 | 2.606× (2.599–3.196) |
| FastAPI | cp314t-gil0/cp314t-gil1 | 2.612× (2.527–2.775) |

## One-client functional controls

Eight one-round c1 samples check CPU-inline and CPU-sync on standard CPython 3.13/3.14 for the candidate and RC7. They are excluded from three-round speedup claims.

| Python | Workload | Variant | Req/s | p99 ms | Functional qualification |
|---|---|---|---|---|---|
| cp313 | cpu-inline | Candidate | 415.5 | 4.483 | qualified |
| cp313 | cpu-inline | RC7 | 433.5 | 4.137 | qualified |
| cp313 | cpu-sync | Candidate | 460.0 | 3.239 | qualified |
| cp313 | cpu-sync | RC7 | 389.5 | 6.851 | qualified |
| cp314 | cpu-inline | Candidate | 575.4 | 2.826 | qualified |
| cp314 | cpu-inline | RC7 | 633.0 | 2.424 | qualified |
| cp314 | cpu-sync | Candidate | 656.0 | 2.474 | qualified |
| cp314 | cpu-sync | RC7 | 601.0 | 2.894 | qualified |

## Installed validation

Code head `ab9f47ab5e13a9f26f95d69ded8a476f36b7623c` has 103 runtime source files matched to the installed candidate for each ABI.

- Full CPython 3.13 installed gate: 330 Rust passed, 2 ignored; 1,195 Python tests ran, 25 skipped; 83 harness tests ran, 3 skipped.
- Eight generic cancellation assertions fail against the published RC7 wheel. All six regression methods, covering ten route/cancellation combinations, pass on the candidate in every focused mode below.
- CPython 3.12 with Pydantic 2.10.6 is a functional lane only; it is not part of the timed matrix.

| Mode | Ran | Passed | Skipped | Actual GIL | Pydantic |
|---|---|---|---|---|---|
| cp312 | 178 | 176 | 2 | on | 2.10.6 |
| cp313 | 178 | 176 | 2 | on | 2.14.3 |
| cp314 | 178 | 176 | 2 | on | 2.14.3 |
| cp314t-gil1 | 178 | 157 | 21 | on | 2.14.3 |
| cp314t-gil0 | 178 | 157 | 21 | off | 2.14.3 |

## Separate paused-worker diagnostic

Twenty-four runs pause the owned worker for nominal 5 ms / run 2 ms, with 16 CPU-sync clients, six rounds across CPython 3.13 and standard 3.14. Signal events, full response bodies, runtime identity and cleanup are retained. These scheduling probes are separate from the natural-load matrix and do not measure capacity.

| Mode | Variant | Valid runs | HTTP 200 | HTTP 503 | Transport errors |
|---|---|---|---|---|---|
| cp313 | Candidate | 6/6 | 2,917 | 0 | 0 |
| cp313 | RC7 | 6/6 | 2,860 | 5 | 0 |
| cp314 | Candidate | 6/6 | 3,875 | 0 | 0 |
| cp314 | RC7 | 6/6 | 3,775 | 0 | 0 |

Candidate: 6,792 successful replies, 0 overload replies. RC7: 6,635 successful replies, 5 overload replies. These isolated refusals do not establish a candidate throughput or admission improvement.

## CI evidence

Completed Performance workflow log evidence captured at 2026-11-17T12:27:40.125Z, code head `ab9f47ab5e13a9f26f95d69ded8a476f36b7623c`: 72 fresh base/head rows across six Python jobs; 0 measured transport errors, 0 measured non-200 responses, 0 warmup transport errors and 0 warmup non-200 responses. All commands drain ongoing deadline requests.

40 rows qualified; 32 were ineligible, including 32 client-bound rows. The committed formatter recomputes 20 qualified and 16 withheld comparisons from retained base/head logs. This verifies formatter output from those populations; the rendered GitHub step-summary body was not read.

| Python job | Rows | Eligible rows | Client-bound rows | Qualified pairs | Withheld pairs |
|---|---|---|---|---|---|
| [3.12](https://github.com/CodeHalwell/bessemer/actions/runs/44079650150/job/134026593422) | 12 | 4 | 8 | 2 | 4 |
| [3.13](https://github.com/CodeHalwell/bessemer/actions/runs/44079650150/job/134026593336) | 12 | 6 | 6 | 3 | 3 |
| [3.14](https://github.com/CodeHalwell/bessemer/actions/runs/44079650150/job/134026593318) | 12 | 8 | 4 | 4 | 2 |
| [3.14t](https://github.com/CodeHalwell/bessemer/actions/runs/44079650150/job/134026593751) | 12 | 6 | 6 | 3 | 3 |
| [3.15](https://github.com/CodeHalwell/bessemer/actions/runs/44079650150/job/134026592878) | 12 | 8 | 4 | 4 | 2 |
| [3.15t](https://github.com/CodeHalwell/bessemer/actions/runs/44079650150/job/134026593500) | 12 | 8 | 4 | 4 | 2 |

Overall check snapshot at 2026-11-17T12:26:08.924Z, head `ab9f47ab5e13a9f26f95d69ded8a476f36b7623c`: 32/32 latest checks passed, 0 pending, 0 failed. This records that timestamp rather than asserting a later merge state.

## Separate generic CPython 3.14 supplement

A separately planned and independently audited 54-sample run adds Request-taking sync handlers and async handlers with synchronous dependencies on standard CPython 3.14 and free-threaded 3.14 with the GIL on/off. Each contender has four sync slots. It is not pooled with the main 260 samples and does not establish multi-version coverage for the other HTTP workloads.

## Failures and qualification

| Variant | Measured/attempted | Measured HTTP statuses | Transport errors | Wrong lengths | Warmup non-200 | Warmup transport | Measured error reasons |
|---|---|---|---|---|---|---|---|
| Candidate | 18/18 | {"200": 1987557} | 0 | 0 | 0 | 0 | {} |
| RC7 | 18/18 | {"200": 1950991} | 0 | 0 | 0 | 0 | {} |
| FastAPI | 18/18 | {"200": 281173} | 0 | 0 | 0 | 0 | {} |

Lifecycle issues: 0. Any failed or missing round withholds the entire paired population, including its CPU comparison. † marks an unqualified diagnostic median; it is not used for a speedup claim.

## Separate generic mode throughput and latency

Each framework cell gives successful req/s / p99 ms. Paired ratios include the three-round range.

| Python | Workload | Candidate req/s / p99 | RC7 req/s / p99 | FastAPI req/s / p99 | Candidate/RC7 paired | Candidate/FastAPI paired |
|---|---|---|---|---|---|---|
| cp314 | generic-request c64 | 29,885 / 6.456 | 28,868 / 6.091 | 4,665 / 29.300 | 0.971× (0.918–1.035) | 6.329× (5.607–9.112) |
| cp314t-gil1 | generic-request c64 | 29,596 / 4.131 | 30,114 / 4.237 | 3,014 / 42.413 | 0.983× (0.919–1.007) | 9.821× (7.334–13.570) |
| cp314t-gil0 | generic-request c64 | 23,362 / 10.619 | 19,800 / 11.668 | 4,886 / 69.001 | 1.180× (1.170–1.270) | 4.782× (4.658–5.383) |
| cp314 | generic-dependency c64 | 29,857 / 6.483 | 29,018 / 7.124 | 3,628 / 68.423 | 1.073× (1.029–1.078) | 7.753× (7.384–8.229) |
| cp314t-gil1 | generic-dependency c64 | 25,810 / 7.877 | 26,924 / 8.238 | 2,143 / 222.453 | 0.959× (0.954–1.049) | 12.041× (10.268–17.350) |
| cp314t-gil0 | generic-dependency c64 | 20,835 / 13.994 | 18,822 / 21.074 | 3,507 / 105.688 | 1.107× (0.758–1.206) | 5.181× (3.563–6.454) |

## Separate generic mode CPU and memory

Framework cells give CPU µs/success / summed RSS MiB. CPU ratios use the same qualified rounds as throughput.

| Python | Workload | Candidate CPU / RSS | RC7 CPU / RSS | FastAPI CPU / RSS | Candidate/RC7 CPU paired | Candidate/FastAPI CPU paired |
|---|---|---|---|---|---|---|
| cp314 | generic-request c64 | 38.87 / 71.5 | 39.16 / 74.3 | 200.27 / 61.8 | 1.012× (0.993–1.084) | 0.193× (0.159–0.194) |
| cp314t-gil1 | generic-request c64 | 40.55 / 92.0 | 40.40 / 97.1 | 253.76 / 69.8 | 1.004× (1.004–1.050) | 0.160× (0.142–0.188) |
| cp314t-gil0 | generic-request c64 | 52.28 / 87.7 | 56.37 / 80.3 | 237.72 / 76.6 | 0.921× (0.893–0.927) | 0.217× (0.205–0.220) |
| cp314 | generic-dependency c64 | 31.72 / 72.8 | 31.61 / 74.9 | 209.06 / 61.8 | 0.999× (0.947–1.098) | 0.157× (0.152–0.163) |
| cp314t-gil1 | generic-dependency c64 | 37.00 / 78.9 | 34.71 / 68.7 | 263.80 / 67.9 | 1.035× (0.976–1.089) | 0.141× (0.118–0.154) |
| cp314t-gil0 | generic-dependency c64 | 45.86 / 66.8 | 43.68 / 79.4 | 239.18 / 67.3 | 1.040× (1.020–1.090) | 0.186× (0.178–0.226) |

CPU includes sampler drain/export time. RSS sums process trees and can count shared pages more than once; unavailable USS is not reported as zero.

## Method and limits

HTTP workloads cover small JSON, a typed 20-item echo, 1 ms I/O, fanout16, synchronous sleep, sync/async streams, 256 KiB bytes, async yield dependencies, 256-model responses and 1,000-item typed uploads. The upload body is 69,403 bytes. Generic sync cases use the matched four-slot pool.

Rates are medians of three rounds; ratios are medians of within-round ratios, with their full three-round range. An error in any contributing round withholds the entire paired population. Raw diagnostic medians remain visible with †. Full semantic/body probes run before and after each timed load; measured SQL retains every status, body length and latency. Warmup status and transport errors are checked; individual warmup body lengths are not recorded.

Candidate and released RC7 (`a106a566d6d27e7167a0b777226771e488147c94`) wheels were built afresh for all timed ABIs with Rust 1.92 and release strip=none. These rates are not measurements of the published PyPI binaries. Dependencies are matched, including FastAPI 0.162.2, Pydantic 2.14.3 and pydantic-core 2.50.4. Python 3.13.18 and standard/free-threaded 3.14.7 run with JIT disabled. The free-threaded GIL-off lane leaves PYTHON_GIL unset and checks that imports do not re-enable the GIL.

CPU-inline and CPU-sync retain the frozen historical reference app; App()-default CPU offload checks the actual four-process backend, with an equivalent four-process FastAPI backend. PYTHON_CPU_COUNT=4 sizes those pools; it does not impose CPU affinity or an OS quota. Sync CPU work and the generic supplement use matched four-slot pools. One worker runs on an unpinned shared host. Local CPU/request and RSS describe this measurement, without establishing production scaling or billing savings.

All Python routes remain Tier 2. This report does not establish general 10× FastAPI throughput, the formal M0 target, fixed-rate SLO acceptance, worker-scaling, startup, long-soak or production-cost acceptance.

## Receipts

This committed folder contains the report, compact summary, per-sample CSVs, protocols, dependency pins, independent SQL audit receipts and CI qualification. The complete execution harness, raw databases, server logs, full native provenance and detailed test receipts are local artifacts outside Git:

- Main: `/Users/danielhalwell/Library/Caches/bessemer-bench/rc7-fixes-2026-11-17/`
- Separate generic follow-up: `/Users/danielhalwell/Library/Caches/bessemer-bench/rc7-generic-ft-2026-11-17/`

The following names refer to those complete local directories; the committed subset is not a standalone reproduction harness.

- `benchmark-plan.json`: frozen source/build/environment identities and ordered jobs.
- `installed-provenance.json`, `validated-code-head.json`: installed wheel/source/native identity and tested code commit.
- `benchmark-results.json`, `analysis.json`, `samples.csv`: complete outcomes, qualification and paired calculations.
- `sql-audit.json`: independent per-response SQL counter/percentile recomputation, bound to the exact results and plan hashes.
- `db/`, `logs/`: raw measurement databases and server logs.
- `scheduler-diagnostics.json`, `scheduler-diagnostic-gate.json`: separate paused-worker observations and qualification.
- `full-installed-check-cp313-execution.json`, `focused-*.json`, `cancellation-regression-proof.json`: installed validation and baseline regression evidence.
- `ci-monitor/performance-qualification.json`: complete retained CI throughput log/formatter qualification, when available.
