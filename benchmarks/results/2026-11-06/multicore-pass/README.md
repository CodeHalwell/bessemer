# CPU entitlement, offload and multicore measurements

[The report](report.md) records the original full matrix and the final Python-only
fix follow-up, with exact verified wheel/native/Python hashes, CPU cost ranges,
same-configuration ratios and throughput variation.

- Original matrix: 144 measured samples plus 144 warmups across four topologies,
  six workloads and configured sizes 1/2/4; 3,296,806 measured responses.
- Final follow-up: 16 measured samples plus 16 warmups for sync CPU and CPU
  offload, candidate processes/free-threaded loops, configured sizes 1/4;
  47,679 measured responses.
- Both runs had zero transport errors, unexpected statuses or client-bound
  flags. All 32 server-session process trees finished with no tracked process
  remaining.

Final medians in requests/second:

| Topology | Sync CPU 1→4 | Scaling | CPU offload 1→4 | Scaling |
| --- | --- | --- | --- | --- |
| CPython 3.13 processes | 605→2,076 | 3.43× | 587→2,015 | 3.43× |
| GIL-disabled CPython 3.14t loops | 768→2,411 | 3.14× | 748→2,455 | 3.28× |

These are unpinned process/loop and explicitly sized pool configurations on an
18-CPU Mac, not enforced vCPU quotas. The free-threaded 1→4 comparisons use the
same interpreter; direct 3.13/3.14t ratios also include interpreter differences.
The original matrix had ≥10% two-round throughput spread in 24 of 72 groups.

The original benchmark environments and wheels remain immutable. Final wheels
live in separate environments and include the custom-executor timeout and sync
worker ContextVar retention fixes. The report verifies each wheel against the
actual installed Python files and native module.

Evidence:

- [Original samples](scaling.json), [command](scaling-command.txt),
  [run log](scaling-run.log), [verified builds](build-facts.json).
- [Final samples](final-scaling.json), [command](final-scaling-command.txt),
  [run log](final-scaling-run.log), [verified builds](build-facts-final.json),
  [source/build manifest](manifest-final.json).
- [Initial 24-route probe](probe.json) and [cleanup verification](probe-cleanup.json).
- [Final installed CPython 3.13 checks](check-final-313.log) and
  [214-test focused GIL-disabled CPython 3.14t checks](check-314t.log).
- [Standalone CPU/Metal GPU benchmark](../../../gpu/README.md),
  [Metal result](gpu-metal.json) and [execution log](gpu-metal.log).

Regenerate the report from these immutable inputs:

```sh
python benchmarks/multicore/report.py \
  benchmarks/results/2026-11-06/multicore-pass/scaling.json \
  --final benchmarks/results/2026-11-06/multicore-pass/final-scaling.json
```
