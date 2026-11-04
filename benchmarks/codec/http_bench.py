"""Exploratory same-host HTTP benchmark comparing installed Bessemer builds.

Each build lives in its own virtual environment. The server is pinned to one
set of cores and oha to another, runs alternate between builds, and the median
of the runs is reported with its spread. Same-host results are exploratory and
do not establish formal performance acceptance.

    python benchmarks/codec/http_bench.py --build before=/venv-a --build after=/venv-b
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import statistics
import subprocess
import tempfile
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
ECHO_BODY = json.dumps(
    [{"id": i, "name": f"item-{i}", "score": i * 1.5} for i in range(20)]
).encode()
SCENARIOS = {
    "json": ("GET", "/json", None),
    "items": ("GET", "/items", None),
    "echo": ("POST", "/echo", ECHO_BODY),
}


def wait_ready(url: str, timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1) as response:
                if response.status == 200:
                    return
        except OSError:
            time.sleep(0.1)
    raise RuntimeError(f"server did not become ready at {url}")


def oha(args, url: str, method: str, body: bytes | None, seconds: float) -> float:
    command = [
        "taskset",
        "-c",
        args.load_cpus,
        args.oha,
        "--no-tui",
        "--output-format",
        "json",
        "-z",
        f"{seconds}s",
        "-c",
        str(args.connections),
        "-m",
        method,
        url,
    ]
    body_path = None
    if body is not None:
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as handle:
            handle.write(body)
            body_path = handle.name
        command[-1:-1] = ["-H", "content-type: application/json", "-D", body_path]
    try:
        output = subprocess.run(
            command, check=True, capture_output=True, text=True
        ).stdout
    finally:
        if body_path is not None:
            os.unlink(body_path)
    result = json.loads(output)
    statuses = result.get("statusCodeDistribution", {})
    if set(statuses) != {"200"}:
        raise RuntimeError(f"non-200 responses: {statuses}")
    return result["summary"]["requestsPerSec"]


def serve(venv: Path, args, port: int) -> subprocess.Popen:
    env = dict(os.environ, PYTHONPATH=str(HERE))
    return subprocess.Popen(
        [
            "taskset",
            "-c",
            args.server_cpus,
            str(venv / "bin" / "bessemer"),
            "run",
            "app:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--workers",
            "1",
        ],
        env=env,
        cwd=HERE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--build", action="append", required=True, help="name=venv")
    parser.add_argument("--scenario", action="append", choices=sorted(SCENARIOS))
    parser.add_argument("--runs", type=int, default=5)
    parser.add_argument("--warmup", type=float, default=5)
    parser.add_argument("--duration", type=float, default=10)
    parser.add_argument("--connections", type=int, default=64)
    parser.add_argument("--server-cpus", default="0,1")
    parser.add_argument("--load-cpus", default="2,3")
    parser.add_argument("--oha", default=shutil.which("oha") or "oha")
    parser.add_argument("--output")
    args = parser.parse_args()
    builds = dict(item.split("=", 1) for item in args.build)
    scenarios = args.scenario or list(SCENARIOS)
    samples = {name: {s: [] for s in scenarios} for name in builds}
    for run in range(args.runs):
        for index, (name, venv) in enumerate(builds.items()):
            port = 18000 + index
            server = serve(Path(venv), args, port)
            try:
                wait_ready(f"http://127.0.0.1:{port}/json")
                for scenario in scenarios:
                    method, path, body = SCENARIOS[scenario]
                    url = f"http://127.0.0.1:{port}{path}"
                    if run == 0:
                        oha(args, url, method, body, args.warmup)
                    rate = oha(args, url, method, body, args.duration)
                    samples[name][scenario].append(rate)
                    print(
                        f"run {run + 1} {name:>8} {scenario:>6} {rate:12,.0f} req/s",
                        flush=True,
                    )
            finally:
                os.killpg(server.pid, signal.SIGTERM)
                server.wait(timeout=30)
    summary = {}
    for name, rows in samples.items():
        summary[name] = {}
        for scenario, values in rows.items():
            median = statistics.median(values)
            spread = (max(values) - min(values)) / median * 100
            summary[name][scenario] = {
                "median_rps": round(median),
                "spread_percent": round(spread, 2),
                "samples": [round(v) for v in values],
            }
    print(json.dumps(summary, indent=2))
    if args.output:
        Path(args.output).write_text(json.dumps(summary, indent=2) + "\n")


if __name__ == "__main__":
    main()
