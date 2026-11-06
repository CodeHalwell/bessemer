#!/usr/bin/env python3
"""Final before/after matrix: configs x routes x connections, 2 rounds, strictly sequential.

Round 1 runs the configs in the order main -> branch -> axum, round 2 in the
reverse order, so neither side always runs first. Output:
raw/matrix/<config>/<route>/c<N>/run<R>/ (result.json, oha.json, server.log, loadavg.txt).
Environment: ONLY_CONFIGS, ONLY_CONNS, ROUNDS, SUBDIR, CLIENT_CPU (default 1-3), SERVER_EXTRA.
"""

import os
import subprocess
import time
from pathlib import Path

R = "/home/user/bessemer/benchmarks/results/2026-11-06/final-pass"
MAIN = "/home/user/venv/bin/python"
BRANCH = "/home/user/venvs/integrated/bin/python"
CONFIGS = {
    "main-asyncio": ("bessemer", MAIN, {"BESSEMER_EVENT_LOOP": "asyncio"}, "asyncio"),
    "main-rust": ("bessemer", MAIN, {"BESSEMER_EVENT_LOOP": "bessemer"}, "bessemer"),
    "branch-default": ("bessemer", BRANCH, {"BESSEMER_EVENT_LOOP": "auto"}, ""),
    "branch-asyncio": (
        "bessemer",
        BRANCH,
        {"BESSEMER_EVENT_LOOP": "asyncio"},
        "asyncio",
    ),
    "branch-rust-iouring": (
        "bessemer",
        BRANCH,
        {"BESSEMER_EVENT_LOOP": "bessemer", "BESSEMER_IO_BACKEND": "io_uring"},
        "bessemer",
    ),
    "axum": ("axum", MAIN, {}, ""),
}
ROUTES = {
    "json": ("/json", {}),
    "items": ("/items/7?q=abc", {}),
    "echo": ("/echo", {"METHOD": "POST", "BODY_FILE": f"{R}/scripts/order.json"}),
}
conns = [int(x) for x in os.environ.get("ONLY_CONNS", "1,64,1024").split(",")]
rounds = int(os.environ.get("ROUNDS", "2"))
subdir = os.environ.get("SUBDIR", "matrix")
client = os.environ.get("CLIENT_CPU", "1-3")
only = os.environ.get("ONLY_CONFIGS")
extra = dict(
    kv.split("=", 1) for kv in os.environ.get("SERVER_EXTRA", "").split() if kv
)
base = dict(
    os.environ,
    OHA=os.path.expanduser("~/.cargo/bin/oha"),
    PATH=os.environ["PATH"] + ":/usr/local/bin",
)
for key in list(base):
    if key.startswith("BESSEMER_"):
        del base[key]
port = 19100
names = list(CONFIGS)
for rnd in range(1, rounds + 1):
    order = names if rnd % 2 == 1 else names[::-1]
    for name in order:
        if only and name not in only.split(","):
            continue
        which, python, senv, loop = CONFIGS[name]
        if which == "bessemer":
            senv = dict(senv, **extra)
        for rname, (route, renv) in ROUTES.items():
            for c in conns:
                port += 1
                out = f"{R}/raw/{subdir}/{name}/{rname}/c{c}/run{rnd}"
                if os.path.exists(f"{out}/result.json"):
                    continue
                os.makedirs(out, exist_ok=True)
                Path(f"{out}/loadavg.txt").write_text(Path("/proc/loadavg").read_text())
                env = dict(
                    base,
                    **senv,
                    **renv,
                    PYTHON=python,
                    CONNECTIONS=str(c),
                    PORT=str(port),
                    CLIENT_CPU=client,
                    SERVER_CPU="0",
                )
                p = subprocess.run(
                    ["bash", f"{R}/scripts/profile_route.sh", which, route, out],
                    env=env,
                    capture_output=True,
                    text=True,
                    check=False,
                )
                line = (p.stdout.strip().splitlines() or ["(no output)"])[-1]
                print(
                    f"[{time.strftime('%H:%M:%S')}] r{rnd} {name} {rname} c{c} rc={p.returncode} {line}",
                    flush=True,
                )
                if p.returncode:
                    print(p.stderr[-500:], flush=True)
                    Path(f"{out}/FAILED.txt").write_text(p.stderr + p.stdout)
                time.sleep(2)
