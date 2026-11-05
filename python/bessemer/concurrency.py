"""Run blocking work off the event loop, as ``fastapi.concurrency`` does."""

from __future__ import annotations

import asyncio
import functools
from collections.abc import AsyncIterator, Callable, Iterable, Iterator
from typing import Any

_END = object()


async def _in_thread[T](func: Callable[[], T]) -> T:
    """Await ``func`` in a thread; on cancellation, wait for it to finish.

    Python cannot stop a running thread, so a cancelled request keeps its
    capacity slot (and its dependencies) until the blocking call returns,
    as Bessemer's own sync handlers do. Cancellation is then re-raised.
    """
    work = asyncio.ensure_future(asyncio.to_thread(func))
    try:
        return await asyncio.shield(work)
    except asyncio.CancelledError:
        while not work.done():
            try:
                await asyncio.shield(work)
            except asyncio.CancelledError:
                continue
            except Exception:  # noqa: BLE001 -- Cancellation wins below.
                break
        raise


async def run_in_threadpool[T](func: Callable[..., T], *args: Any, **kwargs: Any) -> T:
    """Call a blocking function in a thread and await its result.

    The caller's context variables (``current_request`` included) are
    visible in the thread.
    """
    return await _in_thread(functools.partial(func, *args, **kwargs))


async def iterate_in_threadpool[T](iterator: Iterable[T]) -> AsyncIterator[T]:
    """Iterate a blocking iterator, one ``next`` per thread call."""
    source: Iterator[T] = iter(iterator)
    while True:
        item = await _in_thread(functools.partial(next, source, _END))
        if item is _END:
            return
        yield item  # type: ignore[misc]


__all__ = ["iterate_in_threadpool", "run_in_threadpool"]
