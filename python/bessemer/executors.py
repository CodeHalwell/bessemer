"""Bounded CPU offload with request-safe cancellation."""

from __future__ import annotations

import asyncio
import concurrent.futures
import contextvars
import inspect
import multiprocessing
import sys
import threading
from collections import deque
from collections.abc import Callable
from concurrent.futures.process import BrokenProcessPool
from typing import Any, Protocol

from ._cpu import cpu_budget
from ._threads import ContextThreadPoolExecutor
from .responses import HTTPException

PROCESS_CANCELLATIONS = None


class Executor(Protocol):
    async def submit(self, function: Callable, /, *args, **kwargs) -> Any: ...

    async def shutdown(self, *, wait: bool = True) -> None: ...


def process_initializer(cancellations):
    global PROCESS_CANCELLATIONS
    PROCESS_CANCELLATIONS = cancellations


def invoke(function, args, kwargs):
    result = function(*args, **kwargs)
    if inspect.isawaitable(result):
        if inspect.iscoroutine(result):
            result.close()
        raise TypeError("CPU functions must return a synchronous result")
    return result


def process_call(slot, function, args, kwargs):
    if PROCESS_CANCELLATIONS[slot]:
        raise concurrent.futures.CancelledError()
    return contextvars.Context().run(invoke, function, args, kwargs)


def thread_call(cancelled, context, function, args, kwargs):
    if cancelled.is_set():
        raise concurrent.futures.CancelledError()
    return context.run(invoke, function, args, kwargs)


async def await_completion(work, cancel):
    loop = asyncio.get_running_loop()
    future = loop.create_future()
    work.add_done_callback(
        lambda completed: loop.call_soon_threadsafe(
            future.set_result, None, context=contextvars.Context()
        )
    )
    try:
        await asyncio.shield(future)
    except asyncio.CancelledError:
        cancel()
        while not future.done():
            try:
                await asyncio.shield(future)
            except asyncio.CancelledError:
                continue
        raise
    return work.result()


def _shut_down(pools):
    for pool in pools:
        pool.shutdown(wait=False, cancel_futures=True)


class BoundedExecutor:
    worker_limit = 256
    broken_errors = ()

    def __init__(self, max_workers=None, *, max_pending=128):
        if max_workers is not None and (
            type(max_workers) is not int or not 1 <= max_workers <= 256
        ):
            raise ValueError("max_workers must be an integer between 1 and 256")
        if type(max_pending) is not int or not 0 <= max_pending <= 65536:
            raise ValueError("max_pending must be an integer between 0 and 65536")
        self.max_workers = max_workers
        self.max_pending = max_pending
        self.worker_processes = 1
        self.pool = None
        self.pending = set()
        self.rebuilding = False
        self.discarded = []
        self.workers = 0
        self.free = deque()
        self.lock = threading.Lock()
        self.closed = False

    def configure(self, worker_processes):
        if type(worker_processes) is not int or worker_processes < 1:
            raise ValueError("worker_processes must be a positive integer")
        with self.lock:
            if self.pending:
                raise RuntimeError("CPU executor is still running work")
            if self.pool is not None and not self.closed:
                return
            self.pool = None
            self.rebuilding = False
            self.closed = False
            self.worker_processes = worker_processes

    def start(self):
        if self.rebuilding:
            # Capacity accounting survives a rebuild: slots still held by
            # jobs of the discarded pool return through release(). The slot
            # numbers index the pool's cancellation flags, so a rebuilt pool
            # keeps the size of the one it replaces even if the CPU budget
            # has changed since.
            workers = self.workers
            self.rebuilding = False
        else:
            workers = self.max_workers or min(
                self.worker_limit, max(1, cpu_budget() // self.worker_processes)
            )
            self.workers = workers
            self.free = deque(range(workers + self.max_pending))
        self.pool = self.create_pool(workers)

    def close_discarded(self):
        """Shut broken pools down, off this thread and outside the lock.

        concurrent.futures' manager thread holds a broken pool's shutdown
        lock while it fails each of the pool's jobs, and every failure calls
        release(), which takes this executor's lock. Shutting the pool down
        while holding this lock would wait for a lock whose holder is waiting
        for this one. Its shutdown can also wait for the dead pool's
        processes, so it is not done on the event loop's thread either.
        """
        with self.lock:
            discarded, self.discarded = self.discarded, []
        if discarded:
            threading.Thread(
                target=_shut_down,
                args=(discarded,),
                name="bessemer-cpu-close",
                daemon=True,
            ).start()

    def is_broken(self, work):
        """Whether `work` failed because its whole pool is unusable."""
        return False

    def discard(self, pool):
        """Drop a broken pool; the next job builds a new one (lock held)."""
        if pool is not None and pool is self.pool:
            self.discarded.append(pool)
            self.pool = None
            self.rebuilding = True

    def reserve(self, function, args, kwargs):
        with self.lock:
            if self.closed:
                raise RuntimeError("CPU executor is shut down")
            if self.pool is None:
                self.start()
            if not self.free:
                raise HTTPException(
                    503, "CPU capacity is full", headers={"Retry-After": "1"}
                )
            slot = self.free.popleft()
            try:
                try:
                    pool = self.pool
                    work = self.schedule(slot, function, args, kwargs)
                except self.broken_errors:
                    # The pool broke before any job noticed: replace it once.
                    self.discard(pool)
                    self.start()
                    pool = self.pool
                    work = self.schedule(slot, function, args, kwargs)
            except BaseException:
                self.free.append(slot)
                raise
            work.pool = pool
            self.pending.add(work)
        if self.discarded:
            self.close_discarded()
        work.add_done_callback(lambda completed: self.release(slot, completed))
        return work, slot

    def release(self, slot, work):
        with self.lock:
            self.pending.discard(work)
            self.free.append(slot)
            if self.is_broken(work):
                self.discard(getattr(work, "pool", None))

    def cancel(self, slot, work):
        with self.lock:
            if work in self.pending and not work.done():
                self.mark_cancelled(slot)

    async def submit(self, function, /, *args, **kwargs):
        if not callable(function) or inspect.iscoroutinefunction(function):
            raise TypeError("CPU functions must be synchronous callables")
        work, slot = self.reserve(function, args, kwargs)
        return await await_completion(work, lambda: self.cancel(slot, work))

    async def shutdown(self, *, wait=True):
        if type(wait) is not bool:
            raise TypeError("wait must be a boolean")
        with self.lock:
            self.closed = True
            pool = self.pool
        self.close_discarded()
        if pool is not None:
            work = asyncio.create_task(asyncio.to_thread(pool.shutdown, wait=wait))
            try:
                await asyncio.shield(work)
            except asyncio.CancelledError:
                while not work.done():
                    try:
                        await asyncio.shield(work)
                    except asyncio.CancelledError:
                        continue
                raise


class ThreadPoolExecutor(BoundedExecutor):
    def create_pool(self, workers):
        self.cancellations = [
            threading.Event() for _ in range(workers + self.max_pending)
        ]
        return ContextThreadPoolExecutor(
            max_workers=workers, thread_name_prefix="bessemer-cpu"
        )

    def schedule(self, slot, function, args, kwargs):
        cancelled = self.cancellations[slot]
        cancelled.clear()
        context = contextvars.copy_context()
        return self.pool.submit(thread_call, cancelled, context, function, args, kwargs)

    def mark_cancelled(self, slot):
        self.cancellations[slot].set()


class ProcessPoolExecutor(BoundedExecutor):
    # A child killed from outside (out of memory, a crash in native code)
    # breaks the whole pool. ThreadPoolExecutor cannot break this way: an
    # exception in a thread only fails its own job.
    worker_limit = 61 if sys.platform == "win32" else 256
    broken_errors = (BrokenProcessPool,)

    def is_broken(self, work):
        return not work.cancelled() and isinstance(work.exception(), BrokenProcessPool)

    def create_pool(self, workers):
        if sys.platform == "win32" and workers > 61:
            raise ValueError("Windows process pools support at most 61 workers")
        context = multiprocessing.get_context("spawn")
        self.cancellations = context.Array("b", workers + self.max_pending)
        return concurrent.futures.ProcessPoolExecutor(
            max_workers=workers,
            mp_context=context,
            initializer=process_initializer,
            initargs=(self.cancellations,),
        )

    def schedule(self, slot, function, args, kwargs):
        self.cancellations[slot] = 0
        return contextvars.Context().run(
            self.pool.submit, process_call, slot, function, args, kwargs
        )

    def mark_cancelled(self, slot):
        self.cancellations[slot] = 1


def automatic_executor(worker_processes=1):
    """The executor `run_cpu` uses when the application supplies none.

    Processes on every interpreter, free-threaded ones included. Without the
    GIL, threads run a function in parallel only for as long as it leaves
    objects the threads share alone: one that reads a module-level table ran
    at a quarter of the rate it reached in processes (docs/cpu-and-gpu.md).
    Processes cost pickling and memory, but the same on every interpreter,
    and a function behaves in them as it does where there is a GIL.
    `App(executor=ThreadPoolExecutor())` chooses threads.
    """
    executor = ProcessPoolExecutor()
    executor.configure(worker_processes)
    return executor


__all__ = ["Executor", "ProcessPoolExecutor", "ThreadPoolExecutor"]
