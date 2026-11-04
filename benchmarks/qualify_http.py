"""Measure installed framework HTTP capacity with an independent oha process.

These short same-host diagnostics compare the compatibility bridge on a fixed
affinity budget. They do not grant M0, Tier1, scaling or production acceptance.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from http.client import HTTPConnection
from pathlib import Path

from measure_m0 import LoadGenerator, Samples
from qualify_m0 import atomic_json, fingerprint, free_port, resource_context, utc_now
from soak import (
    APPLICATION,
    process_tree_snapshot,
    same_json,
    server_log_errors,
    signal_owned_process,
)

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from qualify_service import process_identity, verify_installed_wheel


def cpu_usage(samples: list[dict], key: str) -> float | None:
    samples = [sample for sample in samples if sample[key]["processes"]]
    if len(samples) < 2:
        return None
    first, last = samples[0], samples[-1]
    before = {
        (item["pid"], item["start_ticks"]): item["cpu_seconds"]
        for item in first[key]["processes"]
    }
    delta = sum(
        max(item["cpu_seconds"] - before[(item["pid"], item["start_ticks"])], 0)
        for item in last[key]["processes"]
        if (item["pid"], item["start_ticks"]) in before
    )
    elapsed = last["elapsed_seconds"] - first["elapsed_seconds"]
    return delta / elapsed * 100 if elapsed else None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--python", type=Path, required=True)
    parser.add_argument("--wheel", type=Path, required=True)
    parser.add_argument("--oha", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--workers", type=int)
    parser.add_argument("--server-cpus", default="0")
    parser.add_argument("--load-cpu", default="2")
    parser.add_argument("--connections", type=int, default=50)
    parser.add_argument("--warmup", type=float, default=5)
    parser.add_argument("--duration", type=float, default=20)
    parser.add_argument("--runs", type=int, default=3)
    args = parser.parse_args()
    if (
        args.runs < 1
        or args.connections < 1
        or not 0 < args.warmup < float("inf")
        or not 0 < args.duration < float("inf")
        or args.workers is not None
        and args.workers < 1
    ):
        parser.error("durations and counts must be positive")
    args.output.mkdir(parents=True, exist_ok=False)
    fixture = args.output / "qualification_app.py"
    fixture.write_text(APPLICATION)
    port = free_port()
    command = [
        "taskset",
        "-c",
        args.server_cpus,
        str(args.python.absolute()),
        "-m",
        "bessemer",
        "run",
        "qualification_app:app",
        "--host",
        "127.0.0.1",
        "--port",
        str(port),
        "--max-in-flight",
        "128",
        "--request-timeout",
        "1",
        "--graceful-timeout",
        "2",
    ]
    if args.workers is not None:
        command.extend(["--workers", str(args.workers)])
    report = {
        "schema_version": 1,
        "started_at": utc_now(),
        "classification": "exploratory_framework_capacity_same_host",
        "formal_acceptance": False,
        "limitations": "Shared host and cgroup, uncontrolled frequency, short runs; no Tier1 or formal scaling claim. Startup verifies JSON contents; oha checks every response status and socket outcome.",
        "wheel": fingerprint(args.wheel),
        "harness": fingerprint(Path(__file__)),
        "fixture": fingerprint(fixture),
        "oha": fingerprint(args.oha),
        "server_command": command,
        "resource_context_start": resource_context(),
        "warmup_seconds": args.warmup,
        "sample_seconds": args.duration,
        "runs_per_endpoint": args.runs,
        "connections": args.connections,
        "runs": [],
        "error": None,
    }
    process = None
    load = None
    owned = {}
    result = 2
    server_log = (args.output / "server.log").open("w")
    try:
        report["installed_wheel"] = verify_installed_wheel(args.python, args.wheel)
        environment = os.environ.copy()
        environment.pop("PYTHONPATH", None)
        process = subprocess.Popen(
            command,
            cwd=args.output,
            env=environment,
            stdout=server_log,
            stderr=server_log,
            start_new_session=True,
        )
        until = time.monotonic() + 15
        while process.poll() is None and time.monotonic() < until:
            try:
                connection = HTTPConnection("127.0.0.1", port, timeout=1)
                connection.request("GET", "/json")
                response = connection.getresponse()
                value = json.loads(response.read(4096))
                connection.close()
                if response.status == 200 and same_json(
                    value, {"ok": True, "count": 42}
                ):
                    break
            except OSError:
                pass
            time.sleep(0.05)
        else:
            raise RuntimeError("server failed readiness")
        for item in process_tree_snapshot(process.pid)["processes"]:
            owned[item["pid"]] = item["start_ticks"]
        rates = {"json": [], "io": []}
        for endpoint, endpoint_rates in rates.items():
            for sample in range(args.runs + 1):
                duration = args.warmup if sample == 0 else args.duration
                stem = (
                    f"{endpoint}-{'warmup' if sample == 0 else f'sample-{sample:02}'}"
                )
                load_command = [
                    "taskset",
                    "-c",
                    args.load_cpu,
                    str(args.oha),
                    "--worker-threads",
                    "1",
                    "-z",
                    f"{duration}s",
                    "-c",
                    str(args.connections),
                    "-w",
                    "-t",
                    "5s",
                    "--http-version",
                    "1.1",
                    "--no-tui",
                    "--output-format",
                    "json",
                    f"http://127.0.0.1:{port}/{endpoint}",
                ]
                raw = args.output / f"{stem}.json"
                started = time.monotonic()
                record = {
                    "endpoint": endpoint,
                    "phase": "warmup" if sample == 0 else "sample",
                    "sample": sample,
                    "command": load_command,
                    "started_at": utc_now(),
                    "raw_file": raw.name,
                    "process_samples": [],
                }
                report["runs"].append(record)
                with (
                    raw.open("w") as output,
                    (args.output / f"{stem}.stderr").open("w") as error,
                ):
                    load = subprocess.Popen(load_command, stdout=output, stderr=error)
                    while load.poll() is None:
                        server = process_tree_snapshot(process.pid)
                        for item in server["processes"]:
                            owned[item["pid"]] = item["start_ticks"]
                        record["process_samples"].append(
                            {
                                "elapsed_seconds": time.monotonic() - started,
                                "server": server,
                                "load": process_tree_snapshot(load.pid),
                            }
                        )
                        if time.monotonic() - started > duration + 30:
                            raise RuntimeError("oha exceeded bounded sample duration")
                        time.sleep(1)
                record["exit_code"] = load.returncode
                record["ended_at"] = utc_now()
                if load.returncode:
                    raise RuntimeError(f"oha failed: {stem}")
                record["requests_per_second"] = LoadGenerator.rate(raw.read_text())
                populations = {
                    tuple(
                        (item["pid"], item["start_ticks"])
                        for item in sample["server"]["processes"]
                    )
                    for sample in record["process_samples"]
                }
                record["server_process_population_stable"] = len(populations) == 1
                if not record["server_process_population_stable"]:
                    raise RuntimeError("server process population changed during load")
                record["sampled_server_cpu_percent"] = cpu_usage(
                    record["process_samples"], "server"
                )
                record["sampled_load_cpu_percent"] = cpu_usage(
                    record["process_samples"], "load"
                )
                if sample:
                    endpoint_rates.append(record["requests_per_second"])
                atomic_json(args.output / "report.json", report)
                print(
                    json.dumps(
                        {
                            key: value
                            for key, value in record.items()
                            if key not in {"command", "process_samples"}
                        }
                    ),
                    flush=True,
                )
        report["statistics"] = {
            name: {
                "median": Samples(name, tuple(values)).median,
                "spread": Samples(name, tuple(values)).spread,
            }
            for name, values in rates.items()
        }
        result = 0
    except (
        OSError,
        ValueError,
        TypeError,
        RuntimeError,
        subprocess.SubprocessError,
        KeyboardInterrupt,
    ) as error:
        report["error"] = str(error) or type(error).__name__
    finally:
        if load is not None and load.poll() is None:
            load.kill()
            load.wait(timeout=5)
        if process is not None:
            started = time.monotonic()
            if process.poll() is None:
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
            try:
                process.wait(timeout=8)
                report["shutdown_forced"] = False
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=5)
                report["shutdown_forced"] = True
            report["shutdown_seconds"] = time.monotonic() - started
            report["process_exit_code"] = process.returncode
            remaining_owned = {
                pid: identity
                for pid, identity in owned.items()
                if process_identity(pid) == identity
            }
            if remaining_owned:
                report["orphan_cleanup_pids"] = sorted(remaining_owned)
                result = 1
            for pid, identity in remaining_owned.items():
                try:
                    signal_owned_process(pid, identity, signal.SIGKILL)
                except (OSError, AttributeError) as error:
                    report.setdefault("cleanup_errors", []).append(
                        {"pid": pid, "signal": "SIGKILL", "error": str(error)}
                    )
            if (
                process.returncode
                or report.get("shutdown_forced")
                or report.get("orphan_cleanup_pids")
            ):
                result = 1
        server_log.close()
        report["server_log_check"] = server_log_errors(args.output / "server.log")
        if not report["server_log_check"]["verified"]:
            result = 1
        report["ended_at"] = utc_now()
        report["qualification_exit_code"] = result
        report["resource_context_end"] = resource_context()
        atomic_json(args.output / "report.json", report)
    return result


if __name__ == "__main__":
    raise SystemExit(main())
