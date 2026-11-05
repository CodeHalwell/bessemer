# Several event loops in one worker (free-threaded 3.14t)

**Question:** with the GIL disabled, can one worker process spread async
handlers over cores by running several asyncio loops, and how close does that
get to several worker processes?

**Setup:** one 4-core host, CPython 3.14.6t (GIL disabled), Pydantic 2.12. The
server runs on CPUs 0-2 and `oha` runs on CPU 3 with 64 connections. Each run
is 10 s after a 3 s warm-up, with three rounds interleaved. The app is
[`benchmarks/compare/bessemer_app.py`](../../../compare/bessemer_app.py). Run
it with `./run.sh PYTHON OHA > raw.txt`.

| Route | 1 worker, 1 loop | 1 worker, 3 loops | 3 workers, 1 loop each |
| --- | ---: | ---: | ---: |
| `/json` | 139.9k | 130.6k | 113.5k |
| `/items/{id}` | 133.9k | 119.6k | 117.3k |
| `POST /echo` (20 Pydantic items) | 14.2k | 26.1k | 28.9k |
| `/io` (1 ms sleep) | 31.5k | 32.3k | 36.6k |

These are medians of the three rounds in [raw.txt](raw.txt). No run returned
anything other than 200.

Resident memory after warm-up on `/echo`:

| Setup | Worker processes | Supervisor |
| --- | ---: | ---: |
| 1 worker, 1 loop | 63 MiB | 37 MiB |
| 1 worker, 3 loops | 66 MiB | 37 MiB |
| 3 workers, 1 loop each | 179 MiB | 37 MiB |

## Reading

- Python-bound work scales. `/echo` spends its time validating and dumping
  Pydantic models. Three loops served 1.84 times the requests of one loop,
  about 90% of three processes, while using 113 MiB less memory.
- The tiny routes did not get faster. At about 130-140k/s the single-core
  client is the limit, so every setup measures the client. One loop is
  already enough here, and the extra loops cost 7-11%. A likely cause, not
  yet measured, is that requests spread over three queues, so each loop
  drains smaller batches.
- `/io` is limited by its 1 ms timer and the 64 connections (at most 64k/s in
  theory), not by the loops.
- The default stays at one loop. Use `loops` on free-threaded builds when the
  handlers do real Python work, or when memory per core matters more than the
  last 10%.
