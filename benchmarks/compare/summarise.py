"""Report successful throughput, withholding comparisons that did not qualify.

    python summarise.py results.json
    python summarise.py --base base.json --head head.json --title 'CI throughput'

Old results remain readable. Missing correctness, warmup or CPU observations
are reported as unqualified rather than treated as zero failures.
"""

from __future__ import annotations

import argparse
import json
import statistics
from collections import defaultdict

from measurement import eligibility_reasons, finite

NAMES = {
    "bessemer": "Bessemer",
    "bessemer-rust": "Bessemer (Rust loop)",
    "bessemer-uring": "Bessemer (io_uring)",
    "fastapi": "FastAPI",
    "axum": "Axum",
}


def load(path: str) -> tuple[dict, list[dict]]:
    with open(path) as handle:
        data = json.load(handle)
    if isinstance(data, list):  # same-host format before two-machine support
        meta, samples = {}, data
    elif isinstance(data, dict):
        meta, samples = data.get("meta", {}), data.get("samples")
    else:
        raise TypeError("result must contain a sample list")
    if not isinstance(meta, dict) or not isinstance(meta.get("settings", {}), dict):
        raise TypeError("result metadata must be an object")
    if not isinstance(samples, list) or any(
        not isinstance(sample, dict)
        or any(
            not isinstance(sample.get(key), str)
            for key in ("config", "framework", "route")
        )
        for sample in samples
    ):
        raise ValueError("result must contain samples with workload identities")
    return meta, samples


def group_reasons(meta, group):
    if not group:
        return ["missing sample"]
    reasons = [reason for sample in group for reason in eligibility_reasons(sample)]
    rounds = [sample.get("round") for sample in group]
    valid_rounds = all(
        isinstance(value, int) and not isinstance(value, bool) and value >= 0
        for value in rounds
    )
    if not valid_rounds:
        reasons.append("round not recorded")
    elif len(set(rounds)) != len(rounds):
        reasons.append("duplicate round")
    expected = meta.get("settings", {}).get("rounds")
    if (
        isinstance(expected, int)
        and valid_rounds
        and expected > 0
        and set(rounds) != set(range(expected))
    ):
        reasons.append("incomplete planned rounds")
    return list(dict.fromkeys(reasons))


def paired_ratios(left_meta, left, right_meta, right):
    reasons = ["base " + reason for reason in group_reasons(left_meta, left)]
    reasons += ["head " + reason for reason in group_reasons(right_meta, right)]
    if reasons:
        return None, reasons
    if {sample.get("round") for sample in left} != {
        sample.get("round") for sample in right
    }:
        reasons.append("unmatched rounds")
    if reasons:
        return None, reasons
    before = {sample["round"]: sample["ok_rps"] for sample in left}
    ratios = [sample["ok_rps"] / before[sample["round"]] for sample in right]
    return statistics.median(ratios), []


def rate(group, compact=False):
    values = [sample["ok_rps"] for sample in group if finite(sample.get("ok_rps"))]
    if not values:
        return "—"
    if not compact:
        return f"{statistics.median(values):,.0f}"
    thousands = lambda value: f"{value / 1000:.1f}k"
    return (
        f"{thousands(statistics.median(values))} "
        f"({thousands(min(values))}-{thousands(max(values))})"
    )


def cell(meta, group, compact=False):
    value = rate(group, compact)
    return value + ("†" if group_reasons(meta, group) else "")


def tables(meta: dict, samples: list[dict]) -> str:
    groups = defaultdict(list)
    order = {"config": [], "framework": [], "route": []}
    for sample in samples:
        groups[sample["config"], sample["framework"], sample["route"]].append(sample)
        for key, seen in order.items():
            if sample[key] not in seen:
                seen.append(sample[key])
    frameworks = order["framework"]
    others = [name for name in frameworks if not name.startswith("bessemer")]
    lines = []
    if meta:
        client, server = meta.get("client", {}), meta.get("server", {})
        lines += [
            f"Mode: {meta.get('mode')}. Started {meta.get('started')}.",
            "",
            f"- Client: {client.get('cpus')} CPUs, {client.get('cpu_model')}, {client.get('oha', 'oha')}.",
            f"- Server: {server.get('cpus')} CPUs, {server.get('cpu_model')}, kernel {server.get('kernel')}.",
            f"- Server packages: {json.dumps(server.get('versions', {}))}.",
            f"- Settings: {json.dumps(meta.get('settings', {}))}.",
            "",
        ]
    withheld = []
    for config in order["config"]:
        header = ["Route", *(NAMES.get(name, name) for name in frameworks)]
        header += [f"Bessemer / {NAMES.get(name, name)}" for name in others]
        header += ["Client CPU", "Server CPU"]
        lines += [f"## {config}", "", "| " + " | ".join(header) + " |"]
        lines.append("| --- |" + " ---: |" * (len(header) - 1))
        for route in order["route"]:
            row = [f"`/{route}`"]
            cpu = {"client_cpu": [], "server_cpu": []}
            for framework in frameworks:
                group = groups.get((config, framework, route), [])
                row.append(cell(meta, group, compact=True))
                reasons = group_reasons(meta, group)
                if reasons:
                    withheld.append(
                        f"{config}, {framework}, /{route}: {'; '.join(reasons)}"
                    )
                for key, values in cpu.items():
                    values += [
                        sample[key] for sample in group if finite(sample.get(key))
                    ]
            for name in others:
                ratio, reasons = paired_ratios(
                    meta,
                    groups.get((config, name, route), []),
                    meta,
                    groups.get((config, "bessemer", route), []),
                )
                row.append(
                    (f"{ratio:.1f}x" if ratio >= 1 else f"{ratio:.0%}")
                    if ratio is not None
                    else "withheld"
                )
                if reasons:
                    withheld.append(
                        f"{config}, Bessemer / {name}, /{route}: {'; '.join(reasons)}"
                    )
            row += [
                f"{statistics.median(values):.0%}" if values else "—"
                for values in cpu.values()
            ]
            lines.append("| " + " | ".join(row) + " |")
        lines.append("")
    lines.append(
        "Rates count HTTP 200 responses per elapsed second, including drain time."
    )
    lines.append(
        "† Unqualified samples are shown for diagnosis; their comparisons are withheld."
    )
    lines.extend("- " + reason for reason in withheld)
    return "\n".join(lines) + "\n"


def comparison(base_meta, base, head_meta, head, title="Throughput comparison"):
    groups = [defaultdict(list), defaultdict(list)]
    for index, samples in enumerate((base, head)):
        for sample in samples:
            groups[index][
                sample["config"], sample["framework"], sample["route"]
            ].append(sample)
    lines = [
        f"### {title}",
        "",
        "Successful HTTP 200 req/s; deadline requests drain before sampling ends. Shared runners are exploratory.",
        "",
        "| Config | Loop | Route | Base OK req/s | Head OK req/s | Change | Qualification |",
        "| --- | --- | --- | ---: | ---: | ---: | --- |",
    ]
    for key in sorted(set(groups[0]) | set(groups[1])):
        before, after = (group.get(key, []) for group in groups)
        ratio, reasons = paired_ratios(base_meta, before, head_meta, after)
        change = f"{(ratio - 1) * 100:+.1f}%" if ratio is not None else "withheld"
        qualification = "; ".join(reasons).replace("|", "\\|") or "qualified"
        config, framework, route = key
        lines.append(
            f"| {config} | {NAMES.get(framework, framework)} | {route} | "
            f"{cell(base_meta, before)} | {cell(head_meta, after)} | {change} | {qualification} |"
        )
    for name, meta, samples in (("base", base_meta, base), ("head", head_meta, head)):
        if not samples:
            lines.append(
                f"\nComparison withheld: {name} {meta.get('load_error', 'has no samples')}."
            )
    lines += ["", "† Unqualified; no percentage change is published for this pair."]
    return "\n".join(lines) + "\n"


def optional_load(path):
    try:
        return load(path)
    except (OSError, ValueError, KeyError, TypeError) as error:
        return {"load_error": f"results unavailable ({type(error).__name__})"}, []


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", nargs="?")
    parser.add_argument("--base")
    parser.add_argument("--head")
    parser.add_argument("--title", default="Throughput comparison")
    args = parser.parse_args(argv)
    if args.base and args.head and not args.path:
        print(
            comparison(
                *optional_load(args.base), *optional_load(args.head), args.title
            ),
            end="",
        )
    elif args.path and not (args.base or args.head):
        print(tables(*load(args.path)), end="")
    else:
        parser.error("provide results.json, or both --base and --head")


if __name__ == "__main__":
    main()
