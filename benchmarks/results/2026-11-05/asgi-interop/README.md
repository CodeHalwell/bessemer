# ASGI middleware, mounts and lifespan state: hot-path check

Do mounted ASGI apps, ASGI middleware and lifespan state slow down apps that
use none of them? Measured with the `benchmarks/compare/bessemer_app.py` app:
one worker pinned to CPU 0, `oha` with 64 connections pinned to CPU 3, 2 s
warm-up then 8 s per route. Both builds use the same native extension; the
baseline is the Python package from `main` at `04bbc82`. Three rounds,
alternating baseline then branch.

| Route | Baseline req/s (median of 3) | Branch req/s (median of 3) | Difference |
| --- | ---: | ---: | ---: |
| `GET /json` | 92,072 | 90,600 | -1.6% |
| `GET /items/7?q=abc` | 67,791 | 68,320 | +0.8% |
| `POST /echo` (Pydantic) | 19,635 | 19,595 | -0.2% |
| `GET /io` (1 ms sleep) | 25,473 | 25,803 | +1.3% |

`/json` was rerun with the branch first in each round, four rounds:
baseline median 91,889, branch 91,180 (-0.8%). Run-to-run spread was up to
3.5%, so every difference is within noise. The code on the `/json` path does
less work than before (`request.state` is no longer built for every request),
and the ASGI code is only reached by apps that add ASGI middleware or mount
an ASGI app.

Exploratory, single host, short runs; not a formal performance gate.
