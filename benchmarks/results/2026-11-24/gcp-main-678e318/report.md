# Bessemer on GCP with CPython 3.14.7 and 3.14.7t

## Findings

- **Free-threaded typed request throughput improved.** On four vCPUs, `main` served 138,146
  replies/s for 20-item typed echo and 5,703 replies/s for 1,000-item echo. The paired gains
  over RC10 were 6.3% and 9.8%, respectively, with gains in all three rounds. On one vCPU,
  the gains were 6.6% and 14.7%. Four-vCPU p99 was nearly flat for 20-item echo (0.702 to
  0.709 ms) and increased for 1,000-item echo (4.654 to 5.386 ms). On one vCPU, p99 fell
  from 2.199 to 2.074 ms for 20 items and from 14.989 to 13.261 ms for 1,000 items.
- **Standard Python still served the typed workloads faster in absolute terms.** On four
  vCPUs, free-threaded `main` reached 93.9% of standard-Python throughput on 20-item echo
  and 87.6% on 1,000-item echo. The same-run RC10 figures were 89.5% and 82.7%. On standard
  Python, `main` ran 1,000-item echo 4.8% faster than RC10 at the paired median, while p99
  increased from 4.391 to 4.668 ms.
- **The 256-model response changed by about 1% or less on four vCPUs.** Relative to RC10,
  standard Python throughput fell 1.2% and p99 fell from 1.058 to 0.951 ms. Free-threaded
  throughput rose 0.35% and p99 rose from 1.140 to 1.294 ms. The free-threaded model
  response still reached 72,961 replies/s, 7.56× FastAPI and 1.26× Axum at the paired
  median.
- **The throughput lead depended on the workload.** On one vCPU, `main` reached 11.03×
  FastAPI for small JSON and 13.24× for path/query on standard Python, and 10.63× and 13.34×
  on free-threaded Python. At four vCPUs, those ratios were 5.83–7.67× across the two Python
  modes. Standard-Python `main` served 251,045 small JSON replies/s, close to Axum's
  248,065. The four-vCPU typed echo, large echo and model fixtures ran 1.24–1.70× Axum
  across the two modes.
- **The CPU checksum and binary download hit different limits.** Inline Python checksum
  throughput stayed close to FastAPI. Axum ran the same checksum in compiled Rust and
  delivered roughly 10–11.5× the four-vCPU inline throughput. The four-vCPU sync route
  varied widely across rounds, with `main`/RC10 ranges of 0.767–1.205× on standard Python
  and 0.779–1.366× on free-threaded Python. All eight four-vCPU binary configurations
  delivered about 21.75 Gbit/s of response bodies.

## Method and qualification

The study ran on 23–24 November 2026, in GCP `us-central1-a`. A dedicated Ubuntu 24.04
x86_64 `c3-standard-8` server used one thread per core and exposed four guest vCPUs on an
Intel Xeon Platinum 8481C. The `e2-highcpu-16` load generator exposed 16 guest vCPUs on an
AMD EPYC 7B12. Both batches used the same private same-zone network and VM pair. Teardown
verification found zero benchmark instances, disks, firewall rules, subnets or VPCs.

- **Bessemer** used release-mode Linux wheels built on the server from
  `678e3184f6280eae287a5e19616f7747e9ef20f1` for `main` and
  `687e70739416d0cca7b4c64de6d4272edf03b101` for RC10, with Rust 1.92.0 and maturin 1.16.0.
  Both wheels name their package version `1.0.0rc10`. The commit, wheel SHA-256 and live
  native-extension SHA-256 identify each build. The same toolchain and recorded dependency
  versions apply to both Bessemer roles within each Python mode.
- **FastAPI** 0.165.0 used Uvicorn 0.54.0, uvloop 0.23.0 and httptools 0.9.0. Both Python
  frameworks used Pydantic 2.14.4 and pydantic-core 2.50.5. The same locked Axum 0.8.9
  binary served both batches. Its executable SHA-256 matched in both build receipts.
- **Load** used oha 1.16.0 and HTTP/1.1 keep-alive on a private IPv4 path. Each measured
  sample had 2 seconds of warmup and 5 seconds of timed load, followed by request drain. The
  four arms rotated order across three rounds within each CPU configuration. Both Python
  frameworks used one worker per pinned vCPU. Axum used one Tokio worker thread per pinned
  vCPU. The two Python modes ran in separate batches. Each mode started with a smoke
  round on one vCPU at four connections, with 1 second of warmup and 3 seconds of
  timed load per sample.
- **Qualification** required complete body probes before and after each sample and aggregate
  response bytes matching the successful reply count. Workers confirmed the GIL was enabled
  on standard Python and disabled on free-threaded Python before and after load, and both
  Bessemer arms verified their native extension hash. Warmup and timed traffic had zero
  non-200 responses and zero transport errors. The client CPU threshold was 85%. Measured
  sample maxima were 59.9% on standard Python and 58.9% on free-threaded Python.

| Python mode | Stage | Qualified / recorded | Timed HTTP 200 | Warmup HTTP 200 |
| --- | --- | ---: | ---: | ---: |
| Standard 3.14.7 | Smoke | 32/32 | 931,604 | 299,590 |
| Standard 3.14.7 | Measured | 192/192 | 50,432,618 | 20,135,724 |
| Free-threaded 3.14.7t | Smoke | 32/32 | 893,828 | 287,410 |
| Free-threaded 3.14.7t | Measured | 192/192 | 49,478,182 | 19,627,864 |

All 384 measured samples qualified. The measured batches served 99,910,800 successful
replies. Bessemer `main` delivered 60,346 standard-Python and 57,640 free-threaded timed
CPU-checksum replies across core configurations, with zero 503s or transport errors.

| Route | Fixture | Connections |
| --- | --- | ---: |
| `/json` | Small JSON response | 64 |
| `/items/7?q=abc` | Integer path and string query parameters | 64 |
| `/echo` | 20-item typed JSON request and response | 64 |
| `/cpu-inline` | 50,000-iteration integer checksum in an async handler | 16 |
| `/cpu-sync` | The same checksum using the stack's sync dispatch or Tokio `spawn_blocking` | 16 |
| `/echo` (large) | 1,000-item typed JSON, 69,403-byte request and 60,403-byte response | 16 |
| `/bytes` | Immutable 262,144-byte response | 16 |
| `/models256` | 256-item typed JSON response, 14,885 bytes | 64 |

## Throughput and p99 latency

Each framework column shows median successful replies per second and median round p99 in
milliseconds. Throughput uses elapsed time including request drain. Each ratio is the median
of three same-round ratios, followed by their observed minimum and maximum.

### Standard CPython 3.14.7

| vCPUs | Workload | Bessemer main | Bessemer RC10 | FastAPI | Rust Axum | Main / RC10 | Main / FastAPI | Main / Axum |
| ---: | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 1 | json | 123,922 / 0.660 | 120,536 / 0.677 | 11,302 / 10.876 | 152,688 / 0.526 | 1.028× (1.016–1.028) | 11.034× (10.838–11.049) | 0.817× (0.795–0.821) |
| 1 | items | 116,655 / 0.699 | 119,348 / 0.687 | 8,773 / 14.127 | 146,889 / 0.556 | 0.977× (0.947–1.029) | 13.239× (13.143–13.650) | 0.808× (0.794–0.819) |
| 1 | echo | 44,577 / 1.898 | 43,956 / 1.942 | 6,694 / 18.544 | 37,829 / 3.025 | 1.018× (1.008–1.026) | 6.699× (6.621–6.721) | 1.186× (1.174–1.193) |
| 1 | cpu-inline | 446 / 35.978 | 445 / 36.142 | 433 / 69.406 | 4,558 / 3.947 | 1.005× (1.004–1.008) | 1.032× (1.024–1.038) | 0.098× (0.097–0.098) |
| 1 | cpu-sync | 440 / 77.095 | 439 / 77.179 | 421 / 84.711 | 4,491 / 3.677 | 1.001× (0.992–1.012) | 1.044× (1.038–1.044) | 0.098× (0.097–0.098) |
| 1 | large-echo | 1,720 / 11.901 | 1,675 / 12.884 | 493 / 73.609 | 1,348 / 12.798 | 1.032× (1.024–1.037) | 3.488× (3.487–3.513) | 1.282× (1.271–1.289) |
| 1 | bytes | 10,372 / 2.663 | 10,370 / 2.143 | 7,847 / 3.386 | 10,378 / 2.677 | 1.000× (1.000–1.000) | 1.322× (1.314–1.322) | 0.999× (0.999–0.999) |
| 1 | models256 | 23,443 / 3.098 | 23,946 / 3.238 | 3,500 / 35.899 | 17,748 / 3.787 | 0.979× (0.977–0.979) | 6.727× (6.684–6.808) | 1.321× (1.316–1.340) |
| 4 | json | 251,045 / 0.455 | 249,755 / 0.455 | 43,090 / 2.683 | 248,065 / 0.514 | 1.005× (0.961–1.123) | 5.831× (5.575–5.842) | 1.008× (0.997–1.013) |
| 4 | items | 245,812 / 0.467 | 245,155 / 0.456 | 33,653 / 3.407 | 251,173 / 0.464 | 1.008× (0.967–1.090) | 7.318× (7.245–7.343) | 0.979× (0.955–1.039) |
| 4 | echo | 146,892 / 0.679 | 145,246 / 0.674 | 25,564 / 4.541 | 86,202 / 1.311 | 1.006× (0.999–1.035) | 5.746× (5.725–5.754) | 1.704× (1.480–1.799) |
| 4 | cpu-inline | 1,771 / 18.105 | 1,737 / 17.958 | 1,719 / 14.165 | 17,869 / 1.352 | 1.016× (0.972–1.020) | 1.032× (0.981–1.032) | 0.099× (0.094–0.099) |
| 4 | cpu-sync | 1,435 / 34.280 | 1,248 / 34.462 | 1,468 / 32.285 | 15,565 / 2.690 | 1.158× (0.767–1.205) | 0.974× (0.839–0.978) | 0.092× (0.077–0.096) |
| 4 | large-echo | 6,519 / 4.668 | 6,249 / 4.391 | 1,944 / 27.476 | 4,713 / 5.391 | 1.048× (1.034–1.075) | 3.357× (3.329–3.369) | 1.383× (1.364–1.420) |
| 4 | bytes | 10,373 / 3.015 | 10,373 / 3.238 | 10,369 / 3.520 | 10,376 / 3.031 | 1.000× (1.000–1.000) | 1.000× (1.000–1.003) | 1.000× (0.999–1.000) |
| 4 | models256 | 84,650 / 0.951 | 85,691 / 1.058 | 13,632 / 8.895 | 58,407 / 1.809 | 0.988× (0.985–0.990) | 6.208× (6.190–6.256) | 1.449× (1.374–1.472) |

### Free-threaded CPython 3.14.7t

| vCPUs | Workload | Bessemer main | Bessemer RC10 | FastAPI | Rust Axum | Main / RC10 | Main / FastAPI | Main / Axum |
| ---: | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 1 | json | 117,948 / 0.698 | 116,177 / 0.705 | 11,040 / 11.107 | 155,039 / 0.526 | 0.995× (0.981–1.047) | 10.631× (10.493–10.803) | 0.759× (0.746–0.776) |
| 1 | items | 112,777 / 0.732 | 113,387 / 0.727 | 8,489 / 14.597 | 143,249 / 0.555 | 0.995× (0.977–1.027) | 13.336× (13.107–13.375) | 0.790× (0.769–0.798) |
| 1 | echo | 41,236 / 2.074 | 38,384 / 2.199 | 6,061 / 20.463 | 37,831 / 3.025 | 1.066× (1.063–1.091) | 6.767× (6.702–6.926) | 1.090× (1.076–1.099) |
| 1 | cpu-inline | 408 / 39.422 | 407 / 39.560 | 393 / 76.569 | 4,568 / 3.937 | 1.001× (0.874–1.004) | 1.038× (0.918–1.040) | 0.089× (0.078–0.090) |
| 1 | cpu-sync | 405 / 39.822 | 405 / 39.706 | 375 / 68.281 | 4,503 / 3.687 | 0.999× (0.996–1.001) | 1.078× (1.077–1.079) | 0.090× (0.090–0.090) |
| 1 | large-echo | 1,522 / 13.261 | 1,329 / 14.989 | 451 / 66.686 | 1,348 / 12.714 | 1.147× (1.139–1.148) | 3.380× (3.363–3.442) | 1.129× (1.122–1.133) |
| 1 | bytes | 10,372 / 2.126 | 10,372 / 2.601 | 7,685 / 3.439 | 10,377 / 2.744 | 1.000× (0.999–1.000) | 1.350× (1.345–1.373) | 1.000× (0.999–1.000) |
| 1 | models256 | 19,643 / 3.888 | 19,889 / 3.613 | 2,451 / 51.299 | 17,728 / 3.794 | 0.988× (0.984–0.996) | 8.009× (7.996–8.197) | 1.108× (1.102–1.121) |
| 4 | json | 250,767 / 0.445 | 248,778 / 0.486 | 41,702 / 2.833 | 259,249 / 0.435 | 1.017× (1.007–1.021) | 6.013× (6.009–6.093) | 0.967× (0.965–0.992) |
| 4 | items | 247,247 / 0.456 | 247,711 / 0.447 | 32,225 / 3.575 | 254,943 / 0.452 | 0.999× (0.993–1.011) | 7.673× (7.617–7.784) | 0.967× (0.965–0.983) |
| 4 | echo | 138,146 / 0.709 | 129,975 / 0.702 | 23,080 / 5.104 | 90,639 / 1.231 | 1.063× (1.061–1.067) | 5.981× (5.977–6.012) | 1.524× (1.436–1.609) |
| 4 | cpu-inline | 1,559 / 19.815 | 1,552 / 29.555 | 1,566 / 15.452 | 17,856 / 1.351 | 1.003× (0.980–1.028) | 0.996× (0.994–1.016) | 0.087× (0.087–0.089) |
| 4 | cpu-sync | 1,564 / 18.130 | 1,216 / 20.607 | 1,471 / 17.278 | 15,560 / 2.673 | 1.286× (0.779–1.366) | 1.063× (0.826–1.093) | 0.101× (0.078–0.103) |
| 4 | large-echo | 5,703 / 5.386 | 5,194 / 4.654 | 1,783 / 13.764 | 4,671 / 5.504 | 1.098× (1.039–1.114) | 3.199× (3.030–3.247) | 1.237× (1.137–1.239) |
| 4 | bytes | 10,373 / 3.146 | 10,372 / 2.990 | 10,373 / 2.912 | 10,378 / 3.341 | 1.000× (1.000–1.003) | 1.000× (1.000–1.000) | 1.000× (0.999–1.000) |
| 4 | models256 | 72,961 / 1.294 | 72,807 / 1.140 | 9,655 / 12.509 | 57,890 / 1.818 | 1.003× (1.002–1.006) | 7.556× (7.541–7.596) | 1.260× (1.194–1.399) |

## Scaling and CPU use

For `main`, moving from one pinned vCPU and worker to four scaled inline CPU throughput by
3.94× on standard Python and 3.82× on free-threaded Python. Sync checksum scaled 3.26× and
3.86× at the paired median. The corresponding gains were 3.28× and 3.35× for typed echo,
3.78× and 3.75× for 1,000-item echo, and 3.62× and 3.72× for 256-model responses. Small JSON
scaled about 2× in both Python modes. These comparisons include the change in the number of
worker processes.

On four vCPUs, estimated server CPU time for a million 20-item echo replies was 25.73
seconds for standard-Python `main`, 154.46 for FastAPI and 42.50 for Axum. Free-threaded
estimates were 27.49, 171.58 and 40.47 seconds respectively. For 256 models the
standard-Python estimates were 45.55, 291.37 and 66.29 seconds. The model response uses
typed serializers in Python and a prebuilt typed collection serialized for each response in
Axum. These are fixture-level guest CPU estimates.

Bessemer and Axum already reached the binary download rate with one vCPU. On four vCPUs,
Bessemer `main` used 20.1% of server CPU on standard Python and 20.6% on free-threaded
Python, compared with 40.1% for FastAPI in both modes. The shared throughput plateau and CPU
headroom are consistent with a path bandwidth limit. The rates count response-body bytes and
exclude HTTP headers and other network overhead.

## Scope

These are short, closed-loop measurements for eight synthetic HTTP/1.1 fixtures on one VM
pair. The sync CPU results varied enough across rounds to leave the direction of any change
uncertain. Latency percentiles represent the tested connection counts, and the ratio ranges
show the three observed rounds. In the scaling tests, the worker count changed with the
number of vCPUs. Axum's native checksum reflects a different application
runtime. The results describe this test setup and leave production capacity, ingress and
sustained mixed traffic unmeasured.

The [prior GCP run](../../2026-11-23/rc10-gcp-python314/report.md) used the published RC10
wheels. It provides historical context. Every main/RC10 speed ratio here uses the
same-toolchain RC10 build tested alongside `main`.

## Reproduce the published results

[Samples](samples.csv) contain the individual measured and smoke observations.
[Summary](summary.json) contains paired comparisons, latency, scaling and CPU estimates.
[Provenance](provenance.json) records the host setup, build hashes, source and fixture
digests, and teardown verification. The public samples use labels for hosts and request
targets and a presence flag for worker PIDs. The export retains runtime identity and
GIL flags, HTTP counts, transport errors, timing and body digests.

From the publication checkout, create a detached test checkout and apply the
[benchmark patch](benchmark-source.patch). The [analyzer](analyse_main.py) verifies the
fixtures and published build receipts, qualifies the 448 observations and regenerates the
summary and benchmark tables.

~~~bash
report_dir="$PWD/benchmarks/results/2026-11-24/gcp-main-678e318"
run_dir="$(mktemp -d)"

git worktree add --detach "$run_dir/source" 678e3184f6280eae287a5e19616f7747e9ef20f1
cd "$run_dir/source"
git apply "$report_dir/benchmark-source.patch"

python3 "$report_dir/analyse_main.py" --repository "$PWD" --results "$report_dir" \
  --output "$run_dir/replayed-summary.json" > "$run_dir/replayed-report.md"
~~~

## Run the benchmarks again

Use two separate Ubuntu 24.04 x86_64 hosts with non-interactive SSH and sudo, private IPv4
connectivity, and package download access. Set `BENCH_SERVER` and `BENCH_CLIENT` to their
SSH aliases and `BENCH_SERVER_PRIVATE_IP` to the server's private address. Allow the client
to reach the controller and dynamic application ports on the server through the private
network. For equivalent CPU settings, use the machine types and thread settings listed
above.

Continue from the patched checkout. The launcher builds both Bessemer wheels and Axum,
runs each workload and copies back the raw observations, build receipts and per-batch
summaries.

~~~bash
launcher=benchmarks/compare/remote/run_two_machines.sh
frameworks=bessemer-auto,bessemer-rc10,fastapi-standard,axum
routes=json,items,echo,cpu-inline,cpu-sync,large-echo,bytes,models256

for mode in standard free-threaded; do
  python_build=3.14.7
  if [[ "$mode" == free-threaded ]]; then
    python_build=3.14.7t
  fi
  TRANSPORT=ssh PYTHON_VERSION="$python_build" RUST_VERSION=1.92.0 \
    "$launcher" "$BENCH_SERVER" "$BENCH_CLIENT" "$BENCH_SERVER_PRIVATE_IP" \
    "$run_dir/smoke-$mode" --frameworks "$frameworks" --routes "$routes" \
    --configs 1-core --connections 4 --rounds 1 --warmup 1 --duration 3
  TRANSPORT=ssh PYTHON_VERSION="$python_build" RUST_VERSION=1.92.0 \
    "$launcher" "$BENCH_SERVER" "$BENCH_CLIENT" "$BENCH_SERVER_PRIVATE_IP" \
    "$run_dir/$mode" --frameworks "$frameworks" --routes "$routes" \
    --configs 1-core,4-core --connections 64 --rounds 3 --warmup 2 --duration 5 \
    --route-connections cpu-inline=16,cpu-sync=16,large-echo=16,bytes=16
done
~~~
