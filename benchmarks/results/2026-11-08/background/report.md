# Delivery-owned BackgroundTasks, 8 November 2026

Baseline local main `2cd06ee`, fast-forwarded before work. CPython 3.13.15,
installed release wheels, native loop, matrix common:app, one worker, fixed
admission 4096. Private vgdb prefixes and PID-scoped controls for every profile;
6,000 requests after 2,000 warmup, c1. Every measured response was 200.

## Attribution and changes

Matrix /bg is an async handler taking BackgroundTasks, returning {"ok": true}
and adding a fresh synchronous no-op lambda. Matrix /json and /sync return
{"ok": true, "count": 42}. Subtracting /sync also includes differences in
binding/orchestration and the small response body, rather than measuring one
task in isolation. The matched probes below control the response shape.

1. `a4fc264`: a small JSON response with deferred tasks previously lost the
   compact reply path because JsonReply could not carry transport extensions.
   JsonReply now owns AfterResponse and admission. HTTP/1 starts work after
   write_all succeeds. Failure, cancellation or abandoned replies start cleanup
   with sent=false; the single shared guard prevents duplicate execution. HTTP/2
   conversion preserves the guard as a response extension. Profile /bg:
   188,267 → 184,363 instructions (-2.1%).
2. `2fe85a3`: generic synchronous work still allocated Python invoke/settle
   closures and a thread-safe callback handle per pool hop. A native PoolInvoke
   now owns its arguments/copied Context, cancellation boundary and waiter.
   Its result joins the existing batched native bridge completion queue. The
   pool thread never mutates an asyncio future. Python fallback behavior is
   retained without a native bridge. Profile /bg: 184,363 → 175,655 (-4.7%).
3. `363855b`: an async handler whose sole binding is BackgroundTasks can now run through
   the native invoker, without Python _invoke, _execute_route, simple argument
   dictionaries or an empty request exit stack. A fresh task collection is
   injected natively. The worker still encodes/resolves the response and defers
   its tasks through the existing delivery-owned boundary, including merging
   response tasks and handling streams. Dependencies, request metadata,
   validation, HTTP/ASGI middleware and other binding shapes retain fallback.
   Profile /bg: 175,655 → 162,441 (-7.5%; earlier sample 161,625).
4. BackgroundTask construction repeated six inspect checks for a plain sync
   function (including the same target twice). Exact Python functions with an
   empty, exact attribute dictionary now classify from current code flags.
   Attributed/decorated/marked functions, dict subclasses, partials, methods and
   callable objects retain inspect behavior. No cache can go stale when code
   changes. Profile /bg: 162,441 → 120,412 (-25.9%).

These stages remove 67,855 instructions/request (36.0% of /bg).
No thread count, admission policy, byte budget, public API, security or timer
setting changed. Native health stays Tier 0 and Python routes Tier 2.

## Controls

| Route | Merged baseline | Candidate | Change |
| --- | ---: | ---: | ---: |
| /bg | 188,267 | 120,412 | -36.0% |
| /sync | 42,946 | 43,062 | +0.27% |
| /json | 33,571 | 33,420 | -0.45% |

The /sync and /json baseline controls are the preceding middleware round,
whose code is the merged baseline. Small control differences are scheduling/
measurement variation. The no-body eligibility fix was rebuilt and measured
before the final callable-classification sample. Classification is not executed
by either control route.

## Matched-shape probes: lifecycle cost before callable classification

`benchmarks/profile/background_app.py` uses identical bodies, native binding,
and a single collection to separate paths. After native binding/handoffs but
before the callable classification improvement:

| Probe | Instructions/request |
| --- | ---: |
| /json: no injected tasks | 33,554 |
| /empty: injected collection, no task | 43,771 |
| /async: one async no-op task | 119,237 |
| /sync: one sync no-op task | 162,475 |

The empty binding/response boundary costs about 10.2k over /json. Actual
post-delivery work adds about 75.5k here (delivery guard/admission ownership,
then-expensive task classification, Context snapshot, post-send loop Task and
completion callbacks). Replacing that async no-op with a synchronous one adds
about 43.2k (Context copy, native pool handoff, waiter, bridge completion and
loop resumption). These are differences between whole requests, not additive
per-function Callgrind attribution or kernel/GIL latency measurements. The probes use
constant callables; matrix /bg allocates its lambda anew.

After the callable classification change, matched /async costs 87,644 and
/sync costs 119,968. With /json and /empty unchanged, the remaining incremental
lifecycle cost is about 43.9k for one async task and another 32.3k for the sync
hop. These include constructing the task descriptor; the measurements do not
attribute all of the difference to asyncio.Task creation. Repeated inspection
alone accounted for approximately 31.6k on the async probe and 42.5k on sync.
Matrix /bg's fresh lambda/different response and route table account for a
small difference from the constant-callable probe.

Task/Future-free post-send execution remains unimplemented. The response still
owns a Python post-send Task and the sync hop still has a cancellation-resistant
waiter. This round keeps those established cancellation/finalization semantics;
no unsafe GC workaround or spin loop is added. Free-threaded ownership uses
supported PyO3 owned handles, queue locks and the existing guarded invocation
state, but actual 3.14t runtime qualification is unavailable here.

## Validation

New tests cover compact writer success/failure delivery notification, ordered
route/response background merging for buffered and streamed native responses,
sync-returned awaitables preserving loop Context/current_request, and logged
StopIteration stopping later tasks. Focused suites pass 34 invoker, 10 delivery,
26 execution, 10 worker/thread and 8 sync-stream tests; the native writer test
also passes. Three callable tests cover replacement of __code__, coroutine
markers/partials/callable objects and a custom function attribute dictionary.

Qualification found and fixed two integration issues: the new loop-to-submitter
cache must be cleared after lane cleanup to let a finished client/App collect;
and compact JSON eligibility must exclude 204/205/304 even when application
headers are empty. The final wire test covers a 204 background response as well
as the existing lean/dependency equality, and HEAD. New test loop closures were
also moved into per-case helpers to satisfy Ruff's late-binding checks.

## Reproduction

```sh
PYTHON=/home/cmux/venvs/codex/bin/python OHA=/home/cmux/.cargo/bin/oha \
  BESSEMER_EVENT_LOOP=auto PROFILE_MAX_IN_FLIGHT=4096 PORT=18101 \
  CONNECTIONS=1 OUT=/tmp/private-bg-profile \
  benchmarks/profile/instructions_matrix.sh /bg 6000
```

For the probes, add MATRIX_DIR=$PWD/benchmarks/profile and
PROFILE_APP=background_app:app, and profile /json, /empty, /async, /sync.
Raw ignored artifacts and inspected client status distributions are under raw/.
No shared-host throughput/latency is used as acceptance; the M0 gate stays open.

Final built/installed-wheel native check exits 0: 242 Rust tests (2 ignored),
1,007 Python tests (34 skipped), 23 benchmark harness tests and static checks.
All 14 raw profile client distributions were inspected: 6,000 measured 200
responses each. Isolated one-core throughput remains for the orchestrator.
