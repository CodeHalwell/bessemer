"""Server CPU per request and requests per second, on Linux, macOS and Windows.

The Linux performance gate counts instructions under callgrind, which does
not exist on macOS or Windows. This harness measures what every platform can:
the server's own CPU time (user + system, for the server process and all its
children) divided by the requests it answered, plus the throughput and p99
latency oha saw. The same script on each runner gives figures that compare
across operating systems on similar hardware.

    python benchmarks/portable/measure.py --oha ./oha --out results.json \
        --loops asyncio,bessemer --workers 1,2 --routes json,items,echo

Workers=1 runs the application in a single process with ``app.run``, so the
figure is a worker's own cost. Workers above 1, and every count with
``--cli``, run under ``bessemer run --workers N`` (the supervisor and its
workers are all counted). The client is
not pinned to CPUs, the same way on every platform.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

import psutil

HERE = Path(__file__).resolve().parent
APP_DIRECTORY = HERE.parent / "compare"
WINDOWS = sys.platform == "win32"

ECHO = json.dumps(
    {
        "items": [
            {"id": i, "name": f"item-{i}", "price": i * 1.25, "tags": ["a", "b"]}
            for i in range(20)
        ]
    }
)
ROUTES = {
    "json": ("GET", "/json", None),
    "items": ("GET", "/items/7?q=abc", None),
    "echo": ("POST", "/echo", ECHO),
}

# Workers=1: the application in this one process, nothing in between.
SINGLE = """
import sys
sys.path.insert(0, {directory!r})
from bessemer_app import app
app.run(host="127.0.0.1", port={port}, event_loop={loop!r},
        max_in_flight=16384, max_connections=16384)
"""


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


class Server:
    def __init__(self, python: str, loop: str, workers: int, cli: bool = False):
        self.port = free_port()
        if workers == 1 and not cli:
            command = [
                python,
                "-c",
                SINGLE.format(directory=str(APP_DIRECTORY), port=self.port, loop=loop),
            ]
        else:
            command = [
                python,
                "-m",
                "bessemer",
                "run",
                "bessemer_app:app",
                "--host",
                "127.0.0.1",
                "--port",
                str(self.port),
                "--workers",
                str(workers),
                "--event-loop",
                loop,
                "--max-in-flight",
                "16384",
                "--max-connections",
                "16384",
            ]
        self.log = tempfile.TemporaryFile(mode="w+")  # noqa: SIM115 -- closed in stop().
        options: dict = {"cwd": APP_DIRECTORY, "stdout": self.log, "stderr": self.log}
        if WINDOWS:
            options["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            options["start_new_session"] = True
        self.process = subprocess.Popen(command, **options)
        self.tree = psutil.Process(self.process.pid)
        self.wait_ready()

    def wait_ready(self, timeout: float = 60) -> None:
        deadline = time.monotonic() + timeout
        url = f"http://127.0.0.1:{self.port}/healthz"
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                break
            try:
                with urllib.request.urlopen(url, timeout=1) as response:
                    if response.status == 200:
                        return
            except OSError:
                time.sleep(0.05)
        self.log.seek(0)
        output = self.log.read()
        self.stop()
        raise RuntimeError(f"server did not start:\n{output}")

    def cpu_seconds(self) -> float:
        """User + system time of the server and everything it started."""
        total = 0.0
        for process in [self.tree, *self.tree.children(recursive=True)]:
            try:
                times = process.cpu_times()
            except psutil.Error:
                continue
            total += times.user + times.system
        return total

    def stop(self) -> None:
        if self.process.poll() is None:
            if WINDOWS:
                subprocess.run(
                    ["taskkill", "/T", "/F", "/PID", str(self.process.pid)],
                    check=False,
                    capture_output=True,
                )
            else:
                try:
                    os.killpg(self.process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            self.process.wait(timeout=10)
        self.log.close()


def oha(binary: str, port: int, route: str, seconds: float, connections: int) -> dict:
    method, path, body = ROUTES[route]
    command = [
        binary,
        "-z",
        f"{seconds}s",
        "-c",
        str(connections),
        "--no-tui",
        "--output-format",
        "json",
        "-m",
        method,
    ]
    if body is not None:
        command += ["-H", "content-type: application/json", "-d", body]
    command.append(f"http://127.0.0.1:{port}{path}")
    output = subprocess.run(command, check=True, capture_output=True, text=True)
    data = json.loads(output.stdout)
    codes = data.get("statusCodeDistribution", {})
    ok = sum(count for code, count in codes.items() if code == "200")
    # Requests still in flight when the duration ends are reported as
    # aborted; they are the client stopping, not the server failing.
    errors = {
        error: count
        for error, count in data.get("errorDistribution", {}).items()
        if "aborted due to deadline" not in error
    }
    return {
        "ok": ok,
        "failed": sum(count for code, count in codes.items() if code != "200")
        + sum(errors.values()),
        "rps": data["summary"]["requestsPerSec"],
        "p99_ms": data.get("latencyPercentiles", {}).get("p99", 0) * 1000,
    }


def host() -> dict:
    return {
        "system": platform.system(),
        "release": platform.release(),
        "machine": platform.machine(),
        "processor": platform.processor(),
        "cpus": os.cpu_count(),
        "python": platform.python_version(),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument("--oha", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--markdown", help="append a table to this file")
    parser.add_argument("--title", default="Server CPU per request")
    parser.add_argument("--loops", default="asyncio,bessemer")
    parser.add_argument("--workers", default="1")
    parser.add_argument("--routes", default="json,items,echo")
    parser.add_argument("--connections", type=int, default=64)
    parser.add_argument("--seconds", type=float, default=5)
    parser.add_argument("--warmup", type=float, default=2)
    parser.add_argument("--rounds", type=int, default=3)
    parser.add_argument(
        "--cli",
        action="store_true",
        help="run every worker count, 1 included, under bessemer run",
    )
    args = parser.parse_args()

    results = []
    for workers in (int(value) for value in args.workers.split(",")):
        for loop in args.loops.split(","):
            server = Server(args.python, loop, workers, args.cli)
            try:
                for route in args.routes.split(","):
                    oha(args.oha, server.port, route, args.warmup, args.connections)
                    samples = []
                    for _ in range(args.rounds):
                        before = server.cpu_seconds()
                        sample = oha(
                            args.oha, server.port, route, args.seconds, args.connections
                        )
                        spent = server.cpu_seconds() - before
                        sample["cpu_us"] = spent / max(sample["ok"], 1) * 1e6
                        samples.append(sample)
                    # The median round by CPU per request, with its own rps.
                    samples.sort(key=lambda sample: sample["cpu_us"])
                    middle = samples[len(samples) // 2]
                    result = {
                        "loop": loop,
                        "workers": workers,
                        "supervised": workers > 1 or args.cli,
                        "route": route,
                        **middle,
                        # Failures in any round count, not just the median's.
                        "failed": sum(s["failed"] for s in samples),
                        "cpu_us_rounds": [round(s["cpu_us"], 2) for s in samples],
                    }
                    results.append(result)
                    print(json.dumps(result), flush=True)
            finally:
                server.stop()

    report = {"host": host(), "connections": args.connections, "results": results}
    Path(args.out).write_text(json.dumps(report, indent=2))
    if args.markdown:
        info = report["host"]
        lines = [
            f"### {args.title}",
            "",
            (
                f"{info['system']} {info['release']} {info['machine']}, "
                f"{info['cpus']} CPUs, Python {info['python']}; "
                f"oha with {args.connections} connections, median of {args.rounds} "
                f"x {args.seconds:g}s."
            ),
            "",
            "| Loop | Workers | Route | CPU µs/req | req/s | p99 ms | Failed |",
            "| --- | ---: | --- | ---: | ---: | ---: | ---: |",
        ]
        for r in results:
            lines.append(
                f"| {r['loop']} | {r['workers']}{' (run)' if r['supervised'] else ''} "
                f"| {r['route']} | {r['cpu_us']:.1f} "
                f"| {r['rps']:,.0f} | {r['p99_ms']:.2f} | {r['failed']} |"
            )
        with open(args.markdown, "a", encoding="utf-8") as summary:
            summary.write("\n".join(lines) + "\n\n")
    failed = sum(r["failed"] for r in results)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
