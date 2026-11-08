# Suspended asyncio requests and main integration

All work uses `/home/cmux/wt/codex-echo` and `/home/cmux/venvs/codex`.
No main checkout, other agent worktree or other venv was changed.

## Main integration

- `d9b3773` merges main through `5926da6`; `91dab44` records qualification.
  Installed release wheel full check: 235 Rust tests passed (2 ignored),
  966 Python tests run (34 skipped), 23 harness tests passed, Rust formatting,
  warnings-denied Clippy, Ruff lint/format and shell syntax passed.
- `36865a3` merges main through `d7cfe84`, including lazy RequestHead, flat
  dependencies and small worker JSON envelopes becoming JsonReply. Deferred
  responses explicitly retain the full response path: JsonReply has no
  transport extensions, so it cannot own the AfterResponse delivery guard.
  The guard retains admission and cleanup budgets until background work ends.
  Plain worker JSON responses can take the new fast path.

Installed CPython 3.13.15 release wheels, auto loop, matrix common:app,
CONNECTIONS=1, 2,000 warmups, 3,000 measured requests, separate private vgdb
prefix and PID controls for every profile:

| Route | Before first merge | First merge | Second merge |
| --- | ---: | ---: | ---: |
| `/sync` | 99,619 | 99,502 | 99,160 |
| `/bg` | 240,515 | 240,967 | 225,617 |
| `/json` | 33,189 | 33,006 | 33,022 |
| matrix `/io` | not measured | 60,570 | 60,461 |
| compare `/io` | not measured | 60,623 | 60,422 |

With 64 connections and 6,000 measured requests, second-merge matrix `/io`
costs 48,222 instructions/request. Batching changes counts substantially;
compare only matching connection counts. Shared-host request rates are not
performance acceptance evidence.

## Profile

The single-connection compare profile adds roughly 27k instructions over
`/json`. Inclusive totals overlap; they are not additive:

| Component | Instructions/request | Share of total |
| --- | ---: | ---: |
| Task wakeup | 5,993 | 9.89% |
| native Handle destruction | 4,782 | 7.89% |
| driven-loop wait | 3,832 | 6.32% |
| Future callback scheduling | 2,737 | 4.52% |
| create_future entry and construction | 1,357 | 2.24% |
| call_later entry | 826 | 1.36% |
| timer heap registration | 539 | 0.89% |
| libc epoll_wait wrapper | 121 | 0.20% |
| libc timerfd_settime wrapper | 7 | 0.01% |

The syscall wrappers exclude kernel work. Short waits already use Linux
CLOCK_MONOTONIC timerfd (macOS uses kqueue); Tokio's millisecond timer wheel
only handles the coarse part of distant waits. `HURRY_AT=32` limits queued
unstarted work and is lowered for smaller capacity. It remains unchanged:
removing it risks admission failures under bursts. Timer registration is not
the main CPU cost in the single-connection profile.

Rejected experiments: grouping decrefs in a Handle Drop implementation raised
matrix `/io` to 61,060 from 60,570; a PyO3 freelist measured 60,131 but failed
the existing unclosed-loop garbage-collection regression; bound callback refs
and vectorcall Future construction measured 60,464 against the matching
second-merge baseline of 60,461. None is retained as an improvement.

## Timer resolution

`benchmarks/profile/sleep_resolution.py` measures sleeps on the installed,
embedded-runtime loop without an HTTP client. The first merged wheel measured
1,000 sequential 1 ms sleeps at 1.037 ms median and 1.115 ms p99; 32,000 sleeps
at concurrency 64 measured 1.051 ms median and 1.222 ms p99. The second-merge
sequential run measured 1.046 ms median and 1.200 ms p99. These shared-host
measurements show no systematic additional millisecond of rounding; scheduling
outliers remain possible under load. No wall-time bound is asserted in CI.

## Scheduler experiments (parked for adaptive admission)

The driven loop can run up to three consecutive ready turns on one Tokio poll.
Each asyncio turn still snapshots its ready queue, promotes due timers and
polls Python descriptors without blocking. It yields to Tokio after that
bounded burst, and waits normally as soon as nothing is ready. Timer deadlines,
Task/Future machinery, context entry, cancellation and hurry/admission logic
are unchanged. The new driven scheduling suite repeats the FIFO, cancellation,
timer, context, reentrant finalizer and collection checks with the embedded
runtime enabled; a socketpair regression verifies readiness is observed before
a stop callback on the next turn. Ninety focused event-loop tests passed.

| Route / connections | Second merge | Scheduler change | Change |
| --- | ---: | ---: | ---: |
| matrix `/io`, 1 | 60,461 | 59,327 | -1.88% |
| compare `/io`, 1 | 60,422 | 59,179 | -2.06% |
| matrix `/io`, 64 | 48,222 | 48,060 | -0.34% |
| matrix `/json`, 1 | 33,022 | 32,917 | -0.32% |
| matrix `/sync`, 1 | 99,160 | 97,804 | -1.37% |

The single-connection saving is measurable; the 64-connection result is small
and batching-dependent. The change does not close the suspended-request gap
or establish a request-rate gain. There is a syscall tradeoff: `strace -c` on
1,000 standalone sleeps using the unpacked saved baseline wheel and the final
installed wheel showed 1,001 -> 2,005 epoll_wait calls, while timerfd_settime
stayed exactly 1,000 and read stayed 1,296. The extra nonblocking Python-poller
checks preserve descriptor readiness between turns. Kernel cost is not included
in Callgrind. The final compare profile's driven wait fell from about 3,832 to
3,261 instructions/request, while the libc epoll wrapper grew from about 121
to 241. Inclusive profile totals are diagnostic, not an additive accounting.
A shared-host standalone sleep(0) timing varied and is not used as acceptance.

The final timing probe measured sequential 1 ms sleeps at 1.040 ms median,
1.113 ms p99 and 1.697 ms maximum; concurrency 64 measured 1.052 ms median,
1.125 ms p99 and 1.295 ms maximum over 32,000 sleeps. A separate earlier loaded
run had p99 up to 5.85 ms, so these are observations, not guarantees. The timer
keeps native resolution with no additional millisecond of rounding.

The broad scheduler experiment wheel SHA-256 is
`d2bc597d85a5906db848aa0e2ec763e1cc6136f57d02cad8e38476052a11a111`.
Its worker.py matches source byte for byte. Only GIL-enabled CPython 3.13.15
was executed locally; free-threaded and other supported Python versions still
require CI qualification. Python routes remain Tier 2 and formal M0 acceptance
remains open.

The broad change also changed `/bg` from 225,617 to 232,805 (+3.19%), so it
was not committed. Its repeated full check passed (235 Rust tests, 2 ignored;
998 Python tests, 34 skipped; 23 harness tests) after an io_uring listener-sharing
distribution test failed once and passed on retry. A narrower timer-only patch
was built next; its full check ran 999 Python tests with one failure in the
new ordinary-ready descriptor test, revealing a pre-existing driven-loop
readiness limitation that the broad patch had masked. The timer-only patch is
saved locally as `/tmp/codex-io-timer-turn.patch`; neither scheduler experiment
is retained in the branch. Adaptive admission was prioritized before continuing
this work. The readiness limitation, timer-only measurement and any final
scheduler decision remain open.
