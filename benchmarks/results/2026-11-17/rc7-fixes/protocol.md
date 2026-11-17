# RC7 fixes benchmark protocol

Prepared from the audited 14 November CPU admission harness. No timed measurements have run at preparation.

- Original 242-sample matrix retained: 234 three-round samples plus eight one-round, single-client controls.
- Added 18 generic sync samples: a synchronous Request handler and an async handler with synchronous Depends, CPython 3.13 at c64, four sync threads, candidate/RC7/FastAPI in three rounds.
- Total: 260 fresh natural-load samples, with rotated execution order.
- CPU at c16: inline, synchronous with four threads, and automatic offload to four processes across CPython 3.13.18, standard 3.14.7, and 3.14.7t with GIL on/off (108 samples).
- CPU sync at c64 on standard 3.13/3.14 (18 samples).
- Nine core HTTP workloads at c64 on 3.13 (81 samples): JSON, typed 20-item echo, 1 ms I/O, fanout16, synchronous sleep, sync/async streams, 256 KiB bytes, and async yield dependencies.
- 256-model responses (nine samples) and 1,000-item typed uploads at c16/c64 (18 samples).
- Eight separate c1 functional controls on candidate/RC7, inline/sync, standard 3.13/3.14; these do not establish a three-round comparison.
- Separate 24-sample synthetic scheduling diagnostic: six rounds × candidate/RC7 × standard 3.13/3.14, 16 clients, four sync slots, three seconds with nominal worker SIGSTOP 5 ms / SIGCONT 2 ms. Every status and complete body is retained; final SIGCONT is mandatory. Excluded from capacity and throughput comparisons.

Candidate and RC7 baseline are both fresh Git source builds for cp313, cp314, and cp314t with Rust 1.92.0 and the same release profile, explicitly setting strip=none. RC7 source commit is a106a566d6d27e7167a0b777226771e488147c94. Published wheel throughput is not claimed. Every installed Python/native member must match its wheel; Python must also match source. All six pre/post build manifests are frozen, and runtime edits invalidate the plan. FastAPI 0.162.2 environments are existing read-only reference installs. Direct pins match all contenders; transitive package differences are explicitly recorded.

Common reference apps remain frozen at main 4965777, using the previous automatic-offload wrapper and 256-model supplement. The additional generic sync population uses a new common app. `port-receipt.json` proves that reference Monitor/measure/load/validate, Server, and Pauses/client retain identical ASTs.

No OS CPU quota or affinity is enforced. PYTHON_CPU_COUNT=4 sizes automatic executors; it does not isolate CPU. The shared M5 Max host runs one application worker and a local client with two oha worker threads. GIL off means PYTHON_GIL is unset; unexpected automatic GIL reenabling invalidates a sample. JIT is disabled.

Prerequisites: builds and tests have stopped; the full installed CPython 3.13 gate and focused 3.14/3.14t GIL-on/off gates pass and match frozen native hashes.

1. `venv-candidate-cp313/bin/python capture_provenance.py`
2. `venv-candidate-cp313/bin/python run_bench.py freeze-plan > frozen-plan-output.json`
3. `venv-candidate-cp313/bin/python run_bench.py preflight > preflight.log`
4. `venv-candidate-cp313/bin/python run_diagnostics.py --fail-fast-candidate > scheduler-diagnostics.log`
5. `venv-candidate-cp313/bin/python run_bench.py run > benchmark.log`
6. After all timing stops: `venv-candidate-cp313/bin/python analyse_bench.py --timings-finished` and `venv-candidate-cp313/bin/python audit_sql.py --timings-finished`.

Every three-round comparison requires all pairs to qualify: zero measured transport/status/length errors, zero warmup transport/status errors, correct live Python/native/package/GIL/pool identity before and after, client below 1.7 cores, and complete cleanup without fallback signals or survivors. Complete semantic responses are checked before and after load. All measurements retain per-response SQLite records and raw status/error/warmup distributions. Failed comparisons are withheld, and all failures remain recorded. There is no overwrite or resume: changed source or a failed attempt uses a separate preserved directory and plan.

CPU per success includes drain/export. Summed RSS can double-count shared pages; USS may be unavailable on macOS. Local results do not establish formal M0, a fixed-rate production SLO, billing, startup/soak, worker-scaling, or GPU qualification.
