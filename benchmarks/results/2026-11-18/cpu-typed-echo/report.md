# CPU and typed-echo performance

Latest main `bbc07ca` was compared with prior main `f0ec5d3`. The candidate was compared with latest main and FastAPI 0.162.2. The candidate wheels are identified by the source manifest and diff hashes in the provenance.

One local ARM64 macOS host ran six rotated rounds per case with one server worker, a one-second warmup and a four-second measured load. CPU cases used the same 50,000-iteration checksum and four configured sync slots. CPython 3.13.18 and 3.14.7 ran with the GIL enabled. Free-threaded 3.14.7 ran with the GIL disabled and `PYTHON_GIL` unset. JIT was disabled.

Cells show median successful replies/s, median p99 ms and median server CPU µs per successful reply, in that order. Ratio cells use the same order and give the median and full range of within-round ratios. Ratios require six eligible pairs.

## Findings and changes

The baseline rerun qualified all 72 samples. The 64-client Python 3.13 sync
CPU case was 0.993× prior main at the paired median (0.967–0.997, lower in
all six rounds). Standard Python 3.14 typed echo was 0.943×
(0.851–1.031, lower in five rounds) and used 1.061× CPU per reply. The other
four target cases crossed throughput parity.

Latest-main stack samples showed Python big-integer arithmetic on the inline
CPU route and GIL waits on the four sync workers. The identical checksum run
outside HTTP had a main/prior-main thread CPU ratio of 0.957 (0.898–1.028).
Both 3.14 echo captures reached the native model parser and encoder. The
profiles and checksum control leave the cause of the historical differences
uncertain.

The sync pool now signals its condition variable only while a worker is
waiting. Active and starting workers check the queue under its mutex. The
native invoker interns the names of frequently called sync dispatch methods.
The typed model plan stores the plain JSON key and colon together, so the parser
can match an expected field in place and scan whitespace after its colon once.
The general key parser still handles reordered and escaped keys, aliases and
whitespace before the colon. Existing parser tests now cover additional
whitespace and malformed separators.

Direct native parser CPU ratios for the canonical 20-item payload were 0.976×
on CPython 3.13, 1.013× on standard 3.14 and 0.971× on 3.14t with the GIL
disabled. All six-pair ranges crossed parity. The test uses the same private
parser and compiled model plans as the HTTP case, with server and handler work
excluded.

### Candidate results

All 144 matrix samples qualified, with 15,790,508 successful replies and zero
measured transport, status, body-length or warmup errors. Sync CPU at 64
clients reached a 549 req/s median against 516 on latest main and 498 on
FastAPI. Its paired candidate/main throughput median was 1.029× (0.980–1.231)
and CPU per reply was 0.988× (0.856–1.012). The 16-client CPU cases were near
parity. JSON and models256 controls also had ranges crossing parity.

Standard 3.13 and 3.14 typed-echo paired throughput ratios had medians of 0.960× and
0.957× main. The slowest 3.14 candidate sample ran with observed host CPU
utilisation of 82.5–99.7%. Its paired main sample reached 70.5%. That pair
remains in the matrix and its timing cause is unconfirmed. All 24 separate
recheck samples qualified with 5,956,105 successful replies and zero errors.
The exact same candidate then measured 1.037× main on 3.13 and 1.010× on 3.14,
with CPU per reply at 0.966× and 0.991×. The ranges crossed parity in both
the matrix and the recheck. Free-threaded typed echo in the matrix was
0.989× (0.964–1.119). The observations support removing the identified work
and leave the size and direction of the net echo throughput change uncertain.

## Installed validation

The installed check ran 350 Rust tests successfully with two ignored, and
1,205 Python cases with 25 skips. Eight Python cases hit macOS client socket
setup error `OSError(49)` during the full invocation. The original failure log
is retained. All eight cases passed unchanged on an isolated rerun, followed
by the previously unreached 83 harness cases (three skips) and six shell
syntax checks. The first 3.12 focused run also hit socket setup errors. After a
cooldown, all five wheel configurations ran 254 focused cases without failure,
with two skips per standard configuration and 21 per free-threaded GIL mode.

The first 72 attempts are retained in the sample file. The recorder overwrote the warmup results, leaving those attempts ineligible. The corrected rerun supplies the baseline comparison.

## Latest main compared with prior main

| Case | Prior main | Latest main |
| --- | --- | --- |
| CPU inline · 3.13 · c16 | 548 / 39.62 / 1734.1 | 547 / 38.84 / 1739.4 |
| CPU sync · 3.13 · c16 | 569 / 47.62 / 1709.4 | 565 / 49.26 / 1709.6 |
| CPU sync · 3.13 · c64 | 571 / 177.39 / 1702.3 | 562 / 177.23 / 1715.6 |
| Typed echo · 3.13 · c64 | 66,208 / 2.40 / 13.8 | 70,025 / 2.40 / 13.2 |
| Typed echo · 3.14 · c64 | 65,497 / 2.54 / 13.9 | 61,453 / 2.50 / 14.9 |
| Typed echo · 3.14t GIL off · c64 | 57,449 / 2.75 / 16.2 | 56,454 / 2.82 / 16.3 |

| Case | Latest main / Prior main |
| --- | --- |
| CPU inline · 3.13 · c16 | 1.006× (0.965–1.033) / 0.990× (0.935–1.012) / 0.998× (0.979–1.032) |
| CPU sync · 3.13 · c16 | 1.009× (0.867–1.033) / 1.010× (0.842–1.211) / 0.991× (0.977–1.106) |
| CPU sync · 3.13 · c64 | 0.993× (0.967–0.997) / 1.010× (0.922–1.027) / 1.010× (0.994–1.024) |
| Typed echo · 3.13 · c64 | 1.044× (0.910–1.117) / 0.980× (0.924–1.116) / 0.962× (0.900–1.083) |
| Typed echo · 3.14 · c64 | 0.943× (0.851–1.031) / 1.023× (0.917–1.125) / 1.061× (0.979–1.153) |
| Typed echo · 3.14t GIL off · c64 | 0.982× (0.897–1.222) / 1.029× (0.857–1.102) / 1.006× (0.831–1.106) |

## Candidate compared with latest main and FastAPI

| Case | Latest main | Candidate | FastAPI |
| --- | --- | --- | --- |
| CPU inline · 3.13 · c16 | 551 / 39.43 / 1723.5 | 549 / 38.96 / 1734.5 | 524 / 62.64 / 1806.3 |
| CPU sync · 3.13 · c16 | 548 / 49.12 / 1775.2 | 559 / 50.01 / 1729.3 | 509 / 45.78 / 1879.0 |
| CPU sync · 3.13 · c64 | 516 / 189.74 / 1846.4 | 549 / 187.11 / 1748.4 | 498 / 158.85 / 1892.8 |
| Typed echo · 3.13 · c64 | 56,458 / 2.83 / 15.8 | 55,355 / 2.82 / 16.3 | 9,468 / 12.79 / 94.3 |
| Typed echo · 3.14 · c64 | 56,352 / 2.84 / 16.1 | 51,466 / 3.03 / 17.5 | 8,843 / 14.22 / 101.4 |
| Typed echo · 3.14t GIL off · c64 | 51,718 / 2.89 / 17.8 | 52,572 / 2.94 / 17.5 | 9,209 / 12.97 / 97.7 |
| JSON control · 3.13 · c64 | 108,297 / 1.67 / 8.4 | 103,312 / 1.74 / 8.7 | 16,143 / 7.97 / 55.2 |
| 256-model control · 3.14 · c64 | 31,899 / 3.90 / 29.1 | 33,026 / 3.76 / 28.3 | 4,702 / 23.29 / 193.7 |

| Case | Candidate / Latest main | Candidate / FastAPI |
| --- | --- | --- |
| CPU inline · 3.13 · c16 | 0.993× (0.923–1.065) / 0.994× (0.919–1.172) / 1.009× (0.948–1.070) | 1.059× (1.034–1.088) / 0.606× (0.567–0.667) / 0.953× (0.923–0.974) |
| CPU sync · 3.13 · c16 | 1.005× (0.962–1.141) / 0.993× (0.823–1.057) / 0.996× (0.917–1.036) | 1.106× (1.080–1.143) / 1.066× (0.932–1.147) / 0.919× (0.894–0.940) |
| CPU sync · 3.13 · c64 | 1.029× (0.980–1.231) / 0.983× (0.823–1.083) / 0.988× (0.856–1.012) | 1.099× (1.039–1.158) / 1.154× (1.008–1.291) / 0.922× (0.883–0.961) |
| Typed echo · 3.13 · c64 | 0.960× (0.886–1.227) / 1.061× (0.801–1.097) / 1.039× (0.846–1.120) | 5.678× (5.408–6.314) / 0.223× (0.200–0.239) / 0.178× (0.161–0.186) |
| Typed echo · 3.14 · c64 | 0.957× (0.597–1.095) / 1.093× (0.905–2.345) / 1.039× (0.918–1.442) | 5.816× (4.529–6.270) / 0.218× (0.202–0.389) / 0.171× (0.161–0.194) |
| Typed echo · 3.14t GIL off · c64 | 0.989× (0.964–1.119) / 0.993× (0.826–1.032) / 1.003× (0.908–1.039) | 5.577× (5.318–6.154) / 0.225× (0.183–0.263) / 0.181× (0.164–0.192) |
| JSON control · 3.13 · c64 | 0.986× (0.876–1.139) / 1.023× (0.656–1.668) / 1.020× (0.904–1.142) | 6.271× (5.528–6.469) / 0.244× (0.172–0.274) / 0.161× (0.157–0.179) |
| 256-model control · 3.14 · c64 | 1.005× (0.926–1.103) / 1.010× (0.921–1.064) / 0.998× (0.922–1.071) | 6.878× (6.220–7.777) / 0.158× (0.144–0.180) / 0.149× (0.132–0.164) |

## Typed-echo recheck

A separate six-round run compared latest main and the candidate on standard CPython. The candidate comparison above retains every original round.

| Case | Latest main | Candidate |
| --- | --- | --- |
| Typed echo · 3.13 · c64 | 61,582 / 2.67 / 14.8 | 63,218 / 2.69 / 14.3 |
| Typed echo · 3.14 · c64 | 61,412 / 2.68 / 14.7 | 62,246 / 2.62 / 14.5 |

| Case | Candidate / Latest main |
| --- | --- |
| Typed echo · 3.13 · c64 | 1.037× (0.972–1.183) / 0.997× (0.891–1.132) / 0.966× (0.848–1.025) |
| Typed echo · 3.14 · c64 | 1.010× (0.942–1.084) / 1.017× (0.918–1.066) / 0.991× (0.925–1.061) |

## Measurement and comparison counts

| Run | Qualified | Successful replies | Non-200 replies | Wrong lengths | Transport errors | Missing warmups |
| --- | --- | --- | --- | --- | --- | --- |
| Initial recorder attempt | 0/72 | 9,351,284 | 0 | 0 | 0 | 72 |
| Corrected baseline rerun | 72/72 | 9,084,040 | 0 | 0 | 0 | 0 |
| Candidate comparison | 144/144 | 15,790,508 | 0 | 0 | 0 | 0 |
| Typed-echo recheck | 24/24 | 5,956,105 | 0 | 0 | 0 | 0 |

The sample file retains each timed attempt and its eligibility reason. The provenance records the wheel, source, validation, protocol and SQL audit hashes and separates the HTTP, profile and function-level evidence. The timings describe the shared local host.
