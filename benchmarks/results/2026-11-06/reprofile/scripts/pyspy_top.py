#!/usr/bin/env python3
"""Top Python-level frames by self time from a py-spy raw (folded) file.
usage: pyspy_top.py folded.txt [N]
Python frame = 'func (path.py:LINE)' (or <frozen ...>); anything else is native.
 Table 1: samples whose LEAF frame is that Python function (pure interpreter self time, no native callee).
 Table 2: samples attributed to the INNERMOST Python frame (leaf may be native code called from it).
 Table 3: top leaf frames of any kind (native symbols demangled)."""

import collections
import re
import subprocess
import sys

path = sys.argv[1]
N = int(sys.argv[2]) if len(sys.argv) > 2 else 30
pat = re.compile(r"^(.*) \((.*?)(?::(\d+))?\)$")


def is_py(frame):
    m = pat.match(frame)
    return bool(m) and (m.group(2).endswith(".py") or m.group(2).startswith("<frozen"))


def key(frame):
    m = pat.match(frame)
    return f"{m.group(1)} ({m.group(2)})" if m else frame


leaf_py = collections.Counter()
inner_py = collections.Counter()
leaf_any = collections.Counter()
thread_total = collections.Counter()
total = 0
no_py = 0
all_total = 0
kept_idle = 0
with open(path) as fh:
    for line in fh:
        stack, _, n = line.rstrip("\n").rpartition(" ")
        n = int(n)
        frames = stack.split(";")
        all_total += n
        # py-spy --native also reports threads that are blocked: the supervisor/main thread
        # (futex wait in App._run), parked Tokio workers (no Python frames) etc. Keep only the
        # Python loop thread (stacks rooted at Thread._bootstrap) - the thread that runs Bessemer's
        # Python code - and report everything as a share of that thread's samples.
        if not frames[0].startswith("Thread._bootstrap ("):
            continue
        total += n
        leaf_any[key(frames[-1])] += n
        pyf = [f for f in frames if is_py(f)]
        if is_py(frames[-1]):
            leaf_py[key(frames[-1])] += n
        if pyf:
            inner_py[key(pyf[-1])] += n
        else:
            no_py += n


def dem(names):
    p = subprocess.run(
        ["llvm-cxxfilt"],
        input="\n".join(names),
        capture_output=True,
        text=True,
        check=True,
    )
    return p.stdout.split("\n")


def show(title, c, demangle=False):
    print(f"\n## {title}")
    items = c.most_common(N)
    names = dem([k for k, _ in items]) if demangle else [k for k, _ in items]
    for (k, n), nm in zip(items, names):
        print(f"{100 * n / total:6.2f}%  {n:7d}  {nm}")


print(
    f"# {path}\n# py-spy --native --rate 2000 -d 8 (blocking sampler; wall-clock samples, includes time blocked in syscalls)\n# all samples {all_total}; kept {total} from the Python loop thread (Thread._bootstrap root); dropped {all_total - total} from main/supervisor and Tokio threads\n# percentages are of the kept loop-thread samples. epoll_wait leaf = loop idle (waiting for work), not CPU."
)
show(f"Top {N} Python functions by self time, leaf is Python code", leaf_py)
show(
    f"Top {N} Python functions by self time incl. native callees (innermost Python frame)",
    inner_py,
)
show(f"Top {N} leaf frames of any kind", leaf_any, True)
