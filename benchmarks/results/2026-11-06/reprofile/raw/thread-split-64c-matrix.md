| cell | run | total us/req (perf stat) | threads: comm = us/req (CPUs) |
|---|--:|--:|---|
| matrix/axum/echo/c64 | 1 | 10.61 | tokio-rt-worker = 10.58 (0.98) |
| matrix/axum/echo/c64 | 2 | 10.89 | tokio-rt-worker = 10.74 (0.96) |
| matrix/axum/items/c64 | 1 | 8.12 | tokio-rt-worker = 8.11 (0.84) |
| matrix/axum/items/c64 | 2 | 8.11 | tokio-rt-worker = 8.13 (0.84) |
| matrix/axum/json/c64 | 1 | 8.03 | tokio-rt-worker = 8.05 (0.77) |
| matrix/axum/json/c64 | 2 | 8.03 | tokio-rt-worker = 8.06 (0.79) |
| matrix/bessemer-asyncio/echo/c64 | 1 | 16.27 | python = 5.50 (0.33); tokio-rt-worker = 5.38 (0.33); tokio-rt-worker = 5.38 (0.33) |
| matrix/bessemer-asyncio/echo/c64 | 2 | 17.04 | python = 5.74 (0.33); tokio-rt-worker = 5.67 (0.33); tokio-rt-worker = 5.64 (0.32) |
| matrix/bessemer-asyncio/items/c64 | 1 | 12.47 | tokio-rt-worker = 4.76 (0.37); tokio-rt-worker = 4.75 (0.37); python = 2.95 (0.23) |
| matrix/bessemer-asyncio/items/c64 | 2 | 12.22 | tokio-rt-worker = 4.69 (0.38); tokio-rt-worker = 4.68 (0.38); python = 2.87 (0.23) |
| matrix/bessemer-asyncio/json/c64 | 1 | 11.44 | tokio-rt-worker = 4.40 (0.38); tokio-rt-worker = 4.38 (0.38); python = 2.65 (0.23) |
| matrix/bessemer-asyncio/json/c64 | 2 | 11.35 | tokio-rt-worker = 4.36 (0.38); tokio-rt-worker = 4.36 (0.38); python = 2.62 (0.23) |
| matrix/bessemer-rust-iouring/echo/c64 | 1 | 14.93 | python = 14.82 (0.98) |
| matrix/bessemer-rust-iouring/echo/c64 | 2 | 15.31 | python = 15.20 (0.98) |
| matrix/bessemer-rust-iouring/items/c64 | 1 | 10.18 | python = 10.10 (0.97) |
| matrix/bessemer-rust-iouring/items/c64 | 2 | 10.58 | python = 10.47 (0.96) |
| matrix/bessemer-rust-iouring/json/c64 | 1 | 9.34 | python = 9.26 (0.96) |
| matrix/bessemer-rust-iouring/json/c64 | 2 | 9.32 | python = 9.24 (0.96) |
| matrix/bessemer-rust/echo/c64 | 1 | 15.71 | python = 15.62 (0.99) |
| matrix/bessemer-rust/echo/c64 | 2 | 15.69 | python = 15.54 (0.98) |
| matrix/bessemer-rust/items/c64 | 1 | 10.94 | python = 10.85 (0.97) |
| matrix/bessemer-rust/items/c64 | 2 | 11.07 | python = 10.99 (0.98) |
| matrix/bessemer-rust/json/c64 | 1 | 10.03 | python = 9.99 (0.97) |
| matrix/bessemer-rust/json/c64 | 2 | 9.84 | python = 9.79 (0.98) |
