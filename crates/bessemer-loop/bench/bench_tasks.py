"""Tasks: create_task + gather, sleep(0) yields and two-task ping-pong."""

from __future__ import annotations

import asyncio
import sys

from _harness import Case, main


async def _trivial() -> int:
    return 1


async def create_gather(n: int, batch: int = 1000) -> int:
    """create_task of trivial coroutines, gathered `batch` at a time."""
    loop = asyncio.get_running_loop()
    total = 0
    left = n
    while left:
        k = min(batch, left)
        tasks = [loop.create_task(_trivial()) for _ in range(k)]
        total += sum(await asyncio.gather(*tasks))
        left -= k
    assert total == n, total
    return n


async def create_await(n: int) -> int:
    """create_task + await one task at a time (task start-up latency)."""
    loop = asyncio.get_running_loop()
    for _ in range(n):
        await loop.create_task(_trivial())
    return n


async def ping_pong(n: int) -> int:
    """Two tasks hand a token back and forth via futures.

    Ops counts task switches: each hand-off wakes the other task once.
    """
    loop = asyncio.get_running_loop()
    rounds = n // 2
    box: list[asyncio.Future] = [loop.create_future(), loop.create_future()]

    async def player(me: int, other: int, serve: bool) -> None:
        if serve:
            box[other].set_result(None)
        for _ in range(rounds if not serve else rounds - 1):
            await box[me]
            box[me] = loop.create_future()
            box[other].set_result(None)
        if serve:
            await box[me]

    a = loop.create_task(player(0, 1, True))
    b = loop.create_task(player(1, 0, False))
    await asyncio.gather(a, b)
    return rounds * 2


async def sleep0_yield(n: int, width: int = 10) -> int:
    """`width` tasks each doing `await asyncio.sleep(0)` n/width times."""
    per = n // width

    async def spin() -> None:
        for _ in range(per):
            await asyncio.sleep(0)

    await asyncio.gather(*(spin() for _ in range(width)))
    return per * width


CASES = [
    Case("create_gather1000", 150_000, create_gather, "task"),
    Case("create_await", 100_000, create_await, "task"),
    Case("ping_pong", 200_000, ping_pong, "switch"),
    Case("sleep0_x10", 200_000, sleep0_yield, "yield"),
]

if __name__ == "__main__":
    sys.exit(main("tasks", CASES))
