# Matched-resource efficiency diagnostic — 6 November 2026

Bessemer used less server CPU per response than FastAPI at each of the six matched, passing low-load points. The candidate Rust loop used **1.16–3.41× less CPU per SLO success** across those points; candidate asyncio used **1.10–2.24× less**. This is a measured CPU-cost proxy, not a cloud bill or an established replica-saving factor. Steady server PSS remained in the same broad range across implementations.

The clearest repeated capacity result is synchronous streaming: **candidate Rust loop passed 4,000 offered requests/s twice, at corrected p99 10.0 and 5.5 ms; FastAPI failed 2,000 twice, at 110.6 and 148.3 ms**. Candidate asyncio had tail outliers at both 2,000 and 4,000, and neither candidate loop passed 8,000 reliably. There is no repeat-safe 8,000 requests/s production-capacity claim.

## Resource scope and target

- Four serial variants: immutable previous-pass Bessemer baseline, candidate asyncio, candidate Rust loop, and FastAPI/Uvicorn; all Linux CPython **3.13.11**, actual GIL enabled, two API workers, one Bessemer event loop and one sync worker per API process.
- Every server instance had kernel-verified `cpu.max=200000 100000`, `cpuset.cpus.effective=0-1`, `memory.max=536870912`, `pids.max=256`, with `docker-init` at PID 1. The Linux VM was local OrbStack aarch64; the Mac oha client ran outside the container resource scope. Docker recorded a 1 GiB combined RAM/swap limit; this was a 512 MiB cgroup memory cap, not a no-swap qualification.
- Same 64 client connections, oha 1.16 fixed offered rate with latency correction, two-second measured samples and 0.5-second warmups. Bessemer admission was 128 in-flight/256 connections; Uvicorn concurrency was 256. Those controls count different things; offered load and external CPU/RAM limits were matched.
- Declared **diagnostic** target: corrected successful-response p99 ≤50 ms, failures ≤0.1%, ≥99% offered requests delivered successfully, ≤1% successes slower than 50 ms, and client CPU below 85% of its two configured threads. It is not the user’s production SLO or a soak test.
- Exact existing concurrency routes/payloads and validation contracts were reused. Mixed work is the same checksum(25,000), sleep(1 ms), checksum(25,000). Full bodies were validated before/after load; per-response load accounting checks status and body length.

## Completed measurements

**80 samples, 1,261,517 completed responses, 45/80 meeting the diagnostic target.** There were zero transport errors, unexpected statuses, body-length mismatches, 429/503 rejects, client-bound samples or OOM kills. Overload was intentional: a response can be valid yet too late or the offered rate can be unmet.

All **12 server instances** stopped normally with exit code 0, no forced PIDs and no remaining descendants. Container cleanup completed and the benchmark container list was empty after both runs.

## Equal offered load: server CPU cost

All four variants passed at every rate below in the initial six-case slice. Values are **server process-tree CPU-seconds per million SLO successes** (within 50 ms), retaining observed exited-child CPU and excluding the sampler/init. The last columns divide FastAPI CPU cost by candidate cost at that same offered rate. These are short-window extrapolations; the process counters have 10 ms granularity.

| Workload | Offered/s | Baseline | Candidate asyncio | Candidate Rust | FastAPI | FastAPI/asyncio | FastAPI/Rust |
|---|---:|---:|---:|---:|---:|---:|---:|
| json | 10,000 | 37.5 | 35.6 | 23.0 | 78.5 | 2.21× | 3.41× |
| echo | 3,000 | 93.3 | 83.4 | 81.7 | 186.7 | 2.24× | 2.28× |
| dependencies | 5,000 | 98.1 | 92.0 | 76.0 | 118.1 | 1.28× | 1.55× |
| io | 10,000 | 52.0 | 49.5 | 33.5 | 78.0 | 1.58× | 2.33× |
| sync-stream | 1,000 | 1,200.0 | 550.0 | 445.0 | 1,170.0 | 2.13× | 2.63× |
| mixed | 300 | 2,150.0 | 2,150.0 | 2,033.3 | 2,366.7 | 1.10× | 1.16× |

At 1,000 sync-stream requests/s, ordinary valid-response CPU cost was 550.0 CPU-s/million for candidate asyncio, 445.0 for the Rust loop and 1,170.0 for FastAPI: approximately 2.13× and 2.63× differences. The table above uses the stricter SLO-success denominator. These values do not establish 10× resource efficiency.

## Memory and process counts

Ranges below span the twelve initial workload/rate points for each variant, including overload. RSS sums shared pages; PSS apportions them. Charged cgroup memory also includes the helper/init and file cache. All memory values are MiB. All measured steady connection counts were 64 established sockets; idle keepalive sockets are included, so these are not active-request memory measurements.

| Variant | Steady RSS range | Steady PSS range | Sampled RSS peak | Charged memory range | Anon range | File range | Server processes | Steady threads range |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| Baseline asyncio | 143.2–148.1 | 114.2–119.1 | 150.3 | 126.1–151.4 | 115.5–119.6 | 7.8–33.5 | 3 | 9.0–11.0 |
| Candidate asyncio | 144.4–150.4 | 115.5–121.2 | 153.5 | 130.1–150.4 | 115.8–121.6 | 7.4–28.4 | 3 | 9.0–11.0 |
| Candidate Rust loop | 141.2–146.9 | 111.8–117.7 | 148.0 | 120.5–140.6 | 112.2–118.3 | 0.8–20.0 | 3 | 5.0–7.0 |
| FastAPI / Uvicorn | 149.0–157.9 | 116.8–125.9 | 157.9 | 153.7–165.8 | 118.0–127.5 | 34.8–35.0 | 4 | 6.0–73.0 |

The data shows modest PSS differences, not a multi-fold RAM reduction. The raw files retain RSS per established socket, incremental RSS, cgroup CPU, helper CPU, memory events and full resource series. Kernel `memory.peak` covers each entire fresh server lifetime, including startup/warmup, and is distinct from sampled endpoint peaks. No active-request count or RAM-per-active-request claim is inferred.

## Initial sparse capacity grid

Each cell is **highest passing / lowest failing tested offered requests/s** from a single initial round. `—` means no failing point was observed, not unlimited capacity. These sparse points are brackets/truncated bounds only; they do not justify a capacity ratio. Sync-stream repeat results below supersede an isolated initial pass.

| Workload | Baseline | Candidate asyncio | Candidate Rust | FastAPI |
|---|---:|---:|---:|---:|
| json | 40,000 / — | 40,000 / — | 40,000 / — | 10,000 / 40,000 |
| echo | 15,000 / — | 15,000 / — | 15,000 / — | 3,000 / 15,000 |
| dependencies | 25,000 / — | 25,000 / — | 25,000 / — | 5,000 / 25,000 |
| io | 10,000 / 40,000 | 10,000 / 40,000 | 10,000 / 40,000 | 10,000 / 40,000 |
| sync-stream | 1,000 / 8,000 | 8,000 / — | 8,000 / — | 1,000 / 8,000 |
| mixed | 300 / 1,500 | 300 / 1,500 | 300 / 1,500 | 300 / 1,500 |

## Repeated synchronous-stream capacity diagnostic

Each row contains both two-second repetitions, in round order. Passing counts require both latency and offered-load delivery conditions. CPU costs under overload are not used as a production-saving ratio.

| Variant | Offered/s | Passes | Corrected p99 ms (round 1 / 2) | Delivered % (round 1 / 2) |
|---|---:|---:|---:|---:|
| Baseline asyncio | 2,000 | 2/2 | 35.9 / 46.5 | 99.98 / 100.00 |
| Baseline asyncio | 4,000 | 0/2 | 873.7 / 455.8 | 57.06 / 78.00 |
| Baseline asyncio | 8,000 | 0/2 | 1433.9 / 1290.3 | 28.64 / 36.13 |
| Baseline asyncio | 16,000 | 0/2 | 1677.7 / 1656.0 | 16.39 / 17.41 |
| Candidate asyncio | 2,000 | 1/2 | 85.0 / 9.0 | 99.98 / 99.98 |
| Candidate asyncio | 4,000 | 1/2 | 9.2 / 67.2 | 99.98 / 99.99 |
| Candidate asyncio | 8,000 | 1/2 | 43.4 / 61.8 | 99.34 / 98.89 |
| Candidate asyncio | 16,000 | 0/2 | 854.5 / 908.3 | 57.19 / 54.54 |
| Candidate Rust loop | 2,000 | 2/2 | 4.0 / 3.8 | 99.98 / 99.98 |
| Candidate Rust loop | 4,000 | 2/2 | 10.0 / 5.5 | 100.00 / 99.99 |
| Candidate Rust loop | 8,000 | 1/2 | 47.4 / 122.4 | 99.98 / 99.99 |
| Candidate Rust loop | 16,000 | 0/2 | 525.3 / 556.1 | 74.04 / 71.61 |
| FastAPI / Uvicorn | 2,000 | 0/2 | 110.6 / 148.3 | 99.52 / 96.70 |
| FastAPI / Uvicorn | 4,000 | 0/2 | 893.7 / 915.6 | 56.05 / 55.07 |
| FastAPI / Uvicorn | 8,000 | 0/2 | 1530.6 / 1467.2 | 23.99 / 27.12 |
| FastAPI / Uvicorn | 16,000 | 0/2 | 1726.3 / 1778.1 | 14.15 / 11.87 |

- Baseline passed 2,000 twice and failed 4,000 twice.
- Candidate Rust passed 2,000 and 4,000 twice; 8,000 passed once and failed once. Its repeated reliable lower bound is 4,000 within this diagnostic, not 8,000.
- Candidate asyncio passed only one of two samples at each of 2,000, 4,000 and 8,000. Its p99 outliers at 2,000/4,000 make a monotonic capacity bracket unsupported. The initial 1,000 sample passed once.
- FastAPI failed both 2,000 samples; the initial 1,000 sample passed once. The exact stable knee below 2,000 remains unmeasured.

## Immutable build identity

All server probes reported CPython 3.13.11, GIL enabled, Pydantic 2.14.3, FastAPI 0.162.0 and Uvicorn 0.54.0. FastAPI did not import Bessemer for its probe. The same tools image and system dependencies were shared. Candidate asyncio and Rust-loop probes reported the identical installed package/native hashes across initial and repeated runs.

Image: `bessemer-efficiency-tools:2026-11-06`

```text
sha256:ca678c220ce5b09a52fab9ccc1f9f47f905395caaacd9b7aab828b3a813c02bb
```

| Artifact | SHA-256 |
|---|---|
| Baseline wheel: `bessemer-1.0.0rc1-cp313-cp313-manylinux_2_34_aarch64.whl` | `3bedfe5478b12f8997865b2b53fae323d53822ab465b727313d9b83ae28bc893` |
| Candidate wheel: `bessemer-1.0.0rc1-cp313-cp313-manylinux_2_34_aarch64.whl` | `b7c0847ae511f1f74466d75652e46eedf9c20c0d6f3861bb03ae155cd9b82264` |
| baseline native module | `4fe8fc091cefdf69c9ca13fea9b625dca83622e65523b30866f218311078a6f8` |
| baseline Python package tree | `b0d88a2aa647bf184faa781cb510bbf8e7ef9d6ffc6572eaab51dd30565b8ba9` |
| candidate native module | `ebb74595b7009aab75d4c232dcdcb8af2c4790f1c3c2d39a05abff75b4a6b0f5` |
| candidate Python package tree | `dc2ab32517a653d02f880de9210e2827a5dee5e877b058037c82aefe42883da4` |
| fastapi Python package tree | `c7832945176b3bbfa3b225c2bbbb2c522e02b4ee004eb9c893d3804e56297c9a` |

Wheel caches are `/Users/danielhalwell/Library/Caches/bessemer-bench/efficiency-2026-11-06/wheels-{baseline,candidate}-linux/`; both wheel versions are `1.0.0rc1`, so hashes identify the actual builds. Source snapshots/hashes are retained in sibling `baseline-source[-hashes.json]` and `candidate-source[-hashes.json]`. The installed framework Python hash is an ordered package-`.py` content aggregate as implemented by the probe; the native hash covers the installed `.so`.

## Raw records, commands and validation

- [Initial 48 samples](initial.json), [exact command](initial-command.txt), [controller log](initial-run.log).
- [Repeated 32 sync-stream samples](stream-capacity.json), [exact command](stream-capacity-command.txt), [controller log](stream-capacity-run.log).
- [Frozen harness source hashes](harness-initial-sha256.json). Each JSON contains pinned image ID, actual `/probe` package/native hashes, Docker commands/configuration, kernel limit readback, raw oha output, SQLite path/hash and complete resource samples. Per-server logs are adjacent with `initial-*` / `stream-capacity-*` names.
- Raw SQLite files remain under `/Users/danielhalwell/Library/Caches/bessemer-bench/efficiency-2026-11-06/sqlite/`; individual paths and SHA-256 values are in every sample. They are not added as large repository artifacts.
- [Candidate probe](probe.json) and [root probe](probe-root.json), including verified caps/normal cleanup; [separate resource-limit smoke](resource-limit-smoke.json).
- [Installed macOS 3.13 full check](check-macos-313.log): 195 Rust passed, 2 ignored; 822 Python passed, 11 skipped; harness 18 passed, 3 skipped.
- [Installed Linux candidate focused checks](check-linux-focused.log): 71 passed, including streaming/cancellation/native-invoker and executor-context coverage.
- [Installed macOS 3.14t streaming checks](check-streams-314t.log): 33 passed, actual GIL disabled. The matched efficiency measurements themselves all used Linux 3.13.11 with the GIL enabled.
- [Final harness checks](check-harness-final.log): Ruff lint/format and complexipy maximum 15 passed; read-only accounting confirmed totals, all twelve cleanups and unchanged package/image identity between initial and repeated runs.
- The earlier [format-only interrupted check](check-macos-313-format-failed.log) is retained; the subsequent full check linked above passed.

No further load was run after these 80 samples. Next investigation is the default-loop tail outliers and Python/native task/handoff overhead; these data do not establish their cause. Longer repeated qualification against a real production SLO and connection mix is needed before choosing a production replica count.
