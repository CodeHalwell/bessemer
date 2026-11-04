"""Summarize functional soak evidence without converting it to release acceptance."""

from __future__ import annotations

import argparse
import json
import statistics
from pathlib import Path


def summarize(report: dict, warmup_seconds: float = 600) -> dict:
    samples = report.get("samples", [])
    if not samples:
        raise ValueError("report has no process/workload samples")
    workload = report.get("workload") or samples[-1]
    count = workload["request_latency"]["count"]
    elapsed = workload["elapsed_seconds"]
    errors = sum(workload["unexpected_errors"].values())
    responses = sum(
        value for status, value in workload["statuses"].items() if status.isdigit()
    )
    expected = report["target_requests_per_second"] * elapsed
    rss = [sample["rss_kib"] / 1024 for sample in samples]
    tail = [sample for sample in samples if sample["elapsed_seconds"] >= warmup_seconds]
    slope = None
    if len(tail) >= 2:
        x = [sample["elapsed_seconds"] for sample in tail]
        y = [sample["rss_kib"] / 1024 for sample in tail]
        mx, my = statistics.mean(x), statistics.mean(y)
        denominator = sum((value - mx) ** 2 for value in x)
        if denominator:
            slope = sum((a - mx) * (b - my) for a, b in zip(x, y)) / denominator * 3600
    seen_cpu = {}
    observed_cpu = 0
    for sample in samples:
        for process in sample["processes"]:
            identity = (process["pid"], process.get("start_ticks"))
            value = process["cpu_seconds"]
            observed_cpu += max(value - seen_cpu.get(identity, 0), 0)
            seen_cpu[identity] = value
    return {
        "classification": "same_host_functional_soak_summary",
        "formal_acceptance": False,
        "completed_requested_duration": bool(report.get("completed_duration")),
        "ended_at": report.get("ended_at"),
        "error": report.get("error"),
        "qualification_exit_code": report.get("qualification_exit_code"),
        "elapsed_seconds": elapsed,
        "request_attempts": count,
        "completed_http_responses": responses,
        "expected_responses": count - errors,
        "attempted_requests_per_second": count / elapsed if elapsed else None,
        "expected_responses_per_second": (count - errors) / elapsed
        if elapsed
        else None,
        "completion_fraction_of_schedule": responses / expected if expected else None,
        "expected_response_fraction_of_schedule": (count - errors) / expected
        if expected
        else None,
        "unexpected_errors": errors,
        "unexpected_error_fraction": errors / count if count else None,
        "statuses": workload["statuses"],
        "request_latency": workload["request_latency"],
        "generator_lag": workload["generator_lag"],
        "rss_mib_first": rss[0],
        "rss_mib_last": rss[-1],
        "rss_mib_min": min(rss),
        "rss_mib_max": max(rss),
        "rss_mib_tail_median": statistics.median(
            [sample["rss_kib"] / 1024 for sample in tail]
        )
        if tail
        else None,
        "rss_slope_mib_per_hour_after_warmup": slope,
        "rss_slope_warmup_seconds": warmup_seconds,
        "memory_note": "Sum of per-process RSS includes shared pages multiple times. A sampled slope is descriptive and is not a leak verdict; worker churn changes the population.",
        "distinct_process_identities_observed": len(seen_cpu),
        "last_process_count": samples[-1]["process_count"],
        "sampled_cpu_seconds": observed_cpu,
        "sampled_average_cpu_percent": observed_cpu / elapsed * 100
        if elapsed
        else None,
        "cpu_note": "CPU is summed by PID/start-time identity and includes observed process startup. Work between the final sample of an exiting worker and its exit is unobserved, so this is a sampled lower bound on total server-process lifetime CPU.",
        "timeout_cleanup": report.get("timeout_cleanup"),
        "fault_events": report.get("fault_events", []),
        "shutdown_seconds": report.get("shutdown_seconds"),
        "shutdown_forced": report.get("shutdown_forced"),
        "process_exit_code": report.get("process_exit_code"),
        "orphan_cleanup_pids": report.get("orphan_cleanup_pids", []),
        "wheel_sha256": report["wheel_sha256"],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=Path)
    parser.add_argument("--warmup-seconds", type=float, default=600)
    args = parser.parse_args()
    if args.warmup_seconds < 0:
        parser.error("warmup must be nonnegative")
    print(
        json.dumps(
            summarize(json.loads(args.report.read_text()), args.warmup_seconds),
            indent=2,
            allow_nan=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
