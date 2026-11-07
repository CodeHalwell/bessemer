# Rejected Future-only capped Linux async comparison

The Future-only cache is rejected: at four vCPUs and 20,000 offered I/O requests/s, it missed the diagnostic SLO in both rounds while main passed both.
No runtime optimization from these constructor-cache trials is retained.

Two 2-second samples per framework/workload/core point, with 0.5-second warmups.
The client ran outside identical 1/2/4-vCPU, 512 MiB, 256-PID containers.
The baseline and Future-only candidate both used the Bessemer Rust loop and matching worker count.
Runs took place in a local Docker Desktop Linux VM on Apple silicon. Two-second windows show outliers and are not capacity or production-cost evidence.
Actual balanced variant order was baseline/Future-only/FastAPI, then FastAPI/Future-only/baseline.
The older Task+Future matrix is rejected evidence and is not included in these comparisons.
CPU µs/200 uses server-process CPU divided by completed successful responses; CPU ratios appear only where both frameworks met the declared diagnostic SLO twice.

| vCPU | Workload | Offered/s | Passes B/N/F | CPU µs/200 B/N/F | Future-only CPU change | FastAPI/Future-only CPU | p99 ms B/N/F | SLO goodput/s B/N/F | RSS MiB B/N/F | PSS MiB B/N/F |
|---:|---|---:|---|---|---:|---:|---|---|---|---|
| 1 | json | 5,000 | 2/2/1 | 38.0/38.5/180.8 | 1.3% | — | 4.9/38.9/174.4 | 4997/4990/2700 | 90.2/89.4/50.8 | 75.9/75.2/46.2 |
| 1 | io | 5,000 | 2/2/0 | 57.0/55.5/207.1 | -2.6% | — | 30.9/16.3/346.9 | 4981/4998/946 | 90.3/90.0/51.3 | 76.1/75.7/46.7 |
| 1 | fanout | 500 | 2/2/2 | 370.0/360.0/610.0 | -2.7% | 1.69× | 14.2/5.6/5.7 | 500/500/500 | 89.7/90.1/51.3 | 75.5/75.8/46.7 |
| 1 | echo | 1,500 | 2/2/2 | 110.0/110.0/388.6 | -0.0% | 3.53× | 6.1/8.6/7.7 | 1500/1500/1499 | 90.3/90.4/51.3 | 76.0/76.1/46.7 |
| 1 | dependencies | 2,500 | 2/2/2 | 121.0/125.0/291.1 | 3.3% | 2.33× | 13.6/23.3/32.5 | 2499/2499/2496 | 90.0/90.2/51.4 | 75.7/75.9/46.8 |
| 1 | mixed | 150 | 2/2/1 | 3533.3/3933.3/4400.0 | 11.3% | — | 13.0/19.6/53.1 | 150/150/142 | 89.6/90.0/51.5 | 75.3/75.7/47.0 |
| 1 | events | 1,000 | 2/2/0 | 405.0/390.0/841.9 | -3.7% | — | 18.7/6.2/134.9 | 1000/1000/289 | 89.7/90.0/55.1 | 75.5/75.8/50.4 |
| 2 | json | 10,000 | 2/1/0 | 36.5/34.3/171.0 | — | — | 17.1/66.3/136.5 | 9997/9233/5282 | 141.8/141.6/148.2 | 112.2/112.3/116.3 |
| 2 | io | 10,000 | 2/1/0 | 46.8/50.5/194.3 | — | — | 8.2/58.7/372.3 | 9996/9184/996 | 142.4/141.6/148.6 | 112.9/112.3/116.8 |
| 2 | fanout | 1,000 | 2/2/2 | 237.6/310.0/592.6 | 30.5% | 1.91× | 6.4/15.8/19.1 | 1000/1000/1000 | 142.0/141.2/148.7 | 112.5/112.0/116.9 |
| 2 | echo | 3,000 | 2/2/2 | 90.0/101.7/356.7 | 13.0% | 3.51× | 4.2/11.9/24.4 | 2999/2999/3000 | 142.8/141.6/148.8 | 113.1/112.2/117.0 |
| 2 | dependencies | 5,000 | 2/2/2 | 104.6/117.6/237.1 | 12.4% | 2.02× | 6.1/13.1/22.7 | 4996/4997/4998 | 142.2/141.8/149.2 | 112.6/112.3/117.5 |
| 2 | mixed | 300 | 1/2/2 | 3641.7/3483.3/4091.7 | — | — | 63.4/12.0/27.3 | 292/300/300 | 142.1/142.1/149.2 | 112.4/112.6/117.5 |
| 2 | events | 2,000 | 2/2/1 | 350.0/368.7/650.0 | 5.3% | — | 5.1/16.4/65.0 | 2000/2000/1966 | 142.2/142.3/153.7 | 112.6/112.8/121.8 |
| 4 | json | 20,000 | 2/2/0 | 33.2/34.5/161.6 | 3.9% | — | 13.1/24.6/530.1 | 19953/19994/1010 | 246.9/248.2/249.8 | 186.6/188.2/189.5 |
| 4 | io | 20,000 | 2/0/0 | 48.9/49.1/188.6 | — | — | 17.0/172.8/800.6 | 19972/4945/502 | 245.8/247.5/250.3 | 185.5/187.6/190.0 |
| 4 | fanout | 2,000 | 2/2/2 | 282.5/276.3/499.0 | -2.2% | 1.81× | 6.6/13.3/15.0 | 2000/2000/1999 | 245.5/247.3/250.7 | 185.2/187.2/190.5 |
| 4 | echo | 6,000 | 1/2/0 | 93.8/96.3/370.9 | — | — | 55.3/6.0/69.9 | 5743/5998/5709 | 246.2/247.9/250.8 | 185.5/187.6/190.7 |
| 4 | dependencies | 10,000 | 2/1/1 | 108.8/115.3/249.7 | — | — | 7.0/55.4/107.7 | 9972/9488/7521 | 246.0/247.7/251.2 | 185.1/187.2/191.0 |
| 4 | mixed | 600 | 2/1/1 | 3845.8/3984.9/4512.1 | — | — | 16.6/37.8/70.2 | 600/593/564 | 245.8/247.3/251.2 | 184.9/186.7/191.1 |
| 4 | events | 4,000 | 2/2/1 | 324.5/328.8/601.5 | 1.3% | — | 9.6/25.6/60.6 | 3999/3995/3736 | 245.7/247.7/263.0 | 185.2/187.3/202.6 |

Totals: {"completed": 1239341, "oom_kills": 0, "successful": 1239341, "transport_errors": 0, "unexpected_status": 0, "warmup_errors": {}, "warmup_statuses": {"200": 304254}, "wrong_body_length": 0}.
Resource/cleanup validation errors: [].
Raw oha output, SQLite path/hash and cgroup/PSS samples are preserved in each matrix JSON.
Source/image/wheel hashes and exact commands are in `final-provenance.json` and `final-matrix-commands.txt`.
