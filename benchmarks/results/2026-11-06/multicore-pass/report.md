# Multicore CPU/I/O diagnostics — 6 November 2026

Two rounds per case/configuration, 2-second samples after 1-second warmups, 64 HTTP/1 keepalive connections. Medians below use both rounds.

The 1/2/4 labels are **configured process/loop and pool sizes on an unpinned 18-CPU Mac**. They are not enforced vCPU quotas. Linux affinity enforcement was not exercised by this run. Process variants use CPython 3.13; free-threaded loops use GIL-disabled CPython 3.14t. Cross-interpreter ratios combine interpreter and topology effects; the free-threaded 1→4 ratio compares parallelism within the same interpreter/build.

The sleep-only route also has a fixed 64-connection/1ms workload ceiling, so its 1→4 throughput is not a CPU-scaling test.

Baseline is the immutable earlier local concurrency-pass wheel, not the original published PyPI wheel. Candidate GIL runs use N API processes and one CPU-offload/sync thread slot per process; free-threaded runs use one process with N loops, N CPU threads and N sync threads. Baseline/FastAPI offload uses one spawned process per API process. Baseline sync handlers retain their eight-thread default; FastAPI retains its AnyIO default.

These frozen benchmark wheels precede the later custom-executor timeout and inherited sync-thread context fixes. Final-build focused checks/measurements are recorded separately.

Measured samples: **144**; measured responses: **3,296,806**. Warmup samples: **144**. Transport errors: **0**; unexpected statuses: **0**.

Client-bound flags: **0/144** measured samples (oha client CPU ≥1.7 cores). Process-tree cleanup: **24/24** server sessions, **0** remaining tracked processes.

## Median requests/second and 1→4 scaling

| Workload | Topology | 1 | 2 | 4 | 1→4 |
| --- | --- | --- | --- | --- | --- |
| cpu-inline | Baseline processes | 597 | 1,106 | 2,118 | 3.55× |
| cpu-inline | Candidate processes | 578 | 1,048 | 2,106 | 3.64× |
| cpu-inline | Candidate free-threaded loops | 716 | 1,390 | 2,255 | 3.15× |
| cpu-inline | FastAPI processes | 567 | 1,070 | 2,008 | 3.54× |
| cpu-sync | Baseline processes | 580 | 1,040 | 2,041 | 3.52× |
| cpu-sync | Candidate processes | 550 | 1,051 | 1,971 | 3.58× |
| cpu-sync | Candidate free-threaded loops | 718 | 1,421 | 2,496 | 3.47× |
| cpu-sync | FastAPI processes | 545 | 1,052 | 1,935 | 3.55× |
| io | Baseline processes | 49,525 | 33,305 | 47,509 | 0.96× |
| io | Candidate processes | 49,122 | 48,207 | 47,334 | 0.96× |
| io | Candidate free-threaded loops | 49,959 | 49,107 | 51,768 | 1.04× |
| io | FastAPI processes | 17,412 | 29,614 | 32,322 | 1.86× |
| fanout | Baseline processes | 13,451 | 20,292 | 32,154 | 2.39× |
| fanout | Candidate processes | 13,075 | 24,069 | 31,440 | 2.40× |
| fanout | Candidate free-threaded loops | 14,365 | 21,940 | 30,230 | 2.10× |
| fanout | FastAPI processes | 9,868 | 19,404 | 23,287 | 2.36× |
| mixed | Baseline processes | 530 | 1,135 | 2,102 | 3.97× |
| mixed | Candidate processes | 562 | 1,141 | 2,138 | 3.81× |
| mixed | Candidate free-threaded loops | 761 | 1,422 | 2,612 | 3.43× |
| mixed | FastAPI processes | 529 | 1,151 | 1,960 | 3.71× |
| cpu-offload | Baseline processes | 539 | 1,053 | 1,944 | 3.61× |
| cpu-offload | Candidate processes | 561 | 1,145 | 2,058 | 3.67× |
| cpu-offload | Candidate free-threaded loops | 724 | 1,413 | 2,509 | 3.46× |
| cpu-offload | FastAPI processes | 553 | 1,164 | 2,016 | 3.65× |

## Ratios at the same configured size

| Workload | Config | Candidate / baseline | Free / baseline | Candidate / FastAPI | Free / FastAPI |
| --- | --- | --- | --- | --- | --- |
| cpu-inline | 1 | 0.97× | 1.20× | 1.02× | 1.26× |
| cpu-inline | 2 | 0.95× | 1.26× | 0.98× | 1.30× |
| cpu-inline | 4 | 0.99× | 1.06× | 1.05× | 1.12× |
| cpu-sync | 1 | 0.95× | 1.24× | 1.01× | 1.32× |
| cpu-sync | 2 | 1.01× | 1.37× | 1.00× | 1.35× |
| cpu-sync | 4 | 0.97× | 1.22× | 1.02× | 1.29× |
| io | 1 | 0.99× | 1.01× | 2.82× | 2.87× |
| io | 2 | 1.45× | 1.47× | 1.63× | 1.66× |
| io | 4 | 1.00× | 1.09× | 1.46× | 1.60× |
| fanout | 1 | 0.97× | 1.07× | 1.33× | 1.46× |
| fanout | 2 | 1.19× | 1.08× | 1.24× | 1.13× |
| fanout | 4 | 0.98× | 0.94× | 1.35× | 1.30× |
| mixed | 1 | 1.06× | 1.44× | 1.06× | 1.44× |
| mixed | 2 | 1.00× | 1.25× | 0.99× | 1.23× |
| mixed | 4 | 1.02× | 1.24× | 1.09× | 1.33× |
| cpu-offload | 1 | 1.04× | 1.34× | 1.02× | 1.31× |
| cpu-offload | 2 | 1.09× | 1.34× | 0.98× | 1.21× |
| cpu-offload | 4 | 1.06× | 1.29× | 1.02× | 1.24× |

## Observed throughput variation

**24/72 groups** had a two-round throughput spread of at least 10% ((maximum − minimum) / median). These ranges qualify the median ratios above.

| Workload | Topology | Config | RPS range | Spread |
| --- | --- | --- | --- | --- |
| io | Baseline processes | 2 | 18,596–48,014 | 88.3% |
| mixed | FastAPI processes | 1 | 446–612 | 31.4% |
| io | FastAPI processes | 1 | 14,754–20,070 | 30.5% |
| mixed | Baseline processes | 1 | 461–598 | 26.0% |
| cpu-offload | Baseline processes | 1 | 470–608 | 25.6% |
| cpu-offload | FastAPI processes | 1 | 484–622 | 25.0% |
| fanout | FastAPI processes | 1 | 8,871–10,864 | 20.2% |
| cpu-offload | Candidate free-threaded loops | 1 | 655–793 | 19.1% |
| mixed | Candidate free-threaded loops | 1 | 691–832 | 18.4% |
| cpu-sync | Candidate processes | 2 | 963–1,138 | 16.7% |
| cpu-sync | FastAPI processes | 1 | 501–589 | 16.2% |
| cpu-sync | Baseline processes | 2 | 958–1,122 | 15.8% |
| cpu-offload | Baseline processes | 4 | 1,804–2,084 | 14.4% |
| mixed | Candidate processes | 1 | 523–600 | 13.7% |
| cpu-inline | Baseline processes | 2 | 1,030–1,181 | 13.7% |
| cpu-inline | FastAPI processes | 1 | 529–605 | 13.3% |
| cpu-offload | Candidate free-threaded loops | 4 | 2,352–2,666 | 12.5% |
| fanout | Candidate free-threaded loops | 4 | 28,390–32,071 | 12.2% |
| cpu-sync | Baseline processes | 1 | 545–615 | 12.1% |
| cpu-sync | Candidate free-threaded loops | 1 | 675–762 | 12.0% |
| mixed | Candidate free-threaded loops | 4 | 2,456–2,768 | 11.9% |
| cpu-sync | Candidate free-threaded loops | 4 | 2,354–2,637 | 11.4% |
| cpu-offload | FastAPI processes | 4 | 1,908–2,123 | 10.6% |
| cpu-inline | Candidate free-threaded loops | 4 | 2,136–2,374 | 10.6% |

## Server CPU cost ranges

Ranges span the two measured rounds. CPU includes supervised descendants and offload processes.

| Workload | Topology | 1: CPU us/response | 2: CPU us/response | 4: CPU us/response |
| --- | --- | --- | --- | --- |
| cpu-inline | Baseline processes | 1,582.2–1,688.2 | 1,665.3–1,850.8 | 1,798.6–1,871.2 |
| cpu-inline | Candidate processes | 1,685.8–1,715.3 | 1,819.4–1,862.6 | 1,814.1–1,905.9 |
| cpu-inline | Candidate free-threaded loops | 1,329.7–1,385.9 | 1,365.7–1,447.2 | 1,660.8–1,830.6 |
| cpu-inline | FastAPI processes | 1,611.6–1,806.0 | 1,749.6–1,816.9 | 1,900.1–1,918.9 |
| cpu-sync | Baseline processes | 1,625.3–1,761.6 | 1,752.3–2,000.0 | 1,912.6–1,931.1 |
| cpu-sync | Candidate processes | 1,729.6–1,817.4 | 1,744.2–1,959.6 | 1,923.8–1,985.6 |
| cpu-sync | Candidate free-threaded loops | 1,421.4–1,620.5 | 1,473.6–1,538.5 | 1,621.1–1,770.1 |
| cpu-sync | FastAPI processes | 1,693.8–1,912.5 | 1,799.6–1,858.4 | 1,953.6–1,987.7 |
| io | Baseline processes | 26.6–32.7 | 32.3–40.2 | 34.3–35.7 |
| io | Candidate processes | 30.6–30.9 | 28.6–32.3 | 34.4–35.9 |
| io | Candidate free-threaded loops | 30.2–31.5 | 37.9–41.2 | 42.3–49.7 |
| io | FastAPI processes | 48.3–62.3 | 51.4–55.4 | 65.8–66.2 |
| fanout | Baseline processes | 86.7–91.1 | 103.3–112.9 | 124.7–129.7 |
| fanout | Candidate processes | 89.6–91.1 | 94.2–96.2 | 125.3–137.3 |
| fanout | Candidate free-threaded loops | 84.2–86.7 | 101.2–104.6 | 127.5–142.6 |
| fanout | FastAPI processes | 89.6–107.9 | 95.9–95.9 | 124.7–130.1 |
| mixed | Baseline processes | 1,613.7–2,023.1 | 1,680.6–1,771.0 | 1,811.7–1,886.7 |
| mixed | Candidate processes | 1,601.7–1,796.3 | 1,677.9–1,742.6 | 1,783.5–1,848.3 |
| mixed | Candidate free-threaded loops | 1,192.3–1,364.3 | 1,362.2–1,398.2 | 1,435.0–1,603.5 |
| mixed | FastAPI processes | 1,583.5–2,025.9 | 1,645.3–1,729.0 | 1,898.6–1,931.6 |
| cpu-offload | Baseline processes | 1,854.5–2,450.2 | 2,074.4–2,221.7 | 2,183.0–2,270.1 |
| cpu-offload | Candidate processes | 1,947.8–2,075.3 | 1,966.2–1,983.9 | 2,193.3–2,306.0 |
| cpu-offload | Candidate free-threaded loops | 1,359.2–1,627.7 | 1,450.0–1,562.2 | 1,595.9–1,783.5 |
| cpu-offload | FastAPI processes | 1,832.8–2,444.0 | 1,966.8–1,973.2 | 2,207.0–2,399.7 |

## Exact measured builds

Native hashes were captured before load and verified against the unchanged installed modules. Wheel contents were checked against installed Python files and native modules after the run. The Python-package hash covers sorted relative .py paths and contents separated by NUL bytes.

### Baseline processes

Python: `3.13.18 (main, Oct 25 2026, 18:31:23) [Clang 21.0.0 (clang-2100.3.34.2)]`. GIL enabled: `True`; free-threaded build: `False`.

Wheel: `/Users/danielhalwell/Library/Caches/bessemer-bench/concurrency-2026-11-06/wheels/bessemer-1.0.0rc1-cp313-cp313-macosx_11_0_arm64.whl`.

```text
wheel_sha256: d05c83d3122c9e5d3ab7dc2f9d05e7883dc053f567f73bf60b737c50e36ff457
native_sha256: 29612ea6f2fd5eeaa1a0e29ba657575b6e5142783aaec193f82c3800d56d1a7e
python_package_sha256: 746e25fff9d0fad0cd515d475c63b74c2205bbc05aa6b99ba118bde6b615d6c4
```

### Candidate processes

Python: `3.13.18 (main, Oct 25 2026, 18:31:23) [Clang 21.0.0 (clang-2100.3.34.2)]`. GIL enabled: `True`; free-threaded build: `False`.

Wheel: `/Users/danielhalwell/Library/Caches/bessemer-bench/multicore-2026-11-06/wheels-313/bessemer-1.0.0rc1-cp313-cp313-macosx_11_0_arm64.whl`.

```text
wheel_sha256: 50824c010388f9505c46e967e0402ad75eb9b50745981f8a37fc8e1b825bed30
native_sha256: d36e23bb2f39df6c0ef34faa8390a373501e9cb80b91245086080c37b6a4c44f
python_package_sha256: 1cfd09c173cfeb807d81ec551ce3b9f9b33df8dd2037a9193fb1f16ea36bc422
```

### Candidate free-threaded loops

Python: `3.14.7 free-threading build (main, Sep 29 2026, 14:50:22) [Clang 22.1.3 ]`. GIL enabled: `False`; free-threaded build: `True`.

Wheel: `/Users/danielhalwell/Library/Caches/bessemer-bench/multicore-2026-11-06/wheels-314t/bessemer-1.0.0rc1-cp314-cp314t-macosx_11_0_arm64.whl`.

```text
wheel_sha256: 4c54c615146e3dd9cc222783160ff025c09e1b0184a6c79ac75a5d9546cddbff
native_sha256: c73a5cf33854e931516da190fd61481e98d086082fb06000c17f9b6663e42cfb
python_package_sha256: 1cfd09c173cfeb807d81ec551ce3b9f9b33df8dd2037a9193fb1f16ea36bc422
```

FastAPI environment: fastapi `0.162.0`, uvicorn `0.54.0`, uvloop `0.23.0`, httptools `0.9.0`, pydantic `2.14.3`.

Raw evidence: [scaling.json](scaling.json), [exact command](scaling-command.txt), [run log](scaling-run.log), [build facts](build-facts.json), [initial 24-route probe](probe.json), [probe cleanup](probe-cleanup.json).

## Final Python-only fixes

The final wheels add custom-executor timeout enforcement and prevent sync worker threads from inheriting the first request's ContextVars. The native modules are unchanged; the wheel and Python-source hashes distinguish these builds. The follow-up uses the same two workloads, configured sizes 1/4, two rounds, 2-second samples, 1-second warmups and 64 connections.

Measured samples: **16**; measured responses: **47,679**. Warmup samples: **16**. Transport errors: **0**; unexpected statuses: **0**.

Client-bound flags: **0/16** measured samples (oha client CPU ≥1.7 cores). Process-tree cleanup: **8/8** server sessions, **0** remaining tracked processes.

| Workload | Topology | 1 RPS | 4 RPS | 1→4 | 1 CPU us/response | 4 CPU us/response | 4 / original 4 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| cpu-sync | Candidate processes | 605 | 2,076 | 3.43× | 1,624.0–1,672.1 | 1,846.1–1,878.9 | 1.05× |
| cpu-sync | Candidate free-threaded loops | 768 | 2,411 | 3.14× | 1,397.4–1,408.8 | 1,683.8–1,789.7 | 0.97× |
| cpu-offload | Candidate processes | 587 | 2,015 | 3.43× | 1,862.7–1,965.3 | 2,204.2–2,265.3 | 0.98× |
| cpu-offload | Candidate free-threaded loops | 748 | 2,455 | 3.28× | 1,416.9–1,466.9 | 1,649.5–1,752.0 | 0.98× |

The last column compares this follow-up with the earlier matrix, not a paired acceptance test. Both runs retain their raw sample ranges.

**Candidate processes**: GIL enabled `True`; free-threaded build `False`.

Wheel: `/Users/danielhalwell/Library/Caches/bessemer-bench/multicore-2026-11-06/final-wheels-313/bessemer-1.0.0rc1-cp313-cp313-macosx_11_0_arm64.whl`.

```text
wheel_sha256: 8d477cb52ba3c9b2c809380b81412d1e12d8db510cbc16b8bbd2e58d70a8151c
native_sha256: d36e23bb2f39df6c0ef34faa8390a373501e9cb80b91245086080c37b6a4c44f
python_package_sha256: b0d88a2aa647bf184faa781cb510bbf8e7ef9d6ffc6572eaab51dd30565b8ba9
```

**Candidate free-threaded loops**: GIL enabled `False`; free-threaded build `True`.

Wheel: `/Users/danielhalwell/Library/Caches/bessemer-bench/multicore-2026-11-06/final-wheels-314t/bessemer-1.0.0rc1-cp314-cp314t-macosx_11_0_arm64.whl`.

```text
wheel_sha256: d2ec097de9879b05e8830db56730d5f990c8ab60d280be58b4eb98fe0167d130
native_sha256: c73a5cf33854e931516da190fd61481e98d086082fb06000c17f9b6663e42cfb
python_package_sha256: b0d88a2aa647bf184faa781cb510bbf8e7ef9d6ffc6572eaab51dd30565b8ba9
```

Final installed validation: [check-final-313.log](check-final-313.log), [check-314t.log](check-314t.log); [source/build manifest](manifest-final.json). CPython 3.13: 188 Rust passed/1 ignored, 814 Python passed/11 skipped, 18 harness passed/3 skipped. CPython 3.14t: 213 focused tests passed/1 skipped, GIL disabled before and after the suite.

Follow-up evidence: [raw samples](final-scaling.json), [command](final-scaling-command.txt), [run log](final-scaling-run.log), [verified final build facts](build-facts-final.json).
