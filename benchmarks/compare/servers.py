"""Start and stop the three comparison servers, and read CPU counters.

Shared by run_compare.py (servers on the same host as the client) and
server_agent.py (servers on a separate host, driven over HTTP).
"""

from __future__ import annotations

import os
import platform
import signal
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
FRAMEWORKS = ("bessemer", "fastapi", "axum")
# Never route a readiness probe through an environment proxy.
DIRECT = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def parse_config(name: str) -> tuple[str, int]:
    """Map "N-core" to (CPU list for taskset, workers or threads)."""
    count, _, suffix = name.partition("-")
    if suffix != "core" or not count.isdigit() or int(count) < 1:
        raise ValueError(f"config must look like '4-core', not {name!r}")
    count = int(count)
    return ("0" if count == 1 else f"0-{count - 1}"), count


def cpu_list(spec: str) -> list[int]:
    cpus: list[int] = []
    for part in spec.split(","):
        first, _, last = part.partition("-")
        cpus.extend(range(int(first), int(last or first) + 1))
    return cpus


def free_port(host: str) -> int:
    with socket.socket() as sock:
        sock.bind((host, 0))
        return sock.getsockname()[1]


def command(framework, python, axum, host, port, workers):
    if framework == "bessemer":
        return [
            python,
            "-m",
            "bessemer",
            "run",
            "bessemer_app:app",
            "--host",
            host,
            "--port",
            str(port),
            "--workers",
            str(workers),
            # The defaults (128 in flight, 1024 connections per worker) would
            # answer 503 at high client concurrency, and the other two
            # servers have no such admission limit.
            "--max-in-flight",
            "16384",
            "--max-connections",
            "16384",
        ]
    if framework == "fastapi":
        return [
            python,
            "-m",
            "uvicorn",
            "fastapi_app:app",
            "--host",
            host,
            "--port",
            str(port),
            "--workers",
            str(workers),
            "--log-level",
            "warning",
            "--no-access-log",
        ]
    if framework == "axum":
        return [axum, str(port), str(workers), host]
    raise ValueError(f"unknown framework {framework!r}")


def resolve_executable(value: str) -> str:
    """Make a caller-relative path absolute without following venv symlinks.

    Servers start in this directory, so relative paths would otherwise break.
    Bare names are still looked up on PATH.
    """
    return os.path.abspath(value) if os.sep in value else value


class Server:
    """One framework process group, pinned to its CPUs."""

    def __init__(self, framework, *, python, axum, host, cpus, workers):
        probe = "127.0.0.1" if host in ("0.0.0.0", "") else host
        self.port = free_port(probe)
        self.probe = probe
        self.process = subprocess.Popen(
            [
                "taskset",
                "-c",
                cpus,
                *command(framework, python, axum, host, self.port, workers),
            ],
            cwd=HERE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )

    def wait_ready(self, deadline: float = 30) -> None:
        end = time.monotonic() + deadline
        while time.monotonic() < end:
            if self.process.poll() is not None:
                raise RuntimeError(f"server exited with {self.process.returncode}")
            try:
                with DIRECT.open(f"http://{self.probe}:{self.port}/healthz", timeout=1):
                    return
            except OSError:
                time.sleep(0.1)
        raise RuntimeError("server did not start")

    def stop(self) -> None:
        if self.process.poll() is not None:
            return
        os.killpg(self.process.pid, signal.SIGINT)
        try:
            self.process.wait(15)
        except subprocess.TimeoutExpired:
            os.killpg(self.process.pid, signal.SIGKILL)
            self.process.wait()


def cpu_times(cpus: list[int] | None = None) -> tuple[int, int]:
    """Return (busy, total) jiffies summed over the given CPUs, or all CPUs."""
    busy = total = 0
    with open("/proc/stat") as stat:
        for line in stat:
            name, *fields = line.split()
            if not name.startswith("cpu") or name == "cpu":
                continue
            if cpus is not None and int(name[3:]) not in cpus:
                continue
            values = [int(value) for value in fields[:8]]
            idle = values[3] + values[4]  # idle + iowait
            busy += sum(values) - idle
            total += sum(values)
    return busy, total


def utilisation(before: tuple[int, int], after: tuple[int, int]) -> float:
    """Fraction of the measured CPUs that were busy between two readings."""
    total = after[1] - before[1]
    return (after[0] - before[0]) / total if total else 0.0


def host_info() -> dict:
    model = ""
    try:
        with open("/proc/cpuinfo") as info:
            for line in info:
                if line.startswith("model name"):
                    model = line.split(":", 1)[1].strip()
                    break
    except OSError:
        pass
    return {
        "hostname": platform.node(),
        "kernel": platform.release(),
        "cpu_model": model,
        "cpus": os.cpu_count(),
        "python": sys.version.split()[0],
    }
