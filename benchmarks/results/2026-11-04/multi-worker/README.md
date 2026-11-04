# Multi-worker scaling (exploratory)

Same 4-vCPU VM and harness as `../eager-start/`, measured on the eager-start
build (PR #9). Single 8-second runs of three samples each, so treat these as
indicative. The server and oha were pinned with `taskset` as shown. Every
sample counted here returned only HTTP 200.

| Workers | Server CPUs | oha CPUs | Connections | `/json` | `/io` | `/healthz` (native) |
| ---: | --- | --- | ---: | ---: | ---: | ---: |
| 1 | 0 | 2 | 50 | 55.8k | 19.4k | 73.0k |
| 1 | 0,1 | 2,3 | 50 | 68.4k | | 126.3k |
| 2 | 0,1 | 2,3 | 50 | 92.0k to 95.4k | 23.3k to 24.7k | 164.7k |
| 2 | 0,1 | 2,3 | 200 | 103.9k | 56.6k | |

What this shows:

- Native health scales 2.25x from one core to two, so the load generator is
  not the limit at these rates.
- One worker given two cores reaches about 68k for `/json`. Its single asyncio
  loop thread is then the ceiling, which is why the default (`workers = 0`)
  starts one worker per core.
- Two workers on two cores reach 92k to 104k for `/json`, 1.65x to 1.85x a
  single pinned core.
- `/io` at 50 connections is bound by concurrency, not CPU: each request holds
  its connection for the 1 ms sleep plus overhead. At 200 connections two
  workers serve 56.6k. One worker at 200 connections sheds with 503 because
  the default `max_in_flight` is 128 per worker, which is intended.
- One Tokio thread per worker was tried and rejected: one worker on one core
  fell from 55.7k to 27.7k for `/json`, and two workers fell to 62.9k. Three
  threads on two cores was also slower than two. The fixed two-thread runtime
  stays.
- With 50 connections the shared listener usually splits them about evenly
  between two workers, with occasional skew such as 14/38. Workers are not
  pinned to separate cores, so the skew costs little here; per-worker
  `SO_REUSEPORT` sockets were not pursued.
