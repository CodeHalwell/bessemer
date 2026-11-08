"""Start reusable pool threads without retaining a caller's ContextVars."""

import concurrent.futures
import contextvars
from concurrent.futures import thread as _thread


class ContextThreadPoolExecutor(concurrent.futures.ThreadPoolExecutor):
    def submit(self, function, /, *args, **kwargs):
        return contextvars.Context().run(super().submit, function, *args, **kwargs)

    def execute(self, work):
        """Enqueue a native work item that owns its completion and cancellation.

        CPython 3.12-3.15's worker calls run() (run(context) since 3.14), and
        shutdown(cancel_futures=True) calls future.cancel(). Native items
        implement both protocols without allocating an unused concurrent Future.
        The locks and shutdown checks match ThreadPoolExecutor.submit.
        """
        with self._shutdown_lock, _thread._global_shutdown_lock:
            if self._broken:
                raise _thread.BrokenThreadPool(self._broken)
            if self._shutdown:
                raise RuntimeError("cannot schedule new futures after shutdown")
            if _thread._shutdown:
                raise RuntimeError(
                    "cannot schedule new futures after interpreter shutdown"
                )
            self._work_queue.put(work)
            # Only thread creation can inherit the caller's context. Queueing
            # and the shutdown checks do not need a separate context entry.
            contextvars.Context().run(self._adjust_thread_count)
