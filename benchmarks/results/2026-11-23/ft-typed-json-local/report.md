# Free-threaded typed JSON: local before and after on CPython 3.14

## Findings

On free-threaded CPython 3.14.6t and one core, the candidate served typed
echo at 1.101× `main` (0.986–1.206, faster in 7 of 8 interleaved pairs) and
the 1,000-item typed upload at 1.188× (1.033–1.228, faster in all 8). The
harness matrix agrees: 1.105× (1.059–1.128) and 1.200× (1.187–1.223), or
1.083× and 1.153× after dividing by the Axum control repeated in each run.
On the upload, median p99 fell from 20.9 ms to 16.3 ms and server CPU per
million replies from 710 s to 587 s.

256-model responses did not change: 0.989× (0.940–1.049) interleaved, faster
in 4 of 8 pairs, and 1.033× (0.985–1.049) in the matrix. This change does
not touch the response encoder.

Standard CPython 3.14.6 did not change. Its four interleaved ratios all
cross parity: small JSON 1.007×, typed echo 0.978×, the upload 0.993× and
256 models 0.995×.

Relative to standard Python in the same matrix, free-threaded Bessemer
moved from 0.879× to 0.926× on typed echo and from 0.773× to 0.894× on the
upload. FastAPI measured 0.934× and 0.959× on those routes, and 0.762× on
256 models, where Bessemer is at 0.882×.

A second change to the response encoder was measured and not kept. It wrote
nested lists and response dicts under their parent's lock, taking each
nested lock only when it was free. Against the candidate it gave 0.959×,
0.990× and 1.000× on the three routes, with every range crossing parity.
Its [patch](nested-lock-encoder.patch) applies to the candidate.

## Method and qualification

The matrix qualified 240/240 samples; 35,367,013 timed HTTP 200 replies.

This is a local, same-host measurement. It is not the two-machine GCP
comparison and establishes no performance acceptance: the server and the load
generator share one four-CPU virtual machine whose throughput moved by up to
10% between identical rounds. It reproduces the pattern of the
[RC10 GCP report](../rc10-gcp-python314/report.md) on one core and compares
two builds under the same conditions.

- Host: Intel(R) Xeon(R) Processor @ 2.10GHz, 4 virtual CPUs, Linux
  6.18.44. Servers were pinned to CPU 0 and `oha` 1.16.0 to CPUs 2 and 3,
  over IPv4 loopback with HTTP/1.1 keep-alive.
- Interpreters: CPython 3.14.6 and free-threaded 3.14.6t (the GCP run used
  3.14.7). A worker of each Bessemer build reported the GIL disabled on
  3.14.6t when asked afterwards, and it stayed disabled after importing the
  FastAPI stack; the same-host harness does not probe it during the runs.
- Bessemer `main` is commit `eef777e` (the 1.0.0rc10 source), and the
  candidate is this change. Both were built here as release wheels with
  maturin 1.16.0 and Rust 1.97.0. They are not the published RC10 wheels.
- FastAPI 0.165.0 with Uvicorn 0.54.0, uvloop 0.23.0 and httptools 0.9.0,
  Pydantic 2.14.4 and the locked Axum 0.8 fixture, as in the GCP run.
- Harness: the GCP run's [benchmark patch](../rc10-gcp-python314/benchmark-source.patch)
  applied to `benchmarks/compare`, with the same routes, payloads and
  connection counts, 2 seconds of warmup, 5 timed seconds with request
  drain, three rotated rounds and `--check-bodies`. Only the one-core
  configuration was run: with four CPUs shared with the load generator, a
  four-core server would be measured against a starved client.

The matrix ran as four consecutive harness runs: `main` then the candidate
on standard Python, then the same on free-threaded Python. Builds are
therefore compared across runs several minutes apart, with Axum repeated in
each as a control. The interleaved measurement removes that gap for
Bessemer: the two builds alternate, eight pairs for each interpreter, with
the order swapped on every pair, 2 seconds of warmup and 4 timed seconds for
each route. Those runs count HTTP 200 replies over the elapsed time and do
not drain requests in flight at the deadline, so they are compared with
each other and not with the matrix. The encoder experiment's pairs compare
an earlier build of the same decoding change with that build plus the
patch.

| Wheel | SHA-256 |
| --- | --- |
| main, cp314 | `ea3f35fdb0a9887683a73a6748add3bc38cdaeb31f58590f60929ad8833db231` |
| main, cp314t | `08fc3cfa1d0387ded65f51ed91aa7370b73feaf719869bdd28ff058ad3f4870d` |
| candidate, cp314 | `9316c2d39a52c4f541777e9c0f35d96c9c8185b8c7fbc17bb7be371db65beabf` |
| candidate, cp314t | `4304fca0cf03879f3426a13ba088a98bac70bb69f2f69432aef42722b9151c9d` |


### Free-threaded over standard throughput, before and after

| Route | Main Bessemer | Candidate Bessemer | FastAPI | Axum control |
| --- | ---: | ---: | ---: | ---: |
| json | 0.914× (0.907–0.982) | 1.007× (1.000–1.008) | 0.997× (0.891–1.008) | 1.002× (0.911–1.104) |
| items | 0.925× (0.867–0.933) | 0.986× (0.974–1.012) | 0.944× (0.929–1.003) | 1.070× (1.042–1.087) |
| echo | 0.879× (0.829–0.911) | 0.926× (0.902–0.968) | 0.934× (0.879–1.005) | 0.992× (0.982–1.141) |
| cpu-inline | 0.866× (0.763–0.872) | 0.906× (0.864–0.963) | 0.934× (0.903–0.939) | 1.007× (0.990–1.056) |
| cpu-sync | 0.860× (0.824–0.885) | 0.933× (0.843–0.951) | 0.931× (0.891–1.031) | 1.133× (1.004–1.141) |
| large-echo | 0.773× (0.771–0.835) | 0.894× (0.890–0.908) | 0.959× (0.841–1.011) | 1.022× (0.976–1.026) |
| bytes | 1.024× (0.902–1.094) | 1.082× (0.917–1.221) | 1.009× (0.959–1.011) | 1.120× (0.963–1.162) |
| models256 | 0.814× (0.782–0.882) | 0.882× (0.846–0.924) | 0.762× (0.748–0.800) | 0.976× (0.941–1.060) |

### Candidate over main, same interpreter

| Python | Route | Main replies/s | Candidate replies/s | Candidate / main | Normalised to Axum |
| --- | --- | ---: | ---: | ---: | ---: |
| standard | json | 113,658 | 106,194 | 0.934× (0.934–0.953) | 1.051× (0.876–1.081) |
| standard | items | 102,675 | 102,858 | 0.992× (0.982–1.031) | 1.013× (1.009–1.031) |
| standard | echo | 48,867 | 51,312 | 1.003× (0.973–1.110) | 1.064× (0.971–1.153) |
| standard | cpu-inline | 480 | 466 | 0.968× (0.935–0.970) | 0.932× (0.931–1.007) |
| standard | cpu-sync | 458 | 463 | 0.982× (0.962–1.012) | 1.018× (0.931–1.025) |
| standard | large-echo | 1,820 | 1,896 | 1.042× (1.039–1.108) | 1.034× (1.018–1.127) |
| standard | bytes | 16,050 | 17,497 | 1.090× (0.913–1.188) | 1.056× (0.871–1.281) |
| standard | models256 | 27,847 | 26,373 | 0.947× (0.888–1.033) | 0.903× (0.873–0.999) |
| free-threaded | json | 103,580 | 106,937 | 1.030× (0.958–1.050) | 1.023× (0.968–1.090) |
| free-threaded | items | 92,258 | 101,390 | 1.099× (1.036–1.147) | 1.076× (0.989–1.110) |
| free-threaded | echo | 43,443 | 47,449 | 1.105× (1.059–1.128) | 1.083× (1.040–1.133) |
| free-threaded | cpu-inline | 406 | 427 | 1.057× (1.008–1.075) | 1.065× (1.021–1.116) |
| free-threaded | cpu-sync | 393 | 411 | 1.064× (0.965–1.111) | 0.993× (0.962–1.021) |
| free-threaded | large-echo | 1,406 | 1,701 | 1.200× (1.187–1.223) | 1.153× (1.112–1.238) |
| free-threaded | bytes | 16,380 | 18,923 | 1.151× (0.996–1.236) | 1.034× (1.006–1.204) |
| free-threaded | models256 | 22,674 | 22,988 | 1.033× (0.985–1.049) | 1.007× (0.979–1.083) |

### Candidate successful HTTP replies per second, one core

| Python | Route | Bessemer | FastAPI | Axum | Bessemer / FastAPI | Bessemer / Axum |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| standard | json | 106,194 | 9,919 | 97,960 | 10.521× (10.323–11.815) | 1.084× (0.996–1.179) |
| standard | items | 102,858 | 7,798 | 95,073 | 13.197× (12.586–13.764) | 1.074× (1.072–1.112) |
| standard | echo | 51,312 | 6,178 | 28,179 | 8.466× (7.935–8.497) | 1.821× (1.799–1.874) |
| standard | cpu-inline | 466 | 442 | 4,891 | 1.062× (1.002–1.074) | 0.095× (0.094–0.102) |
| standard | cpu-sync | 463 | 428 | 4,812 | 1.080× (1.024–1.082) | 0.094× (0.094–0.096) |
| standard | large-echo | 1,896 | 452 | 1,507 | 4.205× (4.013–4.231) | 1.255× (1.211–1.264) |
| standard | bytes | 17,497 | 6,648 | 18,756 | 2.649× (2.494–2.720) | 0.922× (0.920–0.933) |
| standard | models256 | 26,373 | 3,198 | 16,119 | 8.095× (7.777–8.559) | 1.543× (1.523–1.660) |
| free-threaded | json | 106,937 | 9,282 | 98,185 | 11.722× (10.432–11.891) | 1.089× (1.067–1.102) |
| free-threaded | items | 101,390 | 7,492 | 102,479 | 13.533× (12.979–14.384) | 0.989× (0.959–1.079) |
| free-threaded | echo | 47,449 | 5,660 | 27,965 | 8.182× (7.831–8.739) | 1.656× (1.526–1.767) |
| free-threaded | cpu-inline | 427 | 416 | 4,925 | 1.027× (0.992–1.066) | 0.087× (0.082–0.091) |
| free-threaded | cpu-sync | 411 | 404 | 5,221 | 1.083× (0.885–1.093) | 0.079× (0.077–0.081) |
| free-threaded | large-echo | 1,701 | 429 | 1,538 | 4.008× (3.720–4.246) | 1.106× (1.104–1.111) |
| free-threaded | bytes | 18,923 | 6,490 | 20,179 | 2.916× (2.402–3.174) | 0.878× (0.868–1.003) |
| free-threaded | models256 | 22,988 | 2,438 | 16,297 | 9.430× (9.156–9.434) | 1.381× (1.370–1.461) |

### Bessemer latency and CPU on the affected routes

| Python | Route | Build | p50 ms | p99 ms | CPU s / million |
| --- | --- | --- | ---: | ---: | ---: |
| standard | echo | main | 1.254 | 2.344 | 20.01 |
| standard | echo | candidate | 1.203 | 2.297 | 19.10 |
| standard | large-echo | main | 8.658 | 14.872 | 548.46 |
| standard | large-echo | candidate | 8.322 | 14.232 | 526.38 |
| standard | models256 | main | 2.166 | 4.378 | 35.34 |
| standard | models256 | candidate | 2.363 | 4.279 | 37.24 |
| free-threaded | echo | main | 1.419 | 2.781 | 22.51 |
| free-threaded | echo | candidate | 1.334 | 2.400 | 20.57 |
| free-threaded | large-echo | main | 10.825 | 20.885 | 709.62 |
| free-threaded | large-echo | candidate | 9.207 | 16.259 | 586.78 |
| free-threaded | models256 | main | 2.792 | 4.917 | 43.49 |
| free-threaded | models256 | candidate | 2.748 | 4.630 | 42.80 |

### Interleaved Bessemer builds, one core

| Python | Route | Pairs | main replies/s | candidate replies/s | candidate / main | Pairs faster | main p99 ms | candidate p99 ms |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| standard | json | 8 | 104,976 | 106,535 | 1.007× (0.921–1.068) | 5/8 | 1.260 | 1.220 |
| standard | echo | 8 | 50,092 | 49,138 | 0.978× (0.926–1.187) | 3/8 | 2.401 | 2.564 |
| standard | large-echo | 8 | 1,880 | 1,882 | 0.993× (0.939–1.129) | 4/8 | 14.130 | 14.277 |
| standard | models256 | 8 | 25,451 | 25,190 | 0.995× (0.921–1.022) | 4/8 | 4.561 | 4.379 |
| free-threaded | json | 8 | 101,080 | 102,899 | 0.975× (0.961–1.124) | 3/8 | 1.277 | 1.233 |
| free-threaded | echo | 8 | 43,446 | 47,452 | 1.101× (0.986–1.206) | 7/8 | 2.575 | 2.386 |
| free-threaded | large-echo | 8 | 1,398 | 1,636 | 1.188× (1.033–1.228) | 8/8 | 20.910 | 16.893 |
| free-threaded | models256 | 8 | 22,278 | 21,915 | 0.989× (0.940–1.049) | 4/8 | 4.759 | 5.470 |

### Nested-lock encoder experiment, interleaved, one core

| Python | Route | Pairs | candidate replies/s | nested-lock-encoder replies/s | nested-lock-encoder / candidate | Pairs faster | candidate p99 ms | nested-lock-encoder p99 ms |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| free-threaded | echo | 8 | 45,522 | 44,068 | 0.959× (0.853–1.100) | 2/8 | 2.698 | 2.814 |
| free-threaded | large-echo | 8 | 1,498 | 1,507 | 0.990× (0.912–1.133) | 4/8 | 19.148 | 18.599 |
| free-threaded | models256 | 8 | 21,493 | 21,861 | 1.000× (0.798–1.150) | 4/8 | 5.433 | 5.656 |

## Where the time went

`perf` sampled the free-threaded worker on its CPU clock while it served the
1,000-item upload. Shares are of the worker's samples.

| Symbol | main | candidate |
| --- | ---: | ---: |
| `PyDict_SetItem` | 10.2% | 0.0% |
| `_PyDict_FromItems` and its inserts | 0.0% | 6.9% |
| `PyMember_SetOne` | 6.1% | 0.0% |
| `_Py_IncRef` and `_Py_DecRefShared` | 3.0% | 1.0% |
| `_PyObject_SetManagedDict` | 2.8% | 3.5% |
| `set_update_local` and `set_merge_lock_held` | 3.2% | 3.3% |
| `_PyObject_MiMalloc` | 7.4% | 8.9% |

The last three rows are the cost that remains inside CPython: installing
the `__dict__`, copying the field-name set and allocating. On the 256-model
route of `main`, `PyCriticalSection_Begin` and `_End` were 16.2% of the
free-threaded worker and `_Py_IncRef` with `_Py_DecRefShared` 13.9%. The
nested-lock experiment removed both from the profile without changing
throughput: the inline compare-and-swap on each nested lock took their
place.

## Reproduce

Build release wheels of `eef777e` and of this change for CPython 3.14 and
3.14t, install each into its own environment with the packages above, and
apply the GCP run's benchmark patch to a checkout. For each environment:

```bash
cd benchmarks/compare
python3 run_compare.py --python "$ENVIRONMENT/bin/python" \
  --axum "$AXUM" --oha "$OHA" --out "$RESULTS/$BUILD-$PYTHON.json" \
  --rounds 3 --duration 5 --warmup 2 --connections 64 \
  --route-connections cpu-inline=16,cpu-sync=16,large-echo=16,bytes=16 \
  --configs 1-core --client-cpus 2,3 --check-bodies \
  --frameworks bessemer-auto,fastapi-standard,axum \
  --routes json,items,echo,cpu-inline,cpu-sync,large-echo,bytes,models256
```

The `main` runs left out `fastapi-standard`. The interleaved runs started
`bessemer run bessemer_app:app --workers 1` pinned to CPU 0 for one build,
ran `oha` for each route with the harness's connection counts, stopped the
server and did the same for the other build.

[Per-sample matrix data](samples.csv), the [interleaved pairs](interleaved.csv)
and the [encoder experiment's pairs](encoder-experiment.csv) are the inputs
of [`analyse_local.py`](analyse_local.py), which prints the tables above.
The CSV files leave out the harness's local command lines.
