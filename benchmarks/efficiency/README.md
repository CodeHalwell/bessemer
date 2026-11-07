# Matched-resource CPU/RAM and SLO diagnostics

The [6 November measured report](../results/2026-11-06/efficiency-pass/report.md)
records 80 Linux samples and 1,261,517 completed responses with zero transport,
status or body-length errors. At equal passing offered rates, candidate Rust-loop
CPU cost per SLO success was 1.16–3.41 times lower than FastAPI across six cases;
PSS differences were modest. Repeated synchronous streaming passed 4,000/s twice
with the Rust loop (p99 5.5–10.0 ms), while FastAPI failed 2,000/s twice
(110.6–148.3 ms). Neither candidate loop passed 8,000/s reliably; default asyncio
also had explicit p99 outliers at 2,000/s and 4,000/s. The report preserves all
raw files, commands, build hashes and cleanup/validation evidence. These short
diagnostics do not establish a production replica-saving ratio.

This ports the existing concurrency and multicore benchmark lifecycle and reuses
their application routes, Pydantic payloads, response contracts and Python
checksum. It measures fixed offered rates, not just maximum requests/second.
There are no cloud calls, prices or automatic replica-saving claims.

The first Linux slice runs four variants serially: the immutable previous-pass
Bessemer baseline, candidate asyncio, candidate Rust event loop, and FastAPI.
Each uses two API processes in a fresh container with **2 CPU quota, CPUs 0–1,
512 MiB memory, 256 PID limit and init at PID 1**. Kernel readback must confirm
those limits. The oha client runs on the Mac host, outside that resource scope.
The local Linux VM still shares the physical Mac with the client; client CPU
saturation is recorded.

## Run the compact slice

The `container/` tooling defines the local tools image. Recorded runs used
locally prepared baseline/candidate wheels and Linux environments; their hashes
and cache paths are retained in the reports. Those caches and raw SQLite files
are not included in a fresh clone. Prepare equivalent environments first.
The driver mounts the complete `benchmarks/` tree read-only at
`/bench` and the cached environments at `/venvs`; it never pulls an image.

```sh
python3 benchmarks/efficiency/run.py \
  --image bessemer-efficiency-tools:2026-11-06 \
  --venvs /absolute/path/to/efficiency-2026-11-06/venvs \
  --oha /absolute/path/to/oha \
  --out /absolute/path/to/results/initial.json \
  --db-dir /absolute/private/cache/efficiency-sqlite \
  --cases json,echo,dependencies,io,sync-stream,mixed \
  --workers 2 --cpus 2 --cpuset 0,1 --memory-mb 512 --pids-limit 256 \
  --connections 64 --rounds 1 --duration 2 --warmup 0.5 \
  --p99-ms 50 --error-budget 0.001 --min-delivered 0.99
```

Start with `--probe-only --variants candidate --cases json`. The normal default
case set is the shorter `json,io,sync-stream,mixed`; echo and dependencies add
real validation/resource-lifecycle costs. `--rates 1000,5000` overrides every
selected case's ramp. Output files must be new so earlier attempts survive.
Variants rotate/reverse between repeated rounds. One round and two-second
samples are an exploratory first slice, not a production qualification.

For a comparison that isolates Rust-loop changes, select
`--variants baseline-rust,candidate-rust,fastapi`. The `baseline-rust` variant
uses the baseline wheel with the same `bessemer` event loop as `candidate-rust`.
The existing `/fanout` route can be selected with `--cases fanout`; it awaits
16 one-millisecond sibling tasks and has a 1,000/5,000 requests/s diagnostic
ramp. Run separate 1/2/4-vCPU slices with `--workers` and `--cpus` set to the
same count and matching `--cpuset` values (`0`, `0,1`, `0,1,2,3`).

Bessemer uses `max_in_flight=128`, `max_connections=256`, one loop per process
and one sync thread per process. Uvicorn uses `limit_concurrency=256`; its
connection/task counting differs from Bessemer's request admission. The same
64 client connections leave both native limits unsaturated during normal
operation. No extra Python middleware is inserted to imitate those controls.

## What qualifies a tested rate

The default **declared diagnostic target**, not the user's production SLO, is:

- successful-response corrected p99 at most 50 ms;
- transport, unexpected-status and wrong-body-length failures at most 0.1%;
- at least 99% of the offered request count successfully delivered;
- at most 1% of successful responses exceeding the latency target;
- the host client below 85% of its configured CPU-thread capacity.

Every response contract is checked before and after load. During load, raw oha
SQLite records provide status, body length and corrected duration for each
completed transport exchange. The JSON error distribution supplies transport
failures omitted from SQLite. The driver checks SQLite/JSON response counts
agree and records unissued/unmet offered requests. A server that serves only
3,000 of an offered 8,000 requests/second cannot pass by returning fast 200s.

Success goodput counts expected-status/expected-length responses. SLO goodput
counts those responses also within the latency target. These are separately
reported from offered rate, corrected p50/p99, rejects, errors and slow fraction.
SQL databases stay in the private cache; result JSON records their paths and
SHA-256 hashes instead of checking large databases into Git.

Oha 1.16 uses a global `-q` rate and `--latency-correction`. Its
[SQLite writer](https://github.com/hatoo/oha/blob/v1.16.0/src/db.rs) stores
`RequestResult.duration()`, whose [implementation](https://github.com/hatoo/oha/blob/v1.16.0/src/client.rs)
subtracts the scheduled start when latency correction is present. Queueing
delay is therefore included instead of hidden by coordinated omission.

## CPU and memory accounting

The Linux agent samples **every descendant of the server leader**, including
supervised API workers and any offload children. Primary CPU is their observed
user+system time from `/proc`, retaining the last observed counter of exited
children. The agent's own CPU is excluded and reported separately. Exact
cgroup CPU counters are also retained; they include the sampler/init and can
expose CPU work missed if a child both starts and exits between samples.

Per-sample output includes:

- CPU seconds per million successful responses and per million SLO successes;
- steady median and sampled peak process-tree RSS;
- proportional-set-size (PSS), avoiding shared-library duplication in RSS sums;
- cgroup charged memory, lifetime kernel peak, sampled peak, anon/file breakdown
  and memory/OOM events;
- process/thread counts and established application-port TCP connections;
- total and incremental RSS per established connection.

RSS is a sum of resident sets and can count shared pages more than once. PSS
and charged cgroup memory are separate measures. The kernel memory peak covers
the whole fresh server session, including startup/warmup; it is not mislabeled
as an endpoint-specific peak. Sampled peaks can miss brief allocations.
Established connections include idle keepalive sockets and are not the same
as in-flight requests; the output does not invent an active-request count.

The portable sampler can collect CPU/RSS on macOS, but the matched-resource
driver deliberately uses Linux containers. Existing `benchmarks/concurrency`
and `benchmarks/multicore` remain the unpinned Mac diagnostics. No Mac process
count is labeled an enforced vCPU or memory limit.

## Capacity and comparisons

For each workload, report the **highest passing tested offered rate** and the
**lowest failing tested rate**. Two rates establish a bracket or a truncated
bound, not exact capacity. Passing 40k where another build passes 10k/fails40k
does not establish a fourfold capacity advantage. Refine a knee with targeted
midpoints and longer repeated samples before drawing a replica-density result.

Compare CPU and RAM at the **same offered rate when both variants meet the
target**. Overloaded goodput is useful diagnostic evidence, not proof of lower
production cost. The harness does not convert these measurements to currency.
