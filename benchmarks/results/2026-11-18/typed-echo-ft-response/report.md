# Typed echo and free-threaded model responses

Bessemer main `ea96c874`, the candidate wheels and FastAPI 0.162.2 ran on the same shared Apple M5 Max macOS host. The application fixtures, pinned dependencies and load generator were identical across the three roles. FastAPI used Uvicorn with uvloop and httptools. CPython 3.13.18, 3.14.7 and 3.14.7t ran with JIT off. The probes checked the live GIL state before and after the timed samples.

Each workload ran six rotated rounds at 64 connections with one server worker, one second of warmup and four seconds of measured load. The echo validates and returns a 20-item model. The response-only route returns 256 dictionaries checked by a model plan. Ratios are within-round medians and full ranges. A comparison with an ineligible pair has no aggregate ratio. The GIL-mode table compares workloads in the same round.

## Findings and changes

Typed requests previously scanned the digits in short numbers, then visited
them again to compute the value. The scanner now accumulates those digits
while locating the token boundary. Longer integers, exponents and decimals
outside the exact shortcut still use the existing conversions. Native route
binding moves the parsed body into the argument list and transfers it to the
Python fallback if a later binding rejects the native route. The accepted
JSON grammar, duplicate handling and scalar binding order stay the same.

Earlier native profiles found reference accounting and container snapshots
in free-threaded model responses. The ordered-field encoder now writes a
bounded prefix of exact primitive values while the dictionary's critical
section protects them. It holds strong references for the remaining fields,
including nested values, and encodes them after unlocking. Short scalar lists
can write items under their own critical section. A failed speculative
match rolls back its output before the existing fallback. The borrowed ASCII
shortcut requires CPython 3.14 or later, where Unicode layout and mutable
interning state occupy separate storage. Large strings and lists, Unicode
outside ASCII, reordered keys and unsupported values retain their fallbacks.

On both free-threaded GIL settings, all six paired direct encoding measurements
used less thread CPU for typed echo and the 256-dictionary response. The
GIL-disabled 256-model benchmark used a paired median 0.747× the baseline
encoder CPU, with a range of 0.690–0.796×. Typed-echo encoding used 0.830×
(0.787–0.865×). The private typed parser's paired ranges crossed parity in
all four configurations, so it did not establish a repeatable parser gain.

At 64 connections, the GIL-disabled 256-model route was faster in all six
pairs, reaching a paired throughput median of 1.261× main (1.218–1.435×),
with 0.806× CPU per reply and 0.817× p99. The GIL-enabled free-threaded
build measured 1.271× throughput (1.150–1.420×) and 0.799× CPU per reply.
GIL-disabled throughput rose from 0.759× the matching standard 3.14 build
on main to 0.968× on the candidate. Its candidate range was 0.921–1.144×.
The FastAPI ratios for the candidate's model response were 8.307× with
the GIL off and 7.636× with the GIL on.

Typed echo had paired throughput medians of 1.053× main on 3.13 and
1.068× on GIL-disabled 3.14t. The ratios on standard 3.14 and GIL-enabled
3.14t were 0.988× and 0.994×. Every echo throughput range crossed parity.
Candidate echo was 5.497–5.922× FastAPI across the four configurations at
the paired medians. The model response on standard 3.14 and the JSON control
also crossed parity with main. The one-minute host load average ranged from
7.1 to 17.8 across the run.

## HTTP results

Absolute cells show median successful req/s, p99 milliseconds and server CPU microseconds per successful reply, in that order.

| Workload | Main | Candidate | FastAPI |
| --- | --- | --- | --- |
| Typed echo · 3.13 | 67,496 / 2.45 / 13.7 | 71,444 / 2.33 / 13.0 | 12,032 / 10.85 / 77.5 |
| Typed echo · 3.14 | 67,879 / 2.47 / 13.5 | 71,911 / 2.28 / 12.8 | 11,740 / 10.54 / 78.4 |
| Typed echo · 3.14t GIL on | 58,781 / 2.70 / 15.6 | 62,881 / 2.54 / 14.7 | 11,043 / 11.92 / 84.1 |
| Typed echo · 3.14t GIL off | 56,791 / 2.75 / 16.3 | 65,549 / 2.44 / 14.1 | 10,118 / 12.72 / 90.0 |
| 256 model dicts · 3.14 | 33,996 / 3.72 / 26.9 | 32,946 / 3.81 / 28.4 | 5,106 / 22.20 / 180.3 |
| 256 model dicts · 3.14t GIL on | 26,322 / 4.53 / 35.1 | 33,406 / 3.76 / 28.3 | 4,452 / 26.73 / 210.5 |
| 256 model dicts · 3.14t GIL off | 25,803 / 4.56 / 35.1 | 33,857 / 3.76 / 28.1 | 4,018 / 28.11 / 229.8 |
| JSON control · 3.14t GIL off | 121,888 / 1.18 / 7.5 | 128,075 / 1.16 / 7.2 | 20,118 / 6.90 / 45.4 |

### Candidate / main

| Workload | Pairs | Req/s ratio | p99 ratio | CPU/reply ratio |
| --- | --- | --- | --- | --- |
| Typed echo · 3.13 | 6/6 | 1.053× (0.967–1.112) | 0.893× (0.825–1.095) | 0.955× (0.914–1.025) |
| Typed echo · 3.14 | 6/6 | 0.988× (0.895–1.146) | 0.997× (0.882–1.135) | 1.009× (0.889–1.107) |
| Typed echo · 3.14t GIL on | 6/6 | 0.994× (0.944–1.171) | 0.989× (0.893–1.018) | 0.998× (0.870–1.063) |
| Typed echo · 3.14t GIL off | 6/6 | 1.068× (0.996–1.226) | 0.960× (0.818–1.061) | 0.933× (0.818–1.003) |
| 256 model dicts · 3.14 | 6/6 | 0.987× (0.966–1.078) | 1.008× (0.942–1.043) | 1.023× (0.932–1.125) |
| 256 model dicts · 3.14t GIL on | 6/6 | 1.271× (1.150–1.420) | 0.837× (0.761–0.941) | 0.799× (0.752–0.849) |
| 256 model dicts · 3.14t GIL off | 6/6 | 1.261× (1.218–1.435) | 0.817× (0.766–0.869) | 0.806× (0.791–0.824) |
| JSON control · 3.14t GIL off | 6/6 | 1.044× (0.974–1.108) | 0.975× (0.868–1.279) | 0.959× (0.904–1.024) |

### Candidate / FastAPI

| Workload | Pairs | Req/s ratio | p99 ratio | CPU/reply ratio |
| --- | --- | --- | --- | --- |
| Typed echo · 3.13 | 6/6 | 5.864× (5.471–6.961) | 0.208× (0.177–0.250) | 0.172× (0.144–0.182) |
| Typed echo · 3.14 | 6/6 | 5.652× (5.562–6.743) | 0.222× (0.210–0.240) | 0.176× (0.150–0.179) |
| Typed echo · 3.14t GIL on | 6/6 | 5.497× (5.285–7.071) | 0.223× (0.183–0.242) | 0.180× (0.142–0.189) |
| Typed echo · 3.14t GIL off | 6/6 | 5.922× (5.183–9.435) | 0.214× (0.096–0.229) | 0.169× (0.123–0.192) |
| 256 model dicts · 3.14 | 6/6 | 6.546× (6.074–7.031) | 0.170× (0.153–0.179) | 0.156× (0.146–0.166) |
| 256 model dicts · 3.14t GIL on | 6/6 | 7.636× (7.252–9.134) | 0.137× (0.112–0.152) | 0.133× (0.113–0.139) |
| 256 model dicts · 3.14t GIL off | 6/6 | 8.307× (7.063–9.143) | 0.138× (0.113–0.155) | 0.123× (0.114–0.142) |
| JSON control · 3.14t GIL off | 6/6 | 6.521× (5.607–7.102) | 0.165× (0.157–0.255) | 0.155× (0.142–0.176) |

### 256-model responses, free-threaded / standard 3.14

| Role | Free-threaded mode | Req/s ratio | p99 ratio | CPU/reply ratio |
| --- | --- | --- | --- | --- |
| main | 3.14t GIL off | 0.759× (0.687–0.930) | 1.246× (1.094–1.345) | 1.296× (1.253–1.328) |
| main | 3.14t GIL on | 0.782× (0.748–0.793) | 1.202× (1.169–1.242) | 1.294× (1.257–1.340) |
| candidate | 3.14t GIL off | 0.968× (0.921–1.144) | 1.016× (0.880–1.063) | 1.038× (0.908–1.092) |
| candidate | 3.14t GIL on | 0.981× (0.921–1.104) | 1.012× (0.905–1.075) | 1.024× (0.937–1.090) |
| fastapi | 3.14t GIL off | 0.744× (0.697–0.994) | 1.280× (1.032–1.391) | 1.336× (1.053–1.414) |
| fastapi | 3.14t GIL on | 0.811× (0.734–0.971) | 1.256× (1.057–1.401) | 1.239× (1.076–1.358) |

## Native codec probes

The process-bound probes alternate main and candidate for six pairs. They validate actual objects or bytes and measure thread CPU outside HTTP.

| Operation | Python | Main ns | Candidate ns | Candidate / main |
| --- | --- | --- | --- | --- |
| Typed20 parse | 3.13 | 4,036 | 4,238 | 1.060× (0.951–1.109) |
| Typed20 parse | 3.14 | 3,905 | 3,757 | 0.977× (0.886–1.049) |
| Typed20 parse | 3.14t GIL on | 4,347 | 4,420 | 0.997× (0.936–1.125) |
| Typed20 parse | 3.14t GIL off | 4,316 | 4,368 | 1.020× (0.892–1.096) |
| Typed20 model encode | 3.13 | 1,366 | 1,425 | 1.007× (0.989–1.056) |
| Typed20 model encode | 3.14 | 1,408 | 1,375 | 0.986× (0.887–1.036) |
| Typed20 model encode | 3.14t GIL on | 2,005 | 1,642 | 0.821× (0.778–0.865) |
| Typed20 model encode | 3.14t GIL off | 2,031 | 1,691 | 0.830× (0.787–0.865) |
| Models256 dict encode | 3.14 | 17,235 | 16,672 | 0.975× (0.953–0.990) |
| Models256 dict encode | 3.14t GIL on | 23,008 | 17,080 | 0.740× (0.695–0.809) |
| Models256 dict encode | 3.14t GIL off | 23,391 | 17,424 | 0.747× (0.690–0.796) |

## Validation

Rust formatting, warnings-denied Clippy and 351 workspace tests passed, with
two ignored. The installed Python 3.13 check ran 1,205 cases with 25 skips and
14 local client address errors (`OSError(49)`) across nine methods. Seven
methods passed in a grouped rerun and the last two passed individually. The
83 benchmark harness cases (three skips) and six shell syntax checks then
passed. The original full invocation retained its setup failures.

Each focused installed-wheel configuration ran 266 cases. Standard CPython
3.12 with Pydantic 2.10.6, 3.13 and 3.14 each skipped two cases. Both
free-threaded 3.14 GIL settings skipped 21 cases. The first overlapping 3.13
focused run failed a socket-heavy admission test with client-side socket
loss. The unchanged serial run passed. Free-threaded codec Clippy and all
27 codec unit tests passed, and nine installed-wheel stress cases passed with
the GIL explicitly disabled.

A standalone differential check of the baseline and new number scanners
matched result kinds, exact integer values, float bits, consumed offsets and
rejections for 60,039 numeric cases. The Rust encoder tests exercise output
rollback, Unicode and longer-list fallback, escaping and byte limits.

144/144 HTTP samples qualified. The measured runs recorded 24,636,070 successful replies, 0 unexpected statuses, 0 admissions rejected, 0 wrong body lengths and 0 transport errors. Warmup recorded 0 non-200 responses and 0 transport errors. 0 measured runs lacked warmup counters. The independent SQL audit checked 144 measurements. All 240 tracked process identities exited.

The shared-host timings are local evidence. Production scaling and formal M0 performance acceptance remain open.
