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


class BoundedExecutor:
    worker_limit = 256

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
            self.closed = False
            self.worker_processes = worker_processes

    def start(self):
        workers = self.max_workers or min(
            self.worker_limit, max(1, cpu_budget() // self.worker_processes)
        )
        self.pool = self.create_pool(workers)
        self.free = deque(range(workers + self.max_pending))

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
                work = self.schedule(slot, function, args, kwargs)
            except BaseException:
                self.free.append(slot)
                raise
            self.pending.add(work)
        work.add_done_callback(lambda completed: self.release(slot, completed))
        return work, slot

    def release(self, slot, work):
        with self.lock:
            self.pending.discard(work)
            self.free.append(slot)

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
    worker_limit = 61 if sys.platform == "win32" else 256

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
    free_threaded = hasattr(sys, "_is_gil_enabled") and not sys._is_gil_enabled()
    executor = ThreadPoolExecutor() if free_threaded else ProcessPoolExecutor()
    executor.configure(worker_processes)
    return executor


__all__ = ["Executor", "ProcessPoolExecutor", "ThreadPoolExecutor"]
