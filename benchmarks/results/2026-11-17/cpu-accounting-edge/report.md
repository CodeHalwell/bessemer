# CPU admission accounting edge benchmark

Completed 395 fresh samples; 393 qualified. Measured 31,154,378 successful HTTP responses. All raw failures are retained and any failed comparison is withheld.

Candidate is `a4d5b79 base + uncommitted CPU accounting changes`, identified by its frozen source/native/wheel manifests and working-tree diff hash. The module version 1.0.0rc7 and HEAD alone do not identify candidate code. Baselines are exact main `a4d5b79a011aae6eb52e8ea891cd1a232cba3ebd` and FastAPI 0.162.2. Main artifacts are read-only PR131 candidate source wheels: all 103 original runtime/build source hashes match main. Candidate source wheels use the same Rust 1.92.0 release profile with strip=none. Published-wheel throughput is not claimed.

This study evaluates a CPU admission accounting correction. Correctness and targeted refusals are reported separately; a throughput increase is not assumed. The full natural matrix records rates, p99, CPU per successful reply and memory across the existing workloads.

43 candidate/main workload comparisons qualified for all three paired rounds. Their per-workload median throughput ratios span 0.857–1.152×; this is a range across distinct workloads, not a pooled speed score. All failures and mixed results remain visible.

The admission policy has three states. Evidence can attribute a wait to other threads, count it toward congestion, or leave it uncertain at the queue boundary. An actually busy interval that is observationally ambiguous can defer an unestablished congestion streak. An uncertain sample cannot release established overload: the held batch protects it. The 5 ms actual-loop-work target and blocking-handler checks remain; this is not an exact remote-loop CPU clock.

These configurations measured lower candidate throughput in all three paired rounds. No cause is assigned.

| Case (phase:mode:workload) | Paired candidate/main RPS | Paired candidate/main CPU/success |
|---|---|---|
| cpu-c16:cp314t-gil1:cpu-offload-auto | 0.994× (0.967–0.999) | 0.991× (0.969–1.017) |
| cpu-sync-c64:cp314:cpu-sync | 0.979× (0.974–0.982) | 1.014× (0.997–1.019) |
| http-core:cp313:json | 0.946× (0.861–0.973) | 1.098× (1.031–1.146) |
| http-core:cp313:sync | 0.995× (0.989–1.000) | 1.079× (1.013–1.079) |
| http-core:cp313:bytes | 0.857× (0.842–0.961) | 1.143× (1.013–1.168) |

The study has 387 three-round performance samples and eight separate one-round functional controls. All 43 performance case groups use every contender position once across three rounds. Python 3.13.18, standard 3.14.7 and free-threaded 3.14.7 with GIL on/off are recorded explicitly. JIT is disabled and checked before/after; GIL-off is never forced past an incompatible import.

## Core HTTP on Python 3.13

Framework cells give three-round medians of successful req/s / p99 ms. Paired columns give the median and full range of within-round ratios; these need not equal ratios of the displayed medians.

| Python | Workload | Candidate req/s / p99 | Main a4d5b79 req/s / p99 | FastAPI req/s / p99 | Candidate/Main a4d5b79 paired | Candidate/FastAPI paired |
|---|---|---|---|---|---|---|
| cp313 | json c64 | 92,912 / 2.129 | 98,229 / 1.859 | 13,879 / 10.950 | 0.946× (0.861–0.973) | 5.777× (5.692–7.954) |
| cp313 | echo c64 | 54,823 / 2.582 | 52,887 / 2.511 | 9,679 / 13.399 | 0.987× (0.931–1.100) | 5.391× (5.203–5.999) |
| cp313 | io c64 | 42,683 / 2.545 | 41,982 / 2.616 | 18,082 / 11.485 | 1.007× (0.958–1.072) | 2.444× (2.361–2.671) |
| cp313 | fanout c64 | 16,878 / 10.417 | 14,802 / 12.604 | 8,134 / 23.793 | 1.044× (1.009–1.140) | 2.336× (1.801–2.462) |
| cp313 | sync c64 | 12,703 / 6.848 | 12,851 / 5.965 | 8,911 / 14.232 | 0.995× (0.989–1.000) | 1.425× (1.344–1.595) |
| cp313 | sync-stream c64 | 14,821 / 7.634 | 12,835 / 9.892 | 1,271 / 87.864 | 1.008× (1.008–1.155) | 11.660× (10.323–12.449) |
| cp313 | stream c64 | 11,694 / 9.419 | 11,620 / 8.944 | 4,939 / 22.738 | 1.053× (1.006–1.074) | 2.398× (2.368–2.682) |
| cp313 | bytes c64 | 20,641 / 5.680 | 24,513 / 4.548 | 9,742 / 13.678 | 0.857× (0.842–0.961) | 2.195× (1.997–2.360) |
| cp313 | dependencies c64 | 64,048 / 2.192 | 70,456 / 2.151 | 15,705 / 13.613 | 0.940× (0.909–1.024) | 4.273× (3.864–4.682) |

## Core HTTP on Python 3.13 CPU and memory

Framework cells give three-round medians of CPU µs/success / summed RSS MiB. CPU ratios use the same qualified rounds as throughput and give the median and full range of within-round ratios; these need not equal ratios of the displayed medians.

| Python | Workload | Candidate CPU / RSS | Main a4d5b79 CPU / RSS | FastAPI CPU / RSS | Candidate/Main a4d5b79 CPU paired | Candidate/FastAPI CPU paired |
|---|---|---|---|---|---|---|
| cp313 | json c64 | 9.34 / 78.2 | 9.06 / 86.5 | 62.24 / 55.2 | 1.098× (1.031–1.146) | 0.167× (0.137–0.168) |
| cp313 | echo c64 | 16.64 / 87.3 | 17.20 / 86.7 | 93.91 / 55.0 | 0.991× (0.933–1.043) | 0.181× (0.168–0.187) |
| cp313 | io c64 | 13.52 / 86.2 | 13.73 / 86.3 | 52.25 / 55.7 | 0.983× (0.865–1.115) | 0.231× (0.221–0.259) |
| cp313 | fanout c64 | 55.32 / 88.4 | 61.88 / 88.3 | 114.46 / 57.4 | 0.960× (0.891–0.992) | 0.428× (0.414–0.521) |
| cp313 | sync c64 | 39.11 / 88.7 | 36.43 / 88.8 | 116.08 / 58.4 | 1.079× (1.013–1.079) | 0.339× (0.309–0.349) |
| cp313 | sync-stream c64 | 71.56 / 90.7 | 77.86 / 90.2 | 802.83 / 58.6 | 0.968× (0.919–0.992) | 0.089× (0.084–0.096) |
| cp313 | stream c64 | 78.19 / 80.1 | 78.41 / 87.5 | 185.29 / 57.5 | 0.957× (0.927–0.997) | 0.414× (0.376–0.422) |
| cp313 | bytes c64 | 37.47 / 86.7 | 32.06 / 86.9 | 79.74 / 54.9 | 1.143× (1.013–1.168) | 0.462× (0.450–0.489) |
| cp313 | dependencies c64 | 14.41 / 86.2 | 12.97 / 86.3 | 60.19 / 55.5 | 1.055× (0.974–1.111) | 0.225× (0.207–0.251) |

CPU includes sampler drain/export time. RSS sums process trees and can count shared pages more than once; unavailable USS is not reported as zero.

## 256-dict response models on Python 3.13

Framework cells give three-round medians of successful req/s / p99 ms. Paired columns give the median and full range of within-round ratios; these need not equal ratios of the displayed medians.

| Python | Workload | Candidate req/s / p99 | Main a4d5b79 req/s / p99 | FastAPI req/s / p99 | Candidate/Main a4d5b79 paired | Candidate/FastAPI paired |
|---|---|---|---|---|---|---|
| cp313 | models_large c64 | 29,759 / 3.998 | 29,896 / 4.067 | 4,225 / 30.198 | 1.001× (0.975–1.142) | 6.902× (6.562–7.422) |

## 256-dict response models on Python 3.13 CPU and memory

Framework cells give three-round medians of CPU µs/success / summed RSS MiB. CPU ratios use the same qualified rounds as throughput and give the median and full range of within-round ratios; these need not equal ratios of the displayed medians.

| Python | Workload | Candidate CPU / RSS | Main a4d5b79 CPU / RSS | FastAPI CPU / RSS | Candidate/Main a4d5b79 CPU paired | Candidate/FastAPI CPU paired |
|---|---|---|---|---|---|---|
| cp313 | models_large c64 | 31.21 / 89.6 | 30.92 / 83.1 | 216.50 / 55.4 | 0.985× (0.923–1.014) | 0.145× (0.136–0.153) |

CPU includes sampler drain/export time. RSS sums process trees and can count shared pages more than once; unavailable USS is not reported as zero.

## 1000-item typed uploads c16

Framework cells give three-round medians of successful req/s / p99 ms. Paired columns give the median and full range of within-round ratios; these need not equal ratios of the displayed medians.

| Python | Workload | Candidate req/s / p99 | Main a4d5b79 req/s / p99 | FastAPI req/s / p99 | Candidate/Main a4d5b79 paired | Candidate/FastAPI paired |
|---|---|---|---|---|---|---|
| cp313 | echo-large c16 | 2,200 / 12.231 | 2,375 / 10.469 | 641 / 55.306 | 0.977× (0.887–1.054) | 3.694× (3.285–3.702) |

## 1000-item typed uploads c16 CPU and memory

Framework cells give three-round medians of CPU µs/success / summed RSS MiB. CPU ratios use the same qualified rounds as throughput and give the median and full range of within-round ratios; these need not equal ratios of the displayed medians.

| Python | Workload | Candidate CPU / RSS | Main a4d5b79 CPU / RSS | FastAPI CPU / RSS | Candidate/Main a4d5b79 CPU paired | Candidate/FastAPI CPU paired |
|---|---|---|---|---|---|---|
| cp313 | echo-large c16 | 410.28 / 68.9 | 382.86 / 90.2 | 1,395.09 / 58.0 | 1.014× (0.962–1.110) | 0.280× (0.276–0.305) |

CPU includes sampler drain/export time. RSS sums process trees and can count shared pages more than once; unavailable USS is not reported as zero.

## 1000-item typed uploads c64

Framework cells give three-round medians of successful req/s / p99 ms. Paired columns give the median and full range of within-round ratios; these need not equal ratios of the displayed medians.

| Python | Workload | Candidate req/s / p99 | Main a4d5b79 req/s / p99 | FastAPI req/s / p99 | Candidate/Main a4d5b79 paired | Candidate/FastAPI paired |
|---|---|---|---|---|---|---|
| cp313 | echo-large c64 | 2,380 / 36.881 | 2,097 / 46.211 | 652† / 192.638† | 1.026× (0.999–1.135) | withheld (transport_errors) |

## 1000-item typed uploads c64 CPU and memory

Framework cells give three-round medians of CPU µs/success / summed RSS MiB. CPU ratios use the same qualified rounds as throughput and give the median and full range of within-round ratios; these need not equal ratios of the displayed medians.

| Python | Workload | Candidate CPU / RSS | Main a4d5b79 CPU / RSS | FastAPI CPU / RSS | Candidate/Main a4d5b79 CPU paired | Candidate/FastAPI CPU paired |
|---|---|---|---|---|---|---|
| cp313 | echo-large c64 | 382.42 / 96.1 | 424.90 / 73.8 | 1,345.77† / 69.5† | 0.987× (0.900–0.997) | withheld (transport_errors) |

CPU includes sampler drain/export time. RSS sums process trees and can count shared pages more than once; unavailable USS is not reported as zero.

## Generic sync paths across all modes

Framework cells give three-round medians of successful req/s / p99 ms. Paired columns give the median and full range of within-round ratios; these need not equal ratios of the displayed medians.

| Python | Workload | Candidate req/s / p99 | Main a4d5b79 req/s / p99 | FastAPI req/s / p99 | Candidate/Main a4d5b79 paired | Candidate/FastAPI paired |
|---|---|---|---|---|---|---|
| cp313 | generic-request c64 | 35,881 / 3.993 | 33,975 / 4.508 | 5,294 / 32.511 | 1.003× (0.952–1.093) | 6.778× (6.436–6.852) |
| cp314 | generic-request c64 | 43,324 / 3.104 | 37,925 / 4.134 | 6,692 / 17.842 | 1.091× (0.995–1.142) | 6.621× (6.474–7.138) |
| cp314t-gil1 | generic-request c64 | 33,962 / 4.830 | 36,303 / 4.286 | 3,823 / 41.103 | 0.992× (0.936–1.019) | 8.109× (6.592–9.480) |
| cp314t-gil0 | generic-request c64 | 33,089 / 4.424 | 29,768 / 8.153 | 6,835 / 24.953 | 1.072× (0.992–1.112) | 4.361× (3.924–6.023) |
| cp313 | generic-dependency c64 | 47,948 / 3.249 | 44,156 / 3.997 | 5,961 / 18.136 | 1.061× (1.057–1.133) | 8.043× (7.507–9.227) |
| cp314 | generic-dependency c64 | 44,560 / 3.495 | 45,443 / 3.416 | 5,721 / 26.486 | 0.981× (0.938–1.160) | 7.789× (7.072–8.044) |
| cp314t-gil1 | generic-dependency c64 | 48,754 / 3.105 | 38,577 / 3.929 | 3,568 / 52.096 | 1.046× (0.997–1.264) | 11.259× (8.407–14.747) |
| cp314t-gil0 | generic-dependency c64 | 36,408 / 3.855 | 36,569 / 5.504 | 6,250 / 20.428 | 1.108× (0.996–1.255) | 5.863× (5.825–6.111) |

## Generic sync paths across all modes CPU and memory

Framework cells give three-round medians of CPU µs/success / summed RSS MiB. CPU ratios use the same qualified rounds as throughput and give the median and full range of within-round ratios; these need not equal ratios of the displayed medians.

| Python | Workload | Candidate CPU / RSS | Main a4d5b79 CPU / RSS | FastAPI CPU / RSS | Candidate/Main a4d5b79 CPU paired | Candidate/FastAPI CPU paired |
|---|---|---|---|---|---|---|
| cp313 | generic-request c64 | 32.21 / 78.6 | 33.16 / 80.9 | 167.50 / 56.2 | 1.000× (0.967–1.035) | 0.188× (0.184–0.192) |
| cp314 | generic-request c64 | 27.83 / 86.1 | 30.13 / 86.1 | 145.99 / 61.8 | 0.925× (0.923–0.997) | 0.183× (0.175–0.191) |
| cp314t-gil1 | generic-request c64 | 32.73 / 101.3 | 31.80 / 80.8 | 205.69 / 76.5 | 0.997× (0.983–1.029) | 0.166× (0.157–0.205) |
| cp314t-gil0 | generic-request c64 | 40.60 / 89.2 | 41.05 / 73.0 | 186.76 / 76.5 | 0.989× (0.955–1.007) | 0.233× (0.194–0.245) |
| cp313 | generic-dependency c64 | 22.45 / 74.1 | 23.82 / 81.1 | 162.22 / 56.1 | 0.958× (0.924–0.962) | 0.138× (0.130–0.150) |
| cp314 | generic-dependency c64 | 24.02 / 85.5 | 23.36 / 86.4 | 163.68 / 62.1 | 1.023× (0.895–1.028) | 0.147× (0.139–0.155) |
| cp314t-gil1 | generic-dependency c64 | 22.60 / 99.0 | 26.45 / 98.7 | 206.90 / 68.6 | 0.988× (0.854–0.995) | 0.129× (0.104–0.148) |
| cp314t-gil0 | generic-dependency c64 | 30.74 / 79.5 | 30.46 / 87.2 | 186.17 / 75.8 | 0.974× (0.844–1.028) | 0.166× (0.165–0.175) |

CPU includes sampler drain/export time. RSS sums process trees and can count shared pages more than once; unavailable USS is not reported as zero.

## Additional standard/free-threaded HTTP

Framework cells give three-round medians of successful req/s / p99 ms. Paired columns give the median and full range of within-round ratios; these need not equal ratios of the displayed medians.

| Python | Workload | Candidate req/s / p99 | Main a4d5b79 req/s / p99 | FastAPI req/s / p99 | Candidate/Main a4d5b79 paired | Candidate/FastAPI paired |
|---|---|---|---|---|---|---|
| cp314 | json c64 | 91,903 / 2.281 | 90,094 / 2.010 | 15,142 / 9.284 | 1.011× (0.955–1.663) | 5.914× (5.683–10.613) |
| cp314t-gil1 | json c64 | 88,019 / 2.051 | 89,584 / 1.965 | 14,230 / 8.919 | 0.989× (0.983–1.041) | 5.535× (5.431–6.549) |
| cp314t-gil0 | json c64 | 88,871 / 2.023 | 87,354 / 2.332 | 14,669 / 9.589 | 1.017× (0.776–1.427) | 6.206× (5.615–6.988) |
| cp314 | echo c64 | 36,727 / 5.590 | 48,721 / 3.491 | 8,860 / 14.547 | 1.064× (0.708–1.127) | 5.784× (4.146–5.966) |
| cp314t-gil1 | echo c64 | 35,672 / 6.266 | 37,571 / 4.703 | 6,676 / 23.882 | 1.004× (0.949–1.028) | 5.343× (5.055–5.346) |
| cp314t-gil0 | echo c64 | 42,595 / 3.833 | 44,301 / 3.266 | 7,335 / 17.584 | 0.993× (0.961–1.037) | 5.618× (5.580–5.807) |
| cp314 | models_large c64 | 28,162 / 4.266 | 28,884 / 4.080 | 3,955 / 34.566 | 0.996× (0.975–1.051) | 7.197× (6.768–7.297) |
| cp314t-gil1 | models_large c64 | 22,509 / 5.405 | 22,655 / 5.382 | 3,339 / 36.409 | 0.982× (0.978–1.008) | 6.699× (6.696–6.740) |
| cp314t-gil0 | models_large c64 | 22,302 / 5.571 | 22,292 / 6.264 | 3,538 / 34.257 | 1.026× (1.000–1.066) | 6.298× (6.274–7.141) |

## Additional standard/free-threaded HTTP CPU and memory

Framework cells give three-round medians of CPU µs/success / summed RSS MiB. CPU ratios use the same qualified rounds as throughput and give the median and full range of within-round ratios; these need not equal ratios of the displayed medians.

| Python | Workload | Candidate CPU / RSS | Main a4d5b79 CPU / RSS | FastAPI CPU / RSS | Candidate/Main a4d5b79 CPU paired | Candidate/FastAPI CPU paired |
|---|---|---|---|---|---|---|
| cp314 | json c64 | 9.58 / 86.3 | 9.86 / 90.6 | 60.08 / 60.4 | 0.980× (0.608–1.006) | 0.163× (0.093–0.165) |
| cp314t-gil1 | json c64 | 10.06 / 105.7 | 10.04 / 105.8 | 63.98 / 70.8 | 1.002× (0.963–1.029) | 0.168× (0.152–0.179) |
| cp314t-gil0 | json c64 | 9.87 / 105.6 | 9.84 / 105.6 | 60.62 / 70.9 | 1.003× (0.766–1.190) | 0.159× (0.145–0.170) |
| cp314 | echo c64 | 20.94 / 94.1 | 18.56 / 94.3 | 102.04 / 60.3 | 0.963× (0.905–1.242) | 0.170× (0.170–0.205) |
| cp314t-gil1 | echo c64 | 22.71 / 106.3 | 22.23 / 88.0 | 127.52 / 71.0 | 0.971× (0.967–1.022) | 0.179× (0.178–0.196) |
| cp314t-gil0 | echo c64 | 20.49 / 106.1 | 20.15 / 106.0 | 119.47 / 71.1 | 1.003× (0.948–1.017) | 0.174× (0.172–0.178) |
| cp314 | models_large c64 | 32.93 / 96.7 | 32.19 / 89.6 | 226.30 / 60.3 | 1.009× (0.947–1.025) | 0.145× (0.141–0.146) |
| cp314t-gil1 | models_large c64 | 40.80 / 108.9 | 40.51 / 109.1 | 275.62 / 67.0 | 0.991× (0.979–1.012) | 0.149× (0.140–0.150) |
| cp314t-gil0 | models_large c64 | 40.88 / 108.9 | 41.73 / 109.0 | 262.81 / 71.1 | 0.980× (0.933–0.985) | 0.151× (0.145–0.157) |

CPU includes sampler drain/export time. RSS sums process trees and can count shared pages more than once; unavailable USS is not reported as zero.

## CPU work c16

Framework cells give three-round medians of successful req/s / p99 ms. Paired columns give the median and full range of within-round ratios; these need not equal ratios of the displayed medians.

| Python | Workload | Candidate req/s / p99 | Main a4d5b79 req/s / p99 | FastAPI req/s / p99 | Candidate/Main a4d5b79 paired | Candidate/FastAPI paired |
|---|---|---|---|---|---|---|
| cp313 | cpu-inline c16 | 503 / 41.307 | 532 / 38.403 | 495 / 68.062 | 1.021× (0.894–1.023) | 1.112× (0.960–1.123) |
| cp314 | cpu-inline c16 | 648 / 34.362 | 678 / 32.090 | 645 / 52.215 | 0.965× (0.928–1.002) | 1.049× (1.004–1.075) |
| cp314t-gil1 | cpu-inline c16 | 689 / 31.274 | 606 / 36.858 | 581 / 60.197 | 1.152× (0.988–1.204) | 1.131× (1.032–1.293) |
| cp314t-gil0 | cpu-inline c16 | 694 / 31.782 | 628 / 32.897 | 558 / 58.315 | 1.037× (1.010–1.106) | 1.146× (1.117–1.244) |
| cp313 | cpu-sync c16 | 529 / 60.426 | 499 / 59.424 | 426 / 60.049 | 1.010× (0.994–1.060) | 1.167× (1.146–1.244) |
| cp314 | cpu-sync c16 | 647 / 44.006 | 589 / 55.975 | 519 / 56.679 | 1.118× (0.941–1.128) | 1.084× (1.020–1.417) |
| cp314t-gil1 | cpu-sync c16 | 653 / 43.695 | 591 / 46.913 | 511 / 51.786 | 1.046× (0.922–1.153) | 1.278× (1.222–1.534) |
| cp314t-gil0 | cpu-sync c16 | 1,854 / 14.120 | 1,655 / 16.415 | 1,511 / 17.239 | 1.143× (1.103–1.272) | 1.227× (1.169–1.408) |
| cp313 | cpu-offload-auto c16 | 1,704 / 22.374 | 1,665 / 19.413 | 1,669 / 20.165 | 0.996× (0.991–1.050) | 1.015× (0.994–1.032) |
| cp314 | cpu-offload-auto c16 | 2,093 / 18.048 | 2,146 / 16.797 | 2,098 / 19.391 | 1.000× (0.957–1.005) | 1.002× (0.997–1.004) |
| cp314t-gil1 | cpu-offload-auto c16 | 1,972 / 20.870 | 1,984 / 17.895 | 1,983 / 19.794 | 0.994× (0.967–0.999) | 0.995× (0.969–1.037) |
| cp314t-gil0 | cpu-offload-auto c16 | 2,171 / 12.343 | 2,141 / 13.619 | 2,217 / 12.495 | 1.014× (0.917–1.074) | 0.979× (0.896–1.068) |

## CPU work c16 CPU and memory

Framework cells give three-round medians of CPU µs/success / summed RSS MiB. CPU ratios use the same qualified rounds as throughput and give the median and full range of within-round ratios; these need not equal ratios of the displayed medians.

| Python | Workload | Candidate CPU / RSS | Main a4d5b79 CPU / RSS | FastAPI CPU / RSS | Candidate/Main a4d5b79 CPU paired | Candidate/FastAPI CPU paired |
|---|---|---|---|---|---|---|
| cp313 | cpu-inline c16 | 1,857.10 / 79.4 | 1,774.44 / 79.1 | 1,908.23 / 72.4 | 0.973× (0.973–1.126) | 0.900× (0.894–1.047) |
| cp314 | cpu-inline c16 | 1,451.27 / 84.7 | 1,394.30 / 81.5 | 1,464.98 / 81.4 | 1.046× (1.001–1.056) | 0.957× (0.935–0.991) |
| cp314t-gil1 | cpu-inline c16 | 1,378.37 / 96.9 | 1,556.01 / 97.0 | 1,597.92 / 95.2 | 0.886× (0.843–0.995) | 0.901× (0.790–0.969) |
| cp314t-gil0 | cpu-inline c16 | 1,379.61 / 97.0 | 1,512.82 / 96.9 | 1,674.15 / 94.0 | 0.976× (0.912–0.995) | 0.899× (0.824–0.904) |
| cp313 | cpu-sync c16 | 1,806.78 / 80.2 | 1,898.94 / 73.4 | 2,148.24 / 72.4 | 0.994× (0.939–1.006) | 0.886× (0.830–0.894) |
| cp314 | cpu-sync c16 | 1,466.14 / 78.4 | 1,561.92 / 85.8 | 1,728.96 / 81.8 | 0.937× (0.916–1.023) | 0.924× (0.778–0.971) |
| cp314t-gil1 | cpu-sync c16 | 1,459.33 / 98.3 | 1,585.45 / 98.4 | 1,741.62 / 98.5 | 0.963× (0.920–1.086) | 0.838× (0.806–0.846) |
| cp314t-gil0 | cpu-sync c16 | 2,111.02 / 98.4 | 2,289.23 / 79.8 | 2,381.87 / 98.5 | 0.866× (0.822–0.926) | 0.890× (0.743–0.901) |
| cp313 | cpu-offload-auto c16 | 2,493.17 / 222.8 | 2,501.34 / 230.2 | 2,548.14 / 151.8 | 0.981× (0.975–0.997) | 0.948× (0.941–0.985) |
| cp314 | cpu-offload-auto c16 | 2,056.07 / 244.3 | 1,988.28 / 250.9 | 2,046.84 / 183.7 | 0.998× (0.991–1.039) | 0.964× (0.962–1.009) |
| cp314t-gil1 | cpu-offload-auto c16 | 2,056.78 / 275.0 | 2,102.33 / 280.6 | 2,103.39 / 211.0 | 0.991× (0.969–1.017) | 0.981× (0.959–1.017) |
| cp314t-gil0 | cpu-offload-auto c16 | 2,021.63 / 249.9 | 2,065.00 / 280.6 | 2,070.43 / 211.1 | 0.979× (0.965–1.051) | 0.976× (0.919–1.026) |

CPU includes sampler drain/export time. RSS sums process trees and can count shared pages more than once; unavailable USS is not reported as zero.

## CPU sync c64

Framework cells give three-round medians of successful req/s / p99 ms. Paired columns give the median and full range of within-round ratios; these need not equal ratios of the displayed medians.

| Python | Workload | Candidate req/s / p99 | Main a4d5b79 req/s / p99 | FastAPI req/s / p99 | Candidate/Main a4d5b79 paired | Candidate/FastAPI paired |
|---|---|---|---|---|---|---|
| cp313 | cpu-sync c64 | 545 / 186.300 | 487 / 199.144 | 495 / 162.128 | 0.986× (0.953–1.187) | 1.102× (1.060–1.127) |
| cp314 | cpu-sync c64 | 597 / 180.161 | 608 / 170.191 | 540 / 159.510 | 0.979× (0.974–0.982) | 1.116× (1.107–1.155) |

## CPU sync c64 CPU and memory

Framework cells give three-round medians of CPU µs/success / summed RSS MiB. CPU ratios use the same qualified rounds as throughput and give the median and full range of within-round ratios; these need not equal ratios of the displayed medians.

| Python | Workload | Candidate CPU / RSS | Main a4d5b79 CPU / RSS | FastAPI CPU / RSS | Candidate/Main a4d5b79 CPU paired | Candidate/FastAPI CPU paired |
|---|---|---|---|---|---|---|
| cp313 | cpu-sync c64 | 1,765.04 / 81.2 | 1,942.44 / 73.4 | 1,923.24 / 73.7 | 1.016× (0.872–1.038) | 0.929× (0.918–0.950) |
| cp314 | cpu-sync c64 | 1,556.26 / 79.5 | 1,549.64 / 87.1 | 1,705.21 / 82.9 | 1.014× (0.997–1.019) | 0.900× (0.894–0.913) |

CPU includes sampler drain/export time. RSS sums process trees and can count shared pages more than once; unavailable USS is not reported as zero.

## Free-threaded / standard interpreter ratios

Each ratio pairs the same role and workload within each natural round; modes execute sequentially on the shared host. Ratios give the three-round median and range. CPU ratios are CPU time per successful reply.

| Role | Case | Modes, numerator/denominator | Paired RPS | Paired CPU/success |
|---|---|---|---|---|
| candidate | cpu-inline | cp314t-gil1/cp314 | 1.035× (0.995–1.065) | 0.976× (0.950–0.987) |
| candidate | cpu-inline | cp314t-gil0/cp314 | 1.049× (1.024–1.072) | 0.961× (0.951–0.984) |
| candidate | cpu-inline | cp314t-gil0/cp314t-gil1 | 1.007× (0.989–1.055) | 1.001× (0.974–1.008) |
| candidate | cpu-sync | cp314t-gil1/cp314 | 1.009× (0.968–1.043) | 0.995× (0.970–1.081) |
| candidate | cpu-sync | cp314t-gil0/cp314 | 3.024× (2.772–3.293) | 1.321× (1.309–1.471) |
| candidate | cpu-sync | cp314t-gil0/cp314t-gil1 | 2.997× (2.657–3.401) | 1.315× (1.222–1.516) |
| candidate | cpu-offload-auto | cp314t-gil1/cp314 | 0.951× (0.919–0.987) | 1.040× (0.996–1.053) |
| candidate | cpu-offload-auto | cp314t-gil0/cp314 | 1.037× (0.938–1.097) | 0.979× (0.973–1.059) |
| candidate | cpu-offload-auto | cp314t-gil0/cp314t-gil1 | 1.051× (1.021–1.153) | 0.983× (0.935–1.006) |
| main | cpu-inline | cp314t-gil1/cp314 | 0.901× (0.821–0.972) | 1.102× (1.037–1.191) |
| main | cpu-inline | cp314t-gil0/cp314 | 0.990× (0.900–1.003) | 1.011× (1.008–1.101) |
| main | cpu-inline | cp314t-gil0/cp314t-gil1 | 1.096× (1.032–1.100) | 0.925× (0.915–0.974) |
| main | cpu-sync | cp314t-gil1/cp314 | 0.987× (0.987–1.115) | 0.991× (0.945–1.019) |
| main | cpu-sync | cp314t-gil0/cp314 | 2.808× (2.434–2.985) | 1.489× (1.385–1.644) |
| main | cpu-sync | cp314t-gil0/cp314t-gil1 | 2.518× (2.466–3.023) | 1.576× (1.398–1.614) |
| main | cpu-offload-auto | cp314t-gil1/cp314 | 0.945× (0.924–0.989) | 1.044× (1.020–1.077) |
| main | cpu-offload-auto | cp314t-gil0/cp314 | 1.023× (0.979–1.027) | 1.006× (0.998–1.039) |
| main | cpu-offload-auto | cp314t-gil0/cp314t-gil1 | 1.039× (1.036–1.106) | 0.986× (0.927–0.995) |
| fastapi | cpu-inline | cp314t-gil1/cp314 | 0.960× (0.827–1.036) | 1.037× (0.953–1.190) |
| fastapi | cpu-inline | cp314t-gil0/cp314 | 0.961× (0.866–0.984) | 1.041× (1.000–1.143) |
| fastapi | cpu-inline | cp314t-gil0/cp314t-gil1 | 1.001× (0.950–1.048) | 1.005× (0.960–1.049) |
| fastapi | cpu-sync | cp314t-gil1/cp314 | 0.805× (0.684–1.210) | 1.154× (0.892–1.240) |
| fastapi | cpu-sync | cp314t-gil0/cp314 | 2.910× (2.191–3.361) | 1.355× (1.286–1.712) |
| fastapi | cpu-sync | cp314t-gil0/cp314t-gil1 | 2.778× (2.721–4.253) | 1.442× (1.092–1.484) |
| fastapi | cpu-offload-auto | cp314t-gil1/cp314 | 0.950× (0.926–0.986) | 1.035× (0.984–1.048) |
| fastapi | cpu-offload-auto | cp314t-gil0/cp314 | 1.049× (1.031–1.057) | 1.012× (0.995–1.018) |
| fastapi | cpu-offload-auto | cp314t-gil0/cp314t-gil1 | 1.113× (1.046–1.133) | 0.965× (0.961–1.035) |
| candidate | generic-request | cp314t-gil1/cp314 | 0.882× (0.715–0.923) | 1.151× (1.051–1.225) |
| candidate | generic-request | cp314t-gil0/cp314 | 0.885× (0.619–0.899) | 1.380× (1.303–1.561) |
| candidate | generic-request | cp314t-gil0/cp314t-gil1 | 0.974× (0.865–1.003) | 1.240× (1.198–1.274) |
| candidate | generic-dependency | cp314t-gil1/cp314 | 0.978× (0.901–1.168) | 1.031× (0.921–1.114) |
| candidate | generic-dependency | cp314t-gil0/cp314 | 0.858× (0.817–0.903) | 1.253× (1.244–1.303) |
| candidate | generic-dependency | cp314t-gil0/cp314t-gil1 | 0.906× (0.734–0.923) | 1.206× (1.170–1.360) |
| candidate | json | cp314t-gil1/cp314 | 1.014× (0.602–1.023) | 1.015× (1.013–1.647) |
| candidate | json | cp314t-gil0/cp314 | 0.967× (0.957–1.264) | 1.030× (0.865–1.038) |
| candidate | json | cp314t-gil0/cp314t-gil1 | 0.954× (0.936–2.101) | 1.016× (0.525–1.023) |
| candidate | echo | cp314t-gil1/cp314 | 0.872× (0.844–0.971) | 1.128× (1.084–1.154) |
| candidate | echo | cp314t-gil0/cp314 | 0.854× (0.838–1.160) | 1.121× (0.979–1.198) |
| candidate | echo | cp314t-gil0/cp314t-gil1 | 1.012× (0.961–1.194) | 0.971× (0.902–1.062) |
| candidate | models_large | cp314t-gil1/cp314 | 0.801× (0.631–0.831) | 1.245× (1.236–1.435) |
| candidate | models_large | cp314t-gil0/cp314 | 0.791× (0.708–0.833) | 1.250× (1.242–1.308) |
| candidate | models_large | cp314t-gil0/cp314t-gil1 | 1.003× (0.988–1.122) | 0.997× (0.912–1.011) |
| main | generic-request | cp314t-gil1/cp314 | 0.944× (0.824–0.982) | 1.067× (1.018–1.151) |
| main | generic-request | cp314t-gil0/cp314 | 0.805× (0.713–0.900) | 1.336× (1.314–1.431) |
| main | generic-request | cp314t-gil0/cp314t-gil1 | 0.865× (0.820–0.954) | 1.252× (1.244–1.291) |
| main | generic-dependency | cp314t-gil1/cp314 | 0.867× (0.845–1.138) | 1.103× (0.928–1.159) |
| main | generic-dependency | cp314t-gil0/cp314 | 0.805× (0.726–0.835) | 1.316× (1.304–1.319) |
| main | generic-dependency | cp314t-gil0/cp314t-gil1 | 0.837× (0.733–0.952) | 1.193× (1.125–1.421) |
| main | json | cp314t-gil1/cp314 | 0.994× (0.961–1.037) | 1.019× (0.965–1.040) |
| main | json | cp314t-gil0/cp314 | 1.179× (0.961–1.473) | 0.877× (0.687–1.006) |
| main | json | cp314t-gil0/cp314t-gil1 | 1.185× (0.927–1.532) | 0.861× (0.660–1.042) |
| main | echo | cp314t-gil1/cp314 | 0.924× (0.725–0.926) | 1.125× (1.076–1.318) |
| main | echo | cp314t-gil0/cp314 | 0.898× (0.854–0.928) | 1.151× (1.070–1.195) |
| main | echo | cp314t-gil0/cp314t-gil1 | 1.002× (0.972–1.179) | 0.995× (0.906–1.024) |
| main | models_large | cp314t-gil1/cp314 | 0.774× (0.643–0.890) | 1.279× (1.165–1.481) |
| main | models_large | cp314t-gil0/cp314 | 0.752× (0.662–0.875) | 1.301× (1.200–1.415) |
| main | models_large | cp314t-gil0/cp314t-gil1 | 0.984× (0.971–1.029) | 1.017× (0.956–1.030) |
| fastapi | generic-request | cp314t-gil1/cp314 | 0.695× (0.571–0.886) | 1.174× (1.027–1.409) |
| fastapi | generic-request | cp314t-gil0/cp314 | 1.066× (1.021–1.343) | 1.174× (1.033–1.279) |
| fastapi | generic-request | cp314t-gil0/cp314t-gil1 | 1.534× (1.516–1.788) | 1.000× (0.908–1.005) |
| fastapi | generic-dependency | cp314t-gil1/cp314 | 0.637× (0.624–0.823) | 1.232× (1.081–1.264) |
| fastapi | generic-dependency | cp314t-gil0/cp314 | 1.093× (1.089–1.129) | 1.106× (1.051–1.150) |
| fastapi | generic-dependency | cp314t-gil0/cp314t-gil1 | 1.752× (1.324–1.772) | 0.910× (0.853–1.023) |
| fastapi | json | cp314t-gil1/cp314 | 1.070× (0.916–1.154) | 0.935× (0.908–1.088) |
| fastapi | json | cp314t-gil0/cp314 | 0.969× (0.921–1.920) | 1.009× (0.554–1.057) |
| fastapi | json | cp314t-gil0/cp314t-gil1 | 1.006× (0.905–1.664) | 0.971× (0.610–1.079) |
| fastapi | echo | cp314t-gil1/cp314 | 0.944× (0.754–0.996) | 1.070× (0.997–1.250) |
| fastapi | echo | cp314t-gil0/cp314 | 0.862× (0.828–0.913) | 1.139× (1.091–1.171) |
| fastapi | echo | cp314t-gil0/cp314t-gil1 | 0.917× (0.914–1.099) | 1.065× (0.937–1.095) |
| fastapi | models_large | cp314t-gil1/cp314 | 0.839× (0.674–0.873) | 1.218× (1.157–1.485) |
| fastapi | models_large | cp314t-gil0/cp314 | 0.813× (0.790–0.917) | 1.250× (1.122–1.251) |
| fastapi | models_large | cp314t-gil0/cp314t-gil1 | 1.050× (0.941–1.205) | 0.969× (0.842–1.026) |

## Single-client functional controls

These are eight one-round observations on candidate/main, with no FastAPI population and no paired performance claims.

| Mode | Control | Candidate req/s | Main req/s |
|---|---|---|---|
| cp313 | cpu-inline | 280 | 292 |
| cp313 | cpu-sync | 272 | 242 |
| cp314 | cpu-inline | 342 | 356 |
| cp314 | cpu-sync | 341 | 338 |

## Shared host context

Ranges are observed during natural load. They do not establish CPU isolation; first unprimed psutil readings are excluded. Each case cell gives host CPU-percent range / one-minute load-average range. Full 1/5/15-minute ranges remain in the public JSON summary.

| Context | Observed range |
|---|---|
| Host CPU % | 0.0–100.0 |
| Load average 1 min | 10.74–26.30 |
| Load average 5 min | 13.49–21.87 |
| Load average 15 min | 15.99–20.02 |

| Mode | Case | Candidate CPU% / load1m | Main CPU% / load1m | FastAPI CPU% / load1m |
|---|---|---|---|---|
| cp313 | cpu-inline c16 | 0.0–74.3 / 14.95–17.65 | 38.6–85.4 / 14.87–20.38 | 0.0–87.0 / 15.47–19.39 |
| cp314 | cpu-inline c16 | 0.0–83.6 / 14.40–20.81 | 0.0–79.4 / 14.29–21.02 | 0.0–84.4 / 14.51–21.53 |
| cp314t-gil1 | cpu-inline c16 | 0.0–92.1 / 16.57–21.02 | 0.0–95.1 / 18.66–20.81 | 0.0–82.5 / 15.75–20.59 |
| cp314t-gil0 | cpu-inline c16 | 0.0–85.4 / 16.83–20.38 | 0.0–86.0 / 16.20–20.22 | 0.0–92.9 / 16.46–20.08 |
| cp313 | cpu-sync c16 | 0.0–89.3 / 16.43–22.53 | 0.0–84.5 / 17.25–22.78 | 0.0–93.6 / 17.40–23.28 |
| cp314 | cpu-sync c16 | 0.0–93.6 / 16.75–24.53 | 0.0–91.5 / 16.77–24.53 | 0.0–97.7 / 16.72–23.99 |
| cp314t-gil1 | cpu-sync c16 | 0.0–94.9 / 14.13–23.53 | 0.0–93.7 / 14.05–23.14 | 48.0–99.3 / 14.05–22.73 |
| cp314t-gil0 | cpu-sync c16 | 0.0–97.5 / 14.69–22.61 | 0.0–99.9 / 14.69–24.27 | 60.6–98.1 / 14.50–24.27 |
| cp313 | cpu-offload-auto c16 | 0.0–96.1 / 21.37–25.46 | 73.0–94.1 / 20.51–23.13 | 0.0–95.2 / 20.60–22.96 |
| cp314 | cpu-offload-auto c16 | 73.7–100.0 / 20.03–22.70 | 0.0–94.0 / 20.45–22.70 | 0.0–97.3 / 20.28–21.94 |
| cp314t-gil1 | cpu-offload-auto c16 | 0.0–95.6 / 17.92–22.40 | 0.0–98.4 / 16.59–22.93 | 0.0–96.4 / 18.95–22.93 |
| cp314t-gil0 | cpu-offload-auto c16 | 0.0–99.1 / 20.37–23.37 | 0.0–96.6 / 20.97–22.91 | 0.0–97.3 / 19.41–23.37 |
| cp313 | cpu-sync c64 | 0.0–89.9 / 15.94–21.88 | 37.2–91.7 / 15.59–21.49 | 0.0–84.6 / 15.59–20.53 |
| cp314 | cpu-sync c64 | 0.0–91.0 / 16.02–22.28 | 0.0–91.7 / 16.02–21.69 | 33.2–91.9 / 16.81–22.35 |
| cp313 | json c64 | 0.0–98.6 / 18.74–23.06 | 0.0–98.6 / 14.78–22.25 | 0.0–99.5 / 16.15–21.70 |
| cp313 | echo c64 | 0.0–88.3 / 14.24–21.04 | 0.0–92.6 / 15.19–21.04 | 0.0–84.1 / 14.61–21.36 |
| cp313 | io c64 | 0.0–81.3 / 15.47–21.36 | 0.0–89.0 / 15.69–20.40 | 0.0–89.1 / 15.93–20.40 |
| cp313 | fanout c64 | 37.2–96.3 / 14.13–19.74 | 40.2–92.7 / 14.36–19.74 | 0.0–91.2 / 14.76–18.80 |
| cp313 | sync c64 | 0.0–85.8 / 13.40–18.82 | 0.0–87.3 / 12.31–18.35 | 0.0–82.9 / 14.04–18.56 |
| cp313 | sync-stream c64 | 0.0–87.8 / 12.04–18.43 | 38.3–88.6 / 12.01–18.64 | 0.0–87.3 / 12.04–18.04 |
| cp313 | stream c64 | 0.0–87.5 / 11.50–18.76 | 0.0–88.5 / 11.50–18.22 | 0.0–85.9 / 11.69–17.89 |
| cp313 | bytes c64 | 0.0–94.9 / 11.95–17.42 | 0.0–95.3 / 11.95–18.19 | 0.0–93.1 / 11.25–18.81 |
| cp313 | dependencies c64 | 0.0–87.8 / 11.78–19.21 | 0.0–98.6 / 11.78–18.79 | 0.0–100.0 / 11.87–18.57 |
| cp313 | models_large c64 | 0.0–100.0 / 11.27–18.60 | 0.0–99.2 / 11.27–19.32 | 0.0–87.6 / 11.73–17.65 |
| cp313 | echo-large c16 | 0.0–86.2 / 11.82–18.06 | 0.0–90.9 / 11.87–20.47 | 0.0–95.3 / 12.06–19.79 |
| cp313 | echo-large c64 | 0.0–88.4 / 11.70–19.79 | 0.0–92.9 / 11.22–22.31 | 0.0–87.3 / 12.53–22.01 |
| cp313 | generic-request c64 | 0.0–97.8 / 11.83–22.09 | 0.0–90.4 / 11.76–21.52 | 41.9–92.3 / 11.37–22.86 |
| cp314 | generic-request c64 | 0.0–88.6 / 11.78–23.00 | 0.0–94.6 / 14.26–24.07 | 0.0–94.4 / 13.51–23.34 |
| cp314t-gil1 | generic-request c64 | 0.0–88.4 / 11.51–20.54 | 0.0–95.0 / 11.22–19.93 | 0.0–92.9 / 11.28–21.37 |
| cp314t-gil0 | generic-request c64 | 0.0–93.3 / 11.65–18.82 | 0.0–92.9 / 11.65–18.95 | 0.0–96.1 / 11.51–21.54 |
| cp313 | generic-dependency c64 | 49.3–84.8 / 13.20–21.57 | 0.0–93.4 / 13.20–22.91 | 0.0–90.0 / 12.72–22.91 |
| cp314 | generic-dependency c64 | 0.0–95.7 / 13.74–24.18 | 0.0–86.9 / 13.88–26.30 | 0.0–99.6 / 13.48–26.30 |
| cp314t-gil1 | generic-dependency c64 | 0.0–92.4 / 12.62–22.60 | 0.0–93.0 / 13.31–22.60 | 38.3–94.4 / 12.62–23.84 |
| cp314t-gil0 | generic-dependency c64 | 0.0–91.9 / 12.90–21.29 | 0.0–92.7 / 13.13–22.07 | 0.0–90.5 / 13.32–21.12 |
| cp314 | json c64 | 49.9–96.9 / 13.33–21.60 | 0.0–88.7 / 20.79–23.26 | 0.0–93.8 / 19.96–23.75 |
| cp314t-gil1 | json c64 | 0.0–91.8 / 18.59–25.00 | 0.0–90.9 / 19.38–24.72 | 0.0–84.1 / 18.99–24.83 |
| cp314t-gil0 | json c64 | 0.0–92.4 / 13.70–20.30 | 0.0–94.6 / 13.45–21.47 | 0.0–98.1 / 13.76–20.15 |
| cp314 | echo c64 | 61.4–98.7 / 16.92–22.44 | 0.0–90.1 / 16.92–20.62 | 0.0–91.6 / 18.11–20.54 |
| cp314t-gil1 | echo c64 | 0.0–99.5 / 19.36–21.31 | 0.0–93.9 / 18.93–22.08 | 0.0–99.3 / 19.60–21.59 |
| cp314t-gil0 | echo c64 | 0.0–93.2 / 18.29–25.04 | 0.0–89.3 / 19.31–23.43 | 49.8–92.6 / 18.89–25.03 |
| cp314 | models_large c64 | 0.0–93.9 / 11.67–22.07 | 0.0–91.0 / 11.62–23.40 | 0.0–89.3 / 10.74–22.07 |
| cp314t-gil1 | models_large c64 | 0.0–88.9 / 13.83–22.19 | 0.0–88.6 / 10.74–22.19 | 0.0–98.3 / 15.15–21.87 |
| cp314t-gil0 | models_large c64 | 0.0–86.3 / 12.81–21.80 | 0.0–88.8 / 12.08–21.80 | 0.0–86.6 / 12.88–21.36 |

## Failures and qualification

| Variant | Measured/attempted | Measured HTTP statuses | Transport errors | Wrong lengths | Warmup non-200 | Warmup transport | Measured error reasons |
|---|---|---|---|---|---|---|---|
| Candidate | 133/133 | {"200": 14304088} | 0 | 0 | 0 | 0 | {} |
| Main a4d5b79 | 133/133 | {"200": 14035532} | 0 | 0 | 0 | 0 | {} |
| FastAPI 0.162.2 | 129/129 | {"200": 2814758} | 20 | 0 | 0 | 0 | {"timeout": 20} |

Lifecycle issues: 0. Any failed or missing round withholds the entire paired population, including its CPU comparison. † marks an unqualified diagnostic median; it is not used for a speedup claim.

| Phase | Python | Variant | Workload | Round | Measured failures | Warmup failures | Warmup issues |
|---|---|---|---|---|---|---|---|
| http-large-c64 | cp313 | fastapi | echo-large | 2 | 10 | 0 | — |
| http-large-c64 | cp313 | fastapi | echo-large | 3 | 10 | 0 | — |

## Separate scheduling controls

Twenty-four owned worker SIGSTOP/SIGCONT controls use 16 clients and four sync slots on standard 3.13/3.14. Every status/body, runtime, final SIGCONT and cleanup is checked. These artificial pauses provide a scheduling diagnostic; their rates/counts are excluded from the natural-load capacity comparisons. Zero baseline refusals alone would not reproduce or prove the accounting correction.

| Role:mode | Control samples | HTTP statuses | Transport errors |
|---|---|---|---|
| candidate:cp313 | 6 | {"200": 2741} | {} |
| main:cp313 | 6 | {"200": 2753} | {} |
| main:cp314 | 6 | {"200": 3406} | {} |
| candidate:cp314 | 6 | {"200": 3385} | {} |

## Targeted regression evidence

The historical paused-time Rust regression failed on request 2 under e26b3c78. Its adaptive source Git blob exactly equals a4d5b79 main (73f81991aaaf0c2fc3d92ce9a8fdf0791c9b895e), and its native artifact matches the verified main cp313 baseline. The source+fragment, execution receipt and failing log are hash-bound. This is historical baseline evidence, not a new run or proof of the old Intel CI cause.

Fresh candidate Rust regressions are part of the full installed gate and frozen source/log receipts. Their correctness evidence is separate from the 395 natural measurements and paused scheduling counts.

## Validation and scope

Full installed gate: 344 Rust tests passed, 2 ignored; Python and harness counts are included in the validation receipt. All five focused interpreter/GIL gates passed and matched the measured native hashes.

Every full three-round comparison requires zero measured and warmup status/transport/length failures, complete positive warmup records, semantic body probes, stable package/native/GIL/JIT/pool identity, client below 1.7 CPU cores and clean shutdown. Independent SQL audit covers all measured databases and binds the exact plan and result files.

Shared unpinned host; no OS CPU quota or affinity is asserted. Four CPU/sync slots size pools and do not isolate processors. Recorded host ranges describe interference. CPU/request includes drain/export and summed RSS can double-count shared pages. This does not establish formal M0, a fixed-rate production SLO, billing, GPU acceleration or worker-scaling acceptance.

Full raw SQLite, response probes, execution logs and source/native receipts remain in the local study cache. Public summaries retain hashes and version identities without local filesystem paths.

## Local data and reproduction

This public directory is an allowlisted result subset, not a standalone benchmark harness. Exact dependency pins and per-ABI native/wheel/source hashes are in `provenance.json`; the executed test gates, independent audit and post-timing guards have separate receipts.

The local raw study cache is named `cpu-accounting-edge-2026-11-17`. It contains the frozen harness and plan, exact pinned requirements, raw SQLite, response probes, logs, source/build manifests and installed environments. From that cache, regenerate the analysis, audit and public report from the recorded data with:

```sh
./venv-candidate-cp313/bin/python analyse_bench.py --timings-finished
./venv-candidate-cp313/bin/python audit_sql.py --timings-finished
./venv-candidate-cp313/bin/python write_public_report.py --timings-finished --out reproduced-public-report
```

A fresh timing study requires a new cache/frozen plan and validated environments; preserve the recorded run.
