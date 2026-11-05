"""Head-to-head: Bessemer vs FastAPI (uvicorn) vs Axum.

Two modes:

* Same host (default). Each server is pinned to its CPUs with taskset and oha
  to a separate CPU. Exploratory only: on a small host the single client core
  becomes the ceiling (about 130k/s in the 2026-11-05 run).
* Two machines (--agent). The servers run on another host under
  server_agent.py and oha runs here, using every local core unless
  --client-cpus is given.

Frameworks are interleaved within each round so drift affects all of them.
Each sample records how busy the client and server CPUs were, and flags a
sample as client-bound when the load generator was the likely ceiling.

    python run_compare.py --python VENV/bin/python --oha PATH --axum PATH \
        --out results.json [--rounds 3] [--duration 10]

    BENCH_AGENT_TOKEN=... python run_compare.py --oha PATH \
        --agent http://10.0.0.2:9100 --configs 1-core,2-core,4-core \
        --out results.json
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from servers import (
    DIRECT,
    Server,
    cpu_list,
    cpu_times,
    host_info,
    parse_config,
    resolve_executable,
    utilisation,
)

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
# A sample is client-bound when the load generator's CPUs were this busy.
CLIENT_BOUND = 0.85


class LocalServers:
    """Servers on this host, pinned away from the client CPU."""

    def __init__(self, args):
        self.args = args
        self.server: Server | None = None
        self.cpus: list[int] | None = None

    def info(self) -> dict:
        return host_info()

    def start(self, framework: str, config: str) -> tuple[str, int]:
        cpus, workers = parse_config(config)
        self.server = Server(
            framework,
            python=self.args.python,
            axum=self.args.axum,
            host="127.0.0.1",
            cpus=cpus,
            workers=workers,
        )
        self.cpus = cpu_list(cpus)
        try:
            self.server.wait_ready()
        except Exception:
            self.stop()
            raise
        return "127.0.0.1", self.server.port

    def cpu(self) -> tuple[int, int]:
        return cpu_times(self.cpus)

    def stop(self) -> None:
        if self.server is not None:
            self.server.stop()
        self.server = None


class RemoteServers:
    """Servers on another host, driven through server_agent.py."""

    def __init__(self, args):
        self.base = args.agent.rstrip("/")
        self.target = args.target_host or urllib.parse.urlsplit(self.base).hostname
        token = os.environ.get("BENCH_AGENT_TOKEN", "")
        if not token:
            sys.exit("set BENCH_AGENT_TOKEN to the token the agent was started with")
        self.headers = {"authorization": f"Bearer {token}"}

    def call(self, method: str, path: str, body: dict | None = None) -> dict:
        data = None if body is None else json.dumps(body).encode()
        request = urllib.request.Request(
            self.base + path, data=data, method=method, headers=self.headers
        )
        if data is not None:
            request.add_header("content-type", "application/json")
        try:
            with DIRECT.open(request, timeout=60) as response:
                return json.load(response)
        except urllib.error.HTTPError as error:
            raise RuntimeError(f"agent {path}: {error.read().decode()}") from error

    def info(self, deadline: float = 120) -> dict:
        # The agent may still be starting up (it collects package versions
        # before it listens), so retry refused connections for a while.
        end = time.monotonic() + deadline
        while True:
            try:
                return self.call("GET", "/info")
            except urllib.error.URLError as error:
                if isinstance(error, urllib.error.HTTPError):
                    raise
                if time.monotonic() > end:
                    raise RuntimeError(f"agent at {self.base} not reachable") from error
                time.sleep(1)

    def start(self, framework: str, config: str) -> tuple[str, int]:
        reply = self.call("POST", "/start", {"framework": framework, "config": config})
        return self.target, reply["port"]

    def cpu(self) -> tuple[int, int]:
        reply = self.call("GET", "/cpu")
        return reply["busy"], reply["total"]

    def stop(self) -> None:
        self.call("POST", "/stop", {})


def oha(args, host, port, route, duration, client_cpus):
    method, path, body = ROUTES[route]
    # oha defaults to one worker per physical core. Give it one per logical
    # CPU it may use, so a saturated client shows up as busy CPUs and the
    # client_bound flag can see it on SMT hosts too.
    workers = len(cpu_list(client_cpus)) if client_cpus else os.cpu_count()
    cmd = [args.oha, "--worker-threads", str(workers or 1)]
    if client_cpus:
        cmd = ["taskset", "-c", client_cpus, *cmd]
    cmd += [
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
    cmd.append(f"http://{host}:{port}{path}")
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
    parser.add_argument("--python", help="venv with the apps (same-host mode)")
    parser.add_argument("--axum", help="axum-compare binary (same-host mode)")
    parser.add_argument("--oha", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--agent", help="server_agent.py URL for two-machine mode")
    parser.add_argument(
        "--target-host", help="address the servers listen on (default: agent host)"
    )
    parser.add_argument("--rounds", type=int, default=3)
    parser.add_argument("--duration", type=int, default=10)
    parser.add_argument("--warmup", type=int, default=3)
    parser.add_argument("--connections", type=int, default=64)
    parser.add_argument(
        "--client-cpus",
        help="pin oha to these CPUs (default: on one host CPU 3, or 2-3 for "
        "2-core; all CPUs remotely)",
    )
    parser.add_argument("--configs", default="1-core,3-core,2-core")
    parser.add_argument("--frameworks", default="bessemer,fastapi,axum")
    parser.add_argument("--routes", default=",".join(ROUTES))
    args = parser.parse_args()
    if args.agent:
        servers = RemoteServers(args)
        mode = "two-machine"
    else:
        if not (args.python and args.axum):
            parser.error("same-host mode needs --python and --axum")
        args.python = resolve_executable(args.python)
        args.axum = resolve_executable(args.axum)
        servers = LocalServers(args)
        mode = "same-host"
    args.oha = resolve_executable(args.oha)
    for config in args.configs.split(","):
        parse_config(config)
    routes = args.routes.split(",")

    meta = {
        "mode": mode,
        "started": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "client": host_info() | {"oha": oha_version(args.oha)},
        "server": servers.info(),
        "settings": {
            key: getattr(args, key)
            for key in (
                "rounds",
                "duration",
                "warmup",
                "connections",
                "client_cpus",
                "configs",
                "frameworks",
            )
        },
    }
    print(json.dumps({"meta": meta}), flush=True)
    samples = []
    out = Path(args.out)
    for config in args.configs.split(","):
        client_spec = args.client_cpus
        if mode == "same-host" and not client_spec:
            # On a four-CPU host oha gets CPU 3, or CPUs 2-3 when the
            # servers only use two cores.
            client_spec = "2-3" if config == "2-core" else "3"
        client_cpus = cpu_list(client_spec) if client_spec else None
        for round_index in range(args.rounds):
            for framework in args.frameworks.split(","):
                host, port = servers.start(framework, config)
                try:
                    for route in routes:
                        oha(args, host, port, route, args.warmup, client_spec)
                        client_before = cpu_times(client_cpus)
                        server_before = servers.cpu()
                        sample = oha(
                            args, host, port, route, args.duration, client_spec
                        )
                        server_cpu = utilisation(server_before, servers.cpu())
                        client_cpu = utilisation(client_before, cpu_times(client_cpus))
                        sample |= {
                            "config": config,
                            "round": round_index,
                            "framework": framework,
                            "route": route,
                            "client_cpu": round(client_cpu, 3),
                            "server_cpu": round(server_cpu, 3),
                            "client_bound": client_cpu >= CLIENT_BOUND,
                        }
                        samples.append(sample)
                        print(json.dumps(sample), flush=True)
                        # Write as we go so a long cloud run keeps partial data.
                        out.write_text(
                            json.dumps({"meta": meta, "samples": samples}, indent=1)
                        )
                finally:
                    servers.stop()
    out.write_text(json.dumps({"meta": meta, "samples": samples}, indent=1))


def oha_version(path: str) -> str:
    try:
        return subprocess.run(
            [path, "--version"], check=False, capture_output=True, text=True, timeout=10
        ).stdout.strip()
    except OSError:
        return ""


if __name__ == "__main__":
    sys.exit(main())
