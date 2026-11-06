
#### GET /json

| server | conns | req/s | CPUs | µs CPU/req | ctx sw/req | p50 ms | p99 ms | p99.9 ms | oha cores / util | flags |
|---|--:|--:|--:|--:|--:|--:|--:|--:|--:|---|
| bessemer-asyncio | 1024 | 41,267 | 0.98 | **23.84** | 0.464 | 26.649 | 52.16 | 65.0 | 3 / 0.30 |  |

#### GET /items/7?q=abc

| server | conns | req/s | CPUs | µs CPU/req | ctx sw/req | p50 ms | p99 ms | p99.9 ms | oha cores / util | flags |
|---|--:|--:|--:|--:|--:|--:|--:|--:|--:|---|
| bessemer-asyncio | 1024 | 34,239 | 0.98 | **28.82** | 0.494 | 32.347 | 63.22 | 78.9 | 3 / 0.26 |  |

#### POST /echo

| server | conns | req/s | CPUs | µs CPU/req | ctx sw/req | p50 ms | p99 ms | p99.9 ms | oha cores / util | flags |
|---|--:|--:|--:|--:|--:|--:|--:|--:|--:|---|
| bessemer-asyncio | 1024 | 33,619 | 0.99 | **29.38** | 0.292 | 33.560 | 72.52 | 107.5 | 3 / 0.26 |  |
