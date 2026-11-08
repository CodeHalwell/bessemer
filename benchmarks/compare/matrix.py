"""Reproducible feature and concurrency comparisons; shell entry points live here."""

from __future__ import annotations

import argparse
import json
import math
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

from servers import DIRECT, cpu_list, free_port, resolve_executable

HERE = Path(__file__).resolve().parent
FEATURE_ROUTES = (
    "GET:json GET:sync GET:dep GET:yield_dep GET:header GET:cookie "
    "GET:query?a=2&d=7 GET:models GET:plain GET:request?q=1 GET:raise "
    "GET:bg GET:big GET:io POSTF:form POSTJ:dict POSTJ:item"
)


def request_args(spec):
    method, separator, route = spec.partition(":")
    if not separator:
        method, route = "GET", spec.lstrip("/")
    args = []
    if method == "POSTF":
        args = [
            "-m",
            "POST",
            "-d",
            "name=a&age=3",
            "-T",
            "application/x-www-form-urlencoded",
        ]
    elif method == "POSTJ":
        body = (
            {"a": 1, "b": [1, 2, 3]}
            if route == "dict"
            else {"id": 1, "name": "x", "price": 2}
        )
        args = ["-m", "POST", "-d", json.dumps(body), "-T", "application/json"]
    elif method != "GET":
        raise ValueError(f"unknown route method: {method}")
    if not route or any(char.isspace() for char in route):
        raise ValueError(f"invalid route: {route!r}")
    return route, args


def sample(data, *, framework, workers, route, connections, middleware, mode):
    """Keep successful throughput separate from fast errors and failed connects."""
    summary = data["summary"]
    codes = {
        str(key): int(value)
        for key, value in data.get("statusCodeDistribution", {}).items()
    }
    latencies = data.get("latencyPercentiles", {})
    rps = summary["requestsPerSec"]
    # oha's summary.total is elapsed seconds, not a request count. Its
    # requestsPerSec includes transport failures and deadline-aborted requests.
    errors = sum(data.get("errorDistribution", {}).values())
    elapsed = summary["total"]
    if elapsed <= 0:
        raise ValueError("oha reported a nonpositive sample duration")
    return {
        "mode": mode,
        "fw": framework,
        "workers": workers,
        "route": route,
        "conns": connections,
        "mw": middleware,
        "rps": round(rps, 2),
        "ok_rps": round(codes.get("200", 0) / elapsed, 2),
        "p50_ms": round(latencies["p50"] * 1000, 3)
        if latencies.get("p50") is not None
        else None,
        "p99_ms": round(latencies["p99"] * 1000, 3)
        if latencies.get("p99") is not None
        else None,
        "non200": sum(value for code, value in codes.items() if code != "200"),
        "errors": errors,
        "status_codes": codes,
        "error_distribution": data.get("errorDistribution", {}),
    }


def command(framework, workers, args):
    if framework == "axum":
        return [args.axum, str(args.port), str(workers)]
    python = args.bessemer_python if framework == "bessemer" else args.fastapi_python
    module = (
        "feature_matrix_app:app" if args.mode == "feature" else f"{framework}_app:app"
    )
    if framework == "bessemer":
        return [
            python,
            "-m",
            "bessemer",
            "run",
            module,
            "--port",
            str(args.port),
            "--workers",
            str(workers),
        ]
    return [
        python,
        "-m",
        "uvicorn",
        module,
        "--port",
        str(args.port),
        "--workers",
        str(workers),
        "--log-level",
        "warning",
        "--no-access-log",
    ]


def stop(process):
    # Signal the whole group even if its leader already exited: uvicorn and
    # supervised Bessemer can have surviving children after a startup failure.
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=15)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        process.wait()
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def wait_ready(process, port, timeout):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if process.poll() is not None:
            raise RuntimeError(f"server exited with {process.returncode}")
        try:
            with DIRECT.open(f"http://127.0.0.1:{port}/json", timeout=1):
                return
        except OSError:
            time.sleep(0.1)
    raise RuntimeError("server readiness timed out")


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("mode", choices=("feature", "concurrency"))
    result.add_argument(
        "--out", type=Path, required=True, help="append JSON lines here"
    )
    result.add_argument(
        "--frameworks", nargs="+", choices=("bessemer", "fastapi", "axum")
    )
    result.add_argument("--workers", nargs="+", type=int)
    result.add_argument("--connections", nargs="+", type=int)
    result.add_argument(
        "--routes", nargs="+", help="GET:path, POSTJ:path or POSTF:path"
    )
    result.add_argument("--python", default=os.environ.get("PYTHON", sys.executable))
    result.add_argument("--bessemer-python", default=os.environ.get("BPY"))
    result.add_argument("--fastapi-python", default=os.environ.get("FPY"))
    result.add_argument(
        "--axum",
        default=os.environ.get(
            "AXUM", str(HERE / "axum_app/target/release/axum-compare")
        ),
    )
    result.add_argument(
        "--oha", default=os.environ.get("OHA", shutil.which("oha") or "oha")
    )
    result.add_argument(
        "--server-cpus",
        default=os.environ.get("SERVER_CPUS"),
        help="CPU pool; first N CPUs used for N workers",
    )
    result.add_argument("--client-cpus", default=os.environ.get("CLIENT_CPUS"))
    result.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("PORT", "0")),
        help="0 chooses a free port",
    )
    result.add_argument(
        "--duration", type=float, default=float(os.environ.get("DUR", "5"))
    )
    result.add_argument("--warmup", type=float, default=1)
    result.add_argument("--startup-timeout", type=float, default=30)
    result.add_argument(
        "--middleware", action="store_true", default=os.environ.get("MW") == "1"
    )
    return result


def configure(args):
    args.frameworks = args.frameworks or (
        ["bessemer", "fastapi"]
        if args.mode == "feature"
        else ["bessemer", "fastapi", "axum"]
    )
    args.workers = args.workers or ([1] if args.mode == "feature" else [1, 2, 4])
    args.connections = args.connections or (
        [64] if args.mode == "feature" else [64, 256, 1024]
    )
    args.routes = args.routes or (
        FEATURE_ROUTES.split() if args.mode == "feature" else ["json", "io"]
    )
    if args.mode == "feature" and ("axum" in args.frameworks or args.workers != [1]):
        raise ValueError("feature mode supports Bessemer/FastAPI with one server core")
    if args.middleware and args.mode != "feature":
        raise ValueError("middleware applies only to the feature app")
    if (
        min(*args.workers, *args.connections) < 1
        or args.duration <= 0
        or args.warmup < 0
        or args.startup_timeout <= 0
    ):
        raise ValueError(
            "workers, connections, duration and startup timeout must be positive; warmup may be zero"
        )
    if not all(
        math.isfinite(value)
        for value in (args.duration, args.warmup, args.startup_timeout)
    ):
        raise ValueError("durations must be finite")
    if not 0 <= args.port <= 65535:
        raise ValueError("port must be between 0 and 65535")
    available = sorted(os.sched_getaffinity(0))
    count = max(args.workers)
    args.server_cpus = (
        cpu_list(args.server_cpus) if args.server_cpus else available[:count]
    )
    args.client_cpus = (
        cpu_list(args.client_cpus)
        if args.client_cpus
        else [cpu for cpu in available if cpu not in args.server_cpus]
    )
    if (
        len(args.server_cpus) < count
        or not args.client_cpus
        or set(args.server_cpus) & set(args.client_cpus)
    ):
        raise ValueError(
            "provide disjoint --server-cpus (at least one per worker) and nonempty --client-cpus"
        )
    if len(set(args.server_cpus)) != len(args.server_cpus) or len(
        set(args.client_cpus)
    ) != len(args.client_cpus):
        raise ValueError("CPU lists must not contain duplicates")
    if min(*args.server_cpus, *args.client_cpus) < 0:
        raise ValueError("CPU IDs must be nonnegative")
    for spec in args.routes:
        request_args(spec)
    for field in ("python", "axum", "oha"):
        setattr(args, field, resolve_executable(getattr(args, field)))
    args.bessemer_python = resolve_executable(args.bessemer_python or args.python)
    args.fastapi_python = resolve_executable(args.fastapi_python or args.python)
    args.port = args.port or free_port("127.0.0.1")
    return args


def run(args):
    args.out.parent.mkdir(parents=True, exist_ok=True)
    client = ["taskset", "-c", ",".join(map(str, args.client_cpus)), args.oha]
    with args.out.open("a") as output:
        for framework in args.frameworks:
            for workers in args.workers:
                log_path = args.out.with_suffix(f".{framework}-{workers}.log")
                # Refuse an occupied explicit port instead of probing and
                # accidentally benchmarking somebody else's server.
                with socket.socket() as probe:
                    probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                    probe.bind(("127.0.0.1", args.port))
                with log_path.open("w") as log:
                    process = subprocess.Popen(
                        [
                            "taskset",
                            "-c",
                            ",".join(map(str, args.server_cpus[:workers])),
                            *command(framework, workers, args),
                        ],
                        cwd=HERE,
                        env=os.environ
                        | {"FW": framework, "MW": str(int(args.middleware))},
                        stdout=log,
                        stderr=log,
                        start_new_session=True,
                    )
                    try:
                        wait_ready(process, args.port, args.startup_timeout)
                        for spec in args.routes:
                            route, request = request_args(spec)
                            for connections in args.connections:
                                base = [
                                    *client,
                                    "-c",
                                    str(connections),
                                    "--no-tui",
                                    "-H",
                                    "cookie: session_id=abc",
                                    *request,
                                ]
                                url = f"http://127.0.0.1:{args.port}/{route}"
                                if args.warmup:
                                    subprocess.run(
                                        [*base, "-z", f"{args.warmup}s", url],
                                        check=True,
                                        stdout=subprocess.DEVNULL,
                                    )
                                measured = subprocess.run(
                                    [
                                        *base,
                                        "-z",
                                        f"{args.duration}s",
                                        "--output-format",
                                        "json",
                                        url,
                                    ],
                                    capture_output=True,
                                    text=True,
                                    check=True,
                                )
                                row = sample(
                                    json.loads(measured.stdout),
                                    framework=framework,
                                    workers=workers,
                                    route=route,
                                    connections=connections,
                                    middleware=args.middleware,
                                    mode=args.mode,
                                )
                                row.update(
                                    server_cpus=args.server_cpus[:workers],
                                    client_cpus=args.client_cpus,
                                )
                                line = json.dumps(row)
                                print(line, flush=True)
                                output.write(line + "\n")
                                output.flush()
                    finally:
                        stop(process)


def terminate(signum, _frame):
    raise SystemExit(128 + signum)


def main():
    signal.signal(signal.SIGTERM, terminate)
    cli = parser()
    try:
        run(configure(cli.parse_args()))
    except (ValueError, OSError, RuntimeError, subprocess.SubprocessError) as error:
        cli.exit(1, f"{error}\n")


if __name__ == "__main__":
    main()
