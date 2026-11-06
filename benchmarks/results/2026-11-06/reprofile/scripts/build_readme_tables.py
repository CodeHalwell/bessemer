#!/usr/bin/env python3
"""Compact median tables for the README. usage: build_readme_tables.py SUBDIR"""
import glob, json, statistics as st, sys
R = "/home/user/bessemer/benchmarks/results/2026-11-06/reprofile"
sub = sys.argv[1]
SERVERS = ["bessemer-asyncio", "bessemer-rust", "bessemer-rust-iouring", "axum"]
ROUTES = [("json", "GET /json"), ("items", "GET /items/7?q=abc"), ("echo", "POST /echo")]
rows = []
for f in sorted(glob.glob(f"{R}/raw/{sub}/*/*/c*/run*/result.json")):
    p = f.split("/"); d = json.load(open(f)); d["s"], d["r"] = p[-5], p[-4]; rows.append(d)
for rk, rn in ROUTES:
    sel = [d for d in rows if d["r"] == rk]
    if not sel: continue
    print(f"\n#### {rn}\n")
    print("| server | conns | req/s | CPUs | µs CPU/req | ctx sw/req | p50 ms | p99 ms | p99.9 ms | oha cores / util | flags |")
    print("|---|--:|--:|--:|--:|--:|--:|--:|--:|--:|---|")
    for s in SERVERS:
        for c in (1, 64, 1024):
            rs = [d for d in sel if d["s"] == s and d["connections"] == c]
            if not rs: continue
            m = lambda k: st.median([d[k] for d in rs])
            fl = []
            if any(d["client_limited"] for d in rs): fl.append("CLIENT-LIMITED")
            bad = [1 - d["ok_responses"] / d["total_responses"] for d in rs]
            if max(bad) > 0: fl.append(f"{100*st.median(bad):.0f}% 503")
            print(f"| {s} | {c} | {m('req_s'):,.0f} | {m('server_cpus'):.2f} | **{m('us_cpu_per_req'):.2f}** | {m('ctx_switches_per_req'):.3f} | "
                  f"{m('p50_ms'):.3f} | {m('p99_ms'):.2f} | {m('p99_9_ms'):.1f} | {rs[0]['client_cores']} / {m('client_util'):.2f} | {', '.join(fl)} |")
