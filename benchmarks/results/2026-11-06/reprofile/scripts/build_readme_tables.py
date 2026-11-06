#!/usr/bin/env python3
"""Compact median tables for the README. usage: build_readme_tables.py SUBDIR"""

import glob
import json
import statistics as st
import sys
from pathlib import Path

R = "/home/user/bessemer/benchmarks/results/2026-11-06/reprofile"
sub = sys.argv[1]
SERVERS = ["bessemer-asyncio", "bessemer-rust", "bessemer-rust-iouring", "axum"]
ROUTES = [
    ("json", "GET /json"),
    ("items", "GET /items/7?q=abc"),
    ("echo", "POST /echo"),
]
rows = []


def med(rs, k):
    return st.median([d[k] for d in rs])


for f in sorted(glob.glob(f"{R}/raw/{sub}/*/*/c*/run*/result.json")):
    p = f.split("/")
    d = json.loads(Path(f).read_text())
    d["s"], d["r"] = p[-5], p[-4]
    rows.append(d)
for rk, rn in ROUTES:
    sel = [d for d in rows if d["r"] == rk]
    if not sel:
        continue
    print(f"\n#### {rn}\n")
    print(
        "| server | conns | req/s | CPUs | µs CPU/req | ctx sw/req | p50 ms | p99 ms | p99.9 ms | oha cores / util | flags |"
    )
    print("|---|--:|--:|--:|--:|--:|--:|--:|--:|--:|---|")
    for s in SERVERS:
        for c in (1, 64, 1024):
            rs = [d for d in sel if d["s"] == s and d["connections"] == c]
            if not rs:
                continue
            fl = []
            if any(d["client_limited"] for d in rs):
                fl.append("CLIENT-LIMITED")
            bad = [1 - d["ok_responses"] / d["total_responses"] for d in rs]
            if max(bad) > 0:
                fl.append(f"{100 * st.median(bad):.0f}% 503")
            print(
                f"| {s} | {c} | {med(rs, 'req_s'):,.0f} | {med(rs, 'server_cpus'):.2f} | **{med(rs, 'us_cpu_per_req'):.2f}** | {med(rs, 'ctx_switches_per_req'):.3f} | "
                f"{med(rs, 'p50_ms'):.3f} | {med(rs, 'p99_ms'):.2f} | {med(rs, 'p99_9_ms'):.1f} | {rs[0]['client_cores']} / {med(rs, 'client_util'):.2f} | {', '.join(fl)} |"
            )
