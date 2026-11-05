"""Run blocking work off the event loop, as ``fastapi.concurrency`` does."""

from __future__ import annotations

import asyncio
import functools
from collections.abc import AsyncIterator, Callable, Iterable, Iterator
from typing import Any

_END = object()


async def run_in_threadpool[T](func: Callable[..., T], *args: Any, **kwargs: Any) -> T:
    """Call a blocking function in a thread and await its result.

    The caller's context variables (``current_request`` included) are
    visible in the thread.
    """
    return await asyncio.to_thread(functools.partial(func, *args, **kwargs))


async def iterate_in_threadpool[T](iterator: Iterable[T]) -> AsyncIterator[T]:
    """Iterate a blocking iterator, one ``next`` per thread call."""
    source: Iterator[T] = iter(iterator)
    while True:
        item = await asyncio.to_thread(next, source, _END)
        if item is _END:
            return
        yield item  # type: ignore[misc]


__all__ = ["iterate_in_threadpool", "run_in_threadpool"]
