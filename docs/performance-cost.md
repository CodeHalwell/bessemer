# Production cost and capacity

The performance objective is more successful concurrent workload per production
instance, reducing replica count and hosting cost at a fixed latency/error
objective. CPU time, memory and useful capacity are measured together.

## Current engineering pass

The [efficiency harness](../benchmarks/efficiency/README.md) ports the existing
concurrency workloads into identical local Linux containers. The first slice
uses two CPU cores, a two-core cpuset, 512 MiB memory, 256 process slots, two API
workers and 64 offered client connections. The load generator runs outside the
server's resource limits. It compares the previous local wheel, the candidate,
the candidate's Rust loop and FastAPI with the same application work.

The diagnostic objective is p99 at most 50 ms, at most 0.1% transport/status/body
length failures, and at least 99% of offered requests delivered successfully.
These thresholds are declared experiment settings; a deployment should supply
its own objective. Corrected per-request latency includes client scheduling
delay, and missed offered load is reported. A short ramp yields capacity
brackets; it does not establish a sustained production operating limit.

The sampler records server-tree CPU, summed RSS, proportional memory (PSS),
cgroup memory/CPU, anonymous/file memory, threads, connections and child cleanup.
CPU per million successful requests and per million within-objective requests
are reported separately. Agent instrumentation CPU is recorded separately.
The configured limits are read back from cgroup v2, including init at PID 1.

Two measured overheads were removed:

- Synchronous stream production now takes bounded batches: at most 64 iterator
  steps, 1 ms between yieldable steps, and the available 64 KiB buffer allowance.
  The first batch remains one step. Notifications are coalesced and chunks can
  reach the loop before a later iterator step blocks. A 4,096-chunk fixture
  dropped from 4,097 executor submissions to 65 with identical bytes.
- Immediate native streaming steps allocate no completion channel. Pending
  steps still allocate one. Iterator cleanup and payload/admission reservations
  retain their previous ownership through cancellation.

Evidence is under
[`benchmarks/results/2026-11-06/efficiency-pass/`](../benchmarks/results/2026-11-06/efficiency-pass/).
Original and candidate source snapshots/wheels remain separate in the local
benchmark cache. No production deployment or cloud price assumption is involved.

## Next milestones

1. Refine the latency/capacity knee for representative routes and repeat it at
   fixed resource sizes. Include cold/warm memory, burst recovery, cancellations
   and longer runs before selecting production concurrency and replica targets.
2. Use the profiles to reduce native handler dispatch, Request/dictionary and
   asyncio Task costs. The specification's native coroutine/task/future path
   requires a separate implementation preserving task inspection, ContextVars,
   cancellation and compatibility fallbacks; it is not implemented by this pass.
3. Qualify a representative complete application on its deployment architecture,
   including authentication, validation, streaming and CPU work. Establish CPU,
   RAM and headroom per replica, then calculate the required instance count.

At an instance price of P pounds/hour and sustainable capacity G successful
requests/second, serving cost per million requests is `P * 1_000_000 / (3600 * G)`.
Deployment calculations must also include minimum replicas, idle headroom and
other billed components. GPU compute has a separate workload comparison.
