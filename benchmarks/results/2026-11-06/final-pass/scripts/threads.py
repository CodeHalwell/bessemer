"""Per-thread CPU snapshots from /proc. Usage:
threads.py snap PIDS OUTFILE            write {"t": monotonic, "threads": {pid/tid: [comm, ticks]}}
threads.py diff BEFORE AFTER REQUESTS   print per-thread us CPU per request (json)
"""

import json
import os
import sys
import time
from pathlib import Path

HZ = os.sysconf("SC_CLK_TCK")


def snap(pids, out):
    threads = {}
    for pid in pids.split(","):
        try:
            tids = os.listdir(f"/proc/{pid}/task")
        except OSError:
            continue
        for tid in tids:
            try:
                comm = Path(f"/proc/{pid}/task/{tid}/comm").read_text().strip()
                raw = Path(f"/proc/{pid}/task/{tid}/stat").read_text()
                f = raw[raw.rindex(")") + 2 :].split()
                threads[f"{pid}/{tid}"] = [comm, int(f[11]) + int(f[12])]  # utime+stime
            except OSError:
                pass
    with open(out, "w") as fh:
        json.dump({"t": time.monotonic(), "threads": threads}, fh)


def diff(before, after, requests):
    a = json.loads(Path(before).read_text())
    b = json.loads(Path(after).read_text())
    res = {"window_s": b["t"] - a["t"], "requests": requests, "threads": {}}
    for k, (comm, t1) in b["threads"].items():
        t0 = a["threads"].get(k, [comm, 0])[1]
        cpu_s = (t1 - t0) / HZ
        res["threads"][k] = {
            "comm": comm,
            "cpu_s": cpu_s,
            "cpus": cpu_s / res["window_s"],
            "us_per_req": cpu_s * 1e6 / requests if requests else None,
        }
    json.dump(res, sys.stdout)


if sys.argv[1] == "snap":
    snap(sys.argv[2], sys.argv[3])
else:
    diff(sys.argv[2], sys.argv[3], float(sys.argv[4]))
