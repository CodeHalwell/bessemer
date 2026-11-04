# Qualification evidence — 4 November 2026

This record distinguishes executed checks from release gates. All measurements
in this session use one shared Linux host. They do **not** establish formal M0
performance acceptance or general production readiness.

## Environment and preserved inputs

- Linux x86_64, CPython 3.12.14 with a conventional GIL build.
- Five visible logical CPUs; cgroup quota `400000 100000` permits four CPUs.
- Memory cgroup limit: 16 GiB.
- M0 server pinned to logical CPU 0; one-core `oha` load pinned to CPU 1.
- CPU frequency policy, physical topology and exclusive host tenancy were not
  controlled. Server and load share the same kernel and cgroup.
- The pre-change M0 executable was copied before rebuilding. SHA-256:
  `ab620cc41ac1dba1b2b86ab3e956cca9ae14e7b0971b7d113d3cfeeac8d1ec96`.
- The initial installed foundation wheel was copied into an isolated environment.
  Wheel SHA-256:
  `0dc937d8997ccaea3a7b15db45d411183098432e413cdde99d36241dde5a1961`.
- `oha` 1.16.0 Linux amd64 SHA-256:
  `620bb9e16fb53eabc9a3fc45f88bdb41fefa3fee5c05e75892011ce320391716`.
- `wrk2` source commit: `44a94c17d8e6a0bac8559b53da76848e430cb7a7`.
  The bundled LuaJIT ELF bytecode writer truncated its exported module symbol
  with this toolchain. Regenerating the same embedded module as C and linking
  without PIE fixed tool startup. `scripts/qualify_tools.sh` records these steps.

No free-threaded Python scaling result is available from this environment.

## CPython component costs

`benchmarks/python_cost.py` captured seven calibrated repeats on CPU 4 using
CPython 3.12.14 with the GIL and Pydantic 2.13.4. These are Python operation
costs including the common callable/timeit loop overhead; values are not
baseline-subtracted. They exclude PyO3, Rust serialization and HTTP, and cannot
establish the formal M0 per-request budget or a free-threaded scaling result.

| Operation | Median | Repeat spread |
| --- | ---: | ---: |
| No-op Python call | 13 ns | 14.33% |
| Return an existing dictionary | 14 ns | 3.25% |
| Allocate a two-field dictionary | 40 ns | 1.55% |
| Create and close an unstarted coroutine | 52 ns | 13.34% |
| Complete coroutine, handle StopIteration, extract two values | 185 ns | 2.11% |
| Pydantic validation of an existing two-field dictionary | 519 ns | 2.71% |
| Pydantic model dump to JSON-compatible dictionary | 463 ns | 2.48% |

The shared-host spread is retained rather than being presented as deterministic
instruction-count evidence. Allocation patterns describe the code paths; no
allocator event counts are claimed. Formal PyO3 cost profiling and the
specification's Callgrind/perf gates remain open.

## Original M0 throughput: invalid for acceptance

Executed 10:00–10:11 UTC after 30 seconds of warmup for each mode. Each row is
one 60-second sample using HTTP/1.1, keep-alive, 50 connections and no pipelining.
The runner alternated mode order between samples. Startup verified the exact
JSON body and mode marker; every load sample reported only HTTP 200 and no
request errors.

| Sample | Rust requests/second | Python requests/second |
| --- | ---: | ---: |
| 1 | 177,314 | 179,733 |
| 2 | 173,472 | 184,293 |
| 3 | 177,838 | 177,092 |
| 4 | 173,017 | 179,121 |
| 5 | 169,437 | 180,918 |
| Median | 173,472 | 179,733 |
| Spread `(max-min)/median` | 4.84% | 4.01% |

Both spreads exceed the specification's 3% ceiling. The runner exited 2 and
marked `samples_valid=false`. The arithmetic Python/Rust ratio is 1.036, but this
does not validate the 60% target or overturn the original 40% stop criterion.
The baseline was rejected for repeatability. The load generator process was
CPU-saturated: sampled Rust process CPU use was about 86–88% of one logical CPU
while `oha` used about 99.7%. Its capacity may have limited the Rust result, but
process counters do not account for all softirq or other work on the server
core, so they do not establish core headroom or a single bottleneck. Python used
about 98–99% with its load generator also near 100%. CPU capture began partway
through sample 1; complete later intervals were used without treating missing
intervals as zero.

A short diagnostic probe gave `oha` two load CPUs/threads, retaining CPU 0 for
the server. Rust reached 133,110 requests/second and Python 123,938, with server
CPU near 100%. These lower rates demonstrate sensitivity to the shared-host
topology and scheduling; the 30-second probe has no repeatability claim and its
ratio is not a performance-thesis result.

Raw evidence is retained in `benchmarks/results/2026-11-04/` once each run
finishes. Session working copies, executable fingerprints, commands, timestamps
and CPU sampling are under `/workspace/work/qualification/`.

## Corrected fixed-rate latency

The `wrk2` runner uses constant requested rates at 25%, 50% and 75% of the local
Rust median, records delivered/requested rate, rejects socket/status failures or
delivery below 97%, and parses the corrected `Recorded Latency` HdrHistogram.
The throughput reference above is invalid for release acceptance, so these
rates are exploratory workload points. Each mode/rate receives one 60-second
sample, not the full release repetition schedule.

The tool resets its latency histogram after calibration. Total completed
requests include calibration; histogram population is separately recorded.
Default textual percentiles are rounded. For initial runs, p95 is an upper
bound from the first printed histogram row at or above 95%; subsequent runs use
`benchmarks/latency.lua` to obtain exact corrected histogram percentile lookups.
The runner checks startup endpoint contents separately; `wrk2` checks HTTP
status/socket delivery and does not validate each response body.

Executed 10:15–10:21 UTC with the original M0 binary and load pinned to CPUs
1–2, two threads and 50 connections. An initial tool-startup attempt failed
before making requests; it is excluded from the table.

| Requested rate | Mode | Delivered rate | p95 upper bound | p99 | p99.9 |
| ---: | --- | ---: | ---: | ---: | ---: |
| 43,367/s | Rust | 43,323/s | 1.482 ms | 1.880 ms | 3.860 ms |
| 43,367/s | Python | 43,323/s | 1.545 ms | 1.930 ms | 6.260 ms |
| 86,735/s | Rust | 86,647/s | 1.928 ms | 2.380 ms | 5.590 ms |
| 86,735/s | Python | 86,646/s | 2.095 ms | 2.640 ms | 7.780 ms |
| 130,103/s | Rust | 129,969/s | 5.491 ms | 24.850 ms | 43.620 ms |
| 130,103/s | Python | 127,896/s | 936.959 ms | 1,010 ms | 1,020 ms |

All six runs reported no socket or non-success status errors and delivered at
least 97% of the requested rate. At the largest workload, Python's corrected
tail latency rose to approximately one second while delivery fell to 98.30%.
This is evidence of a local tail-latency problem near that offered load, not an
accepted saturation reference or a general deployment capacity guarantee.
These six points use the original M0 executable; the framework checkpoint is
measured separately below.

## Framework checkpoint capacity and normal-route latency

The immutable foundation wheel and corrected `0.2.0a2` wheel were compared
12:05–12:12 UTC. Each configuration used 50 keep-alive connections, five seconds
of warmup and three 20-second samples per endpoint. Server affinity was CPU 0
or CPUs 0–1; external `oha` ran on CPU 2. Existing functional soaks remained on
CPUs 3–4. Other agents paused builds, scans and load for this window.

| Artifact | Server workers / CPUs | JSON median | JSON spread | Awaited 1 ms I/O median | I/O spread |
| --- | ---: | ---: | ---: | ---: | ---: |
| Foundation | 1 / 1 | 55,392/s | 4.80% | 23,170/s | 1.66% |
| Corrected a2 | 1 / 1 | 27,508/s | 0.33% | 16,419/s | 1.84% |
| Corrected a2 | 2 / 2 | 51,291/s | 28.75% | 19,557/s | 3.34% |

All samples reported only HTTP 200, no socket errors, stable worker populations,
clean shutdowns and no server error markers. The load generator used about
10–34% of its pinned core; JSON server CPU was about one or two cores for the
respective configurations. Recorded cgroup throttling increased by zero in
all three configurations. The one-core hardened checkpoint has substantially
lower throughput than the foundation in these workloads. The two-core JSON
samples were 37,321, 52,066 and 51,291/s, so their large spread prevents a stable
scaling conclusion. This short shared-host comparison does not apply the M0
acceptance criteria or establish free-threaded scaling or production capacity.

A separate one-worker, one-core `wrk2` diagnostic ran 12:13–12:16 UTC at 25%,
50% and 75% of the short foundation JSON median. Each point received one
30-second sample, including calibration. Exact corrected HdrHistogram
percentiles and post-calibration population counts are retained in raw output.

| Requested rate | Artifact | Delivered / requested | Corrected p95 | Corrected p99 | Delivery check |
| ---: | --- | ---: | ---: | ---: | --- |
| 13,847/s | Foundation | 99.59% | 1.778 ms | 2.129 ms | Pass |
| 13,847/s | Corrected a2 | 99.59% | 2.739 ms | 5.455 ms | Pass |
| 27,695/s | Foundation | 99.59% | 2.475 ms | 4.077 ms | Pass |
| 27,695/s | Corrected a2 | 96.61% | 873.471 ms | 897.535 ms | Fail |
| 41,543/s | Foundation | 96.66% | 4.467 ms | 8.807 ms | Fail |
| 41,543/s | Corrected a2 | 63.67% | 10,485.759 ms | 10,731.519 ms | Fail |

All six points reported no socket or non-success status errors. The three
delivery failures remain invalid measurements of the requested workload; their
corrected tails describe overload, not an accepted latency SLO. The runner
exited one to preserve these failures. No later release candidate inherits
these results. Raw commands, wheel fingerprints, process CPU/RSS, source
snapshots and output are in `benchmarks/results/2026-11-04/framework-capacity-a2/`
and `benchmarks/results/2026-11-04/framework-latency-a2/`.

## Functional soak and process evidence

The installed-wheel harness is independent of the server process. Its scheduled
HTTP workload validates status codes and JSON response contents for async JSON,
awaited I/O, JSON creation, expected exceptions, async deadline cancellation and
sync work continuing after its deadline. Expected HTTP 409 and 504 responses
are counted separately from unexpected errors. The workload does not treat an
expected deadline response as successful cancellation cleanup by itself;
long-running capacity and process behavior provide additional evidence.

Samples retain all request latency counts, scheduled-to-completion latency,
generator lag, errors/examples and the complete current server process tree's
RSS, anonymous RSS, CPU time and thread count. RSS is summed over processes and
therefore includes shared pages more than once. The same-host Python generator
is suitable for functional soak, not a saturation or corrected-latency claim.
Its latency population includes deliberate 25-millisecond deadline paths and
therefore is not a normal-route latency SLO.

A five-second harness smoke completed 200 requests with zero unexpected errors.
A second provenance smoke compared all six installed Python/native payload
files byte for byte with the preserved wheel, completed 201 requests with zero
unexpected errors, and shut down cleanly.

The later cleanup smoke explicitly observed five async handlers started,
cancelled and fully cleaned up, and five timed-out sync threads completing.
Future runs collect these counters from every live worker through real HTTP
after stopping load. Counters must be nonnegative integers and balance; missing
or incomplete state cannot pass. A separate failure run can use
`--kill-worker-at 30 --duration 90 --server-workers 2`; it retains every HTTP
error and reports how quickly a replacement PID actually served a request.
The two-hour clean soak does not inject this fault.

An early candidate wheel, SHA-256
`21a3979100126529bc5f5b8a257c42e32a7f33496344e1122eacb05ceff8a78a`,
completed a 90-second, 100 requests/second, two-worker fault run. Of 9,001
requests, six reported `RemoteDisconnected` when one worker received SIGKILL;
no further request errors followed. A replacement PID served an independent
HTTP probe within 0.720 seconds. The surviving worker balanced 120 async
start/cancel/cleanup counts and 120 sync completions; the replacement balanced
62 of each. Parent and children shut down in 0.064 seconds without forced kill
or orphan cleanup. The harness exited 1 to retain the injected-fault HTTP errors;
this is recovery evidence, not a zero-error result. An earlier exploratory
version of the fault run is also retained and clearly separated.

The load generator was corrected after the first fault run: every persistent
connection now cycles through all request cases, and connections recycle after
100 requests so replacement workers receive load. Previously, using a global
sequence modulo 40 with eight connections assigned timeout cases to only two
connections. This correction changes load distribution, not the set of cases.

The extended candidate profile additionally checks constrained Pydantic input
and 422 responses, response-model filtering of a private field, Request and
query injection, async dependency finalizers, background task completion,
explicit response headers/cookies and OpenAPI. Its 30-second, 3,001-request
smoke had zero unexpected errors and balanced all cleanup counters in both
workers. A two-hour extended soak on the immutable early wheel started at
10:41 UTC and is still running. Its result applies to that wheel, not later
builds.

The final qualification environment is prepared without system-site packages.
Its five Pydantic dependency wheels are pinned by version and SHA-256 in
`benchmarks/qualification-cp312-linux-x86_64.lock`, installed offline with
`--require-hashes`, and recorded in a dependency manifest. A 501-request smoke
in that environment passed strict JSON type checks, response header/cookie
checks and both workers' cleanup counters. The earlier exploratory environment
inherited stable base-runtime dependencies; it was isolated from the mutable
build environment but did not freeze all transitive package bytes.

The immutable foundation two-hour soak completed 10:13–12:13 UTC at 100 requests
per second with eight load threads. It made 720,002 HTTP responses with zero
transport errors and nine unexpected HTTP 200 responses from a sync handler
whose 40-millisecond work has a 25-millisecond deadline. Eight occurred
11:29:56–11:30:06 UTC, and one at 11:59:24 UTC, overlapping Docker builds;
scheduling interference is a possible trigger, not an established cause.
The harness exited one. Server exit was zero, shutdown took 0.031 seconds, and
RSS rose from 17.95 to 18.38 MiB. Its post-warmup sampled slope of +0.11 MiB/hour
is descriptive, not a leak verdict. This older harness did not collect the later
per-worker cancellation or lifespan markers. The raw report and separate
postchecks are in `benchmarks/results/2026-11-04/foundation-soak-2h/`; this is a
completed failed-response soak. Candidate qualification uses separate immutable
wheels and environments. A running soak is not counted as a completed result.

The `0.2.0a1` checkpoint, core commit `c6930e8` plus Docker-context commit
`ff19784`, started its extended two-worker soak at 11:30:27 UTC. Its copied wheel
SHA-256 is
`f3ee994687cb4ebdb9dc4a74ce504ad6016d82b8fcdbc2b098d1e92d8ea473fb`.
Installed package bytes and the pinned dependency manifest were checked before
startup. This run is scheduled to finish at 13:30 UTC and is still in progress.
Independent container tests found a lifespan cleanup defect in this checkpoint:
the parent can close the heartbeat reader during drain and inject a channel
error into worker cleanup. This soak continues as evidence about that artifact;
parent exit zero alone cannot establish normal lifespan completion. A corrected
heartbeat or outbound-budget build must be qualified as a separate artifact.
Future clean-soak reports also reject worker error/critical logs, Python
tracebacks and forced-drain/shutdown messages. Earlier runs receive this check
separately after completion, preserving their original report and harness.
The final extended fixture additionally records each worker's lifespan start
and normal completion using its PID and a unique instance identifier. Cleanup
waits 0.65 seconds after the lifespan yield, crossing the 0.5-second heartbeat
interval while remaining within the two-second grace period. Every started
instance must record normal completion and balanced final counters; PID reuse
cannot pair another instance's markers. These markers apply only to new runs.
A 20-second control run on the known-defective `0.2.0a1` wheel completed 2,001
valid HTTP responses and both lifespan finalizers, while both workers printed
heartbeat-channel errors and the parent exited zero. The corrected guard
rejected the run with exit one; its raw logs, fixture and source snapshots are
retained in `benchmarks/results/2026-11-04/a1-heartbeat-log-rejection/`.

The corrected `0.2.0a2` checkpoint, commit `ac8b397`, is a separate immutable
wheel with SHA-256
`4f3dde488ea683235d16db8c050315c2dc7426210ee0b4edc4bcbf24ae81a2a2`.
Its identical 20-second control completed 2,001 expected HTTP responses, both
workers' cancellation, sync, dependency and background counters, and both
normal lifespan finalizers. Server logs contained zero error markers; parent
and harness exited zero after 0.765-second shutdown. Raw evidence is retained
in `benchmarks/results/2026-11-04/a2-lifespan-positive-smoke/`.
Its extended two-hour soak started at 11:58:27 UTC and is scheduled to end at
13:58 UTC. It is still running and does not qualify any later outbound-budget
candidate.

A separate a2 90-second fault check used a Linux pidfd to kill one worker while
100 requests/second continued. Of 9,001 attempts, two transport failures occurred
at the fault; none followed. A replacement PID served an independent HTTP probe
within 1.218 seconds. Survivor and replacement balanced 136/66 async cancellation
and sync counts, plus 134/69 dependency/background counts, and both normal
lifespan finalizers completed. Parent exit was zero, shutdown took 0.765 seconds,
and no orphan cleanup was needed. Harness exit one preserves the two failures,
the supervisor's error log and the killed instance's missing normal finalizer.
This is recovery evidence, not a clean soak; raw artifacts are in
`benchmarks/results/2026-11-04/a2-pidfd-worker-fault/`.

The final `0.2.0a3` wheel is preserved separately with SHA-256
`5737cc939d90152e5316a387a21a9b84d8af650d7d1370361ff3cb6fc6cd5790`.
Its isolated interpreter's installed package bytes and pinned dependency
versions were verified before launch. Its 20-second positive control completed
2,001 expected responses, both workers' cleanup counters and normal lifespan
markers, with zero server error lines and parent/harness exit zero. Raw evidence
is in `benchmarks/results/2026-11-04/final-a3-positive-control/`.
Its separate two-hour extended soak started at 12:34 UTC and is still in
progress, scheduled to finish at 14:34 UTC. The a2 results above do not establish
this artifact's qualification.

Its own 90-second, 100 requests/second, two-worker fault run completed 9,001
attempts with two `RemoteDisconnected` errors at the controlled SIGKILL and no
later errors. A replacement PID served HTTP within 0.493 seconds. Survivor and
replacement balanced 168/21 async cancellation and sync counts, and 168/19
dependency/background counts; both normal finalizers completed. Parent exit was
zero and shutdown took 0.765 seconds without orphan cleanup. Harness exit one
retains the injected-fault errors, supervisor error log and the killed instance's
missing normal-finalizer marker. This remains recovery evidence, with raw
artifacts in `benchmarks/results/2026-11-04/final-a3-worker-fault/`.

## Reproduction and remaining release gates

Install tools explicitly into a new directory (requires Linux x86_64, `curl`,
Git, a C compiler, Make and OpenSSL development headers):

```sh
scripts/qualify_tools.sh /absolute/path/to/new-tools
cargo build --release --bin bessemer-m0
python3 benchmarks/qualify_m0.py \
  --binary "$PWD/target/release/bessemer-m0" \
  --oha /absolute/path/to/new-tools/oha \
  --output /absolute/path/to/new-throughput-results
```

For corrected latency against already running endpoints:

```sh
python3 benchmarks/qualify_latency.py \
  --wrk2 /absolute/path/to/new-tools/wrk2-src/wrk \
  --rust-url http://SERVER:RUST_PORT/ \
  --python-url http://SERVER:PYTHON_PORT/ \
  --throughput-report /absolute/path/to/new-throughput-results/report.json \
  --output /absolute/path/to/new-latency-results
```

`--allow-invalid-baseline` is an explicit exploratory override and never grants
acceptance. The throughput recorder also accepts already running endpoints on
a separately provisioned server. It requires a server manifest and records its
claims without treating them as independently verified:

```sh
python3 benchmarks/qualify_m0.py \
  --rust-url http://SERVER:RUST_PORT/ \
  --python-url http://SERVER:PYTHON_PORT/ \
  --server-manifest /absolute/path/to/completed-server-manifest.json \
  --oha /absolute/path/to/new-tools/oha \
  --output /absolute/path/to/new-remote-throughput-results
```

Start with `benchmarks/server-manifest.example.json` and replace placeholders
with measured evidence. The original
`benchmarks/measure_m0.py RUST_URL PYTHON_URL` contract remains available.
For a release, execute the full
30-second warmup and five alternating 60-second samples on representative,
separate hosts with a dedicated pinned server core and controlled CPU policy;
repeat until both spreads are at most 3%, then apply the unchanged 40% stop and
60% target criteria. Repeat fixed-rate latency with the accepted saturation
reference and the supported deployment topology. Free-threaded scaling,
representative ingress/TLS and real application dependency failures remain
separate gates. The specified 24-hour RSS soak and sanitizer/Valgrind/ThreadSanitizer
coverage are not satisfied by a two-hour same-host run.

For a functional soak, create a dedicated venv, install an immutable wheel,
then run:

```sh
python3 scripts/qualify_service.py \
  --python /absolute/path/to/venv/bin/python \
  --wheel /absolute/path/to/bessemer.whl \
  --output /absolute/path/to/new-soak-results \
  --duration 7200 --rate 100 --server-workers 2
```

Omit `--server-workers` only for the old foundation wheel, whose CLI predates
supervision. Future runs verify installed executable payload bytes before
starting and preserve the interpreter's venv path. The initial two-hour run
started before this automated check was added; its wheel/environment were
created separately and the later provenance smoke verified that environment.
Use `--profile extended` for the candidate API surface described above. Each
new run preserves its fixture and exact harness/workload source snapshots;
the workload clock stops before post-load cleanup probes and shutdown.
`benchmarks/summarize_soak.py REPORT.json` reports delivery, complete process
population, sampled CPU and descriptive RSS slope without declaring leak
freedom or accepting an incomplete run.

For a separate short framework-capacity diagnostic, use a fresh output folder:

```sh
python3 benchmarks/qualify_http.py \
  --python /absolute/path/to/venv/bin/python \
  --wheel /absolute/path/to/bessemer.whl \
  --oha /absolute/path/to/new-tools/oha \
  --workers 2 --server-cpus 0,1 --load-cpu 2 \
  --output /absolute/path/to/new-framework-capacity-results
```

This measures `/json` and awaited `/io` through the installed compatibility
bridge using an external load process. Defaults are five seconds of warmup and
three 20-second samples with 50 persistent HTTP/1.1 connections. It captures the
whole process population and CPU/RSS, rejects HTTP/socket errors and unexpected
worker replacement, and records medians/spreads. These short shared-host runs
do not replace the formal M0 schedule. Omit `--workers` only for the old
foundation CLI. Keep this saturation run separate from the clean functional
soaks; their load generators and servers were moved to CPUs 3–4 before the
capacity window, with per-thread affinity changes retained alongside raw data.
