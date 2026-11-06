#!/usr/bin/env python3
"""Tables for the 2-worker run (raw/multicore) and the callgrind counts (raw/callgrind).

usage: aggregate_extra.py   (writes ../multicore-table.md and ../callgrind-table.md)
"""

import glob
import json
import re
import statistics
from pathlib import Path

from metrics import corrected_result

R = Path(__file__).resolve().parent.parent
out = [
    (
        "| config (2 workers / threads on CPUs 0-1; oha on CPUs 2-3) | run | req/s (200) | "
        "server CPUs | us CPU/req | ctx sw/req | p50 ms | p99 ms | p99.9 ms | "
        "oha util (2 cores) | non-200 |"
    ),
    "|---|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|",
]
for name in ("main-default", "branch-default", "axum"):
    ds = []
    for run in (1, 2):
        result_path = R / f"raw/multicore/{name}/run{run}/result.json"
        d = corrected_result(
            json.loads(result_path.read_text()),
            json.loads(result_path.with_name("oha.json").read_text()),
        )
        d["non200"] = d["total_responses"] - d["ok_responses"]
        ds.append(d)
        mark = " CLIENT-LIMITED" if d["client_limited"] else ""
        out.append(
            f"| {name} | {run} | {d['ok_req_s']:,.0f} | {d['server_cpus']:.2f} | {d['us_cpu_per_req']:.2f} | "
            f"{d['ctx_switches_per_req']:.3f} | {d['p50_ms']:.2f} | {d['p99_ms']:.2f} | {d['p99_9_ms']:.1f} | "
            f"{d['client_util']:.2f}{mark} | {d['non200']} ({100 * d['non200'] / d['total_responses']:.1f}%) |"
        )
    m = statistics.median
    out.append(
        f"| **{name} median** | | **{m(d['ok_req_s'] for d in ds):,.0f}** | {m(d['server_cpus'] for d in ds):.2f} | "
        f"**{m(d['us_cpu_per_req'] for d in ds):.2f}** | {m(d['ctx_switches_per_req'] for d in ds):.3f} | "
        f"{m(d['p50_ms'] for d in ds):.2f} | **{m(d['p99_ms'] for d in ds):.2f}** | **{m(d['p99_9_ms'] for d in ds):.1f}** | "
        f"{m(d['client_util'] for d in ds):.2f} | {m(d['non200'] for d in ds):.0f} |"
    )
Path(R / "multicore-table.md").write_text("\n".join(out) + "\n")
print("\n".join(out))

counts = {}
for line in Path(R / "raw/callgrind.log").read_text().splitlines():
    hit = re.match(r"(\S+)-(json|items|echo): \S+ (\d+) instructions per request", line)
    if hit:
        counts[(hit[1], hit[2])] = int(hit[3])
cg = [
    "| route | main asyncio | main rust | branch asyncio | branch rust | branch rust vs main rust | branch asyncio vs main asyncio |",
    "|---|--:|--:|--:|--:|--:|--:|",
]
for key, label in (
    ("json", "GET /json"),
    ("items", "GET /items/7?q=abc"),
    ("echo", "POST /echo"),
):
    v = [
        counts[(c, key)]
        for c in ("main-asyncio", "main-rust", "branch-asyncio", "branch-rust")
    ]
    cg.append(
        f"| {label} | {v[0]:,} | {v[1]:,} | {v[2]:,} | {v[3]:,} | {100 * (v[3] / v[1] - 1):+.1f}% | {100 * (v[2] / v[0] - 1):+.1f}% |"
    )
Path(R / "callgrind-table.md").write_text("\n".join(cg) + "\n")
print("\n".join(cg))
assert glob.glob(f"{R}/raw/callgrind/*/callgrind.out.*")

after = [
    "| config | route | conns | req/s (200) | us CPU/req (+-) | p50 / p99 / p99.9 ms | non-200 |",
    "|---|---|--:|--:|--:|--:|--:|",
]
for config, route, label, conns in (
    ("branch-default", "json", "GET /json", 1024),
    ("branch-default", "items", "GET /items/7?q=abc", 1024),
    ("branch-default", "echo", "POST /echo", 1024),
    ("branch-default", "json", "GET /json", 64),
    ("main-rust", "json", "GET /json", 1024),
):
    paths = sorted(
        (R / "raw/after-inflight-fix" / config / route / f"c{conns}").glob(
            "run*/result.json"
        )
    )
    ds = [
        corrected_result(
            json.loads(path.read_text()),
            json.loads(path.with_name("oha.json").read_text()),
        )
        for path in paths
    ]
    cpu = [d["us_cpu_per_req"] for d in ds]
    name = (
        "branch-default (fixed)"
        if config == "branch-default"
        else "main-rust (same session)"
    )
    after.append(
        f"| {name} | {label} | {conns} | {statistics.median(d['ok_req_s'] for d in ds):,.0f} |"
        f" **{statistics.median(cpu):.2f}** (+-{(max(cpu) - min(cpu)) / 2:.2f}) |"
        f" {statistics.median(d['p50_ms'] for d in ds):.2f} /"
        f" {statistics.median(d['p99_ms'] for d in ds):.2f} /"
        f" {statistics.median(d['p99_9_ms'] for d in ds):.1f} |"
        f" {sum(d['total_responses'] - d['ok_responses'] for d in ds)} |"
    )
Path(R / "after-inflight-fix-table.md").write_text("\n".join(after) + "\n")
