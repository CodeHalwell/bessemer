#!/usr/bin/env python3
"""Top Python frames by self time from a non-native py-spy raw file (leaf frame = self). usage: file [N]"""
import collections, re, sys
path = sys.argv[1]; N = int(sys.argv[2]) if len(sys.argv) > 2 else 30
pat = re.compile(r"^(.*) \((.*?):(\d+)\)$")
self_c = collections.Counter(); total = 0
for line in open(path):
    stack, _, n = line.rstrip("\n").rpartition(" "); n = int(n); total += n
    m = pat.match(stack.split(";")[-1]); self_c[f"{m.group(1)} ({m.group(2)})" if m else stack.split(";")[-1]] += n
print(f"# {path}: py-spy (no --native) rate 2000, 8 s, {total} samples (idle threads excluded by py-spy's default heuristic)")
for k, n in self_c.most_common(N): print(f"{100*n/total:6.2f}%  {n:7d}  {k}")
