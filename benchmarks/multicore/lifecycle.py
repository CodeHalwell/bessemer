"""Port the concurrency harness's installed-server startup and CPU accounting."""

import json
import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from server import DIRECT, Server

HERE = Path(__file__).resolve().parent


def process_tree(root):
    output = subprocess.check_output(["ps", "-axo", "pid=,ppid="], text=True)
    parents = {
        int(pid): int(parent)
        for pid, parent in (line.split() for line in output.splitlines())
    }
    members = {root}
    while True:
        expanded = members | {
            pid for pid, parent in parents.items() if parent in members
        }
        if expanded == members:
            return members, set(parents)
        members = expanded


@dataclass(frozen=True)
class Topology:
    name: str
    python: str
    cpus: int
    workers: int
    loops: int
    gil: bool
    framework: str = "bessemer"


class InstalledServer(Server):
    def __init__(self, variant, log, args):
        super().__init__(variant, log)
        self.args = args

    def __enter__(self):
        variant = self.variant
        environment = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith(("BESSEMER_", "UVICORN_", "MULTICORE_"))
            and key != "PYTHONPATH"
        }
        environment.update(
            MULTICORE_FRAMEWORK=variant.framework,
            MULTICORE_EXPECT_GIL="1" if variant.gil else "0",
            MULTICORE_ITERATIONS=str(self.args.iterations),
            MULTICORE_POOL_WORKERS=str(max(1, variant.cpus // variant.workers)),
        )
        self.process = subprocess.Popen(
            [
                variant.python,
                str(HERE / "launch.py"),
                "--port",
                str(self.port),
                "--cpus",
                str(variant.cpus),
                "--workers",
                str(variant.workers),
                "--loops",
                str(variant.loops),
                "--event-loop",
                self.args.event_loop,
            ],
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

    def probe(self):
        with DIRECT.open(f"http://127.0.0.1:{self.port}/probe", timeout=10) as response:
            result = json.load(response)
        if result["gil_enabled"] != self.variant.gil:
            raise ValueError("server GIL state disagrees with the topology label")
        if not self.variant.gil and not result["free_threaded_build"]:
            raise ValueError("thread topology requires a real free-threaded build")
        return result

    def __exit__(self, *args):
        tracked, _ = process_tree(self.process.pid)
        super().__exit__(*args)
        deadline = time.monotonic() + 3
        while True:
            _, alive = process_tree(self.process.pid)
            remaining = tracked & alive
            if not remaining or time.monotonic() >= deadline:
                break
            time.sleep(0.02)
        self.cleanup = {
            "tracked_pids": sorted(tracked),
            "remaining_pids": sorted(remaining),
        }
        if remaining:
            raise RuntimeError(
                f"server descendants survived cleanup: {sorted(remaining)}"
            )


def versions(python):
    script = (
        "import hashlib, importlib.metadata as m, json, sys, sysconfig; "
        "from pathlib import Path; import bessemer._native as n; "
        "print(json.dumps({'python':sys.version,'executable':sys.executable,"
        "'gil_enabled':getattr(sys,'_is_gil_enabled',lambda:True)(),"
        "'free_threaded_build':bool(sysconfig.get_config_var('Py_GIL_DISABLED')),"
        "'bessemer':m.version('bessemer'),'native_file':n.__file__,"
        "'native_sha256':hashlib.sha256(Path(n.__file__).read_bytes()).hexdigest()}))"
    )
    return json.loads(subprocess.check_output([python, "-c", script], text=True))
