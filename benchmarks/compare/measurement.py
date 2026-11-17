"""Successful throughput and qualification shared by comparison reports."""

from __future__ import annotations

import math


def finite(value):
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
    )


def counts(value):
    if not isinstance(value, dict) or any(
        not isinstance(count, int) or isinstance(count, bool) or count < 0
        for count in value.values()
    ):
        return None
    return {str(key): count for key, count in value.items()}


def oha_sample(data):
    """Keep missing observations missing; an empty error distribution is valid."""
    summary = data.get("summary", {})
    if not isinstance(summary, dict):
        summary = {}
    codes = counts(data.get("statusCodeDistribution"))
    errors = counts(data.get("errorDistribution"))
    elapsed = summary.get("total")
    elapsed = elapsed if finite(elapsed) and elapsed > 0 else None
    successful = codes.get("200", 0) if codes is not None else None
    latency = data.get("latencyPercentiles", {})
    if not isinstance(latency, dict):
        latency = {}

    def milliseconds(key):
        value = latency.get(key)
        return value * 1000 if finite(value) and value >= 0 else None

    return {
        # Retain the old total-attempt rate for existing raw-result consumers.
        "rps": summary.get("requestsPerSec"),
        "ok_rps": successful / elapsed
        if successful is not None and elapsed is not None
        else None,
        "elapsed_seconds": elapsed,
        "successful_responses": successful,
        "completed_responses": sum(codes.values()) if codes is not None else None,
        "p50_ms": milliseconds("p50"),
        "p99_ms": milliseconds("p99"),
        "non_200": sum(count for code, count in codes.items() if code != "200")
        if codes is not None
        else None,
        "errors": sum(errors.values()) if errors is not None else None,
        "status_codes": codes,
        "error_distribution": errors,
    }


def measurement_reasons(sample):
    reasons = []
    if not finite(sample.get("elapsed_seconds")) or sample["elapsed_seconds"] <= 0:
        reasons.append("missing positive elapsed time")
    if not finite(sample.get("ok_rps")) or sample["ok_rps"] <= 0:
        reasons.append("missing positive successful throughput")
    successful = sample.get("successful_responses")
    if not isinstance(successful, int) or successful <= 0:
        reasons.append("no successful responses recorded")
    elapsed = sample.get("elapsed_seconds")
    if (
        isinstance(successful, int)
        and successful > 0
        and finite(elapsed)
        and elapsed > 0
        and finite(sample.get("ok_rps"))
        and not math.isclose(
            sample["ok_rps"], successful / elapsed, rel_tol=1e-12, abs_tol=1e-12
        )
    ):
        reasons.append("inconsistent successful throughput")
    for field, label in (
        ("non_200", "non-200 responses"),
        ("errors", "transport errors"),
    ):
        value = sample.get(field)
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            reasons.append(f"missing {label}")
        elif value:
            reasons.append(f"{label}: {value}")
    codes = counts(sample.get("status_codes"))
    errors = counts(sample.get("error_distribution"))
    if codes is None:
        reasons.append("missing status distribution")
    elif (
        sample.get("successful_responses") != codes.get("200", 0)
        or sample.get("completed_responses") != sum(codes.values())
        or sample.get("non_200")
        != sum(count for code, count in codes.items() if code != "200")
    ):
        reasons.append("inconsistent status counters")
    if errors is None:
        reasons.append("missing error distribution")
    elif sample.get("errors") != sum(errors.values()):
        reasons.append("inconsistent error counters")
    return reasons


def eligibility_reasons(sample):
    """Old result files remain readable but cannot invent unrecorded checks."""
    reasons = measurement_reasons(sample)
    warmup = sample.get("warmup")
    if not isinstance(warmup, dict):
        reasons.append("warmup not recorded")
    elif warmup.get("skipped") is not True:
        reasons.extend("warmup " + reason for reason in measurement_reasons(warmup))
    for field in ("client_cpu", "server_cpu"):
        value = sample.get(field)
        if not finite(value) or not 0 <= value <= 1:
            reasons.append(field.replace("_", " ") + " unavailable")
    if sample.get("client_bound") is True:
        reasons.append("client-bound")
    elif sample.get("client_bound") is not False:
        reasons.append("client-bound check unavailable")
    recorded = sample.get("eligibility_reasons", [])
    if isinstance(recorded, list):
        reasons.extend(reason for reason in recorded if isinstance(reason, str))
    if sample.get("eligible") is False and not reasons:
        reasons.append("sample marked ineligible")
    return list(dict.fromkeys(reasons))
