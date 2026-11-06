import collections
import json
import pickle
import re

from classify import bucket

P = "/home/user/bessemer/benchmarks/results/2026-11-06/reprofile/raw/perf/"


def cgdist(name):
    with open(f"{name}.pkl", "rb") as fh:
        selfc, obj, calls, _summary = pickle.load(fh)
    callers = collections.defaultdict(list)
    for (a, b), c in calls.items():
        callers[b].append((a, c))
    memo = {}

    def dist(f, depth=0, stack=()):
        if f in memo:
            return memo[f]
        b = bucket(f, obj.get(f))
        if b:
            memo[f] = {b: 1.0}
            return memo[f]
        if depth > 40 or f in stack:
            return {"unattributed": 1.0}
        cs = callers.get(f, [])
        tot = sum(c for _, c in cs)
        if not tot:
            return {"unattributed": 1.0}
        d = collections.Counter()
        for a, c in cs:
            for k, v in dist(a, depth + 1, stack + (f,)).items():
                d[k] += v * c / tot
        if depth == 0:
            memo[f] = dict(d)
        return dict(d)

    return {f: dist(f) for f in selfc}, obj


out = {}
for perfdir, cg, total in [
    ("bessemer-rust-json", "bessemer-json", 12.22),
    ("bessemer-rust-echo", "bessemer-echo", 16.41),
    ("bessemer-asyncio-json", "asyncio-json", 13.86),
    ("bessemer-asyncio-echo", "asyncio-echo", 18.53),
]:
    D, obj = cgdist(cg)
    res = collections.Counter()
    miss = collections.Counter()
    with open(P + perfdir + "/perf-report-dso-sym-full.demangled.txt") as fh:
        for line in fh:
            m = re.match(r"\s*([\d.]+)%\s+(\S+)\s+\[(.)\]\s+(.*)$", line)
            if not m:
                continue
            pct = float(m.group(1))
            dso = m.group(2)
            sym = m.group(4).strip()
            if m.group(3) == "k" or "kernel" in dso:
                res["kernel"] += pct
                continue
            if "vdso" in dso:
                res["rust_http"] += pct
                continue
            s = sym.replace("'2", "")
            if s in D:
                for k, v in D[s].items():
                    res[k] += pct * v
            else:
                b = bucket(s, dso)
                if b:
                    res[b] += pct
                else:
                    miss[(dso.split("/")[-1], s[:80])] += pct
                    res["unmatched:" + dso.split("/")[-1]] += pct
    t = sum(res.values())
    print(perfdir, f"sum%={t:.1f}")
    for k, v in res.most_common():
        print(f"   {k:34s}{v:6.1f}%  {total * v / t:6.2f} us")
    print("   top unmatched:", [(k, round(v, 2)) for k, v in miss.most_common(6)])
    out[perfdir] = {k: total * v / t for k, v in res.items()}
with open("perfsplit.json", "w") as fh:
    json.dump(out, fh, indent=1)
