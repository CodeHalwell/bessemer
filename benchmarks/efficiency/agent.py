"""Local-only controller for a single server and its complete process tree."""

import argparse
import json
import math
import os
import signal
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from resources import (
    Resources,
    cgroup,
    descendants,
    linux_processes,
    portable_processes,
)


class PhaseError(RuntimeError):
    pass


def verify_limits(args):
    actual = cgroup()
    if not args.require_limits:
        return {"enforced": False, "actual": actual}
    if actual is None or actual["pid1_comm"] not in {"docker-init", "tini"}:
        raise RuntimeError(
            "qualification requires cgroup v2 readback and init at PID 1"
        )
    quota, period = map(int, actual["cpu_max"].split())
    expected = args.cpus, args.memory_mb * 1048576, args.pids_limit
    found = quota / period, actual["memory_max_bytes"], actual["pids_max"]
    if found != expected:
        raise RuntimeError(
            f"resource readback differs: expected {expected}, found {found}"
        )
    if actual["cpuset_effective"] != args.cpuset:
        raise RuntimeError(f"unexpected effective cpuset: {actual['cpuset_effective']}")
    return {"enforced": True, "expected": expected, "actual": actual}


class Agent:
    def __init__(self, args):
        self.args, self.limits = args, verify_limits(args)
        self.lock = threading.RLock()
        self.phase, self.cleanup, self.resources = "idle", None, None
        environment = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith(("BESSEMER_", "UVICORN_")) and key != "PYTHONPATH"
        }
        environment["BENCHMARK_FRAMEWORK"] = args.framework
        if args.framework == "bessemer":
            command = [
                args.python,
                "-m",
                "bessemer",
                "run",
                "benchmark_app:app",
                "--workers",
                str(args.workers),
                "--loops",
                "1",
                "--sync-workers",
                "1",
                "--event-loop",
                args.event_loop,
                "--max-in-flight",
                "128",
                "--max-connections",
                "256",
            ]
        else:
            command = [
                args.python,
                "-m",
                "uvicorn",
                "benchmark_app:app",
                "--workers",
                str(args.workers),
                "--limit-concurrency",
                "256",
                "--no-access-log",
                "--log-level",
                "warning",
            ]
        command += ["--host", "0.0.0.0", "--port", str(args.port)]
        self.command = command
        self.process = subprocess.Popen(
            command,
            cwd=Path(__file__).resolve().parent,
            env=environment,
            start_new_session=True,
        )
        try:
            self.resources = Resources(
                self.process.pid, args.port, interval=args.interval
            )
        except BaseException:
            self.close()
            raise

    def snapshot(self):
        with self.lock:
            return {
                "pid": self.process.pid,
                "exit_code": self.process.poll(),
                "command": self.command,
                "limits": self.limits,
                "resources": self.resources.snapshot(),
            }

    def begin(self):
        with self.lock:
            if self.phase != "idle" or self.process.poll() is not None:
                raise PhaseError("measurement requires an idle live server")
            baseline = self.resources.begin()
            self.phase = "measuring"
            return baseline

    def end(self, duration):
        if not math.isfinite(duration) or duration <= 0:
            raise ValueError("X-Duration must be finite and positive")
        with self.lock:
            if self.phase != "measuring":
                raise PhaseError("no measurement is active")
            try:
                return self.resources.end(duration)
            finally:
                self.phase = "idle"

    def close(self):
        with self.lock:
            return self.close_locked()

    def close_locked(self):
        if self.cleanup is not None:
            return self.cleanup
        self.phase = "stopped"
        tracked = self.track_processes()
        if self.process.poll() is None:
            self.signal_group(signal.SIGTERM)
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.signal_group(signal.SIGKILL)
                self.process.wait(timeout=3)
        remaining = self.remaining(tracked)
        forced = sorted(remaining)
        for pid in remaining:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        deadline = time.monotonic() + 3
        while remaining and time.monotonic() < deadline:
            time.sleep(0.02)
            remaining = self.remaining(tracked)
        self.cleanup = {
            "tracked_pids": sorted(tracked),
            "forced_pids": forced,
            "remaining_pids": sorted(remaining),
            "server_exit_code": self.process.returncode,
        }
        return self.cleanup

    def track_processes(self):
        known = set()
        if self.resources is not None:
            self.resources.close()
            with self.resources.lock:
                known = self.resources.known.copy()
        rows = (
            linux_processes(False) if sys.platform == "linux" else portable_processes()
        )
        current = rows.get(self.process.pid)
        members = set()
        if current and (
            self.process.poll() is None or (self.process.pid, current["start"]) in known
        ):
            members = descendants(rows, self.process.pid)
        tracked = {pid: rows[pid]["start"] for pid in members}
        tracked.update(
            {
                pid: start
                for pid, start in known
                if pid in rows and rows[pid]["start"] == start
            }
        )
        return tracked

    def signal_group(self, signum):
        try:
            os.killpg(self.process.pid, signum)
        except ProcessLookupError:
            pass

    @staticmethod
    def remaining(tracked):
        rows = (
            linux_processes(False) if sys.platform == "linux" else portable_processes()
        )
        return {
            pid
            for pid, start in tracked.items()
            if pid in rows and rows[pid]["start"] == start
        }


class Handler(BaseHTTPRequestHandler):
    timeout = 10

    def log_message(self, *args):
        pass

    def respond(self, value, status=200):
        payload = json.dumps(value).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        self.respond(self.server.agent.snapshot())

    def do_POST(self):
        agent = self.server.agent
        try:
            self.operation(agent)
        except PhaseError as error:
            self.respond({"error": str(error)}, 409)
        except (ValueError, TypeError) as error:
            self.respond({"error": str(error)}, 400)

    def operation(self, agent):
        if self.path == "/begin":
            self.respond(agent.begin())
        elif self.path == "/end":
            self.respond(agent.end(float(self.headers.get("X-Duration", ""))))
        elif self.path == "/stop":
            cleanup = agent.close()
            try:
                self.respond(cleanup)
            finally:
                self.server.request_shutdown()
        else:
            self.respond({"error": "unknown operation"}, 404)


class Server(HTTPServer):
    def __init__(self, address):
        super().__init__(address, Handler)
        self.stopping = threading.Event()

    def request_shutdown(self):
        if not self.stopping.is_set():
            self.stopping.set()
            threading.Thread(target=self.shutdown, daemon=True).start()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--python", required=True)
    parser.add_argument("--framework", choices=("bessemer", "fastapi"), required=True)
    parser.add_argument("--event-loop", default="asyncio")
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--control-port", type=int, default=8001)
    parser.add_argument("--interval", type=float, default=0.25)
    parser.add_argument("--require-limits", action="store_true")
    parser.add_argument("--cpus", type=int, default=2)
    parser.add_argument("--memory-mb", type=int, default=512)
    parser.add_argument("--pids-limit", type=int, default=256)
    parser.add_argument("--cpuset", default="0-1")
    args = parser.parse_args()
    if args.workers < 1 or args.interval <= 0 or not math.isfinite(args.interval):
        parser.error("workers and sampling interval must be positive")
    agent = Agent(args)
    server = None
    try:
        server = Server(("0.0.0.0", args.control_port))
        server.agent = agent

        def shutdown(signum, frame):
            server.request_shutdown()

        signal.signal(signal.SIGTERM, shutdown)
        signal.signal(signal.SIGINT, shutdown)
        server.serve_forever()
    finally:
        agent.close()
        if server is not None:
            server.server_close()


if __name__ == "__main__":
    main()
