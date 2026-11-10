"""Pass or fail a functional soak report against stated limits.

`scripts/qualify_service.py` already fails a run on unexpected responses,
unverified cleanup, an unclean shutdown and worker errors in the server log,
and `summarize_soak.py` describes memory without judging it. A scheduled soak
needs three more limits, applied here: the scheduled load was really
delivered, the processes that started are the ones that finished, and
resident memory stopped growing once the server was warm.

A pass bounds growth over one run on a shared machine. It is not a statement
that the server has no leak, and it is not performance acceptance.
"""

from __future__ import annotations

import argparse
import json
import statistics
from pathlib import Path

from summarize_soak import summarize

# Two windows of at least this many samples must fit after warm-up, or memory
# growth was not measured and the run cannot pass.
WINDOW_SAMPLES = 3


def memory_growth(samples: list[dict], warmup_seconds: float, key: str) -> dict | None:
    """Median of the last tenth of the warm samples minus that of the first."""
    warm = [sample for sample in samples if sample["elapsed_seconds"] >= warmup_seconds]
    if len(warm) < 2 * WINDOW_SAMPLES:
        return None
    width = max(WINDOW_SAMPLES, len(warm) // 10)
    first = statistics.median(sample[key] / 1024 for sample in warm[:width])
    last = statistics.median(sample[key] / 1024 for sample in warm[-width:])
    return {
        "warm_samples": len(warm),
        "window_samples": width,
        "first_window_median_mib": first,
        "last_window_median_mib": last,
        "growth_mib": last - first,
        "measured_seconds": warm[-1]["elapsed_seconds"] - warm[0]["elapsed_seconds"],
    }


def evaluate(
    report: dict,
    *,
    warmup_seconds: float,
    max_rss_growth_mib: float,
    min_delivery_fraction: float,
) -> dict:
    summary = summarize(report, warmup_seconds)
    samples = report["samples"]
    checks = []

    def check(name: str, passed: bool, observed, limit) -> None:
        checks.append(
            {"name": name, "passed": bool(passed), "observed": observed, "limit": limit}
        )

    check(
        "harness verdict",
        report.get("qualification_exit_code") == 0
        and report.get("completed_duration") is True
        and report.get("error") is None,
        {
            "exit_code": report.get("qualification_exit_code"),
            "completed_duration": bool(report.get("completed_duration")),
            "error": report.get("error"),
        },
        "exit code 0 after the full requested duration",
    )
    check(
        "unexpected responses",
        summary["unexpected_errors"] == 0,
        summary["unexpected_errors"],
        0,
    )
    delivered = summary["expected_response_fraction_of_schedule"]
    check(
        "delivered load",
        delivered is not None and delivered >= min_delivery_fraction,
        delivered,
        f">= {min_delivery_fraction} of the scheduled requests",
    )
    # A worker that was replaced makes the summed memory of the first and last
    # samples describe different processes, and is a failure of its own in a
    # run that injects no fault.
    expected_processes = samples[0]["process_count"]
    if report.get("server_workers") is not None:
        expected_processes = report["server_workers"] + 1
    population = {
        "first_sample": samples[0]["process_count"],
        "last_sample": summary["last_process_count"],
        "distinct_identities": summary["distinct_process_identities_observed"],
    }
    check(
        "process population",
        set(population.values()) == {expected_processes},
        population,
        f"{expected_processes} process{'' if expected_processes == 1 else 'es'} "
        "throughout, none replaced",
    )
    rss = memory_growth(samples, warmup_seconds, "rss_kib")
    check(
        "resident memory growth after warm-up",
        rss is not None and rss["growth_mib"] <= max_rss_growth_mib,
        None if rss is None else round(rss["growth_mib"], 3),
        f"<= {max_rss_growth_mib} MiB between the first and last tenth of the "
        f"samples taken after {warmup_seconds:g} s",
    )
    # The limit is absolute, so what it can catch depends on how many requests
    # the measured part of the run served: the steady leak per request that
    # would just have reached it.
    limit_bytes_per_request = None
    rate = summary["attempted_requests_per_second"]
    if rss is not None and rate and rss["measured_seconds"] > 0:
        limit_bytes_per_request = (
            max_rss_growth_mib * 1024 * 1024 / (rate * rss["measured_seconds"])
        )
    return {
        "classification": "same_host_functional_soak_gate",
        "formal_acceptance": False,
        "passed": all(item["passed"] for item in checks),
        "checks": checks,
        "warmup_seconds": warmup_seconds,
        "max_rss_growth_mib": max_rss_growth_mib,
        "limit_bytes_per_request": limit_bytes_per_request,
        "rss": rss,
        "anonymous_rss": memory_growth(samples, warmup_seconds, "anonymous_rss_kib"),
        "memory_note": "Resident memory is summed over the supervisor and its "
        "workers, so shared pages count more than once; the limit is on growth, "
        "which that does not affect. A pass bounds growth over this run only.",
        "summary": summary,
    }


def markdown(result: dict) -> str:
    summary = result["summary"]
    load = (
        f"{summary['request_attempts']:,} requests in "
        f"{summary['elapsed_seconds'] / 60:.1f} minutes "
        f"({summary['attempted_requests_per_second']:.1f} per second), "
        f"{summary['unexpected_errors']} unexpected."
    )
    lines = [
        f"### Soak: {'passed' if result['passed'] else 'FAILED'}",
        "",
        load,
        "",
        "| Check | Result | Observed | Limit |",
        "| --- | --- | --- | --- |",
    ]
    for item in result["checks"]:
        observed = json.dumps(item["observed"]).replace("|", "\\|")
        lines.append(
            f"| {item['name']} | {'pass' if item['passed'] else 'FAIL'} "
            f"| {observed} | {item['limit']} |"
        )
    rss = result["rss"]
    if rss is not None:
        slope = summary["rss_slope_mib_per_hour_after_warmup"]
        memory = (
            f"Resident memory after warm-up: {rss['first_window_median_mib']:.2f} MiB "
            f"to {rss['last_window_median_mib']:.2f} MiB over "
            f"{rss['measured_seconds'] / 60:.1f} minutes "
            f"({rss['warm_samples']} samples; fitted slope "
            f"{'n/a' if slope is None else format(slope, '.2f')} MiB per hour, "
            "not gated)."
        )
        lines += ["", memory]
        if result["limit_bytes_per_request"] is not None:
            sensitivity = (
                f"The {result['max_rss_growth_mib']:g} MiB limit is absolute: over "
                "the requests this run served after warm-up it is about "
                f"{result['limit_bytes_per_request']:.0f} bytes per request, and a "
                "shorter run can only see a larger leak."
            )
            lines += ["", sensitivity]
    lines += ["", result["memory_note"], ""]
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=Path)
    parser.add_argument("--warmup-seconds", type=float, default=600)
    parser.add_argument("--max-rss-growth-mib", type=float, default=8)
    parser.add_argument("--min-delivery-fraction", type=float, default=0.97)
    parser.add_argument("--json", type=Path, help="Write the full verdict here")
    parser.add_argument("--markdown", type=Path, help="Append a summary table here")
    args = parser.parse_args()
    if args.warmup_seconds < 0 or args.max_rss_growth_mib < 0:
        parser.error("warm-up and growth limit must be nonnegative")
    if not 0 < args.min_delivery_fraction <= 1:
        parser.error("delivery fraction must be in (0, 1]")
    try:
        result = evaluate(
            json.loads(args.report.read_text()),
            warmup_seconds=args.warmup_seconds,
            max_rss_growth_mib=args.max_rss_growth_mib,
            min_delivery_fraction=args.min_delivery_fraction,
        )
    except (OSError, ValueError, KeyError) as error:
        # No readable report is a failed soak, not a skipped check.
        print(f"soak gate: no usable report: {error!r}")
        return 1
    if args.json is not None:
        args.json.write_text(json.dumps(result, indent=2, allow_nan=False) + "\n")
    text = markdown(result)
    if args.markdown is not None:
        with args.markdown.open("a") as stream:
            stream.write(text)
    print(text)
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
