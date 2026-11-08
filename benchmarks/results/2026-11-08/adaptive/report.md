# Adaptive admission, 8 November 2026

Implemented in `f7a1f5e`, after latest-main integration `b6ee362`.
Installed release full check passed: 238 Rust tests (2 ignored), 983 Python
(34 skipped), 23 harness; static checks passed. Both loop backends admit
1,000 simultaneous healthy 1 ms sleepers, shed sustained blocking overload,
and recover. Explicit integer cap and config inputs retain their semantics.

Private-prefix Callgrind, c1, 2,000 warmups / 3,000 measured: `/json`
33,022 -> 33,130 (+0.33%), `/sync` 99,160 -> 99,575, `/bg` 225,617 ->
230,215. Includes the latest main parity merge. Same candidate with fixed
admission `/bg`: 229,678. Raw profiles are in `raw/` locally.

## Shared-machine concurrency smoke, not performance acceptance

Used a private-port/worktree copy of `/home/cmux/matrix/conc.sh`, default
5-second samples, server CPUs 0..workers-1 and oha CPUs 4–6. No process was
moved or globally isolated. Other agents were profiling/building, and our
check/build also overlapped portions. Throughput changes are confounded and
must be repeated by the orchestrator with isolation. The baseline is the
saved second-main-merge wheel (fixed128); the candidate includes subsequent
main parity changes. Both use the same compare application. These numbers
only provide a functional concurrency smoke: the candidate produced zero
non-200 responses at all measured combinations. Oha also reported transport errors, recorded in raw JSONL; their totals are
close to the outstanding connection count at the end of timed samples.

| workers | route | connections | before successful req/s | after successful req/s | before non-200 | after non-200 |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| 1 | /json | 64 | 61,792 | 54,732 | 0 | 0 |
| 1 | /json | 256 | 62,407 | 52,659 | 0 | 0 |
| 1 | /json | 1024 | 50,591 | 41,031 | 0 | 0 |
| 1 | /io | 64 | 29,473 | 27,601 | 0 | 0 |
| 1 | /io | 256 | 22,852 | 49,276 | 52,728 | 0 |
| 1 | /io | 1024 | 13,142 | 40,379 | 65,547 | 0 |
| 4 | /json | 64 | 161,654 | 110,601 | 0 | 0 |
| 4 | /json | 256 | 178,051 | 148,848 | 0 | 0 |
| 4 | /json | 1024 | 168,041 | 115,811 | 0 | 0 |
| 4 | /io | 64 | 46,208 | 38,459 | 0 | 0 |
| 4 | /io | 256 | 122,305 | 82,932 | 0 | 0 |
| 4 | /io | 1024 | 147,450 | 100,579 | 12,938 | 0 |

Native health remains Tier 0; Python bridge routes remain Tier 2. The formal
M0 gate and isolated throughput qualification remain open.
