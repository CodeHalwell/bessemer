"""call_soon throughput: plain callbacks with no coroutine or future machinery."""

from __future__ import annotations

import asyncio
import sys

from _harness import Case, main


async def chain(n: int) -> int:
    """One callback that reschedules itself n times (ready queue depth 1)."""
    loop = asyncio.get_running_loop()
    done = loop.create_future()
    remaining = n

    def step() -> None:
        nonlocal remaining
        remaining -= 1
        if remaining:
            loop.call_soon(step)
        else:
            done.set_result(None)

    loop.call_soon(step)
    await done
    return n


async def fanout(n: int, width: int = 100) -> int:
    """`width` independent self-rescheduling chains, n callbacks in total."""
    loop = asyncio.get_running_loop()
    done = loop.create_future()
    remaining = n

    def step(arg: int) -> None:
        nonlocal remaining
        remaining -= 1
        if remaining >= width:
            loop.call_soon(step, arg)
        elif remaining == 0:
            done.set_result(None)

    for i in range(min(width, n)):
        loop.call_soon(step, i)
    await done
    return n


async def batch(n: int, size: int = 1000) -> int:
    """Schedule `size` callbacks at once from Python, then drain them, repeatedly."""
    loop = asyncio.get_running_loop()
    count = 0

    def cb() -> None:
        nonlocal count
        count += 1

    left = n
    while left:
        k = min(size, left)
        for _ in range(k):
            loop.call_soon(cb)
        left -= k
        # sleep(0) yields once; the callbacks queued above run in that iteration.
        await asyncio.sleep(0)
    assert count == n, count
    return n


async def threadsafe(n: int) -> int:
    """call_soon_threadsafe from the loop thread (cost of the locked path)."""
    loop = asyncio.get_running_loop()
    done = loop.create_future()
    remaining = n

    def step() -> None:
        nonlocal remaining
        remaining -= 1
        if remaining:
            loop.call_soon_threadsafe(step)
        else:
            done.set_result(None)

    loop.call_soon_threadsafe(step)
    await done
    return n


CASES = [
    Case("chain", 400_000, chain, "cb", "one callback rescheduling itself"),
    Case("fanout100", 400_000, fanout, "cb", "100 concurrent self-rescheduling chains"),
    Case("batch1000", 400_000, batch, "cb", "1000 callbacks queued per loop iteration"),
    Case("threadsafe_chain", 300_000, threadsafe, "cb", "call_soon_threadsafe chain"),
]

if __name__ == "__main__":
    sys.exit(main("call_soon", CASES))
