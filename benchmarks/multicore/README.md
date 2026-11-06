# Processes versus free-threaded loops

This ports the existing concurrency harness's server lifecycle, HTTP validation,
error accounting and process-tree CPU measurements to 1/2/4 CPU configurations.
It compares an immutable baseline with N GIL worker processes, a candidate with
N GIL processes, and a true GIL-disabled candidate with one process and N event
loops. The application checks the GIL after importing the runtime; mislabeled
thread runs fail. An optional FastAPI variant uses N Uvicorn processes.

The workloads are pure-Python inline async CPU work, sync CPU work, 1 ms async
I/O, 16-way async fan-out, mixed inline CPU/I/O, and CPU offload. The same
picklable function and inputs run in every backend. Candidates use `App.run_cpu`;
older Bessemer/FastAPI builds use a bounded-size spawned `ProcessPoolExecutor`
for the offload route. That fallback has one pool process per server process,
so N server processes do not each create N pool processes. The candidate
free-threaded offload workload requires `App.run_cpu`.

Example setup using separately installed wheels:

```sh
PYTHON/bin/python benchmarks/multicore/run.py \
  --baseline-python /absolute/path/to/BASELINE/bin/python \
  --candidate-python /absolute/path/to/CANDIDATE/bin/python \
  --free-threaded-python /absolute/path/to/CPYTHON-314T/bin/python \
  --oha /absolute/path/to/oha --cpus 1,2,4 \
  --out /tmp/bessemer-multicore/raw.json
```

Start with `--probe-only --cpus 1 --rounds 1` to check imports, GIL state,
responses and cleanup without a throughput run. A short subset is
`--cases cpu-inline,io --cpus 1,2 --rounds 1 --duration 1 --warmup 1`.
Use `--topologies baseline,candidate` to omit an unavailable free-threaded
environment; add `--fastapi-python ...` to include FastAPI. `--event-loop`
selects the same asyncio or Bessemer event loop for every Bessemer topology.

Variant order rotates and reverses between rounds, as do CPU configuration and
workload order. Both warmup and measured errors fail the run. Each completed
sample includes actual GIL/build state, native module hash, response validation,
CPU entitlement/affinity, CPU time per response, client CPU saturation and raw
oha output. Inspect the `client_bound` field before interpreting scaling.

On Linux the server limits affinity before re-executing workers, and descendants
inherit it. These are affinity/configuration sweeps, not emulated cgroup quotas;
an enclosing container quota still applies and is recorded by `/probe`. macOS
has no portable per-process affinity API, so its 1/2/4 configurations are
unpinned worker/loop counts. Local same-host measurements do not establish
formal performance acceptance or a production free-threading qualification.
