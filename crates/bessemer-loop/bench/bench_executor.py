"""run_in_executor round trips through the loop's default ThreadPoolExecutor."""

from __future__ import annotations

import asyncio
import sys

from _harness import Case, main


def _noop() -> int:
    return 1


async def sequential(n: int) -> int:
    """One call in flight: submit, wake the loop from the worker, resume."""
    loop = asyncio.get_running_loop()
    total = 0
    for _ in range(n):
        total += await loop.run_in_executor(None, _noop)
    assert total == n
    return n


async def concurrent(n: int, width: int = 32) -> int:
    """`width` calls in flight at once, gathered, until n calls complete."""
    loop = asyncio.get_running_loop()
    total = 0
    left = n
    while left:
        k = min(width, left)
        results = await asyncio.gather(
            *(loop.run_in_executor(None, _noop) for _ in range(k))
        )
        total += sum(results)
        left -= k
    assert total == n
    return n


CASES = [
    Case("sequential", 8_000, sequential, "call"),
    Case("concurrent32", 12_000, concurrent, "call"),
]

if __name__ == "__main__":
    sys.exit(main("executor", CASES))
