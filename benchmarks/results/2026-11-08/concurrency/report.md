# Single-worker high-concurrency investigation, 8 November 2026

Baseline: local main `52d6905`, including the explicit Handle free-list revert.
Installed CPython 3.13.15 release wheels, compare app `/io`, native loop, one
worker. Only the assigned worktree and venv were modified. Private vgdb prefixes
and PID-scoped controls; no global CPU isolation or throughput qualification.

## Profiling findings

Callgrind slows the loop enough to trigger adaptive admission shedding at c256
and c1024. The original 12,000-request samples cost 60,381 / 47,623 / 57,592 /
64,920 instructions at c1 / c64 / c256 / c1024, but the high-concurrency samples
include 503 responses and replacement connections. These are not successful
request costs. The profile harness now retains the client response distribution
and accepts a private `PROFILE_MAX_IN_FLIGHT` override. Fixed 4,096 runs below
serve every measured request with status 200 and isolate scheduling/transport
cost from the load controller's response to instrumentation overhead.

The largest scaling term was connection-state copying, not the timer heap.
In the original adaptive samples, memcpy is 25.0% of c256 and 31.5% of c1024
instructions, mostly spawning and completing connection tasks. Each task
reserves the TLS branch even for plaintext traffic. In the installed release
binary, Tokio's task-stage copy size falls from `0x3358` (13,144 bytes) to
`0x1be8` (7,144 bytes) when TLS state is allocated only on the TLS branch
(`a839721`). No additional task or change in cancellation/timeout ownership.
TLS connections gain one allocation; plaintext connections gain none.

A rejected experiment boxed the Hyper upgrade and HTTP/2 futures: it did not
reduce the dominant copies and did not improve measured instructions. It is
not committed. No timer/epoll turn batching or GC free-list workaround was
restored. The earlier apparent GC attribution came from Callgrind's overlapping
inclusive Python-frame annotations; direct GC cost was small.

For the longer c1024 baseline (60,000 requests), inclusive timer registration
is 1.08%, heap removal 0.40%, next-timeout calculation below 0.01%, Rust loop
waiting 0.01%, and deadline-FIFO copying 0.04%. Inclusive percentages overlap.
These profiles do not support a major timer-heap or deadline-FIFO rewrite.
The real asyncio Task/Future and context machinery remains a larger cost.

Normalized long-profile components (inclusive, instructions/request):

| Component | Baseline c256 | Baseline c1024 | TLS c256 | TLS c1024 |
| --- | ---: | ---: | ---: | ---: |
| memcpy | 2,272 | 3,602 | 1,812 | 1,896 |
| timer heap pop | 191 | 200 | 192 | 200 |
| schedule_at | 531 | 538 | 531 | 538 |
| socket readiness polling | 444 | 443 | 444 | 444 |
| next_timeout | 3 | 2 | 3 | 2 |
| loop wait_async | 8 | 5 | 8 | 5 |
| deadline FIFO copy | 20 | 20 | 20 | 20 |

## Successful-request instruction counts

2,000 warmups, fixed 4,096 admission, 12,000 measured requests; all 200 responses.

| Connections | Baseline | TLS state separated | Change |
| --- | ---: | ---: | ---: |
| 1 | 60,289 | 60,287 | 0.0% |
| 64 | 47,259 | 46,821 | -0.9% |
| 256 | 49,387 | 47,010 | -4.8% |
| 1024 | 57,500 | 49,152 | -14.5% |

Fresh connections make setup/teardown a substantial part of short high-c
samples. Longer 60,000-request samples amortize that cost:

| Connections | Baseline | TLS state separated | Change |
| --- | ---: | ---: | ---: |
| 256 | 47,243 | 46,815 | -0.9% |
| 1024 | 49,778 | 47,761 | -4.1% |

This is an instruction/memory improvement, not proof of a steady-load
throughput or p99 improvement. Under Callgrind, a 1 ms sleep may expire while
other callbacks run, changing batching relative to native execution. Isolated
throughput qualification still belongs to the orchestrator. Perf was not
installed in this environment. Raw profiles remain local under `raw/`.

## Connection-pinning follow-up and validation

`151066a` moves the 7 KB future into a separately pinned
allocation. Tokio's completed task then stores/moves only its pointer rather
than copying the whole future into an output variant. It adds one allocation
per connection, none per request. No task, abort handle, shutdown watch or
connection-slot ownership changes.

| Connections | Baseline | TLS + pin | Change |
| --- | ---: | ---: | ---: |
| 1 | 60,289 | 60,374 | +0.1% |
| 64 | 47,259 | 46,604 | -1.4% |
| 256 | 49,387 | 46,793 | -5.3% |
| 1024 | 57,500 | 48,232 | -16.1% |

The longer pinned-candidate samples cost 46,799 at c256 and 47,659 at c1024
(-0.9% / -4.3% versus baseline). They are within 0.04% / 0.22% of the TLS-only
samples; the second change chiefly saves connection teardown work.
All the fixed-capacity samples above returned only 200 responses.

The TLS-only installed full check passed 238 Rust tests (2 ignored), 987 Python
tests (34 skipped), 23 harness tests and all static checks. Added a stalled TLS
handshake regression that verifies its deadline releases a single connection
slot before the next TLS request succeeds (focused TLS suite: 8 passed).
Combined pre-merge installed-wheel check passed: 238 Rust tests (2 ignored),
988 Python tests (34 skipped), 23 harness tests; formatting, warnings-denied
Clippy, Ruff and shell syntax passed.

Local main moved to `78a8aef` during this run. It independently contains Pi's
connection-pinning change, identical in behavior to `151066a`. Merge `dd7fb52`
keeps that shared change once and retains the separate TLS-state reduction.
The tables above use `52d6905` as their common baseline and therefore include
both independently implemented changes; do not attribute pinning as a new
change relative to `78a8aef`. Post-merge measurements compare the merged candidate directly against a private
release wheel built from `78a8aef` inside this worktree. This reference wheel was
unpacked for PYTHONPATH profiling; the assigned venv retains the candidate.

| Connections | Current main `78a8aef` | Merged candidate `dd7fb52` | Change |
| --- | ---: | ---: | ---: |
| 1 | 60,243 | 60,273 | +0.05% |
| 64 | 46,939 | 46,903 | -0.08% |
| 256 | 47,350 | 47,150 | -0.42% |
| 1024 | 50,024 | 49,222 | -1.60% |

Fixed 4,096, 12,000 requests each, all 200 responses. Most of the instruction
saving versus the original baseline is already in current main's independently
landed pinning change. The incremental TLS-state reduction chiefly saves memory:
with 1,024 open plaintext connections holding incomplete headers, worker RSS grew
by 24,576 KiB on current main versus 18,432 KiB on the candidate (6 MiB saved).
Each sample used one worker, native loop, max_connections=2048, 60s header timeout;
RSS was read after 0.5s with all connections open. This is a memory smoke sample,
not a throughput claim. The merged candidate's longer c256 sample costs 46,747.
The merged candidate's longer c1024 sample costs 48,005. These longer merged
samples use the same 60,000-request method; no matching long run of `78a8aef` was
performed, so compare them to the original baseline only with the merge caveat.

Final merged installed-wheel check passed 240 Rust tests (2 ignored), 991 Python
tests (34 skipped), 23 harness tests; formatting, warnings-denied Clippy, Ruff
lint/format and shell syntax all passed. The installed package is the merged
candidate. Only GIL-enabled CPython 3.13 ran locally; supported-build CI and
isolated throughput/p99 qualification remain outstanding.
No scheduler semantics, real Task identity/factories, cancellation accounting,
post-delivery background work, or JSON delivery guards were changed. Python
bridge routes remain Tier 2, native health Tier 0; the formal M0 gate stays open.

Reproduce a successful-request profile from this worktree (change connections
and output directory for each sample):

```sh
MATRIX_DIR=$PWD/benchmarks/compare PROFILE_APP=bessemer_app:app \
PROFILE_MAX_IN_FLIGHT=4096 CONNECTIONS=256 BESSEMER_EVENT_LOOP=auto \
PYTHON=/home/cmux/venvs/codex/bin/python OHA=$HOME/.cargo/bin/oha \
PORT=18101 OUT=$PWD/benchmarks/results/2026-11-08/concurrency/raw/repeat-256 \
benchmarks/profile/instructions_matrix.sh /io 12000
```
