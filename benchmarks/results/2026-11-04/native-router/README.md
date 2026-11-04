# Native routing and a borrowed request head (exploratory)

These are same-host interleaved A/Bs on the same 4-vCPU VM and harness as
`../lean-path/`. They are diagnostics, not the formal M0 gate.

- `lean`: `main` with the lean worker path (PR #16)
- `handle`: the native handler borrows the request head instead of cloning
  headers, method, path and query strings per request
- `router`: requests are routed by `src/router.rs` instead of the axum
  `Router` and tower layers

Each configuration ran one worker with `--workers 1`, with the server pinned to
CPU 0 and oha pinned to CPU 2. Load was 50 keep-alive connections, a 3-second
warmup, then three 15-second samples per endpoint. Each pair ran twice,
interleaved. Every sample returned only HTTP 200. Raw output is in `raw/`
(`ab6-*` is lean against handle, `ab7-*` is handle against router).

| Endpoint | lean | handle | router | lean to router |
| --- | ---: | ---: | ---: | ---: |
| `/json` | 73,681 / 71,875 | 75,718 / 73,737 | 85,842 / 85,337 | +18% |
| `/items/7?q=abc` | 56,920 / 56,918 | 60,680 / 59,875 | 67,044 / 68,470 | +19% |
| `/io` (awaited 1 ms) | 20,606 / 20,428 | 20,215 / 20,453 | 20,980 / 21,432 | +3% |

Values are run 1 / run 2 medians in requests per second. The handle column
comes from the second run pair, against which the router was measured; the
first pair gave 71,626 / 76,859 and 59,040 / 60,009.

The router does one `matchit` lookup (the matcher axum 0.8 uses) and runs the
native controls around the handler directly. That removes these per-request
costs: the router clone, the method router, the boxed handler service, the
controls layer future, `Path` extraction into a map, and the request
extension inserts. The 404 and 405 fallbacks, `Allow`, `HEAD` handling,
percent-decoding and telemetry templates are unchanged, and `src/router.rs`
tests them.
