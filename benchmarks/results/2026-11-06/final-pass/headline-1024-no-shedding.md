| route | conns | main default (asyncio, max_in_flight 4096) us/req | branch default (rust, max_in_flight 4096) us/req | axum us/req | branch vs axum | main default vs axum | branch vs main default |
|---|--:|--:|--:|--:|--:|--:|--:|
| GET /json | 1024 | 20.88 | 11.34 | 13.55 | -16% | +54% | -46% |
| GET /items/7?q=abc | 1024 | 22.52 | 11.80 | 14.81 | -20% | +52% | -48% |
| POST /echo | 1024 | 23.62 | 14.57 | 20.61 | -29% | +15% | -38% |
