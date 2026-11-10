"""Shared bounded handler pool, with a native queue when available."""

import collections
import concurrent.futures
import contextvars
import functools
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

# Pools whose executor was garbage collected without a joining shutdown.
_finalized = set()
# The finalizer can run during any allocation, including one made while
# ``_pools_lock`` is held, so it must not take that lock. It hands pools over
# through this deque instead; ``append`` and ``popleft`` are atomic.
_retiring = collections.deque()


def _retire(pool):
    _retiring.append(pool)
    pool.shutdown(False)


def _run_submitted(future, context, function, args, kwargs):
    """Run one submitted call on a pool thread and settle its future."""
    if not future.set_running_or_notify_cancel():
        return
    try:
        result = context.run(function, *args, **kwargs)
    except BaseException as error:  # noqa: BLE001 -- executor boundary
        future.set_exception(error)
        # The future holds the error, whose traceback holds this frame. A
        # frame that still held the future would close a cycle, and everything
        # the failed call had in scope would wait for the garbage collector.
        future = None
    else:
        future.set_result(result)


class ContextThreadPoolExecutor(concurrent.futures.ThreadPoolExecutor):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._native_pool = (
            SyncPool(self._max_workers, self._thread_name_prefix or "bessemer-handler")
            if SyncPool is not None
            else None
        )
        if self._native_pool is not None:
            # Keep join handles until shutdown, even if an executor is garbage
            # collected. Its finalizer stops idle threads; interpreter shutdown
            # joins any remaining ones before Python thread states disappear.
            # A finalized pool is dropped here once its threads have exited.
            with _pools_lock:
                while _retiring:
                    _finalized.add(_retiring.popleft())
                retired = [pool for pool in _finalized if pool.threads_alive == 0]
                _pools.difference_update(retired)
                _finalized.difference_update(retired)
                _pools.add(self._native_pool)
            weakref.finalize(self, _retire, self._native_pool)

    def submit(self, function, /, *args, **kwargs):
        if self._native_pool is None:
            return contextvars.Context().run(super().submit, function, *args, **kwargs)
        future = concurrent.futures.Future()
        self._native_pool.execute(
            functools.partial(
                _run_submitted, future, contextvars.Context(), function, args, kwargs
            ),
            future,
        )
        return future

    def execute(self, work):
        if self._native_pool is not None:
            if callable(work):
                self._native_pool.execute(work)
            else:
                self._native_pool.execute(work.run, work.future)
        else:
            self.submit(work if callable(work) else work.run)

    def shutdown(self, wait=True, *, cancel_futures=False):
        if self._native_pool is not None:
            self._native_pool.shutdown(wait, cancel_futures)
            if wait:
                with _pools_lock:
                    while _retiring:
                        _finalized.add(_retiring.popleft())
                    _pools.discard(self._native_pool)
                    _finalized.discard(self._native_pool)
        super().shutdown(wait=wait, cancel_futures=cancel_futures)
