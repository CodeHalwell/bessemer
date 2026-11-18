# Four-workload performance benchmark

Completed 395 fresh samples; 393 qualified. Measured 40,317,608 successful HTTP responses. All raw failures are retained and any failed comparison is withheld.

Candidate is `f0ec5d3 base + uncommitted four-workload performance changes`, identified by its frozen source/native/wheel manifests and working-tree diff hash. The module version 1.0.0rc7 and HEAD alone do not identify candidate code. Baselines are exact main `f0ec5d3657b97cb7a1a82a397c6223e28954c4f6` and FastAPI 0.162.2. Main artifacts are read-only PR132 candidate source wheels: all 103 original runtime/build source hashes match main. Candidate source wheels use the same Rust 1.92.0 release profile with strip=none. Published-wheel throughput is not claimed.

This study evaluates performance changes across four previously observed decreases. Correctness, targeted four-case comparisons and paused scheduling controls are separate from the natural matrix. The full matrix records rates, p99, CPU per successful reply and memory across the existing workloads; a throughput increase is not assumed.

43 candidate/main workload comparisons qualified for all three paired rounds. Their per-workload median throughput ratios span 0.871–1.188×; this is a range across distinct workloads, not a pooled speed score. All failures and mixed results remain visible.

These configurations measured lower candidate throughput in all three paired rounds. No cause is assigned.

| Case (phase:mode:workload) | Paired candidate/main RPS | Paired candidate/main CPU/success |
|---|---|---|
| cpu-c16:cp313:cpu-inline | 0.987× (0.975–0.996) | 1.008× (1.001–1.018) |
| cpu-c16:cp313:cpu-sync | 0.965× (0.963–0.975) | 1.033× (1.026–1.037) |
| http-core:cp313:echo | 0.892× (0.836–0.996) | 1.124× (0.993–1.179) |
| http-generic-sync:cp313:generic-dependency | 0.949× (0.845–0.952) | 1.061× (1.058–1.127) |
| http-ft:cp314:echo | 0.947× (0.939–0.960) | 1.044× (1.034–1.060) |
| http-ft:cp314t-gil0:echo | 0.943× (0.926–0.973) | 1.050× (1.022–1.083) |

The study has 387 three-round performance samples and eight separate one-round functional controls. All 43 performance case groups use every contender position once across three rounds. Python 3.13.18, standard 3.14.7 and free-threaded 3.14.7 with GIL on/off are recorded explicitly. JIT is disabled and checked before/after; GIL-off is never forced past an incompatible import.

## Core HTTP on Python 3.13

Framework cells give three-round medians of successful req/s / p99 ms. Paired columns give the median and full range of within-round ratios; these need not equal ratios of the displayed medians.

| Python | Workload | Candidate req/s / p99 | Main f0ec5d3 req/s / p99 | FastAPI req/s / p99 | Candidate/Main f0ec5d3 paired | Candidate/FastAPI paired |
|---|---|---|---|---|---|---|
| cp313 | json c64 | 131,410 / 1.055 | 131,462 / 1.077 | 20,103 / 6.644 | 1.000× (0.968–1.070) | 6.402× (6.162–7.223) |
| cp313 | echo c64 | 63,223 / 2.555 | 71,194 / 2.372 | 11,373 / 11.017 | 0.892× (0.836–0.996) | 5.559× (5.491–6.015) |
| cp313 | io c64 | 48,244 / 2.694 | 48,039 / 2.781 | 20,071 / 9.534 | 1.004× (1.004–1.010) | 2.404× (2.204–2.440) |
| cp313 | fanout c64 | 21,273 / 8.076 | 22,042 / 7.734 | 10,379 / 15.673 | 1.049× (0.927–1.353) | 2.161× (2.050–2.329) |
| cp313 | sync c64 | 12,931 / 6.199 | 12,908 / 6.323 | 9,994 / 12.628 | 1.002× (1.001–1.006) | 1.294× (1.071–1.497) |
| cp313 | sync-stream c64 | 17,689 / 7.189 | 16,620 / 7.478 | 1,661 / 65.707 | 0.974× (0.973–1.064) | 10.649× (10.125–10.815) |
| cp313 | stream c64 | 14,195 / 8.665 | 13,331 / 8.482 | 5,804 / 23.000 | 1.062× (1.018–1.085) | 2.618× (2.446–2.793) |
| cp313 | bytes c64 | 26,165 / 4.534 | 23,563 / 4.965 | 11,374 / 11.308 | 1.110× (0.939–1.147) | 2.391× (2.294–2.406) |
| cp313 | dependencies c64 | 83,218 / 2.201 | 78,547 / 2.233 | 16,509 / 11.421 | 0.992× (0.931–1.059) | 4.709× (4.539–5.142) |

## Core HTTP on Python 3.13 CPU and memory

Framework cells give three-round medians of CPU µs/success / summed RSS MiB. CPU ratios use the same qualified rounds as throughput and give the median and full range of within-round ratios; these need not equal ratios of the displayed medians.

| Python | Workload | Candidate CPU / RSS | Main f0ec5d3 CPU / RSS | FastAPI CPU / RSS | Candidate/Main f0ec5d3 CPU paired | Candidate/FastAPI CPU paired |
|---|---|---|---|---|---|---|
| cp313 | json c64 | 7.03 / 86.0 | 6.97 / 79.0 | 45.24 / 54.9 | 1.007× (0.934–1.022) | 0.158× (0.139–0.164) |
| cp313 | echo c64 | 14.47 / 86.6 | 12.86 / 79.5 | 80.63 / 55.0 | 1.124× (0.993–1.179) | 0.180× (0.166–0.181) |
| cp313 | io c64 | 10.50 / 85.8 | 10.72 / 78.9 | 46.86 / 55.3 | 0.972× (0.946–1.024) | 0.222× (0.218–0.242) |
| cp313 | fanout c64 | 44.27 / 87.5 | 42.89 / 80.4 | 92.52 / 57.3 | 0.957× (0.759–1.067) | 0.458× (0.431–0.479) |
| cp313 | sync c64 | 36.07 / 88.9 | 35.71 / 88.9 | 107.69 / 58.1 | 1.000× (0.950–1.010) | 0.329× (0.305–0.391) |
| cp313 | sync-stream c64 | 61.83 / 83.4 | 64.72 / 83.4 | 651.98 / 58.8 | 1.020× (0.955–1.024) | 0.095× (0.093–0.098) |
| cp313 | stream c64 | 64.25 / 80.2 | 68.03 / 81.0 | 158.03 / 57.3 | 0.942× (0.923–0.983) | 0.383× (0.357–0.407) |
| cp313 | bytes c64 | 29.24 / 86.5 | 31.63 / 79.3 | 65.76 / 54.6 | 0.924× (0.890–1.066) | 0.434× (0.429–0.445) |
| cp313 | dependencies c64 | 11.12 / 79.8 | 11.72 / 86.5 | 57.43 / 55.3 | 0.999× (0.949–1.073) | 0.206× (0.190–0.214) |

CPU includes sampler drain/export time. RSS sums process trees and can count shared pages more than once; unavailable USS is not reported as zero.

## 256-dict response models on Python 3.13

Framework cells give three-round medians of successful req/s / p99 ms. Paired columns give the median and full range of within-round ratios; these need not equal ratios of the displayed medians.

| Python | Workload | Candidate req/s / p99 | Main f0ec5d3 req/s / p99 | FastAPI req/s / p99 | Candidate/Main f0ec5d3 paired | Candidate/FastAPI paired |
|---|---|---|---|---|---|---|
| cp313 | models_large c64 | 36,149 / 3.520 | 37,138 / 3.549 | 5,200 / 22.356 | 0.973× (0.941–1.151) | 6.737× (6.379–6.992) |

## 256-dict response models on Python 3.13 CPU and memory

Framework cells give three-round medians of CPU µs/success / summed RSS MiB. CPU ratios use the same qualified rounds as throughput and give the median and full range of within-round ratios; these need not equal ratios of the displayed medians.

| Python | Workload | Candidate CPU / RSS | Main f0ec5d3 CPU / RSS | FastAPI CPU / RSS | Candidate/Main f0ec5d3 CPU paired | Candidate/FastAPI CPU paired |
|---|---|---|---|---|---|---|
| cp313 | models_large c64 | 26.40 / 89.2 | 25.57 / 89.4 | 177.78 / 55.1 | 1.032× (0.919–1.057) | 0.151× (0.147–0.158) |

CPU includes sampler drain/export time. RSS sums process trees and can count shared pages more than once; unavailable USS is not reported as zero.

## 1000-item typed uploads c16

Framework cells give three-round medians of successful req/s / p99 ms. Paired columns give the median and full range of within-round ratios; these need not equal ratios of the displayed medians.

| Python | Workload | Candidate req/s / p99 | Main f0ec5d3 req/s / p99 | FastAPI req/s / p99 | Candidate/Main f0ec5d3 paired | Candidate/FastAPI paired |
|---|---|---|---|---|---|---|
| cp313 | echo-large c16 | 2,603 / 9.351 | 2,655 / 9.039 | 772 / 44.022 | 0.990× (0.980–1.044) | 3.474× (3.372–4.400) |

## 1000-item typed uploads c16 CPU and memory

Framework cells give three-round medians of CPU µs/success / summed RSS MiB. CPU ratios use the same qualified rounds as throughput and give the median and full range of within-round ratios; these need not equal ratios of the displayed medians.

| Python | Workload | Candidate CPU / RSS | Main f0ec5d3 CPU / RSS | FastAPI CPU / RSS | Candidate/Main f0ec5d3 CPU paired | Candidate/FastAPI CPU paired |
|---|---|---|---|---|---|---|
| cp313 | echo-large c16 | 351.47 / 83.0 | 344.79 / 83.3 | 1,131.17 / 58.0 | 1.008× (0.951–1.019) | 0.300× (0.247–0.311) |

CPU includes sampler drain/export time. RSS sums process trees and can count shared pages more than once; unavailable USS is not reported as zero.

## 1000-item typed uploads c64

Framework cells give three-round medians of successful req/s / p99 ms. Paired columns give the median and full range of within-round ratios; these need not equal ratios of the displayed medians.

| Python | Workload | Candidate req/s / p99 | Main f0ec5d3 req/s / p99 | FastAPI req/s / p99 | Candidate/Main f0ec5d3 paired | Candidate/FastAPI paired |
|---|---|---|---|---|---|---|
| cp313 | echo-large c64 | 2,565 / 34.824 | 2,540 / 34.498 | 723† / 142.652† | 1.010× (0.995–1.021) | withheld (transport_errors) |

## 1000-item typed uploads c64 CPU and memory

Framework cells give three-round medians of CPU µs/success / summed RSS MiB. CPU ratios use the same qualified rounds as throughput and give the median and full range of within-round ratios; these need not equal ratios of the displayed medians.

| Python | Workload | Candidate CPU / RSS | Main f0ec5d3 CPU / RSS | FastAPI CPU / RSS | Candidate/Main f0ec5d3 CPU paired | Candidate/FastAPI CPU paired |
|---|---|---|---|---|---|---|
| cp313 | echo-large c64 | 350.22 / 89.2 | 351.19 / 88.4 | 1,177.76† / 64.6† | 0.998× (0.981–1.006) | withheld (transport_errors) |

CPU includes sampler drain/export time. RSS sums process trees and can count shared pages more than once; unavailable USS is not reported as zero.

## Generic sync paths across all modes

Framework cells give three-round medians of successful req/s / p99 ms. Paired columns give the median and full range of within-round ratios; these need not equal ratios of the displayed medians.

| Python | Workload | Candidate req/s / p99 | Main f0ec5d3 req/s / p99 | FastAPI req/s / p99 | Candidate/Main f0ec5d3 paired | Candidate/FastAPI paired |
|---|---|---|---|---|---|---|
| cp313 | generic-request c64 | 54,613 / 2.855 | 50,928 / 3.243 | 8,654 / 15.901 | 1.165× (0.886–1.215) | 6.570× (6.252–7.149) |
| cp314 | generic-request c64 | 57,923 / 2.753 | 54,448 / 2.993 | 8,479 / 14.746 | 1.021× (0.874–1.064) | 6.462× (5.274–6.983) |
| cp314t-gil1 | generic-request c64 | 51,666 / 2.992 | 45,818 / 3.114 | 6,877 / 22.046 | 1.128× (0.903–1.239) | 7.580× (7.314–8.464) |
| cp314t-gil0 | generic-request c64 | 44,185 / 3.470 | 46,836 / 3.231 | 11,175 / 11.560 | 0.943× (0.874–1.022) | 3.822× (3.813–4.485) |
| cp313 | generic-dependency c64 | 56,582 / 2.919 | 59,617 / 2.876 | 9,240 / 13.134 | 0.949× (0.845–0.952) | 6.124× (6.118–8.822) |
| cp314 | generic-dependency c64 | 60,556 / 2.841 | 58,964 / 2.789 | 9,299 / 13.333 | 1.139× (1.027–1.155) | 7.272× (6.512–9.595) |
| cp314t-gil1 | generic-dependency c64 | 58,155 / 2.752 | 54,533 / 3.095 | 7,988 / 21.518 | 1.066× (0.814–1.224) | 7.281× (5.947–8.922) |
| cp314t-gil0 | generic-dependency c64 | 53,904 / 2.990 | 51,019 / 3.067 | 9,966 / 12.267 | 1.035× (0.779–1.061) | 5.053× (4.564–5.409) |

## Generic sync paths across all modes CPU and memory

Framework cells give three-round medians of CPU µs/success / summed RSS MiB. CPU ratios use the same qualified rounds as throughput and give the median and full range of within-round ratios; these need not equal ratios of the displayed medians.

| Python | Workload | Candidate CPU / RSS | Main f0ec5d3 CPU / RSS | FastAPI CPU / RSS | Candidate/Main f0ec5d3 CPU paired | Candidate/FastAPI CPU paired |
|---|---|---|---|---|---|---|
| cp313 | generic-request c64 | 23.00 / 80.8 | 24.76 / 73.4 | 119.41 / 56.1 | 0.865× (0.830–1.082) | 0.185× (0.172–0.194) |
| cp314 | generic-request c64 | 21.76 / 85.9 | 23.28 / 86.1 | 120.34 / 61.9 | 0.967× (0.934–1.097) | 0.188× (0.175–0.222) |
| cp314t-gil1 | generic-request c64 | 24.12 / 101.3 | 27.65 / 101.4 | 134.34 / 76.4 | 0.872× (0.812–1.063) | 0.179× (0.164–0.183) |
| cp314t-gil0 | generic-request c64 | 32.30 / 101.2 | 32.98 / 101.3 | 132.07 / 76.5 | 0.980× (0.934–1.046) | 0.254× (0.222–0.258) |
| cp313 | generic-dependency c64 | 20.44 / 80.8 | 19.31 / 80.8 | 114.28 / 56.0 | 1.061× (1.058–1.127) | 0.179× (0.140–0.179) |
| cp314 | generic-dependency c64 | 19.11 / 79.1 | 19.86 / 82.6 | 114.46 / 61.9 | 0.907× (0.885–0.962) | 0.154× (0.127–0.167) |
| cp314t-gil1 | generic-dependency c64 | 20.17 / 98.7 | 20.57 / 99.2 | 118.89 / 75.6 | 0.981× (0.847–1.226) | 0.170× (0.142–0.200) |
| cp314t-gil0 | generic-dependency c64 | 24.45 / 98.6 | 25.33 / 99.0 | 131.00 / 75.7 | 0.983× (0.954–1.103) | 0.192× (0.187–0.198) |

CPU includes sampler drain/export time. RSS sums process trees and can count shared pages more than once; unavailable USS is not reported as zero.

## Additional standard/free-threaded HTTP

Framework cells give three-round medians of successful req/s / p99 ms. Paired columns give the median and full range of within-round ratios; these need not equal ratios of the displayed medians.

| Python | Workload | Candidate req/s / p99 | Main f0ec5d3 req/s / p99 | FastAPI req/s / p99 | Candidate/Main f0ec5d3 paired | Candidate/FastAPI paired |
|---|---|---|---|---|---|---|
| cp314 | json c64 | 107,471 / 1.863 | 123,365 / 1.296 | 18,181 / 7.626 | 0.871× (0.836–1.104) | 5.918× (5.760–6.015) |
| cp314t-gil1 | json c64 | 127,057 / 1.263 | 121,847 / 1.534 | 22,075 / 6.665 | 1.065× (0.986–1.109) | 5.756× (5.657–6.945) |
| cp314t-gil0 | json c64 | 118,058 / 1.183 | 115,289 / 1.425 | 17,536 / 8.039 | 1.104× (0.913–1.143) | 6.733× (6.293–7.687) |
| cp314 | echo c64 | 67,538 / 2.428 | 71,939 / 2.311 | 11,559 / 11.347 | 0.947× (0.939–0.960) | 5.686× (5.492–5.843) |
| cp314t-gil1 | echo c64 | 62,254 / 2.502 | 60,101 / 2.540 | 11,812 / 11.706 | 1.036× (0.960–1.150) | 5.271× (5.211–5.468) |
| cp314t-gil0 | echo c64 | 63,558 / 2.508 | 65,296 / 2.418 | 10,946 / 12.453 | 0.943× (0.926–0.973) | 5.891× (5.792–6.844) |
| cp314 | models_large c64 | 34,765 / 3.650 | 31,834 / 3.806 | 5,184 / 22.041 | 1.008× (1.007–1.167) | 6.809× (6.706–7.295) |
| cp314t-gil1 | models_large c64 | 25,478 / 4.651 | 28,245 / 4.127 | 4,285 / 27.809 | 1.060× (0.882–1.169) | 6.345× (5.073–6.990) |
| cp314t-gil0 | models_large c64 | 28,752 / 4.430 | 24,091 / 4.962 | 4,592 / 25.766 | 1.188× (1.042–1.194) | 6.409× (5.788–6.708) |

## Additional standard/free-threaded HTTP CPU and memory

Framework cells give three-round medians of CPU µs/success / summed RSS MiB. CPU ratios use the same qualified rounds as throughput and give the median and full range of within-round ratios; these need not equal ratios of the displayed medians.

| Python | Workload | Candidate CPU / RSS | Main f0ec5d3 CPU / RSS | FastAPI CPU / RSS | Candidate/Main f0ec5d3 CPU paired | Candidate/FastAPI CPU paired |
|---|---|---|---|---|---|---|
| cp314 | json c64 | 8.36 / 92.5 | 7.41 / 92.7 | 48.39 / 60.1 | 1.128× (0.892–1.185) | 0.173× (0.166–0.174) |
| cp314t-gil1 | json c64 | 7.25 / 105.7 | 7.50 / 105.8 | 41.69 / 70.9 | 0.940× (0.896–1.042) | 0.174× (0.145–0.175) |
| cp314t-gil0 | json c64 | 7.82 / 105.8 | 7.81 / 105.8 | 51.89 / 70.8 | 0.925× (0.885–1.085) | 0.151× (0.132–0.159) |
| cp314 | echo c64 | 13.64 / 93.3 | 12.87 / 92.6 | 79.48 / 60.2 | 1.044× (1.034–1.060) | 0.175× (0.172–0.182) |
| cp314t-gil1 | echo c64 | 14.90 / 106.2 | 15.39 / 106.2 | 78.40 / 70.9 | 0.968× (0.895–1.032) | 0.190× (0.185–0.191) |
| cp314t-gil0 | echo c64 | 14.60 / 106.3 | 14.29 / 106.2 | 84.51 / 70.9 | 1.050× (1.022–1.083) | 0.173× (0.151–0.173) |
| cp314 | models_large c64 | 26.83 / 88.9 | 27.30 / 88.7 | 177.68 / 60.1 | 0.986× (0.983–0.992) | 0.150× (0.141–0.151) |
| cp314t-gil1 | models_large c64 | 32.21 / 109.2 | 33.88 / 109.3 | 217.70 / 70.9 | 0.946× (0.891–0.968) | 0.159× (0.147–0.170) |
| cp314t-gil0 | models_large c64 | 32.97 / 109.2 | 34.13 / 109.2 | 205.38 / 70.9 | 0.972× (0.966–1.004) | 0.159× (0.152–0.173) |

CPU includes sampler drain/export time. RSS sums process trees and can count shared pages more than once; unavailable USS is not reported as zero.

## CPU work c16

Framework cells give three-round medians of successful req/s / p99 ms. Paired columns give the median and full range of within-round ratios; these need not equal ratios of the displayed medians.

| Python | Workload | Candidate req/s / p99 | Main f0ec5d3 req/s / p99 | FastAPI req/s / p99 | Candidate/Main f0ec5d3 paired | Candidate/FastAPI paired |
|---|---|---|---|---|---|---|
| cp313 | cpu-inline c16 | 568 / 38.373 | 576 / 36.457 | 542 / 60.667 | 0.987× (0.975–0.996) | 1.050× (1.029–1.096) |
| cp314 | cpu-inline c16 | 736 / 28.410 | 737 / 29.274 | 717 / 44.432 | 1.010× (0.987–1.010) | 1.041× (1.001–1.052) |
| cp314t-gil1 | cpu-inline c16 | 735 / 27.931 | 745 / 27.921 | 699 / 46.996 | 0.997× (0.966–1.033) | 1.055× (1.028–1.072) |
| cp314t-gil0 | cpu-inline c16 | 739 / 28.565 | 730 / 28.519 | 686 / 46.670 | 1.013× (0.999–1.014) | 1.075× (1.071–1.100) |
| cp313 | cpu-sync c16 | 588 / 45.658 | 605 / 42.758 | 556 / 37.389 | 0.965× (0.963–0.975) | 1.057× (1.050–1.098) |
| cp314 | cpu-sync c16 | 755 / 35.503 | 755 / 36.080 | 699 / 32.401 | 0.976× (0.958–1.013) | 1.085× (1.023–1.116) |
| cp314t-gil1 | cpu-sync c16 | 756 / 35.171 | 749 / 34.932 | 679 / 33.390 | 1.015× (0.953–1.087) | 1.114× (1.075–1.160) |
| cp314t-gil0 | cpu-sync c16 | 2,406 / 9.731 | 2,136 / 12.354 | 2,043 / 11.412 | 1.127× (0.886–1.233) | 1.100× (0.977–1.296) |
| cp313 | cpu-offload-auto c16 | 2,039 / 10.268 | 2,009 / 10.207 | 2,028 / 10.389 | 0.995× (0.745–1.015) | 0.995× (0.653–1.006) |
| cp314 | cpu-offload-auto c16 | 2,483 / 8.319 | 2,512 / 8.320 | 2,541 / 8.369 | 1.007× (0.964–1.022) | 1.004× (0.953–1.035) |
| cp314t-gil1 | cpu-offload-auto c16 | 2,553 / 9.846 | 2,494 / 10.825 | 2,586 / 9.157 | 1.024× (1.009–1.052) | 0.987× (0.967–1.002) |
| cp314t-gil0 | cpu-offload-auto c16 | 2,556 / 8.207 | 2,564 / 8.315 | 2,518 / 8.584 | 1.011× (0.975–1.021) | 1.015× (1.010–1.024) |

## CPU work c16 CPU and memory

Framework cells give three-round medians of CPU µs/success / summed RSS MiB. CPU ratios use the same qualified rounds as throughput and give the median and full range of within-round ratios; these need not equal ratios of the displayed medians.

| Python | Workload | Candidate CPU / RSS | Main f0ec5d3 CPU / RSS | FastAPI CPU / RSS | Candidate/Main f0ec5d3 CPU paired | Candidate/FastAPI CPU paired |
|---|---|---|---|---|---|---|
| cp313 | cpu-inline c16 | 1,693.34 / 79.2 | 1,685.18 / 79.1 | 1,755.04 / 71.7 | 1.008× (1.001–1.018) | 0.955× (0.936–0.978) |
| cp314 | cpu-inline c16 | 1,324.73 / 77.5 | 1,318.30 / 84.7 | 1,333.81 / 80.9 | 0.991× (0.985–1.012) | 0.974× (0.961–1.005) |
| cp314t-gil1 | cpu-inline c16 | 1,325.79 / 97.5 | 1,303.85 / 97.2 | 1,367.59 / 95.0 | 1.003× (0.974–1.017) | 0.960× (0.949–0.969) |
| cp314t-gil0 | cpu-inline c16 | 1,315.76 / 97.0 | 1,323.27 / 97.1 | 1,395.74 / 95.0 | 0.997× (0.986–0.999) | 0.942× (0.925–0.951) |
| cp313 | cpu-sync c16 | 1,687.25 / 79.6 | 1,638.59 / 79.5 | 1,765.97 / 72.2 | 1.033× (1.026–1.037) | 0.955× (0.920–0.959) |
| cp314 | cpu-sync c16 | 1,315.60 / 85.6 | 1,311.09 / 85.1 | 1,393.95 / 81.1 | 1.018× (0.993–1.027) | 0.938× (0.915–0.973) |
| cp314t-gil1 | cpu-sync c16 | 1,306.42 / 98.8 | 1,327.28 / 98.9 | 1,430.35 / 98.3 | 0.984× (0.916–1.034) | 0.913× (0.890–0.942) |
| cp314t-gil0 | cpu-sync c16 | 1,655.22 / 98.4 | 1,870.62 / 98.5 | 1,910.20 / 98.4 | 0.885× (0.819–1.136) | 0.925× (0.797–1.047) |
| cp313 | cpu-offload-auto c16 | 2,150.03 / 222.0 | 2,184.52 / 229.9 | 2,223.40 / 150.6 | 1.009× (0.984–1.021) | 0.979× (0.967–1.088) |
| cp314 | cpu-offload-auto c16 | 1,788.31 / 249.5 | 1,788.55 / 249.1 | 1,819.95 / 182.9 | 0.992× (0.981–1.000) | 0.967× (0.940–0.983) |
| cp314t-gil1 | cpu-offload-auto c16 | 1,792.76 / 281.9 | 1,822.49 / 281.7 | 1,815.10 / 210.9 | 0.984× (0.974–0.987) | 0.988× (0.974–0.995) |
| cp314t-gil0 | cpu-offload-auto c16 | 1,760.00 / 281.5 | 1,748.07 / 281.8 | 1,852.85 / 210.8 | 0.988× (0.976–1.021) | 0.950× (0.944–0.954) |

CPU includes sampler drain/export time. RSS sums process trees and can count shared pages more than once; unavailable USS is not reported as zero.

## CPU sync c64

Framework cells give three-round medians of successful req/s / p99 ms. Paired columns give the median and full range of within-round ratios; these need not equal ratios of the displayed medians.

| Python | Workload | Candidate req/s / p99 | Main f0ec5d3 req/s / p99 | FastAPI req/s / p99 | Candidate/Main f0ec5d3 paired | Candidate/FastAPI paired |
|---|---|---|---|---|---|---|
| cp313 | cpu-sync c64 | 560 / 179.490 | 578 / 171.276 | 542 / 142.429 | 0.965× (0.954–1.094) | 1.033× (1.023–1.262) |
| cp314 | cpu-sync c64 | 739 / 152.853 | 726 / 147.378 | 665 / 121.241 | 1.005× (0.928–1.017) | 1.104× (1.051–1.111) |

## CPU sync c64 CPU and memory

Framework cells give three-round medians of CPU µs/success / summed RSS MiB. CPU ratios use the same qualified rounds as throughput and give the median and full range of within-round ratios; these need not equal ratios of the displayed medians.

| Python | Workload | Candidate CPU / RSS | Main f0ec5d3 CPU / RSS | FastAPI CPU / RSS | Candidate/Main f0ec5d3 CPU paired | Candidate/FastAPI CPU paired |
|---|---|---|---|---|---|---|
| cp313 | cpu-sync c64 | 1,739.63 / 74.1 | 1,703.63 / 80.9 | 1,796.20 / 72.9 | 1.027× (0.946–1.036) | 0.969× (0.835–0.977) |
| cp314 | cpu-sync c64 | 1,329.11 / 86.1 | 1,348.91 / 86.2 | 1,462.14 / 82.5 | 0.994× (0.985–1.052) | 0.913× (0.909–0.938) |

CPU includes sampler drain/export time. RSS sums process trees and can count shared pages more than once; unavailable USS is not reported as zero.

## Free-threaded / standard interpreter ratios

Each ratio pairs the same role and workload within each natural round; modes execute sequentially on the shared host. Ratios give the three-round median and range. CPU ratios are CPU time per successful reply.

| Role | Case | Modes, numerator/denominator | Paired RPS | Paired CPU/success |
|---|---|---|---|---|
| candidate | cpu-inline | cp314t-gil1/cp314 | 0.999× (0.953–1.066) | 1.002× (0.946–1.034) |
| candidate | cpu-inline | cp314t-gil0/cp314 | 1.007× (0.977–1.017) | 0.993× (0.988–1.026) |
| candidate | cpu-inline | cp314t-gil0/cp314t-gil1 | 1.007× (0.954–1.025) | 0.992× (0.991–1.044) |
| candidate | cpu-sync | cp314t-gil1/cp314 | 1.006× (0.976–1.045) | 0.993× (0.970–1.012) |
| candidate | cpu-sync | cp314t-gil0/cp314 | 3.170× (2.644–3.338) | 1.266× (1.228–1.520) |
| candidate | cpu-sync | cp314t-gil0/cp314t-gil1 | 3.194× (2.627–3.247) | 1.266× (1.251–1.530) |
| candidate | cpu-offload-auto | cp314t-gil1/cp314 | 0.997× (0.989–1.054) | 1.021× (1.002–1.021) |
| candidate | cpu-offload-auto | cp314t-gil0/cp314 | 1.029× (0.984–1.032) | 0.997× (0.978–1.018) |
| candidate | cpu-offload-auto | cp314t-gil0/cp314t-gil1 | 0.987× (0.979–1.041) | 0.995× (0.958–0.997) |
| main | cpu-inline | cp314t-gil1/cp314 | 0.996× (0.977–1.055) | 1.002× (0.955–1.019) |
| main | cpu-inline | cp314t-gil0/cp314 | 0.991× (0.987–1.002) | 1.004× (0.998–1.012) |
| main | cpu-inline | cp314t-gil0/cp314t-gil1 | 0.991× (0.939–1.026) | 1.010× (0.980–1.051) |
| main | cpu-sync | cp314t-gil1/cp314 | 1.000× (0.921–1.005) | 1.002× (0.997–1.087) |
| main | cpu-sync | cp314t-gil0/cp314 | 2.747× (2.594–3.026) | 1.456× (1.329–1.540) |
| main | cpu-sync | cp314t-gil0/cp314t-gil1 | 2.817× (2.748–3.011) | 1.417× (1.326–1.461) |
| main | cpu-offload-auto | cp314t-gil1/cp314 | 0.993× (0.961–0.995) | 1.026× (1.019–1.028) |
| main | cpu-offload-auto | cp314t-gil0/cp314 | 1.021× (0.980–1.030) | 0.983× (0.977–1.022) |
| main | cpu-offload-auto | cp314t-gil0/cp314t-gil1 | 1.028× (0.986–1.072) | 0.959× (0.957–0.996) |
| fastapi | cpu-inline | cp314t-gil1/cp314 | 0.975× (0.970–1.011) | 1.025× (0.990–1.028) |
| fastapi | cpu-inline | cp314t-gil0/cp314 | 0.953× (0.950–0.956) | 1.046× (1.044–1.046) |
| fastapi | cpu-inline | cp314t-gil0/cp314t-gil1 | 0.980× (0.940–0.982) | 1.021× (1.018–1.054) |
| fastapi | cpu-sync | cp314t-gil1/cp314 | 0.968× (0.960–0.986) | 1.021× (1.008–1.033) |
| fastapi | cpu-sync | cp314t-gil0/cp314 | 3.020× (2.635–3.127) | 1.329× (1.284–1.499) |
| fastapi | cpu-sync | cp314t-gil0/cp314t-gil1 | 3.119× (2.746–3.172) | 1.302× (1.274–1.451) |
| fastapi | cpu-offload-auto | cp314t-gil1/cp314 | 1.018× (0.999–1.058) | 0.997× (0.965–1.015) |
| fastapi | cpu-offload-auto | cp314t-gil0/cp314 | 0.974× (0.965–1.050) | 1.028× (0.969–1.043) |
| fastapi | cpu-offload-auto | cp314t-gil0/cp314t-gil1 | 0.966× (0.957–0.992) | 1.028× (1.004–1.030) |
| candidate | generic-request | cp314t-gil1/cp314 | 0.892× (0.867–1.101) | 1.108× (0.940–1.140) |
| candidate | generic-request | cp314t-gil0/cp314 | 0.834× (0.763–0.886) | 1.385× (1.297–1.484) |
| candidate | generic-request | cp314t-gil0/cp314t-gil1 | 0.855× (0.804–0.962) | 1.339× (1.215–1.380) |
| candidate | generic-dependency | cp314t-gil1/cp314 | 0.960× (0.766–0.997) | 1.056× (1.009–1.282) |
| candidate | generic-dependency | cp314t-gil0/cp314 | 0.751× (0.618–0.894) | 1.463× (1.265–1.521) |
| candidate | generic-dependency | cp314t-gil0/cp314t-gil1 | 0.806× (0.753–0.931) | 1.198× (1.187–1.450) |
| candidate | json | cp314t-gil1/cp314 | 1.089× (1.063–1.182) | 0.922× (0.868–0.941) |
| candidate | json | cp314t-gil0/cp314 | 1.099× (1.005–1.114) | 0.936× (0.905–0.989) |
| candidate | json | cp314t-gil0/cp314t-gil1 | 0.929× (0.923–1.048) | 1.073× (0.962–1.078) |
| candidate | echo | cp314t-gil1/cp314 | 0.922× (0.906–0.983) | 1.092× (1.043–1.110) |
| candidate | echo | cp314t-gil0/cp314 | 0.941× (0.889–0.970) | 1.071× (1.061–1.131) |
| candidate | echo | cp314t-gil0/cp314t-gil1 | 1.021× (0.904–1.071) | 0.980× (0.956–1.084) |
| candidate | models_large | cp314t-gil1/cp314 | 0.780× (0.668–0.861) | 1.274× (1.195–1.284) |
| candidate | models_large | cp314t-gil0/cp314 | 0.822× (0.754–0.846) | 1.226× (1.216–1.314) |
| candidate | models_large | cp314t-gil0/cp314t-gil1 | 1.053× (0.983–1.129) | 1.018× (0.962–1.024) |
| main | generic-request | cp314t-gil1/cp314 | 0.841× (0.777–0.980) | 1.188× (1.037–1.271) |
| main | generic-request | cp314t-gil0/cp314 | 0.860× (0.833–0.885) | 1.416× (1.361–1.434) |
| main | generic-request | cp314t-gil0/cp314t-gil1 | 1.022× (0.850–1.139) | 1.192× (1.071–1.382) |
| main | generic-dependency | cp314t-gil1/cp314 | 0.941× (0.925–1.072) | 1.036× (0.948–1.054) |
| main | generic-dependency | cp314t-gil0/cp314 | 0.865× (0.838–0.903) | 1.275× (1.250–1.318) |
| main | generic-dependency | cp314t-gil0/cp314t-gil1 | 0.891× (0.842–0.936) | 1.250× (1.231–1.319) |
| main | json | cp314t-gil1/cp314 | 1.044× (0.834–1.084) | 0.940× (0.919–1.187) |
| main | json | cp314t-gil0/cp314 | 0.867× (0.815–1.215) | 1.142× (0.813–1.212) |
| main | json | cp314t-gil0/cp314t-gil1 | 0.977× (0.830–1.121) | 1.021× (0.886–1.215) |
| main | echo | cp314t-gil1/cp314 | 0.835× (0.821–0.895) | 1.196× (1.122–1.206) |
| main | echo | cp314t-gil0/cp314 | 0.908× (0.904–0.993) | 1.110× (1.023–1.115) |
| main | echo | cp314t-gil0/cp314t-gil1 | 1.102× (1.086–1.110) | 0.925× (0.912–0.928) |
| main | models_large | cp314t-gil1/cp314 | 0.763× (0.672–0.948) | 1.316× (1.241–1.411) |
| main | models_large | cp314t-gil0/cp314 | 0.696× (0.637–0.948) | 1.230× (1.204–1.350) |
| main | models_large | cp314t-gil0/cp314t-gil1 | 1.000× (0.834–1.037) | 0.991× (0.853–1.026) |
| fastapi | generic-request | cp314t-gil1/cp314 | 0.739× (0.686–0.852) | 1.195× (1.059–1.274) |
| fastapi | generic-request | cp314t-gil0/cp314 | 1.225× (1.202–1.394) | 1.116× (1.023–1.175) |
| fastapi | generic-request | cp314t-gil0/cp314t-gil1 | 1.636× (1.625–1.785) | 0.966× (0.876–0.983) |
| fastapi | generic-dependency | cp314t-gil1/cp314 | 0.859× (0.813–1.236) | 1.039× (0.811–1.093) |
| fastapi | generic-dependency | cp314t-gil0/cp314 | 1.152× (1.010–1.298) | 1.069× (1.003–1.207) |
| fastapi | generic-dependency | cp314t-gil0/cp314t-gil1 | 1.242× (1.050–1.341) | 1.104× (1.029–1.237) |
| fastapi | json | cp314t-gil1/cp314 | 1.130× (0.903–1.216) | 0.892× (0.861–1.107) |
| fastapi | json | cp314t-gil0/cp314 | 0.966× (0.753–1.065) | 1.072× (0.944–1.307) |
| fastapi | json | cp314t-gil0/cp314t-gil1 | 0.834× (0.794–0.943) | 1.181× (1.058–1.245) |
| fastapi | echo | cp314t-gil1/cp314 | 1.022× (0.955–1.022) | 0.990× (0.986–1.060) |
| fastapi | echo | cp314t-gil0/cp314 | 0.858× (0.779–0.949) | 1.143× (1.063–1.281) |
| fastapi | echo | cp314t-gil0/cp314t-gil1 | 0.839× (0.815–0.929) | 1.155× (1.078–1.209) |
| fastapi | models_large | cp314t-gil1/cp314 | 0.897× (0.827–0.897) | 1.130× (1.129–1.225) |
| fastapi | models_large | cp314t-gil0/cp314 | 0.887× (0.886–0.893) | 1.137× (1.137–1.156) |
| fastapi | models_large | cp314t-gil0/cp314t-gil1 | 0.996× (0.989–1.072) | 1.006× (0.943–1.008) |

## Single-client functional controls

These are eight one-round observations on candidate/main, with no FastAPI population and no paired performance claims.

| Mode | Control | Candidate req/s | Main req/s |
|---|---|---|---|
| cp313 | cpu-inline | 490 | 553 |
| cp313 | cpu-sync | 547 | 546 |
| cp314 | cpu-inline | 688 | 664 |
| cp314 | cpu-sync | 669 | 650 |

## Shared host context

Ranges are observed during natural load. They do not establish CPU isolation; first unprimed psutil readings are excluded. Each case cell gives host CPU-percent range / one-minute load-average range. Full 1/5/15-minute ranges remain in the public JSON summary.

| Context | Observed range |
|---|---|
| Host CPU % | 0.0–100.0 |
| Load average 1 min | 9.70–18.87 |
| Load average 5 min | 10.90–15.46 |
| Load average 15 min | 12.37–14.37 |

| Mode | Case | Candidate CPU% / load1m | Main CPU% / load1m | FastAPI CPU% / load1m |
|---|---|---|---|---|
| cp313 | cpu-inline c16 | 0.0–72.4 / 10.23–12.67 | 0.0–74.5 / 10.69–13.62 | 0.0–68.0 / 10.71–13.43 |
| cp314 | cpu-inline c16 | 0.0–67.7 / 10.43–13.62 | 0.0–66.3 / 10.74–13.17 | 0.0–59.3 / 10.43–13.25 |
| cp314t-gil1 | cpu-inline c16 | 0.0–71.2 / 10.14–13.95 | 0.0–69.8 / 10.21–13.58 | 0.0–63.0 / 10.33–13.45 |
| cp314t-gil0 | cpu-inline c16 | 0.0–71.6 / 10.35–14.13 | 0.0–65.6 / 10.20–13.56 | 0.0–79.6 / 9.70–14.13 |
| cp313 | cpu-sync c16 | 0.0–62.6 / 10.29–14.49 | 33.7–67.3 / 10.18–13.89 | 0.0–63.1 / 10.65–14.49 |
| cp314 | cpu-sync c16 | 0.0–69.6 / 10.08–13.77 | 0.0–72.1 / 10.44–13.31 | 0.0–68.2 / 10.08–13.68 |
| cp314t-gil1 | cpu-sync c16 | 0.0–65.0 / 9.73–12.83 | 0.0–63.5 / 9.99–12.83 | 0.0–68.3 / 9.73–12.81 |
| cp314t-gil0 | cpu-sync c16 | 0.0–84.2 / 9.99–14.12 | 0.0–85.4 / 10.35–14.18 | 0.0–80.6 / 10.80–14.12 |
| cp313 | cpu-offload-auto c16 | 59.4–93.1 / 10.58–17.08 | 0.0–98.2 / 10.58–17.95 | 0.0–86.7 / 10.31–15.42 |
| cp314 | cpu-offload-auto c16 | 0.0–83.9 / 10.95–17.56 | 0.0–93.4 / 10.69–17.42 | 0.0–90.4 / 11.14–17.38 |
| cp314t-gil1 | cpu-offload-auto c16 | 0.0–88.7 / 10.97–15.42 | 0.0–93.7 / 11.05–14.59 | 0.0–86.2 / 10.70–14.78 |
| cp314t-gil0 | cpu-offload-auto c16 | 0.0–88.9 / 11.19–14.96 | 0.0–91.6 / 11.41–15.38 | 0.0–92.9 / 11.78–15.42 |
| cp313 | cpu-sync c64 | 0.0–72.1 / 11.81–18.32 | 0.0–72.7 / 11.81–17.98 | 0.0–79.9 / 12.05–18.87 |
| cp314 | cpu-sync c64 | 0.0–86.1 / 11.50–17.66 | 0.0–68.1 / 11.89–17.44 | 0.0–69.4 / 11.81–18.18 |
| cp313 | json c64 | 0.0–81.2 / 11.50–17.61 | 0.0–81.5 / 11.71–17.88 | 0.0–70.9 / 11.71–17.76 |
| cp313 | echo c64 | 0.0–82.1 / 12.21–16.99 | 0.0–80.0 / 12.63–16.62 | 0.0–88.9 / 12.29–17.25 |
| cp313 | io c64 | 0.0–84.3 / 12.29–15.97 | 0.0–64.6 / 12.50–15.84 | 0.0–70.2 / 12.65–16.49 |
| cp313 | fanout c64 | 0.0–69.2 / 12.67–16.13 | 0.0–85.4 / 12.86–16.31 | 0.0–62.4 / 12.76–16.13 |
| cp313 | sync c64 | 0.0–72.2 / 15.02–16.30 | 0.0–96.4 / 15.22–17.09 | 0.0–74.1 / 14.64–16.76 |
| cp313 | sync-stream c64 | 0.0–72.9 / 14.64–16.99 | 0.0–85.3 / 14.67–17.34 | 0.0–70.9 / 14.18–17.34 |
| cp313 | stream c64 | 0.0–71.4 / 14.01–17.27 | 0.0–76.5 / 13.85–17.08 | 0.0–71.9 / 13.85–17.81 |
| cp313 | bytes c64 | 0.0–78.6 / 13.70–17.52 | 0.0–78.8 / 13.65–16.12 | 0.0–84.7 / 14.00–17.52 |
| cp313 | dependencies c64 | 36.1–68.5 / 14.00–16.48 | 0.0–69.3 / 13.82–16.28 | 0.0–69.7 / 13.82–16.28 |
| cp313 | models_large c64 | 0.0–74.1 / 12.29–15.37 | 0.0–75.5 / 12.17–15.34 | 0.0–73.6 / 12.17–15.14 |
| cp313 | echo-large c16 | 0.0–79.4 / 13.45–14.43 | 0.0–75.9 / 12.42–13.74 | 0.0–86.2 / 12.26–15.15 |
| cp313 | echo-large c64 | 0.0–74.6 / 12.42–14.02 | 0.0–68.4 / 12.18–13.94 | 0.0–75.5 / 12.03–14.19 |
| cp313 | generic-request c64 | 0.0–74.4 / 12.80–14.71 | 0.0–79.6 / 12.98–14.81 | 0.0–80.0 / 12.67–15.24 |
| cp314 | generic-request c64 | 0.0–75.9 / 12.56–14.42 | 0.0–69.0 / 12.67–14.22 | 0.0–77.9 / 12.56–14.42 |
| cp314t-gil1 | generic-request c64 | 48.4–76.4 / 12.49–15.00 | 0.0–79.9 / 12.53–15.57 | 0.0–67.9 / 12.70–15.57 |
| cp314t-gil0 | generic-request c64 | 0.0–73.9 / 12.81–15.05 | 0.0–71.5 / 12.42–15.37 | 0.0–73.5 / 12.42–15.05 |
| cp313 | generic-dependency c64 | 0.0–92.4 / 12.39–15.63 | 0.0–82.1 / 11.88–16.14 | 0.0–100.0 / 11.73–14.38 |
| cp314 | generic-dependency c64 | 0.0–74.1 / 12.84–16.21 | 0.0–76.3 / 12.07–15.95 | 0.0–89.7 / 12.48–15.95 |
| cp314t-gil1 | generic-dependency c64 | 0.0–83.7 / 12.96–15.40 | 0.0–95.9 / 13.04–14.64 | 0.0–67.8 / 12.69–14.48 |
| cp314t-gil0 | generic-dependency c64 | 0.0–100.0 / 12.94–14.00 | 0.0–79.8 / 12.76–14.48 | 0.0–78.6 / 13.18–13.48 |
| cp314 | json c64 | 0.0–86.7 / 13.42–14.75 | 0.0–83.7 / 12.82–15.13 | 0.0–81.7 / 13.06–14.96 |
| cp314t-gil1 | json c64 | 0.0–77.4 / 12.43–14.92 | 0.0–76.9 / 12.43–15.47 | 0.0–66.0 / 12.42–14.79 |
| cp314t-gil0 | json c64 | 0.0–80.3 / 13.06–15.06 | 0.0–91.5 / 12.42–14.99 | 0.0–97.2 / 13.38–14.84 |
| cp314 | echo c64 | 0.0–76.3 / 13.11–14.65 | 0.0–71.1 / 12.69–14.28 | 0.0–67.4 / 12.69–14.26 |
| cp314t-gil1 | echo c64 | 0.0–73.7 / 12.45–15.15 | 0.0–71.2 / 12.88–15.15 | 0.0–62.5 / 12.64–14.62 |
| cp314t-gil0 | echo c64 | 0.0–68.6 / 12.77–14.29 | 0.0–69.9 / 12.65–14.62 | 0.0–82.6 / 12.65–14.44 |
| cp314 | models_large c64 | 0.0–90.6 / 12.61–16.19 | 0.0–69.0 / 12.83–15.86 | 0.0–71.0 / 12.83–15.79 |
| cp314t-gil1 | models_large c64 | 0.0–71.2 / 12.39–14.84 | 0.0–95.7 / 12.04–15.09 | 0.0–64.1 / 12.42–15.09 |
| cp314t-gil0 | models_large c64 | 40.3–74.4 / 12.03–15.60 | 0.0–66.7 / 11.33–15.23 | 0.0–68.1 / 11.14–16.00 |

## Failures and qualification

| Variant | Measured/attempted | Measured HTTP statuses | Transport errors | Wrong lengths | Warmup non-200 | Warmup transport | Measured error reasons |
|---|---|---|---|---|---|---|---|
| Candidate | 133/133 | {"200": 18393023} | 0 | 0 | 0 | 0 | {} |
| Main f0ec5d3 | 133/133 | {"200": 18286156} | 0 | 0 | 0 | 0 | {} |
| FastAPI 0.162.2 | 129/129 | {"200": 3638429} | 16 | 0 | 0 | 0 | {"timeout": 16} |

Lifecycle issues: 0. Any failed or missing round withholds the entire paired population, including its CPU comparison. † marks an unqualified diagnostic median; it is not used for a speedup claim.

| Phase | Python | Variant | Workload | Round | Measured failures | Warmup failures | Warmup issues |
|---|---|---|---|---|---|---|---|
| http-large-c64 | cp313 | fastapi | echo-large | 2 | 9 | 0 | — |
| http-large-c64 | cp313 | fastapi | echo-large | 3 | 7 | 0 | — |

## Separate scheduling controls

Twenty-four owned worker SIGSTOP/SIGCONT controls use 16 clients and four sync slots on standard 3.13/3.14. Every status/body, runtime, final SIGCONT and cleanup is checked. These artificial pauses provide a scheduling diagnostic; their rates/counts are excluded from the natural-load capacity comparisons. Zero baseline refusals alone would not reproduce or prove the accounting correction.

| Role:mode | Control samples | HTTP statuses | Transport errors |
|---|---|---|---|
| candidate:cp313 | 6 | {"200": 2539} | {} |
| main:cp313 | 6 | {"200": 2487} | {} |
| main:cp314 | 6 | {"200": 2974} | {} |
| candidate:cp314 | 6 | {"200": 3170} | {} |

## Targeted regression evidence

The historical paused-time Rust regression failed on request 2 under e26b3c78. Its adaptive source Git blob exactly equals the historical a4d5b79 reference (73f81991aaaf0c2fc3d92ce9a8fdf0791c9b895e), and its native artifact matches the verified main cp313 baseline. The source+fragment, execution receipt and failing log are hash-bound. This is historical a4 reference evidence, not a fresh failure on current f0ec main, a new run or proof of the old Intel CI cause.

Fresh candidate Rust regressions are part of the full installed gate and frozen source/log receipts. Their correctness evidence is separate from the 395 natural measurements and paused scheduling counts.

## Separate four-workload investigation

The initial 48 compare reference a4d5b79 with main f0ec5d3 on the four specified cases, in six balanced rounds. The separately frozen final 72 add the final candidate. CPU-sync uses standard 3.13 and four sync slots; Request uses free-threaded 3.14 with GIL on and four sync slots; typed20 echo and 256-dict models use standard 3.14. Every case uses 64 clients, 1 s warmup and 4 s load. Each role occupies each position 3 times initially or 2 times in the final study. These studies are never pooled with 395 natural samples or 24 paused controls.

All displayed rates, p99, CPU and RSS are group medians. Paired columns use within-round ratios across all six rounds and show the median/full range; ratios need not equal a ratio of displayed medians. Failed populations remain withheld.

The first48 overlapped unexpected rustup synchronization/download of six components. Full command bounds are unknown: observed component-install file timestamps span 08:34:39.776–08:35:01.067 UTC, formatter writes followed at 08:35:02.3 UTC, and the command completed no later than 08:35:07 UTC on 18 November. The whole first48 is retained as an interfered diagnostic and excluded from primary baseline conclusions. The separately frozen quiet48 replay supplies initial baseline evidence; exact affected sample rows are not assigned.

### First 48 interfered diagnostic, excluded from primary baseline

Completed 48 attempts; 48 passed the recorded HTTP/runtime/cleanup gates; reported host interference excludes this study from primary baseline evidence. All failures are retained.

| Case:role | Median req/s | Median p99 ms | Median CPU µs/success | Median RSS MiB |
|---|---|---|---|---|
| cpu-sync-313-c64:reference-a4 | 514 | 189.146 | 1,850.397 | 70.5 |
| cpu-sync-313-c64:main | 533 | 181.000 | 1,813.995 | 77.6 |
| generic-request-ft-gil1-c64:main | 38,475 | 3.694 | 31.769 | 101.3 |
| generic-request-ft-gil1-c64:reference-a4 | 38,371 | 3.686 | 31.779 | 101.0 |
| typed20-314-c64:reference-a4 | 46,376 | 3.677 | 18.485 | 78.9 |
| typed20-314-c64:main | 54,260 | 2.968 | 16.687 | 93.2 |
| models256-314-c64:main | 30,787 | 4.128 | 30.038 | 96.0 |
| models256-314-c64:reference-a4 | 29,306 | 4.303 | 31.291 | 96.7 |

| Paired numerator/denominator | Paired RPS (six-round range) | Paired CPU/success (six-round range) |
|---|---|---|
| cpu-sync-313-c64:main/reference-a4 | 1.008× (0.914–1.085) | 0.994× (0.939–1.112) |
| generic-request-ft-gil1-c64:main/reference-a4 | 1.038× (0.843–1.225) | 0.998× (0.836–1.157) |
| typed20-314-c64:main/reference-a4 | 1.066× (1.014–1.177) | 0.956× (0.881–1.030) |
| models256-314-c64:main/reference-a4 | 1.103× (0.761–1.153) | 0.940× (0.920–1.060) |

### Quiet initial48 reference/main

Completed 48 attempts; 48 qualified. All failures are retained.

| Case:role | Median req/s | Median p99 ms | Median CPU µs/success | Median RSS MiB |
|---|---|---|---|---|
| cpu-sync-313-c64:reference-a4 | 458 | 211.230 | 2,012.355 | 62.1 |
| cpu-sync-313-c64:main | 439 | 234.117 | 2,011.867 | 65.4 |
| generic-request-ft-gil1-c64:main | 30,797 | 4.971 | 35.631 | 85.9 |
| generic-request-ft-gil1-c64:reference-a4 | 32,025 | 4.726 | 35.326 | 87.0 |
| typed20-314-c64:reference-a4 | 47,732 | 3.835 | 18.092 | 80.8 |
| typed20-314-c64:main | 50,405 | 3.328 | 17.669 | 89.1 |
| models256-314-c64:main | 26,752 | 4.807 | 33.299 | 80.4 |
| models256-314-c64:reference-a4 | 28,783 | 4.288 | 31.991 | 93.0 |

| Paired numerator/denominator | Paired RPS (six-round range) | Paired CPU/success (six-round range) |
|---|---|---|
| cpu-sync-313-c64:main/reference-a4 | 0.953× (0.780–1.081) | 1.000× (0.960–1.110) |
| generic-request-ft-gil1-c64:main/reference-a4 | 0.907× (0.856–1.208) | 1.031× (0.868–1.043) |
| typed20-314-c64:main/reference-a4 | 1.079× (0.830–1.264) | 0.976× (0.864–1.168) |
| models256-314-c64:main/reference-a4 | 0.949× (0.904–0.990) | 1.033× (1.010–1.097) |

### Final 72 candidate/main/reference

Completed 72 attempts; 72 qualified. All failures are retained.

| Case:role | Median req/s | Median p99 ms | Median CPU µs/success | Median RSS MiB |
|---|---|---|---|---|
| cpu-sync-313-c64:reference-a4 | 600 | 172.462 | 1,644.520 | 80.4 |
| cpu-sync-313-c64:main | 598 | 170.910 | 1,654.731 | 80.4 |
| cpu-sync-313-c64:candidate | 605 | 174.134 | 1,630.483 | 73.8 |
| generic-request-ft-gil1-c64:main | 50,656 | 3.005 | 25.025 | 101.3 |
| generic-request-ft-gil1-c64:candidate | 54,994 | 2.813 | 23.046 | 101.2 |
| generic-request-ft-gil1-c64:reference-a4 | 51,722 | 2.956 | 24.780 | 101.3 |
| typed20-314-c64:candidate | 72,506 | 2.332 | 12.711 | 89.2 |
| typed20-314-c64:reference-a4 | 73,617 | 2.392 | 12.464 | 93.0 |
| typed20-314-c64:main | 70,306 | 2.377 | 13.050 | 89.3 |
| models256-314-c64:reference-a4 | 36,112 | 3.565 | 26.182 | 96.0 |
| models256-314-c64:main | 34,539 | 3.626 | 27.363 | 92.0 |
| models256-314-c64:candidate | 36,411 | 3.506 | 25.731 | 96.1 |

| Paired numerator/denominator | Paired RPS (six-round range) | Paired CPU/success (six-round range) |
|---|---|---|
| cpu-sync-313-c64:main/reference-a4 | 0.974× (0.954–1.034) | 1.024× (0.977–1.036) |
| cpu-sync-313-c64:candidate/main | 1.026× (0.984–1.042) | 0.978× (0.972–1.015) |
| cpu-sync-313-c64:candidate/reference-a4 | 1.002× (0.995–1.023) | 0.997× (0.982–1.016) |
| generic-request-ft-gil1-c64:main/reference-a4 | 0.993× (0.898–1.028) | 1.019× (0.971–1.097) |
| generic-request-ft-gil1-c64:candidate/main | 1.065× (1.006–1.147) | 0.932× (0.873–0.981) |
| generic-request-ft-gil1-c64:candidate/reference-a4 | 1.060× (1.008–1.116) | 0.936× (0.900–0.992) |
| typed20-314-c64:main/reference-a4 | 0.989× (0.844–1.068) | 1.015× (0.932–1.165) |
| typed20-314-c64:candidate/main | 1.027× (0.945–1.064) | 0.973× (0.960–1.057) |
| typed20-314-c64:candidate/reference-a4 | 0.979× (0.870–1.137) | 1.025× (0.895–1.133) |
| models256-314-c64:main/reference-a4 | 0.969× (0.893–1.018) | 1.027× (0.983–1.105) |
| models256-314-c64:candidate/main | 1.031× (0.933–1.068) | 0.968× (0.924–0.978) |
| models256-314-c64:candidate/reference-a4 | 0.992× (0.915–1.048) | 0.987× (0.928–1.081) |

### Separate native diagnostics

Eight native stack captures use the frozen initial identities and actual four-case slot/runtime configuration. Each uses 1 s warmup, 5.5 s load and 5 s sampling. They have no throughput/CPU-cost claims and are excluded from timing comparisons. Stack sample counts include waiting threads; an absent sampled symbol does not prove zero calls.

### Recorded mechanisms and retained validation attempt

These instrumented diagnostics record call entries and selected native path hits, with no throughput, CPU saving or allocation-count claims. Each generic loop/pool pass completed 500 semantic requests; only the pool pass records handler-side conversions. Coroutine resumes can exceed logical request counts.

| Generic pool census | Semantic requests | Full-header conversion entries | Native headers entries | Native host entries |
|---|---|---|---|---|
| main | 500 | 500 | 500 | 0 |
| candidate | 500 | 0 | 0 | 500 |

Two separate count-only fixture windows each made 500 serial calls plus two full-semantic probes, with no dedicated warmup. All readiness/runtime requests are separately ledgered; initial readiness connection failures are not fixture failures. Counts cover the worker lifetime through first close. Native hits mean successful path calls; exact-float hits are helper successes, not float-cache hits. Actual Python/Pydantic validator calls were not measured.

| Count-only fixture | Full-semantic fixture calls | Native parse attempts/successes | Native response attempts/successes | Exact-float helper attempts/successes | Native refusals |
|---|---|---|---|---|---|
| echo | 502 | 502/502 | 502/502 | 10040/10040 | 0 |
| models_large | 502 | 0/0 | 502/502 | 128512/128512 | 0 |

Both original full scripts/check.sh commands failed and remain preserved: the first had five client OSError49 cases, the second seven; each ran 1205 Python tests with25 skips and350 passing Rust tests (two ignored). Source/native bytes matched and new semantic tests passed. In a separate instrumented candidate-first control, candidate passed all four unchanged512-connection cases, while unchanged main in the second slot had four OSError49 setup errors;97 recorded failing connect attempts still had an unassigned local port. The underlying cause remains unknown: netstat returned no rows and psutil connection inspection was denied. This does not prove port exhaustion. Required coverage was subsequently completed by the explicit controller and isolated unchanged-case retries described under Validation and scope; no single scripts/check.sh invocation passed.

### Rejected parser/constructor followup, separate 36 samples

A separate standard-3.14 diagnostic compared unchanged main f0ec5d3, unchanged selected a5 source (role previous) and experimental d51 source (role candidate). It used typed20 echo and256-dict responses at64 clients, default admission,1 s warmup/4 s load, in six rounds with each role in each position twice. The experiment read the live constructor slot directly only on a normal-GIL build and reduced the parser initial reservation from64 to32 slots. It was rejected; the selected production candidate remains the original a5 source. No inline/noinline change was applied.

All 36 measurements passed the recorded diagnostic gates, with 6,538,537 HTTP200 replies, 0 transport errors and 0 unexpected statuses. All 72 tracked identities are gone. Experimental qualification is limited to209 standard-3.14 focused tests (207 passed, two skipped) plus one same-source embedded-CP313 Rust constructor regression; this is not full installed acceptance or the production candidate.

| Case:role | Median req/s | Median CPU µs/success |
|---|---|---|
| typed20-314-c64:main | 57,623 | 15.654 |
| typed20-314-c64:previous | 60,560 | 14.930 |
| typed20-314-c64:candidate | 59,855 | 15.115 |
| models256-314-c64:previous | 32,237 | 28.461 |
| models256-314-c64:candidate | 32,776 | 28.149 |
| models256-314-c64:main | 32,986 | 28.246 |

| Trial numerator/denominator | Paired RPS (six-round range) | Paired CPU/success (six-round range) |
|---|---|---|
| typed20-314-c64:previous/main | 1.04173× (0.89458–1.19339) | 0.96605× (0.86932–1.12062) |
| typed20-314-c64:candidate/main | 0.96795× (0.72192–1.21840) | 1.03323× (0.84977–1.25617) |
| typed20-314-c64:candidate/previous | 0.97242× (0.77116–1.11874) | 1.03002× (0.88862–1.19875) |
| models256-314-c64:previous/main | 1.03518× (0.89372–1.09540) | 0.97624× (0.92131–1.09880) |
| models256-314-c64:candidate/main | 1.00982× (0.92880–1.12620) | 0.98877× (0.89481–1.05934) |
| models256-314-c64:candidate/previous | 1.02070× (0.93165–1.07247) | 0.98069× (0.96335–1.05539) |

The unchanged a5/main typed20 RPS ratio in this later study was 1.04173× (0.89458–1.19339), whereas the primary three-round study recorded 0.947× (0.939–0.960). The primary decrease did not consistently repeat across these independent studies. Both are retained without pooling, best-of selection or attributing a speed change to the rejected experiment.

### Separate codec micro36 and static code generation

The private-codec diagnostic ran three cases, two unchanged main/a5 roles and six balanced rounds (36 isolated windows). It includes Python/private-FFI/result allocation and N−1 prior-result decrefs, retaining the final result for post-check; it excludes HTTP body-meter/admission. GC policy was unchanged, with observed GC counters retained. All CPU and wall ratio ranges cross parity, so these windows establish no consistent six-round win/loss or HTTP cause.

| Private codec case | Main CPU ns/call | a5 CPU ns/call | Paired a5/main CPU (six-round range) | Paired a5/main wall (six-round range) |
|---|---|---|---|---|
| encode-order20 | 1,451.63 | 1,372.09 | 0.94210× (0.85301–1.11836) | 0.92922× (0.81534–1.13239) |
| encode-models256 | 17,613.46 | 16,718.61 | 0.95305× (0.90950–1.01137) | 0.93749× (0.85435–1.08855) |
| parse-order20 | 3,646.08 | 3,813.56 | 1.07830× (0.85911–1.18275) | 1.11157× (0.80804–1.29932) |

Static llvm-objdump receipts bind the same CP314 main/a5 native binaries. The generic value symbol spans760/696 bytes, model-node spans1116/1172 bytes, and a5 exact_float spans676 bytes to the next symbol. These are symbol spans, not exact function or memory sizes. The a5 generic value path uses an AArch64 b tail branch to exact_float; static layout does not establish runtime overhead or causality. Source/native/wheel/test identities and raw evidence hashes are retained in investigation-summary.json.

## Validation and scope

Required installed coverage completed by retaining the full-suite run and passing seven unchanged strict cases in an isolated retry group, then the83-test harness (three skips) and six shell syntax checks. Both original full scripts failed: the retained second attempt ran1205 unique Python cases with25 skips and seven client setup errors. Its1173 initial passes plus seven duplicate-case retries establish1180 unique passes; retries are not additional unique tests. No single scripts/check.sh invocation passed. Rust coverage:350 passed, two ignored. All five focused interpreter/GIL gates passed and matched the measured native hashes.

Every full three-round comparison requires zero measured status/transport/body-length failures and warmup status/transport failures, complete positive warmup records, semantic body probes, stable package/native/GIL/JIT/pool identity, client below 1.7 CPU cores and clean shutdown. Independent SQL audit covers all measured databases and binds the exact plan and result files.

Shared unpinned host; no OS CPU quota or affinity is asserted. Four CPU/sync slots size pools and do not isolate processors. Recorded host ranges describe interference. CPU/request includes drain/export and summed RSS can double-count shared pages. This does not establish formal M0, a fixed-rate production SLO, billing, GPU acceleration or worker-scaling acceptance.

Full raw SQLite, response probes, execution logs and source/native receipts remain in the local study cache. Public summaries retain hashes and version identities without local filesystem paths.

## Local data and reproduction

This public directory is an allowlisted result subset, not a standalone benchmark harness. Exact dependency pins and per-ABI native/wheel/source hashes are in `provenance.json`; the executed test gates, independent audit and post-timing guards have separate receipts.

The local raw study cache is named `four-workload-performance-2026-11-18`. It contains the frozen harness and plan, exact pinned requirements, raw SQLite, response probes, logs, source/build manifests and installed environments. From that cache, regenerate the analysis, audit and public report from the recorded data with:

```sh
./venv-candidate-cp313/bin/python analyse_bench.py --timings-finished
./venv-candidate-cp313/bin/python audit_sql.py --timings-finished
./venv-candidate-cp313/bin/python write_public_report.py --timings-finished --out reproduced-public-report
```

A fresh timing study requires a new cache/frozen plan and validated environments; preserve the recorded run.
