# CPU offload and GPU experiments

`App.run_cpu` runs synchronous work in a bounded executor owned by the worker.
The default uses spawned processes when the GIL is enabled, and threads when
the interpreter actually runs without the GIL. The pool starts on its first
submission. Mounted applications share the automatic pool unless they supply
their own executor.

```python
from bessemer import App
from bessemer.executors import ProcessPoolExecutor

app = App(executor=ProcessPoolExecutor(max_workers=4, max_pending=32))

def score(value, *, multiplier=2):
    return value * multiplier

@app.get("/score/{value}")
async def calculate(value: int):
    return {"score": await app.run_cpu(score, value, multiplier=3, timeout=5)}
```

Process functions and arguments must be picklable; define functions in an
importable module. Thread jobs receive a copy of the submitting ContextVars.
Process jobs start with a fresh context. Async functions and awaitable results
are rejected. `run_cpu` requires a running application worker, including during
its lifespan.

Capacity includes running and queued work. A full pool returns HTTP 503 with
`Retry-After: 1`. Cancellation skips queued user functions when possible and
waits until the underlying work leaves the pool before returning capacity.
Running functions finish before cancellation cleanup completes. A per-call
timeout returns HTTP 504 after that cleanup; the transport's request deadline
can send its response sooner while retaining admission capacity. Shutdown waits
for actual work completion.

Automatic pool sizing considers process affinity, process CPU count, cgroup
v1/v2 quotas, enclosing quotas and cpusets, capped at 256. Its CPU count is
divided across supervised worker processes. Explicit `max_workers` overrides
automatic sizing. Fractional CPU quotas round up to one usable worker.

`--sync-workers`, `BESSEMER_SYNC_WORKERS` and `[tool.bessemer].sync_workers`
control synchronous handler threads per worker process. Zero selects a native
GIL pool that starts threads lazily up to eight. On Linux and macOS it can grow
to sixteen when at least four jobs remain queued for 10 ms and a checked
process CPU sample shows less than 0.75 CPU cores used. CPU sampling and growth
are spaced by at least 20 ms. A missing or invalid CPU clock disables growth.
Extra threads park when idle and remain available until shutdown. Windows and
source-only GIL pools retain an eight-thread limit; GIL-disabled interpreters
use the available CPU share. Explicit values from
1 to 256 set a fixed ceiling and disable adaptive growth. `--loops` controls
event-loop threads separately.
Stateless WebSocket sessions can use those lanes; application lifespan, state
or app-scoped resources retain first-loop affinity.

Custom executors implement async `submit(function, /, *args, **kwargs)` and
async `shutdown(*, wait=True)`. They must retain capacity through actual
cancellation cleanup. The implementation uses Python futures and stdlib pools;
the specification's Tokio-native executor future and Ray/Dask backends remain
unimplemented.

The optional [Rust GPU probe](../benchmarks/gpu/README.md) measures numerical
compute separately from HTTP processing. GPU dependencies are outside the
default runtime. NVIDIA's [CUDA Rust reference](https://developer.nvidia.com/blog/introducing-cuda-rust-two-tracks-for-writing-gpu-kernels/)
recommends cuTile first, with cuda-oxide for direct SIMT control. These are
Linux/NVIDIA paths; the local Apple Silicon probe uses wgpu with Metal. There
is no bundled GPU executor or automatic conversion of Python handlers to GPU
kernels.
