# Eager start for native-queue handlers (exploratory)

This is a same-host interleaved A/B on the same 4-vCPU VM and harness as
`../allocator/`. It is a diagnostic, not the formal M0 gate.

- `alloc`: mimalloc as the native allocator (PR #8)
- `eager`: this change, handlers from the native queue start eagerly

Each configuration ran one worker with `--workers 1`, with the server pinned to
CPU 0 and oha pinned to CPU 2. Load was 50 keep-alive connections, a 3-second
warmup, then three 15-second samples per endpoint. The sequence ran twice,
interleaved as alloc, eager, alloc, eager. Every sample returned only HTTP 200.
Raw output is in `raw/`.

| Endpoint | PR #8 | This change | Change |
| --- | ---: | ---: | ---: |
| `/json` | 44,836 / 45,180 | 55,987 / 55,795 | +24% |
| `/items/7?q=abc` | 36,440 / 36,266 | 45,611 / 45,960 | +26% |
| `/io` (awaited 1 ms) | 19,088 / 18,996 | 19,360 / 19,442 | +2% |

Values are run 1 / run 2 medians in requests per second, and the percentages
use the mean of each pair. A handler that returns without suspending now
completes inside the drain callback: no loop scheduling of its first step and
no done callback. `/io` suspends on its sleep, so it keeps the normal path and
moves little. Against `main` on this host (18.4k), one-core `/json` is now
about 3x.
