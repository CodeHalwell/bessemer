"""call_later / call_at timer scheduling, cancellation and firing."""

from __future__ import annotations

import asyncio
import sys

from _harness import Case, main

# Small varied delays: half are zero (fire on the next iteration), the rest are
# a few microseconds to 0.2 ms, so the timer heap is genuinely ordered and the
# loop's wait never exceeds a millisecond or two.
DELAYS = [0.0, 0.0, 0.0, 0.0, 1e-6, 5e-6, 2e-5, 5e-5, 1e-4, 2e-4]


async def schedule_cancel_half(n: int, batch: int = 1000) -> int:
    """Schedule n timers with mixed tiny delays and cancel every other one.

    Ops counts scheduled timers; each one is also either cancelled or fired.
    """
    loop = asyncio.get_running_loop()
    fired = 0
    expected = 0

    def cb() -> None:
        nonlocal fired
        fired += 1

    left = n
    nd = len(DELAYS)
    while left:
        k = min(batch, left)
        handles = [loop.call_later(DELAYS[i % nd], cb) for i in range(k)]
        for h in handles[::2]:
            h.cancel()
        expected += k - len(handles[::2])
        left -= k
        await asyncio.sleep(0)
    # Wait for the stragglers with the longest delay.
    while fired < expected:
        await asyncio.sleep(max(DELAYS))
    assert fired == expected, (fired, expected)
    return n


async def zero_delay_fire(n: int, batch: int = 1000) -> int:
    """call_later(0, ...) for n timers; each fires on the next iteration."""
    loop = asyncio.get_running_loop()
    fired = 0

    def cb() -> None:
        nonlocal fired
        fired += 1

    left = n
    while left:
        k = min(batch, left)
        for _ in range(k):
            loop.call_later(0, cb)
        left -= k
        while fired < n - left:
            await asyncio.sleep(0)
    return n


async def far_future_cancel(n: int, batch: int = 5000) -> int:
    """call_at timers far in the future that are all cancelled (heap churn)."""
    loop = asyncio.get_running_loop()
    base = loop.time() + 3600.0

    def cb() -> None:
        raise AssertionError("cancelled timer fired")

    left = n
    while left:
        k = min(batch, left)
        handles = [loop.call_at(base + i * 1e-3, cb) for i in range(k)]
        for h in handles:
            h.cancel()
        left -= k
        # Let the loop prune the cancelled handles from its heap.
        await asyncio.sleep(0)
    return n


async def timer_chain(n: int) -> int:
    """One zero-delay timer that re-arms itself from its own callback."""
    loop = asyncio.get_running_loop()
    done = loop.create_future()
    remaining = n

    def step() -> None:
        nonlocal remaining
        remaining -= 1
        if remaining:
            loop.call_later(0, step)
        else:
            done.set_result(None)

    loop.call_later(0, step)
    await done
    return n


CASES = [
    Case("schedule_cancel_half", 200_000, schedule_cancel_half, "timer"),
    Case("zero_delay_fire", 200_000, zero_delay_fire, "timer"),
    Case("far_future_cancel", 200_000, far_future_cancel, "timer"),
    Case("timer_chain", 200_000, timer_chain, "timer"),
]

if __name__ == "__main__":
    sys.exit(main("timers", CASES))
