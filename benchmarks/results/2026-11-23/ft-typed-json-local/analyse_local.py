"""Summarise the local before/after runs for free-threaded typed JSON.

`samples.csv` holds every sample of four `run_compare.py` runs: wheels built
from main and from the candidate, each on standard and free-threaded CPython
3.14. `interleaved.csv` holds a second measurement of Bessemer alone, in which
the two builds alternate on one interpreter. This prints the Markdown tables
used in `report.md`:

    python3 analyse_local.py > tables.md

`encoder-experiment.csv` is the same interleaved measurement of the candidate
against the candidate with `nested-lock-encoder.patch` applied, on
free-threaded Python.

`--from-raw DIR` first rewrites the CSV files from the harness's result files
(`main-standard.json`, `main-free-threaded.json`, `candidate-standard.json`
and `candidate-free-threaded.json`), leaving out the local command lines, and
from the interleaved runs' `ab-*.txt` lines of pair, environment, route,
replies per second, p50 and p99.

A sample qualifies when its warmup and timed load returned only HTTP 200 with
no transport errors and the load generator stayed below the harness's client
CPU bound. Ratios pair the same numbered round (or the same interleaved pair)
and are reported as the median with the minimum and maximum; a group with an
unqualified sample gives no ratio.
"""

from __future__ import annotations

import argparse
import csv
import json
import statistics
from pathlib import Path

HERE = Path(__file__).resolve().parent
BUILDS = ("main", "candidate")
MODES = ("standard", "free-threaded")
ROUTES = (
    "json",
    "items",
    "echo",
    "cpu-inline",
    "cpu-sync",
    "large-echo",
    "bytes",
    "models256",
)
FOCUS = ("echo", "large-echo", "models256")
BESSEMER = "bessemer-auto"
FASTAPI = "fastapi-standard"
AXUM = "axum"
INTERLEAVED = ("python", "pair", "build", "route", "ok_rps", "p50_ms", "p99_ms")
COLUMNS = (
    "build",
    "python",
    "round",
    "framework",
    "route",
    "connections",
    "ok_rps",
    "successful_responses",
    "non_200",
    "errors",
    "warmup_successful_responses",
    "warmup_non_200",
    "warmup_errors",
    "elapsed_seconds",
    "p50_ms",
    "p99_ms",
    "server_cpu",
    "client_cpu",
    "client_bound",
    "response_body_bytes",
    "body_sha256_before",
    "body_sha256_after",
)


def from_raw(directory: Path) -> None:
    rows = []
    for build in BUILDS:
        for mode in MODES:
            data = json.loads((directory / f"{build}-{mode}.json").read_text())
            for sample in data["samples"]:
                warmup = sample.get("warmup") or {}
                rows.append(
                    {
                        "build": build,
                        "python": mode,
                        "warmup_successful_responses": warmup.get(
                            "successful_responses"
                        ),
                        "warmup_non_200": warmup.get("non_200"),
                        "warmup_errors": warmup.get("errors"),
                        "body_sha256_before": (sample.get("body_before") or {}).get(
                            "sha256"
                        ),
                        "body_sha256_after": (sample.get("body_after") or {}).get(
                            "sha256"
                        ),
                    }
                    | {
                        name: sample.get(name)
                        for name in COLUMNS
                        if name in sample and name != "python"
                    }
                )
    with (HERE / "samples.csv").open("w", newline="") as handle:
        writer = csv.DictWriter(handle, COLUMNS, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
    # Environment names in the interleaved runs, to the build each held.
    builds = {"base": "main", "venv": "candidate", "p1": "candidate"}
    for target, sources, names in (
        (
            "interleaved.csv",
            (
                ("standard", "ab-standard.txt"),
                ("free-threaded", "ab-free-threaded.txt"),
            ),
            builds,
        ),
        (
            "encoder-experiment.csv",
            (("free-threaded", "ab-encoder.txt"),),
            builds | {"venv": "nested-lock-encoder"},
        ),
    ):
        lines = []
        for mode, name in sources:
            for line in (directory / name).read_text().splitlines():
                pair, environment, route, rate, p50, p99 = line.split()
                build = names[environment.split("-")[0]]
                lines.append((mode, pair, build, route, rate, p50, p99))
        with (HERE / target).open("w", newline="") as handle:
            writer = csv.writer(handle, lineterminator="\n")
            writer.writerow(INTERLEAVED)
            writer.writerows(lines)


def number(text: str) -> float | None:
    return float(text) if text not in ("", "None") else None


def qualified(sample: dict) -> bool:
    return (
        bool(number(sample["ok_rps"]))
        and sample["errors"] == "0"
        and sample["non_200"] == "0"
        and sample["warmup_errors"] == "0"
        and sample["warmup_non_200"] == "0"
        and sample["client_bound"] == "False"
    )


def load() -> dict:
    """(build, mode, framework, route) -> samples ordered by round."""
    groups: dict = {}
    with (HERE / "samples.csv").open(newline="") as handle:
        for sample in csv.DictReader(handle):
            key = (
                sample["build"],
                sample["python"],
                sample["framework"],
                sample["route"],
            )
            groups.setdefault(key, []).append(sample)
    for samples in groups.values():
        samples.sort(key=lambda sample: int(sample["round"]))
    return groups


def rates(groups: dict, key: tuple) -> list[float] | None:
    samples = groups.get(key)
    if not samples or not all(qualified(sample) for sample in samples):
        return None
    return [float(sample["ok_rps"]) for sample in samples]


def median(values: list[float] | None) -> float | None:
    return statistics.median(values) if values else None


def ratio(numerator: list[float] | None, denominator: list[float] | None):
    """Median and range of the paired ratios, or None."""
    if not numerator or not denominator or len(numerator) != len(denominator):
        return None
    pairs = [top / bottom for top, bottom in zip(numerator, denominator, strict=True)]
    return statistics.median(pairs), min(pairs), max(pairs)


def show_rate(value: float | None) -> str:
    return "n/a" if value is None else f"{value:,.0f}"


def show_ratio(value) -> str:
    if value is None:
        return "n/a"
    middle, low, high = value
    return f"{middle:.3f}× ({low:.3f}–{high:.3f})"


def field(groups: dict, key: tuple, name: str) -> str:
    samples = groups.get(key)
    if not samples or not all(qualified(sample) for sample in samples):
        return "n/a"
    values = [number(sample[name]) for sample in samples]
    values = [value for value in values if value is not None]
    return f"{statistics.median(values):.3f}" if values else "n/a"


def cpu_per_million(groups: dict, key: tuple) -> str:
    """Server CPU seconds per million replies on the one pinned core."""
    samples = groups.get(key)
    if not samples or not all(qualified(sample) for sample in samples):
        return "n/a"
    values = [
        float(sample["server_cpu"]) / float(sample["ok_rps"]) * 1e6
        for sample in samples
        if number(sample["server_cpu"]) is not None
    ]
    return f"{statistics.median(values):.2f}" if values else "n/a"


def matrix_tables(groups: dict) -> None:
    samples = [sample for group in groups.values() for sample in group]
    clean = sum(qualified(sample) for sample in samples)
    replies = sum(int(sample["successful_responses"] or 0) for sample in samples)
    print(
        f"Qualified {clean}/{len(samples)} samples; "
        f"{replies:,} timed HTTP 200 replies.\n"
    )

    print("### Free-threaded over standard throughput, before and after\n")
    print("| Route | Main Bessemer | Candidate Bessemer | FastAPI | Axum control |")
    print("| --- | ---: | ---: | ---: | ---: |")
    for route in ROUTES:
        cells = [
            show_ratio(
                ratio(
                    rates(groups, (build, "free-threaded", framework, route)),
                    rates(groups, (build, "standard", framework, route)),
                )
            )
            for build, framework in (
                ("main", BESSEMER),
                ("candidate", BESSEMER),
                ("candidate", FASTAPI),
                ("candidate", AXUM),
            )
        ]
        print(f"| {route} | " + " | ".join(cells) + " |")

    print("\n### Candidate over main, same interpreter\n")
    print(
        "| Python | Route | Main replies/s | Candidate replies/s | Candidate / main "
        "| Normalised to Axum |"
    )
    print("| --- | --- | ---: | ---: | ---: | ---: |")
    for mode in MODES:
        for route in ROUTES:
            before = rates(groups, ("main", mode, BESSEMER, route))
            after = rates(groups, ("candidate", mode, BESSEMER, route))
            before_axum = rates(groups, ("main", mode, AXUM, route))
            after_axum = rates(groups, ("candidate", mode, AXUM, route))
            normalised = None
            if before and after and before_axum and after_axum:
                normalised = ratio(
                    [a / c for a, c in zip(after, after_axum, strict=True)],
                    [b / c for b, c in zip(before, before_axum, strict=True)],
                )
            print(
                f"| {mode} | {route} | {show_rate(median(before))} | "
                f"{show_rate(median(after))} | {show_ratio(ratio(after, before))} | "
                f"{show_ratio(normalised)} |"
            )

    print("\n### Candidate successful HTTP replies per second, one core\n")
    print(
        "| Python | Route | Bessemer | FastAPI | Axum | Bessemer / FastAPI "
        "| Bessemer / Axum |"
    )
    print("| --- | --- | ---: | ---: | ---: | ---: | ---: |")
    for mode in MODES:
        for route in ROUTES:
            bessemer = rates(groups, ("candidate", mode, BESSEMER, route))
            fastapi = rates(groups, ("candidate", mode, FASTAPI, route))
            axum = rates(groups, ("candidate", mode, AXUM, route))
            print(
                f"| {mode} | {route} | {show_rate(median(bessemer))} | "
                f"{show_rate(median(fastapi))} | {show_rate(median(axum))} | "
                f"{show_ratio(ratio(bessemer, fastapi))} | "
                f"{show_ratio(ratio(bessemer, axum))} |"
            )

    print("\n### Bessemer latency and CPU on the affected routes\n")
    print("| Python | Route | Build | p50 ms | p99 ms | CPU s / million |")
    print("| --- | --- | --- | ---: | ---: | ---: |")
    for mode in MODES:
        for route in FOCUS:
            for build in BUILDS:
                key = (build, mode, BESSEMER, route)
                print(
                    f"| {mode} | {route} | {build} | {field(groups, key, 'p50_ms')} | "
                    f"{field(groups, key, 'p99_ms')} | {cpu_per_million(groups, key)} |"
                )


def interleaved_table(name: str, title: str, first: str, second: str) -> None:
    path = HERE / name
    if not path.exists():
        return
    groups: dict = {}
    with path.open(newline="") as handle:
        for row in csv.DictReader(handle):
            key = (row["python"], row["route"], row["build"])
            groups.setdefault(key, {})[int(row["pair"])] = row
    print(f"\n### {title}\n")
    print(
        f"| Python | Route | Pairs | {first} replies/s | {second} replies/s "
        f"| {second} / {first} | Pairs faster | {first} p99 ms | {second} p99 ms |"
    )
    print("| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |")
    for mode in MODES:
        for route in ("json", *FOCUS):
            before = groups.get((mode, route, first), {})
            after = groups.get((mode, route, second), {})
            pairs = sorted(set(before) & set(after))
            if not pairs:
                continue
            old = [float(before[pair]["ok_rps"]) for pair in pairs]
            new = [float(after[pair]["ok_rps"]) for pair in pairs]
            faster = sum(after > before for before, after in zip(old, new, strict=True))
            p99 = [
                statistics.median(float(rows[pair]["p99_ms"]) for pair in pairs)
                for rows in (before, after)
            ]
            print(
                f"| {mode} | {route} | {len(pairs)} | {show_rate(median(old))} | "
                f"{show_rate(median(new))} | {show_ratio(ratio(new, old))} | "
                f"{faster}/{len(pairs)} | {p99[0]:.3f} | {p99[1]:.3f} |"
            )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--from-raw", type=Path)
    args = parser.parse_args()
    if args.from_raw:
        from_raw(args.from_raw)
    matrix_tables(load())
    interleaved_table(
        "interleaved.csv",
        "Interleaved Bessemer builds, one core",
        "main",
        "candidate",
    )
    interleaved_table(
        "encoder-experiment.csv",
        "Nested-lock encoder experiment, interleaved, one core",
        "candidate",
        "nested-lock-encoder",
    )


if __name__ == "__main__":
    main()
