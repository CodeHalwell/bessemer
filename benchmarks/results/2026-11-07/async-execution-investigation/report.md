# Async constructor-cache assessment: no runtime change retained

**Neither runtime prototype is retained.** The final Future-only cache improved installed-wheel `loop.create_future()` component CPU by 22.7%, but the balanced Linux end-to-end matrix failed the diagnostic I/O gate: at four vCPUs and 20,000 offered requests/s, main met the SLO in both rounds, while Future-only missed in both. Main I/O p99 was 10.9/23.1 ms and delivery 1.000/0.998; Future-only p99 was 208.2/137.4 ms and delivery 0.911/0.932. Both returned only HTTP 200s; the failure was latency and delivered rate. The final workspace runtime core is byte-for-byte main (`639c32b54aa64fc5ea23dd71d8ad2c5e49ff23000f3571dc8b6add7a46a4d18d`).

The earlier Task+Future cache was already rejected after a separate four-vCPU B,C,C,B fanout repeat used 20.6% more CPU per successful response than main. The Future-only candidate removed the Task cache and had no repeatable fanout regression in its preliminary repeat, but the final balanced matrix exposed the I/O failure. The retained code changes are benchmark coverage, balanced variant ordering, and focused regression tests; no constructor-cache runtime optimization is claimed.

## Retained source changes

The runtime source is unchanged. The retained changes add an explicit Rust-loop
baseline and existing fan-out route to the efficiency harness, fix paired-round
ordering, and add four loop compatibility regressions plus two no-server ordering
tests. Main's installed 3.13 and GIL-disabled 3.14t wheels pass all four added loop
cases; the retained harness passes 23 tests (three platform skips). Changed Python
passes Ruff, formatting and complexipy at 15.

A separate private native Task/Future implementation was also excluded before
these constructor-cache trials: immediate eager Task CPU was about twice CPython's,
suspended cases used 19–39% more CPU, and two task-factory cases still failed.
The specification's native Task/Future driver remains unimplemented.

## Final balanced Future-only matrix

Fresh cache harness copied from source after the ordering fix. Round order was main/Future-only/FastAPI, then FastAPI/Future-only/main. Seven workloads × three variants × two rounds × 1/2/4 vCPUs yielded **126 measured samples**: **1,239,341/1,239,341 HTTP 200s**, **304,254 warmup 200s**, zero transport/status/body/OOM errors, and **18/18 clean server sessions**. CPU quotas, cpusets, 512 MiB memory and 256 PID caps were verified for every server. The client ran outside the cap. Individual diagnostic SLO passes were 98/126: main 14/13/13, Future-only 14/12/10, FastAPI 8/9/5 at 1/2/4 vCPUs respectively. Ratios are shown only for cells where both relevant variants passed both rounds.

At passing matched loads, Future-only CPU per 200 response varied from -3.7% to +30.5% against main, with no consistent HTTP gain. At two vCPUs, fanout used 30.5% more CPU and both variants passed; at four vCPUs fanout used 2.2% less. The four-vCPU I/O SLO failure is the rejection gate. FastAPI comparisons are also workload-specific: seven cells passed for all three variants, with FastAPI/Future-only CPU ratios from 1.69× to 3.53×. At one-vCPU JSON, Future-only steady PSS was 75.2 MiB versus FastAPI 46.2 MiB, so CPU ratios alone do not establish a production cost reduction.

The full 21-cell table with CPU per 200 response, p99, SLO goodput, RSS and PSS is `final-low-report.md`; machine-readable groups and passing-only comparisons are in `final-low-summary.json`. Raw oha/status/SQLite/cgroup/cleanup evidence is in `final-low-{1,2,4}core.json`. Exact commands and source/image/wheel/native hashes are in `final-matrix-commands.txt` and `final-provenance.json`.

## Separate earlier evidence

The rejected Task+Future matrix and targeted repeats plus the Future-only preliminary fanout repeat produced **142 samples**, **1,441,086/1,441,086 measured 200s**, **334,338 warmup 200s**, zero transport/status/body/OOM errors, and **30/30 clean measured server sessions**. They were different code variants and test windows; their performance metrics are not pooled with the final matrix. Details: `trial-report.md`, `trial-summary.json`, `future-only-report.md`, and `future-only-summary.json`.

The exact installed Future-only macOS wheel in a B,F,F,B component probe improved `loop.create_future()` creation CPU 208.17→160.90 ns/op (-22.7%), create+result 242.06→198.01 ns/op (-18.2%), callback batch 845.29→773.13 ns/op (-8.5%), and bookkept callback batch 1,053.75→942.30 ns/op (-10.6%). This component result did not translate into a passing broad HTTP change. Raw samples and hashes: `component-report.md` and `component-summary.json`.

Across the separate trial collections there are **268 sample artifacts**, **2,680,427 measured 200 responses**, **638,592 warmup 200 responses**, and **48 clean measured server sessions**. These are an evidence inventory, not a pooled performance estimate.

All Linux loads were short local Docker Desktop ARM64 VM diagnostics with two-second measured windows in the matrices. They do not establish production capacity or cost acceptance. The earlier Task+Future matrix used a harness with an ordering bug; the final Future-only matrix used the corrected balanced order.

Complete commands, source snapshots, wheels, raw oha/SQLite files and the additional named reports are preserved under `~/Library/Caches/bessemer-bench/async-next-2026-11-07/`. The repository summaries retain their hashes and local artifact paths; those raw files are not duplicated here.
