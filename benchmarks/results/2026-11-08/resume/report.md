# Suspended-request lifecycle follow-up, 8 November 2026

Worktree `/home/cmux/wt/codex-echo`, assigned CPython 3.13.15 venv only.

- `26f2c66` reverts `ffa003d`. Its instruction saving did not establish a
  throughput gain and added epoll calls. Original asyncio-turn/Tokio scheduling
  and precise native timers are restored.
- `ca07722` merges current local main (`4d51a7c`), including its newer response
  path work. All before/after numbers below use this merged, reverted baseline.
- `fe3a2ab` uses the existing lean invocation as its asyncio Task's completion
  callback. Avoids bound-method allocation and a task-table lookup; preserves
  real Task identity, factories, context and cancellation. Both tracking tables
  and native resumed-result encoding remain.
- `c7620d0` adds a bounded 256-entry callback Handle free list. PyO3 0.29 returns
  reused GC objects untracked: explicitly restore tracking before publication.
  This fixes the leak that rejected the earlier free-list experiment. Ready
  and timer callback cycles and abandoned unclosed loops remain collectable.

## Measurements

Installed release wheels; matrix common:app unless marked compare. Auto loop,
private vgdb prefix and PID-scoped controls. 2,000 warmups; c1 uses 3,000 measured
requests, c64 uses 6,000. Baseline wheel saved privately and unpacked under this
worktree; only our venv was installed. No machine-wide CPU isolation was used.

| Route | merged/reverted baseline | callback only | callback + pool |
| --- | ---: | ---: | ---: |
| `/io`, c1 | 61,192 | 60,421 | 60,083 (−1.81%) |
| `/json`, c1 | 33,257 | 33,292 | 33,261 |
| compare `/io`, c1 | 61,218 | — | 60,272 (−1.55%) |
| `/io`, c64 | 48,770 | — | 47,835 (−1.92%) |
| `/sync`, c1 | 99,355 | — | 98,691 |
| `/bg`, c1 | 231,364 | — | 230,057 |

These are instruction counts, not throughput claims. In particular, the final
c1 count is higher than the earlier timer-batched count because that shortcut
was reverted. The purpose of the revert is to remove its extra epoll work.
For 1,000 standalone 1 ms sleeps, final strace counts are 1,001 epoll_wait,
1,000 timerfd_settime, 1,296 read: back to the original syscall counts.
A loaded sequential timer sample measured median 1.037 ms / p99 1.126 ms,
with an 8.028 ms maximum; no additional millisecond of timer rounding.

## Validation and remaining work

Final required installed-wheel full check passed: 238 Rust tests (2 ignored),
988 Python tests (34 skipped), 23 harness tests; formatting, Clippy with warnings
denied, Ruff lint/format and shell syntax all passed. Focused native invoker
suite passed 53 tests; focused event-loop suite passed 66. Added user completion
callback ContextVars/current_request checks on both loop backends and repeated
abandoned ready/timer callback cycle collection. An earlier full invocation
stopped at Ruff while the new test's import was being added; the corrected
complete run above passed. No runtime change followed that full check.

The much larger suspended-request gap remains open. Profiles identify real
Task wakeup/step handling, Future callbacks and Handle destruction as larger
costs than timer heap registration. No replacement Task/Future scheduler,
special-case asyncio.sleep implementation, or shortcut past cancellation and
context handling was introduced. Preserved post-delivery background admission,
upload and response-byte budgets and native JSON delivery guards are exercised
by the full suite. Isolated throughput qualification belongs to the orchestrator;
no larger throughput win has been established. Only GIL-enabled CPython 3.13
was executed locally; other supported builds need CI. Python bridge routes
remain Tier 2, native health Tier 0, and formal M0 acceptance remains open.
