"""Same-host head-to-head: Bessemer vs FastAPI (uvicorn) vs Axum.

Each server is pinned to its CPUs with taskset and oha to a separate CPU.
Frameworks are interleaved within each round so drift affects all of them.
Exploratory: one host, short runs. Usage:

    python run_compare.py --python VENV/bin/python --oha PATH --axum PATH \
        --out results.json [--rounds 3] [--duration 10]
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
ECHO = json.dumps(
    {
        "items": [
            {"id": i, "name": f"item-{i}", "price": i * 1.25, "tags": ["a", "b"]}
            for i in range(20)
        ]
    }
).encode()
ROUTES = {
    "json": ("GET", "/json", None),
    "items": ("GET", "/items/7?q=abc", None),
    "echo": ("POST", "/echo", ECHO),
    "io": ("GET", "/io", None),
}
CONFIGS = {
    # name: (server CPUs, Python workers / Axum threads, client CPUs)
    "1-core": ("0", 1, "3"),
    "3-core": ("0-2", 3, "3"),
    # Two server cores with oha on the other two, so the client has headroom.
    "2-core": ("0-1", 2, "2-3"),
}
# Bessemer variants: name -> extra environment for the server.
BESSEMER_VARIANTS = {
    "bessemer": {},
    "bessemer-rust": {"BESSEMER_EVENT_LOOP": "bessemer"},
    "bessemer-uring": {
        "BESSEMER_EVENT_LOOP": "bessemer",
        "BESSEMER_IO_BACKEND": "io_uring",
    },
}


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def command(framework, args, port, workers):
    if framework in BESSEMER_VARIANTS:
        return [
            args.python,
            "-m",
            "bessemer",
            "run",
            "bessemer_app:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--workers",
            str(workers),
        ]
    if framework == "fastapi":
        return [
            args.python,
            "-m",
            "uvicorn",
            "fastapi_app:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--workers",
            str(workers),
            "--log-level",
            "warning",
            "--no-access-log",
        ]
    return [args.axum, str(port), str(workers)]


# Never route the readiness probe through an environment proxy.
DIRECT = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def wait_ready(port, deadline=30):
    end = time.monotonic() + deadline
    while time.monotonic() < end:
        try:
            with DIRECT.open(f"http://127.0.0.1:{port}/healthz", timeout=1):
                return
        except OSError:
            time.sleep(0.1)
    raise RuntimeError("server did not start")


def oha(args, port, route, duration, client_cpus):
    method, path, body = ROUTES[route]
    cmd = [
        "taskset",
        "-c",
        client_cpus,
        args.oha,
        "-z",
        f"{duration}s",
        "-c",
        str(args.connections),
        "--no-tui",
        "--output-format",
        "json",
        "-m",
        method,
    ]
    if body is not None:
        cmd += ["-H", "content-type: application/json", "-d", body.decode()]
    cmd.append(f"http://127.0.0.1:{port}{path}")
    output = subprocess.run(cmd, check=True, capture_output=True, text=True).stdout
    data = json.loads(output)
    codes = data.get("statusCodeDistribution", {})
    latency = data.get("latencyPercentiles", {})
    return {
        "rps": data["summary"]["requestsPerSec"],
        "p50_ms": latency.get("p50", 0) * 1000,
        "p99_ms": latency.get("p99", 0) * 1000,
        "non_200": sum(count for code, count in codes.items() if code != "200"),
        "errors": sum(data.get("errorDistribution", {}).values()),
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--python", required=True)
    parser.add_argument("--oha", required=True)
    parser.add_argument("--axum", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--rounds", type=int, default=3)
    parser.add_argument("--duration", type=int, default=10)
    parser.add_argument("--warmup", type=int, default=3)
    parser.add_argument("--connections", type=int, default=64)
    parser.add_argument("--client-cpus", help="override the config's client CPUs")
    parser.add_argument("--configs", default=",".join(CONFIGS))
    parser.add_argument("--frameworks", default="bessemer,fastapi,axum")
    parser.add_argument("--routes", default=",".join(ROUTES))
    args = parser.parse_args()
    # Servers start in this directory, so make caller-relative executable
    # paths absolute (without following the venv symlink); bare names are
    # still looked up on PATH.
    for name in ("python", "oha", "axum"):
        value = getattr(args, name)
        if os.sep in value:
            setattr(args, name, os.path.abspath(value))
    results = []
    for config in args.configs.split(","):
        cpus, workers, client_cpus = CONFIGS[config]
        client_cpus = args.client_cpus or client_cpus
        for round_index in range(args.rounds):
            for framework in args.frameworks.split(","):
                port = free_port()
                server = subprocess.Popen(
                    ["taskset", "-c", cpus, *command(framework, args, port, workers)],
                    cwd=HERE,
                    env=os.environ | BESSEMER_VARIANTS.get(framework, {}),
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True,
                )
                try:
                    wait_ready(port)
                    for route in args.routes.split(","):
                        oha(args, port, route, args.warmup, client_cpus)
                        sample = oha(args, port, route, args.duration, client_cpus)
                        sample |= {
                            "config": config,
                            "round": round_index,
                            "framework": framework,
                            "route": route,
                        }
                        results.append(sample)
                        print(json.dumps(sample), flush=True)
                finally:
                    os.killpg(server.pid, signal.SIGINT)
                    try:
                        server.wait(15)
                    except subprocess.TimeoutExpired:
                        os.killpg(server.pid, signal.SIGKILL)
                        server.wait()
    Path(args.out).write_text(json.dumps(results, indent=1))


if __name__ == "__main__":
    sys.exit(main())
