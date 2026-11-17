# Generic dependency and free-threaded performance

Completed 395 fresh samples; 393 qualified. Measured 32,464,979 successful HTTP responses. All raw failures are retained and any failed comparison is withheld.

Candidate is `523932a base + uncommitted dispatch/async changes`, identified by its frozen source/native/wheel manifests and working-tree diff hash. The module version 1.0.0rc7 and HEAD alone do not identify candidate code. Baselines are exact main `523932a8a92a31c3fd124772dce6bfd52ba9b3d5` and FastAPI 0.162.2. Main artifacts are read-only PR130 source wheels: all 103 original runtime/build source hashes match main. Candidate source wheels use the same Rust 1.92.0 release profile with strip=none. Published-wheel throughput is not claimed.

Generic Depends improved paired candidate/main throughput by 4.4–16.6% across the four modes, using 8.1–18.0% less CPU per successful reply. Request-taking sync handlers improved by 0.8–7.9% in median paired throughput, with lower CPU per success in every mode. Broader workload results were mixed.

The free-threaded gap remains: GIL-off CPU sync reached 2.844× standard 3.14 throughput, while models256 reached 0.801×, generic Depends 0.880× and Request 0.809×. This run does not show a blanket throughput improvement.

The following candidate/main cases were below throughput parity in all three paired rounds. These measured decreases remain part of the result; no cause is assigned.

| Measured case | Paired candidate/main RPS | Paired candidate/main CPU/success |
|---|---|---|
| Standard 3.13 CPU inline c16 | 0.971× (0.962–0.999) | 1.026× (1.002–1.043) |
| FT GIL-on CPU inline c16 | 0.983× (0.981–0.987) | 1.008× (1.004–1.016) |
| FT GIL-on CPU sync c16 | 0.980× (0.966–0.994) | 1.004× (1.002–1.037) |
| Standard 3.13 256KiB bytes c64 | 0.962× (0.917–0.976) | 1.036× (1.017–1.047) |
| FT GIL-off JSON c64 | 0.987× (0.936–0.993) | 1.027× (1.007–1.051) |

The study has 387 three-round performance samples and eight separate one-round functional controls. All 43 performance case groups use every contender position once across three rounds. Python 3.13.18, standard 3.14.7 and free-threaded 3.14.7 with GIL on/off are recorded explicitly. JIT is disabled and checked before/after; GIL-off is never forced past an incompatible import.

## Core HTTP on Python 3.13

Each framework cell gives successful req/s / p99 ms. Paired ratios include the three-round range.

| Python | Workload | Candidate req/s / p99 | Main523932a req/s / p99 | FastAPI req/s / p99 | Candidate/Main523932a paired | Candidate/FastAPI paired |
|---|---|---|---|---|---|---|
| cp313 | json c64 | 98,482 / 1.884 | 98,680 / 1.852 | 17,310 / 7.612 | 1.028× (0.995–1.084) | 5.715× (5.689–6.868) |
| cp313 | echo c64 | 54,623 / 2.736 | 56,022 / 2.652 | 10,608 / 12.161 | 0.963× (0.935–1.008) | 5.173× (4.938–5.222) |
| cp313 | io c64 | 41,947 / 3.553 | 43,385 / 2.717 | 17,012 / 12.821 | 0.962× (0.961–1.025) | 2.451× (2.377–2.537) |
| cp313 | fanout c64 | 17,691 / 9.859 | 17,779 / 10.082 | 7,753 / 20.323 | 0.995× (0.920–1.128) | 2.339× (2.282–2.509) |
| cp313 | sync c64 | 12,767 / 6.124 | 12,753 / 6.212 | 8,077 / 16.953 | 1.001× (0.996–1.002) | 1.564× (1.319–1.581) |
| cp313 | sync-stream c64 | 13,120 / 10.732 | 13,291 / 10.941 | 1,140 / 118.607 | 0.987× (0.954–1.060) | 11.335× (9.605–11.911) |
| cp313 | stream c64 | 10,913 / 9.979 | 11,038 / 10.134 | 4,332 / 24.678 | 0.989× (0.946–1.116) | 2.550× (2.519–2.609) |
| cp313 | bytes c64 | 20,842 / 6.827 | 21,673 / 5.913 | 10,712 / 13.366 | 0.962× (0.917–0.976) | 1.946× (1.888–2.051) |
| cp313 | dependencies c64 | 59,943 / 2.810 | 59,744 / 2.755 | 14,108 / 12.978 | 1.016× (0.996–1.061) | 4.203× (4.181–4.321) |

## Core HTTP on Python 3.13 CPU and memory

Framework cells give CPU µs/success / summed RSS MiB. CPU ratios use the same qualified rounds as throughput.

| Python | Workload | Candidate CPU / RSS | Main523932a CPU / RSS | FastAPI CPU / RSS | Candidate/Main523932a CPU paired | Candidate/FastAPI CPU paired |
|---|---|---|---|---|---|---|
| cp313 | json c64 | 8.88 / 86.8 | 9.08 / 87.1 | 52.87 / 55.3 | 0.977× (0.935–0.995) | 0.166× (0.145–0.168) |
| cp313 | echo c64 | 16.49 / 87.2 | 16.30 / 87.1 | 87.77 / 55.2 | 1.029× (0.982–1.071) | 0.187× (0.187–0.199) |
| cp313 | io c64 | 13.14 / 86.8 | 12.84 / 87.1 | 55.47 / 55.7 | 1.024× (1.000–1.038) | 0.238× (0.237–0.245) |
| cp313 | fanout c64 | 53.43 / 88.5 | 53.21 / 88.6 | 121.74 / 57.6 | 1.002× (0.903–1.098) | 0.439× (0.408–0.442) |
| cp313 | sync c64 | 38.72 / 89.2 | 39.24 / 89.2 | 123.66 / 58.2 | 0.989× (0.972–1.015) | 0.323× (0.297–0.349) |
| cp313 | sync-stream c64 | 75.09 / 90.8 | 74.76 / 91.0 | 849.01 / 59.3 | 0.994× (0.986–1.021) | 0.090× (0.088–0.100) |
| cp313 | stream c64 | 83.44 / 88.2 | 82.94 / 88.3 | 202.71 / 57.9 | 0.993× (0.948–1.077) | 0.400× (0.393–0.412) |
| cp313 | bytes c64 | 36.91 / 86.8 | 36.25 / 86.9 | 73.74 / 55.2 | 1.036× (1.017–1.047) | 0.509× (0.485–0.509) |
| cp313 | dependencies c64 | 15.02 / 87.0 | 15.29 / 87.1 | 67.38 / 55.7 | 0.982× (0.975–0.997) | 0.225× (0.222–0.225) |

CPU includes sampler drain/export time. RSS sums process trees and can count shared pages more than once; unavailable USS is not reported as zero.

## 256-dict response models on Python 3.13

Each framework cell gives successful req/s / p99 ms. Paired ratios include the three-round range.

| Python | Workload | Candidate req/s / p99 | Main523932a req/s / p99 | FastAPI req/s / p99 | Candidate/Main523932a paired | Candidate/FastAPI paired |
|---|---|---|---|---|---|---|
| cp313 | models_large c64 | 30,779 / 4.505 | 30,402 / 4.008 | 4,523 / 26.509 | 1.021× (0.962–1.027) | 6.733× (6.730–6.805) |

## 256-dict response models on Python 3.13 CPU and memory

Framework cells give CPU µs/success / summed RSS MiB. CPU ratios use the same qualified rounds as throughput.

| Python | Workload | Candidate CPU / RSS | Main523932a CPU / RSS | FastAPI CPU / RSS | Candidate/Main523932a CPU paired | Candidate/FastAPI CPU paired |
|---|---|---|---|---|---|---|
| cp313 | models_large c64 | 30.48 / 90.2 | 30.81 / 89.7 | 206.54 / 55.3 | 0.989× (0.965–1.011) | 0.147× (0.146–0.148) |

CPU includes sampler drain/export time. RSS sums process trees and can count shared pages more than once; unavailable USS is not reported as zero.

## 1000-item typed uploads c16

Each framework cell gives successful req/s / p99 ms. Paired ratios include the three-round range.

| Python | Workload | Candidate req/s / p99 | Main523932a req/s / p99 | FastAPI req/s / p99 | Candidate/Main523932a paired | Candidate/FastAPI paired |
|---|---|---|---|---|---|---|
| cp313 | echo-large c16 | 2,298 / 11.630 | 2,300 / 11.650 | 666 / 51.474 | 1.016× (0.995–1.021) | 3.444× (3.443–3.451) |

## 1000-item typed uploads c16 CPU and memory

Framework cells give CPU µs/success / summed RSS MiB. CPU ratios use the same qualified rounds as throughput.

| Python | Workload | Candidate CPU / RSS | Main523932a CPU / RSS | FastAPI CPU / RSS | Candidate/Main523932a CPU paired | Candidate/FastAPI CPU paired |
|---|---|---|---|---|---|---|
| cp313 | echo-large c16 | 400.11 / 90.7 | 399.24 / 91.2 | 1,346.82 / 58.4 | 0.989× (0.975–1.002) | 0.295× (0.289–0.298) |

CPU includes sampler drain/export time. RSS sums process trees and can count shared pages more than once; unavailable USS is not reported as zero.

## 1000-item typed uploads c64

Each framework cell gives successful req/s / p99 ms. Paired ratios include the three-round range.

| Python | Workload | Candidate req/s / p99 | Main523932a req/s / p99 | FastAPI req/s / p99 | Candidate/Main523932a paired | Candidate/FastAPI paired |
|---|---|---|---|---|---|---|
| cp313 | echo-large c64 | 2,292 / 38.243 | 2,270 / 40.234 | 664† / 168.011† | 1.004× (0.999–1.014) | withheld (transport_errors) |

## 1000-item typed uploads c64 CPU and memory

Framework cells give CPU µs/success / summed RSS MiB. CPU ratios use the same qualified rounds as throughput.

| Python | Workload | Candidate CPU / RSS | Main523932a CPU / RSS | FastAPI CPU / RSS | Candidate/Main523932a CPU paired | Candidate/FastAPI CPU paired |
|---|---|---|---|---|---|---|
| cp313 | echo-large c64 | 403.60 / 95.7 | 404.94 / 97.6 | 1,346.36† / 72.8† | 0.997× (0.988–1.001) | withheld (transport_errors) |

CPU includes sampler drain/export time. RSS sums process trees and can count shared pages more than once; unavailable USS is not reported as zero.

## Generic sync paths across all modes

Each framework cell gives successful req/s / p99 ms. Paired ratios include the three-round range.

| Python | Workload | Candidate req/s / p99 | Main523932a req/s / p99 | FastAPI req/s / p99 | Candidate/Main523932a paired | Candidate/FastAPI paired |
|---|---|---|---|---|---|---|
| cp313 | generic-request c64 | 39,415 / 4.397 | 37,712 / 4.486 | 5,800 / 30.324 | 1.013× (1.009–1.121) | 6.879× (6.557–7.000) |
| cp314 | generic-request c64 | 39,726 / 4.710 | 39,464 / 4.140 | 5,912 / 23.528 | 1.008× (0.999–1.017) | 6.732× (6.666–6.875) |
| cp314t-gil1 | generic-request c64 | 36,438 / 4.500 | 34,848 / 4.766 | 4,017 / 41.929 | 1.058× (1.046–1.099) | 9.197× (8.266–9.563) |
| cp314t-gil0 | generic-request c64 | 32,044 / 5.503 | 29,174 / 7.156 | 7,345 / 26.600 | 1.079× (1.061–1.150) | 4.362× (4.022–4.560) |
| cp313 | generic-dependency c64 | 46,051 / 3.394 | 44,771 / 3.663 | 6,113 / 23.888 | 1.053× (1.022–1.054) | 7.502× (7.303–8.109) |
| cp314 | generic-dependency c64 | 45,573 / 3.590 | 43,249 / 4.176 | 6,190 / 21.522 | 1.044× (1.017–1.138) | 7.533× (7.147–7.942) |
| cp314t-gil1 | generic-dependency c64 | 42,702 / 3.834 | 40,683 / 3.679 | 4,413 / 43.543 | 1.057× (1.044–1.069) | 9.834× (9.629–10.361) |
| cp314t-gil0 | generic-dependency c64 | 40,252 / 3.941 | 34,839 / 4.883 | 6,965 / 26.638 | 1.166× (1.155–1.223) | 5.779× (5.475–5.896) |

## Generic sync paths across all modes CPU and memory

Framework cells give CPU µs/success / summed RSS MiB. CPU ratios use the same qualified rounds as throughput.

| Python | Workload | Candidate CPU / RSS | Main523932a CPU / RSS | FastAPI CPU / RSS | Candidate/Main523932a CPU paired | Candidate/FastAPI CPU paired |
|---|---|---|---|---|---|---|
| cp313 | generic-request c64 | 29.22 / 81.4 | 30.19 / 81.2 | 156.01 / 56.7 | 0.986× (0.938–0.990) | 0.189× (0.185–0.191) |
| cp314 | generic-request c64 | 28.41 / 86.8 | 29.70 / 87.0 | 159.32 / 62.2 | 0.969× (0.948–0.974) | 0.179× (0.178–0.182) |
| cp314t-gil1 | generic-request c64 | 31.21 / 101.4 | 33.10 / 102.0 | 191.23 / 76.5 | 0.943× (0.939–0.954) | 0.162× (0.158–0.169) |
| cp314t-gil0 | generic-request c64 | 40.47 / 101.4 | 44.72 / 101.5 | 175.86 / 76.5 | 0.905× (0.894–0.912) | 0.228× (0.226–0.233) |
| cp313 | generic-dependency c64 | 23.14 / 81.2 | 24.98 / 81.5 | 151.60 / 56.5 | 0.919× (0.911–0.938) | 0.151× (0.147–0.154) |
| cp314 | generic-dependency c64 | 22.82 / 86.8 | 25.31 / 87.3 | 155.14 / 62.1 | 0.909× (0.877–0.927) | 0.148× (0.143–0.155) |
| cp314t-gil1 | generic-dependency c64 | 24.69 / 98.6 | 28.22 / 99.1 | 176.66 / 75.8 | 0.890× (0.867–0.904) | 0.140× (0.128–0.146) |
| cp314t-gil0 | generic-dependency c64 | 29.18 / 98.7 | 35.68 / 98.9 | 169.97 / 75.9 | 0.820× (0.818–0.827) | 0.172× (0.170–0.176) |

CPU includes sampler drain/export time. RSS sums process trees and can count shared pages more than once; unavailable USS is not reported as zero.

## Additional standard/free-threaded HTTP

Each framework cell gives successful req/s / p99 ms. Paired ratios include the three-round range.

| Python | Workload | Candidate req/s / p99 | Main523932a req/s / p99 | FastAPI req/s / p99 | Candidate/Main523932a paired | Candidate/FastAPI paired |
|---|---|---|---|---|---|---|
| cp314 | json c64 | 99,295 / 1.957 | 103,321 / 1.829 | 16,934 / 7.881 | 0.948× (0.935–1.032) | 5.860× (5.783–5.974) |
| cp314t-gil1 | json c64 | 93,527 / 1.945 | 93,311 / 2.040 | 16,508 / 8.118 | 1.004× (1.002–1.016) | 5.665× (5.642–5.766) |
| cp314t-gil0 | json c64 | 94,261 / 1.987 | 94,928 / 1.918 | 16,241 / 8.339 | 0.987× (0.936–0.993) | 5.804× (5.177–6.143) |
| cp314 | echo c64 | 54,665 / 2.588 | 54,522 / 2.660 | 9,995 / 12.794 | 1.017× (1.003–1.045) | 5.511× (5.342–5.617) |
| cp314t-gil1 | echo c64 | 49,475 / 3.001 | 49,642 / 3.136 | 9,514 / 13.487 | 0.990× (0.969–1.043) | 5.339× (4.999–5.534) |
| cp314t-gil0 | echo c64 | 49,652 / 3.135 | 50,402 / 2.813 | 9,255 / 13.696 | 0.995× (0.985–1.004) | 5.407× (5.365–5.410) |
| cp314 | models_large c64 | 30,545 / 3.950 | 29,942 / 4.358 | 4,628 / 26.678 | 1.011× (0.992–1.034) | 6.600× (6.311–6.905) |
| cp314t-gil1 | models_large c64 | 24,327 / 4.987 | 23,935 / 5.019 | 3,631 / 33.675 | 1.015× (0.999–1.055) | 6.700× (6.596–6.760) |
| cp314t-gil0 | models_large c64 | 24,026 / 4.916 | 23,426 / 5.345 | 3,618 / 34.315 | 1.026× (0.994–1.062) | 6.641× (6.614–6.683) |

## Additional standard/free-threaded HTTP CPU and memory

Framework cells give CPU µs/success / summed RSS MiB. CPU ratios use the same qualified rounds as throughput.

| Python | Workload | Candidate CPU / RSS | Main523932a CPU / RSS | FastAPI CPU / RSS | Candidate/Main523932a CPU paired | Candidate/FastAPI CPU paired |
|---|---|---|---|---|---|---|
| cp314 | json c64 | 8.99 / 93.5 | 8.77 / 93.8 | 54.99 / 60.4 | 1.018× (0.965–1.047) | 0.164× (0.158–0.167) |
| cp314t-gil1 | json c64 | 9.64 / 105.9 | 9.54 / 105.6 | 56.41 / 71.2 | 1.010× (0.979–1.020) | 0.170× (0.170–0.171) |
| cp314t-gil0 | json c64 | 9.57 / 105.8 | 9.46 / 105.5 | 57.18 / 71.0 | 1.027× (1.007–1.051) | 0.167× (0.161–0.183) |
| cp314 | echo c64 | 16.53 / 93.8 | 16.74 / 94.0 | 93.30 / 60.3 | 0.989× (0.959–1.003) | 0.176× (0.175–0.183) |
| cp314t-gil1 | echo c64 | 18.53 / 106.3 | 18.30 / 106.1 | 98.08 / 71.2 | 0.994× (0.970–1.025) | 0.186× (0.180–0.192) |
| cp314t-gil0 | echo c64 | 18.45 / 106.2 | 18.25 / 106.0 | 100.48 / 71.1 | 1.004× (0.984–1.011) | 0.181× (0.181–0.184) |
| cp314 | models_large c64 | 30.79 / 96.8 | 31.00 / 97.0 | 203.12 / 60.7 | 0.993× (0.970–1.000) | 0.152× (0.146–0.155) |
| cp314t-gil1 | models_large c64 | 38.59 / 109.4 | 38.85 / 108.6 | 256.87 / 71.0 | 0.993× (0.979–1.025) | 0.150× (0.150–0.152) |
| cp314t-gil0 | models_large c64 | 39.47 / 109.4 | 39.44 / 109.1 | 257.68 / 71.0 | 1.001× (0.962–1.004) | 0.151× (0.151–0.153) |

CPU includes sampler drain/export time. RSS sums process trees and can count shared pages more than once; unavailable USS is not reported as zero.

## CPU work c16

Each framework cell gives successful req/s / p99 ms. Paired ratios include the three-round range.

| Python | Workload | Candidate req/s / p99 | Main523932a req/s / p99 | FastAPI req/s / p99 | Candidate/Main523932a paired | Candidate/FastAPI paired |
|---|---|---|---|---|---|---|
| cp313 | cpu-inline c16 | 496 / 42.620 | 510 / 37.713 | 478 / 69.317 | 0.971× (0.962–0.999) | 1.038× (1.033–1.070) |
| cp314 | cpu-inline c16 | 641 / 31.458 | 663 / 30.482 | 598 / 55.652 | 0.980× (0.966–1.010) | 1.061× (1.057–1.088) |
| cp314t-gil1 | cpu-inline c16 | 665 / 31.913 | 678 / 32.010 | 653 / 50.674 | 0.983× (0.981–0.987) | 1.022× (0.954–1.032) |
| cp314t-gil0 | cpu-inline c16 | 640 / 33.573 | 678 / 31.065 | 652 / 50.220 | 0.998× (0.944–1.017) | 1.031× (0.982–1.080) |
| cp313 | cpu-sync c16 | 537 / 56.898 | 507 / 59.057 | 451 / 49.708 | 0.997× (0.967–1.062) | 1.088× (1.045–1.210) |
| cp314 | cpu-sync c16 | 703 / 39.481 | 652 / 44.625 | 630 / 44.543 | 0.995× (0.995–1.079) | 1.108× (1.093–1.165) |
| cp314t-gil1 | cpu-sync c16 | 640 / 47.377 | 653 / 43.373 | 490 / 54.328 | 0.980× (0.966–0.994) | 1.198× (1.129–1.304) |
| cp314t-gil0 | cpu-sync c16 | 1,805 / 13.791 | 1,940 / 13.996 | 1,518 / 20.862 | 1.017× (0.870–1.075) | 1.373× (1.147–1.401) |
| cp313 | cpu-offload-auto c16 | 1,727 / 18.492 | 1,772 / 16.739 | 1,702 / 17.196 | 0.975× (0.965–1.007) | 0.999× (0.972–1.045) |
| cp314 | cpu-offload-auto c16 | 2,247 / 16.916 | 2,202 / 14.059 | 2,216 / 13.694 | 1.001× (0.951–1.020) | 1.014× (0.989–1.017) |
| cp314t-gil1 | cpu-offload-auto c16 | 2,058 / 20.225 | 2,077 / 19.923 | 2,057 / 23.038 | 1.009× (0.877–1.071) | 1.017× (0.955–1.019) |
| cp314t-gil0 | cpu-offload-auto c16 | 2,183 / 13.460 | 2,156 / 14.571 | 2,232 / 16.513 | 1.012× (0.956–1.012) | 1.005× (0.918–1.030) |

## CPU work c16 CPU and memory

Framework cells give CPU µs/success / summed RSS MiB. CPU ratios use the same qualified rounds as throughput.

| Python | Workload | Candidate CPU / RSS | Main523932a CPU / RSS | FastAPI CPU / RSS | Candidate/Main523932a CPU paired | Candidate/FastAPI CPU paired |
|---|---|---|---|---|---|---|
| cp313 | cpu-inline c16 | 1,934.27 / 79.9 | 1,854.28 / 79.3 | 2,005.39 / 72.4 | 1.026× (1.002–1.043) | 0.965× (0.941–0.966) |
| cp314 | cpu-inline c16 | 1,498.96 / 85.3 | 1,454.90 / 85.1 | 1,597.35 / 81.6 | 1.029× (1.003–1.030) | 0.956× (0.935–0.957) |
| cp314t-gil1 | cpu-inline c16 | 1,430.26 / 97.1 | 1,418.22 / 96.8 | 1,459.34 / 95.2 | 1.008× (1.004–1.016) | 0.974× (0.973–1.042) |
| cp314t-gil0 | cpu-inline c16 | 1,478.49 / 97.1 | 1,412.82 / 96.8 | 1,463.98 / 95.1 | 1.001× (0.990–1.046) | 0.981× (0.932–1.010) |
| cp313 | cpu-sync c16 | 1,807.09 / 80.3 | 1,866.90 / 80.6 | 2,078.33 / 72.6 | 1.002× (0.968–1.017) | 0.916× (0.861–0.955) |
| cp314 | cpu-sync c16 | 1,386.20 / 85.3 | 1,459.06 / 85.6 | 1,503.54 / 81.7 | 0.991× (0.950–1.007) | 0.922× (0.899–0.923) |
| cp314t-gil1 | cpu-sync c16 | 1,486.06 / 98.9 | 1,480.77 / 98.2 | 1,804.32 / 98.6 | 1.004× (1.002–1.037) | 0.907× (0.824–0.911) |
| cp314t-gil0 | cpu-sync c16 | 2,174.89 / 98.6 | 2,000.33 / 98.2 | 2,400.75 / 98.5 | 0.992× (0.941–1.141) | 0.784× (0.771–0.999) |
| cp313 | cpu-offload-auto c16 | 2,424.82 / 229.8 | 2,408.33 / 231.0 | 2,539.74 / 152.4 | 0.998× (0.990–1.007) | 0.966× (0.938–0.982) |
| cp314 | cpu-offload-auto c16 | 1,945.58 / 250.4 | 1,939.82 / 251.1 | 2,015.52 / 183.8 | 1.003× (0.985–1.008) | 0.962× (0.962–0.965) |
| cp314t-gil1 | cpu-offload-auto c16 | 1,989.14 / 281.2 | 2,033.52 / 280.6 | 2,037.28 / 211.9 | 0.979× (0.975–1.006) | 0.971× (0.963–0.977) |
| cp314t-gil0 | cpu-offload-auto c16 | 1,990.74 / 281.0 | 1,960.71 / 280.4 | 2,027.19 / 211.8 | 1.006× (0.993–1.015) | 0.982× (0.981–0.997) |

CPU includes sampler drain/export time. RSS sums process trees and can count shared pages more than once; unavailable USS is not reported as zero.

## CPU sync c64

Each framework cell gives successful req/s / p99 ms. Paired ratios include the three-round range.

| Python | Workload | Candidate req/s / p99 | Main523932a req/s / p99 | FastAPI req/s / p99 | Candidate/Main523932a paired | Candidate/FastAPI paired |
|---|---|---|---|---|---|---|
| cp313 | cpu-sync c64 | 504 / 192.360 | 499 / 192.801 | 441 / 182.017 | 0.984× (0.962–1.018) | 1.111× (1.047–1.171) |
| cp314 | cpu-sync c64 | 637 / 174.518 | 634 / 174.463 | 559 / 149.609 | 1.021× (1.003–1.028) | 1.139× (1.131–1.185) |

## CPU sync c64 CPU and memory

Framework cells give CPU µs/success / summed RSS MiB. CPU ratios use the same qualified rounds as throughput.

| Python | Workload | Candidate CPU / RSS | Main523932a CPU / RSS | FastAPI CPU / RSS | Candidate/Main523932a CPU paired | Candidate/FastAPI CPU paired |
|---|---|---|---|---|---|---|
| cp313 | cpu-sync c64 | 1,898.47 / 81.5 | 1,909.05 / 81.7 | 2,074.33 / 73.9 | 1.024× (0.991–1.042) | 0.943× (0.910–0.963) |
| cp314 | cpu-sync c64 | 1,489.14 / 86.7 | 1,494.30 / 86.8 | 1,665.55 / 83.0 | 0.990× (0.988–0.997) | 0.894× (0.886–0.900) |

CPU includes sampler drain/export time. RSS sums process trees and can count shared pages more than once; unavailable USS is not reported as zero.

## Free-threaded / standard interpreter ratios

Each ratio pairs the same role and workload within each natural round; modes execute sequentially on the shared host. Ratios give the three-round median and range. CPU ratios are CPU time per successful reply.

| Role | Case | Modes, numerator/denominator | Paired RPS | Paired CPU/success |
|---|---|---|---|---|
| candidate | cpu-inline | cp314t-gil1/cp314 | 0.983× (0.978–1.037) | 0.996× (0.954–1.010) |
| candidate | cpu-inline | cp314t-gil0/cp314 | 0.990× (0.990–1.009) | 1.000× (0.969–1.009) |
| candidate | cpu-inline | cp314t-gil0/cp314t-gil1 | 1.012× (0.954–1.026) | 0.990× (0.973–1.057) |
| candidate | cpu-sync | cp314t-gil1/cp314 | 0.998× (0.812–1.029) | 0.998× (0.981–1.187) |
| candidate | cpu-sync | cp314t-gil0/cp314 | 2.844× (2.566–2.900) | 1.435× (1.407–1.571) |
| candidate | cpu-sync | cp314t-gil0/cp314t-gil1 | 2.848× (2.820–3.160) | 1.410× (1.323–1.464) |
| candidate | cpu-offload-auto | cp314t-gil1/cp314 | 0.933× (0.866–0.955) | 1.022× (1.016–1.032) |
| candidate | cpu-offload-auto | cp314t-gil0/cp314 | 1.013× (0.912–1.017) | 1.023× (1.022–1.032) |
| candidate | cpu-offload-auto | cp314t-gil0/cp314t-gil1 | 1.061× (0.978–1.175) | 1.001× (0.990–1.015) |
| main | cpu-inline | cp314t-gil1/cp314 | 1.005× (0.976–1.022) | 0.998× (0.975–1.020) |
| main | cpu-inline | cp314t-gil0/cp314 | 1.001× (0.941–1.047) | 1.002× (0.952–1.050) |
| main | cpu-inline | cp314t-gil0/cp314t-gil1 | 0.996× (0.920–1.073) | 1.004× (0.933–1.077) |
| main | cpu-sync | cp314t-gil1/cp314 | 0.999× (0.906–1.045) | 1.003× (0.968–1.088) |
| main | cpu-sync | cp314t-gil0/cp314 | 2.722× (2.631–3.316) | 1.505× (1.246–1.506) |
| main | cpu-sync | cp314t-gil0/cp314t-gil1 | 3.003× (2.634–3.173) | 1.384× (1.287–1.502) |
| main | cpu-offload-auto | cp314t-gil1/cp314 | 0.939× (0.893–0.943) | 1.034× (1.022–1.052) |
| main | cpu-offload-auto | cp314t-gil0/cp314 | 0.973× (0.956–1.002) | 1.023× (1.011–1.024) |
| main | cpu-offload-auto | cp314t-gil0/cp314t-gil1 | 1.032× (1.018–1.121) | 0.991× (0.961–1.001) |
| fastapi | cpu-inline | cp314t-gil1/cp314 | 1.093× (1.002–1.105) | 0.917× (0.914–0.993) |
| fastapi | cpu-inline | cp314t-gil0/cp314 | 1.045× (0.970–1.090) | 0.961× (0.917–1.027) |
| fastapi | cpu-inline | cp314t-gil0/cp314t-gil1 | 0.968× (0.946–0.998) | 1.034× (1.003–1.049) |
| fastapi | cpu-sync | cp314t-gil1/cp314 | 0.874× (0.741–1.030) | 1.099× (0.990–1.202) |
| fastapi | cpu-sync | cp314t-gil0/cp314 | 2.412× (2.001–2.801) | 1.615× (1.327–1.880) |
| fastapi | cpu-sync | cp314t-gil0/cp314t-gil1 | 2.701× (2.341–3.207) | 1.564× (1.207–1.631) |
| fastapi | cpu-offload-auto | cp314t-gil1/cp314 | 0.928× (0.921–0.929) | 1.022× (1.000–1.025) |
| fastapi | cpu-offload-auto | cp314t-gil0/cp314 | 1.004× (0.997–1.007) | 1.003× (0.995–1.006) |
| fastapi | cpu-offload-auto | cp314t-gil0/cp314t-gil1 | 1.085× (1.073–1.089) | 0.982× (0.981–0.995) |
| candidate | generic-request | cp314t-gil1/cp314 | 0.916× (0.906–0.970) | 1.097× (1.079–1.113) |
| candidate | generic-request | cp314t-gil0/cp314 | 0.809× (0.791–0.814) | 1.434× (1.385–1.435) |
| candidate | generic-request | cp314t-gil0/cp314t-gil1 | 0.864× (0.834–0.898) | 1.289× (1.283–1.308) |
| candidate | generic-dependency | cp314t-gil1/cp314 | 0.954× (0.863–0.965) | 1.082× (1.016–1.152) |
| candidate | generic-dependency | cp314t-gil0/cp314 | 0.880× (0.825–0.910) | 1.294× (1.202–1.314) |
| candidate | generic-dependency | cp314t-gil0/cp314t-gil1 | 0.943× (0.922–0.956) | 1.183× (1.141–1.196) |
| candidate | json | cp314t-gil1/cp314 | 0.942× (0.937–0.952) | 1.075× (1.030–1.106) |
| candidate | json | cp314t-gil0/cp314 | 0.960× (0.863–0.963) | 1.065× (1.038–1.163) |
| candidate | json | cp314t-gil0/cp314t-gil1 | 1.011× (0.920–1.019) | 1.007× (0.991–1.052) |
| candidate | echo | cp314t-gil1/cp314 | 0.910× (0.870–0.922) | 1.121× (1.081–1.123) |
| candidate | echo | cp314t-gil0/cp314 | 0.908× (0.878–0.928) | 1.103× (1.100–1.123) |
| candidate | echo | cp314t-gil0/cp314t-gil1 | 1.019× (0.952–1.044) | 0.984× (0.979–1.039) |
| candidate | models_large | cp314t-gil1/cp314 | 0.795× (0.786–0.830) | 1.254× (1.248–1.262) |
| candidate | models_large | cp314t-gil0/cp314 | 0.801× (0.776–0.812) | 1.282× (1.240–1.291) |
| candidate | models_large | cp314t-gil0/cp314t-gil1 | 0.988× (0.979–1.008) | 1.022× (0.993–1.023) |
| main | generic-request | cp314t-gil1/cp314 | 0.883× (0.855–0.898) | 1.114× (1.107–1.130) |
| main | generic-request | cp314t-gil0/cp314 | 0.739× (0.716–0.766) | 1.509× (1.502–1.525) |
| main | generic-request | cp314t-gil0/cp314t-gil1 | 0.837× (0.797–0.896) | 1.354× (1.349–1.357) |
| main | generic-dependency | cp314t-gil1/cp314 | 0.929× (0.908–0.965) | 1.088× (1.087–1.136) |
| main | generic-dependency | cp314t-gil0/cp314 | 0.806× (0.732–0.823) | 1.410× (1.359–1.423) |
| main | generic-dependency | cp314t-gil0/cp314t-gil1 | 0.852× (0.807–0.867) | 1.250× (1.241–1.308) |
| main | json | cp314t-gil1/cp314 | 0.889× (0.873–0.971) | 1.103× (1.017–1.115) |
| main | json | cp314t-gil0/cp314 | 0.919× (0.862–1.004) | 1.079× (1.001–1.127) |
| main | json | cp314t-gil0/cp314t-gil1 | 1.034× (0.987–1.034) | 0.984× (0.979–1.010) |
| main | echo | cp314t-gil1/cp314 | 0.899× (0.881–0.982) | 1.102× (1.048–1.134) |
| main | echo | cp314t-gil0/cp314 | 0.924× (0.889–0.974) | 1.090× (1.054–1.129) |
| main | echo | cp314t-gil0/cp314t-gil1 | 0.993× (0.989–1.049) | 1.005× (0.962–1.024) |
| main | models_large | cp314t-gil1/cp314 | 0.792× (0.780–0.813) | 1.233× (1.215–1.276) |
| main | models_large | cp314t-gil0/cp314 | 0.782× (0.762–0.810) | 1.268× (1.251–1.289) |
| main | models_large | cp314t-gil0/cp314t-gil1 | 0.963× (0.962–1.040) | 1.015× (1.010–1.044) |
| fastapi | generic-request | cp314t-gil1/cp314 | 0.677× (0.676–0.746) | 1.210× (1.156–1.254) |
| fastapi | generic-request | cp314t-gil0/cp314 | 1.237× (1.227–1.324) | 1.104× (1.099–1.127) |
| fastapi | generic-request | cp314t-gil0/cp314t-gil1 | 1.812× (1.775–1.828) | 0.908× (0.899–0.954) |
| fastapi | generic-dependency | cp314t-gil1/cp314 | 0.712× (0.666–0.731) | 1.143× (1.133–1.224) |
| fastapi | generic-dependency | cp314t-gil0/cp314 | 1.125× (1.111–1.211) | 1.096× (1.084–1.096) |
| fastapi | generic-dependency | cp314t-gil0/cp314t-gil1 | 1.657× (1.561–1.690) | 0.948× (0.895–0.967) |
| fastapi | json | cp314t-gil1/cp314 | 0.974× (0.971–0.976) | 1.029× (1.012–1.032) |
| fastapi | json | cp314t-gil0/cp314 | 0.959× (0.916–0.995) | 1.040× (1.008–1.091) |
| fastapi | json | cp314t-gil0/cp314t-gil1 | 0.983× (0.940–1.025) | 1.027× (0.980–1.057) |
| fastapi | echo | cp314t-gil1/cp314 | 0.930× (0.907–0.970) | 1.071× (1.026–1.094) |
| fastapi | echo | cp314t-gil0/cp314 | 0.912× (0.904–0.945) | 1.091× (1.066–1.092) |
| fastapi | echo | cp314t-gil0/cp314t-gil1 | 0.973× (0.940–1.043) | 1.020× (0.975–1.064) |
| fastapi | models_large | cp314t-gil1/cp314 | 0.796× (0.774–0.810) | 1.246× (1.226–1.299) |
| fastapi | models_large | cp314t-gil0/cp314 | 0.800× (0.767–0.807) | 1.250× (1.230–1.320) |
| fastapi | models_large | cp314t-gil0/cp314t-gil1 | 0.996× (0.990–1.005) | 1.003× (1.003–1.016) |

## Single-client functional controls

These are eight one-round observations on candidate/main, with no FastAPI population and no paired performance claims.

| Mode | Control | Candidate req/s | Main req/s |
|---|---|---|---|
| cp313 | cpu-inline | 456 | 461 |
| cp313 | cpu-sync | 478 | 507 |
| cp314 | cpu-inline | 654 | 628 |
| cp314 | cpu-sync | 629 | 624 |

## Shared host context

Ranges are observed during natural load. They do not establish CPU isolation; first unprimed psutil readings are excluded. Each case cell gives host CPU-percent range / one-minute load-average range. Full 1/5/15-minute ranges remain in the public JSON summary.

| Context | Observed range |
|---|---|
| Host CPU % | 0.0–100.0 |
| Load average 1 min | 11.87–24.26 |
| Load average 5 min | 12.51–21.32 |
| Load average 15 min | 13.92–19.61 |

| Mode | Case | Candidate CPU% / load1m | Main CPU% / load1m | FastAPI CPU% / load1m |
|---|---|---|---|---|
| cp313 | cpu-inline c16 | 0.0–90.8 / 12.09–19.96 | 0.0–88.9 / 12.65–19.65 | 0.0–91.2 / 12.60–20.48 |
| cp314 | cpu-inline c16 | 0.0–90.8 / 12.63–18.95 | 0.0–89.7 / 12.42–18.95 | 0.0–88.8 / 11.99–19.49 |
| cp314t-gil1 | cpu-inline c16 | 0.0–87.4 / 12.00–18.95 | 0.0–92.4 / 12.21–20.73 | 38.0–79.4 / 12.00–19.10 |
| cp314t-gil0 | cpu-inline c16 | 39.6–90.5 / 12.19–18.69 | 0.0–95.7 / 12.42–20.09 | 0.0–80.8 / 11.96–18.48 |
| cp313 | cpu-sync c16 | 0.0–95.7 / 11.96–19.78 | 0.0–86.9 / 12.04–20.18 | 0.0–100.0 / 12.13–20.99 |
| cp314 | cpu-sync c16 | 0.0–92.6 / 11.87–21.04 | 0.0–89.4 / 12.12–21.28 | 0.0–95.4 / 11.87–21.96 |
| cp314t-gil1 | cpu-sync c16 | 0.0–95.4 / 11.94–19.58 | 0.0–90.5 / 13.62–19.87 | 0.0–96.8 / 12.28–19.68 |
| cp314t-gil0 | cpu-sync c16 | 0.0–92.6 / 14.12–21.17 | 0.0–94.8 / 14.12–21.08 | 0.0–97.7 / 15.19–19.27 |
| cp313 | cpu-offload-auto c16 | 0.0–98.6 / 15.25–23.37 | 0.0–98.6 / 16.34–23.37 | 0.0–99.4 / 16.77–22.47 |
| cp314 | cpu-offload-auto c16 | 0.0–97.1 / 17.32–21.80 | 0.0–98.2 / 16.79–21.18 | 70.4–96.2 / 16.79–21.19 |
| cp314t-gil1 | cpu-offload-auto c16 | 0.0–99.6 / 17.94–21.10 | 0.0–95.6 / 17.94–21.57 | 0.0–97.1 / 17.32–20.70 |
| cp314t-gil0 | cpu-offload-auto c16 | 0.0–95.9 / 18.83–21.69 | 0.0–97.8 / 18.45–21.64 | 0.0–97.3 / 17.45–22.53 |
| cp313 | cpu-sync c64 | 0.0–88.9 / 17.57–19.78 | 0.0–92.9 / 17.61–21.32 | 0.0–93.1 / 16.84–20.90 |
| cp314 | cpu-sync c64 | 0.0–98.0 / 16.33–21.16 | 0.0–92.3 / 16.99–21.71 | 0.0–91.4 / 16.99–21.09 |
| cp313 | json c64 | 0.0–95.7 / 16.18–20.85 | 65.0–94.5 / 16.18–21.59 | 0.0–90.5 / 17.21–20.37 |
| cp313 | echo c64 | 0.0–98.2 / 17.21–22.26 | 0.0–93.6 / 18.00–22.00 | 0.0–94.9 / 18.01–22.98 |
| cp313 | io c64 | 0.0–91.2 / 17.93–21.19 | 0.0–95.5 / 17.86–22.21 | 0.0–91.2 / 17.86–20.99 |
| cp313 | fanout c64 | 0.0–92.4 / 17.59–21.55 | 0.0–94.8 / 17.59–21.43 | 54.4–96.6 / 17.89–22.04 |
| cp313 | sync c64 | 0.0–100.0 / 17.73–21.98 | 0.0–97.2 / 16.16–23.02 | 0.0–93.2 / 15.51–21.39 |
| cp313 | sync-stream c64 | 0.0–95.6 / 15.51–23.11 | 59.4–91.2 / 15.57–23.11 | 0.0–90.7 / 15.60–21.90 |
| cp313 | stream c64 | 0.0–96.4 / 15.15–22.61 | 0.0–93.8 / 17.27–22.08 | 62.4–89.1 / 17.27–22.31 |
| cp313 | bytes c64 | 0.0–94.4 / 19.60–22.81 | 0.0–91.8 / 18.41–22.39 | 0.0–91.2 / 18.41–23.31 |
| cp313 | dependencies c64 | 0.0–92.3 / 18.30–21.72 | 0.0–90.5 / 17.96–21.42 | 0.0–95.5 / 17.96–21.16 |
| cp313 | models_large c64 | 0.0–91.2 / 17.88–19.65 | 0.0–94.5 / 18.03–20.77 | 0.0–90.3 / 17.55–20.99 |
| cp313 | echo-large c16 | 0.0–91.9 / 17.67–21.85 | 0.0–94.7 / 17.99–20.98 | 0.0–97.3 / 17.19–21.10 |
| cp313 | echo-large c64 | 0.0–99.6 / 16.13–22.89 | 0.0–91.9 / 16.22–24.26 | 0.0–95.3 / 16.36–20.68 |
| cp313 | generic-request c64 | 0.0–100.0 / 15.00–20.22 | 0.0–97.1 / 15.52–20.12 | 58.2–93.6 / 15.00–19.72 |
| cp314 | generic-request c64 | 61.5–96.5 / 17.72–21.32 | 0.0–96.6 / 19.14–21.13 | 0.0–90.6 / 17.99–20.94 |
| cp314t-gil1 | generic-request c64 | 0.0–89.7 / 18.53–21.02 | 0.0–94.3 / 18.07–22.05 | 0.0–89.5 / 17.56–21.54 |
| cp314t-gil0 | generic-request c64 | 0.0–94.0 / 16.09–20.51 | 0.0–93.8 / 17.12–20.54 | 0.0–91.3 / 16.54–22.77 |
| cp313 | generic-dependency c64 | 0.0–95.2 / 17.39–22.95 | 57.4–91.5 / 17.12–23.68 | 0.0–89.9 / 17.12–23.54 |
| cp314 | generic-dependency c64 | 0.0–94.8 / 17.88–21.55 | 0.0–92.2 / 17.88–23.10 | 0.0–91.6 / 17.15–22.21 |
| cp314t-gil1 | generic-dependency c64 | 0.0–92.8 / 19.32–20.43 | 0.0–91.9 / 19.52–20.94 | 0.0–90.9 / 19.65–20.91 |
| cp314t-gil0 | generic-dependency c64 | 0.0–97.9 / 18.22–19.52 | 0.0–94.1 / 18.53–20.00 | 0.0–93.3 / 18.53–19.92 |
| cp314 | json c64 | 0.0–95.7 / 17.61–20.52 | 0.0–96.6 / 17.13–21.32 | 0.0–92.8 / 17.13–20.52 |
| cp314t-gil1 | json c64 | 0.0–96.8 / 16.31–19.40 | 0.0–92.5 / 16.25–18.39 | 57.1–92.4 / 16.25–19.78 |
| cp314t-gil0 | json c64 | 66.3–96.2 / 16.57–18.39 | 0.0–96.9 / 16.31–18.91 | 0.0–92.0 / 16.57–19.08 |
| cp314 | echo c64 | 0.0–94.7 / 15.43–19.77 | 0.0–93.1 / 15.43–20.35 | 0.0–100.0 / 15.55–20.25 |
| cp314t-gil1 | echo c64 | 0.0–94.4 / 15.00–20.02 | 0.0–100.0 / 15.48–18.98 | 0.0–94.6 / 15.15–19.54 |
| cp314t-gil0 | echo c64 | 0.0–96.9 / 16.20–19.30 | 0.0–96.4 / 16.18–18.91 | 0.0–100.0 / 16.56–18.64 |
| cp314 | models_large c64 | 0.0–99.8 / 16.58–18.79 | 0.0–90.9 / 16.31–18.72 | 0.0–90.8 / 16.63–18.66 |
| cp314t-gil1 | models_large c64 | 0.0–93.8 / 15.99–20.18 | 51.6–91.1 / 15.52–20.18 | 0.0–89.2 / 15.47–19.05 |
| cp314t-gil0 | models_large c64 | 55.7–94.5 / 15.71–20.35 | 0.0–97.9 / 15.16–21.35 | 0.0–92.2 / 15.16–21.56 |

## Failures and qualification

| Variant | Measured/attempted | Measured HTTP statuses | Transport errors | Wrong lengths | Warmup non-200 | Warmup transport | Measured error reasons |
|---|---|---|---|---|---|---|---|
| Candidate | 133/133 | {"200": 14843335} | 0 | 0 | 0 | 0 | {} |
| Main523932a | 133/133 | {"200": 14607136} | 0 | 0 | 0 | 0 | {} |
| FastAPI0.162.2 | 129/129 | {"200": 3014508} | 14 | 0 | 0 | 0 | {"timeout": 14} |

Lifecycle issues: 0. Any failed or missing round withholds the entire paired population, including its CPU comparison. † marks an unqualified diagnostic median; it is not used for a speedup claim.

| Phase | Python | Variant | Workload | Round | Measured failures | Warmup failures | Warmup issues |
|---|---|---|---|---|---|---|---|
| http-large-c64 | cp313 | fastapi | echo-large | 2 | 11 | 0 | — |
| http-large-c64 | cp313 | fastapi | echo-large | 3 | 3 | 0 | — |

## Separate execution profiles

Sixteen diagnostic windows each sent 500 serial requests: eight loop call censuses and eight actual work-thread CPU windows. All 8,000 replies passed semantics. Each CPU comparison has one window per role; clock/wrapper overhead and shared-host variation remain. These observations do not measure throughput, capacity or causality and are not pooled with the natural run.

| Mode | Route | Main work CPU µs/call | Candidate work CPU µs/call | Observed Candidate/Main |
|---|---|---|---|---|
| cp313 | generic-request | 10.373 | 6.355 | 0.613× |
| cp313 | generic-dependency | 4.447 | 2.294 | 0.516× |
| cp314t-gil0 | generic-request | 7.269 | 10.570 | 1.454× |
| cp314t-gil0 | generic-dependency | 6.847 | 6.329 | 0.924× |

The free-threaded GIL-off Request window increased to 1.454× main work CPU. It is retained alongside every other observation.

Call entries include coroutine resume events and are not allocation/object counts. The source removes one ordinary-result wrapper coroutine on both routes and an async-handler wrapper on the dependency route.

| Mode | Route | _await_result entries, main → candidate | _execute_plan entries, main → candidate |
|---|---|---|---|
| cp313 | generic-request | 500 → 0 | 1000 → 1000 |
| cp313 | generic-dependency | 500 → 0 | 1500 → 1000 |
| cp314t-gil0 | generic-request | 500 → 0 | 1000 → 1000 |
| cp314t-gil0 | generic-dependency | 500 → 0 | 1500 → 1000 |

## Separate native dispatch experiment

Before the final Python changes, 36 balanced c16 samples compared the native dispatch patch with main on standard 3.13 and free-threaded GIL-off. All 36 qualified: 3,779,139 successful replies, zero errors, independent SQL audit passed. Request, Depends and models256 negative controls are all retained. This experiment has different candidate bytes and c16 concurrency; the final generic workloads use c64. Its counts and effects are not pooled with the final 395-sample study.

| Mode | Case c16 | Native-only req/s / p99 ms | Main req/s / p99 ms | Paired native/main RPS | Paired native/main CPU/success |
|---|---|---|---|---|---|
| cp313 | generic-request | 28,822 / 1.699 | 27,882 / 1.754 | 1.034× (0.788–1.089) | 0.972× (0.951–1.061) |
| cp314t-gil0 | generic-request | 23,111 / 2.174 | 23,154 / 2.536 | 0.998× (0.921–1.100) | 0.934× (0.930–0.970) |
| cp313 | generic-dependency | 32,312 / 1.740 | 29,813 / 1.850 | 1.089× (1.040–1.107) | 0.926× (0.919–0.935) |
| cp314t-gil0 | generic-dependency | 31,085 / 1.746 | 28,341 / 2.020 | 1.097× (1.057–1.176) | 0.874× (0.848–0.878) |
| cp313 | models_large | 26,146 / 1.816 | 26,836 / 1.736 | 0.973× (0.953–1.032) | 1.029× (0.964–1.043) |
| cp314t-gil0 | models_large | 20,947 / 2.062 | 20,824 / 2.030 | 1.006× (1.002–1.028) | 0.993× (0.979–1.000) |

## Validation and scope

Full installed gate: 330 Rust tests passed, 2 ignored; Python and harness counts are included in the validation receipt. All five focused interpreter/GIL gates passed and matched the measured native hashes.

Every full three-round comparison requires zero measured and warmup status/transport/length failures, complete positive warmup records, semantic body probes, stable package/native/GIL/JIT/pool identity, client below 1.7 CPU cores and clean shutdown. Independent SQL audit covers all measured databases and binds the exact plan and result files.

Shared unpinned host; no OS CPU quota or affinity is asserted. Four CPU/sync slots size pools and do not isolate processors. Recorded host ranges describe interference. CPU/request includes drain/export and summed RSS can double-count shared pages. This does not establish formal M0, a fixed-rate production SLO, billing, GPU acceleration or worker-scaling acceptance.

Full raw SQLite, response probes, execution logs and source/native receipts remain in the local study cache. Public summaries retain hashes and version identities without local filesystem paths.

## Local data and reproduction

This public directory is an allowlisted result subset, not a standalone benchmark harness. Exact dependency pins and per-ABI native/wheel/source hashes are in `provenance.json`; the executed test gates, independent audit and post-timing guards have separate receipts.

The local raw study cache is named `generic-ft-performance-2026-11-17`. It contains the frozen harness and plan, exact pinned requirements, raw SQLite, response probes, logs, source/build manifests and installed environments. From that cache, regenerate the analysis, audit and public report from the recorded data with:

```sh
./venv-candidate-cp313/bin/python analyse_bench.py --timings-finished
./venv-candidate-cp313/bin/python audit_sql.py --timings-finished
./venv-candidate-cp313/bin/python write_public_report.py --timings-finished --out reproduced-public-report
```

A fresh timing study requires a new cache/frozen plan and validated environments; preserve the recorded run.
