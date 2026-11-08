"""Background work run after a handler, as in ``fastapi.background``."""

from __future__ import annotations

import inspect
from collections.abc import Callable
from types import FunctionType
from typing import Any


class BackgroundTask:
    """One call to make after the handler: ``BackgroundTask(func, *args, **kwargs)``.

    Pass it as a response's ``background``, or use ``BackgroundTasks``. Tasks
    run in order while the request's dependencies are still available.
    """

    __slots__ = ("args", "call", "kind", "kwargs")

    def __init__(self, func: Callable[..., Any], *args: Any, **kwargs: Any) -> None:
        if not callable(func):
            raise TypeError("background task must be callable")
        attributes = func.__dict__ if type(func) is FunctionType else None
        if type(attributes) is dict and not attributes:
            # Exact unadorned functions have no partialmethod/coroutine marker
            # for inspect to unwrap. Read the current code, without caching:
            # replacing __code__ must affect the next task's classification.
            flags = func.__code__.co_flags
            if flags & (inspect.CO_GENERATOR | inspect.CO_ASYNC_GENERATOR):
                raise TypeError("background tasks cannot be generator functions")
            kind = "async" if flags & inspect.CO_COROUTINE else "sync"
        else:
            target = func if inspect.isroutine(func) else type(func).__call__
            if (
                inspect.isgeneratorfunction(func)
                or inspect.isasyncgenfunction(func)
                or inspect.isgeneratorfunction(target)
                or inspect.isasyncgenfunction(target)
            ):
                raise TypeError("background tasks cannot be generator functions")
            kind = (
                "async"
                if inspect.iscoroutinefunction(func)
                or inspect.iscoroutinefunction(target)
                else "sync"
            )
        self.call = func
        self.args = args
        self.kwargs = kwargs
        self.kind = kind

    @property
    def func(self) -> Callable[..., Any]:
        return self.call

    @property
    def is_async(self) -> bool:
        return self.kind == "async"


def __getattr__(name: str) -> Any:
    # BackgroundTasks lives in bessemer.request, which imports this module.
    if name == "BackgroundTasks":
        from .request import BackgroundTasks

        return BackgroundTasks
    raise AttributeError(name)


__all__ = ["BackgroundTask", "BackgroundTasks"]  # noqa: F822 -- Lazily resolved.
