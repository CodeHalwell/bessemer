from __future__ import annotations

import argparse
import json
import math
import shutil
import statistics
import subprocess
import sys
from dataclasses import dataclass
from urllib.parse import urljoin
from urllib.request import urlopen

MAX_SPREAD = 0.03
STOP_RATIO = 0.40
TARGET_RATIO = 0.60


@dataclass(frozen=True)
class Samples:
    name: str
    rates: tuple[float, ...]

    def __post_init__(self) -> None:
        if not self.rates or any(
            not math.isfinite(rate) or rate <= 0 for rate in self.rates
        ):
            raise ValueError(f"{self.name}: rates must be positive and finite")

    @property
    def median(self) -> float:
        return statistics.median(self.rates)

    @property
    def spread(self) -> float:
        return (max(self.rates) - min(self.rates)) / self.median

    def validate(self) -> None:
        if self.spread > MAX_SPREAD:
            raise ValueError(
                f"{self.name}: spread {self.spread:.2%} exceeds 3%; discard and rerun"
            )


@dataclass(frozen=True)
class Comparison:
    rust: Samples
    python: Samples

    @property
    def ratio(self) -> float:
        return self.python.median / self.rust.median

    @property
    def stop(self) -> bool:
        return self.ratio < STOP_RATIO

    def validate(self) -> None:
        self.rust.validate()
        self.python.validate()


class LoadGenerator:
    def __init__(self, connections: int) -> None:
        executable = shutil.which("oha")
        if executable is None:
            raise FileNotFoundError("oha is required for the M0 benchmark")
        self.executable = executable
        self.connections = connections

    @staticmethod
    def rate(output: str) -> float:
        data = json.loads(output)
        try:
            summary = data["summary"]
            statuses = data["statusCodeDistribution"]
            errors = data["errorDistribution"]
            rate = summary["requestsPerSec"]
            success = summary["successRate"]
        except (KeyError, TypeError) as error:
            raise ValueError("invalid oha JSON output") from error
        if not isinstance(statuses, dict) or not isinstance(errors, dict):
            raise TypeError("invalid oha status or error distribution")
        count = statuses.get("200")
        if (
            isinstance(success, bool)
            or success != 1
            or errors
            or set(statuses) != {"200"}
            or isinstance(count, bool)
            or not isinstance(count, int)
            or count <= 0
        ):
            raise ValueError("oha reported failed requests or non-200 responses")
        if isinstance(rate, bool) or not isinstance(rate, (int, float)):
            raise TypeError("oha returned an invalid request rate")
        if not math.isfinite(rate) or rate <= 0:
            raise ValueError("oha returned a non-positive or non-finite request rate")
        return float(rate)

    def measure(self, url: str, duration: str) -> float:
        command = [
            self.executable,
            "-z",
            duration,
            "-c",
            str(self.connections),
            "-w",
            "-t",
            "5s",
            "--http-version",
            "1.1",
            "--no-tui",
            "--output-format",
            "json",
            url,
        ]
        result = subprocess.run(command, capture_output=True, text=True, check=False)
        if result.returncode:
            detail = result.stderr.strip() or result.stdout.strip()
            raise RuntimeError(f"oha failed for {url}: {detail}")
        return self.rate(result.stdout)


def positive_int(value: str) -> int:
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return number


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Compare M0 Rust and Python throughput"
    )
    parser.add_argument("rust_url")
    parser.add_argument("python_url")
    parser.add_argument("--warmup", default="30s")
    parser.add_argument("--duration", default="60s")
    parser.add_argument("--runs", type=positive_int, default=5)
    parser.add_argument("--connections", type=positive_int, default=50)
    return parser.parse_args()


def verify_endpoint(name: str, url: str) -> None:
    with urlopen(url, timeout=5) as response:
        if response.read(1024) != b'{"ok":true,"count":42}':
            raise ValueError(f"{name}: unexpected response body")
    with urlopen(urljoin(url, "/__m0__/mode"), timeout=5) as response:
        if response.read(32).decode("ascii") != name:
            raise ValueError(f"{name}: endpoint is serving the wrong mode")


def collect(args: argparse.Namespace) -> Comparison:
    load = LoadGenerator(args.connections)
    endpoints = (("rust", args.rust_url), ("python", args.python_url))
    rates: dict[str, list[float]] = {"rust": [], "python": []}
    for name, url in endpoints:
        verify_endpoint(name, url)
        print(f"Warming {name} for {args.warmup}", flush=True)
        load.measure(url, args.warmup)
    for index in range(args.runs):
        order = endpoints if index % 2 == 0 else tuple(reversed(endpoints))
        for name, url in order:
            rate = load.measure(url, args.duration)
            rates[name].append(rate)
            print(f"{name} {index + 1}/{args.runs}: {rate:,.0f} req/s", flush=True)
    return Comparison(
        Samples("rust", tuple(rates["rust"])), Samples("python", tuple(rates["python"]))
    )


def main() -> int:
    try:
        comparison = collect(arguments())
        for sample in (comparison.rust, comparison.python):
            print(
                f"{sample.name}: median {sample.median:,.0f} req/s; spread {sample.spread:.2%}"
            )
        comparison.validate()
        print(
            f"Observed Python/Rust: {comparison.ratio:.2%}; "
            f"{'below' if comparison.stop else 'at or above'} the 40% stop threshold"
        )
        print(
            f"Observed ratio is {'at or above' if comparison.ratio >= TARGET_RATIO else 'below'} 60%"
        )
        print(
            "Acceptance requires five 60s runs, 30s warm-up, dedicated cores and a separate load generator."
        )
        return int(comparison.stop)
    except (OSError, RuntimeError, TypeError, ValueError) as error:
        print(f"Benchmark failed: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
