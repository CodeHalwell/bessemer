"""Small fixed-rate, explicitly capped CPU/RAM/SLO comparison; no price model."""

import argparse
import json
import platform
import sys
from pathlib import Path

from cases import CASES, RATES
from driver import DIRECT, VARIANTS, Server, variant_order
from measure import sample


def arguments():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", default="bessemer-efficiency-tools:2026-11-06")
    parser.add_argument("--venvs", type=Path, required=True)
    parser.add_argument("--oha", required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--db-dir", type=Path, required=True)
    parser.add_argument(
        "--variants", default="baseline,candidate,candidate-rust,fastapi"
    )
    parser.add_argument("--cases", default="json,io,sync-stream,mixed")
    parser.add_argument("--rates", help="override every case's ramp, e.g. 1000,5000")
    parser.add_argument("--duration", type=float, default=2)
    parser.add_argument("--warmup", type=float, default=0.5)
    parser.add_argument("--rounds", type=int, default=1)
    parser.add_argument("--connections", type=int, default=64)
    parser.add_argument("--client-threads", type=int, default=2)
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--cpus", type=int, default=2)
    parser.add_argument("--cpuset", default="0,1")
    parser.add_argument("--memory-mb", type=int, default=512)
    parser.add_argument("--pids-limit", type=int, default=256)
    parser.add_argument("--p99-ms", type=float, default=50)
    parser.add_argument("--error-budget", type=float, default=0.001)
    parser.add_argument("--min-delivered", type=float, default=0.99)
    parser.add_argument("--probe-only", action="store_true")
    args = parser.parse_args()
    args.venvs, args.out, args.db_dir = (
        path.resolve() for path in (args.venvs, args.out, args.db_dir)
    )
    if args.out.exists():
        parser.error("output exists; use a new filename to preserve earlier attempts")
    if (
        not 0 < args.min_delivered <= 1
        or not 0 <= args.error_budget < 1
        or min(
            args.duration,
            args.warmup,
            args.p99_ms,
            args.connections,
            args.client_threads,
            args.workers,
            args.cpus,
            args.memory_mb,
            args.pids_limit,
            args.rounds,
        )
        <= 0
    ):
        parser.error(
            "counts/durations/resources must be positive and error-budget in [0,1)"
        )
    args.variant_names, args.case_names = (
        args.variants.split(","),
        args.cases.split(","),
    )
    try:
        for name in args.variant_names:
            VARIANTS[name]
        args.ramps = {
            name: [int(rate) for rate in args.rates.split(",")]
            if args.rates
            else RATES[name]
            for name in args.case_names
        }
        if any(rate <= 0 for rates in args.ramps.values() for rate in rates):
            raise ValueError("rates must be positive")
    except (KeyError, ValueError) as error:
        parser.error(str(error))
    return args


def save(args, results):
    args.out.write_text(json.dumps(results, indent=2, default=str) + "\n")


def run_server(args, results, variant, round_number):
    log = args.out.parent / f"{args.out.stem}-{round_number}-{variant.name}.log"
    server = Server(args, variant, log)
    try:
        with server:
            results["servers"].append(server.metadata)
            if results["servers"][0]["image_id"] != server.metadata["image_id"]:
                raise RuntimeError("Docker image changed between comparison variants")
            if (
                results["servers"][0]["application"]["versions"]
                != server.metadata["application"]["versions"]
            ):
                raise RuntimeError(
                    "framework dependency versions differ between environments"
                )
            for name in args.case_names:
                case = CASES[name]
                case.validate(DIRECT, server.url)
                if args.probe_only:
                    continue
                for rate in args.ramps[name]:
                    prefix = (
                        f"{args.out.stem}-{round_number}-{variant.name}-{name}-{rate}"
                    )
                    row = sample(
                        args, server, case, rate, args.db_dir / (prefix + ".sqlite")
                    )
                    row.update(
                        variant=variant.name, round=round_number, server_log=log.name
                    )
                    case.validate(DIRECT, server.url)
                    results["samples"].append(row)
                    save(args, results)
                    print(
                        json.dumps(
                            {
                                key: value
                                for key, value in row.items()
                                if key not in {"resources", "oha", "warmup"}
                            }
                        ),
                        flush=True,
                    )
    finally:
        results["cleanups"].append(
            {"variant": variant.name, "round": round_number, "cleanup": server.cleanup}
        )
        save(args, results)
    if (
        not server.cleanup
        or server.cleanup.get("forced_pids")
        or server.cleanup.get("remaining_pids")
        or server.cleanup.get("error")
    ):
        raise RuntimeError("server cleanup did not finish normally")


def main():
    args = arguments()
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.db_dir.mkdir(parents=True, exist_ok=True)
    results = {
        "host_platform": platform.platform(),
        "controller_python": sys.version,
        "settings": vars(args),
        "slo": {
            "p99_ms": args.p99_ms,
            "error_budget": args.error_budget,
            "min_delivered_fraction": args.min_delivered,
            "label": "exploratory benchmark target; not a user production SLO",
        },
        "servers": [],
        "samples": [],
        "cleanups": [],
    }
    for index in range(args.rounds):
        for name in variant_order(args.variant_names, index):
            run_server(args, results, VARIANTS[name], index + 1)
    save(args, results)


if __name__ == "__main__":
    main()
