"""Render the completed multicore matrix and verify its immutable build files."""

import argparse
import hashlib
import importlib.metadata
import json
import statistics
import zipfile
from collections import defaultdict
from pathlib import Path
from urllib.parse import unquote, urlparse

NAMES = {
    "baseline-processes": "Baseline processes",
    "candidate-processes": "Candidate processes",
    "candidate-free-loops": "Candidate free-threaded loops",
    "fastapi-processes": "FastAPI processes",
}
CASES = ("cpu-inline", "cpu-sync", "io", "fanout", "mixed", "cpu-offload")


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def build_facts(version):
    native = Path(version["native_file"])
    if sha256(native.read_bytes()) != version["native_sha256"]:
        raise ValueError(f"native module changed during the run: {native}")
    package = native.parent
    sources = {
        str(path.relative_to(package)): path.read_bytes()
        for path in sorted(package.rglob("*.py"))
    }
    aggregate = b"".join(
        name.encode() + b"\0" + data + b"\0" for name, data in sources.items()
    )
    dist = next(package.parent.glob("bessemer-*.dist-info"))
    direct = json.loads((dist / "direct_url.json").read_text())
    wheel = Path(unquote(urlparse(direct["url"]).path))
    with zipfile.ZipFile(wheel) as archive:
        if sha256(archive.read("bessemer/" + native.name)) != version["native_sha256"]:
            raise ValueError(
                f"wheel no longer matches the measured native module: {wheel}"
            )
        for name, content in sources.items():
            if archive.read("bessemer/" + name) != content:
                raise ValueError(
                    f"wheel Python file differs from installed benchmark: {name}"
                )
    packages = {
        d.metadata["Name"].lower(): d.version
        for d in importlib.metadata.distributions(path=[str(package.parent)])
    }
    return {
        **version,
        "wheel_file": str(wheel),
        "wheel_sha256": sha256(wheel.read_bytes()),
        "python_package_sha256": sha256(aggregate),
        "python_files": {name: sha256(data) for name, data in sources.items()},
        "packages": {
            name: packages.get(name)
            for name in ("fastapi", "uvicorn", "uvloop", "httptools", "pydantic")
        },
    }


def table(headers, rows):
    return [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join("---" for _ in headers) + " |",
        *("| " + " | ".join(map(str, row)) + " |" for row in rows),
        "",
    ]


def grouped(samples):
    result = defaultdict(list)
    for sample in samples:
        result[
            (sample["workload"], sample["topology"]["name"], sample["topology"]["cpus"])
        ].append(sample)
    return result


def median(groups, case, name, count):
    return statistics.median(row["rps"] for row in groups[case, name, count])


def rates(groups):
    rows = []
    for case in CASES:
        for name, label in NAMES.items():
            values = [median(groups, case, name, count) for count in (1, 2, 4)]
            rows.append(
                [
                    case,
                    label,
                    *(f"{value:,.0f}" for value in values),
                    f"{values[-1] / values[0]:.2f}×",
                ]
            )
    return table(["Workload", "Topology", "1", "2", "4", "1→4"], rows)


def ratios(groups):
    rows = []
    for case in CASES:
        for count in (1, 2, 4):
            values = {name: median(groups, case, name, count) for name in NAMES}
            ratios = [
                values[left] / values[right]
                for left, right in (
                    ("candidate-processes", "baseline-processes"),
                    ("candidate-free-loops", "baseline-processes"),
                    ("candidate-processes", "fastapi-processes"),
                    ("candidate-free-loops", "fastapi-processes"),
                )
            ]
            rows.append([case, count, *(f"{value:.2f}×" for value in ratios)])
    return table(
        [
            "Workload",
            "Config",
            "Candidate / baseline",
            "Free / baseline",
            "Candidate / FastAPI",
            "Free / FastAPI",
        ],
        rows,
    )


def cpu_costs(groups):
    rows = []
    for case in CASES:
        for name, label in NAMES.items():
            ranges = []
            for count in (1, 2, 4):
                values = [
                    row["server_cpu_us_per_response"]
                    for row in groups[case, name, count]
                ]
                ranges.append(f"{min(values):,.1f}–{max(values):,.1f}")
            rows.append([case, label, *ranges])
    return table(
        [
            "Workload",
            "Topology",
            "1: CPU us/response",
            "2: CPU us/response",
            "4: CPU us/response",
        ],
        rows,
    )


def variation(groups):
    varied = []
    for (case, name, count), samples in groups.items():
        values = [row["rps"] for row in samples]
        spread = (max(values) - min(values)) / statistics.median(values) * 100
        if spread >= 10:
            varied.append((spread, case, name, count, min(values), max(values)))
    rows = [
        [case, NAMES[name], count, f"{low:,.0f}–{high:,.0f}", f"{spread:.1f}%"]
        for spread, case, name, count, low, high in sorted(varied, reverse=True)
    ]
    return [
        (
            f"**{len(varied)}/{len(groups)} groups** had a two-round throughput spread of at least 10% "
            "((maximum − minimum) / median). These ranges qualify the median ratios above."
        ),
        "",
        *table(["Workload", "Topology", "Config", "RPS range", "Spread"], rows),
    ]


def evidence(data):
    rows = data["samples"]
    warmups = [row["warmup"] for row in rows]
    errors = sum(sum(row["errors"].values()) for row in rows + warmups)
    unexpected = sum(row["unexpected_status"] for row in rows + warmups)
    bound = sum(row["client_bound"] for row in rows)
    responses = sum(row["responses"] for row in rows)
    cleanup = data["cleanups"]
    remaining = sum(len(row["remaining_pids"]) for row in cleanup)
    return [
        (
            f"Measured samples: **{len(rows)}**; measured responses: **{responses:,}**. "
            f"Warmup samples: **{len(warmups)}**. Transport errors: **{errors}**; unexpected statuses: **{unexpected}**."
        ),
        "",
        (
            f"Client-bound flags: **{bound}/{len(rows)}** measured samples (oha client CPU ≥1.7 cores). "
            f"Process-tree cleanup: **{len(cleanup)}/{len(cleanup)}** server sessions, **{remaining}** remaining tracked processes."
        ),
        "",
    ]


def final_patch(path, original):
    data = json.loads(path.read_text())
    groups = grouped(data["samples"])
    names = ("candidate-processes", "candidate-free-loops")
    expected = {
        (case, name, count)
        for case in ("cpu-sync", "cpu-offload")
        for name in names
        for count in (1, 4)
    }
    if set(groups) != expected or any(len(values) != 2 for values in groups.values()):
        raise ValueError("final report requires the complete sixteen-sample matrix")
    facts = {name: build_facts(version) for name, version in data["versions"].items()}
    (path.parent / "build-facts-final.json").write_text(
        json.dumps(facts, indent=2) + "\n"
    )
    rows = []
    for case in ("cpu-sync", "cpu-offload"):
        for name in names:
            first, fourth = median(groups, case, name, 1), median(groups, case, name, 4)
            costs = []
            for count in (1, 4):
                values = [
                    row["server_cpu_us_per_response"]
                    for row in groups[case, name, count]
                ]
                costs.append(f"{min(values):,.1f}–{max(values):,.1f}")
            rows.append(
                [
                    case,
                    NAMES[name],
                    f"{first:,.0f}",
                    f"{fourth:,.0f}",
                    f"{fourth / first:.2f}×",
                    *costs,
                    f"{fourth / median(original, case, name, 4):.2f}×",
                ]
            )
    out = [
        "## Final Python-only fixes",
        "",
        (
            "The final wheels add custom-executor timeout enforcement and prevent sync worker threads from inheriting the first request's ContextVars. "
            "The native modules are unchanged; the wheel and Python-source hashes distinguish these builds. "
            "The follow-up uses the same two workloads, configured sizes 1/4, two rounds, 2-second samples, 1-second warmups and 64 connections."
        ),
        "",
        *evidence(data),
        *table(
            [
                "Workload",
                "Topology",
                "1 RPS",
                "4 RPS",
                "1→4",
                "1 CPU us/response",
                "4 CPU us/response",
                "4 / original 4",
            ],
            rows,
        ),
        "The last column compares this follow-up with the earlier matrix, not a paired acceptance test. Both runs retain their raw sample ranges.",
        "",
    ]
    for name in names:
        info = facts[name]
        out.extend(
            [
                f"**{NAMES[name]}**: GIL enabled `{info['gil_enabled']}`; free-threaded build `{info['free_threaded_build']}`.",
                "",
                f"Wheel: `{info['wheel_file']}`.",
                "",
                "```text",
                f"wheel_sha256: {info['wheel_sha256']}",
                f"native_sha256: {info['native_sha256']}",
                f"python_package_sha256: {info['python_package_sha256']}",
                "```",
                "",
            ]
        )
    check_files = sorted(path.parent.glob("check-final*.log"))
    check_files.append(path.parent / "check-314t.log")
    checks = ", ".join(f"[{log.name}]({log.name})" for log in check_files)
    out.extend(
        [
            (
                f"Final installed validation: {checks}; [source/build manifest](manifest-final.json). "
                "CPython 3.13: 188 Rust passed/1 ignored, 814 Python passed/11 skipped, 18 harness passed/3 skipped. "
                "CPython 3.14t: 213 focused tests passed/1 skipped, GIL disabled before and after the suite."
            ),
            "",
            (
                "Follow-up evidence: [raw samples](final-scaling.json), [command](final-scaling-command.txt), "
                "[run log](final-scaling-run.log), [verified final build facts](build-facts-final.json)."
            ),
            "",
        ]
    )
    return out


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument(
        "--final", type=Path, help="append the sixteen-sample final-patch follow-up"
    )
    args = parser.parse_args()
    data = json.loads(args.input.read_text())
    groups = grouped(data["samples"])
    expected = {
        (case, name, count) for case in CASES for name in NAMES for count in (1, 2, 4)
    }
    if set(groups) != expected or any(len(values) != 2 for values in groups.values()):
        raise ValueError("report requires the complete two-round matrix")
    facts = {name: build_facts(version) for name, version in data["versions"].items()}
    (args.input.parent / "build-facts.json").write_text(
        json.dumps(facts, indent=2) + "\n"
    )
    out = [
        "# Multicore CPU/I/O diagnostics — 6 November 2026",
        "",
        (
            "Two rounds per case/configuration, 2-second samples after 1-second warmups, "
            "64 HTTP/1 keepalive connections. Medians below use both rounds."
        ),
        "",
        (
            "The 1/2/4 labels are **configured process/loop and pool sizes on an unpinned 18-CPU Mac**. "
            "They are not enforced vCPU quotas. Linux affinity enforcement was not exercised by this run. "
            "Process variants use CPython 3.13; free-threaded loops use GIL-disabled CPython 3.14t. "
            "Cross-interpreter ratios combine interpreter and topology effects; the free-threaded 1→4 ratio "
            "compares parallelism within the same interpreter/build."
        ),
        "",
        "The sleep-only route also has a fixed 64-connection/1ms workload ceiling, so its 1→4 throughput is not a CPU-scaling test.",
        "",
        (
            "Baseline is the immutable earlier local concurrency-pass wheel, not the original published PyPI wheel. "
            "Candidate GIL runs use N API processes and one CPU-offload/sync thread slot per process; "
            "free-threaded runs use one process with N loops, N CPU threads and N sync threads. "
            "Baseline/FastAPI offload uses one spawned process per API process. Baseline sync handlers "
            "retain their eight-thread default; FastAPI retains its AnyIO default."
        ),
        "",
        (
            "These frozen benchmark wheels precede the later custom-executor timeout and inherited "
            "sync-thread context fixes. Final-build focused checks/measurements are recorded separately."
        ),
        "",
        *evidence(data),
        "## Median requests/second and 1→4 scaling",
        "",
        *rates(groups),
        "## Ratios at the same configured size",
        "",
        *ratios(groups),
        "## Observed throughput variation",
        "",
        *variation(groups),
        "## Server CPU cost ranges",
        "",
        "Ranges span the two measured rounds. CPU includes supervised descendants and offload processes.",
        "",
        *cpu_costs(groups),
        "## Exact measured builds",
        "",
        (
            "Native hashes were captured before load and verified against the unchanged installed modules. "
            "Wheel contents were checked against installed Python files and native modules after the run. "
            "The Python-package hash covers sorted relative .py paths and contents separated by NUL bytes."
        ),
        "",
    ]
    for name in ("baseline-processes", "candidate-processes", "candidate-free-loops"):
        info = facts[name]
        out.extend(
            [
                f"### {NAMES[name]}",
                "",
                f"Python: `{info['python']}`. GIL enabled: `{info['gil_enabled']}`; free-threaded build: `{info['free_threaded_build']}`.",
                "",
                f"Wheel: `{info['wheel_file']}`.",
                "",
                "```text",
                f"wheel_sha256: {info['wheel_sha256']}",
                f"native_sha256: {info['native_sha256']}",
                f"python_package_sha256: {info['python_package_sha256']}",
                "```",
                "",
            ]
        )
    packages = facts["fastapi-processes"]["packages"]
    out.extend(
        [
            "FastAPI environment: "
            + ", ".join(f"{name} `{value}`" for name, value in packages.items())
            + ".",
            "",
            (
                "Raw evidence: [scaling.json](scaling.json), [exact command](scaling-command.txt), "
                "[run log](scaling-run.log), [build facts](build-facts.json), "
                "[initial 24-route probe](probe.json), [probe cleanup](probe-cleanup.json)."
            ),
            "",
        ]
    )
    output = args.output or args.input.parent / "report.md"
    if args.final is not None:
        out.extend(final_patch(args.final, groups))
    output.write_text("\n".join(out))
    print(output)


if __name__ == "__main__":
    main()
