#!/usr/bin/env python3
"""Per-thread CPU per request. usage: threads_table.py glob-root-subdir -> markdown (median of runs)."""
import glob, json, statistics, sys, collections
R = "/home/user/bessemer/benchmarks/results/2026-11-06/reprofile"
pattern, = sys.argv[1:2]
rows = collections.defaultdict(list)
for f in sorted(glob.glob(f"{R}/{pattern}/result.json")):
    d = json.load(open(f)); key = f[len(R) + 5:].rsplit("/run", 1)[0] if "/run" in f else f[len(R)+5:].rsplit("/", 1)[0]
    th = [(k, v) for k, v in d["threads"].items() if v["cpu_s"] > 0.02 * d["window_s"] / 10]
    th.sort(key=lambda kv: -kv[1]["cpu_s"])
    rows[key].append((d["us_cpu_per_req"], [(v["comm"], v["us_per_req"], v["cpus"]) for k, v in th]))
print("| cell | run | total us/req (perf stat) | threads: comm = us/req (CPUs) |\n|---|--:|--:|---|")
for key, runs in rows.items():
    for i, (tot, th) in enumerate(runs, 1):
        print(f"| {key} | {i} | {tot:.2f} | " + "; ".join(f"{c} = {u:.2f} ({p:.2f})" for c, u, p in th) + " |")
