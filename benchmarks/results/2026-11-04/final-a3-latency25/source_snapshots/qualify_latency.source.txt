"""Record corrected fixed-rate wrk2 latency evidence at fractions of Axum load.

The results are exploratory on this shared host. They do not replace the
separate-host, repeatable release gate defined in the specification.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import subprocess
from pathlib import Path

from measure_m0 import verify_endpoint
from qualify_m0 import atomic_json, fingerprint, resource_context, utc_now


def time_seconds(value: str) -> float:
    match = re.fullmatch(r"([0-9]+(?:\.[0-9]+)?)(ns|us|ms|s)", value)
    if match is None:
        raise ValueError(f"unrecognized wrk2 time value:{value}")
    return float(match[1]) * {"ns": 1e-9, "us": 1e-6, "ms": 1e-3, "s": 1}[match[2]]


def parse_wrk_output(output: str, requested_rate: int) -> dict:
    if requested_rate < 1:
        raise ValueError("requested rate must be positive")
    heading = "Latency Distribution (HdrHistogram - Recorded Latency)"
    if heading not in output:
        raise ValueError("wrk2 corrected Recorded Latency histogram is missing")
    corrected = output.split(heading, 1)[1].split(
        "----------------------------------------------------------", 1
    )[0]
    percentiles = {}
    for percentile, value in re.findall(
        r"^\s*([0-9.]+)%\s+([0-9.]+(?:ns|us|ms|s))\s*$", corrected, re.MULTILINE
    ):
        percentiles[float(percentile)] = time_seconds(value)
    if not {50.0, 99.0, 99.9}.issubset(percentiles):
        raise ValueError("wrk2 corrected percentiles are incomplete")
    spectrum = re.findall(
        r"^\s*([0-9.]+)\s+([0-9.]+)\s+([0-9]+)\s+[0-9.inf]+\s*$",
        corrected,
        re.MULTILINE,
    )
    p95 = next(
        (
            float(value) / 1000
            for value, percentile, count in spectrum
            if float(percentile) >= 0.95 and int(count) > 0
        ),
        None,
    )
    if p95 is None:
        raise ValueError("wrk2 corrected histogram has no populated 95th percentile")
    precision = (
        "p95 is an upper bound from the first printed spectrum row at or above 95%"
    )
    exact = re.findall(
        r"^QUALIFICATION_PERCENTILES_US ([0-9]+) ([0-9]+) ([0-9]+) ([0-9]+)$",
        output,
        re.MULTILINE,
    )
    if len(exact) > 1:
        raise ValueError("wrk2 exact percentile footer is ambiguous")
    if "QUALIFICATION_PERCENTILES_US" in output and not exact:
        raise ValueError("wrk2 exact percentile footer is malformed")
    if exact:
        values = [float(value) / 1_000_000 for value in exact[0]]
        if values != sorted(values):
            raise ValueError("wrk2 exact percentile footer is not ordered")
        percentiles = {50.0: values[0], 99.0: values[2], 99.9: values[3]}
        p95 = values[1]
        precision = "direct corrected HdrHistogram percentile lookup via wrk2 done()"
    rates = re.findall(r"Requests/sec:\s+([0-9.]+)", output)
    counts = re.findall(r"^\s*([0-9]+) requests in ", output, re.MULTILINE)
    if len(rates) != 1 or len(counts) != 1 or int(counts[0]) < 1:
        raise ValueError("wrk2 request totals/rate are missing or ambiguous")
    rate = float(rates[0])
    if not math.isfinite(rate) or rate <= 0:
        raise ValueError("wrk2 rate must be positive and finite")
    errors = []
    sockets = re.findall(
        r"Socket errors: connect (\d+), read (\d+), write (\d+), timeout (\d+)", output
    )
    if sockets and sum(map(int, sockets[0])):
        errors.append("socket_errors")
    statuses = re.findall(r"Non-2xx or 3xx responses: (\d+)", output)
    if statuses and int(statuses[0]):
        errors.append("non_success_statuses")
    if rate < requested_rate * 0.97:
        errors.append("requested_rate_not_achieved")
    return {
        "requests": int(counts[0]),
        "requested_rate": requested_rate,
        "actual_rate": rate,
        "rate_fraction_achieved": rate / requested_rate,
        "p50_seconds": percentiles[50.0],
        "p95_seconds": p95,
        "p99_seconds": percentiles[99.0],
        "p999_seconds": percentiles[99.9],
        "percentile_method": precision,
        "corrected_histogram_population": max(int(count) for _, _, count in spectrum),
        "histogram_note": "wrk2 resets its latency histogram after calibration; total completed requests also include calibration",
        "coordinated_omission_correction": "wrk2 Recorded Latency",
        "delivery_valid": not errors,
        "invalid_reasons": errors,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wrk2", type=Path, required=True)
    parser.add_argument("--rust-url", required=True)
    parser.add_argument("--python-url", required=True)
    parser.add_argument("--throughput-report", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--load-cpus", default="1")
    parser.add_argument("--threads", type=int, default=1)
    parser.add_argument("--connections", type=int, default=50)
    parser.add_argument("--duration", type=int, default=60)
    parser.add_argument("--runs", type=int, default=1)
    parser.add_argument("--allow-invalid-baseline", action="store_true")
    args = parser.parse_args()
    if args.duration < 20 or args.connections < 1 or args.threads < 1 or args.runs < 1:
        parser.error(
            "duration must allow at least 20 seconds for wrk2 calibration; counts must be positive"
        )
    baseline = json.loads(args.throughput_report.read_text())
    if not baseline.get("samples_valid") and not args.allow_invalid_baseline:
        parser.error(
            "throughput sample spread was invalid; repeat it or explicitly allow an exploratory invalid baseline"
        )
    rust_saturation = baseline["statistics"]["rust_median"]
    latency_script = Path(__file__).with_name("latency.lua")
    args.output.mkdir(parents=True, exist_ok=False)
    report = {
        "schema_version": 1,
        "started_at": utc_now(),
        "classification": "exploratory_same_host_fixed_rate",
        "formal_acceptance": False,
        "reason": "Shared host and unverified frequency/isolation; current local throughput estimate is a workload reference only.",
        "throughput_report": fingerprint(args.throughput_report),
        "baseline_samples_valid": baseline.get("samples_valid"),
        "rust_reference_requests_per_second": rust_saturation,
        "wrk2": fingerprint(args.wrk2),
        "harness": fingerprint(Path(__file__)),
        "latency_script": fingerprint(latency_script),
        "wrk2_source": "giltene/wrk2 commit 44a94c17d8e6a0bac8559b53da76848e430cb7a7 when using session-built tool; independently verify other binaries",
        "resource_context": resource_context(),
        "runs": [],
        "error": None,
    }
    result = 0
    try:
        for mode, url in (("rust", args.rust_url), ("python", args.python_url)):
            verify_endpoint(mode, url)
        report["endpoint_verification"] = {
            "rust": args.rust_url,
            "python": args.python_url,
            "exact_payload_and_mode_verified": True,
        }
        for sample in range(args.runs):
            for fraction in (0.25, 0.50, 0.75):
                modes = (("rust", args.rust_url), ("python", args.python_url))
                if sample % 2:
                    modes = tuple(reversed(modes))
                rate = max(1, math.floor(rust_saturation * fraction))
                for mode, url in modes:
                    command = [
                        "taskset",
                        "-c",
                        args.load_cpus,
                        str(args.wrk2),
                        "-t",
                        str(args.threads),
                        "-c",
                        str(args.connections),
                        "-d",
                        f"{args.duration}s",
                        "-R",
                        str(rate),
                        "--timeout",
                        "5s",
                        "--latency",
                        "-s",
                        str(latency_script),
                        url,
                    ]
                    started = utc_now()
                    process = subprocess.run(
                        command,
                        capture_output=True,
                        text=True,
                        timeout=args.duration + 30,
                        check=False,
                    )
                    stem = f"sample-{sample + 1:02}-{int(fraction * 100)}pct-{mode}"
                    (args.output / f"{stem}.txt").write_text(process.stdout)
                    (args.output / f"{stem}.stderr").write_text(process.stderr)
                    run = {
                        "mode": mode,
                        "fraction": fraction,
                        "sample": sample + 1,
                        "command": command,
                        "started_at": started,
                        "ended_at": utc_now(),
                        "exit_code": process.returncode,
                        "raw_file": f"{stem}.txt",
                    }
                    report["runs"].append(run)
                    if process.returncode:
                        run["error"] = f"wrk2  exited with code {process.returncode}"
                        result = 2
                    else:
                        try:
                            run.update(parse_wrk_output(process.stdout, rate))
                            if not run["percentile_method"].startswith("direct"):
                                raise ValueError("wrk2 exact percentile footer missing")
                            if not run["delivery_valid"]:
                                result = 1
                        except ValueError as error:
                            run["error"] = str(error)
                            result = 2
                    atomic_json(args.output / "report.json", report)
                    print(
                        json.dumps(
                            {
                                key: value
                                for key, value in run.items()
                                if key not in {"command"}
                            }
                        ),
                        flush=True,
                    )
    except (
        OSError,
        ValueError,
        subprocess.SubprocessError,
        KeyboardInterrupt,
    ) as error:
        report["error"] = str(error) or type(error).__name__
        result = 2
    finally:
        report["ended_at"] = utc_now()
        report["qualification_exit_code"] = result
        report["all_runs_completed"] = len(report["runs"]) == args.runs * 6
        report["all_runs_delivery_valid"] = bool(report["runs"]) and all(
            run.get("delivery_valid", False) for run in report["runs"]
        )
        atomic_json(args.output / "report.json", report)
    return result


if __name__ == "__main__":
    raise SystemExit(main())
