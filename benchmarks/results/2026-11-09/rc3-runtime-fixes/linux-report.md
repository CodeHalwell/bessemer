# Retained Linux ARM64 worker qualification

This check used the **retained wheel** SHA `9c8c772ac4a4f681b19bf534d11d41b1c93e1366954bc1a4da337df3735a6d40`, native library SHA `36fab52644080b2e412d806e2df36511efa63763b585ef6aa6843beb8e29c776`, and source manifest SHA `7c4a47217f6a9255e229bf257cd64296338e0cdcc903963d0a8f7bfb095a0ea1`. The candidate retains the sync-pool and model-codec changes; HTTP/1 body source is the tagged RC3 baseline. These six samples are separate from the earlier 36-sample pre-ablation trial.

Each sample used 256 connections, one second warmup and two seconds of closed-loop HTTP load. The server had 4 vCPU, effective CPU set `0-3`, 512 MiB memory and a 256 PID limit, with Tini as PID 1. The separate Oha client was configured for 4 vCPU, CPU set `4-7`, and 4 threads. Exact status and body were checked before and after each load; every measured SQLite response had its status and body length checked. Latency p99 is uncorrected closed-loop latency.

| Workers | Route | Successful RPS | p99 | Successful / measured | Server CPU seconds ÷ HTTP seconds* |
| ---: | --- | ---: | ---: | ---: | ---: |
| 1 | Native health | 424,775 | 0.811 ms | 850,627 / 850,627 | 1.00 |
| 1 | Python JSON | 318,731 | 1.435 ms | 638,143 / 638,143 | 0.99 |
| 2 | Python JSON | 635,404 | 0.991 ms | 1,272,348 / 1,272,348 | 1.98 |
| 2 | Native health | 771,904 | 1.005 ms | 1,545,162 / 1,545,162 | 1.99 |
| 4 | Native health | 1,049,662 | 0.682 ms | 2,106,616 / 2,106,616 | 3.70 |
| 4 | Python JSON | 873,735 | 0.904 ms | 1,749,719 / 1,749,719 | 3.88 |

All **8,162,615 measured responses succeeded** and were within 50 ms; transport errors, unexpected statuses, wrong body lengths and rejections were zero. All three server sessions exited cleanly with zero forced or remaining tracked processes. Each sample read back the specified server cgroup CPU, memory and PID limits and `docker-init` at PID 1. There were no OOM kills; highest cgroup memory peak was 228.9 MiB. The retained installed wheel separately passed **50/50** focused native thread/model tests and **2/2** Tini/orphan checks.

Observed 1-to-4-worker throughput ratios in this single run were 2.47× for native health and 2.74× for Python JSON. This confirms multi-worker scaling under this local configuration; the short, closed-loop samples do not establish a stable production capacity. The recorded client-bound flags were false, though they do not rule out a load-generator limit.

\* The numerator spans the outer observation window, including warmup and post-load activity. Dividing it by Oha's HTTP duration is a utilization diagnostic, not a clean active-core average. Oha stores SQLite results after the timed HTTP load, so its export is also included in outer client time.

Raw [six-sample results](../scaling-retained/results.json), [machine-readable summary](retained-qualification-summary.json), [retained build receipt](../receipts/candidate-retained-build.json), [focused tests](../receipts/candidate-retained-focused-tests.json), and [orphan tests](../receipts/candidate-retained-orphan-qualification.json) preserve the measured values and commands. The [historical report](report.md) labels its 36 samples as pre-ablation and excludes them from these retained-wheel results.
