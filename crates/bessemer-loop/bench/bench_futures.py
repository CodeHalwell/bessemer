"""Futures: create_future + set_result + await in a tight coroutine loop."""

from __future__ import annotations

import asyncio
import sys

from _harness import Case, main


async def resolved(n: int) -> int:
    """create_future, set_result, then await an already-done future (no suspend)."""
    loop = asyncio.get_running_loop()
    for i in range(n):
        fut = loop.create_future()
        fut.set_result(i)
        await fut
    return n


async def via_call_soon(n: int) -> int:
    """await a pending future resolved by a callback (one suspend per op)."""
    loop = asyncio.get_running_loop()
    for i in range(n):
        fut = loop.create_future()
        loop.call_soon(fut.set_result, i)
        await fut
    return n


async def done_callbacks(n: int) -> int:
    """create_future + add_done_callback + set_result, awaited in batches."""
    loop = asyncio.get_running_loop()
    hits = 0

    def on_done(_fut: asyncio.Future) -> None:
        nonlocal hits
        hits += 1

    left = n
    while left:
        k = min(1000, left)
        for i in range(k):
            fut = loop.create_future()
            fut.add_done_callback(on_done)
            fut.set_result(i)
        left -= k
        await asyncio.sleep(0)
    assert hits == n, hits
    return n


CASES = [
    Case("resolved_await", 500_000, resolved, "future"),
    Case("call_soon_resolved", 200_000, via_call_soon, "future"),
    Case("done_callback", 300_000, done_callbacks, "future"),
]

if __name__ == "__main__":
    sys.exit(main("futures", CASES))
