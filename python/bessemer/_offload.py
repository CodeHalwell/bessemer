"""Per-worker ownership of automatic and application CPU executors."""

import asyncio
import math
import threading

from .executors import BoundedExecutor, automatic_executor
from .responses import HTTPException


class CPUOffload:
    def __init__(self, executor):
        if executor is not None and any(
            not callable(getattr(executor, name, None))
            for name in ("submit", "shutdown")
        ):
            raise TypeError("executor must provide submit and shutdown methods")
        self.configured = executor
        self.default = None
        self.executor = None
        self.owns_default = False
        self.lock = threading.Lock()

    def configure(self, worker_processes, default=None):
        with self.lock:
            self.default = (
                automatic_executor(worker_processes) if default is None else default
            )
            self.owns_default = default is None
            self.executor = self.default if self.configured is None else self.configured
            if isinstance(self.executor, BoundedExecutor):
                self.executor.configure(worker_processes)
            return self.default

    async def submit(self, function, args, kwargs, timeout):
        if timeout is not None and (
            type(timeout) not in (int, float)
            or not math.isfinite(timeout)
            or timeout <= 0
        ):
            raise ValueError("timeout must be a finite positive number")
        executor = self.executor
        if executor is None:
            raise RuntimeError("CPU offload requires a running application worker")
        timer = asyncio.timeout(timeout)
        try:
            async with timer:
                result = await executor.submit(function, *args, **kwargs)
        except TimeoutError as error:
            if timer.expired():
                raise HTTPException(504, "CPU work timed out") from error
            raise
        if timer.expired():
            raise HTTPException(504, "CPU work timed out")
        return result

    async def shutdown(self):
        if self.executor is None:
            return
        try:
            await self.executor.shutdown(wait=True)
        finally:
            if self.owns_default and self.executor is not self.default:
                await self.default.shutdown(wait=True)
            self.executor = None
