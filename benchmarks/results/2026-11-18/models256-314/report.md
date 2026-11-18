# 256-model responses on standard Python 3.14

Current main `e745a7b7`, prior main `7225e6df`, the candidate and FastAPI 0.162.2 used standard CPython 3.14.7 with JIT off on the same shared Apple M5 Max. FastAPI used Uvicorn with uvloop and httptools. Six rotated rounds used one worker, one second of warmup and four seconds of measured load. Full response probes and live interpreter identity bracketed every sample.

The 256-item route returns dictionaries through a response model, with identical 14,885-byte JSON across roles. Absolute cells show median successful req/s, p99 milliseconds and server CPU microseconds per reply. Ratios are paired within each round and show the median and full range. A comparison with an ineligible pair has no aggregate ratio.

## Investigation and changes

The first IPv4 baseline had macOS client address assignment errors in both
roles. Seven of its 12 attempts qualified, with 3,668 measured and 1,612 warmup
socket errors across the rest. The comparison is withheld and all attempts
are retained. The rerun and candidate matrix used the same IPv6 loopback
address for all roles. The only harness changes for IPv6 are the bound
address, port socket family and URL rendering.

The IPv6 baseline qualified all 12 runs. Current main was 0.979× prior main
for throughput at the paired median (0.839–1.042×), with 1.013× server CPU
per reply (0.978–1.066×). The historical throughput and CPU differences
remain unattributed. The two baseline native profiles reached JSON encoding
on the actual HTTP route, and the paired direct encoder CPU range crossed
parity at 0.993× (0.899–1.087×).

Standard-GIL encoding now walks an ordered response dict and writes its
values in one pass when the compiled field plan permits built-in scalar
and list access. It matches live key identities and rolls back the output
on an order mismatch. Defaults, aliases, strict extra-key checking and
general nested mappings keep their existing validation paths.

The native response is 14,885 bytes. Previously it exceeded the 8 KiB
compact JSON cutoff and the general HTTP/1 writer copied it into a
connection buffer. Native-owned JSON now stays eligible up to the
configured response size, with larger replies writing headers and body
together using the existing vectored writer. The retained byte permit
stays with the response through the write or response conversion. The
Python envelope's copy limit stays at 8 KiB.

In the private native encoder test, the 256-dictionary response used
0.892× main's CPU (0.826–0.909×), improving in all six pairs. The direct
echo encoder range crossed parity at 1.005× (0.916–1.238×).

The 64-client candidate reached 18,110 req/s, a paired median 1.120× main
(0.931–1.272×). Five of six throughput pairs improved. CPU per reply was
0.901× (0.850–1.029×) and p99 latency was 0.860× (0.489–1.210×).
Throughput was 7.463× FastAPI. At 16 clients the candidate reached
16,792 req/s and 1.109× main (0.952–1.255×), with 0.914× CPU per reply
(0.861–0.942×) and 7.556× FastAPI throughput.

The typed-echo and small-JSON controls had paired throughput medians of
0.997× and 0.961× main, respectively, with both ranges crossing parity.
Small JSON also used 1.041× CPU per reply at the paired median. The
candidate matrix's one-minute host load average ranged from 17.7 to 25.3.
These results show repeatable direct encoder savings and mixed
end-to-end results across the individual rounds.

## Initial IPv4 attempt with socket setup errors

| Workload | prior-main | main |
| --- | --- | --- |
| 256 model dicts · 64 clients | 15,775 / 12.38 / 51.5 | 14,414 / 15.16 / 55.4 |

| Workload | Comparison | Pairs | Req/s ratio | p99 ratio | CPU/reply ratio |
| --- | --- | --- | --- | --- | --- |
| 256 model dicts · 64 clients | main/prior-main | 3/6 | — | — | — |

7/12 samples qualified with 614,717 successful replies, 0 unexpected statuses, 0 rejections, 0 wrong lengths and 3668 transport errors. Warmup recorded 0 non-200 responses and 1612 transport errors. 0 measured samples lacked warmup counters. The SQL audit checked 11 samples and all 24 tracked process identities exited.

## Current main compared with prior main on IPv6

| Workload | prior-main | main |
| --- | --- | --- |
| 256 model dicts · 64 clients | 15,352 / 12.27 / 55.1 | 15,276 / 12.49 / 55.6 |

| Workload | Comparison | Pairs | Req/s ratio | p99 ratio | CPU/reply ratio |
| --- | --- | --- | --- | --- | --- |
| 256 model dicts · 64 clients | main/prior-main | 6/6 | 0.979× (0.839–1.042) | 1.056× (0.902–1.675) | 1.013× (0.978–1.066) |

12/12 samples qualified with 735,025 successful replies, 0 unexpected statuses, 0 rejections, 0 wrong lengths and 0 transport errors. Warmup recorded 0 non-200 responses and 0 transport errors. 0 measured samples lacked warmup counters. The SQL audit checked 12 samples and all 24 tracked process identities exited.

## Candidate compared with current main and FastAPI on IPv6

| Workload | main | candidate | fastapi |
| --- | --- | --- | --- |
| 256 model dicts · 64 clients | 16,195 / 9.80 / 53.9 | 18,110 / 8.79 / 48.0 | 2,406 / 60.87 / 363.2 |
| 256 model dicts · 16 clients | 15,251 / 4.56 / 53.5 | 16,792 / 4.20 / 48.3 | 2,248 / 22.71 / 371.3 |
| Typed echo · 64 clients | 28,462 / 7.64 / 28.8 | 28,851 / 6.65 / 29.5 | 4,849 / 32.01 / 176.9 |
| Small JSON · 64 clients | 46,651 / 4.69 / 17.9 | 45,037 / 4.90 / 18.6 | 8,462 / 19.92 / 101.3 |

| Workload | Comparison | Pairs | Req/s ratio | p99 ratio | CPU/reply ratio |
| --- | --- | --- | --- | --- | --- |
| 256 model dicts · 64 clients | candidate/main | 6/6 | 1.120× (0.931–1.272) | 0.860× (0.489–1.210) | 0.901× (0.850–1.029) |
| 256 model dicts · 64 clients | candidate/fastapi | 6/6 | 7.463× (6.056–10.541) | 0.156× (0.070–0.216) | 0.132× (0.109–0.158) |
| 256 model dicts · 64 clients | main/fastapi | 6/6 | 6.512× (6.309–8.289) | 0.168× (0.136–0.248) | 0.152× (0.125–0.154) |
| 256 model dicts · 16 clients | candidate/main | 6/6 | 1.109× (0.952–1.255) | 0.893× (0.751–1.271) | 0.914× (0.861–0.942) |
| 256 model dicts · 16 clients | candidate/fastapi | 6/6 | 7.556× (5.927–8.717) | 0.197× (0.131–0.312) | 0.129× (0.121–0.141) |
| 256 model dicts · 16 clients | main/fastapi | 6/6 | 6.529× (5.946–7.788) | 0.221× (0.159–0.305) | 0.144× (0.131–0.152) |
| Typed echo · 64 clients | candidate/main | 6/6 | 0.997× (0.756–1.326) | 0.960× (0.473–1.653) | 1.017× (0.937–1.150) |
| Typed echo · 64 clients | candidate/fastapi | 6/6 | 5.787× (5.133–6.362) | 0.219× (0.178–0.245) | 0.174× (0.160–0.187) |
| Typed echo · 64 clients | main/fastapi | 6/6 | 5.662× (4.798–6.793) | 0.230× (0.134–0.398) | 0.169× (0.153–0.183) |
| Small JSON · 64 clients | candidate/main | 6/6 | 0.961× (0.722–1.048) | 1.051× (0.863–1.538) | 1.041× (0.972–1.263) |
| Small JSON · 64 clients | candidate/fastapi | 6/6 | 5.241× (4.609–5.718) | 0.270× (0.155–0.307) | 0.185× (0.176–0.199) |
| Small JSON · 64 clients | main/fastapi | 6/6 | 5.388× (5.091–6.386) | 0.236× (0.144–0.327) | 0.180× (0.148–0.188) |

72/72 samples qualified with 5,919,097 successful replies, 0 unexpected statuses, 0 rejections, 0 wrong lengths and 0 transport errors. Warmup recorded 0 non-200 responses and 0 transport errors. 0 measured samples lacked warmup counters. The SQL audit checked 72 samples and all 120 tracked process identities exited.

## Direct codec probes

Six pairs per operation measured thread CPU outside HTTP while validating exact decoded objects or complete response bytes. Both probe comparisons run outside the HTTP transport. The baseline is current / prior main and the candidate is candidate / current main.

| Study | Operation | Reference ns | New ns | CPU ratio |
| --- | --- | --- | --- | --- |
| Baseline | Models256 dict encode | 33,490 | 32,777 | 0.993× (0.899–1.087) |
| Candidate | Models256 dict encode | 32,562 | 28,164 | 0.892× (0.826–0.909) |
| Candidate | Typed20 encode | 2,611 | 2,588 | 1.005× (0.916–1.238) |

## Installed validation

Rust formatting, warnings-denied Clippy and 352 workspace tests passed on
macOS, with two ignored. The installed 3.14.7 Python suite covered 1,205 cases
and ended with 25 skips, 172 errors and 10 failures. All 172 error traces
ended in local client address assignment error `OSError(49)`. Four
failed admission subtests included the same error, and the remaining six
failed their client-loss assertions with large numbers of socket errors.
The failed full invocation is preserved. The installed 3.14t wheel passed
all 69 focused model, prepared-body and concurrency cases with the GIL
explicitly disabled.

The isolated ARM64 Linux check used standard Python 3.14.3 from the local
build image. Formatting, Clippy and 363 Rust tests passed (two ignored).
A separate invocation passed Ruff and ran all 1,205 installed-wheel Python
cases with 15 skips. Its four failed subtests came from two GC tests, one
subtest per event loop. The same two tests on unchanged main produced the
same four assertions in the same Linux environment. Both methods passed
on macOS with Python 3.14.7. Linux tooling and Python checks ran across
separate invocations after container setup errors. The remaining benchmark
harness (83 cases, three skips) and six shell syntax checks passed on macOS.

These measurements describe this shared host. Production scaling and the formal M0 gate remain open.
