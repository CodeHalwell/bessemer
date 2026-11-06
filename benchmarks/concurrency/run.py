"""Compare published and candidate wheels across the API capability surface."""

import argparse
import json
import platform
from pathlib import Path

from measure import http_sample, websocket_sample
from server import DIRECT, Server, Variant, resolve_executable
from workloads import WORKLOADS


class Comparison:
    def __init__(self, args):
        self.args = args
        self.output = Path(args.out).resolve()
        self.output.parent.mkdir(parents=True, exist_ok=True)
        self.variants = [
            Variant("rc1-default", args.baseline_python),
            Variant("candidate-default", args.candidate_python),
            Variant("rc1-rust", args.baseline_python, loop="bessemer"),
            Variant("candidate-rust", args.candidate_python, loop="bessemer"),
            Variant("fastapi", args.baseline_python, framework="fastapi"),
        ]
        names = (
            args.variants.split(",")
            if args.variants
            else [v.name for v in self.variants]
        )
        self.variants = [variant for variant in self.variants if variant.name in names]
        available = {workload.name: workload for workload in WORKLOADS}
        routes = args.routes.split(",") if args.routes else list(available)
        self.workloads = [available[name] for name in routes if name != "websocket"]
        self.websocket = not args.routes or "websocket" in routes
        if not self.variants:
            raise ValueError("no comparison variants selected")
        self.results = {
            "platform": platform.platform(),
            "settings": vars(args),
            "versions": {v.name: v.versions() for v in self.variants},
            "cpu_note": "Unpinned process-tree ps CPU deltas; same-host client.",
            "samples": [],
        }

    def record(self, result, variant, round_number):
        result.update(variant=variant.name, round=round_number)
        self.results["samples"].append(result)
        temporary = self.output.with_suffix(self.output.suffix + ".tmp")
        temporary.write_text(json.dumps(self.results, indent=2) + "\n")
        temporary.replace(self.output)
        print(
            json.dumps({key: value for key, value in result.items() if key != "raw"}),
            flush=True,
        )
        if result.get("unexpected_status") or result["errors"]:
            raise RuntimeError("sample failed; partial results preserved")

    def sample_server(self, server, round_number):
        args = self.args
        for workload in self.workloads:
            workload.validate(DIRECT, f"http://127.0.0.1:{server.port}")
            warmup = http_sample(args, server, workload, args.warmup)
            if warmup["unexpected_status"] or warmup["errors"]:
                warmup["phase"] = "warmup"
                self.record(warmup, server.variant, round_number)
            self.record(
                http_sample(args, server, workload, args.duration),
                server.variant,
                round_number,
            )
        if self.websocket:
            websocket_sample(args, server, args.warmup)
            self.record(
                websocket_sample(args, server, args.duration),
                server.variant,
                round_number,
            )

    def run(self):
        for index in range(self.args.rounds):
            offset = index % len(self.variants)
            for variant in self.variants[offset:] + self.variants[:offset]:
                log = self.output.parent / f"{index + 1}-{variant.name}.log"
                with log.open("w") as stream, Server(variant, stream) as server:
                    self.sample_server(server, index + 1)


def arguments():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline-python", required=True)
    parser.add_argument("--candidate-python", required=True)
    parser.add_argument("--oha", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--rounds", type=int, default=3)
    parser.add_argument("--duration", type=int, default=2)
    parser.add_argument("--warmup", type=int, default=1)
    parser.add_argument("--connections", type=int, default=64)
    parser.add_argument("--ws-connections", type=int, default=32)
    parser.add_argument("--routes")
    parser.add_argument("--variants")
    args = parser.parse_args()
    for field in ("baseline_python", "candidate_python", "oha"):
        setattr(args, field, resolve_executable(getattr(args, field)))
    if (
        min(
            args.rounds,
            args.duration,
            args.warmup,
            args.connections,
            args.ws_connections,
        )
        < 1
    ):
        parser.error("sample counts, durations and connections must be positive")
    return args


if __name__ == "__main__":
    Comparison(arguments()).run()
