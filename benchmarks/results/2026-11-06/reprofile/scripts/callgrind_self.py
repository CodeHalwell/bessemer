#!/usr/bin/env python3
"""Self-cost (Ir) per function from a callgrind out file, in callgrind_annotate's '--inclusive=no' layout.
Own parser because callgrind_annotate 3.22 prints bogus self costs (>1000%) for these profiles
(self cost of recursive '2 cycle entries is mis-accumulated). Cost lines that follow a 'calls=' line
are the inclusive cost of the call and are NOT added to the caller's self cost.
usage: callgrind_self.py FILE [N]"""
import re, subprocess, sys, collections
path = sys.argv[1]; N = int(sys.argv[2]) if len(sys.argv) > 2 else 50
names = {"fn": {}, "ob": {}, "fl": {}, "fi": {}, "fe": {}}
def unc(kind, s):
    m = re.match(r"\((\d+)\)(?: (.*))?$", s)
    if not m: return s
    if m.group(2) is not None: names[kind][m.group(1)] = m.group(2); return m.group(2)
    return names[kind][m.group(1)]
self_c = collections.Counter(); obj_of = {}; summary = 0
fn = ob = fl = None; after_calls = False; total = 0
callee_ob = None
for line in open(path):
    line = line.rstrip("\n")
    if line.startswith("summary:"): summary = int(line.split()[1]); continue
    if line.startswith("fn="): fn = unc("fn", line[3:]); obj_of[fn] = ob; after_calls = False; continue
    if line.startswith("ob="): ob = unc("ob", line[3:]); continue
    if line.startswith("fl=") or line.startswith("fi=") or line.startswith("fe="):
        unc("fl", line[3:]); continue
    if line.startswith("cfn="): unc("fn", line[4:]); continue
    if line.startswith(("cob=",)): unc("ob", line[4:]); continue
    if line.startswith(("cfi=", "cfl=")): unc("fl", line[4:]); continue
    if line.startswith("calls="): after_calls = True; continue
    if line and (line[0].isdigit() or line[0] in "+-*"):
        if after_calls: after_calls = False; continue
        parts = line.split()
        if len(parts) >= 2 and fn is not None:
            c = int(parts[1]); self_c[fn] += c; total += c
dem = subprocess.run(["llvm-cxxfilt"], input="\n".join(k for k, _ in self_c.most_common(N)), capture_output=True, text=True).stdout.split("\n")
print(f"# {path}\n# self cost (Ir) per function, parsed directly from the callgrind file; summed self cost {total:,} vs summary: {summary:,}")
print(f"# {'Ir':>12}  {'%':>6}  function [object]")
for (k, c), d in zip(self_c.most_common(N), dem):
    print(f"{c:14,}  {100*c/total:6.2f}  {d} [{(obj_of.get(k) or '?').split('/')[-1]}]")
