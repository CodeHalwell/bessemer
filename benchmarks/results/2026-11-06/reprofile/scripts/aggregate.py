#!/usr/bin/env python3
"""Aggregate raw/<subdir>/<server>/<route>/c<N>/run<R>/result.json into markdown + csv.
usage: aggregate.py SUBDIR [OUT_PREFIX]"""
import glob, json, os, statistics, sys
R = "/home/user/bessemer/benchmarks/results/2026-11-06/reprofile"
sub = sys.argv[1]
SERVERS = ["bessemer-asyncio", "bessemer-rust", "bessemer-rust-iouring", "axum"]
ROUTES = [("json", "GET /json"), ("items", "GET /items/7?q=abc"), ("echo", "POST /echo")]
rows = []
for f in sorted(glob.glob(f"{R}/raw/{sub}/*/*/c*/run*/result.json")):
    parts = f.split("/")
    d = json.load(open(f))
    d["cell_server"], d["cell_route"], d["cell_run"] = parts[-5], parts[-4], parts[-2]
    rows.append(d)
import csv
with open(f"{R}/raw/{sub}-all-runs.csv", "w", newline="") as fh:
    w = csv.writer(fh)
    w.writerow(["server", "route", "conns", "run", "req_s", "server_cpus", "us_cpu_per_req", "cs_per_req",
                "p50_ms", "p99_ms", "p99.9_ms", "client_cpus", "client_cores", "client_util", "ok", "total", "status"])
    for d in rows:
        w.writerow([d["cell_server"], d["cell_route"], d["connections"], d["cell_run"], round(d["req_s"]),
                    round(d["server_cpus"], 3), round(d["us_cpu_per_req"], 2), round(d["ctx_switches_per_req"], 4),
                    round(d["p50_ms"], 3), round(d["p99_ms"], 3), round(d["p99_9_ms"], 3),
                    round(d["client_cpus"], 2), d["client_cores"], round(d["client_util"], 2),
                    d["ok_responses"], d["total_responses"], json.dumps(d["status"])])
def med(v): return statistics.median(v)
out = []
for rkey, rname in ROUTES:
    out.append(f"\n#### {rname}\n")
    out.append("| server | conns | req/s (run1 / run2) | req/s median | CPUs | us CPU/req (run1 / run2) | us CPU/req median | ctx sw/req | p50 ms | p99 ms | p99.9 ms | oha util | notes |")
    out.append("|---|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|---|")
    for s in SERVERS:
        for c in (1, 64, 1024):
            rs = [d for d in rows if d["cell_server"] == s and d["cell_route"] == rkey and d["connections"] == c]
            if not rs: continue
            rs.sort(key=lambda d: d["cell_run"])
            notes = []
            if any(d["client_limited"] for d in rs):
                notes.append("CLIENT-LIMITED (oha >= 0.95 of its cores in %d of %d runs)" % (sum(d["client_limited"] for d in rs), len(rs)))
            bad = [d for d in rs if d["ok_responses"] < d["total_responses"]]
            if bad:
                fr = med([1 - d["ok_responses"] / d["total_responses"] for d in bad])
                notes.append("%.0f%% of responses non-200 (%s); req/s and CPU/req count them" % (100 * fr, ",".join(sorted({k for d in bad for k in d["status"] if k != "200"}))))
            # "aborted due to deadline" is oha cancelling the requests in flight when its -z timer ends (a few hundred); not a server error
            errs = sorted({k for d in rs for k in d["errors"] if k != "aborted due to deadline"})
            if errs:
                notes.append("oha errors: " + "; ".join(errs))
            cl = "%d core%s" % (rs[0]["client_cores"], "s" if rs[0]["client_cores"] > 1 else "")
            out.append("| %s | %d | %s | %s | %.2f | %s | %.2f | %.3f | %.3f | %.3f | %.3f | %s (%s) | %s |" % (
                s, c, " / ".join(f"{d['req_s']:,.0f}" for d in rs), f"{med([d['req_s'] for d in rs]):,.0f}",
                med([d["server_cpus"] for d in rs]),
                " / ".join(f"{d['us_cpu_per_req']:.2f}" for d in rs), med([d["us_cpu_per_req"] for d in rs]),
                med([d["ctx_switches_per_req"] for d in rs]),
                med([d["p50_ms"] for d in rs]), med([d["p99_ms"] for d in rs]), med([d["p99_9_ms"] for d in rs]),
                " / ".join(f"{d['client_util']:.2f}" for d in rs), cl, "; ".join(notes)))
open(f"{R}/raw/{sub}-tables.md", "w").write("\n".join(out) + "\n")
print("\n".join(out))
