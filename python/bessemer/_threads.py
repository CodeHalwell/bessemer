"""Start reusable pool threads without retaining a caller's ContextVars."""

import concurrent.futures
import contextvars


class ContextThreadPoolExecutor(concurrent.futures.ThreadPoolExecutor):
    def submit(self, function, /, *args, **kwargs):
        return contextvars.Context().run(super().submit, function, *args, **kwargs)
