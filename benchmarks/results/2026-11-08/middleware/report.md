# HTTP middleware call path, 8 November 2026

Baseline local main `3c253e7`, fast-forwarded before implementation. Installed
CPython 3.13.15 release wheels, native loop, matrix `common:app`, one worker,
fixed admission 4096. Profiles use private vgdb prefixes and PID-scoped control,
6,000 measured requests after 2,000 warmup, c1. Every measured response was 200.

## Choice and implementation

The current no-middleware `/sync` control is 42,946 instructions/request,
matching the prior round's 43,000 rather than the supplied 68k. The supplied
68k workload is not reproduced here. `/bg` remains about 188k and still uses
Python request/dependency and post-send scheduling machinery. The HTTP
middleware control reproduces the supplied 91k, with two `_invoke` entries,
closure/coroutine wrappers, request binding checks and buffered-response cleanup
tracking. Callgrind identifies the Python interpreter as the dominant part of
this fallback. Its C-level frames do not separately attribute Python functions;
the Python boundaries above come from inspecting the invoked path.

Optimize HTTP middleware, retaining its existing Request and response wrapper:

- Compile no-input async handler eligibility once. In `call_next`, run eligible
  handlers inside the existing middleware request boundary, avoiding the second
  `_invoke` and its metadata filtering, binding and route dispatch checks.
  Claims and unexpected inputs retain the full path; parameterized handlers,
  sync handlers, dependency plans and validation retain their existing path.
- Keep current_request set/reset around each handler call, including repeated
  call_next, exceptions and responses. Exception handlers and response encoding
  use the same worker methods; cancellation propagates as before.
- Call the outermost middleware directly instead of allocating its wrapper and
  awaiting its adoption coroutine. Inner middleware levels preserve their
  existing composition; outer streams still go through adoption.
- Record only streams/files for discarded-response cleanup, avoiding a generator
  scan for ordinary buffered JSON. Discarded streams still close before return
  or cancellation; delivery/background admission ownership is unchanged.

## Instructions/request

| Route | Before | After | Change |
| --- | ---: | ---: | ---: |
| `MW=1 /json` | 91,798 | 80,761 | -12.0% |
| `/sync` (no MW) | 43,000 | 42,946 | -0.13% |
| `/bg` (no MW) | 187,724 | 188,005 | +0.15% |
| `/json` (no MW) | 33,649 | 33,571 | -0.23% |

No-MW before controls are the prior round's final qualified wheel, whose code
is the merged baseline. Their tiny changes are measurement/scheduling variation;
this change does not alter those request paths. Intermediate MW optimization
was 84,233; removing buffered cleanup tracking and the remaining outer adoption
coroutine reduced it further to 80,761. No throughput acceptance is inferred.

## Validation and remaining work

New tests cover repeated call_next with current_request reassignment and restored
middleware context, plus exception handler context/response/header propagation.
The middleware suite passes 17 tests. Existing tests also exercise nested and
mounted middleware, streams replaced by outer middleware, cancellation cleanup,
invalid middleware return values and background delivery.

Request construction, `_CalledResponse`, mutable validated headers and the
Python fallback task remain. No native pool, timer, security, public API or
admission policy change is included. Native health remains Tier 0, application
bridge routes Tier 2. The formal M0 gate is open. 3.14t runtime was not available.

Reproduce:

```sh
MW=1 PYTHON=/home/cmux/venvs/codex/bin/python OHA=/home/cmux/.cargo/bin/oha \
  BESSEMER_EVENT_LOOP=auto PROFILE_MAX_IN_FLIGHT=4096 PORT=18101 \
  CONNECTIONS=1 OUT=/tmp/private-mw-profile \
  benchmarks/profile/instructions_matrix.sh /json 6000
```
Raw ignored profiles/client status distributions live under `raw/`.

Final rebuilt and installed wheel passes `BESSEMER_REQUIRE_NATIVE=1
./scripts/check.sh`: 241 Rust tests (2 ignored), 1,001 Python tests (34 skipped),
23 benchmark harness tests and all static checks. Isolated one-core `MW=1 /json`
throughput comparison was requested and acknowledged; results are pending.
