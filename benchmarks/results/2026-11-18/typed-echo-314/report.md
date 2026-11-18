# Typed echo on standard Python 3.14

Current main `7225e6df`, prior main `ea96c874`, the candidate and FastAPI 0.162.2 used standard CPython 3.14.7 with JIT off on the same shared Apple M5 Max. FastAPI used Uvicorn with uvloop and httptools. Six rotated rounds used one worker, one second of warmup and four seconds of measured load. Full response probes and live interpreter identity bracketed every sample.

Echo validates and returns a 20-item model. Models256 returns dictionaries through a response model. Absolute cells show median successful req/s, p99 milliseconds and server CPU microseconds per reply. Ratios are paired within each round and show the median and full range. A comparison with an ineligible pair has no aggregate ratio.

## Investigation and changes

The fresh baseline's typed-echo median was 0.955× prior main at 64 clients
(0.843–1.049× across pairs). It was lower in four rounds. CPU per reply
was 1.059× (0.952–1.165×). The direct parse and encode ratios crossed
parity. Five-second native stack samples reached the native model parser
and Python object assembly on each commit. Those profiles and the direct
probes leave the cause of the historical throughput variation open.

The typed parser reads the matching ordered prefix directly into fresh field
slots and resumes at the first mismatch with the fields it already built.
The general parser still checks reordered, escaped, aliased and duplicate
keys and validates ignored values. Standard-GIL model encoding uses the
already-checked dictionary size and checks each declared key in order. It
avoids a terminal dictionary cursor call for every ordered model instance.

The request path sizes its argument vector for values actually bound on
the transport thread. A single native body travels separately, so the
body-only route skips an empty allocation and another parameter scan.
Body buffering returns a single terminal data frame directly after enforcing
the length and charging the retained-byte budget. A stream without an
end-of-stream hint retains its normal polling and trailer handling. The
budget test checks the poll counts, byte identity and budget ownership.

In the FastAPI matrix, typed echo at 16 clients reached 62,019 req/s, a
paired median 1.091× current main (0.999–1.248×) with 0.924× server CPU per
reply. At 64 clients it reached 60,630 req/s and 1.003× main (0.650–1.213×).
The slowest candidate echo sample had observed host CPU utilisation up to
99.3% against 71.0% for its paired main sample. Both samples remain eligible
and the timing cause is unconfirmed. The paired FastAPI ratios were 5.719×
at 16 clients and 6.259× at 64 clients.

The 256-model control had a lower matrix median of 0.933× main, while the
small JSON control measured 1.077×. Both ranges crossed parity. The direct
candidate/main parser CPU ratio was 1.002× (0.858–1.030×). The encoder ratio
was 0.983× (0.886–1.050×). The measurements do not attribute the HTTP results
to either codec change in isolation.

A separate recheck used the same wheels at 64 clients. Typed echo gained
8.0% at the paired median (0.861–1.132×) and was faster in five of six pairs.
Server CPU per reply was 0.926× main (0.879–1.158×). The model control was
0.970× main (0.946–1.013×) and used 1.027× CPU per reply (1.000–1.051×),
higher in all six pairs. It therefore remains an open efficiency result.
A separate direct probe of that exact 256-dictionary response encoder was
0.962× main (0.911–1.159×). The probe leaves the cause of the model HTTP
control difference unconfirmed. The matrix and recheck stay separate in
the results and raw samples.

## Current main compared with prior main

| Workload | prior-main | main |
| --- | --- | --- |
| Typed echo · 64 clients | 53,797 / 2.99 / 16.7 | 51,092 / 3.24 / 17.3 |

| Workload | Comparison | Pairs | Req/s ratio | p99 ratio | CPU/reply ratio |
| --- | --- | --- | --- | --- | --- |
| Typed echo · 64 clients | main/prior-main | 6/6 | 0.955× (0.843–1.049) | 1.045× (0.868–1.188) | 1.059× (0.952–1.165) |

12/12 samples qualified with 2,540,079 successful replies, 0 unexpected statuses, 0 rejections, 0 wrong lengths and 0 transport errors. Warmup recorded 0 non-200 responses and 0 transport errors. 0 measured samples lacked warmup counters. The SQL audit checked 12 samples and all 24 tracked process identities exited.

## Candidate compared with current main and FastAPI

| Workload | main | candidate | fastapi |
| --- | --- | --- | --- |
| Typed echo · 64 clients | 61,004 / 2.69 / 14.9 | 60,630 / 2.65 / 14.9 | 9,882 / 13.16 / 90.6 |
| Typed echo · 16 clients | 57,840 / 0.59 / 15.3 | 62,019 / 0.56 / 14.3 | 10,964 / 3.64 / 82.9 |
| 256 model dicts · 64 clients | 33,841 / 3.70 / 27.7 | 33,200 / 3.85 / 28.2 | 4,779 / 24.27 / 190.3 |
| Small JSON · 64 clients | 103,755 / 2.17 / 8.6 | 112,689 / 1.31 / 8.0 | 17,508 / 7.56 / 51.4 |

| Workload | Comparison | Pairs | Req/s ratio | p99 ratio | CPU/reply ratio |
| --- | --- | --- | --- | --- | --- |
| Typed echo · 64 clients | candidate/main | 6/6 | 1.003× (0.650–1.213) | 0.987× (0.884–1.852) | 0.990× (0.830–1.385) |
| Typed echo · 64 clients | candidate/fastapi | 6/6 | 6.259× (4.607–6.840) | 0.202× (0.151–0.336) | 0.161× (0.146–0.202) |
| Typed echo · 64 clients | main/fastapi | 6/6 | 6.451× (5.130–7.086) | 0.203× (0.150–0.227) | 0.156× (0.146–0.194) |
| Typed echo · 16 clients | candidate/main | 6/6 | 1.091× (0.999–1.248) | 0.954× (0.479–1.022) | 0.924× (0.835–1.001) |
| Typed echo · 16 clients | candidate/fastapi | 6/6 | 5.719× (5.256–6.826) | 0.153× (0.135–0.162) | 0.171× (0.146–0.186) |
| Typed echo · 16 clients | main/fastapi | 6/6 | 5.219× (4.862–6.064) | 0.161× (0.138–0.297) | 0.187× (0.164–0.194) |
| 256 model dicts · 64 clients | candidate/main | 6/6 | 0.933× (0.821–1.058) | 1.082× (0.952–1.190) | 1.036× (0.968–1.081) |
| 256 model dicts · 64 clients | candidate/fastapi | 6/6 | 6.705× (5.740–7.661) | 0.169× (0.133–0.188) | 0.149× (0.137–0.156) |
| 256 model dicts · 64 clients | main/fastapi | 6/6 | 7.104× (6.993–7.323) | 0.154× (0.140–0.166) | 0.144× (0.142–0.147) |
| Small JSON · 64 clients | candidate/main | 6/6 | 1.077× (0.896–1.335) | 0.722× (0.490–1.390) | 0.948× (0.757–1.123) |
| Small JSON · 64 clients | candidate/fastapi | 6/6 | 6.652× (5.783–6.959) | 0.174× (0.145–0.288) | 0.151× (0.144–0.174) |
| Small JSON · 64 clients | main/fastapi | 6/6 | 6.008× (5.212–6.587) | 0.274× (0.157–0.314) | 0.165× (0.152–0.191) |

72/72 samples qualified with 13,543,764 successful replies, 0 unexpected statuses, 0 rejections, 0 wrong lengths and 0 transport errors. Warmup recorded 0 non-200 responses and 0 transport errors. 0 measured samples lacked warmup counters. The SQL audit checked 72 samples and all 120 tracked process identities exited.

## Separate 64-client echo and model recheck

| Workload | main | candidate |
| --- | --- | --- |
| Typed echo · 64 clients | 53,544 / 2.95 / 16.9 | 56,453 / 2.85 / 16.0 |
| 256 model dicts · 64 clients | 31,155 / 4.07 / 29.2 | 30,494 / 4.21 / 29.9 |

| Workload | Comparison | Pairs | Req/s ratio | p99 ratio | CPU/reply ratio |
| --- | --- | --- | --- | --- | --- |
| Typed echo · 64 clients | candidate/main | 6/6 | 1.080× (0.861–1.132) | 0.942× (0.897–1.087) | 0.926× (0.879–1.158) |
| 256 model dicts · 64 clients | candidate/main | 6/6 | 0.970× (0.946–1.013) | 1.016× (0.961–1.074) | 1.027× (1.000–1.051) |

24/24 samples qualified with 4,140,702 successful replies, 0 unexpected statuses, 0 rejections, 0 wrong lengths and 0 transport errors. Warmup recorded 0 non-200 responses and 0 transport errors. 0 measured samples lacked warmup counters. The SQL audit checked 24 samples and all 48 tracked process identities exited.

## Direct codec probes

Six pairs per operation measured thread CPU outside HTTP while validating exact decoded objects or complete response bytes. The baseline is current / prior main and the candidate is candidate / current main.

| Study | Operation | Reference ns | New ns | CPU ratio |
| --- | --- | --- | --- | --- |
| baseline | Typed20 parse | 4,080 | 3,898 | 0.944× (0.914–1.094) |
| baseline | Typed20 encode | 1,352 | 1,375 | 0.996× (0.946–1.052) |
| candidate | Typed20 parse | 3,725 | 3,737 | 1.002× (0.858–1.030) |
| candidate | Typed20 encode | 1,334 | 1,294 | 0.983× (0.886–1.050) |
| candidate | Models256 dict encode | 17,594 | 16,902 | 0.962× (0.911–1.159) |

## Installed validation

Rust formatting, warnings-denied Clippy and 351 workspace tests passed, with
two ignored. The installed standard 3.14 wheel's full Python run covered
1,205 cases with 25 skips. Seven methods hit the local macOS client address
error `OSError(49)`. All seven unchanged methods passed in isolated reruns,
including one that needed a further socket retry. The 83 benchmark harness
cases (three skips) and six shell syntax checks passed. The initial full
invocation and socket failures are retained in the validation receipts.

The independently installed free-threaded 3.14 wheel passed all 69 focused
model, prepared-body and concurrency stress cases with the GIL explicitly
disabled. The new Rust coverage checks terminal and non-terminal one-frame
bodies at the exact byte limit, preserves the original bytes pointer, and
requires the byte and request permits to survive the completed read.

These measurements describe this shared host. Production scaling and the formal M0 gate remain open.
