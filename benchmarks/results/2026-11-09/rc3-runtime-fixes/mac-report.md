# Final retained-wheel local comparison

## Provenance and scope

- This is the **retained** build, native SHA-256 `67ddd7150d0f0845c2592e95bf2a3cddf5795fecf9646fccce077da8e7ac836e`, wheel SHA-256 `c50f922f3deddf3fd0a993cb9396e42a70a808565a040a3e8911ff79bd59f2ca`. Its same-toolchain baseline is **locally built tagged RC3**, native SHA-256 `cdd59de4b152df186780102b5133cedf96e270d22d28fafef2cb28836f677743`, wheel SHA-256 `4866a02def1416154b73e785ea603aa838db187161d04749388aa1a343d87215`. It is not the published PyPI wheel.
- The baseline source is tag `v1.0.0rc3` at `2557c73816ebad3f94f0e214e9c33b39e3a02152`; both wheels used the local rustc 1.98.0/maturin 1.16.0 toolchain and the same Python 3.13.18/interpreter and 22 common dependencies. FastAPI ran from the local baseline environment with Uvicorn/uvloop/httptools. Exact installed native bytes, both wheels, build receipts and retained source hashes passed preflight; retained source, installed native bytes, driver and baseline wheel/receipt passed postflight.
- The run is local macOS on a shared, unpinned host. Worker counts are **not** vCPU quotas. The earlier 156-cell comparison used a discarded 18bd candidate and a published RC3 wheel built with another compiler; its data is preserved separately and is not pooled with these final results.
- The plan completed 69/69 HTTP cells plus 9/9 auxiliary cells: two reversed rounds across seven core routes; JSON at 1/2/4 workers with 256 connections; single matched JSON 10k/s, echo 4k/s and 20-second sync 10k/s samples; idle connection RSS, WebSocket and SSE checks. All HTTP statuses and body lengths came from the oha SQLite record. Exact semantic bodies were checked before and after each HTTP load. Core rounds used 64 connections, 0.5-second warmup and 2-second measurement. The full commands, fixtures, package versions and process cleanup receipts are in `raw.json`.

## Integrity and comparison gate

- 10,104,177 measured responses succeeded; 0 measured transport errors, 0 unexpected statuses, 0 wrong body lengths, 0 HTTP cell exceptions, and 0 server cleanup errors. All 78 HTTP/auxiliary cleanup receipts had zero survivors, tracking or signal errors, and forced kills. All nine auxiliary checks were valid.
- 67/69 HTTP cells passed the predeclared response, warmup, host, client and cleanup comparison gate. The two ineligible cells were FastAPI JSON scaling warmups (256 connections); they had 1,183 warmup connection errors combined, but their measured windows had no errors. No HTTP cell crossed the 60% total-host-CPU flag. Candidate and local RC3 WebSocket clients were client-bound and have no qualified server throughput ratio.
- Throughput ratios below are **within-round retained ÷ comparator**, with median [min,max] and n. An n=2 ratio is descriptive, not a tight confidence interval. CPU efficiency is comparator CPU µs per successful response ÷ retained CPU µs per successful response; above 1 favors retained. Server CPU and RSS cover the process tree. CPU timing includes an oha subprocess/export tail and is approximate; low client CPU over that outer wall interval does not prove active-load client headroom. USS was unavailable on macOS.

## Core route throughput and CPU

| Workload | Retained median RPS | Local RC3 median RPS | FastAPI median RPS | Retained/local RC3 eligible | Retained/FastAPI eligible | Local RC3/retained CPU efficiency |
| --- | ---: | ---: | ---: | --- | --- | --- |
| echo1000 | 1701 | 1907 | 975 | 0.88× [0.78, 0.99], n=2 | 1.74× [1.43, 2.05], n=2 | 0.88× [0.78, 0.99], n=2 |
| echo | 63906 | 63552 | 14183 | 1.00× [0.99, 1.01], n=2 | 4.55× [4.39, 4.72], n=2 | 1.00× [1.00, 1.01], n=2 |
| fanout | 20755 | 20997 | 10045 | 0.99× [0.96, 1.02], n=2 | 2.08× [2.00, 2.16], n=2 | 0.99× [0.97, 1.02], n=2 |
| io | 52173 | 52104 | 19803 | 1.00× [1.00, 1.00], n=2 | 2.72× [2.28, 3.16], n=2 | 1.04× [1.00, 1.07], n=2 |
| json | 126066 | 134133 | 24703 | 0.92× [0.85, 0.99], n=2 | 5.02× [4.71, 5.33], n=2 | 0.94× [0.89, 0.99], n=2 |
| models256 | 32562 | 28870 | 5437 | 1.13× [1.12, 1.13], n=2 | 6.05× [5.76, 6.35], n=2 | 1.14× [1.14, 1.14], n=2 |
| sync-stream | 20560 | 20184 | 1797 | 1.01× [0.99, 1.03], n=2 | 11.37× [11.04, 11.70], n=2 | 1.01× [1.00, 1.03], n=2 |

The retained code shows a consistent 12–13% throughput gain on the 256-model route in both rounds, with approximately 12% lower server CPU per response. Echo, I/O, fanout and sync-stream are near the local RC3 baseline. JSON is lower in both rounds (0.85× and 0.99×); the 1,000-item echo is lower in both rounds (0.78× and 0.99×), with higher CPU per response. These last two routes do not support an overall speed or cost improvement claim.

| Core route | Round | Retained RPS | Local RC3 RPS | Retained/local RC3 | Retained CPU µs | Local RC3 CPU µs | Retained/local RC3 host CPU % |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| echo1000 | 1 | 1376 | 1767 | 0.78× | 721.5 | 559.5 | 20.4/25.6 |
| echo1000 | 2 | 2026 | 2048 | 0.99× | 490.1 | 485.7 | 25.9/25.3 |
| echo | 1 | 48684 | 49027 | 0.99× | 19.8 | 19.8 | 36.1/34.4 |
| echo | 2 | 79128 | 78077 | 1.01× | 12.5 | 12.7 | 23.2/23.9 |
| fanout | 1 | 17404 | 16984 | 1.02× | 56.6 | 57.6 | 24.4/28.4 |
| fanout | 2 | 24106 | 25010 | 0.96× | 40.7 | 39.4 | 32.9/28.8 |
| io | 1 | 50219 | 50058 | 1.00× | 13.7 | 13.7 | 29.9/26.4 |
| io | 2 | 54128 | 54150 | 1.00× | 9.4 | 10.0 | 23.9/31.4 |
| json | 1 | 85009 | 99729 | 0.85× | 10.8 | 9.6 | 42.7/38.9 |
| json | 2 | 167123 | 168537 | 0.99× | 5.9 | 5.9 | 29.2/29.0 |
| models256 | 1 | 26845 | 23960 | 1.12× | 36.4 | 41.4 | 31.8/27.2 |
| models256 | 2 | 38279 | 33779 | 1.13× | 25.8 | 29.5 | 36.5/25.4 |
| sync-stream | 1 | 15725 | 15807 | 0.99× | 71.8 | 71.7 | 33.9/36.2 |
| sync-stream | 2 | 25395 | 24561 | 1.03× | 45.7 | 47.0 | 37.2/37.0 |

| Route | Retained p99 ms median | Local RC3 p99 ms median | FastAPI p99 ms median | Retained RSS MB median | Local RC3 RSS MB median | FastAPI RSS MB median |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| echo1000 | 46.9 | 48.5 | 874.8 | 90.2 | 90.2 | 62.2 |
| echo | 1.7 | 1.7 | 9.0 | 80.0 | 79.8 | 55.8 |
| fanout | 8.0 | 7.9 | 15.3 | 81.6 | 81.3 | 57.8 |
| io | 1.8 | 1.8 | 9.8 | 79.8 | 79.6 | 56.0 |
| json | 1.1 | 0.9 | 5.2 | 84.4 | 79.2 | 60.3 |
| models256 | 3.1 | 3.2 | 24.2 | 82.8 | 82.9 | 56.0 |
| sync-stream | 5.2 | 5.3 | 50.1 | 84.4 | 84.7 | 63.3 |

## Worker scaling and fixed load

The scaling JSON route held 256 connections across 1, 2 and 4 workers. Ratios to local RC3 are clean in both rounds. FastAPI 1-worker scaling has no eligible pair because of the two warmup failures; raw FastAPI medians remain visible.

| Workers | Retained median RPS | Local RC3 median RPS | FastAPI raw median RPS | Retained/local RC3 eligible | Retained/FastAPI eligible |
| ---: | ---: | ---: | ---: | --- | --- |
| 1 | 165883 | 164485 | 29631 | 1.01× [1.01, 1.01], n=2 | — (n=0) |
| 2 | 208368 | 203869 | 55963 | 1.02× [1.01, 1.03], n=2 | 3.73× [3.57, 3.89], n=2 |
| 4 | 190502 | 193628 | 91201 | 0.98× [0.96, 1.01], n=2 | 2.09× [1.96, 2.22], n=2 |

Retained medians were 165,883, 208,368 and 190,502 RPS at 1/2/4 workers: four workers were 0.91× the two-worker value. Local RC3 showed a similar plateau (164,485/203,869/193,628 RPS). This macOS comparison does not demonstrate a worker-scaling fix. A separate Linux worker/vCPU qualification is required for a production scaling claim.

All three variants passed the reference SLO in the single offered-load JSON 10k/s, echo 4k/s and 20-second sync 10k/s samples. Since offered rate constrained throughput, compare CPU and p99 rather than treating equal RPS as equal capacity. Each row below is one sample, not a repeated estimate.

| Offered workload | Variant | Successful goodput RPS | p99 ms | Server CPU µs/success | Aggregate RSS MB | SLO |
| --- | --- | ---: | ---: | ---: | ---: | --- |
| json | retained | 9996 | 2.3 | 17.0 | 79.4 | 1/1 |
| json | local-rc3 | 9993 | 2.3 | 16.4 | 79.1 | 1/1 |
| json | fastapi | 9998 | 1.8 | 42.4 | 55.3 | 1/1 |
| echo | retained | 3998 | 1.4 | 41.9 | 79.4 | 1/1 |
| echo | local-rc3 | 3997 | 0.7 | 39.4 | 78.9 | 1/1 |
| echo | fastapi | 4000 | 1.4 | 100.3 | 55.5 | 1/1 |
| sync | retained | 10000 | 6.5 | 51.3 | 81.8 | 1/1 |
| sync | local-rc3 | 10000 | 7.2 | 50.8 | 81.6 | 1/1 |
| sync | fastapi | 10000 | 5.0 | 82.8 | 59.0 | 1/1 |

## Idle memory and bidirectional protocols

| Metric | Retained | Local RC3 | FastAPI |
| --- | ---: | ---: | ---: |
| Idle aggregate RSS MB | 78.1 | 77.8 | 54.5 |
| RSS at 1,024 idle sockets MB | 91.0 | 90.7 | 66.9 |
| Incremental RSS bytes/socket | 12624 | 12544 | 12160 |
| WebSocket echoes/s (client-bound on retained/local RC3) | 275628 | 253280 | 147720 |
| WebSocket p99 ms | 3.3 | 3.7 | 14.6 |
| SSE p99 first event ms | 5.6 | 5.5 | 17.2 |

All 1,024 idle sockets per variant were observed and closed; incremental socket RSS was about 12–13 KB across variants. Retained Bessemer had about 78 MB idle aggregate RSS versus FastAPI's 55 MB because the observed process trees differ. Both Bessemer WebSocket clients reached their client CPU bound, so the measured echo rates cannot rank server capacity. All 32 SSE streams per variant validated; these are one-off latency probes, not distribution estimates.

## Conclusion

The retained code is functionally clean in this matrix and improves the 256-model route. It is approximately level with tagged RC3 on most core routes, slower on JSON and 1,000-item echo in these two rounds, and does not improve the macOS 2→4-worker plateau. Against FastAPI it is faster on the closed-loop routes here, while using more idle memory. The local data do not establish an overall throughput or deployment-cost gain from this patch. `summary.json` preserves every paired ratio and per-variant range; `raw.json` preserves all samples and provenance. The earlier 18bd trial remains separate.
