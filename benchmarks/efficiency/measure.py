"""Fixed offered load, corrected per-request SLO goodput and CPU/RAM costs."""

import hashlib
import json
import math
import resource
import sqlite3
import subprocess
import time


def percentile(values, fraction):
    return (
        sorted(values)[max(0, math.ceil(len(values) * fraction) - 1)]
        if values
        else None
    )


def records(path, expected_status, expected_bytes, slo_ms, offered):
    with sqlite3.connect(path) as database:
        rows = database.execute(
            "SELECT status,duration,len_bytes,start,end FROM oha"
        ).fetchall()
    successful = [
        row for row in rows if row[0] == expected_status and row[2] == expected_bytes
    ]
    latencies = [row[1] * 1000 for row in successful]
    good = sum(value <= slo_ms for value in latencies)
    return {
        "completed": len(rows),
        "successful": len(successful),
        "within_slo": good,
        "unexpected_status": sum(row[0] != expected_status for row in rows),
        "wrong_body_length": sum(
            row[0] == expected_status and row[2] != expected_bytes for row in rows
        ),
        "rejected": sum(row[0] in (429, 503) for row in rows),
        "p50_ms": percentile(latencies, 0.5),
        "p99_ms": percentile(latencies, 0.99),
        "offered_requests": offered,
        "db": {
            "path": str(path),
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "rows": len(rows),
        },
    }


def oha(args, server, case, rate, duration, database=None):
    command = [
        args.oha,
        "--worker-threads",
        str(args.client_threads),
        "-q",
        str(rate),
        "--latency-correction",
        "-z",
        f"{duration}s",
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
    if case.payload is not None:
        command += [
            "-m",
            "POST",
            "-H",
            "content-type: application/json",
            "-d",
            case.payload.decode(),
        ]
    command.append(server.url + case.path)
    result = subprocess.run(
        command, capture_output=True, text=True, timeout=duration + 10, check=False
    )
    if result.returncode:
        raise RuntimeError(f"oha exited {result.returncode}: {result.stderr}")
    return json.loads(result.stdout)


def child_cpu():
    usage = resource.getrusage(resource.RUSAGE_CHILDREN)
    return usage.ru_utime + usage.ru_stime


def sample(args, server, case, rate, database):
    warmup = oha(args, server, case, rate, args.warmup)
    expected_bytes = (
        len(case.expected)
        if isinstance(case.expected, bytes)
        else len(
            json.dumps(
                case.expected, separators=(",", ":"), ensure_ascii=False
            ).encode()
        )
    )
    server.control("/begin")
    started, client_before = time.monotonic(), child_cpu()
    try:
        raw = oha(args, server, case, rate, args.duration, database)
    finally:
        sampled = server.control("/end", duration=args.duration)
    elapsed = time.monotonic() - started
    client_cores = (child_cpu() - client_before) / elapsed
    result = records(
        database, case.status, expected_bytes, args.p99_ms, rate * args.duration
    )
    if result["completed"] != sum(raw.get("statusCodeDistribution", {}).values()):
        raise RuntimeError("SQLite records disagree with oha status counts")
    errors = sum(raw.get("errorDistribution", {}).values())
    missing = max(0, result["offered_requests"] - result["completed"] - errors - 1)
    failed = result["unexpected_status"] + result["wrong_body_length"] + errors
    attempted = result["completed"] + errors
    result.update(
        {
            "rate": rate,
            "duration": args.duration,
            "workload": case.name,
            "transport_errors": errors,
            "error_distribution": raw.get("errorDistribution", {}),
            "unmet_offered_requests": missing,
            "failure_fraction": failed / max(1, attempted),
            "delivered_fraction": min(
                1, result["successful"] / max(1, result["offered_requests"])
            ),
            "slow_fraction": (result["successful"] - result["within_slo"])
            / max(1, result["successful"]),
            "success_goodput_rps": result["successful"] / args.duration,
            "slo_goodput_rps": result["within_slo"] / args.duration,
            "client_cpu_cores": client_cores,
            "client_bound": client_cores >= 0.85 * args.client_threads,
            "resources": sampled,
            "oha": raw,
            "warmup": warmup,
        }
    )
    success, good = result["successful"], result["within_slo"]
    cpu = sampled["server_cpu_seconds"]
    result["cpu_seconds_per_million_successes"] = (
        cpu * 1e6 / success if success else None
    )
    result["cpu_seconds_per_million_slo_successes"] = cpu * 1e6 / good if good else None
    result["meets_slo"] = bool(
        result["p99_ms"] is not None
        and result["p99_ms"] <= args.p99_ms
        and result["failure_fraction"] <= args.error_budget
        and result["delivered_fraction"] >= args.min_delivered
        and result["slow_fraction"] <= 0.01
        and not result["client_bound"]
    )
    return result
