# Native sync invocation and post-response work

Follow-up to `f10d7ef`, 8 November 2026. All changes are on
`perf/codex-echo` in `/home/cmux/wt/codex-echo`; only the assigned
`/home/cmux/venvs/codex` environment was installed or changed.

## Retained changes

- `2138807`: handle already-closed destination loops and closure racing with
  completion scheduling. Abandoned returned coroutine objects are closed. A
  closed loop cannot resume its Task; normal shutdown still drains work first.
- `107c4b0`: native argument binding, request construction and result encoding
  for plain sync handlers. User code stays on the bounded executor in a copied
  Context, with the invocation's queued/running cancellation accounting.
  Native work items avoid allocating an unused concurrent Future and implement
  CPython's worker/shutdown item protocol. Unexpected responses and awaitables
  use the normal Python response/exception boundary. No public API changes.
- The post-response change transfers request admission to a transport completion
  guard. HTTP/1 signals after writing the response; HTTP/2 retains completion
  through the body and its retained data frames. Abandoned responses unwind
  dependencies without starting new background work. Background collections run
  in order, sync tasks use the pool, failures are logged after delivery, and
  cleanup finishes before capacity returns. Streaming tasks, HEAD, middleware
  replacement and worker shutdown use the same lifetime ownership.

## Instruction counts

CPython 3.13.15, installed release wheels, Pydantic current in the assigned
venv; auto loop, one connection, 2,000 warmup requests then 3,000 measured
requests. `/sync`, `/bg`, `/json` use `/home/cmux/matrix/common:app` with
`FW=bessemer`. `/items/7?q=abc` and POST `/echo` use the original compare app;
`/echo` receives `/home/cmux/wt/order.json` (20 items). All accepted profiles
use a private vgdb prefix and PID-scoped controls.

| Route | Before native sync (2138807) | Native sync step | Final, post-send background | Final vs before |
| --- | ---: | ---: | ---: | ---: |
| `/sync` | 158,515 | 99,426 | 99,619 | -37.15% |
| `/bg` | 221,948 | 222,010 | 240,515 | +8.37% |
| `/json` | 33,144 | 33,135 | 33,189 | +0.14% |
| `/items/7?q=abc` | 36,164 | 36,053 | 36,124 | -0.11% |
| POST `/echo` | 203,862 | 201,893 | 203,828 | -0.02% |

The final `/sync` count is below 100k but does not reach a substantially lower
threshold or match the async path. A lean Python pool wrapper measured 134,909;
a native item using ordinary executor.submit measured 125,312; native
future-free items measured 99,606 before a minor context-entry cleanup reached
99,426. The required thread hop and asyncio suspension remain.

The first correct post-send implementation measured `/bg` at 257,778. Avoiding
an extra group Task for one collection and unnecessary Context restoration when
there are no dependency finalizers reduced it to 240,515. This remains an
8.37% instruction increase: it fixes response timing and accounts for the whole
post-response lifetime; it is not claimed as a CPU optimization. No reliable
throughput claim is made on this shared host. Python routes remain Tier 2 and
formal M0 performance acceptance remains open.

Reproduce with `benchmarks/profile/instructions_matrix.sh`, selecting
`PROFILE_APP=bessemer_app:app MATRIX_DIR=$PWD/benchmarks/compare` for compare
routes and `METHOD=POST BODY_FILE=/home/cmux/wt/order.json` for `/echo`.
Accepted raw directories under the local gitignored `raw/` are
`before-native-*`, `native-final-sync` / `native-queue-*`, and
`deferred-*` (use `deferred-final-bg` for the final `/bg` value).

## Validation and limits

The rebuilt, installed wheel passed 93 focused native/execution tests before
post-send work. The background/interop run passed 132 tests (one skip), covering
both loops, HTTP/1 and HTTP/2 buffered/streamed delivery, HEAD, ordered sync and
async tasks, current_request/ContextVars and generator tokens, exception logging,
shutdown cancellation, middleware replacement, and admission through abandoned
stream cleanup. The final delivery/API subset passed 19 tests after adding the
replacement and abandonment cases. Old assertions that required background work
to finish before response delivery were updated to wait for task completion.

The first full check passed Rust/static checks and ran 952 Python tests with one
failure: the old API test read a task's output file immediately after delivery.
Its completion wait was corrected, and the full check was repeated on the final
installed wheel. Exact final results are appended below after completion.
Only GIL-enabled CPython 3.13 was executed locally; CPython 3.12, 3.14/3.15 and
free-threaded worker protocol/context handling require CI qualification.

Final required check: **passed**, using the final installed release wheel.
235 Rust tests passed (2 ignored); 954 Python tests ran (34 skipped);
23 benchmark harness tests passed. Rust formatting, warnings-denied workspace
Clippy, Ruff lint/format and shell syntax checks all passed. The final wheel
SHA-256 is `41fe580e47bfc185e230f37fe50c1f3d1fa0a5752316694595b6023829e80f2d`.
The installed worker was checked byte-for-byte against source. Raw check logs
are retained in `raw/check-followup-final.log`.
