"""Run every event-loop benchmark for each loop and print a comparison table.

Each (benchmark, loop) pair runs in its own subprocess so that one loop's
allocator state, threads or crash cannot affect another's numbers.

    python3 run_all.py                       # all available loops, ~2 minutes
    python3 run_all.py --quick               # smoke test, a few seconds
    python3 run_all.py --loop asyncio --loop bessemer --cpu 2 \
        --json results.json --markdown results.md
"""

from __future__ import annotations

import argparse
import json
import math
import os
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from _harness import LOOP_NAMES, environment, format_rate

BENCHMARKS = ("call_soon", "timers", "futures", "tasks", "sockets", "executor")
BASELINE = "asyncio"


def loop_available(python: str, name: str) -> tuple[bool, str]:
    """Check in a child process that the loop imports and can be created."""
    code = (
        "import sys; sys.path.insert(0, sys.argv[1]); import _harness; "
        "l = _harness.loop_factory(sys.argv[2])(); l.close(); "
        "print(_harness.loop_version(sys.argv[2]))"
    )
    proc = subprocess.run(
        [python, "-c", code, str(HERE), name],
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
    )
    if proc.returncode == 0:
        return True, proc.stdout.strip()
    lines = proc.stderr.strip().splitlines()
    return False, lines[-1] if lines else f"exit status {proc.returncode}"


def run_one(args: argparse.Namespace, bench: str, loop: str) -> dict:
    cmd = [args.python, str(HERE / f"bench_{bench}.py"), "--loop", loop, "--json"]
    if args.quick:
        cmd.append("--quick")
    else:
        cmd += ["--repeat", str(args.repeat), "--scale", str(args.scale)]
    if args.cpu is not None:
        cmd += ["--cpu", str(args.cpu)]
    t0 = time.perf_counter()
    try:
        proc = subprocess.run(
            cmd, check=False, capture_output=True, text=True, timeout=args.timeout
        )
    except subprocess.TimeoutExpired:
        return {
            "benchmark": bench,
            "loop": loop,
            "error": f"timed out after {args.timeout}s",
        }
    elapsed = time.perf_counter() - t0
    if proc.returncode != 0:
        tail = proc.stderr.strip().splitlines()[-5:]
        return {
            "benchmark": bench,
            "loop": loop,
            "error": f"exit status {proc.returncode}: " + " | ".join(tail),
        }
    doc = json.loads(proc.stdout.strip().splitlines()[-1])
    doc["elapsed_s"] = elapsed
    return doc


def build_rows(runs: list[dict], loops: list[str]) -> list[dict]:
    """One row per (benchmark, case) with each loop's summary."""
    rows: dict[tuple[str, str], dict] = {}
    for run in runs:
        for res in run.get("results", []):
            key = (res["benchmark"], res["case"])
            row = rows.setdefault(
                key, {"benchmark": key[0], "case": key[1], "unit": res["unit"]}
            )
            row[res["loop"]] = res
    order = {b: i for i, b in enumerate(BENCHMARKS)}
    return sorted(rows.values(), key=lambda r: order.get(r["benchmark"], 99))


def relative(row: dict, loop: str) -> float | None:
    base = row.get(BASELINE)
    res = row.get(loop)
    if not base or not res or base["ops_per_sec"] <= 0:
        return None
    return res["ops_per_sec"] / base["ops_per_sec"]


def render_markdown(
    rows: list[dict], loops: list[str], errors: list[dict], meta: dict
) -> str:
    out: list[str] = []
    env = meta["environment"]
    out.append(
        f"Python {env['python']} on {env['platform']} ({env['cpu_count']} CPUs"
        + (
            f", pinned to CPU {env['pinned_cpu']}"
            if env["pinned_cpu"] is not None
            else ""
        )
        + "); loops: "
        + ", ".join(f"{name} {meta['loop_versions'].get(name, '?')}" for name in loops)
        + f"; {meta['repeat']} runs per case, median shown"
        + (" (QUICK MODE: numbers are not meaningful)" if meta["quick"] else "")
        + "."
    )
    out.append("")

    # Summary: median ops/sec per loop, and speed relative to asyncio.
    head = ["benchmark", "unit"]
    for loop in loops:
        head.append(f"{loop} ops/s")
        if loop != BASELINE and BASELINE in loops:
            head.append(f"{loop} vs {BASELINE}")
    out.append("| " + " | ".join(head) + " |")
    out.append("|" + "|".join(["---", "---"] + ["---:"] * (len(head) - 2)) + "|")
    for row in rows:
        cells = [f"{row['benchmark']}/{row['case']}", row["unit"]]
        for loop in loops:
            res = row.get(loop)
            cells.append(
                f"{format_rate(res['ops_per_sec'])} ±{res['spread_pct']:.0f}%"
                if res
                else "n/a"
            )
            if loop != BASELINE and BASELINE in loops:
                rel = relative(row, loop)
                cells.append(f"{rel:.2f}x" if rel else "n/a")
        out.append("| " + " | ".join(cells) + " |")
    if BASELINE in loops and len(loops) > 1:
        cells = ["**geometric mean**", ""]
        for loop in loops:
            cells.append("")
            if loop != BASELINE:
                rels = [r for r in (relative(row, loop) for row in rows) if r]
                gm = (
                    math.exp(sum(math.log(r) for r in rels) / len(rels))
                    if rels
                    else None
                )
                cells.append(f"**{gm:.2f}x**" if gm else "n/a")
        out.append("| " + " | ".join(cells) + " |")
    out.append("")

    # Detail: wall and CPU cost per operation.
    out.append("<details><summary>Per-operation wall and CPU time</summary>")
    out.append("")
    out.append(
        "| benchmark | loop | ops/s | ns/op wall | ns/op CPU | spread | ops x runs |"
    )
    out.append("|---|---|---:|---:|---:|---:|---:|")
    for row in rows:
        for loop in loops:
            res = row.get(loop)
            if not res:
                continue
            out.append(
                f"| {row['benchmark']}/{row['case']} | {loop} | {format_rate(res['ops_per_sec'])} "
                f"| {res['ns_per_op']:.0f} | {res['cpu_ns_per_op']:.0f} "
                f"| ±{res['spread_pct']:.1f}% | {res['ops']} x {res['repeat']} |"
            )
    out.append("")
    out.append("</details>")
    if errors:
        out.append("")
        out.append("Failures:")
        out.append("")
        for err in errors:
            out.append(f"- {err['benchmark']} on {err['loop']}: {err['error']}")
    return "\n".join(out) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--loop",
        action="append",
        choices=LOOP_NAMES,
        help="loop to benchmark (repeatable; default: every loop that imports)",
    )
    parser.add_argument(
        "--bench",
        action="append",
        choices=BENCHMARKS,
        help="benchmark to run (repeatable; default: all)",
    )
    parser.add_argument(
        "--repeat", type=int, default=5, help="timed runs per case (default 5)"
    )
    parser.add_argument(
        "--scale", type=float, default=1.0, help="multiply every op count"
    )
    parser.add_argument(
        "--quick", action="store_true", help="CI smoke mode: tiny op counts, one run"
    )
    parser.add_argument(
        "--cpu", type=int, default=None, help="pin every benchmark process to this CPU"
    )
    parser.add_argument("--json", metavar="PATH", help="also write raw results as JSON")
    parser.add_argument(
        "--markdown", metavar="PATH", help="also write the tables as Markdown"
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=300.0,
        help="seconds allowed per (benchmark, loop) subprocess (default 300)",
    )
    parser.add_argument(
        "--python",
        default=sys.executable,
        help="interpreter for the benchmark processes",
    )
    args = parser.parse_args(argv)

    if args.cpu is not None:
        if args.cpu not in os.sched_getaffinity(0):
            parser.error(f"CPU {args.cpu} is not available to this process")
        os.sched_setaffinity(0, {args.cpu})

    explicit = args.loop is not None
    requested = list(dict.fromkeys(args.loop or LOOP_NAMES))
    loops: list[str] = []
    versions: dict[str, str] = {}
    for name in requested:
        ok, info = loop_available(args.python, name)
        if ok:
            loops.append(name)
            versions[name] = info
        elif explicit:
            print(f"error: loop {name!r} is not usable: {info}", file=sys.stderr)
            return 2
        else:
            print(f"skipping {name}: {info}", file=sys.stderr)
    if not loops:
        print("error: no event loop available", file=sys.stderr)
        return 2

    benches = list(dict.fromkeys(args.bench or BENCHMARKS))
    runs: list[dict] = []
    errors: list[dict] = []
    started = time.perf_counter()
    for bench in benches:
        # Interleave loops per benchmark so slow drift (thermal, noisy
        # neighbours) affects every loop alike.
        for loop in loops:
            print(f"running {bench} on {loop} ...", file=sys.stderr, flush=True)
            doc = run_one(args, bench, loop)
            if "error" in doc:
                print(f"  FAILED: {doc['error']}", file=sys.stderr)
                errors.append(doc)
            else:
                print(f"  done in {doc['elapsed_s']:.1f}s", file=sys.stderr)
                runs.append(doc)
    total = time.perf_counter() - started

    meta = {
        "timestamp": datetime.now(UTC).isoformat(timespec="seconds"),
        "environment": environment(",".join(loops), args.cpu),
        "loop_versions": versions,
        "loops": loops,
        "benchmarks": benches,
        "repeat": 1 if args.quick else args.repeat,
        "scale": args.scale,
        "quick": args.quick,
        "total_s": total,
    }
    rows = build_rows(runs, loops)
    markdown = render_markdown(rows, loops, errors, meta)
    print()
    print(markdown)
    print(f"total {total:.1f}s", file=sys.stderr)

    if args.markdown:
        Path(args.markdown).write_text(markdown)
    if args.json:
        Path(args.json).write_text(
            json.dumps({"meta": meta, "runs": runs, "errors": errors}, indent=2) + "\n"
        )
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
