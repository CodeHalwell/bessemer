| route | conns | main default (asyncio, max_in_flight 4096) us/req | branch default (rust, max_in_flight 4096) us/req | axum us/req | branch vs axum | main default vs axum | branch vs main default |
|---|--:|--:|--:|--:|--:|--:|--:|
| GET /json | 1024 | 20.93 | 11.35 | 13.56 | -16% | +54% | -46% |
| GET /items/7?q=abc | 1024 | 22.57 | 11.82 | 14.83 | -20% | +52% | -48% |
| POST /echo | 1024 | 23.67 | 14.59 | 20.65 | -29% | +15% | -38% |
