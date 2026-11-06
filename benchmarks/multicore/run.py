"""Installed-wheel process versus free-threaded-loop scaling diagnostics."""

import argparse
import json
import platform
import sys
from dataclasses import asdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
# Reuse the existing HTTP load/error checks and process-tree CPU accounting.
sys.path.insert(0, str(HERE.parent / "concurrency"))
from lifecycle import InstalledServer, Topology, versions
from measure import http_sample
from server import DIRECT
from work import checksum
from workloads import Workload


def workloads(iterations):
    expected = {"checksum": checksum(iterations)}
    return {
        "cpu-inline": Workload("cpu-inline", "/cpu-inline", expected),
        "cpu-sync": Workload("cpu-sync", "/cpu-sync", expected),
        "io": Workload("io", "/io", {"ok": True}),
        "fanout": Workload("fanout", "/fanout", {"completed": 16}),
        "mixed": Workload(
            "mixed", "/mixed", {"checksum": 2 * checksum(iterations // 2)}
        ),
        "cpu-offload": Workload("cpu-offload", "/cpu-offload", expected),
    }


def topologies(args, cpus):
    choices = {
        "baseline": Topology(
            "baseline-processes", args.baseline_python, cpus, cpus, 1, True
        ),
        "candidate": Topology(
            "candidate-processes", args.candidate_python, cpus, cpus, 1, True
        ),
        "free": Topology(
            "candidate-free-loops", args.free_threaded_python, cpus, 1, cpus, False
        ),
    }
    if args.fastapi_python:
        choices["fastapi"] = Topology(
            "fastapi-processes", args.fastapi_python, cpus, cpus, 1, True, "fastapi"
        )
    names = args.topologies.split(",") if args.topologies else list(choices)
    return [choices[name] for name in names]


def checked(result):
    if not result["responses"] or result["unexpected_status"] or result["errors"]:
        raise RuntimeError("HTTP sample failed: " + json.dumps(result))
    return result


class Comparison:
    def __init__(self, args):
        self.args = args
        self.output = Path(args.out).resolve()
        self.output.parent.mkdir(parents=True, exist_ok=True)
        self.results = {
            "platform": platform.platform(),
            "settings": vars(args),
            "note": "Linux affinity/configuration sweep; not an emulated cgroup quota. "
            "On other platforms CPU configurations are unpinned. Same-host client.",
            "versions": {
                variant.name: versions(variant.python)
                for variant in topologies(args, args.cpu_counts[0])
            },
            "samples": [],
            "cleanups": [],
        }
        available = workloads(args.iterations)
        self.cases = (
            [available[name] for name in args.cases.split(",")]
            if args.cases
            else list(available.values())
        )

    def record(self, row):
        self.results["samples"].append(row)
        self.output.write_text(json.dumps(self.results, indent=2) + "\n")
        print(
            json.dumps(
                {
                    key: value
                    for key, value in row.items()
                    if key not in {"raw", "warmup"}
                }
            ),
            flush=True,
        )

    def sample(self, variant, round_number):
        args = self.args
        log = (
            self.output.parent
            / f"{self.output.stem}-{round_number}-{variant.cpus}-{variant.name}.log"
        )
        with log.open("w") as stream, InstalledServer(variant, stream, args) as server:
            probe = server.probe()
            if (
                not variant.gil
                and any(case.name == "cpu-offload" for case in self.cases)
                and probe["offload"] != "App.run_cpu"
            ):
                raise RuntimeError(
                    "free-threaded offload requires the candidate App.run_cpu API"
                )
            metadata = {
                "topology": asdict(variant),
                "probe": probe,
                "round": round_number,
                "server_log": log.name,
            }
            for case in self.cases if round_number % 2 else self.cases[::-1]:
                case.validate(DIRECT, f"http://127.0.0.1:{server.port}")
                if args.probe_only:
                    self.record({**metadata, "workload": case.name, "probe_only": True})
                    continue
                warmup = http_sample(args, server, case, args.warmup)
                try:
                    checked(warmup)
                except RuntimeError:
                    self.record({**metadata, **warmup, "phase": "warmup"})
                    raise
                result = http_sample(args, server, case, args.duration)
                self.record({**metadata, **result, "warmup": warmup})
                checked(result)
        self.results["cleanups"].append(
            {"topology": asdict(variant), "round": round_number, **server.cleanup}
        )
        self.output.write_text(json.dumps(self.results, indent=2) + "\n")

    def run(self):
        for index in range(self.args.rounds):
            cpus = (
                self.args.cpu_counts if index % 2 == 0 else self.args.cpu_counts[::-1]
            )
            for count in cpus:
                variants = topologies(self.args, count)
                offset = index % len(variants)
                variants = variants[offset:] + variants[:offset]
                if index % 2:
                    variants.reverse()
                for variant in variants:
                    self.sample(variant, index + 1)


def arguments():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline-python", required=True)
    parser.add_argument("--candidate-python", required=True)
    parser.add_argument("--free-threaded-python", required=True)
    parser.add_argument("--fastapi-python")
    parser.add_argument("--oha", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--cpus", default="1,2,4")
    parser.add_argument(
        "--topologies", help="comma-separated baseline,candidate,free,fastapi"
    )
    parser.add_argument(
        "--cases",
        help="comma-separated cpu-inline,cpu-sync,io,fanout,mixed,cpu-offload",
    )
    parser.add_argument(
        "--event-loop", choices=("asyncio", "bessemer"), default="asyncio"
    )
    parser.add_argument("--iterations", type=int, default=50_000)
    parser.add_argument("--rounds", type=int, default=3)
    parser.add_argument("--duration", type=int, default=2)
    parser.add_argument("--warmup", type=int, default=1)
    parser.add_argument("--connections", type=int, default=32)
    parser.add_argument("--probe-only", action="store_true")
    args = parser.parse_args()
    try:
        args.cpu_counts = [int(value) for value in args.cpus.split(",")]
        if not args.cpu_counts or any(
            not 1 <= value <= 256 for value in args.cpu_counts
        ):
            raise ValueError("CPU configurations must be integers from 1 to 256")
        topologies(args, args.cpu_counts[0])
        if args.cases:
            available = workloads(1)
            if any(name not in available for name in args.cases.split(",")):
                raise ValueError("unknown workload")
    except (ValueError, KeyError) as error:
        parser.error(str(error))
    if (
        min(args.iterations, args.rounds, args.duration, args.warmup, args.connections)
        < 1
    ):
        parser.error(
            "iteration/sample counts, durations and connections must be positive"
        )
    return args


if __name__ == "__main__":
    Comparison(arguments()).run()
