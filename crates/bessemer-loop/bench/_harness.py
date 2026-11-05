"""Shared harness for the event-loop micro-benchmarks.

Every ``bench_*.py`` script defines a list of :class:`Case` objects and calls
:func:`main`. A case is an ``async def workload(n)`` coroutine function that
performs ``n`` operations on the running loop and returns the number of
operations it actually performed. The harness creates a fresh loop from the
selected factory, runs one shortened warm-up, then times ``--repeat`` runs and
reports the median and spread of both wall time and process CPU time.
"""

from __future__ import annotations

import argparse
import asyncio
import gc
import json
import os
import platform
import statistics
import sys
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

LOOP_NAMES = ("asyncio", "uvloop", "bessemer")

# Multiplier applied to every case's operation count in --quick mode. Quick
# mode only proves that each case runs; its numbers are not meaningful.
QUICK_SCALE = 0.01


@dataclass(frozen=True)
class Case:
    """One timed workload.

    ``ops`` is the default operation count, chosen so a stock asyncio run
    takes roughly a quarter of a second on a modern x86 core.
    """

    name: str
    ops: int
    workload: Callable[[int], Awaitable[int]]
    unit: str = "op"
    description: str = ""


@dataclass
class Result:
    benchmark: str
    case: str
    loop: str
    unit: str
    ops: int
    repeat: int
    wall_s: list[float] = field(default_factory=list)
    cpu_s: list[float] = field(default_factory=list)

    def summary(self) -> dict:
        wall = statistics.median(self.wall_s)
        cpu = statistics.median(self.cpu_s)
        rates = [self.ops / w for w in self.wall_s if w > 0]
        return {
            "benchmark": self.benchmark,
            "case": self.case,
            "loop": self.loop,
            "unit": self.unit,
            "ops": self.ops,
            "repeat": self.repeat,
            "ops_per_sec": self.ops / wall if wall > 0 else float("inf"),
            "ns_per_op": wall * 1e9 / self.ops,
            "cpu_ns_per_op": cpu * 1e9 / self.ops,
            "cpu_ops_per_sec": self.ops / cpu if cpu > 0 else float("inf"),
            # Spread is the min..max range of the per-run rate as a
            # percentage of the median rate.
            "spread_pct": (
                (max(rates) - min(rates)) * 100 / statistics.median(rates)
                if len(rates) > 1
                else 0.0
            ),
            "ops_per_sec_min": min(rates) if rates else 0.0,
            "ops_per_sec_max": max(rates) if rates else 0.0,
            "wall_s": self.wall_s,
            "cpu_s": self.cpu_s,
        }


def loop_factory(name: str) -> Callable[[], asyncio.AbstractEventLoop]:
    """Return a zero-argument factory for the named event loop."""
    if name == "asyncio":
        return asyncio.new_event_loop
    if name == "uvloop":
        import uvloop

        return uvloop.new_event_loop
    if name == "bessemer":
        from bessemer import _loop as bessemer_loop

        return bessemer_loop.new_event_loop
    raise ValueError(f"unknown loop {name!r}; expected one of {LOOP_NAMES}")


def loop_version(name: str) -> str:
    try:
        if name == "asyncio":
            return platform.python_version()
        if name == "uvloop":
            import uvloop

            return uvloop.__version__
        if name == "bessemer":
            from importlib import metadata

            import bessemer._loop  # noqa: F401 - reports unavailable if missing

            try:
                return metadata.version("bessemer")
            except metadata.PackageNotFoundError:
                return "unknown"
    except ImportError:
        return "unavailable"
    return "unknown"


def pin_cpu(cpu: int | None) -> None:
    if cpu is not None:
        os.sched_setaffinity(0, {cpu})


def _run_once(
    loop: asyncio.AbstractEventLoop, case: Case, n: int
) -> tuple[int, float, float]:
    gc.collect()
    w0 = time.perf_counter()
    c0 = time.process_time()
    done = loop.run_until_complete(case.workload(n))
    c1 = time.process_time()
    w1 = time.perf_counter()
    return done, w1 - w0, c1 - c0


def run_case(
    benchmark: str,
    case: Case,
    loop_name: str,
    *,
    repeat: int,
    scale: float,
) -> Result:
    n = max(1, int(case.ops * scale))
    loop = loop_factory(loop_name)()
    try:
        asyncio.set_event_loop(loop)
        # Warm-up: a shorter run that pays for imports, the default executor's
        # thread start-up and any allocator growth before timing starts.
        _run_once(loop, case, max(1, n // 5))
        result = Result(benchmark, case.name, loop_name, case.unit, 0, repeat)
        for _ in range(repeat):
            # Workloads may round n (e.g. to whole ping-pong rounds); they
            # return the count they actually performed.
            done, wall, cpu = _run_once(loop, case, n)
            if done <= 0 or (result.ops and done != result.ops):
                raise RuntimeError(f"{case.name}: workload reported {done} ops")
            result.ops = done
            result.wall_s.append(wall)
            result.cpu_s.append(cpu)
        return result
    finally:
        try:
            loop.run_until_complete(loop.shutdown_asyncgens())
            if hasattr(loop, "shutdown_default_executor"):
                loop.run_until_complete(loop.shutdown_default_executor())
        finally:
            asyncio.set_event_loop(None)
            loop.close()


def environment(loop_name: str, cpu: int | None) -> dict:
    return {
        "python": sys.version.split()[0],
        "implementation": platform.python_implementation(),
        "platform": platform.platform(),
        "machine": platform.machine(),
        "cpu_count": os.cpu_count(),
        "pinned_cpu": cpu,
        "loop": loop_name,
        "loop_version": loop_version(loop_name),
    }


def format_rate(rate: float) -> str:
    if rate >= 1e6:
        return f"{rate / 1e6:.2f}M"
    if rate >= 1e3:
        return f"{rate / 1e3:.1f}k"
    return f"{rate:.0f}"


def main(benchmark: str, cases: list[Case], argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=f"{benchmark} event-loop benchmark")
    parser.add_argument("--loop", choices=LOOP_NAMES, default="asyncio")
    parser.add_argument(
        "--repeat", type=int, default=5, help="timed runs per case (default 5)"
    )
    parser.add_argument(
        "--scale",
        type=float,
        default=1.0,
        help="multiply every case's operation count (default 1.0)",
    )
    parser.add_argument(
        "--quick",
        action="store_true",
        help="smoke mode: tiny op counts and one run, only checks the cases work",
    )
    parser.add_argument(
        "--cpu", type=int, default=None, help="pin this process to one CPU"
    )
    parser.add_argument(
        "--case",
        action="append",
        default=None,
        help="run only the named case (may be given more than once)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="print one JSON document on stdout instead of a text report",
    )
    args = parser.parse_args(argv)

    pin_cpu(args.cpu)
    scale = args.scale * (QUICK_SCALE if args.quick else 1.0)
    repeat = 1 if args.quick else max(1, args.repeat)

    selected = cases
    if args.case:
        known = {c.name for c in cases}
        unknown = set(args.case) - known
        if unknown:
            parser.error(f"unknown case(s) {sorted(unknown)}; known: {sorted(known)}")
        selected = [c for c in cases if c.name in args.case]

    summaries = []
    for case in selected:
        res = run_case(benchmark, case, args.loop, repeat=repeat, scale=scale)
        s = res.summary()
        summaries.append(s)
        if not args.json:
            print(
                f"{args.loop:9} {benchmark}/{case.name:24} "
                f"{format_rate(s['ops_per_sec']):>8} {case.unit}/s  "
                f"{s['ns_per_op']:9.0f} ns/{case.unit} wall  "
                f"{s['cpu_ns_per_op']:9.0f} ns/{case.unit} cpu  "
                f"±{s['spread_pct']:.1f}%  (n={s['ops']}, runs={s['repeat']})",
                flush=True,
            )
    if args.json:
        json.dump(
            {
                "benchmark": benchmark,
                "environment": environment(args.loop, args.cpu),
                "quick": args.quick,
                "results": summaries,
            },
            sys.stdout,
        )
        sys.stdout.write("\n")
    return 0
