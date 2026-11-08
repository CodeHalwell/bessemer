"""Measure 1 ms asyncio sleeps against the installed Rust loop, without load tools."""

import argparse
import asyncio
import json
import statistics
import time

from bessemer._loop import new_event_loop


async def measure(samples, concurrency):
    elapsed = []

    async def sleeper():
        for _ in range(samples):
            started = time.perf_counter_ns()
            await asyncio.sleep(0.001)
            elapsed.append((time.perf_counter_ns() - started) / 1000)

    await asyncio.gather(*(sleeper() for _ in range(concurrency)))
    elapsed.sort()
    return {
        "samples": len(elapsed),
        "concurrency": concurrency,
        "min_us": elapsed[0],
        "p50_us": statistics.median(elapsed),
        "p95_us": elapsed[int(len(elapsed) * 0.95)],
        "p99_us": elapsed[int(len(elapsed) * 0.99)],
        "max_us": elapsed[-1],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--samples", type=int, default=1000)
    parser.add_argument("--concurrency", type=int, default=1)
    args = parser.parse_args()
    if args.samples < 1 or args.concurrency < 1:
        parser.error("samples and concurrency must be positive")
    loop = new_event_loop()
    loop._core_enable_runtime()
    try:
        print(
            json.dumps(loop.run_until_complete(measure(args.samples, args.concurrency)))
        )
    finally:
        loop.close()


if __name__ == "__main__":
    main()
