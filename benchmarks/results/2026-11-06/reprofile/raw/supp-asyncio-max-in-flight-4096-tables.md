
#### GET /json

| server | conns | req/s (run1 / run2) | req/s median | CPUs | us CPU/req (run1 / run2) | us CPU/req median | ctx sw/req | p50 ms | p99 ms | p99.9 ms | oha util | notes |
|---|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|---|
| bessemer-asyncio | 1024 | 41,255 / 41,279 | 41,267 | 0.98 | 23.85 / 23.82 | 23.84 | 0.464 | 26.649 | 52.160 | 65.011 | 0.30 / 0.30 (3 cores) |  |

#### GET /items/7?q=abc

| server | conns | req/s (run1 / run2) | req/s median | CPUs | us CPU/req (run1 / run2) | us CPU/req median | ctx sw/req | p50 ms | p99 ms | p99.9 ms | oha util | notes |
|---|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|---|
| bessemer-asyncio | 1024 | 32,447 / 36,031 | 34,239 | 0.98 | 30.30 / 27.35 | 28.82 | 0.494 | 32.347 | 63.222 | 78.908 | 0.26 / 0.27 (3 cores) |  |

#### POST /echo

| server | conns | req/s (run1 / run2) | req/s median | CPUs | us CPU/req (run1 / run2) | us CPU/req median | ctx sw/req | p50 ms | p99 ms | p99.9 ms | oha util | notes |
|---|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|---|
| bessemer-asyncio | 1024 | 33,541 / 33,697 | 33,619 | 0.99 | 29.44 / 29.31 | 29.38 | 0.292 | 33.560 | 72.516 | 107.523 | 0.26 / 0.26 (3 cores) |  |
