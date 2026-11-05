"""Server-side half of the two-machine comparison.

Runs on the server host and starts one framework at a time when the client
asks, so the load generator can run on separate hardware. It only launches
the three fixed comparison servers, and every request must carry the shared
token. Bind it to a private address; it is not meant for the open internet.

    BENCH_AGENT_TOKEN=... python server_agent.py --python VENV/bin/python \
        --axum PATH --listen 10.0.0.2:9100 --bind 10.0.0.2

Endpoints (JSON):
    GET  /info                        host details
    POST /start {framework, config}   stop any running server, start this one
    POST /stop                        stop the running server
    GET  /cpu                         busy/total jiffies on the server's CPUs
"""

from __future__ import annotations

import argparse
import hmac
import json
import os
import subprocess
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer

from servers import (
    FRAMEWORKS,
    Server,
    cpu_list,
    cpu_times,
    host_info,
    parse_config,
    resolve_executable,
)


def versions(python: str) -> dict:
    script = (
        "import importlib.metadata as m, json, sys\n"
        "out = {'python': sys.version.split()[0]}\n"
        "for name in ('bessemer', 'fastapi', 'uvicorn', 'pydantic', 'uvloop', 'httptools'):\n"
        "    try: out[name] = m.version(name)\n"
        "    except m.PackageNotFoundError: pass\n"
        "print(json.dumps(out))\n"
    )
    try:
        result = subprocess.run(
            [python, "-c", script],
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
        return json.loads(result.stdout)
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return {}


class Agent:
    def __init__(self, args):
        self.args = args
        self.server: Server | None = None
        self.cpus: list[int] | None = None
        self.info = host_info() | {"versions": versions(args.python)}

    def start(self, framework: str, config: str) -> dict:
        if framework not in FRAMEWORKS:
            raise ValueError(f"unknown framework {framework!r}")
        cpus, workers = parse_config(config)
        wanted = cpu_list(cpus)
        if max(wanted) >= (os.cpu_count() or 1):
            raise ValueError(f"{config} needs more CPUs than this host has")
        self.stop()
        self.server = Server(
            framework,
            python=self.args.python,
            axum=self.args.axum,
            host=self.args.bind,
            cpus=cpus,
            workers=workers,
        )
        self.cpus = wanted
        try:
            self.server.wait_ready()
        except Exception:
            self.stop()
            raise
        return {"port": self.server.port}

    def stop(self) -> dict:
        if self.server is not None:
            self.server.stop()
        self.server = None
        self.cpus = None
        return {"stopped": True}

    def cpu(self) -> dict:
        busy, total = cpu_times(self.cpus)
        return {"busy": busy, "total": total, "cpus": self.cpus}


def handler_for(agent: Agent, token: str):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):  # quiet
            pass

        def reply(self, status: int, body: dict) -> None:
            data = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def authorised(self) -> bool:
            supplied = self.headers.get("authorization", "")
            if hmac.compare_digest(supplied.encode(), f"Bearer {token}".encode()):
                return True
            self.reply(401, {"error": "bad token"})
            return False

        def do_GET(self):
            if not self.authorised():
                return
            if self.path == "/info":
                self.reply(200, agent.info)
            elif self.path == "/cpu":
                self.reply(200, agent.cpu())
            else:
                self.reply(404, {"error": "not found"})

        def do_POST(self):
            if not self.authorised():
                return
            length = int(self.headers.get("content-length") or 0)
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
                if self.path == "/start":
                    self.reply(200, agent.start(body["framework"], body["config"]))
                elif self.path == "/stop":
                    self.reply(200, agent.stop())
                else:
                    self.reply(404, {"error": "not found"})
            except (KeyError, ValueError, RuntimeError) as error:
                self.reply(400, {"error": str(error)})

    return Handler


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--python", required=True, help="venv with the apps installed")
    parser.add_argument("--axum", required=True, help="axum-compare release binary")
    parser.add_argument("--listen", default="0.0.0.0:9100", help="agent address")
    parser.add_argument("--bind", default="0.0.0.0", help="address servers bind to")
    args = parser.parse_args()
    token = os.environ.get("BENCH_AGENT_TOKEN", "")
    if len(token) < 16:
        sys.exit("set BENCH_AGENT_TOKEN to a random value of at least 16 characters")
    args.python = resolve_executable(args.python)
    args.axum = resolve_executable(args.axum)
    host, _, port = args.listen.rpartition(":")
    agent = Agent(args)
    server = HTTPServer((host, int(port)), handler_for(agent, token))
    print(f"agent listening on {args.listen}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        agent.stop()


if __name__ == "__main__":
    sys.exit(main())
