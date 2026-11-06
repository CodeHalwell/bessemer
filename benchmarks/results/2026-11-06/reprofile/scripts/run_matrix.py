#!/usr/bin/env python3
"""Run the matrix: servers x routes x connections, 2 rounds (round 1 for every
cell, then round 2), strictly sequential. Output: raw/matrix/<server>/<route>/c<N>/run<R>/."""
import os, subprocess, sys, time, json

R = "/home/user/bessemer/benchmarks/results/2026-11-06/reprofile"
SERVERS = {
    "bessemer-asyncio": ("bessemer", {"BESSEMER_EVENT_LOOP": "asyncio"}),
    "bessemer-rust": ("bessemer", {"BESSEMER_EVENT_LOOP": "bessemer"}),
    "bessemer-rust-iouring": ("bessemer", {"BESSEMER_EVENT_LOOP": "bessemer", "BESSEMER_IO_BACKEND": "io_uring"}),
    "axum": ("axum", {}),
}
ROUTES = {
    "json": ("/json", {}),
    "items": ("/items/7?q=abc", {}),
    "echo": ("/echo", {"METHOD": "POST", "BODY_FILE": f"{R}/scripts/order.json"}),
}
CONNS = [1, 64, 1024]
rounds = int(os.environ.get("ROUNDS", "2"))
subdir = os.environ.get("SUBDIR", "matrix")
only_conns = os.environ.get("ONLY_CONNS")
if only_conns:
    CONNS = [int(x) for x in only_conns.split(",")]
client64 = os.environ.get("CLIENT64", "3")  # oha CPUs for 1 and 64 connections
only_servers = os.environ.get("ONLY_SERVERS")
extra = dict(kv.split("=", 1) for kv in os.environ.get("SERVER_EXTRA", "").split() if kv)  # oha CPUs for 1 and 64 connections
base = dict(os.environ, PYTHON="/home/user/venv/bin/python", OHA=os.path.expanduser("~/.cargo/bin/oha"),
            PATH=os.environ["PATH"] + ":/usr/local/bin")
for k in list(base):
    if k.startswith("BESSEMER_"):
        del base[k]
port = 18100
for rnd in range(1, rounds + 1):
    for sname, (which, senv) in SERVERS.items():
        if only_servers and sname not in only_servers.split(","):
            continue
        senv = dict(senv, **(extra if which == "bessemer" else {}))
        for rname, (route, renv) in ROUTES.items():
            for c in CONNS:
                port += 1
                out = f"{R}/raw/{subdir}/{sname}/{rname}/c{c}/run{rnd}"
                if os.path.exists(f"{out}/result.json"):
                    continue
                env = dict(base, **senv, **renv, CONNECTIONS=str(c), PORT=str(port),
                           CLIENT_CPU="1-3" if c == 1024 else client64, SERVER_CPU="0")
                t = time.time()
                p = subprocess.run(["bash", f"{R}/scripts/profile_route.sh", which, route, out],
                                   env=env, capture_output=True, text=True)
                line = (p.stdout.strip().splitlines() or ["(no output)"])[-1]
                print(f"[{time.strftime('%H:%M:%S')}] r{rnd} {sname} {rname} c{c} rc={p.returncode} {line}", flush=True)
                if p.returncode:
                    print(p.stderr[-500:], flush=True)
                    os.makedirs(out, exist_ok=True)
                    open(f"{out}/FAILED.txt", "w").write(p.stderr + p.stdout)
                if sname == "bessemer-rust-iouring":
                    log = open(f"{out}/server.log").read() if os.path.exists(f"{out}/server.log") else ""
                    if "serving connections through io_uring" not in log:
                        print("  WARNING: io_uring not confirmed in server.log", flush=True)
                time.sleep(2)
