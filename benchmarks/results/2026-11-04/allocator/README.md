# mimalloc as the native global allocator (exploratory)

This is a same-host interleaved A/B on the same 4-vCPU VM and harness as
`../native-handoff/`. It is a diagnostic, not the formal M0 gate.

- `next`: the native batched handoff (PR #7)
- `alloc`: this change, mimalloc as the Rust global allocator

Each configuration ran one worker with `--workers 1`, with the server pinned to
CPU 0 and oha pinned to CPU 2. Load was 50 keep-alive connections, a 3-second
warmup, then three 15-second samples per endpoint. The sequence ran twice,
interleaved as next, alloc, next, alloc. Every sample returned only HTTP 200.
Raw output is in `raw/`.

| Endpoint | PR #7 | This change | Change |
| --- | ---: | ---: | ---: |
| `/json` | 40,044 / 41,180 | 45,220 / 46,049 | +12% |
| `/items/7?q=abc` | 30,420 / 30,161 | 37,263 / 36,810 | +22% |
| `/io` (awaited 1 ms) | 18,313 / 18,407 | 18,870 / 18,950 | +3% |

Values are run 1 / run 2 medians in requests per second, and the percentages
use the mean of each pair. `perf` on PR #7 had put about 6.5% of worker samples
in glibc `malloc`/`free` on Tokio threads. The larger gain on `/items` fits its
extra per-request allocations (path and query parsing, argument maps).
`/io` is bound by the awaited sleep on the asyncio loop, so it moves least.
