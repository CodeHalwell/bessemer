"""Local server lifecycle and process-tree CPU accounting."""

import json
import os
import signal
import socket
import subprocess
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path

HERE = Path(__file__).resolve().parent
DIRECT = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def resolve_executable(value):
    return os.path.abspath(value) if os.sep in value else value


def cpu_seconds(value):
    days, separator, clock = value.partition("-")
    seconds = 0.0
    for part in (clock if separator else value).split(":"):
        seconds = seconds * 60 + float(part)
    return seconds + (int(days) * 86400 if separator else 0)


def tree_cpu(root):
    output = subprocess.check_output(["ps", "-axo", "pid=,ppid=,time="], text=True)
    rows = {
        int(pid): (int(parent), cpu_seconds(cpu))
        for pid, parent, cpu in (line.split() for line in output.splitlines())
    }
    members = {root}
    while next_members := members | {
        pid for pid, (parent, _) in rows.items() if parent in members
    }:
        if next_members == members:
            return sum(rows[pid][1] for pid in members if pid in rows)
        members = next_members


@dataclass(frozen=True)
class Variant:
    name: str
    python: str
    framework: str = "bessemer"
    loop: str = "asyncio"

    def versions(self):
        script = (
            "import importlib.metadata as m, json, sys, hashlib; "
            "from pathlib import Path; import bessemer._native as n; "
            "print(json.dumps({'python':sys.version,'native_file':n.__file__,"
            "'native_sha256':hashlib.sha256(Path(n.__file__).read_bytes()).hexdigest(),"
            "'packages':"
            "{p:m.version(p) for p in ['bessemer','pydantic','fastapi','uvicorn']}}))"
        )
        return json.loads(
            subprocess.check_output([self.python, "-c", script], text=True)
        )


class Server:
    def __init__(self, variant, log):
        self.variant, self.log = variant, log
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            self.port = listener.getsockname()[1]

    def __enter__(self):
        variant = self.variant
        environment = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith(("BESSEMER_", "UVICORN_")) and key != "PYTHONPATH"
        }
        environment["BENCHMARK_FRAMEWORK"] = variant.framework
        if variant.framework == "fastapi":
            arguments = [
                "uvicorn",
                "app:app",
                "--no-access-log",
                "--log-level",
                "warning",
            ]
        else:
            environment["BESSEMER_EVENT_LOOP"] = variant.loop
            arguments = [
                "bessemer",
                "run",
                "app:app",
                "--max-in-flight",
                "16384",
                "--max-connections",
                "16384",
            ]
        command = [
            variant.python,
            "-m",
            *arguments,
            "--host",
            "127.0.0.1",
            "--port",
            str(self.port),
            "--workers",
            "1",
        ]
        self.process = subprocess.Popen(
            command,
            cwd=HERE,
            env=environment,
            stdout=self.log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        try:
            self.wait_ready()
        except BaseException:
            self.__exit__(None, None, None)
            raise
        return self

    def wait_ready(self):
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                raise RuntimeError(f"{self.variant.name} exited; inspect server log")
            try:
                with DIRECT.open(
                    f"http://127.0.0.1:{self.port}/healthz", timeout=1
                ) as response:
                    if response.status == 200:
                        return
            except OSError:
                time.sleep(0.05)
        raise TimeoutError("server readiness")

    def __exit__(self, *args):
        if self.process.poll() is None:
            os.killpg(self.process.pid, signal.SIGTERM)
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                os.killpg(self.process.pid, signal.SIGKILL)
                self.process.wait(timeout=5)
