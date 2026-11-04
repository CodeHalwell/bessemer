"""Container qualification only; mount read-only rather than shipping this app."""

import asyncio
import importlib.metadata
import json
import os
import subprocess
import sys
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from bessemer import App


def event(name: str, **values) -> None:
    # Controlled qualification events remain below PIPE_BUF and use one write.
    os.write(
        1, (json.dumps({"event": name, "pid": os.getpid(), **values}) + "\n").encode()
    )


@asynccontextmanager
async def lifespan(app):
    app.state.qualification_children = []
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
        "init_comm": read("/proc/1/comm"),
        "supervisor_ppid": int(
            (read(f"/proc/{os.getppid()}/stat") or "").rsplit(")", 1)[1].split()[1]
        ),
        "worker_argv": "_worker" in (read("/proc/self/cmdline") or "").split("\0"),
        "supervisor_argv": "run"
        in (read(f"/proc/{os.getppid()}/cmdline") or "").split("\0")
        and "_worker" not in (read(f"/proc/{os.getppid()}/cmdline") or "").split("\0"),
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


@app.post("/__qualification/spawn-child")
async def spawn_child(enabled: bool = False):
    if not enabled or os.environ.get("BESSEMER_QUALIFICATION_FAULTS") != "1":
        return {"enabled": False}
    marker = Path("/tmp", f"qualification-child-{uuid.uuid4().hex}")
    code = "import signal,sys,time;from pathlib import Path;signal.signal(signal.SIGTERM,signal.SIG_IGN);Path(sys.argv[1]).touch();time.sleep(120)"
    # Keep the default worker process group; root's supervisor will kill it
    # after the worker crashes, and the image's init must adopt and reap it.
    child = await asyncio.to_thread(
        subprocess.Popen, [sys.executable, "-c", code, str(marker)]
    )
    app.state.qualification_children.append(child)
    for _ in range(200):
        if marker.exists():
            break
        await asyncio.sleep(0.01)
    else:
        raise RuntimeError("qualification child did not become ready")
    fields = Path(f"/proc/{child.pid}/stat").read_text().rsplit(")", 1)[1].split()
    event("grandchild_started", child_pid=child.pid)
    return {
        "worker_pid": os.getpid(),
        "child_pid": child.pid,
        "child_ppid": int(fields[1]),
        "child_start_ticks": int(fields[19]),
    }


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
