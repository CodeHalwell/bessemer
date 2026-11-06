| config | route | conns | req/s (200) | us CPU/req (+-) | p50 / p99 / p99.9 ms | non-200 |
|---|---|--:|--:|--:|--:|--:|
| branch-default (fixed) | GET /json | 1024 | 84,913 | **11.68** (+-0.03) | 11.71 / 18.48 / 25.0 | 0 |
| branch-default (fixed) | GET /items/7?q=abc | 1024 | 82,842 | **11.98** (+-0.11) | 12.06 / 18.02 / 25.3 | 0 |
| branch-default (fixed) | POST /echo | 1024 | 68,648 | **14.46** (+-0.04) | 14.61 / 19.01 / 24.7 | 0 |
| branch-default (fixed) | GET /json | 64 | 94,398 | **10.58** (+-0.15) | 0.67 / 1.12 / 1.9 | 0 |
| main-rust (same session) | GET /json | 1024 | 78,211 | **12.70** (+-0.03) | 12.78 / 18.55 / 29.9 | 0 |
