"""Container qualification only; mount read-only rather than shipping this app."""

import asyncio
import importlib.metadata
import json
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path

from bessemer import App


def event(name: str, **values) -> None:
    print(json.dumps({"event": name, "pid": os.getpid(), **values}), flush=True)


@asynccontextmanager
async def lifespan(app):
    event("lifespan_started")
    yield
    event("lifespan_closed")


app = App(lifespan=lifespan)
app.health("/healthz")


def read(path: str) -> str | None:
    try:
        return Path(path).read_text().strip()
    except OSError:
        return None


def write_probe(directory: str) -> dict:
    path = Path(directory, f"bessemer-qualification-{os.getpid()}")
    try:
        path.write_text("qualification")
        path.unlink()
        return {"writable": True}
    except OSError as error:
        return {"writable": False, "errno": error.errno}


@app.get("/identity")
async def identity():
    status = read("/proc/self/status") or ""
    fields = dict(line.split(":", 1) for line in status.splitlines() if ":" in line)
    return {
        "pid": os.getpid(),
        "ppid": os.getppid(),
        "uid": os.getuid(),
        "gid": os.getgid(),
        "version": importlib.metadata.version("bessemer"),
        "cap_effective": fields.get("CapEff", "").strip(),
        "no_new_privileges": fields.get("NoNewPrivs", "").strip(),
        "seccomp": fields.get("Seccomp", "").strip(),
        "application_directory": write_probe("/app"),
        "temporary_directory": write_probe("/tmp"),
        "cgroup": {
            "cpu_max": read("/sys/fs/cgroup/cpu.max"),
            "memory_max": read("/sys/fs/cgroup/memory.max"),
            "memory_current": read("/sys/fs/cgroup/memory.current"),
            "memory_peak": read("/sys/fs/cgroup/memory.peak"),
            "pids_max": read("/sys/fs/cgroup/pids.max"),
            "v1_cpu_quota": read("/sys/fs/cgroup/cpu/cpu.cfs_quota_us"),
            "v1_cpu_period": read("/sys/fs/cgroup/cpu/cpu.cfs_period_us"),
            "v1_memory_limit": read("/sys/fs/cgroup/memory/memory.limit_in_bytes"),
            "v1_pids_max": read("/sys/fs/cgroup/pids/pids.max"),
        },
    }


@app.post("/__qualification/crash")
async def crash(enabled: bool = False):
    if enabled and os.environ.get("BESSEMER_QUALIFICATION_FAULTS") == "1":
        event("worker_crash")
        os._exit(41)
    return {"enabled": False}


@app.get("/hold")
async def hold(tag: str = "hold", seconds: float = 0.35):
    event("hold_started", tag=tag)
    await asyncio.sleep(min(max(seconds, 0), 2))
    event("hold_completed", tag=tag)
    return {"completed": True, "pid": os.getpid(), "tag": tag}


@app.get("/__qualification/block")
async def block(enabled: bool = False):
    if enabled and os.environ.get("BESSEMER_QUALIFICATION_FAULTS") == "1":
        event("loop_blocked")
        time.sleep(120)  # noqa: ASYNC251 -- Deliberate event-loop fault for hard shutdown qualification.
    return {"blocked": False}
