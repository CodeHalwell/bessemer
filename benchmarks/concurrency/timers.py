"""Measure real sleep delays against an installed wheel."""

import argparse
import asyncio
import hashlib
import json
import statistics
import time
from pathlib import Path

from bessemer import App, _native
from bessemer.testclient import TestClient


async def measure(delay, count):
    values = []
    for _ in range(count):
        started = time.perf_counter()
        await asyncio.sleep(delay)
        values.append((time.perf_counter() - started) * 1000)
    return {
        "median_ms": statistics.median(values),
        "min_ms": min(values),
        "max_ms": max(values),
        "samples_ms": values,
    }


def run(count):
    app = App()

    @app.get("/sleep")
    async def sleep(delay: float):
        return await measure(delay, count)

    samples = {}
    for loop in ("asyncio", "bessemer"):
        with TestClient(app, server_options={"event_loop": loop}) as client:
            samples[loop] = {
                str(delay): client.get(f"/sleep?delay={delay}").json()
                for delay in (0.0001, 0.001, 0.005, 0.02)
            }
    return {
        "native_file": _native.__file__,
        "native_sha256": hashlib.sha256(
            Path(_native.__file__).read_bytes()
        ).hexdigest(),
        "count": count,
        "samples": samples,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--count", type=int, default=100)
    args = parser.parse_args()
    if args.count < 1:
        parser.error("--count must be positive")
    print(json.dumps(run(args.count), indent=2))
