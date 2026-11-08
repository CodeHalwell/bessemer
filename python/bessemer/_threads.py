"""Shared bounded handler pool, with a native queue when available."""

import concurrent.futures
import contextvars
import threading
import weakref

try:
    from ._native import SyncPool
except ImportError:
    SyncPool = None

_pools = set()
_pools_lock = threading.Lock()


def _shutdown_pools():
    with _pools_lock:
        pools = list(_pools)
    for pool in pools:
        pool.shutdown(wait=True)


threading._register_atexit(_shutdown_pools)


class ContextThreadPoolExecutor(concurrent.futures.ThreadPoolExecutor):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._native_pool = (
            SyncPool(self._max_workers) if SyncPool is not None else None
        )
        if self._native_pool is not None:
            # Keep join handles until shutdown, even if an executor is garbage
            # collected. Its finalizer stops idle threads; interpreter shutdown
            # joins any remaining ones before Python thread states disappear.
            with _pools_lock:
                _pools.add(self._native_pool)
            weakref.finalize(self, self._native_pool.shutdown, False)

    def submit(self, function, /, *args, **kwargs):
        if self._native_pool is None:
            return contextvars.Context().run(super().submit, function, *args, **kwargs)
        future = concurrent.futures.Future()
        context = contextvars.Context()

        def run():
            if not future.set_running_or_notify_cancel():
                return
            try:
                result = context.run(function, *args, **kwargs)
            except BaseException as error:  # noqa: BLE001 -- executor boundary
                future.set_exception(error)
            else:
                future.set_result(result)

        self._native_pool.execute(run, future)
        return future

    def execute(self, work):
        if self._native_pool is not None:
            if callable(work):
                self._native_pool.execute(work)
            else:
                self._native_pool.execute(work.run, work.future)
        else:
            self.submit(work)

    def shutdown(self, wait=True, *, cancel_futures=False):
        if self._native_pool is not None:
            self._native_pool.shutdown(wait, cancel_futures)
            if wait:
                with _pools_lock:
                    _pools.discard(self._native_pool)
        super().shutdown(wait=wait, cancel_futures=cancel_futures)
