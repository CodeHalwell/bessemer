# CPython 3.14t qualification — 8 November 2026

Base: local main `75a64bf`, fast-forward merged into `perf/codex-echo`.
Private interpreter: uv CPython **3.14.7+freethreaded**, venv
`/home/cmux/venvs/codex-314t`. Release wheel tag:
`cp314-cp314t-manylinux_2_34_x86_64`.

## Findings and changes

- The free-threaded model-list encoder did not check that its input was an
  exact list before using list-layout macros; non-list inputs could segfault.
  It now refuses other types before touching list storage. Existing invalid-input
  cases exercise this guard on the actual free-threaded wheel.
- Lists of model dictionaries previously declined native encoding on
  free-threaded builds. They now use private dict snapshots copied under
  critical sections, retaining strong references through encoding. Nested
  mutable lists are likewise snapshotted. Copies are size-checked under the
  same lock before allocation. Existing byte-order, defaults, fallback and
  invalid-input tests pass; new concurrent mutation stress covers nested
  model dictionaries and lists.
- The process-wide GC-count regression was contaminated by garbage from
  earlier suites on free-threaded Python (about 846 objects, versus zero
  when run alone). Run that measurement in an isolated subprocess, retaining
  its original request workload and <150-object threshold.
- Added real HTTP stress on asyncio and Bessemer loops, including one and two
  loop lanes sharing four native sync workers, request ContextVar isolation,
  ordered sync/async background completion, and repeated disconnects while
  receive waits. The existing tests retain response-before-background,
  admission-through-cleanup, queued cancellation and repeated shutdown checks.

No threading race was reproduced in the native sync pool, bridge completion,
background delivery, adaptive admission or disconnect watch. This is Linux
qualification on this interpreter and host, not exhaustive race detection or
qualification of every free-threaded Python version and platform.

An existing compatibility limitation was also observed: lean routes construct a
bare `current_request` from the route template, so a handler without a Request
argument sees the template path and empty path parameters through that global.
This is independent of free threading; this qualification round does not change
metadata binding. The new stress verifies per-request State identity instead.

## Reproduction

```sh
uv python install 3.14t
uv venv --python 3.14t /home/cmux/venvs/codex-314t
uv pip install -p /home/cmux/venvs/codex-314t/bin/python \
  maturin==1.16.0 ruff==0.17.3 pydantic fastapi uvicorn pytest httpx h2 \
  opentelemetry-api ag-ui-protocol itsdangerous
export PATH=$HOME/.cargo/bin:$PATH CARGO_BUILD_JOBS=3 PYTHON_GIL=0
/home/cmux/venvs/codex-314t/bin/maturin build --release \
  -i /home/cmux/venvs/codex-314t/bin/python -o /home/cmux/wheels/codex-314t
uv pip install -p /home/cmux/venvs/codex-314t/bin/python \
  --force-reinstall --no-deps /home/cmux/wheels/codex-314t/*.whl
CARGO_INCREMENTAL=0 CARGO_PROFILE_DEV_DEBUG=0 CARGO_PROFILE_TEST_DEBUG=0 \
  PYTHON=/home/cmux/venvs/codex-314t/bin/python \
  BESSEMER_REQUIRE_NATIVE=1 ./scripts/check.sh
```

Debug symbols/incremental compilation were disabled for the Rust checks after
an initial debug build exhausted the shared disk. Only this worktree's Cargo
build cache was cleaned. The initial TLS setup failure also passed on rerun
once space was available; no TLS behavior or settings changed.

## Validation

`sysconfig.get_config_var("Py_GIL_DISABLED") == 1`;
`sys._is_gil_enabled() == False` before and after importing the installed
Bessemer extension and pydantic-core. The repeated stress runner checks the
GIL remains disabled after every batch.

- Focused model/threading/GC suite: **43 tests passed** with `PYTHON_GIL=0`.
- Repeated cancellation/shutdown/background/pool stress: **10 × 25 tests passed**;
  `sys._is_gil_enabled()` stayed false after every batch.
- The same focused suite with `PYTHON_GIL=1`: **43 run, 42 passed, 1 expected skip**.
- Full `BESSEMER_REQUIRE_NATIVE=1 ./scripts/check.sh` with `PYTHON_GIL=0`:
  **242 Rust tests passed, 2 ignored; 1,024 Python tests run, 1,013 passed,
  11 skipped; all 23 benchmark-harness tests passed**. Rust formatting,
  warnings-denied Clippy, Ruff lint/format and shell syntax checks passed.
- Skips: six Redis integration tests (redis-server unavailable), four Windows
  supervisor tests on Linux, one forced-loop-environment test (no forced loop
  configured). HTTP stress explicitly exercises both loop backends.
- The final four stress tests were rerun after strengthening the State identity
  assertion, and the 250-test repeated stress used that final version.

No instruction-count or throughput comparison: this round qualifies
correctness, and does not claim a performance improvement. The M0 gate remains
open.
