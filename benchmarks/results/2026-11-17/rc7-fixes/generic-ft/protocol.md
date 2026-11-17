# Separate free-threaded generic sync supplement

This is a separate study, `rc7-generic-ft-2026-11-17`. Its primary comparison is `rc7-fixes-2026-11-17`; populations are not pooled.

54 fresh samples: two generic workloads × three additional interpreter modes × candidate/RC7/FastAPI × three rotated rounds. Workloads match the primary CPython 3.13 generic population: a synchronous Request handler and an async handler with a synchronous Depends. Modes are standard CPython 3.14.7 and free-threaded 3.14.7 with GIL on/off. Each sample uses 64 clients, four actual sync slots, one second warmup and four seconds measurement.

Native wheels and all dependencies are reused read-only from the primary study. There are no new builds. Installed provenance, source hashes, exact pins, native/GIL identities, full installed check and focused tests retain their original receipts. FastAPI's actual AnyIO limit and Bessemer's actual default executor bound must both be four. GIL-off is measured with PYTHON_GIL unset; an import reenabling it invalidates the sample.

The primary 24-sample synthetic scheduler diagnostic is inherited by explicit path and SHA, including its original plan hash. No new CPU scheduling diagnostic is claimed. Primary timings and the independent 260-database audit must finish before the supplement's completion receipt can be finalised. A separate 18-configuration generic preflight precedes all supplement timings.

Run only after the primary timing and SQL audit have stopped, using the primary candidate interpreter:

1. `../rc7-fixes-2026-11-17/venv-candidate-cp313/bin/python finalize_inheritance.py`
2. `../rc7-fixes-2026-11-17/venv-candidate-cp313/bin/python run_bench.py freeze-plan > frozen-plan-output.json`
3. `../rc7-fixes-2026-11-17/venv-candidate-cp313/bin/python run_bench.py preflight > preflight.log`
4. `../rc7-fixes-2026-11-17/venv-candidate-cp313/bin/python run_bench.py run > benchmark.log`
5. After all timings stop, run `analyse_bench.py --timings-finished` and `audit_sql.py --timings-finished` with the same interpreter.

Every complete three-round comparison requires zero measured and warmup status/transport/length failures, semantic probes before/after, stable runtime identities, client below 1.7 CPU cores and complete cleanup without fallback signals. Failure populations remain recorded and comparisons are withheld. The primary control counts do not become supplement throughput data. No formal M0, fixed-rate production SLO, cost or scaling acceptance is claimed.
