| cell | run | total us/req (perf stat) | threads: comm = us/req (CPUs) |
|---|--:|--:|---|
| perf/axum-json | 1 | 11.44 | tokio-rt-worker = 11.33 (0.98) |
| perf/bessemer-asyncio-echo | 1 | 18.53 | tokio-rt-worker = 6.36 (0.34); tokio-rt-worker = 6.35 (0.34); python = 5.75 (0.31) |
| perf/bessemer-asyncio-json | 1 | 13.86 | tokio-rt-worker = 5.56 (0.40); tokio-rt-worker = 5.54 (0.39); python = 2.76 (0.20) |
| perf/bessemer-rust-echo | 1 | 16.41 | python = 16.29 (0.99) |
| perf/bessemer-rust-json | 1 | 12.22 | python = 12.19 (0.99) |
