# Framework overhead, first pass (exploratory)

Same-host A/B of three wheels built from source on one 4-vCPU cloud VM
(Intel Xeon, CPython 3.13.18, oha 1.16.0). This is a diagnostic. It does not
meet the formal M0 gate, which needs separate server and load hosts, five
60-second samples and at most 3% spread.

- `found`: foundation commit `3a29c36` (0.1.0)
- `base`: `main` at `969ff57` (0.2.0a3)
- `cand`: this branch

Wheel hashes are in `wheels.sha256`. The server was pinned to CPU 0 with
`--workers 1` (the foundation CLI has no worker flag). oha was pinned to CPU 2
with 50 keep-alive connections. Each endpoint got a 3-second warmup and then
three 15-second samples. The full sequence ran twice, interleaved as
found, base, cand, found, base, cand. Every sample returned only HTTP 200. The
fixture is in `bench.source.txt`, and raw medians and samples are in `raw/`.

| Endpoint | Foundation | main | This branch | Change vs main |
| --- | ---: | ---: | ---: | ---: |
| `/json` | 26,116 / 26,677 | 18,403 / 18,127 | 29,519 / 28,843 | +59% |
| `/items/7?q=abc` | 22,832 / 22,919 | 15,532 / 15,755 | 23,417 / 23,822 | +51% |
| `/io` (awaited 1 ms sleep) | 15,801 / 15,615 | 12,198 / 12,174 | 14,689 / 14,461 | +20% |

Values are the run 1 / run 2 medians in requests per second. The percentage
uses the mean of the two medians. This host is about half as fast as the one
behind the earlier 55.4k/27.5k figures, but the main/foundation ratio is
similar (0.69 here against 0.64 there).

Before this change, profiling found these costs (perf on the worker under
`/json` load, plus cProfile of the loop thread):

- Every Tokio entry into Python created and then deleted a CPython thread
  state, including mapping and unmapping its evaluation stack. This took about
  8–10% of server CPU (`_PyThreadState_DeleteCurrent`, `mmap`/`munmap`).
- `jsonable` walked and copied every result in Python before Rust converted it
  again. It was about 25% of loop-thread time.
- Each request built a `Request` from empty metadata, a `DependencyResolver`
  and a per-request parameter map, even for routes that needed none of them.

`direct_worker.source.txt` drives the Worker without networking. Its median
rose from 60.2k to 86.2k requests/second for `/json`.
