"""Equivalent inline, sync, I/O, mixed and CPU-offload application work."""

import asyncio
import inspect
import multiprocessing
import os
import sys
import sysconfig
import threading
from concurrent.futures import ProcessPoolExecutor
from contextlib import asynccontextmanager

from work import checksum

FRAMEWORK = os.environ.get("MULTICORE_FRAMEWORK", "bessemer")
ITERATIONS = int(os.environ.get("MULTICORE_ITERATIONS", "50000"))
POOL_WORKERS = int(os.environ.get("MULTICORE_POOL_WORKERS", "1"))
POOL = None
CPU_EXECUTOR = None


@asynccontextmanager
async def lifespan(app):
    global POOL
    if not hasattr(app, "run_cpu"):
        # Re-exec rather than forking an interpreter with runtime threads.
        POOL = ProcessPoolExecutor(
            max_workers=POOL_WORKERS, mp_context=multiprocessing.get_context("spawn")
        )
    try:
        yield
    finally:
        if POOL is not None:
            await asyncio.to_thread(POOL.shutdown, wait=True, cancel_futures=True)
            POOL = None


if FRAMEWORK == "fastapi":
    from fastapi import FastAPI

    app = FastAPI(lifespan=lifespan)
else:
    from bessemer import App

    if hasattr(App, "run_cpu"):
        from bessemer.executors import ProcessPoolExecutor as CPUProcessPool
        from bessemer.executors import ThreadPoolExecutor as CPUThreadPool

        pool_type = (
            CPUProcessPool
            if getattr(sys, "_is_gil_enabled", lambda: True)()
            else CPUThreadPool
        )
        CPU_EXECUTOR = pool_type(max_workers=POOL_WORKERS, max_pending=1024)
        app = App(executor=CPU_EXECUTOR, lifespan=lifespan)
    else:
        app = App(lifespan=lifespan)

GIL = getattr(sys, "_is_gil_enabled", lambda: True)()
if GIL != (os.environ.get("MULTICORE_EXPECT_GIL", "1") == "1"):
    raise RuntimeError("benchmark application imported with an unexpected GIL state")


@app.get("/healthz")
async def health():
    return {"ok": True}


@app.get("/probe")
async def probe():
    from bessemer.supervisor import cpu_quota_count

    return {
        "pid": os.getpid(),
        "thread": threading.get_ident(),
        "gil_enabled": getattr(sys, "_is_gil_enabled", lambda: True)(),
        "free_threaded_build": bool(sysconfig.get_config_var("Py_GIL_DISABLED")),
        "cpu_budget": cpu_quota_count(),
        "affinity": sorted(os.sched_getaffinity(0))
        if hasattr(os, "sched_getaffinity")
        else None,
        "offload": "App.run_cpu" if hasattr(app, "run_cpu") else "ProcessPoolExecutor",
        "fallback_pool_workers": POOL_WORKERS if POOL is not None else None,
        "pool_workers": CPU_EXECUTOR.max_workers
        if CPU_EXECUTOR is not None
        else POOL_WORKERS,
        "pool_backend": type(CPU_EXECUTOR).__name__
        if CPU_EXECUTOR is not None
        else "ProcessPoolExecutor",
        "sync_workers": POOL_WORKERS
        if FRAMEWORK == "bessemer"
        and "sync_workers" in inspect.signature(type(app).run).parameters
        else 8
        if FRAMEWORK == "bessemer"
        else "AnyIO default",
    }


@app.get("/cpu-inline")
async def cpu_inline():
    return {"checksum": checksum(ITERATIONS)}


@app.get("/cpu-sync")
def cpu_sync():
    return {"checksum": checksum(ITERATIONS)}


@app.get("/io")
async def io():
    await asyncio.sleep(0.001)
    return {"ok": True}


@app.get("/fanout")
async def fanout():
    await asyncio.gather(*(asyncio.sleep(0.001) for _ in range(16)))
    return {"completed": 16}


@app.get("/mixed")
async def mixed():
    first = checksum(ITERATIONS // 2)
    await asyncio.sleep(0.001)
    return {"checksum": first + checksum(ITERATIONS // 2)}


@app.get("/cpu-offload")
async def cpu_offload():
    if hasattr(app, "run_cpu"):
        value = await app.run_cpu(checksum, ITERATIONS)
    else:
        value = await asyncio.get_running_loop().run_in_executor(
            POOL, checksum, ITERATIONS
        )
    return {"checksum": value}
