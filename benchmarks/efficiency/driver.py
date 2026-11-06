"""Identical capped Linux containers; the fixed-rate client stays on the host."""

import json
import socket
import subprocess
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path

DIRECT = urllib.request.build_opener(urllib.request.ProxyHandler({}))
HERE = Path(__file__).resolve().parent


def free_port():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


@dataclass(frozen=True)
class Variant:
    name: str
    environment: str
    framework: str = "bessemer"
    event_loop: str = "asyncio"


VARIANTS = {
    "baseline": Variant("baseline", "baseline"),
    "candidate": Variant("candidate", "candidate"),
    "candidate-rust": Variant("candidate-rust", "candidate", event_loop="bessemer"),
    "fastapi": Variant("fastapi", "fastapi", "fastapi"),
}


def canonical_cpuset(value):
    cpus = set()
    for entry in value.split(","):
        ends = list(map(int, entry.split("-")))
        cpus.update(range(ends[0], ends[-1] + 1))
    ranges = []
    for cpu in sorted(cpus):
        if ranges and cpu == ranges[-1][-1] + 1:
            ranges[-1].append(cpu)
        else:
            ranges.append([cpu])
    return ",".join(
        str(run[0]) if len(run) == 1 else f"{run[0]}-{run[-1]}" for run in ranges
    )


class Server:
    def __init__(self, args, variant, log):
        self.args, self.variant, self.log = args, variant, log
        self.name = "bessemer-efficiency-" + uuid.uuid4().hex[:12]
        self.port, self.control_port = free_port(), free_port()
        self.url = f"http://127.0.0.1:{self.port}"
        self.control_url = f"http://127.0.0.1:{self.control_port}"
        self.cleanup = None

    def __enter__(self):
        args, variant = self.args, self.variant
        command = [
            "docker",
            "run",
            "--detach",
            "--pull=never",
            "--name",
            self.name,
            "--init",
            "--cpus",
            str(args.cpus),
            "--cpuset-cpus",
            args.cpuset,
            "--memory",
            f"{args.memory_mb}m",
            "--pids-limit",
            str(args.pids_limit),
            "--publish",
            f"127.0.0.1:{self.port}:8000",
            "--publish",
            f"127.0.0.1:{self.control_port}:8001",
            "--mount",
            f"type=bind,source={HERE.parent},target=/bench,readonly",
            "--mount",
            f"type=bind,source={args.venvs},target=/venvs,readonly",
            args.image,
            "/usr/local/bin/python",
            "/bench/efficiency/agent.py",
            "--python",
            f"/venvs/{variant.environment}/bin/python",
            "--framework",
            variant.framework,
            "--event-loop",
            variant.event_loop,
            "--workers",
            str(args.workers),
            "--require-limits",
            "--cpus",
            str(args.cpus),
            "--memory-mb",
            str(args.memory_mb),
            "--pids-limit",
            str(args.pids_limit),
            "--cpuset",
            canonical_cpuset(args.cpuset),
        ]
        self.command = command
        subprocess.check_output(command, text=True)
        try:
            self.wait_ready()
            self.inspect = json.loads(
                subprocess.check_output(["docker", "inspect", self.name], text=True)
            )[0]
            self.metadata = {
                "variant": asdict(variant),
                "container_command": command,
                "image_id": self.inspect["Image"],
                "host_config": self.inspect["HostConfig"],
                "agent": self.snapshot(),
                "application": self.get(self.url + "/probe"),
            }
            if not self.metadata["agent"]["limits"]["enforced"]:
                raise RuntimeError("container did not confirm resource enforcement")
            return self
        except BaseException:
            self.__exit__(None, None, None)
            raise

    @staticmethod
    def get(url):
        with DIRECT.open(url, timeout=2) as response:
            return json.load(response)

    def snapshot(self):
        return self.get(self.control_url + "/snapshot")

    def control(self, path, duration=None):
        headers = {"X-Duration": str(duration)} if duration is not None else {}
        request = urllib.request.Request(
            self.control_url + path, method="POST", headers=headers
        )
        with DIRECT.open(request, timeout=20) as response:
            return json.load(response)

    def wait_ready(self):
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            try:
                state = self.snapshot()
                if state["exit_code"] is not None:
                    raise RuntimeError(f"server exited: {state}")
                with DIRECT.open(self.url + "/healthz", timeout=1) as response:
                    if response.status == 200:
                        return
            except (OSError, urllib.error.URLError):
                time.sleep(0.1)
        raise TimeoutError("server did not become ready; inspect its container log")

    def __exit__(self, *args):
        try:
            self.cleanup = self.control("/stop")
        except (OSError, ValueError) as error:
            self.cleanup = {"error": str(error), "forced_container_removal": True}
        finally:
            logs = subprocess.run(
                ["docker", "logs", self.name],
                capture_output=True,
                text=True,
                check=False,
            )
            self.log.write_text(logs.stdout + logs.stderr)
            try:
                subprocess.run(
                    ["docker", "wait", self.name],
                    capture_output=True,
                    timeout=10,
                    check=False,
                )
            finally:
                subprocess.run(
                    ["docker", "rm", "--force", self.name],
                    capture_output=True,
                    check=False,
                )
