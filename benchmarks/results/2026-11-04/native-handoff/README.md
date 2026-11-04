# Native Tokio-to-asyncio handoff (exploratory)

This is a same-host interleaved A/B on the same 4-vCPU VM and harness as
`../framework-overhead-pass1/`. It is a diagnostic, not the formal M0 gate.

- `base`: `main` at `969ff57`
- `cand`: the framework-overhead pass (PR #3, `0f7f491`)
- `next`: this change, the native batched handoff

Each configuration ran one worker with `--workers 1`, with the server pinned to
CPU 0 and oha pinned to CPU 2. Load was 50 keep-alive connections, a 3-second
warmup, then three 15-second samples per endpoint. The sequence ran twice,
interleaved as base, cand, next, base, cand, next. Every sample returned only
HTTP 200. Raw output is in `raw/`.

| Endpoint | main | PR #3 | This change | vs PR #3 | vs main |
| --- | ---: | ---: | ---: | ---: | ---: |
| `/json` | 18,434 / 18,295 | 30,109 / 30,485 | 39,110 / 38,955 | +29% | +113% |
| `/items/7?q=abc` | 15,587 / 15,924 | 23,873 / 23,783 | 29,689 / 29,722 | +25% | +89% |
| `/io` (awaited 1 ms) | 12,098 / 12,023 | 15,015 / 14,478 | 17,428 / 16,767 | +16% | +42% |

Values are run 1 / run 2 medians in requests per second, and the percentages
use the mean of each pair. For reference, the foundation build measured
26.4k, 22.9k and 15.7k on this host in the earlier run, so all three endpoints
are now above it.

`perf` was run on the worker under `/json` load. It shows no Python execution
on Tokio threads any more. Python runs only on the asyncio loop thread, at about
35% of samples. The Tokio side is now mostly kernel TCP work on loopback plus
about 6.5% in `malloc`/`free`.
