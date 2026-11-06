#!/usr/bin/env python3
"""Aggregate raw/matrix/<config>/<route>/c<N>/run<R>/result.json into markdown tables and a csv.

usage: aggregate.py [SUBDIR]   (default matrix; writes ../SUBDIR-tables.md, ../SUBDIR-all-runs.csv and,
for the main matrix, ../headline.md)
"""

import csv
import glob
import json
import statistics
import sys
from pathlib import Path

R = Path("/home/user/bessemer/benchmarks/results/2026-11-06/final-pass")
SUB = sys.argv[1] if len(sys.argv) > 1 else "matrix"
CONFIGS = [
    "main-asyncio",
    "main-rust",
    "branch-default",
    "branch-asyncio",
    "branch-rust-iouring",
    "axum",
]
ROUTES = [
    ("json", "GET /json"),
    ("items", "GET /items/7?q=abc"),
    ("echo", "POST /echo"),
]
CONNS = (1, 64, 1024)

rows = []
for f in sorted(glob.glob(f"{R}/raw/{SUB}/*/*/c*/run*/result.json")):
    parts = f.split("/")
    d = json.loads(Path(f).read_text())
    d["config"], d["route_key"], d["run"] = parts[-5], parts[-4], parts[-2]
    d["non200"] = d["total_responses"] - d["ok_responses"]
    d["ok_req_s"] = d["req_s"] * d["ok_responses"] / d["total_responses"]
    rows.append(d)

with open(R / f"{SUB}-all-runs.csv", "w", newline="") as fh:
    w = csv.writer(fh)
    w.writerow(
        [
            *("config", "route", "conns", "run", "req_s", "ok_req_s", "non200"),
            *("server_cpus", "us_cpu_per_req", "cs_per_req", "p50_ms", "p99_ms"),
            *("p99.9_ms", "client_util", "client_limited", "status"),
        ]
    )
    for d in rows:
        w.writerow(
            [d["config"], d["route_key"], d["connections"], d["run"],
             round(d["req_s"]), round(d["ok_req_s"]), d["non200"],
             round(d["server_cpus"], 3), round(d["us_cpu_per_req"], 2),
             round(d["ctx_switches_per_req"], 4), round(d["p50_ms"], 3),
             round(d["p99_ms"], 3), round(d["p99_9_ms"], 3),
             round(d["client_util"], 2), d["client_limited"], json.dumps(d["status"])]
        )  # fmt: skip


def cell(config, route, conns):
    rs = [
        d
        for d in rows
        if (d["config"], d["route_key"], d["connections"]) == (config, route, conns)
    ]
    return sorted(rs, key=lambda d: d["run"])


def med(rs, key):
    return statistics.median(d[key] for d in rs)


def spread(rs, key):
    v = [d[key] for d in rs]
    return (max(v) - min(v)) / 2


def flags(rs):
    out = []
    if any(d["client_limited"] for d in rs):
        out.append("CLIENT-LIMITED")
    if any(d["non200"] for d in rs):
        out.append("non-200")
    if len(rs) < 2:
        out.append(f"{len(rs)} run")
    return out


if SUB != "matrix":
    CONNS = tuple(c for c in CONNS if any(d["connections"] == c for d in rows))
    CONFIGS = [c for c in CONFIGS if any(d["config"] == c for d in rows)]
out = []
for rkey, rname in ROUTES:
    out.append(f"\n#### {rname}\n")
    head = "| config |"
    sep = "|---|"
    for c in CONNS:
        head += f" {c}: req/s (200) | CPUs | us CPU/req (+-) | ctx sw/req | p50 / p99 / p99.9 ms | non-200 | flags |"
        sep += "--:|--:|--:|--:|--:|--:|---|"
    out += [head, sep]
    for cfg in CONFIGS:
        line = f"| {cfg} |"
        for c in CONNS:
            rs = cell(cfg, rkey, c)
            if not rs:
                line += " - | - | - | - | - | - | MISSING |"
                continue
            non = sum(d["non200"] for d in rs) / sum(d["total_responses"] for d in rs)
            nn = f"{100 * non:.1f}%" if non else "0"
            line += (
                f" {med(rs, 'ok_req_s'):,.0f} | {med(rs, 'server_cpus'):.2f} |"
                f" **{med(rs, 'us_cpu_per_req'):.2f}** (+-{spread(rs, 'us_cpu_per_req'):.2f}) |"
                f" {med(rs, 'ctx_switches_per_req'):.3f} |"
                f" {med(rs, 'p50_ms'):.3f} / {med(rs, 'p99_ms'):.2f} / {med(rs, 'p99_9_ms'):.1f} |"
                f" {nn} | {', '.join(flags(rs))} |"
            )
        out.append(line)
Path(R / f"{SUB}-tables.md").write_text("\n".join(out) + "\n")

if SUB != "matrix":
    print("\n".join(out))
    raise SystemExit
hl = [
    (
        "| route | conns | main default (asyncio) us/req | branch default (rust) us/req |"
        " axum us/req | branch vs axum | main default vs axum | branch vs main default | notes |"
    ),
    "|---|--:|--:|--:|--:|--:|--:|--:|---|",
]
for rkey, rname in ROUTES:
    for c in (64, 1024):
        m, b, a = (cell(x, rkey, c) for x in ("main-asyncio", "branch-default", "axum"))
        mv, bv, av = (med(x, "us_cpu_per_req") for x in (m, b, a))
        notes = []
        for lab, x in (("main", m), ("branch", b), ("axum", a)):
            fl = flags(x)
            if fl:
                notes.append(f"{lab}: {', '.join(fl)}")
        hl.append(
            f"| {rname} | {c} | {mv:.2f} | {bv:.2f} | {av:.2f} | {100 * (bv / av - 1):+.0f}% |"
            f" {100 * (mv / av - 1):+.0f}% | {100 * (bv / mv - 1):+.0f}% | {'; '.join(notes)} |"
        )
Path(R / "headline.md").write_text("\n".join(hl) + "\n")
print("\n".join(hl))
print("\n".join(out))
