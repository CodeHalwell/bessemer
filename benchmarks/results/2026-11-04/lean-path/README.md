# Lean worker path for plain async routes (exploratory)

This is a same-host interleaved A/B on the same 4-vCPU VM and harness as
`../eager-start/`. It is a diagnostic, not the formal M0 gate.

- `m4`: `main` after PRs #3 to #10 merged
- `lean`: this change

Each configuration ran one worker with `--workers 1`, with the server pinned to
CPU 0 and oha pinned to CPU 2. Load was 50 keep-alive connections, a 3-second
warmup, then three 15-second samples per endpoint. The sequence ran twice,
interleaved as m4, lean, m4, lean. Every sample returned only HTTP 200.
Raw output is in `raw/`.

| Endpoint | `main` | This change | Change |
| --- | ---: | ---: | ---: |
| `/json` | 58,710 / 58,956 | 73,255 / 73,882 | +25% |
| `/items/7?q=abc` | 46,058 / 46,212 | 58,154 / 57,977 | +26% |
| `/io` (awaited 1 ms) | 19,267 / 19,167 | 20,662 / 20,531 | +7% |

Values are run 1 / run 2 medians in requests per second, and the percentages
use the mean of each pair.

`drainbench.source.txt` drives `Worker._drain_native` with a stand-in bridge,
so it times only the Python side of a request on CPU 0:

| Route | `main` | This change |
| --- | ---: | ---: |
| `/json` | 4.27 µs | 2.47 µs |
| `/items/{item_id}` | 5.41 µs | 3.56 µs |

An async handler that takes only native inputs (no dependencies, before-hooks,
background tasks or principal) is now called directly. It skips the
`AsyncExitStack`, `BackgroundTasks` and exit bookkeeping, and a request with no
metadata is built without `Request.__init__`'s parsing. Streaming results still
get an exit stack and background tasks, created only when they stream.
