# JSON and bytes performance benchmark

Completed 395 fresh samples; 392 qualified. Measured 42,046,247 successful HTTP responses. All raw failures are retained and any failed comparison is withheld.

Candidate is `a4d5b79 base + uncommitted CPU accounting and JSON/bytes performance changes`, identified by its frozen source/native/wheel manifests and working-tree diff hash. The module version 1.0.0rc7 and HEAD alone do not identify candidate code. Baselines are exact main `a4d5b79a011aae6eb52e8ea891cd1a232cba3ebd` and FastAPI 0.162.2. Main artifacts are read-only PR131 candidate source wheels: all 103 original runtime/build source hashes match main. Candidate source wheels use the same Rust 1.92.0 release profile with strip=none. Published-wheel throughput is not claimed.

This study evaluates CPU admission accounting and JSON/bytes performance changes. Correctness, targeted JSON/bytes comparisons and paused scheduling controls are separate from the natural matrix. The full matrix records rates, p99, CPU per successful reply and memory across the existing workloads; a throughput increase is not assumed.

43 candidate/main workload comparisons qualified for all three paired rounds. Their per-workload median throughput ratios span 0.924–1.059×; this is a range across distinct workloads, not a pooled speed score. All failures and mixed results remain visible.

These configurations measured lower candidate throughput in all three paired rounds. No cause is assigned.

| Case (phase:mode:workload) | Paired candidate/main RPS | Paired candidate/main CPU/success |
|---|---|---|
| cpu-sync-c64:cp313:cpu-sync | 0.983× (0.971–0.998) | 1.018× (1.013–1.024) |
| http-generic-sync:cp314t-gil1:generic-request | 0.965× (0.961–0.988) | 1.032× (1.014–1.038) |
| http-ft:cp314:echo | 0.970× (0.966–0.975) | 1.029× (1.022–1.030) |
| http-ft:cp314:models_large | 0.987× (0.908–0.999) | 1.016× (0.998–1.072) |

The study has 387 three-round performance samples and eight separate one-round functional controls. All 43 performance case groups use every contender position once across three rounds. Python 3.13.18, standard 3.14.7 and free-threaded 3.14.7 with GIL on/off are recorded explicitly. JIT is disabled and checked before/after; GIL-off is never forced past an incompatible import.

## Core HTTP on Python 3.13

Framework cells give three-round medians of successful req/s / p99 ms. Paired columns give the median and full range of within-round ratios; these need not equal ratios of the displayed medians.

| Python | Workload | Candidate req/s / p99 | Main a4d5b79 req/s / p99 | FastAPI req/s / p99 | Candidate/Main a4d5b79 paired | Candidate/FastAPI paired |
|---|---|---|---|---|---|---|
| cp313 | json c64 | 130,900 / 1.053 | 129,806 / 1.044 | 21,350 / 6.108 | 1.018× (0.942–1.032) | 6.107× (6.054–6.678) |
| cp313 | echo c64 | 69,401 / 1.925 | 70,911 / 1.864 | 12,180 / 10.200 | 1.016× (0.915–1.094) | 5.587× (5.527–6.037) |
| cp313 | io c64 | 47,516 / 2.228 | 47,322 / 2.295 | 21,565 / 9.142 | 1.008× (0.998–1.020) | 2.199× (2.183–2.237) |
| cp313 | fanout c64 | 21,390 / 8.103 | 20,979 / 8.412 | 9,736 / 16.365 | 1.033× (0.973–1.038) | 2.148× (2.139–2.197) |
| cp313 | sync c64 | 12,893 / 5.614 | 12,907 / 5.676 | 11,259 / 11.094 | 1.001× (0.999–1.002) | 1.144× (1.144–1.319) |
| cp313 | sync-stream c64 | 16,851 / 6.674 | 17,833 / 6.451 | 1,581 / 64.970 | 0.924× (0.870–1.022) | 10.748× (8.817–11.531) |
| cp313 | stream c64 | 13,466 / 8.184 | 13,164 / 8.298 | 5,349 / 20.869 | 0.952× (0.928–1.070) | 2.518× (2.326–2.519) |
| cp313 | bytes c64 | 29,639 / 3.742 | 29,650 / 3.801 | 12,953 / 10.084 | 1.004× (0.949–1.088) | 2.298× (2.068–2.380) |
| cp313 | dependencies c64 | 77,834 / 1.916 | 79,611 / 1.838 | 18,188 / 10.830 | 0.962× (0.893–1.001) | 4.280× (4.064–4.330) |

## Core HTTP on Python 3.13 CPU and memory

Framework cells give three-round medians of CPU µs/success / summed RSS MiB. CPU ratios use the same qualified rounds as throughput and give the median and full range of within-round ratios; these need not equal ratios of the displayed medians.

| Python | Workload | Candidate CPU / RSS | Main a4d5b79 CPU / RSS | FastAPI CPU / RSS | Candidate/Main a4d5b79 CPU paired | Candidate/FastAPI CPU paired |
|---|---|---|---|---|---|---|
| cp313 | json c64 | 7.08 / 86.1 | 7.11 / 79.4 | 43.19 / 54.8 | 0.987× (0.964–1.064) | 0.165× (0.150–0.166) |
| cp313 | echo c64 | 13.43 / 86.4 | 13.19 / 86.3 | 75.83 / 55.3 | 0.982× (0.916–1.079) | 0.181× (0.167–0.181) |
| cp313 | io c64 | 10.74 / 86.0 | 11.05 / 79.1 | 44.39 / 55.2 | 0.984× (0.945–1.012) | 0.242× (0.235–0.246) |
| cp313 | fanout c64 | 44.19 / 87.6 | 45.65 / 81.1 | 98.03 / 57.1 | 0.968× (0.957–1.028) | 0.461× (0.451–0.464) |
| cp313 | sync c64 | 37.19 / 81.6 | 36.64 / 81.2 | 98.96 / 58.0 | 0.997× (0.991–1.015) | 0.376× (0.319–0.377) |
| cp313 | sync-stream c64 | 65.07 / 90.3 | 61.70 / 90.5 | 660.77 / 59.0 | 1.072× (0.984–1.111) | 0.094× (0.092–0.110) |
| cp313 | stream c64 | 68.03 / 87.2 | 70.67 / 80.7 | 171.37 / 57.2 | 1.056× (0.930–1.068) | 0.397× (0.391–0.430) |
| cp313 | bytes c64 | 28.28 / 86.7 | 28.13 / 79.0 | 63.12 / 54.8 | 1.001× (0.927–1.053) | 0.446× (0.430–0.491) |
| cp313 | dependencies c64 | 11.87 / 83.0 | 11.63 / 86.1 | 52.76 / 55.6 | 1.036× (0.996–1.111) | 0.225× (0.221–0.238) |

CPU includes sampler drain/export time. RSS sums process trees and can count shared pages more than once; unavailable USS is not reported as zero.

## 256-dict response models on Python 3.13

Framework cells give three-round medians of successful req/s / p99 ms. Paired columns give the median and full range of within-round ratios; these need not equal ratios of the displayed medians.

| Python | Workload | Candidate req/s / p99 | Main a4d5b79 req/s / p99 | FastAPI req/s / p99 | Candidate/Main a4d5b79 paired | Candidate/FastAPI paired |
|---|---|---|---|---|---|---|
| cp313 | models_large c64 | 36,960 / 3.013 | 36,346 / 3.029 | 5,438 / 21.936 | 1.008× (1.006–1.044) | 6.722× (6.687–6.887) |

## 256-dict response models on Python 3.13 CPU and memory

Framework cells give three-round medians of CPU µs/success / summed RSS MiB. CPU ratios use the same qualified rounds as throughput and give the median and full range of within-round ratios; these need not equal ratios of the displayed medians.

| Python | Workload | Candidate CPU / RSS | Main a4d5b79 CPU / RSS | FastAPI CPU / RSS | Candidate/Main a4d5b79 CPU paired | Candidate/FastAPI CPU paired |
|---|---|---|---|---|---|---|
| cp313 | models_large c64 | 26.04 / 89.2 | 26.21 / 88.8 | 172.33 / 54.9 | 0.994× (0.968–0.995) | 0.151× (0.149–0.153) |

CPU includes sampler drain/export time. RSS sums process trees and can count shared pages more than once; unavailable USS is not reported as zero.

## 1000-item typed uploads c16

Framework cells give three-round medians of successful req/s / p99 ms. Paired columns give the median and full range of within-round ratios; these need not equal ratios of the displayed medians.

| Python | Workload | Candidate req/s / p99 | Main a4d5b79 req/s / p99 | FastAPI req/s / p99 | Candidate/Main a4d5b79 paired | Candidate/FastAPI paired |
|---|---|---|---|---|---|---|
| cp313 | echo-large c16 | 2,772 / 7.896 | 2,730 / 8.249 | 833 / 41.332 | 1.030× (0.976–1.044) | 3.375× (3.264–3.517) |

## 1000-item typed uploads c16 CPU and memory

Framework cells give three-round medians of CPU µs/success / summed RSS MiB. CPU ratios use the same qualified rounds as throughput and give the median and full range of within-round ratios; these need not equal ratios of the displayed medians.

| Python | Workload | Candidate CPU / RSS | Main a4d5b79 CPU / RSS | FastAPI CPU / RSS | Candidate/Main a4d5b79 CPU paired | Candidate/FastAPI CPU paired |
|---|---|---|---|---|---|---|
| cp313 | echo-large c16 | 340.30 / 83.1 | 345.61 / 83.2 | 1,122.60 / 58.4 | 0.978× (0.957–1.019) | 0.301× (0.291–0.310) |

CPU includes sampler drain/export time. RSS sums process trees and can count shared pages more than once; unavailable USS is not reported as zero.

## 1000-item typed uploads c64

Framework cells give three-round medians of successful req/s / p99 ms. Paired columns give the median and full range of within-round ratios; these need not equal ratios of the displayed medians.

| Python | Workload | Candidate req/s / p99 | Main a4d5b79 req/s / p99 | FastAPI req/s / p99 | Candidate/Main a4d5b79 paired | Candidate/FastAPI paired |
|---|---|---|---|---|---|---|
| cp313 | echo-large c64 | 2,687 / 32.686 | 2,736 / 31.910 | 795† / 142.545† | 0.982× (0.961–1.025) | withheld (transport_errors) |

## 1000-item typed uploads c64 CPU and memory

Framework cells give three-round medians of CPU µs/success / summed RSS MiB. CPU ratios use the same qualified rounds as throughput and give the median and full range of within-round ratios; these need not equal ratios of the displayed medians.

| Python | Workload | Candidate CPU / RSS | Main a4d5b79 CPU / RSS | FastAPI CPU / RSS | Candidate/Main a4d5b79 CPU paired | Candidate/FastAPI CPU paired |
|---|---|---|---|---|---|---|
| cp313 | echo-large c64 | 349.62 / 89.2 | 345.13 / 89.6 | 1,152.01† / 65.6† | 1.012× (0.985–1.029) | withheld (transport_errors) |

CPU includes sampler drain/export time. RSS sums process trees and can count shared pages more than once; unavailable USS is not reported as zero.

## Generic sync paths across all modes

Framework cells give three-round medians of successful req/s / p99 ms. Paired columns give the median and full range of within-round ratios; these need not equal ratios of the displayed medians.

| Python | Workload | Candidate req/s / p99 | Main a4d5b79 req/s / p99 | FastAPI req/s / p99 | Candidate/Main a4d5b79 paired | Candidate/FastAPI paired |
|---|---|---|---|---|---|---|
| cp313 | generic-request c64 | 52,143 / 2.624 | 52,614 / 2.639 | 9,220 / 13.753 | 0.980× (0.946–1.031) | 5.655× (5.350–6.010) |
| cp314 | generic-request c64 | 51,564 / 2.691 | 53,480 / 2.580 | 8,537 / 15.194 | 0.946× (0.935–1.112) | 6.035× (5.857–6.619) |
| cp314t-gil1 | generic-request c64 | 48,738 / 2.821 | 49,322 / 2.731 | 6,579 / 20.515 | 0.965× (0.961–0.988) | 7.109× (6.817–7.814) |
| cp314t-gil0 | generic-request c64 | 44,302 / 2.997 | 43,530 / 3.010 | 11,678 / 10.315 | 1.018× (0.949–1.080) | 3.907× (3.794–3.984) |
| cp313 | generic-dependency c64 | 59,986 / 2.400 | 59,163 / 2.311 | 8,760 / 13.228 | 0.977× (0.922–1.082) | 6.987× (6.608–7.305) |
| cp314 | generic-dependency c64 | 64,241 / 2.316 | 64,101 / 2.348 | 9,307 / 14.319 | 1.002× (0.996–1.134) | 6.926× (6.896–7.478) |
| cp314t-gil1 | generic-dependency c64 | 60,420 / 2.283 | 57,139 / 2.388 | 7,191 / 19.045 | 1.059× (1.015–1.079) | 8.402× (7.972–9.286) |
| cp314t-gil0 | generic-dependency c64 | 53,193 / 2.532 | 53,264 / 2.517 | 9,875 / 11.817 | 1.001× (0.911–1.102) | 5.387× (4.707–5.529) |

## Generic sync paths across all modes CPU and memory

Framework cells give three-round medians of CPU µs/success / summed RSS MiB. CPU ratios use the same qualified rounds as throughput and give the median and full range of within-round ratios; these need not equal ratios of the displayed medians.

| Python | Workload | Candidate CPU / RSS | Main a4d5b79 CPU / RSS | FastAPI CPU / RSS | Candidate/Main a4d5b79 CPU paired | Candidate/FastAPI CPU paired |
|---|---|---|---|---|---|---|
| cp313 | generic-request c64 | 24.71 / 73.2 | 24.30 / 76.7 | 114.53 / 56.2 | 1.021× (0.980–1.058) | 0.216× (0.204–0.224) |
| cp314 | generic-request c64 | 24.81 / 79.3 | 24.09 / 86.3 | 122.06 / 61.8 | 1.045× (0.895–1.070) | 0.205× (0.187–0.211) |
| cp314t-gil1 | generic-request c64 | 26.48 / 101.7 | 26.11 / 101.5 | 141.50 / 76.4 | 1.032× (1.014–1.038) | 0.194× (0.179–0.197) |
| cp314t-gil0 | generic-request c64 | 34.91 / 101.3 | 35.38 / 101.0 | 126.13 / 76.4 | 0.987× (0.947–1.030) | 0.267× (0.263–0.277) |
| cp313 | generic-dependency c64 | 19.48 / 80.2 | 19.72 / 72.9 | 120.56 / 56.4 | 1.023× (0.943–1.069) | 0.158× (0.154–0.166) |
| cp314 | generic-dependency c64 | 18.53 / 79.3 | 18.52 / 85.8 | 114.91 / 61.8 | 1.001× (0.896–1.001) | 0.161× (0.150–0.161) |
| cp314t-gil1 | generic-dependency c64 | 19.72 / 99.0 | 20.55 / 98.6 | 131.97 / 75.7 | 0.953× (0.936–0.990) | 0.149× (0.137–0.155) |
| cp314t-gil0 | generic-dependency c64 | 25.14 / 98.6 | 25.02 / 98.5 | 133.28 / 75.8 | 1.005× (0.924–1.079) | 0.188× (0.186–0.211) |

CPU includes sampler drain/export time. RSS sums process trees and can count shared pages more than once; unavailable USS is not reported as zero.

## Additional standard/free-threaded HTTP

Framework cells give three-round medians of successful req/s / p99 ms. Paired columns give the median and full range of within-round ratios; these need not equal ratios of the displayed medians.

| Python | Workload | Candidate req/s / p99 | Main a4d5b79 req/s / p99 | FastAPI req/s / p99 | Candidate/Main a4d5b79 paired | Candidate/FastAPI paired |
|---|---|---|---|---|---|---|
| cp314 | json c64 | 130,301 / 1.046 | 131,389 / 1.042 | 20,305 / 6.393 | 1.023× (0.943–1.046) | 6.146× (5.964–6.744) |
| cp314t-gil1 | json c64 | 125,484 / 1.122 | 125,787 / 1.131 | 21,000 / 6.523 | 1.028× (0.974–1.059) | 6.233× (5.975–6.251) |
| cp314t-gil0 | json c64 | 126,108 / 1.074 | 124,029 / 1.072 | 20,175 / 6.655 | 1.003× (0.987–1.050) | 6.408× (6.210–6.454) |
| cp314 | echo c64 | 71,316 / 1.930 | 73,147 / 1.864 | 12,768 / 10.007 | 0.970× (0.966–0.975) | 5.689× (5.220–5.730) |
| cp314t-gil1 | echo c64 | 63,123 / 2.143 | 63,519 / 2.132 | 11,346 / 11.108 | 0.994× (0.968–1.042) | 5.432× (5.138–5.616) |
| cp314t-gil0 | echo c64 | 62,863 / 2.129 | 62,169 / 2.068 | 11,558 / 11.278 | 1.011× (0.955–1.074) | 5.662× (5.314–5.673) |
| cp314 | models_large c64 | 36,066 / 3.211 | 36,532 / 3.091 | 5,644 / 21.003 | 0.987× (0.908–0.999) | 6.403× (6.131–6.798) |
| cp314t-gil1 | models_large c64 | 28,685 / 3.824 | 28,237 / 4.071 | 4,463 / 26.773 | 1.016× (0.954–1.027) | 6.260× (6.120–6.427) |
| cp314t-gil0 | models_large c64 | 28,845 / 3.796 | 28,549 / 3.935 | 4,312 / 27.610 | 1.040× (0.963–1.074) | 6.513× (6.417–6.950) |

## Additional standard/free-threaded HTTP CPU and memory

Framework cells give three-round medians of CPU µs/success / summed RSS MiB. CPU ratios use the same qualified rounds as throughput and give the median and full range of within-round ratios; these need not equal ratios of the displayed medians.

| Python | Workload | Candidate CPU / RSS | Main a4d5b79 CPU / RSS | FastAPI CPU / RSS | Candidate/Main a4d5b79 CPU paired | Candidate/FastAPI CPU paired |
|---|---|---|---|---|---|---|
| cp314 | json c64 | 7.09 / 92.4 | 7.08 / 92.7 | 45.17 / 59.8 | 0.978× (0.956–1.071) | 0.166× (0.150–0.168) |
| cp314t-gil1 | json c64 | 7.37 / 105.7 | 7.37 / 105.7 | 43.75 / 71.0 | 0.974× (0.949–1.020) | 0.162× (0.161–0.169) |
| cp314t-gil0 | json c64 | 7.34 / 105.7 | 7.43 / 105.7 | 45.70 / 70.9 | 0.993× (0.959–1.010) | 0.156× (0.156–0.161) |
| cp314 | echo c64 | 13.10 / 89.0 | 12.82 / 92.8 | 72.77 / 60.2 | 1.029× (1.022–1.030) | 0.177× (0.176–0.191) |
| cp314t-gil1 | echo c64 | 15.02 / 106.1 | 14.96 / 106.1 | 81.93 / 71.0 | 0.998× (0.954–1.030) | 0.187× (0.181–0.198) |
| cp314t-gil0 | echo c64 | 15.09 / 106.2 | 15.33 / 106.0 | 80.53 / 71.1 | 0.985× (0.933–1.050) | 0.181× (0.180–0.193) |
| cp314 | models_large c64 | 26.55 / 89.5 | 26.14 / 96.3 | 167.12 / 60.0 | 1.016× (0.998–1.072) | 0.159× (0.151–0.162) |
| cp314t-gil1 | models_large c64 | 33.39 / 109.2 | 33.70 / 109.0 | 211.19 / 70.9 | 0.986× (0.977–1.051) | 0.158× (0.154–0.167) |
| cp314t-gil0 | models_large c64 | 33.26 / 109.2 | 33.70 / 109.1 | 217.46 / 71.0 | 0.963× (0.941–1.042) | 0.158× (0.148–0.158) |

CPU includes sampler drain/export time. RSS sums process trees and can count shared pages more than once; unavailable USS is not reported as zero.

## CPU work c16

Framework cells give three-round medians of successful req/s / p99 ms. Paired columns give the median and full range of within-round ratios; these need not equal ratios of the displayed medians.

| Python | Workload | Candidate req/s / p99 | Main a4d5b79 req/s / p99 | FastAPI req/s / p99 | Candidate/Main a4d5b79 paired | Candidate/FastAPI paired |
|---|---|---|---|---|---|---|
| cp313 | cpu-inline c16 | 588 / 32.844 | 576 / 34.388 | 560 / 57.406 | 0.977× (0.975–1.046) | 1.065× (1.030–1.073) |
| cp314 | cpu-inline c16 | 734 / 26.813 | 749 / 27.845 | 695 / 46.979 | 0.988× (0.979–1.019) | 1.075× (1.042–1.076) |
| cp314t-gil1 | cpu-inline c16 | 743 / 26.824 | 757 / 27.351 | 697 / 46.569 | 0.995× (0.977–1.018) | 1.082× (1.054–1.138) |
| cp314t-gil0 | cpu-inline c16 | 742 / 28.244 | 749 / 28.982 | 707 / 45.377 | 1.000× (0.987–1.008) | 1.049× (1.045–1.057) |
| cp313 | cpu-sync c16 | 587 / 45.628 | 582 / 45.511 | 538 / 41.995 | 1.008× (0.976–1.021) | 1.091× (1.087–1.106) |
| cp314 | cpu-sync c16 | 743 / 36.225 | 745 / 36.171 | 682 / 34.588 | 1.011× (0.986–1.016) | 1.093× (1.090–1.121) |
| cp314t-gil1 | cpu-sync c16 | 744 / 36.851 | 747 / 36.054 | 653 / 35.584 | 0.995× (0.972–1.003) | 1.135× (1.104–1.144) |
| cp314t-gil0 | cpu-sync c16 | 1,926 / 12.430 | 1,924 / 12.282 | 1,792 / 13.194 | 1.001× (0.928–1.248) | 1.035× (0.987–1.339) |
| cp313 | cpu-offload-auto c16 | 2,037 / 9.926 | 2,011 / 10.056 | 2,059 / 10.101 | 1.011× (1.000–1.026) | 0.991× (0.990–0.997) |
| cp314 | cpu-offload-auto c16 | 2,583 / 7.944 | 2,576 / 8.052 | 2,580 / 8.044 | 1.002× (0.983–1.027) | 1.021× (0.993–1.022) |
| cp314t-gil1 | cpu-offload-auto c16 | 2,586 / 8.460 | 2,578 / 8.700 | 2,580 / 9.325 | 1.002× (0.994–1.008) | 0.996× (0.990–1.002) |
| cp314t-gil0 | cpu-offload-auto c16 | 2,596 / 7.835 | 2,586 / 8.063 | 2,568 / 7.934 | 1.019× (0.996–1.025) | 1.004× (0.996–1.011) |

## CPU work c16 CPU and memory

Framework cells give three-round medians of CPU µs/success / summed RSS MiB. CPU ratios use the same qualified rounds as throughput and give the median and full range of within-round ratios; these need not equal ratios of the displayed medians.

| Python | Workload | Candidate CPU / RSS | Main a4d5b79 CPU / RSS | FastAPI CPU / RSS | Candidate/Main a4d5b79 CPU paired | Candidate/FastAPI CPU paired |
|---|---|---|---|---|---|---|
| cp313 | cpu-inline c16 | 1,667.87 / 71.8 | 1,683.25 / 72.0 | 1,735.72 / 71.9 | 1.018× (0.967–1.027) | 0.942× (0.935–0.975) |
| cp314 | cpu-inline c16 | 1,322.08 / 84.6 | 1,290.34 / 84.8 | 1,384.30 / 80.7 | 1.015× (0.986–1.025) | 0.944× (0.944–0.968) |
| cp314t-gil1 | cpu-inline c16 | 1,307.61 / 97.4 | 1,287.70 / 96.9 | 1,382.88 / 95.2 | 1.005× (0.990–1.016) | 0.935× (0.884–0.953) |
| cp314t-gil0 | cpu-inline c16 | 1,307.87 / 97.0 | 1,293.49 / 96.9 | 1,358.91 / 95.3 | 0.999× (0.998–1.019) | 0.960× (0.956–0.962) |
| cp313 | cpu-sync c16 | 1,672.63 / 79.9 | 1,676.38 / 80.5 | 1,801.32 / 72.0 | 0.998× (0.987–1.017) | 0.928× (0.915–0.929) |
| cp314 | cpu-sync c16 | 1,320.10 / 77.8 | 1,319.49 / 78.1 | 1,422.09 / 81.2 | 0.993× (0.992–1.017) | 0.927× (0.909–0.928) |
| cp314t-gil1 | cpu-sync c16 | 1,328.56 / 98.5 | 1,316.31 / 98.5 | 1,468.69 / 98.4 | 1.006× (1.003–1.018) | 0.899× (0.889–0.920) |
| cp314t-gil0 | cpu-sync c16 | 2,049.00 / 98.4 | 2,053.27 / 98.7 | 2,199.98 / 98.5 | 0.998× (0.811–1.081) | 0.978× (0.758–1.031) |
| cp313 | cpu-offload-auto c16 | 2,146.04 / 222.1 | 2,177.13 / 228.7 | 2,182.86 / 151.4 | 0.986× (0.977–0.997) | 0.977× (0.977–0.989) |
| cp314 | cpu-offload-auto c16 | 1,732.76 / 249.8 | 1,734.36 / 241.7 | 1,784.34 / 183.4 | 0.999× (0.980–1.022) | 0.957× (0.946–0.984) |
| cp314t-gil1 | cpu-offload-auto c16 | 1,763.21 / 281.5 | 1,764.90 / 281.5 | 1,805.58 / 211.1 | 0.998× (0.996–0.999) | 0.985× (0.977–0.990) |
| cp314t-gil0 | cpu-offload-auto c16 | 1,734.22 / 281.8 | 1,739.54 / 281.5 | 1,808.75 / 210.8 | 0.985× (0.985–1.000) | 0.962× (0.956–0.975) |

CPU includes sampler drain/export time. RSS sums process trees and can count shared pages more than once; unavailable USS is not reported as zero.

## CPU sync c64

Framework cells give three-round medians of successful req/s / p99 ms. Paired columns give the median and full range of within-round ratios; these need not equal ratios of the displayed medians.

| Python | Workload | Candidate req/s / p99 | Main a4d5b79 req/s / p99 | FastAPI req/s / p99 | Candidate/Main a4d5b79 paired | Candidate/FastAPI paired |
|---|---|---|---|---|---|---|
| cp313 | cpu-sync c64 | 580 / 170.054 | 598 / 167.370 | 533 / 147.369 | 0.983× (0.971–0.998) | 1.089× (1.057–1.089) |
| cp314 | cpu-sync c64 | 748 / 147.439 | 749 / 149.552 | 684 / 113.381 | 1.018× (0.965–1.025) | 1.114× (1.078–1.115) |

## CPU sync c64 CPU and memory

Framework cells give three-round medians of CPU µs/success / summed RSS MiB. CPU ratios use the same qualified rounds as throughput and give the median and full range of within-round ratios; these need not equal ratios of the displayed medians.

| Python | Workload | Candidate CPU / RSS | Main a4d5b79 CPU / RSS | FastAPI CPU / RSS | Candidate/Main a4d5b79 CPU paired | Candidate/FastAPI CPU paired |
|---|---|---|---|---|---|---|
| cp313 | cpu-sync c64 | 1,691.32 / 81.0 | 1,651.66 / 80.9 | 1,824.86 / 73.6 | 1.018× (1.013–1.024) | 0.931× (0.927–0.964) |
| cp314 | cpu-sync c64 | 1,309.03 / 86.5 | 1,307.73 / 86.4 | 1,427.53 / 82.7 | 0.983× (0.974–1.023) | 0.910× (0.901–0.928) |

CPU includes sampler drain/export time. RSS sums process trees and can count shared pages more than once; unavailable USS is not reported as zero.

## Free-threaded / standard interpreter ratios

Each ratio pairs the same role and workload within each natural round; modes execute sequentially on the shared host. Ratios give the three-round median and range. CPU ratios are CPU time per successful reply.

| Role | Case | Modes, numerator/denominator | Paired RPS | Paired CPU/success |
|---|---|---|---|---|
| candidate | cpu-inline | cp314t-gil1/cp314 | 1.013× (1.007–1.022) | 0.989× (0.980–0.990) |
| candidate | cpu-inline | cp314t-gil0/cp314 | 1.025× (0.988–1.033) | 0.978× (0.972–1.008) |
| candidate | cpu-inline | cp314t-gil0/cp314t-gil1 | 1.003× (0.981–1.019) | 0.998× (0.983–1.019) |
| candidate | cpu-sync | cp314t-gil1/cp314 | 0.987× (0.977–1.017) | 1.014× (0.984–1.015) |
| candidate | cpu-sync | cp314t-gil0/cp314 | 2.622× (2.460–3.130) | 1.527× (1.288–1.645) |
| candidate | cpu-sync | cp314t-gil0/cp314t-gil1 | 2.577× (2.494–3.203) | 1.551× (1.270–1.620) |
| candidate | cpu-offload-auto | cp314t-gil1/cp314 | 0.992× (0.982–1.020) | 1.026× (0.994–1.033) |
| candidate | cpu-offload-auto | cp314t-gil0/cp314 | 1.005× (0.977–1.028) | 1.001× (0.969–1.019) |
| candidate | cpu-offload-auto | cp314t-gil0/cp314t-gil1 | 1.008× (0.995–1.013) | 0.975× (0.974–0.987) |
| main | cpu-inline | cp314t-gil1/cp314 | 1.015× (1.001–1.024) | 0.997× (0.976–1.000) |
| main | cpu-inline | cp314t-gil0/cp314 | 1.012× (0.990–1.037) | 0.997× (0.967–1.004) |
| main | cpu-inline | cp314t-gil0/cp314t-gil1 | 0.997× (0.989–1.013) | 1.000× (0.991–1.004) |
| main | cpu-sync | cp314t-gil1/cp314 | 1.003× (1.000–1.021) | 0.998× (0.988–1.002) |
| main | cpu-sync | cp314t-gil0/cp314 | 2.583× (2.547–2.679) | 1.556× (1.510–1.576) |
| main | cpu-sync | cp314t-gil0/cp314t-gil1 | 2.583× (2.496–2.673) | 1.560× (1.507–1.595) |
| main | cpu-offload-auto | cp314t-gil1/cp314 | 1.001× (1.000–1.001) | 1.021× (1.013–1.027) |
| main | cpu-offload-auto | cp314t-gil0/cp314 | 0.992× (0.983–1.008) | 1.005× (0.999–1.015) |
| main | cpu-offload-auto | cp314t-gil0/cp314t-gil1 | 0.991× (0.983–1.007) | 0.986× (0.984–0.988) |
| fastapi | cpu-inline | cp314t-gil1/cp314 | 1.002× (0.936–1.034) | 0.999× (0.979–1.073) |
| fastapi | cpu-inline | cp314t-gil0/cp314 | 1.018× (1.006–1.063) | 0.984× (0.956–0.996) |
| fastapi | cpu-inline | cp314t-gil0/cp314t-gil1 | 1.028× (1.004–1.088) | 0.976× (0.917–0.997) |
| fastapi | cpu-sync | cp314t-gil1/cp314 | 0.977× (0.939–0.997) | 1.023× (0.995–1.059) |
| fastapi | cpu-sync | cp314t-gil0/cp314 | 2.599× (2.548–2.977) | 1.559× (1.347–1.578) |
| fastapi | cpu-sync | cp314t-gil0/cp314t-gil1 | 2.714× (2.661–2.985) | 1.490× (1.354–1.524) |
| fastapi | cpu-offload-auto | cp314t-gil1/cp314 | 1.018× (1.000–1.023) | 0.988× (0.985–1.012) |
| fastapi | cpu-offload-auto | cp314t-gil0/cp314 | 1.016× (0.994–1.025) | 0.989× (0.977–1.014) |
| fastapi | cpu-offload-auto | cp314t-gil0/cp314t-gil1 | 0.998× (0.994–1.002) | 1.002× (0.989–1.004) |
| candidate | generic-request | cp314t-gil1/cp314 | 0.942× (0.935–0.957) | 1.066× (1.055–1.081) |
| candidate | generic-request | cp314t-gil0/cp314 | 0.886× (0.819–0.909) | 1.357× (1.354–1.460) |
| candidate | generic-request | cp314t-gil0/cp314t-gil1 | 0.947× (0.869–0.949) | 1.286× (1.271–1.351) |
| candidate | generic-dependency | cp314t-gil1/cp314 | 0.937× (0.930–0.959) | 1.065× (1.038–1.071) |
| candidate | generic-dependency | cp314t-gil0/cp314 | 0.828× (0.777–0.844) | 1.365× (1.341–1.424) |
| candidate | generic-dependency | cp314t-gil0/cp314t-gil1 | 0.883× (0.810–0.907) | 1.275× (1.259–1.371) |
| candidate | json | cp314t-gil1/cp314 | 0.991× (0.944–1.006) | 1.010× (0.983–1.058) |
| candidate | json | cp314t-gil0/cp314 | 0.993× (0.939–0.999) | 1.006× (0.995–1.061) |
| candidate | json | cp314t-gil0/cp314t-gil1 | 0.988× (0.947–1.058) | 1.012× (0.950–1.050) |
| candidate | echo | cp314t-gil1/cp314 | 0.891× (0.857–0.925) | 1.146× (1.104–1.170) |
| candidate | echo | cp314t-gil0/cp314 | 0.918× (0.854–0.936) | 1.109× (1.098–1.183) |
| candidate | echo | cp314t-gil0/cp314t-gil1 | 1.012× (0.996–1.030) | 0.995× (0.968–1.011) |
| candidate | models_large | cp314t-gil1/cp314 | 0.795× (0.750–0.802) | 1.258× (1.258–1.283) |
| candidate | models_large | cp314t-gil0/cp314 | 0.812× (0.800–0.818) | 1.253× (1.229–1.267) |
| candidate | models_large | cp314t-gil0/cp314t-gil1 | 1.021× (1.006–1.082) | 0.987× (0.976–0.996) |
| main | generic-request | cp314t-gil1/cp314 | 0.938× (0.910–1.061) | 1.069× (0.954–1.099) |
| main | generic-request | cp314t-gil0/cp314 | 0.814× (0.797–0.959) | 1.469× (1.269–1.498) |
| main | generic-request | cp314t-gil0/cp314t-gil1 | 0.895× (0.849–0.904) | 1.337× (1.331–1.401) |
| main | generic-dependency | cp314t-gil1/cp314 | 0.920× (0.891–0.995) | 1.082× (1.002–1.110) |
| main | generic-dependency | cp314t-gil0/cp314 | 0.855× (0.823–0.868) | 1.321× (1.301–1.359) |
| main | generic-dependency | cp314t-gil0/cp314t-gil1 | 0.895× (0.872–0.959) | 1.256× (1.190–1.298) |
| main | json | cp314t-gil1/cp314 | 0.957× (0.923–1.014) | 1.042× (0.992–1.082) |
| main | json | cp314t-gil0/cp314 | 0.973× (0.934–0.995) | 1.028× (1.003–1.074) |
| main | json | cp314t-gil0/cp314t-gil1 | 1.012× (0.982–1.016) | 0.993× (0.986–1.011) |
| main | echo | cp314t-gil1/cp314 | 0.857× (0.837–0.897) | 1.191× (1.138–1.208) |
| main | echo | cp314t-gil0/cp314 | 0.833× (0.819–0.946) | 1.216× (1.076–1.238) |
| main | echo | cp314t-gil0/cp314t-gil1 | 0.979× (0.929–1.104) | 1.024× (0.904–1.068) |
| main | models_large | cp314t-gil1/cp314 | 0.773× (0.714–0.780) | 1.307× (1.274–1.307) |
| main | models_large | cp314t-gil0/cp314 | 0.765× (0.735–0.786) | 1.303× (1.273–1.353) |
| main | models_large | cp314t-gil0/cp314t-gil1 | 1.008× (0.951–1.071) | 1.000× (0.997–1.035) |
| fastapi | generic-request | cp314t-gil1/cp314 | 0.798× (0.771–0.847) | 1.131× (1.095–1.159) |
| fastapi | generic-request | cp314t-gil0/cp314 | 1.376× (1.368–1.387) | 1.038× (1.033–1.039) |
| fastapi | generic-request | cp314t-gil0/cp314t-gil1 | 1.737× (1.624–1.775) | 0.918× (0.891–0.950) |
| fastapi | generic-dependency | cp314t-gil1/cp314 | 0.773× (0.749–0.830) | 1.152× (1.083–1.163) |
| fastapi | generic-dependency | cp314t-gil0/cp314 | 1.138× (1.037–1.172) | 1.087× (1.069–1.180) |
| fastapi | generic-dependency | cp314t-gil0/cp314t-gil1 | 1.372× (1.342–1.564) | 1.003× (0.919–1.024) |
| fastapi | json | cp314t-gil1/cp314 | 1.034× (0.904–1.069) | 0.969× (0.935–1.104) |
| fastapi | json | cp314t-gil0/cp314 | 0.953× (0.924–1.019) | 1.056× (0.990–1.081) |
| fastapi | json | cp314t-gil0/cp314t-gil1 | 0.953× (0.921–1.022) | 1.059× (0.979–1.091) |
| fastapi | echo | cp314t-gil1/cp314 | 0.889× (0.875–0.986) | 1.126× (1.023–1.141) |
| fastapi | echo | cp314t-gil0/cp314 | 0.919× (0.862–0.922) | 1.087× (1.086–1.151) |
| fastapi | echo | cp314t-gil0/cp314t-gil1 | 0.986× (0.935–1.035) | 1.009× (0.964–1.062) |
| fastapi | models_large | cp314t-gil1/cp314 | 0.839× (0.735–0.841) | 1.202× (1.195–1.353) |
| fastapi | models_large | cp314t-gil0/cp314 | 0.782× (0.764–0.816) | 1.275× (1.233–1.301) |
| fastapi | models_large | cp314t-gil0/cp314t-gil1 | 0.973× (0.930–1.040) | 1.032× (0.961–1.061) |

## Single-client functional controls

These are eight one-round observations on candidate/main, with no FastAPI population and no paired performance claims.

| Mode | Control | Candidate req/s | Main req/s |
|---|---|---|---|
| cp313 | cpu-inline | 564 | 466 |
| cp313 | cpu-sync | 541 | 568 |
| cp314 | cpu-inline | 714 | 679 |
| cp314 | cpu-sync | 699 | 708 |

## Shared host context

Ranges are observed during natural load. They do not establish CPU isolation; first unprimed psutil readings are excluded. Each case cell gives host CPU-percent range / one-minute load-average range. Full 1/5/15-minute ranges remain in the public JSON summary.

| Context | Observed range |
|---|---|
| Host CPU % | 0.0–96.7 |
| Load average 1 min | 7.27–17.22 |
| Load average 5 min | 9.12–13.66 |
| Load average 15 min | 9.75–12.27 |

| Mode | Case | Candidate CPU% / load1m | Main CPU% / load1m | FastAPI CPU% / load1m |
|---|---|---|---|---|
| cp313 | cpu-inline c16 | 0.0–60.2 / 7.88–10.33 | 37.5–65.3 / 7.88–10.69 | 0.0–70.5 / 7.88–10.53 |
| cp314 | cpu-inline c16 | 0.0–71.3 / 7.27–13.76 | 39.1–64.4 / 7.50–13.06 | 0.0–62.0 / 7.38–13.52 |
| cp314t-gil1 | cpu-inline c16 | 0.0–63.9 / 7.64–12.81 | 0.0–63.1 / 7.51–12.59 | 0.0–65.4 / 7.32–12.30 |
| cp314t-gil0 | cpu-inline c16 | 0.0–65.7 / 7.39–11.27 | 0.0–61.5 / 7.39–11.48 | 0.0–59.9 / 7.80–11.27 |
| cp313 | cpu-sync c16 | 38.4–63.0 / 7.98–13.52 | 0.0–58.5 / 8.05–13.65 | 0.0–56.9 / 7.64–13.51 |
| cp314 | cpu-sync c16 | 0.0–62.2 / 7.60–14.34 | 0.0–60.3 / 7.43–14.08 | 0.0–77.4 / 7.48–14.36 |
| cp314t-gil1 | cpu-sync c16 | 0.0–69.8 / 7.58–14.71 | 0.0–62.9 / 7.56–14.25 | 0.0–79.3 / 7.58–14.15 |
| cp314t-gil0 | cpu-sync c16 | 0.0–81.4 / 7.56–13.79 | 0.0–79.5 / 7.78–14.37 | 0.0–84.7 / 7.72–14.37 |
| cp313 | cpu-offload-auto c16 | 0.0–89.1 / 7.83–14.48 | 0.0–96.7 / 8.32–14.61 | 0.0–82.4 / 8.62–14.35 |
| cp314 | cpu-offload-auto c16 | 0.0–89.9 / 10.03–14.72 | 0.0–88.9 / 9.69–15.23 | 61.9–88.2 / 10.12–15.37 |
| cp314t-gil1 | cpu-offload-auto c16 | 0.0–79.1 / 11.02–13.69 | 0.0–83.5 / 10.25–14.05 | 0.0–82.3 / 10.24–14.21 |
| cp314t-gil0 | cpu-offload-auto c16 | 0.0–84.4 / 11.74–14.42 | 0.0–83.2 / 11.81–14.11 | 0.0–90.5 / 11.11–14.11 |
| cp313 | cpu-sync c64 | 0.0–70.9 / 9.16–14.05 | 0.0–66.8 / 9.38–13.67 | 0.0–65.1 / 9.18–15.26 |
| cp314 | cpu-sync c64 | 0.0–61.0 / 9.60–13.67 | 0.0–55.9 / 9.42–12.90 | 0.0–62.9 / 9.60–12.66 |
| cp313 | json c64 | 0.0–75.5 / 10.33–12.16 | 0.0–69.6 / 10.55–12.16 | 0.0–66.6 / 10.33–12.25 |
| cp313 | echo c64 | 0.0–64.3 / 10.55–11.94 | 0.0–71.2 / 10.26–11.95 | 0.0–70.8 / 10.56–12.78 |
| cp313 | io c64 | 0.0–59.5 / 10.46–12.78 | 0.0–62.5 / 11.16–13.47 | 0.0–65.5 / 10.76–13.43 |
| cp313 | fanout c64 | 0.0–77.1 / 10.59–13.08 | 0.0–65.2 / 11.58–12.75 | 0.0–63.6 / 10.99–12.45 |
| cp313 | sync c64 | 0.0–62.6 / 11.73–12.81 | 0.0–71.8 / 11.28–12.26 | 0.0–75.8 / 11.51–12.94 |
| cp313 | sync-stream c64 | 0.0–75.8 / 10.93–13.27 | 0.0–68.6 / 10.70–13.27 | 0.0–88.9 / 10.93–12.99 |
| cp313 | stream c64 | 0.0–82.9 / 10.52–12.92 | 0.0–76.4 / 10.13–15.65 | 0.0–66.1 / 10.20–15.65 |
| cp313 | bytes c64 | 0.0–67.8 / 9.64–14.93 | 0.0–74.5 / 9.60–15.66 | 0.0–75.5 / 9.60–15.28 |
| cp313 | dependencies c64 | 0.0–68.8 / 9.21–15.04 | 0.0–76.7 / 9.16–15.12 | 0.0–63.7 / 8.88–14.97 |
| cp313 | models_large c64 | 0.0–61.3 / 10.36–14.97 | 0.0–66.3 / 9.46–14.90 | 0.0–65.0 / 10.47–13.85 |
| cp313 | echo-large c16 | 0.0–66.7 / 9.68–13.39 | 0.0–64.3 / 10.11–13.12 | 0.0–69.6 / 9.46–12.79 |
| cp313 | echo-large c64 | 0.0–69.3 / 9.92–12.48 | 0.0–63.2 / 9.24–12.41 | 0.0–66.2 / 10.18–12.44 |
| cp313 | generic-request c64 | 44.8–68.3 / 9.08–12.10 | 0.0–65.9 / 8.92–12.49 | 0.0–56.4 / 9.26–12.05 |
| cp314 | generic-request c64 | 0.0–75.2 / 9.48–13.40 | 0.0–75.4 / 9.68–13.52 | 0.0–69.1 / 9.87–13.52 |
| cp314t-gil1 | generic-request c64 | 0.0–81.1 / 9.13–13.40 | 0.0–65.8 / 9.14–13.85 | 0.0–61.0 / 9.05–13.03 |
| cp314t-gil0 | generic-request c64 | 0.0–67.4 / 8.91–13.85 | 0.0–70.8 / 8.91–14.09 | 0.0–66.1 / 9.17–14.09 |
| cp313 | generic-dependency c64 | 0.0–71.0 / 10.66–14.08 | 0.0–70.8 / 10.69–14.10 | 39.5–65.9 / 10.20–13.61 |
| cp314 | generic-dependency c64 | 0.0–67.5 / 11.05–14.44 | 0.0–75.1 / 10.80–14.16 | 0.0–58.0 / 10.80–14.30 |
| cp314t-gil1 | generic-dependency c64 | 0.0–67.4 / 10.29–15.02 | 0.0–68.8 / 10.40–15.06 | 0.0–63.9 / 10.60–15.28 |
| cp314t-gil0 | generic-dependency c64 | 0.0–76.1 / 10.32–14.57 | 0.0–71.8 / 10.20–14.35 | 0.0–70.3 / 10.29–14.35 |
| cp314 | json c64 | 0.0–75.8 / 8.99–13.71 | 0.0–73.9 / 9.31–14.17 | 0.0–71.4 / 9.25–13.71 |
| cp314t-gil1 | json c64 | 0.0–74.1 / 9.87–16.80 | 0.0–78.2 / 9.45–16.50 | 0.0–65.2 / 9.41–17.22 |
| cp314t-gil0 | json c64 | 0.0–75.1 / 9.18–15.57 | 0.0–81.3 / 9.41–14.98 | 0.0–64.6 / 9.29–14.98 |
| cp314 | echo c64 | 0.0–65.7 / 9.04–14.05 | 0.0–72.2 / 9.33–14.23 | 0.0–64.0 / 9.31–14.23 |
| cp314t-gil1 | echo c64 | 0.0–68.1 / 10.86–13.62 | 0.0–78.9 / 10.19–14.43 | 0.0–60.5 / 10.09–14.37 |
| cp314t-gil0 | echo c64 | 0.0–63.5 / 9.56–14.43 | 0.0–68.9 / 10.01–13.64 | 43.1–64.3 / 9.52–13.64 |
| cp314 | models_large c64 | 0.0–81.7 / 9.39–13.08 | 0.0–66.2 / 9.60–13.51 | 0.0–66.5 / 9.08–13.07 |
| cp314t-gil1 | models_large c64 | 0.0–62.6 / 9.33–12.71 | 0.0–65.4 / 9.64–12.50 | 0.0–58.1 / 9.94–12.71 |
| cp314t-gil0 | models_large c64 | 0.0–57.5 / 9.08–13.09 | 0.0–66.2 / 9.38–12.68 | 0.0–68.6 / 9.35–13.22 |

## Failures and qualification

| Variant | Measured/attempted | Measured HTTP statuses | Transport errors | Wrong lengths | Warmup non-200 | Warmup transport | Measured error reasons |
|---|---|---|---|---|---|---|---|
| Candidate | 133/133 | {"200": 19102069} | 0 | 0 | 0 | 0 | {} |
| Main a4d5b79 | 133/133 | {"200": 19078750} | 0 | 0 | 0 | 0 | {} |
| FastAPI 0.162.2 | 129/129 | {"200": 3865428} | 13 | 0 | 0 | 0 | {"timeout": 13} |

Lifecycle issues: 0. Any failed or missing round withholds the entire paired population, including its CPU comparison. † marks an unqualified diagnostic median; it is not used for a speedup claim.

| Phase | Python | Variant | Workload | Round | Measured failures | Warmup failures | Warmup issues |
|---|---|---|---|---|---|---|---|
| http-large-c64 | cp313 | fastapi | echo-large | 1 | 5 | 0 | — |
| http-large-c64 | cp313 | fastapi | echo-large | 2 | 5 | 0 | — |
| http-large-c64 | cp313 | fastapi | echo-large | 3 | 3 | 0 | — |

## Separate scheduling controls

Twenty-four owned worker SIGSTOP/SIGCONT controls use 16 clients and four sync slots on standard 3.13/3.14. Every status/body, runtime, final SIGCONT and cleanup is checked. These artificial pauses provide a scheduling diagnostic; their rates/counts are excluded from the natural-load capacity comparisons. Zero baseline refusals alone would not reproduce or prove the accounting correction.

| Role:mode | Control samples | HTTP statuses | Transport errors |
|---|---|---|---|
| candidate:cp313 | 6 | {"200": 2544} | {} |
| main:cp313 | 6 | {"200": 2562} | {} |
| main:cp314 | 6 | {"200": 3414} | {} |
| candidate:cp314 | 6 | {"200": 3240} | {} |

## Targeted regression evidence

The historical paused-time Rust regression failed on request 2 under e26b3c78. Its adaptive source Git blob exactly equals a4d5b79 main (73f81991aaaf0c2fc3d92ce9a8fdf0791c9b895e), and its native artifact matches the verified main cp313 baseline. The source+fragment, execution receipt and failing log are hash-bound. This is historical baseline evidence, not a new run or proof of the old Intel CI cause.

Fresh candidate Rust regressions are part of the full installed gate and frozen source/log receipts. Their correctness evidence is separate from the 395 natural measurements and paused scheduling counts.

## Separate JSON/bytes investigation

Two separately frozen studies use standard CPython 3.13, 64 clients, 1 s warmup and 4 s load. The initial 48 compare old-fix (sealed prior accounting candidate e777af7e) with main a4d5b79 across six balanced rounds. The final 72 add the final candidate, with each role occupying every position twice. Default admission and explicit fixed cap 4096 are separate diagnostic strata. These samples and their counts are never pooled with the 395 natural samples or 24 paused scheduling controls.

The tables preserve every raw group median and every qualified/withheld six-round comparison. Ratios pair the same round; a ratio of displayed medians can differ. CPU ratios are CPU time per successful response.

### Initial 48 old-fix/main

Completed 48 attempts; 48 qualified. All measured failures are retained and comparisons containing a failed sample are withheld.

| Admission:workload:role | Median req/s | Median p99 ms | Median CPU µs/success | Median RSS MiB |
|---|---|---|---|---|
| default:json:old-fix | 131,082 | 1.067 | 7.063 | 86.0 |
| default:json:main | 132,437 | 1.031 | 7.007 | 85.8 |
| default:bytes:main | 28,430 | 3.829 | 29.370 | 79.1 |
| default:bytes:old-fix | 29,522 | 3.714 | 28.506 | 86.0 |
| fixed4096:bytes:old-fix | 29,109 | 3.855 | 28.786 | 82.6 |
| fixed4096:bytes:main | 30,142 | 3.671 | 27.885 | 86.2 |
| fixed4096:json:main | 134,443 | 1.033 | 6.921 | 86.1 |
| fixed4096:json:old-fix | 135,764 | 1.046 | 6.827 | 86.1 |

| Paired numerator/denominator | Paired RPS median (six-round range) | Paired CPU/success median (six-round range) |
|---|---|---|
| default:json:old-fix/main | 1.007× (0.944–1.065) | 0.985× (0.937–1.057) |
| default:bytes:old-fix/main | 1.052× (0.908–1.130) | 0.965× (0.902–1.083) |
| fixed4096:json:old-fix/main | 1.004× (0.954–1.205) | 0.993× (0.832–1.047) |
| fixed4096:bytes:old-fix/main | 0.965× (0.879–1.033) | 1.033× (0.978–1.112) |
| default/fixed4096:json:old-fix | 0.954× (0.912–1.103) | 1.045× (0.906–1.095) |
| default/fixed4096:bytes:old-fix | 1.029× (0.949–1.121) | 0.982× (0.910–1.048) |
| default/fixed4096:json:main | 0.985× (0.927–1.151) | 1.012× (0.863–1.086) |
| default/fixed4096:bytes:main | 0.958× (0.917–1.011) | 1.037× (0.994–1.081) |

### Final 72 candidate/old-fix/main

Completed 72 attempts; 72 qualified. All measured failures are retained and comparisons containing a failed sample are withheld.

| Admission:workload:role | Median req/s | Median p99 ms | Median CPU µs/success | Median RSS MiB |
|---|---|---|---|---|
| default:json:old-fix | 129,961 | 1.066 | 7.129 | 82.5 |
| default:json:main | 126,873 | 1.060 | 7.307 | 78.9 |
| default:json:candidate | 130,535 | 1.058 | 7.041 | 81.0 |
| default:bytes:main | 28,143 | 3.884 | 29.682 | 78.9 |
| default:bytes:candidate | 28,131 | 3.828 | 29.681 | 86.3 |
| default:bytes:old-fix | 27,890 | 3.867 | 29.954 | 82.7 |
| fixed4096:bytes:candidate | 28,076 | 3.819 | 29.819 | 82.9 |
| fixed4096:bytes:old-fix | 28,322 | 3.814 | 29.495 | 86.5 |
| fixed4096:bytes:main | 27,404 | 3.927 | 30.190 | 86.2 |
| fixed4096:json:main | 127,542 | 1.054 | 7.242 | 82.5 |
| fixed4096:json:candidate | 130,302 | 1.054 | 7.091 | 85.9 |
| fixed4096:json:old-fix | 129,655 | 1.052 | 7.107 | 82.4 |

| Paired numerator/denominator | Paired RPS median (six-round range) | Paired CPU/success median (six-round range) |
|---|---|---|
| default:json:old-fix/main | 1.052× (0.889–1.102) | 0.941× (0.911–1.124) |
| default:json:candidate/old-fix | 0.995× (0.871–1.087) | 1.003× (0.921–1.122) |
| default:json:candidate/main | 1.037× (0.885–1.121) | 0.962× (0.883–1.110) |
| default:bytes:old-fix/main | 0.992× (0.900–1.105) | 1.004× (0.924–1.095) |
| default:bytes:candidate/old-fix | 1.042× (0.949–1.150) | 0.962× (0.886–1.049) |
| default:bytes:candidate/main | 1.028× (0.971–1.049) | 0.973× (0.956–1.031) |
| fixed4096:json:old-fix/main | 1.002× (0.880–1.044) | 1.002× (0.954–1.138) |
| fixed4096:json:candidate/old-fix | 1.022× (0.871–1.089) | 0.975× (0.922–1.143) |
| fixed4096:json:candidate/main | 1.019× (0.850–1.137) | 0.982× (0.879–1.175) |
| fixed4096:bytes:old-fix/main | 1.056× (0.987–1.078) | 0.953× (0.930–1.011) |
| fixed4096:bytes:candidate/old-fix | 0.961× (0.943–1.070) | 1.039× (0.944–1.057) |
| fixed4096:bytes:candidate/main | 1.024× (0.944–1.121) | 0.990× (0.897–1.053) |
| default/fixed4096:json:old-fix | 0.999× (0.986–1.132) | 1.000× (0.880–1.016) |
| default/fixed4096:bytes:old-fix | 0.990× (0.866–1.070) | 1.012× (0.941–1.129) |
| default/fixed4096:json:main | 0.970× (0.891–1.041) | 1.048× (0.954–1.117) |
| default/fixed4096:bytes:main | 1.050× (0.940–1.091) | 0.962× (0.916–1.046) |
| default/fixed4096:json:candidate | 1.014× (0.802–1.113) | 0.990× (0.899–1.218) |
| default/fixed4096:bytes:candidate | 1.036× (0.937–1.114) | 0.970× (0.898–1.057) |

### Separate native and count diagnostics

Four native stack captures completed on main/old-fix JSON/bytes. Their stack sample counts are diagnostics, including waiting threads; they are excluded from rates and CPU-cost comparisons. A symbol absent from a sampled stack does not prove zero calls.

Four temporary instrumented old-fix windows recorded 1,385,481 bridge submissions through first close, including runtime/validation probes. There was no dedicated warmup. Job size was 256 bytes. All four recorded zero tracking submissions, late observations and queue-process/batch CPU-clock getter attempts, with an empty queue at close. This does not establish zero CPU-clock calls in other production workloads. Instrumented counts are separate from the production wheels and timing studies.

| Count-only window | Bridge submissions | Tracking submissions | Late observations | Queue CPU getter attempts | Batch CPU getter attempts |
|---|---|---|---|---|---|
| default:json | 606,257 | 0 | 0 | 0 | 0 |
| fixed4096:bytes | 101,698 | 0 | 0 | 0 | 0 |
| default:bytes | 117,510 | 0 | 0 | 0 | 0 |
| fixed4096:json | 560,016 | 0 | 0 | 0 | 0 |

## Validation and scope

Full installed gate: 346 Rust tests passed, 2 ignored; Python and harness counts are included in the validation receipt. All five focused interpreter/GIL gates passed and matched the measured native hashes.

Every full three-round comparison requires zero measured status/transport/body-length failures and warmup status/transport failures, complete positive warmup records, semantic body probes, stable package/native/GIL/JIT/pool identity, client below 1.7 CPU cores and clean shutdown. Independent SQL audit covers all measured databases and binds the exact plan and result files.

Shared unpinned host; no OS CPU quota or affinity is asserted. Four CPU/sync slots size pools and do not isolate processors. Recorded host ranges describe interference. CPU/request includes drain/export and summed RSS can double-count shared pages. This does not establish formal M0, a fixed-rate production SLO, billing, GPU acceleration or worker-scaling acceptance.

Full raw SQLite, response probes, execution logs and source/native receipts remain in the local study cache. Public summaries retain hashes and version identities without local filesystem paths.

## Local data and reproduction

This public directory is an allowlisted result subset, not a standalone benchmark harness. Exact dependency pins and per-ABI native/wheel/source hashes are in `provenance.json`; the executed test gates, independent audit and post-timing guards have separate receipts.

The local raw study cache is named `json-bytes-performance-2026-11-17`. It contains the frozen harness and plan, exact pinned requirements, raw SQLite, response probes, logs, source/build manifests and installed environments. From that cache, regenerate the analysis, audit and public report from the recorded data with:

```sh
./venv-candidate-cp313/bin/python analyse_bench.py --timings-finished
./venv-candidate-cp313/bin/python audit_sql.py --timings-finished
./venv-candidate-cp313/bin/python write_public_report.py --timings-finished --out reproduced-public-report
```

A fresh timing study requires a new cache/frozen plan and validated environments; preserve the recorded run.
