"""Compare instructions per request between two Bessemer installs.

The CI performance gate. Each install (a Python with bessemer installed, e.g.
one built from the base commit and one from the pull request) serves the
comparison app in ``benchmarks/compare`` under Valgrind's callgrind, and the
worker's user-space instruction count for a fixed number of requests is
divided by that number. Like ``instructions.sh`` it ignores kernel time, but
it does not depend on how busy the machine is, so it can gate a shared CI
runner where req/s moves by several percent between runs.

Requests go over one keep-alive connection from this (uninstrumented) Python,
so each request is its own loop batch and the count repeats to about 0.1%.
``PYTHONHASHSEED`` is fixed so dict and set layouts match between runs.

    python benchmarks/profile/instructions_compare.py \\
        --base BASE_VENV/bin/python --head HEAD_VENV/bin/python \\
        [--routes json,items,echo] [--loops asyncio,bessemer] \\
        [--threshold 2.0] [--json out.json] [--markdown summary.md]

Exits 1 if any route's count rises by more than ``--threshold`` percent.
"""

from __future__ import annotations

import argparse
import http.client
import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
APP_DIR = HERE.parent / "compare"
ECHO = json.dumps(
    {
        "items": [
            {"id": i, "name": f"item-{i}", "price": i * 1.25, "tags": ["a", "b"]}
            for i in range(20)
        ]
    }
).encode()
# name: (method, path, body). Same workloads as compare/run_compare.py.
ROUTES = {
    "json": ("GET", "/json", None),
    "items": ("GET", "/items/7?q=abc", None),
    "echo": ("POST", "/echo", ECHO),
}


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def wait_ready(port: int, server: subprocess.Popen, deadline: float = 300) -> None:
    end = time.monotonic() + deadline
    while time.monotonic() < end:
        if server.poll() is not None:
            raise RuntimeError(
                f"server exited with {server.returncode} before it was ready"
            )
        try:
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            conn.request("GET", "/healthz")
            if conn.getresponse().status == 200:
                conn.close()
                return
        except OSError:
            pass
        time.sleep(0.5)
    raise RuntimeError("server did not become ready")


def drive(port: int, route: str, count: int) -> None:
    method, path, body = ROUTES[route]
    headers = {"content-type": "application/json"} if body is not None else {}
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=120)
    try:
        for _ in range(count):
            conn.request(method, path, body=body, headers=headers)
            response = conn.getresponse()
            response.read()
            if response.status != 200:
                raise RuntimeError(f"{method} {path} returned {response.status}")
    finally:
        conn.close()


def callgrind_control(*args: str) -> None:
    subprocess.run(
        ["callgrind_control", *args], check=False, capture_output=True, timeout=60
    )


def measure(python: str, route: str, loop: str, requests: int, warmup: int) -> int:
    """Instructions per request for one route on one install."""
    port = free_port()
    with (
        tempfile.TemporaryDirectory(prefix="callgrind-") as out,
        open(Path(out) / "server.log", "wb") as log_stream,
    ):
        env = os.environ | {
            "PYTHONHASHSEED": "0",
            "BESSEMER_EVENT_LOOP": loop,
        }
        server = subprocess.Popen(
            [
                "valgrind",
                "--tool=callgrind",
                "--trace-children=yes",
                f"--callgrind-out-file={out}/callgrind.out.%p",
                python,
                "-m",
                "bessemer",
                "run",
                "bessemer_app:app",
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
                "--workers",
                "1",
            ],
            cwd=APP_DIR,
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=log_stream,
            start_new_session=True,
        )
        try:
            wait_ready(port, server)
            drive(port, route, warmup)
            # Zero the counters, serve exactly `requests` requests, and dump.
            callgrind_control("-z")
            drive(port, route, requests)
            callgrind_control("-d")
            time.sleep(1)
        except Exception:
            log = (Path(out) / "server.log").read_text(errors="replace")
            print(log[-4000:], file=sys.stderr)
            raise
        finally:
            os.killpg(server.pid, signal.SIGTERM)
            try:
                server.wait(60)
            except subprocess.TimeoutExpired:
                os.killpg(server.pid, signal.SIGKILL)
                server.wait()
        # The worker's dump of the measured window is the largest numbered dump.
        best = 0
        for dump in Path(out).glob("callgrind.out.*.*"):
            with dump.open() as stream:
                for line in stream:
                    if line.startswith("summary:"):
                        best = max(best, int(line.split()[1]))
                        break
        if not best:
            raise RuntimeError("callgrind wrote no dump for the measured window")
        return best // requests


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--base", required=True, help="Python with the base install")
    parser.add_argument(
        "--head", required=True, help="Python with the candidate install"
    )
    parser.add_argument("--routes", default=",".join(ROUTES))
    parser.add_argument("--loops", default="asyncio,bessemer")
    parser.add_argument("--requests", type=int, default=2000)
    parser.add_argument("--warmup", type=int, default=500)
    parser.add_argument(
        "--threshold", type=float, default=2.0, help="percent rise that fails"
    )
    parser.add_argument("--json", help="write results here")
    parser.add_argument("--markdown", help="append a Markdown table here")
    parser.add_argument("--title", default="Instructions per request")
    args = parser.parse_args()

    rows = []
    failed = False
    for loop in args.loops.split(","):
        for route in args.routes.split(","):
            base = measure(args.base, route, loop, args.requests, args.warmup)
            head = measure(args.head, route, loop, args.requests, args.warmup)
            change = (head - base) / base * 100
            regressed = change > args.threshold
            failed |= regressed
            row = {
                "loop": loop,
                "route": route,
                "base": base,
                "head": head,
                "change": change,
                "regressed": regressed,
            }
            rows.append(row)
            print(json.dumps(row), flush=True)

    if args.json:
        Path(args.json).write_text(json.dumps(rows, indent=2) + "\n")
    lines = [
        f"### {args.title}",
        "",
        f"Callgrind, one connection, {args.requests} requests; fails above +{args.threshold:g}%.",
        "",
        "| Loop | Route | Base | Head | Change | |",
        "| --- | --- | ---: | ---: | ---: | --- |",
    ]
    for row in rows:
        mark = (
            "regression"
            if row["regressed"]
            else ("faster" if row["change"] < -0.5 else "ok")
        )
        lines.append(
            f"| {row['loop']} | `{ROUTES[row['route']][1]}` | {row['base']:,} | {row['head']:,} "
            f"| {row['change']:+.2f}% | {mark} |"
        )
    table = "\n".join(lines) + "\n"
    print(table)
    if args.markdown:
        with open(args.markdown, "a") as stream:
            stream.write(table + "\n")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
