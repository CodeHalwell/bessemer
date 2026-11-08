# Sync completion handoff, 8 November 2026

CPython 3.13.15, Linux x86_64, current Pydantic, installed release wheels,
`common:app` from `/home/cmux/matrix`, `FW=bessemer`, one worker,
`BESSEMER_EVENT_LOOP=auto`, one connection, 3,000 measured requests after
2,000 warmup requests. Main baseline is `362f71a`, supplied by the orchestrator
in `/home/cmux/venvs/baseline`. Candidate uses `/home/cmux/venvs/codex`.

| Route | Baseline instructions/request | Candidate | Change |
| --- | ---: | ---: | ---: |
| `/sync` | 211,426 | 157,784 | -25.37% |
| `/bg` | 275,278 | 220,809 | -19.79% |
| `/json` | 33,101 | 33,154 | +0.16% |

A repeat baseline `/bg` measured 275,268 (-0.004%). An intermediate
candidate with the direct completion handoff but still using `asyncio.shield`
measured 171,166 instructions/request on `/sync` (-19.04%).

Worker builds `_lean_plans` only for async handlers. Sync handlers therefore
enter the native invoker through the Python `_invoke` / `_execute_route` fallback,
then `_execute_plan` / `_call_sync`; they do not use native lean result encoding.
That additional orchestration and the mandatory pool hop remain a substantial
gap versus `/json`.

The pool still executes every sync call off the loop with a copied request
context. Completion now posts directly to a loop-owned future, instead of
chaining the executor's concurrent future through `asyncio.wrap_future`.
The internal future declines cancellation so the request Task does not resume
until the actual pool call finishes; cancellation is then delivered to that
Task, preserving acquisition callbacks and capacity accounting without an
additional shield future. Pool startup/shutdown and its thread budget are unchanged.
No changes to Rust or to the public API are needed.

Reproduce each row from the worktree (substitute the appropriate Python and
output directory):

```sh
PYTHON=/home/cmux/venvs/codex/bin/python OHA=$HOME/.cargo/bin/oha \
  CONNECTIONS=1 BESSEMER_EVENT_LOOP=auto PORT=18101 \
  OUT=$PWD/instructions-candidate-sync \
  benchmarks/profile/instructions_matrix.sh /sync 3000
```

The matrix profiler is a copy of the stock script with an external app directory
(`MATRIX_DIR`, default `/home/cmux/matrix`), a private vgdb prefix, and PID-scoped
reset/dump commands. Stock global `callgrind_control` commands otherwise reset
other agents' counters. Initial `matrix-base-*` profiles were discarded because
concurrent global resets could contaminate them. The accepted runs use
`matrix-baseline-*`, `matrix-candidate-sync` (intermediate) and `matrix-final-*`.
Raw profiles and strace outputs are retained locally under `raw/` (gitignored).

Strace `-f -c` over startup, 2,000 warmup requests at eight connections and
10,000 measured requests at one connection still shows the thread handoff:
baseline/candidate futex counts 73,125/77,357, epoll waits 41,119/41,108,
sendto 23,495/23,410, recvfrom 32,541/32,570. Traced load-window wall times were
6.87/6.38 seconds. These scheduler-dependent measurements on a busy shared
machine do not establish a syscall reduction, latency improvement or throughput
acceptance. The change removes user-space orchestration rather than the pool hop.

## Remaining scope

BackgroundTasks uses the same improved sync handoff, and its existing ordered
sync/async execution is preserved. **Buffered background tasks still run before
the response is sent**, contrary to the task's requested FastAPI timing and the
original specification. That existing gap is already documented in
`docs/fastapi-parity.md`; this patch does not close it. Streams run their tasks
on EOF, before the stream finishes. Correct post-send execution requires a
transport completion/abandonment hook carrying admission through cleanup for
HTTP/1, HTTP/2, HEAD, disconnect and shutdown, plus ownership through middleware
response replacement. Starting detached tasks from the worker or merely
wrapping the response in a stream would not provide those guarantees.

Native direct encoding from a pool thread, submission/completion batching,
spinning workers and a native pool were not implemented. A native pool may
remove the remaining executor concurrent-future allocation but must retain
context isolation, shutdown and free-threaded synchronization guarantees.
Python routes remain Tier 2 and the formal M0 gate remains open.

## Validation

The release wheel was built with `CARGO_BUILD_JOBS=3` and installed with `uv pip`
into the assigned venv before integration tests. The required full check passed
Rust formatting, warnings-denied Clippy, 235 Rust tests (2 ignored), Ruff checks
and formatting, 943 Python tests run (34 skipped), 23 harness tests, and shell
syntax checks. A later repeated-cancellation regression and exception subcases
(SystemExit, KeyboardInterrupt, StopIteration) passed focused testing; the repeated full
check passed the final 943 Python tests (34 skipped), 235 Rust tests (2 ignored)
and 23 harness tests, with all required static checks green. No free-threaded interpreter was
available in this assigned environment. Raw validation logs are retained locally.
