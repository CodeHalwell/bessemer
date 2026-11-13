# CPU offload and GPU experiments

`App.run_cpu` runs synchronous work in a bounded executor owned by the worker.
The default is a pool of spawned processes on every interpreter, free-threaded
ones included ([why](#threads-or-processes)). The pool starts on its first
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

## Threads or processes

`ProcessPoolExecutor` is the default; `App(executor=ThreadPoolExecutor())`
runs the work in threads of the worker instead. With the GIL, threads run
Python one at a time, so they suit only functions that spend their time in C
code that releases it. Without the GIL (free-threaded CPython 3.14t) threads
run Python in parallel, take arguments and return results without pickling
them, and see the worker's own objects. Whether they are faster depends on
what the function touches.

Requests per second through `run_cpu` with four pool workers, eight
connections, one server worker on four cores (CPython 3.14.6, 13 November
2026; ranges are repeated runs, each on a new server):

| Function | 3.14t, threads | 3.14t, processes | 3.14 with the GIL, processes |
| --- | ---: | ---: | ---: |
| Arithmetic on local values, 20,000 steps | 1,603–1,843 | 1,315–1,466 | 1,350 |
| The same, its module compiled from source | 270–287 | | |
| The same loop reading a module-level list | 266–289 | 1,074–1,186 | 1,219 |
| `zlib.crc32` over a 256 KiB argument, 40 times | 1,132–1,326 | 801–857 | 792 |

Threads without the GIL are a quarter faster where nothing is shared, and
half as fast again where a large argument would otherwise be pickled. They
are four times slower where the loop reads objects every thread uses: each
read updates the object's reference count, and four cores then contend for
the same memory. A lookup table, a configuration object or a shared array in
a hot loop is enough.

What counts as shared is not always visible in the code. The second row is
the first row's function, which uses nothing but its own local values and the
constant `0xFFFFFFFF`. CPython 3.14.6t makes that constant immortal, so that
no thread counts references to it, when the module is loaded from its
bytecode cache, and does not when it compiles the module from source: on the
first run after an edit, with `PYTHONDONTWRITEBYTECODE=1`, or where the cache
cannot be written, as in many container images. Four threads running the
function together then finished about half the work one thread does alone.
Nothing reports any of this; the route is simply slow.

In processes each worker has its own copy of every object, and all three
functions run at the rate of the interpreter with the GIL.

Up to 1.0.0rc5 the default followed the interpreter and chose threads when
the GIL was off. It is now processes everywhere, so that moving an
application to a free-threaded interpreter cannot make `run_cpu` several
times slower, and so that a function passed to it meets the same rules on
every interpreter (above). Choose `ThreadPoolExecutor` on a free-threaded
interpreter for a function you have measured there, as it is deployed, or
one whose arguments are too large to pickle on every call.

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
