"""Measure per-worker CPU and socket ownership under a short JSON load.

Uses the installed wheel, the shared benchmark app, oha 1.16+, and psutil.
This is a local diagnostic, not a scaling acceptance or capacity claim.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import signal
import subprocess
import sys
import time
from collections import Counter
from contextlib import contextmanager
from pathlib import Path

from server import DIRECT, Server, Variant, resolve_executable
from workloads import WORKLOADS

HERE = Path(__file__).resolve().parent
JSON = next(workload for workload in WORKLOADS if workload.name == "json")


def psutil_module():
    try:
        import psutil
    except ImportError as error:
        raise RuntimeError(
            "install psutil in the benchmark driver environment"
        ) from error
    return psutil


def installed_version(python: str) -> dict:
    script = (
        "import hashlib, importlib.metadata as m, json, sys; "
        "from pathlib import Path; import bessemer._native as n; "
        "print(json.dumps({'python':sys.version, 'bessemer':m.version('bessemer'), "
        "'native_file':n.__file__, "
        "'native_sha256':hashlib.sha256(Path(n.__file__).read_bytes()).hexdigest()}))"
    )
    return json.loads(subprocess.check_output([python, "-c", script], text=True))


def records_parser():
    # Import after the POSIX check; the reference parser imports resource.
    sys.path.insert(0, str(HERE.parent))
    from efficiency.measure import records

    return records


def worker_pids(supervisor_pid: int) -> list[int]:
    psutil = psutil_module()
    return sorted(
        child.pid
        for child in psutil.Process(supervisor_pid).children(recursive=True)
        if "_worker" in " ".join(child.cmdline())
    )


def cpu_seconds(pid: int) -> float | None:
    psutil = psutil_module()
    try:
        times = psutil.Process(pid).cpu_times()
        return times.user + times.system
    except psutil.Error:
        return None


def lsof_counts(pids: list[int], port: int) -> tuple[dict[int, int], str | None]:
    try:
        result = subprocess.run(
            [
                "lsof",
                "-nP",
                "-a",
                "-p",
                ",".join(map(str, pids)),
                f"-iTCP:{port}",
                "-sTCP:ESTABLISHED",
                "-Fpn",
            ],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        return {}, f"lsof:{type(error).__name__}"
    if result.returncode or not result.stdout.strip():
        return {}, f"lsof exit {result.returncode}: {result.stderr[-200:]}"
    counts: Counter[int] = Counter()
    visible = set()
    current = None
    for line in result.stdout.splitlines():
        if line.startswith("p") and line[1:].isdigit():
            current = int(line[1:])
            visible.add(current)
        elif line.startswith("n") and current in pids:
            counts[current] += 1
    if not counts:
        return {}, "lsof returned no socket records"
    return {pid: counts[pid] for pid in pids if pid in visible}, None


def socket_counts(
    pids: list[int], port: int
) -> tuple[dict[str, int | None], dict[str, str]]:
    psutil = psutil_module()
    counts: dict[str, int | None] = {}
    errors: dict[str, str] = {}
    for pid in pids:
        try:
            sockets = psutil.Process(pid).net_connections(kind="tcp")
            counts[str(pid)] = sum(
                item.status == psutil.CONN_ESTABLISHED
                and bool(item.laddr)
                and item.laddr.port == port
                for item in sockets
            )
        except psutil.Error as error:
            counts[str(pid)] = None
            errors[str(pid)] = f"psutil:{type(error).__name__}"
    if errors:
        fallback, lsof_error = lsof_counts(pids, port)
        for pid in list(errors):
            if int(pid) in fallback:
                counts[pid] = fallback[int(pid)]
                errors.pop(pid)
        if lsof_error:
            errors["lsof"] = lsof_error
    return counts, errors


def capture(pids: list[int], port: int, *, sockets: bool) -> dict:
    row = {
        "monotonic": time.monotonic(),
        "cpu_seconds": {str(pid): cpu_seconds(pid) for pid in pids},
    }
    if sockets:
        row["established_sockets"], row["socket_errors"] = socket_counts(pids, port)
    return row


def cpu_cores(before: dict, after: dict) -> dict[str, float | None]:
    elapsed = after["monotonic"] - before["monotonic"]
    if elapsed <= 0:
        raise ValueError("nonpositive CPU interval")
    return {
        pid: (last - first) / elapsed
        if first is not None and last is not None
        else None
        for pid, first in before["cpu_seconds"].items()
        for last in (after["cpu_seconds"].get(pid),)
    }


def ownership_status(counts: dict[str, int | None], expected: int) -> str:
    if not counts or any(count is None for count in counts.values()):
        return "unavailable"
    total = sum(counts.values())
    if total == 0:
        return "empty"
    return "complete" if total == expected else "partial"


def oha_command(
    args: argparse.Namespace, port: int, seconds: float, database: Path | None
) -> list[str]:
    command = [
        args.oha,
        "--worker-threads",
        str(args.client_threads),
        "-z",
        f"{seconds}s",
        "-c",
        str(args.connections),
        "-t",
        "2s",
        "--connect-timeout",
        "1s",
        "--no-tui",
        "--disable-compression",
        "--wait-ongoing-requests-after-deadline",
        "--output-format",
        "json",
    ]
    if database is not None:
        command += ["--db-url", str(database)]
    return command + [f"http://127.0.0.1:{port}{JSON.path}"]


def wait_until(deadline: float) -> None:
    while time.monotonic() < deadline:
        time.sleep(min(0.05, deadline - time.monotonic()))


def measure(args: argparse.Namespace, port: int, pids: list[int]) -> dict:
    raw_path = args.out.with_suffix(".oha.json")
    err_path = args.out.with_suffix(".oha.stderr.txt")
    database = args.out.with_suffix(".oha.sqlite")
    if any(path.exists() for path in (raw_path, err_path, database)):
        raise FileExistsError("oha output already exists")
    command = oha_command(args, port, args.duration, database)
    monitor_before = time.process_time()
    with raw_path.open("w") as raw_file, err_path.open("w") as err_file:
        process = subprocess.Popen(
            command, stdout=raw_file, stderr=err_file, start_new_session=True
        )
        started = time.monotonic()
        try:
            wait_until(started + args.duration * 0.1)
            before = capture(pids, port, sockets=False)
            client_before = cpu_seconds(process.pid)
            wait_until(started + args.duration * 0.5)
            middle = capture(pids, port, sockets=True)
            wait_until(started + args.duration * 0.9)
            after = capture(pids, port, sockets=False)
            client_after = cpu_seconds(process.pid)
            process.wait(timeout=args.duration + 20)
        finally:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=5)
    if process.returncode:
        raise RuntimeError(
            f"oha exit {process.returncode}: {err_path.read_text()[-300:]}"
        )
    elapsed = after["monotonic"] - before["monotonic"]
    return {
        "command": command,
        "oha": json.loads(raw_path.read_text()),
        "database": str(database),
        "snapshots": {"before": before, "middle": middle, "after": after},
        "worker_cpu_cores": cpu_cores(before, after),
        "client_cpu_cores": (
            (client_after - client_before) / elapsed
            if client_before is not None and client_after is not None
            else None
        ),
        "monitor_cpu_seconds": time.process_time() - monitor_before,
        "cpu_window_note": "Approximate 10%-90% of requested load duration; excludes oha SQLite export. Monitor CPU is separate from server-worker CPU.",
    }


def reconcile(raw: dict, records: dict) -> dict:
    codes = raw.get("statusCodeDistribution", {})
    if sum(codes.values()) != records["completed"]:
        raise ValueError("oha completed count differs from SQLite rows")
    if (
        sum(count for code, count in codes.items() if code != "200")
        != records["unexpected_status"]
    ):
        raise ValueError("oha status failures differ from SQLite rows")
    elapsed = raw["summary"]["total"]
    if elapsed <= 0:
        raise ValueError("oha measured interval must be positive")
    errors = raw.get("errorDistribution", {})
    return {
        **records,
        "transport_errors": sum(errors.values()),
        "error_distribution": errors,
        "successful_rps": records["successful"] / elapsed,
        "measured_seconds": elapsed,
    }


def parse_measured(measurement: dict, expected_bytes: int) -> dict:
    return reconcile(
        measurement["oha"],
        records_parser()(Path(measurement["database"]), 200, expected_bytes, 50, None),
    )


def track_members(server: Server, identities: dict[int, float]) -> None:
    if not hasattr(server, "process"):
        return
    psutil = psutil_module()
    try:
        parent = psutil.Process(server.process.pid)
        members = [parent, *parent.children(recursive=True)]
    except psutil.Error:
        return
    for process in members:
        try:
            identities[process.pid] = process.create_time()
        except psutil.Error:
            continue


@contextmanager
def track_before_close(server: Server, identities: dict[int, float]):
    try:
        yield
    finally:
        track_members(server, identities)


def live_pids(identities: dict[int, float]) -> list[int]:
    psutil = psutil_module()
    live = []
    for pid, created in identities.items():
        try:
            process = psutil.Process(pid)
            if process.create_time() == created and process.is_running():
                live.append(pid)
        except psutil.Error:
            pass
    return live


def signal_owned(pid: int, created: float, signum: int) -> str | None:
    psutil = psutil_module()
    try:
        if psutil.Process(pid).create_time() != created:
            return "process identity changed"
        if os.getpgid(pid) == pid:
            os.killpg(pid, signum)
        else:
            os.kill(pid, signum)
    except ProcessLookupError:
        return None
    except psutil.NoSuchProcess:
        return None
    except psutil.Error as error:
        return f"psutil:{type(error).__name__}: {error}"
    except OSError as error:
        return f"{type(error).__name__}: {error}"
    return None


def signal_workers(
    pids: list[int], identities: dict[int, float], signum: int, errors: dict[str, str]
) -> None:
    for pid in pids:
        if error := signal_owned(pid, identities[pid], signum):
            errors[str(pid)] = error


def surviving_workers(
    identities: dict[int, float], supervisor_pid: int | None
) -> list[int]:
    return [pid for pid in live_pids(identities) if pid != supervisor_pid]


def cleanup_receipt(server: Server, identities: dict[int, float]) -> dict:
    supervisor_pid = server.process.pid if hasattr(server, "process") else None
    leftovers = surviving_workers(identities, supervisor_pid)
    fallback_sigterm = list(leftovers)
    signal_errors = {}
    signal_workers(leftovers, identities, signal.SIGTERM, signal_errors)
    deadline = time.monotonic() + 2
    while leftovers and time.monotonic() < deadline:
        time.sleep(0.05)
        leftovers = surviving_workers(identities, supervisor_pid)
    forced = list(leftovers)
    signal_workers(forced, identities, signal.SIGKILL, signal_errors)
    if forced:
        time.sleep(0.05)
    return {
        "supervisor_exit_code": server.process.returncode
        if hasattr(server, "process")
        else None,
        "forced_kill": getattr(server, "forced_kill", None),
        "tracked_pids": sorted(identities),
        "fallback_sigterm_pids": fallback_sigterm,
        "fallback_sigkill_pids": forced,
        "remaining_pids": live_pids(identities),
        "signal_errors": signal_errors,
    }


def check_warmup(args: argparse.Namespace, port: int) -> dict:
    warmup = subprocess.run(
        oha_command(args, port, args.warmup, None),
        capture_output=True,
        text=True,
        timeout=args.warmup + 20,
        check=True,
    )
    raw = json.loads(warmup.stdout)
    statuses = raw.get("statusCodeDistribution", {})
    if (
        raw.get("errorDistribution")
        or not statuses.get("200")
        or set(statuses) != {"200"}
    ):
        raise ValueError("warmup had transport or status errors")
    return raw


def finish_receipt(
    result: dict, args: argparse.Namespace, server: Server, identities: dict[int, float]
) -> None:
    counts = (
        result.get("measurement", {})
        .get("snapshots", {})
        .get("middle", {})
        .get("established_sockets", {})
    )
    result["ownership_status"] = ownership_status(counts, args.connections)
    result["observed_sockets"] = (
        sum(counts.values())
        if counts and all(value is not None for value in counts.values())
        else None
    )
    if result["ownership_status"] != "complete" and not result.get("error"):
        result["error"] = f"worker socket ownership {result['ownership_status']}"
    result["cleanup"] = cleanup_receipt(server, identities)
    result["valid"] = bool(
        not result.get("error")
        and result["ownership_status"] == "complete"
        and result["cleanup"]["supervisor_exit_code"] == 0
        and result["cleanup"]["forced_kill"] is False
        and not result["cleanup"]["fallback_sigterm_pids"]
        and not result["cleanup"]["remaining_pids"]
        and not result["cleanup"]["signal_errors"]
    )
    args.out.write_text(
        json.dumps(result, indent=2, sort_keys=True, default=str) + "\n"
    )


def run(args: argparse.Namespace) -> dict:
    if sys.platform == "win32":
        raise RuntimeError("POSIX process-group cleanup is required")
    psutil = psutil_module()
    for path in (
        args.out,
        args.out.with_suffix(".server.log"),
        args.out.with_suffix(".oha.json"),
        args.out.with_suffix(".oha.stderr.txt"),
        args.out.with_suffix(".oha.sqlite"),
    ):
        if path.exists():
            raise FileExistsError(path)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    variant = Variant(
        "default", args.python, loop=None, workers=args.workers, tuned_limits=False
    )
    log = args.out.with_suffix(".server.log").open("w")
    server = Server(variant, log)
    result = {
        "settings": vars(args) | {"out": str(args.out)},
        "source_sha256": {
            str(path): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in (
                Path(__file__),
                HERE / "server.py",
                HERE / "workloads.py",
                HERE.parent / "efficiency" / "measure.py",
            )
        },
    }
    identities = {}
    try:
        result["versions"] = installed_version(variant.python)
        with server, track_before_close(server, identities):
            result["server_command"] = server.command
            result["server_environment"] = server.environment
            identities[server.process.pid] = psutil.Process(
                server.process.pid
            ).create_time()
            pids = worker_pids(server.process.pid)
            if len(pids) != args.workers:
                raise RuntimeError(f"expected {args.workers} workers; found {pids}")
            identities.update({pid: psutil.Process(pid).create_time() for pid in pids})
            result["worker_pids"] = pids
            base = f"http://127.0.0.1:{server.port}"
            JSON.validate(DIRECT, base)
            with DIRECT.open(base + JSON.path, timeout=5) as response:
                expected_bytes = len(response.read())
            result["warmup"] = check_warmup(args, server.port)
            result["measurement"] = measure(args, server.port, pids)
            result["records"] = parse_measured(result["measurement"], expected_bytes)
            JSON.validate(DIRECT, base)
            if worker_pids(server.process.pid) != pids:
                raise RuntimeError("worker identities changed during measurement")
            if any(
                result["records"][key]
                for key in (
                    "transport_errors",
                    "unexpected_status",
                    "wrong_body_length",
                )
            ):
                raise ValueError("measured HTTP responses failed validation")
    except Exception as error:  # noqa: BLE001 -- Retain failed sample and cleanup receipt.
        result["error"] = f"{type(error).__name__}: {error}"
    finally:
        log.close()
        finish_receipt(result, args, server, identities)
    return result


def normalise_executables(args: argparse.Namespace) -> None:
    args.python = resolve_executable(args.python)
    args.oha = resolve_executable(args.oha)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--python", required=True, help="installed Bessemer interpreter"
    )
    parser.add_argument("--oha", required=True, help="oha 1.16+ executable")
    parser.add_argument("--out", required=True, type=Path, help="new JSON receipt path")
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--connections", type=int, default=256)
    parser.add_argument("--client-threads", type=int, default=2)
    parser.add_argument("--warmup", type=float, default=2)
    parser.add_argument("--duration", type=float, default=5)
    args = parser.parse_args()
    if (
        min(
            args.workers,
            args.connections,
            args.client_threads,
            args.warmup,
            args.duration,
        )
        <= 0
    ):
        parser.error("worker, connection, thread, and duration values must be positive")
    normalise_executables(args)
    result = run(args)
    print(
        json.dumps(
            {
                "worker_pids": result.get("worker_pids"),
                "sockets": result.get("measurement", {})
                .get("snapshots", {})
                .get("middle", {})
                .get("established_sockets"),
                "worker_cpu_cores": result.get("measurement", {}).get(
                    "worker_cpu_cores"
                ),
                "successful_rps": result.get("records", {}).get("successful_rps"),
                "error": result.get("error"),
                "cleanup": result["cleanup"],
            },
            sort_keys=True,
        )
    )
    if not result["valid"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
