"""Run the Bessemer/FastAPI comparison at a fixed rate on macOS."""

from __future__ import annotations

import argparse
import json
import os
import platform
import resource
import signal
import subprocess
import time
from pathlib import Path
from urllib.request import Request

from run_compare import ECHO, ROUTES
from servers import BESSEMER_VARIANTS, DIRECT, command, free_port, resolve_executable

HERE = Path(__file__).resolve().parent


def cpu_seconds(text: str) -> float:
    days, sep, remainder = text.partition("-")
    if not sep:
        remainder, days = days, "0"
    seconds = 0.0
    for item in remainder.split(":"):
        seconds = seconds * 60 + float(item)
    return int(days) * 86400 + seconds


def process_tree_cpu(root: int) -> float | None:
    result = subprocess.run(
        ["ps", "-axo", "pid=,ppid=,time="], capture_output=True, text=True, check=True
    )
    rows = {}
    for line in result.stdout.splitlines():
        try:
            pid, parent, cpu = line.split()
            rows[int(pid)] = (int(parent), cpu_seconds(cpu))
        except ValueError:
            continue
    if root not in rows:
        return None
    descendants = {root}
    while True:
        expanded = descendants | {
            pid for pid, (parent, _) in rows.items() if parent in descendants
        }
        if expanded == descendants:
            break
        descendants = expanded
    return sum(rows[pid][1] for pid in descendants if pid in rows)


def environment(framework: str) -> dict[str, str]:
    environ = {
        name: value
        for name, value in os.environ.items()
        if not name.startswith(("BESSEMER_", "UVICORN_"))
    }
    environ.pop("PYTHONPATH", None)
    environ.update(BESSEMER_VARIANTS.get(framework, {}))
    return environ


def ready(process: subprocess.Popen, port: int) -> None:
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"server exited with status {process.returncode}")
        try:
            with DIRECT.open(f"http://127.0.0.1:{port}/healthz", timeout=1) as response:
                if response.status == 200:
                    return
        except OSError:
            time.sleep(0.1)
    raise RuntimeError("server did not become ready")


def validate(port: int) -> None:
    expected = {
        "json": {"ok": True, "count": 42},
        "items": {"id": 7, "q": "abc"},
        "echo": json.loads(ECHO),
        "io": {"ok": True},
    }
    for name, (method, path, body) in ROUTES.items():
        headers = {"Content-Type": "application/json"} if body is not None else {}
        request = Request(
            f"http://127.0.0.1:{port}{path}", data=body, headers=headers, method=method
        )
        with DIRECT.open(request, timeout=5) as response:
            actual = json.load(response)
        if actual != expected[name]:
            raise RuntimeError(f"unexpected /{name} response: {actual!r}")


def run_oha(args, port: int, route: str, duration: int) -> dict:
    method, path, body = ROUTES[route]
    cmd = [
        args.oha,
        "--worker-threads",
        str(args.client_threads),
        "-q",
        str(args.qps),
        "--latency-correction",
        "-z",
        f"{duration}s",
        "-c",
        str(args.connections),
        "--no-tui",
        "--wait-ongoing-requests-after-deadline",
        "--output-format",
        "json",
        "-m",
        method,
    ]
    if body is not None:
        cmd += ["-H", "content-type: application/json", "-d", body.decode()]
    cmd.append(f"http://127.0.0.1:{port}{path}")
    return json.loads(
        subprocess.run(
            cmd, check=True, capture_output=True, text=True, timeout=duration + 20
        ).stdout
    )


def child_cpu() -> float:
    usage = resource.getrusage(resource.RUSAGE_CHILDREN)
    return usage.ru_utime + usage.ru_stime


def sample(args, process: subprocess.Popen, port: int, route: str) -> dict:
    run_oha(args, port, route, args.warmup)
    server_before = process_tree_cpu(process.pid)
    client_before = child_cpu()
    started = time.monotonic()
    result = run_oha(args, port, route, args.duration)
    elapsed = time.monotonic() - started
    client_seconds = child_cpu() - client_before
    server_after = process_tree_cpu(process.pid)
    server_seconds = (
        None
        if server_before is None or server_after is None
        else server_after - server_before
    )
    codes = result.get("statusCodeDistribution", {})
    latency = result.get("latencyPercentiles", {})
    return {
        "route": route,
        "rps": result["summary"]["requestsPerSec"],
        "p50_ms": latency.get("p50", 0) * 1000,
        "p99_ms": latency.get("p99", 0) * 1000,
        "non_200": sum(count for code, count in codes.items() if code != "200"),
        "errors": sum(result.get("errorDistribution", {}).values()),
        "status_codes": codes,
        "error_distribution": result.get("errorDistribution", {}),
        "elapsed_seconds": elapsed,
        "client_cpu_cores": client_seconds / elapsed,
        "server_cpu_cores": None
        if server_seconds is None
        else server_seconds / elapsed,
        "oha": result,
    }


def versions(python: str) -> dict:
    script = """import importlib.metadata as m, json, sys, bessemer, bessemer._native
names = ['bessemer', 'fastapi', 'uvicorn', 'pydantic', 'starlette', 'uvloop', 'httptools']
def version(name):
    try: return m.version(name)
    except m.PackageNotFoundError: return None
print(json.dumps({'python': sys.version, 'executable': sys.executable, 'bessemer_file': bessemer.__file__, 'native_file': bessemer._native.__file__, 'versions': {n: version(n) for n in names}}))"""
    return json.loads(
        subprocess.run(
            [python, "-c", script],
            check=True,
            capture_output=True,
            text=True,
            env=environment("fastapi"),
            cwd=HERE,
        ).stdout
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--python", required=True)
    parser.add_argument("--oha", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--qps", type=int, required=True)
    parser.add_argument("--rounds", type=int, default=3)
    parser.add_argument("--duration", type=int, default=5)
    parser.add_argument("--warmup", type=int, default=1)
    parser.add_argument("--connections", type=int, default=64)
    parser.add_argument("--client-threads", type=int, default=2)
    parser.add_argument("--frameworks", default="bessemer,fastapi")
    parser.add_argument("--routes", default=",".join(ROUTES))
    args = parser.parse_args()
    if (
        min(
            args.rounds,
            args.duration,
            args.warmup,
            args.connections,
            args.client_threads,
            args.qps,
        )
        < 1
    ):
        parser.error(
            "rounds, duration, warmup, connections, client threads and qps must be positive"
        )
    args.python, args.oha = map(resolve_executable, (args.python, args.oha))
    frameworks = args.frameworks.split(",")
    routes = args.routes.split(",")
    if any(name not in (*BESSEMER_VARIANTS, "fastapi") for name in frameworks) or any(
        name not in ROUTES for name in routes
    ):
        parser.error("unknown framework or route")
    out = Path(args.out).resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    meta = {
        "mode": "macOS same host, unpinned, one worker per server, fixed rate",
        "started": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "platform": platform.platform(),
        "machine": platform.machine(),
        "cpu_count": os.cpu_count(),
        "oha": subprocess.run(
            [args.oha, "--version"], check=True, capture_output=True, text=True
        ).stdout.strip(),
        "environment": versions(args.python),
        "settings": {
            key: getattr(args, key)
            for key in (
                "rounds",
                "duration",
                "warmup",
                "connections",
                "client_threads",
                "qps",
                "frameworks",
                "routes",
            )
        },
        "cpu_note": "Estimated cores: ps process-tree CPU time delta for server, getrusage child CPU delta for oha; unpinned and sampled over wall time.",
        "oha_deadline": "Waits for in-flight requests after the timed interval; the reference runner aborts them at deadline.",
        "latency_correction": "oha --latency-correction is enabled with -q.",
    }
    print(json.dumps({"meta": meta}), flush=True)
    results = []
    for round_index in range(args.rounds):
        offset = round_index % len(frameworks)
        order = frameworks[offset:] + frameworks[:offset]
        for framework in order:
            port = free_port("127.0.0.1")
            log_path = out.parent / f"server-{round_index + 1}-{framework}.log"
            with log_path.open("w") as log:
                process = subprocess.Popen(
                    command(framework, args.python, "/bin/false", "127.0.0.1", port, 1),
                    cwd=HERE,
                    env=environment(framework),
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
                try:
                    ready(process, port)
                    validate(port)
                    for route in routes:
                        record = sample(args, process, port, route)
                        record.update(
                            {
                                "config": "1-worker",
                                "round": round_index,
                                "framework": framework,
                            }
                        )
                        results.append(record)
                        print(
                            json.dumps(
                                {
                                    key: value
                                    for key, value in record.items()
                                    if key != "oha"
                                }
                            ),
                            flush=True,
                        )
                        out.write_text(
                            json.dumps({"meta": meta, "samples": results}, indent=2)
                            + "\n"
                        )
                finally:
                    if process.poll() is None:
                        os.killpg(process.pid, signal.SIGINT)
                        try:
                            process.wait(timeout=15)
                        except subprocess.TimeoutExpired:
                            os.killpg(process.pid, signal.SIGKILL)
                            process.wait()


if __name__ == "__main__":
    main()
