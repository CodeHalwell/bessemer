| route | conns | main default (asyncio) us/req | branch default (rust) us/req | axum us/req | branch vs axum | main default vs axum | branch vs main default | notes |
|---|--:|--:|--:|--:|--:|--:|--:|---|
| GET /json | 64 | 13.73 | 10.52 | 11.64 | -10% | +18% | -23% |  |
| GET /json | 1024 | 27.59 | 14.82 | 13.56 | +9% | +103% | -46% | main: non-200; branch: non-200 |
| GET /items/7?q=abc | 64 | 14.40 | 10.82 | 11.83 | -9% | +22% | -25% |  |
| GET /items/7?q=abc | 1024 | 26.51 | 15.48 | 14.83 | +4% | +79% | -42% | main: non-200; branch: non-200 |
| POST /echo | 64 | 17.91 | 13.43 | 14.61 | -8% | +23% | -25% |  |
| POST /echo | 1024 | 27.67 | 17.59 | 20.65 | -15% | +34% | -36% | main: non-200; branch: non-200 |
