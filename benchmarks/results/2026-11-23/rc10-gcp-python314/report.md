# Bessemer RC10 on GCP with CPython 3.14.7 and 3.14.7t

## Findings

On one core, Bessemer reached 10.96× and 13.33× FastAPI on small JSON and
path/query with standard Python. Free-threaded Python delivered 10.61× and
13.18× on the same routes. On four cores, Bessemer and Axum were within 3%
of one another on these two workloads at the paired median in both passes.

On four cores, switching Bessemer to free-threaded Python reduced throughput
by 10.5% for 20-item typed echo, 15.3% for 1,000-item typed echo and 12.4%
for 256-model responses. The 256-model route fell from 82,262 to 72,132
replies per second. The Axum control also slowed by 5.4% on that route. The
Axum-normalised Bessemer change was 6.8% lower at the paired median.
Bessemer still led FastAPI by 7.45× and Axum by 1.25× on 256 models in the
free-threaded pass.

For inline CPU work on four cores, Bessemer and FastAPI were within 1% of
one another in both passes. Axum ran the same checksum as compiled Rust
and achieved 10.4× Bessemer on standard Python and 11.3× in the
free-threaded pass. Bessemer's sync checksum gained 6.4% at the paired
median on 3.14t, with a round range from 0.991× to 1.256×. Its median p99
fell from 27.47 ms to 18.08 ms. Each Python server used one process per
allocated core, so standard Python could already run work on multiple
cores through independent processes. The sync route used each stack's
default blocking dispatch.

The 256 KiB download reached about 2.53 GiB/s in all six four-core groups.
Bessemer and Axum reached essentially the same rate on one core. These
rates count response bodies over the observed network path.

## Method and qualification

- Standard CPython qualified 144/144 samples with 33,291,379 timed HTTP 200 replies, 0 other timed HTTP statuses and 0 timed transport errors. Warmup returned 13,246,637 HTTP 200 replies, 0 other statuses and 0 transport errors. The count of missing warmup or timed observations was 0.
- Free-threaded CPython qualified 144/144 samples with 32,067,762 timed HTTP 200 replies, 0 other timed HTTP statuses and 0 timed transport errors. Warmup returned 12,809,838 HTTP 200 replies, 0 other statuses and 0 transport errors. The count of missing warmup or timed observations was 0.

Server Intel(R) Xeon(R) Platinum 8481C CPU @ 2.70GHz with 4 guest CPUs. Client AMD EPYC 7B12 with 16 guest CPUs. The same server and load generator served both runs.

Bessemer 1.0.0rc10 and FastAPI 0.165.0 used Pydantic 2.14.4. FastAPI used Uvicorn 0.54.0 with uvloop 0.23.0 and httptools 0.9.0. Axum 0.8.9 was built from the locked native fixture and ran in both mode passes.

- standard RC10 Linux wheel SHA-256 `bd342f9c91c1fba6f75a42c1a3e09cb3b39f7ffc520a83f23445feb9020606fd`.
- free-threaded RC10 Linux wheel SHA-256 `228fc4874c76b14ebeb3d89b74112ffbaaf389c82bea497346a5aec009d210e6`.

3 rotated rounds per mode with 2 seconds warmup and 5 seconds timed HTTP/1.1 load with request drain. Runtime modes were scheduled separately. The ratio range is the minimum to maximum across corresponding rounds. Only complete, qualified round sets produce rates or ratios. CPU seconds per million estimates pinned server CPU time from guest counters.

Python workers reported their interpreter build and actual GIL state before and after load. Response probes checked the complete synthetic payloads and matching body digests. The benchmark also checked the aggregate response body bytes against the successful request count. Samples qualify when warmup and timed traffic both have zero HTTP and transport errors and the load generator stays below the client CPU limit.

| Workload | Fixture | Connections |
| --- | --- | ---: |
| json | Small JSON response | 64 |
| items | Integer path parameter and string query parameter | 64 |
| echo | 20-item typed JSON request and response | 64 |
| cpu-inline | 50,000-iteration integer checksum inside an async handler | 16 |
| cpu-sync | Same checksum using sync handler dispatch or Tokio spawn_blocking | 16 |
| large-echo | 1,000-item typed JSON request and response | 16 |
| bytes | 262,144-byte immutable response | 16 |
| models256 | 256-item typed JSON response | 64 |

### standard successful HTTP replies per second

| Cores | Route | Clients | Bessemer | FastAPI | Axum | Bessemer / FastAPI | Bessemer / Axum |
| ---: | --- | ---: | ---: | ---: | ---: | ---: | ---: |
| 1 | json | 64 | 123,463 | 11,270 | 149,241 | 10.962× (10.770–11.033) | 0.827× (0.782–0.829) |
| 1 | items | 64 | 117,206 | 8,812 | 143,390 | 13.329× (11.981–13.409) | 0.814× (0.736–0.821) |
| 1 | echo | 64 | 43,631 | 6,640 | 37,809 | 6.570× (6.520–6.614) | 1.152× (1.148–1.164) |
| 1 | cpu-inline | 16 | 445 | 432 | 4,557 | 1.028× (1.027–1.039) | 0.098× (0.098–0.098) |
| 1 | cpu-sync | 16 | 440 | 421 | 4,503 | 1.044× (1.043–1.047) | 0.098× (0.098–0.098) |
| 1 | large-echo | 16 | 1,664 | 482 | 1,348 | 3.451× (3.306–3.464) | 1.236× (1.221–1.240) |
| 1 | bytes | 16 | 10,364 | 7,931 | 10,372 | 1.307× (1.307–1.325) | 1.000× (0.999–1.000) |
| 1 | models256 | 64 | 22,902 | 3,375 | 17,680 | 6.784× (6.578–6.925) | 1.295× (1.293–1.332) |
| 4 | json | 64 | 225,744 | 43,007 | 237,300 | 5.254× (5.102–5.414) | 0.972× (0.951–0.973) |
| 4 | items | 64 | 227,439 | 33,711 | 233,431 | 6.742× (6.655–6.860) | 0.974× (0.960–1.063) |
| 4 | echo | 64 | 141,265 | 25,652 | 88,580 | 5.511× (5.484–5.519) | 1.595× (1.541–1.652) |
| 4 | cpu-inline | 16 | 1,719 | 1,717 | 17,844 | 1.001× (0.994–1.029) | 0.096× (0.096–0.099) |
| 4 | cpu-sync | 16 | 1,512 | 1,511 | 15,448 | 0.994× (0.817–1.083) | 0.098× (0.080–0.104) |
| 4 | large-echo | 16 | 6,183 | 1,947 | 4,770 | 3.175× (3.018–3.327) | 1.296× (1.229–1.373) |
| 4 | bytes | 16 | 10,365 | 10,366 | 10,373 | 1.000× (0.996–1.000) | 0.999× (0.995–0.999) |
| 4 | models256 | 64 | 82,261 | 13,620 | 60,820 | 6.042× (5.990–6.049) | 1.346× (1.317–1.381) |

### free-threaded successful HTTP replies per second

| Cores | Route | Clients | Bessemer | FastAPI | Axum | Bessemer / FastAPI | Bessemer / Axum |
| ---: | --- | ---: | ---: | ---: | ---: | ---: | ---: |
| 1 | json | 64 | 117,068 | 11,028 | 153,554 | 10.610× (10.508–10.639) | 0.762× (0.757–0.764) |
| 1 | items | 64 | 111,129 | 8,472 | 141,074 | 13.181× (12.474–13.472) | 0.798× (0.723–0.809) |
| 1 | echo | 64 | 38,054 | 6,068 | 36,904 | 6.262× (6.220–6.272) | 1.032× (1.013–1.032) |
| 1 | cpu-inline | 16 | 406 | 392 | 4,558 | 1.035× (0.921–1.041) | 0.089× (0.078–0.089) |
| 1 | cpu-sync | 16 | 404 | 376 | 4,495 | 1.077× (1.061–1.083) | 0.090× (0.089–0.090) |
| 1 | large-echo | 16 | 1,342 | 448 | 1,347 | 2.977× (2.968–3.020) | 0.996× (0.994–1.000) |
| 1 | bytes | 16 | 10,363 | 7,596 | 10,371 | 1.364× (1.347–1.372) | 0.999× (0.998–1.000) |
| 1 | models256 | 64 | 19,834 | 2,462 | 17,655 | 8.041× (8.012–8.101) | 1.123× (1.117–1.157) |
| 4 | json | 64 | 227,960 | 41,649 | 232,544 | 5.480× (5.401–5.579) | 0.999× (0.943–1.108) |
| 4 | items | 64 | 223,547 | 32,292 | 228,640 | 6.941× (5.476–7.057) | 0.978× (0.773–1.005) |
| 4 | echo | 64 | 126,699 | 23,192 | 92,216 | 5.463× (5.231–5.517) | 1.349× (1.337–1.383) |
| 4 | cpu-inline | 16 | 1,574 | 1,566 | 17,841 | 1.009× (1.005–1.037) | 0.088× (0.088–0.091) |
| 4 | cpu-sync | 16 | 1,603 | 1,474 | 15,438 | 1.088× (1.053–1.095) | 0.104× (0.101–0.104) |
| 4 | large-echo | 16 | 5,239 | 1,789 | 4,816 | 2.927× (2.925–2.929) | 1.086× (1.086–1.115) |
| 4 | bytes | 16 | 10,366 | 10,365 | 10,369 | 1.000× (0.998–1.000) | 1.000× (0.997–1.000) |
| 4 | models256 | 64 | 72,132 | 9,681 | 57,560 | 7.451× (7.436–7.458) | 1.254× (1.229–1.255) |

### Free-threaded over standard throughput

| Cores | Route | Bessemer | FastAPI | Axum control |
| ---: | --- | ---: | ---: | ---: |
| 1 | json | 0.949× (0.948–0.954) | 0.978× (0.977–0.985) | 1.029× (0.978–1.036) |
| 1 | items | 0.970× (0.948–1.006) | 0.965× (0.960–0.966) | 0.985× (0.967–1.024) |
| 1 | echo | 0.871× (0.868–0.873) | 0.916× (0.910–0.918) | 0.982× (0.974–0.983) |
| 1 | cpu-inline | 0.905× (0.800–0.918) | 0.905× (0.893–0.909) | 1.000× (0.998–1.000) |
| 1 | cpu-sync | 0.920× (0.909–0.922) | 0.892× (0.891–0.894) | 0.998× (0.998–1.001) |
| 1 | large-echo | 0.809× (0.803–0.810) | 0.921× (0.899–0.941) | 1.000× (0.995–1.000) |
| 1 | bytes | 1.000× (1.000–1.000) | 0.969× (0.952–0.971) | 1.000× (0.999–1.000) |
| 1 | models256 | 0.861× (0.855–0.866) | 0.725× (0.707–0.736) | 0.997× (0.970–1.014) |
| 4 | json | 1.010× (0.998–1.023) | 0.968× (0.966–0.969) | 0.971× (0.867–1.055) |
| 4 | items | 0.996× (0.765–1.004) | 0.959× (0.955–0.959) | 1.000× (0.952–1.052) |
| 4 | echo | 0.895× (0.860–0.903) | 0.902× (0.901–0.904) | 1.041× (1.032–1.053) |
| 4 | cpu-inline | 0.916× (0.889–0.954) | 0.912× (0.907–0.914) | 1.000× (0.998–1.001) |
| 4 | cpu-sync | 1.064× (0.991–1.256) | 0.975× (0.966–0.987) | 0.999× (0.991–1.004) |
| 4 | large-echo | 0.847× (0.811–0.886) | 0.919× (0.914–0.922) | 1.003× (0.985–1.025) |
| 4 | bytes | 1.000× (0.998–1.004) | 1.000× (0.999–1.001) | 1.000× (0.999–1.000) |
| 4 | models256 | 0.876× (0.875–0.882) | 0.710× (0.709–0.711) | 0.946× (0.919–0.984) |

### Free-threaded throughput normalised to the repeated Axum control

Each mode ran Axum on the same server with the same workload. For each corresponding round, divide the free-threaded Python-to-Axum ratio by the standard Python-to-Axum ratio. The result estimates the mode change relative to native baseline drift. Read it alongside the direct mode ratios, especially on high-bandwidth routes.

| Cores | Route | Bessemer | FastAPI |
| ---: | --- | ---: | ---: |
| 1 | json | 0.922× (0.915–0.975) | 0.951× (0.949–1.000) |
| 1 | items | 0.982× (0.980–0.985) | 0.975× (0.943–0.997) |
| 1 | echo | 0.887× (0.883–0.896) | 0.935× (0.925–0.940) |
| 1 | cpu-inline | 0.905× (0.801–0.918) | 0.905× (0.895–0.909) |
| 1 | cpu-sync | 0.919× (0.910–0.924) | 0.893× (0.890–0.896) |
| 1 | large-echo | 0.809× (0.803–0.814) | 0.921× (0.904–0.941) |
| 1 | bytes | 1.000× (0.999–1.000) | 0.970× (0.952–0.970) |
| 1 | models256 | 0.864× (0.843–0.893) | 0.726× (0.709–0.748) |
| 4 | json | 1.028× (0.969–1.165) | 0.997× (0.916–1.117) |
| 4 | items | 1.004× (0.728–1.046) | 0.959× (0.912–1.003) |
| 4 | echo | 0.867× (0.816–0.867) | 0.866× (0.856–0.876) |
| 4 | cpu-inline | 0.916× (0.891–0.954) | 0.912× (0.908–0.914) |
| 4 | cpu-sync | 1.060× (1.000–1.257) | 0.976× (0.962–0.996) |
| 4 | large-echo | 0.860× (0.791–0.884) | 0.911× (0.900–0.933) |
| 4 | bytes | 1.001× (0.998–1.005) | 1.000× (1.000–1.001) |
| 4 | models256 | 0.932× (0.890–0.952) | 0.750× (0.722–0.774) |

### Throughput scaling from one core

Each ratio matches the same runtime, route and numbered round. Python workers and Tokio worker threads scale with the number of pinned guest cores.

| Python | Cores | Route | Bessemer | FastAPI | Axum |
| --- | ---: | --- | ---: | ---: | ---: |
| standard | 4 | json | 1.828× (1.792–1.885) | 3.816× (3.783–3.840) | 1.590× (1.441–1.608) |
| standard | 4 | items | 1.933× (1.914–2.188) | 3.822× (3.822–3.857) | 1.623× (1.516–1.629) |
| standard | 4 | echo | 3.245× (3.232–3.254) | 3.869× (3.863–3.879) | 2.359× (2.261–2.425) |
| standard | 4 | cpu-inline | 3.869× (3.830–3.942) | 3.968× (3.963–3.981) | 3.914× (3.913–3.917) |
| standard | 4 | cpu-sync | 3.437× (2.810–3.679) | 3.590× (3.542–3.621) | 3.430× (3.429–3.444) |
| standard | 4 | large-echo | 3.751× (3.547–3.876) | 4.035× (3.906–4.055) | 3.534× (3.501–3.567) |
| standard | 4 | bytes | 1.000× (0.996–1.000) | 1.307× (1.306–1.325) | 1.000× (1.000–1.001) |
| standard | 4 | models256 | 3.574× (3.547–3.592) | 4.048× (3.911–4.061) | 3.440× (3.421–3.526) |
| free-threaded | 4 | json | 1.948× (1.921–1.983) | 3.772× (3.739–3.781) | 1.517× (1.330–1.554) |
| free-threaded | 4 | items | 2.000× (1.665–2.012) | 3.819× (3.793–3.820) | 1.597× (1.557–1.655) |
| free-threaded | 4 | echo | 3.326× (3.224–3.352) | 3.812× (3.811–3.834) | 2.501× (2.422–2.568) |
| free-threaded | 4 | cpu-inline | 3.871× (3.860–4.571) | 3.997× (3.973–4.059) | 3.915× (3.906–3.926) |
| free-threaded | 4 | cpu-sync | 3.964× (3.886–3.965) | 3.919× (3.915–3.923) | 3.434× (3.411–3.450) |
| free-threaded | 4 | large-echo | 3.912× (3.883–3.925) | 3.990× (3.938–4.039) | 3.575× (3.498–3.587) |
| free-threaded | 4 | bytes | 1.000× (0.999–1.000) | 1.365× (1.347–1.372) | 1.000× (1.000–1.000) |
| free-threaded | 4 | models256 | 3.641× (3.632–3.650) | 3.932× (3.917–3.958) | 3.320× (3.250–3.357) |

### Large HTTP payloads

The large echo request contained 69,403 bytes (SHA-256 `4bb4a57404b9fd07a74eaa6cc13e930a4f9b6bc965f252599f07a951c1185ffc`).
The bytes endpoint has no request body. MiB/s counts successful HTTP response bodies, with HTTP headers and request bytes excluded. Private network bandwidth can limit these rates.

| Python | Cores | Route | Framework | Response bytes per reply | Response MiB/s |
| --- | ---: | --- | --- | ---: | ---: |
| standard | 1 | large-echo | bessemer-auto | 60,403 | 95.9 |
| standard | 1 | large-echo | fastapi-standard | 60,403 | 27.8 |
| standard | 1 | large-echo | axum | 60,403 | 77.6 |
| standard | 1 | bytes | bessemer-auto | 262,144 | 2,590.9 |
| standard | 1 | bytes | fastapi-standard | 262,144 | 1,982.8 |
| standard | 1 | bytes | axum | 262,144 | 2,593.1 |
| standard | 4 | large-echo | bessemer-auto | 60,403 | 356.2 |
| standard | 4 | large-echo | fastapi-standard | 60,403 | 112.2 |
| standard | 4 | large-echo | axum | 60,403 | 274.8 |
| standard | 4 | bytes | bessemer-auto | 262,144 | 2,591.3 |
| standard | 4 | bytes | fastapi-standard | 262,144 | 2,591.5 |
| standard | 4 | bytes | axum | 262,144 | 2,593.4 |
| free-threaded | 1 | large-echo | bessemer-auto | 60,403 | 77.3 |
| free-threaded | 1 | large-echo | fastapi-standard | 60,403 | 25.8 |
| free-threaded | 1 | large-echo | axum | 60,403 | 77.6 |
| free-threaded | 1 | bytes | bessemer-auto | 262,144 | 2,590.7 |
| free-threaded | 1 | bytes | fastapi-standard | 262,144 | 1,898.9 |
| free-threaded | 1 | bytes | axum | 262,144 | 2,592.7 |
| free-threaded | 4 | large-echo | bessemer-auto | 60,403 | 301.8 |
| free-threaded | 4 | large-echo | fastapi-standard | 60,403 | 103.1 |
| free-threaded | 4 | large-echo | axum | 60,403 | 277.4 |
| free-threaded | 4 | bytes | bessemer-auto | 262,144 | 2,591.6 |
| free-threaded | 4 | bytes | fastapi-standard | 262,144 | 2,591.2 |
| free-threaded | 4 | bytes | axum | 262,144 | 2,592.1 |

### standard latency and CPU

| Cores | Route | Framework | p50 ms | p99 ms | Server CPU | Client CPU | CPU s / million |
| ---: | --- | --- | ---: | ---: | ---: | ---: | ---: |
| 1 | json | bessemer-auto | 0.508 | 0.739 | 95.4% | 36.5% | 7.73 |
| 1 | json | fastapi-standard | 5.357 | 10.805 | 99.4% | 5.6% | 88.02 |
| 1 | json | axum | 0.424 | 0.614 | 93.6% | 43.3% | 6.27 |
| 1 | items | bessemer-auto | 0.536 | 0.768 | 95.4% | 35.1% | 8.16 |
| 1 | items | fastapi-standard | 6.926 | 13.940 | 99.4% | 4.4% | 112.80 |
| 1 | items | axum | 0.440 | 0.648 | 94.7% | 43.2% | 6.60 |
| 1 | echo | bessemer-auto | 1.461 | 1.774 | 98.0% | 19.9% | 22.51 |
| 1 | echo | fastapi-standard | 9.292 | 18.663 | 99.4% | 3.9% | 149.69 |
| 1 | echo | axum | 1.497 | 2.993 | 98.2% | 18.3% | 25.98 |
| 1 | cpu-inline | bessemer-auto | 35.961 | 36.205 | 99.6% | 0.5% | 2240.52 |
| 1 | cpu-inline | fastapi-standard | 34.645 | 69.455 | 99.6% | 0.5% | 2303.24 |
| 1 | cpu-inline | axum | 3.734 | 4.038 | 99.4% | 2.8% | 218.23 |
| 1 | cpu-sync | bessemer-auto | 34.213 | 77.333 | 99.6% | 0.4% | 2265.86 |
| 1 | cpu-sync | fastapi-standard | 36.603 | 77.777 | 99.6% | 0.6% | 2366.79 |
| 1 | cpu-sync | axum | 3.538 | 3.743 | 99.6% | 2.6% | 221.18 |
| 1 | large-echo | bessemer-auto | 9.587 | 11.510 | 99.6% | 2.4% | 598.47 |
| 1 | large-echo | fastapi-standard | 29.277 | 74.033 | 99.6% | 0.9% | 2065.07 |
| 1 | large-echo | axum | 11.869 | 13.431 | 99.6% | 1.8% | 739.09 |
| 1 | bytes | bessemer-auto | 1.641 | 2.298 | 62.7% | 27.5% | 60.47 |
| 1 | bytes | fastapi-standard | 1.571 | 3.248 | 99.2% | 16.0% | 125.08 |
| 1 | bytes | axum | 1.415 | 2.694 | 51.3% | 29.8% | 49.49 |
| 1 | models256 | bessemer-auto | 2.785 | 3.128 | 98.6% | 10.2% | 43.05 |
| 1 | models256 | fastapi-standard | 18.456 | 36.977 | 99.6% | 2.2% | 295.10 |
| 1 | models256 | axum | 3.697 | 3.895 | 99.2% | 9.9% | 56.02 |
| 4 | json | bessemer-auto | 0.263 | 0.559 | 71.4% | 56.5% | 12.65 |
| 4 | json | fastapi-standard | 1.239 | 2.624 | 98.0% | 22.6% | 91.03 |
| 4 | json | axum | 0.252 | 0.513 | 70.2% | 57.2% | 11.89 |
| 4 | items | bessemer-auto | 0.261 | 0.568 | 74.7% | 56.6% | 13.11 |
| 4 | items | fastapi-standard | 1.629 | 3.448 | 98.4% | 17.1% | 116.76 |
| 4 | items | axum | 0.254 | 0.544 | 71.1% | 55.2% | 12.20 |
| 4 | echo | bessemer-auto | 0.435 | 0.785 | 93.7% | 54.4% | 26.51 |
| 4 | echo | fastapi-standard | 2.173 | 4.478 | 98.5% | 18.5% | 153.70 |
| 4 | echo | axum | 0.707 | 1.289 | 91.5% | 39.8% | 41.32 |
| 4 | cpu-inline | bessemer-auto | 8.901 | 17.975 | 96.1% | 1.3% | 2248.59 |
| 4 | cpu-inline | fastapi-standard | 9.244 | 23.238 | 99.5% | 1.4% | 2317.33 |
| 4 | cpu-inline | axum | 0.897 | 1.352 | 98.7% | 12.1% | 221.25 |
| 4 | cpu-sync | bessemer-auto | 9.348 | 27.474 | 86.2% | 1.0% | 2281.10 |
| 4 | cpu-sync | fastapi-standard | 9.702 | 27.398 | 92.2% | 1.5% | 2440.04 |
| 4 | cpu-sync | axum | 0.953 | 2.621 | 88.9% | 9.5% | 230.08 |
| 4 | large-echo | bessemer-auto | 2.336 | 4.237 | 94.7% | 8.6% | 612.68 |
| 4 | large-echo | fastapi-standard | 7.505 | 27.221 | 99.4% | 2.5% | 2041.21 |
| 4 | large-echo | axum | 3.295 | 5.191 | 97.1% | 5.7% | 814.33 |
| 4 | bytes | bessemer-auto | 1.283 | 3.141 | 21.6% | 32.1% | 83.34 |
| 4 | bytes | fastapi-standard | 1.177 | 2.894 | 40.7% | 30.4% | 157.02 |
| 4 | bytes | axum | 1.477 | 2.923 | 18.8% | 28.1% | 72.49 |
| 4 | models256 | bessemer-auto | 0.769 | 1.240 | 96.1% | 38.5% | 46.73 |
| 4 | models256 | fastapi-standard | 4.355 | 8.850 | 99.2% | 8.6% | 291.34 |
| 4 | models256 | axum | 1.047 | 1.715 | 96.8% | 29.4% | 63.66 |

### free-threaded latency and CPU

| Cores | Route | Framework | p50 ms | p99 ms | Server CPU | Client CPU | CPU s / million |
| ---: | --- | --- | ---: | ---: | ---: | ---: | ---: |
| 1 | json | bessemer-auto | 0.536 | 0.771 | 95.4% | 35.1% | 8.15 |
| 1 | json | fastapi-standard | 5.462 | 11.039 | 99.2% | 6.0% | 90.05 |
| 1 | json | axum | 0.408 | 0.629 | 94.1% | 44.8% | 6.13 |
| 1 | items | bessemer-auto | 0.559 | 0.821 | 95.4% | 34.1% | 8.57 |
| 1 | items | fastapi-standard | 7.169 | 14.464 | 99.4% | 4.9% | 117.33 |
| 1 | items | axum | 0.442 | 0.702 | 94.2% | 45.1% | 6.67 |
| 1 | echo | bessemer-auto | 1.675 | 1.953 | 98.2% | 18.0% | 25.81 |
| 1 | echo | fastapi-standard | 10.171 | 20.424 | 99.4% | 4.1% | 163.82 |
| 1 | echo | axum | 1.514 | 3.063 | 98.2% | 20.6% | 26.56 |
| 1 | cpu-inline | bessemer-auto | 39.320 | 39.646 | 99.6% | 0.3% | 2450.56 |
| 1 | cpu-inline | fastapi-standard | 38.251 | 76.759 | 99.6% | 0.7% | 2542.40 |
| 1 | cpu-inline | axum | 3.732 | 4.042 | 99.4% | 2.7% | 218.07 |
| 1 | cpu-sync | bessemer-auto | 39.518 | 39.805 | 99.6% | 0.5% | 2462.55 |
| 1 | cpu-sync | fastapi-standard | 47.653 | 68.900 | 99.8% | 0.4% | 2651.16 |
| 1 | cpu-sync | axum | 3.536 | 3.754 | 99.4% | 2.7% | 221.12 |
| 1 | large-echo | bessemer-auto | 11.886 | 15.457 | 99.6% | 2.0% | 742.32 |
| 1 | large-echo | fastapi-standard | 33.419 | 67.053 | 99.6% | 0.8% | 2221.59 |
| 1 | large-echo | axum | 11.900 | 13.427 | 99.6% | 1.7% | 740.73 |
| 1 | bytes | bessemer-auto | 1.602 | 2.499 | 64.6% | 29.9% | 62.33 |
| 1 | bytes | fastapi-standard | 1.636 | 3.422 | 99.4% | 14.3% | 130.87 |
| 1 | bytes | axum | 1.654 | 2.610 | 56.5% | 27.4% | 54.46 |
| 1 | models256 | bessemer-auto | 3.216 | 3.625 | 98.8% | 8.5% | 49.91 |
| 1 | models256 | fastapi-standard | 25.497 | 51.051 | 99.6% | 1.7% | 404.58 |
| 1 | models256 | axum | 3.707 | 3.990 | 99.0% | 10.5% | 55.99 |
| 4 | json | bessemer-auto | 0.262 | 0.544 | 74.9% | 55.5% | 13.00 |
| 4 | json | fastapi-standard | 1.282 | 2.688 | 97.9% | 20.6% | 94.01 |
| 4 | json | axum | 0.254 | 0.571 | 69.0% | 56.2% | 12.10 |
| 4 | items | bessemer-auto | 0.267 | 0.549 | 74.8% | 55.4% | 13.47 |
| 4 | items | fastapi-standard | 1.713 | 3.649 | 98.4% | 16.8% | 122.01 |
| 4 | items | axum | 0.258 | 0.575 | 71.5% | 56.0% | 12.48 |
| 4 | echo | bessemer-auto | 0.484 | 0.852 | 93.6% | 51.0% | 29.57 |
| 4 | echo | fastapi-standard | 2.430 | 5.121 | 98.7% | 15.4% | 170.23 |
| 4 | echo | axum | 0.687 | 1.187 | 91.6% | 39.5% | 39.73 |
| 4 | cpu-inline | bessemer-auto | 8.295 | 19.796 | 96.5% | 1.1% | 2449.81 |
| 4 | cpu-inline | fastapi-standard | 10.147 | 25.577 | 99.5% | 1.2% | 2542.73 |
| 4 | cpu-inline | axum | 0.899 | 1.350 | 98.9% | 10.1% | 221.67 |
| 4 | cpu-sync | bessemer-auto | 9.943 | 18.079 | 99.3% | 0.7% | 2477.17 |
| 4 | cpu-sync | fastapi-standard | 10.913 | 17.463 | 99.5% | 1.7% | 2700.15 |
| 4 | cpu-sync | axum | 0.953 | 2.608 | 88.9% | 10.4% | 230.34 |
| 4 | large-echo | bessemer-auto | 2.852 | 4.725 | 99.2% | 6.6% | 756.60 |
| 4 | large-echo | fastapi-standard | 8.739 | 17.819 | 99.6% | 2.5% | 2226.91 |
| 4 | large-echo | axum | 3.287 | 5.211 | 97.5% | 6.1% | 809.79 |
| 4 | bytes | bessemer-auto | 1.286 | 3.307 | 22.7% | 32.9% | 87.78 |
| 4 | bytes | fastapi-standard | 1.448 | 2.637 | 42.0% | 31.8% | 162.12 |
| 4 | bytes | axum | 1.420 | 3.147 | 19.3% | 28.3% | 74.46 |
| 4 | models256 | bessemer-auto | 0.844 | 1.422 | 96.7% | 33.6% | 53.57 |
| 4 | models256 | fastapi-standard | 6.181 | 12.494 | 99.3% | 6.3% | 410.29 |
| 4 | models256 | axum | 1.106 | 1.813 | 95.9% | 29.1% | 66.30 |

## Reproduce the run

Both runtime passes ran in `us-central1-a` using a `c3-standard-8` server with one thread per core and an `e2-highcpu-16` load generator. Both VMs used Ubuntu 24.04 x86_64. The client reached the server over the same-zone private IPv4 network. Verified teardown found zero benchmark instances, disks, firewall rules, subnets or VPCs.

| Runtime | Stage | Qualified / recorded |
| --- | --- | ---: |
| standard | smoke | 24/24 |
| standard | measured | 144/144 |
| free-threaded | smoke | 24/24 |
| free-threaded | measured | 144/144 |

[Sample measurements](samples.csv) include every smoke and measured observation,
including excluded windows. [The summary](summary.json) provides the aggregate
comparisons. [Provenance](provenance.json) records artifact hashes, package
versions, fixture hashes, the benchmark phases and the cleanup receipt. The CSV
retains HTTP status counts, transport error counts, body digests and runtime
probe flags without the operational connection identifiers.

Check out commit `687e70739416d0cca7b4c64de6d4272edf03b101` and apply the
[benchmark patch](benchmark-source.patch). Give the runner two separate
Ubuntu 24.04 x86_64 hosts with SSH
aliases and private IP connectivity. The launcher installs the hash-pinned
PyPI wheels, checks the GIL state after importing the installed packages,
and builds the native fixture with the locked Rust dependencies.

Run these commands from the patched checkout with the results directory
and private benchmark hosts set for your environment.

```bash
git apply "$REPORT_DIR/benchmark-source.patch"
for variant in standard free-threaded; do
  python_build=3.14.7
  if [[ "$variant" == free-threaded ]]; then
    python_build=3.14.7t
  fi
  PYTHON_VERSION="$python_build" RUST_VERSION=1.92.0 \
    benchmarks/compare/remote/run_two_machines.sh \
      "$BENCH_SERVER" "$BENCH_CLIENT" "$BENCH_SERVER_PRIVATE_IP" "$RESULTS/$variant" \
      --configs 1-core,4-core --connections 64 --rounds 3 --warmup 2 --duration 5 \
      --routes json,items,echo,cpu-inline,cpu-sync,large-echo,bytes,models256 \
      --route-connections cpu-inline=16,cpu-sync=16,large-echo=16,bytes=16
done
python3 "$REPORT_DIR/analyse_314.py" --repository "$PWD" \
  --standard "$RESULTS/standard" --free-threaded "$RESULTS/free-threaded" \
  --output "$RESULTS/summary-qualified.json" > "$RESULTS/report.md"
```
