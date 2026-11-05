"""Turn a run_compare.py results file into Markdown tables.

    python summarise.py results.json > tables.md

Each table gives the median requests per second for every framework, with the
range across rounds, and Bessemer's ratio to the others. A cell is marked !
when a sample had non-200 responses, and * when a sample was client-bound
(the load generator, not the server, may have set the number). A ratio is
marked * when any cell behind it carries either mark. The CPU columns give
the median busy fraction of the client and server CPUs.
"""

from __future__ import annotations

import json
import statistics
import sys
from collections import defaultdict


def load(path: str) -> tuple[dict, list[dict]]:
    with open(path) as handle:
        data = json.load(handle)
    if isinstance(data, list):  # same-host format before two-machine support
        return {}, data
    return data.get("meta", {}), data["samples"]


NAMES = {"bessemer": "Bessemer", "fastapi": "FastAPI", "axum": "Axum"}


def thousands(value: float) -> str:
    return f"{value / 1000:.1f}k"


def tables(meta: dict, samples: list[dict]) -> str:
    groups: dict[tuple, list[dict]] = defaultdict(list)
    order: dict[str, list] = {"config": [], "framework": [], "route": []}
    for sample in samples:
        groups[sample["config"], sample["framework"], sample["route"]].append(sample)
        for key, seen in order.items():
            if sample[key] not in seen:
                seen.append(sample[key])
    frameworks = order["framework"]
    others = [name for name in frameworks if name != "bessemer"]
    lines: list[str] = []
    if meta:
        client, server = meta.get("client", {}), meta.get("server", {})
        lines += [
            f"Mode: {meta.get('mode')}. Started {meta.get('started')}.",
            "",
            (
                f"- Client: {client.get('cpus')} CPUs, {client.get('cpu_model')}, "
                f"{client.get('oha', 'oha')}."
            ),
            (
                f"- Server: {server.get('cpus')} CPUs, {server.get('cpu_model')}, "
                f"kernel {server.get('kernel')}."
            ),
            f"- Server packages: {json.dumps(server.get('versions', {}))}.",
            f"- Settings: {json.dumps(meta.get('settings', {}))}.",
            "",
        ]
    for config in order["config"]:
        header = ["Route", *(NAMES.get(name, name) for name in frameworks)]
        header += [f"Bessemer / {NAMES.get(name, name)}" for name in others]
        header += ["Client CPU", "Server CPU"]
        lines += [f"## {config}", "", "| " + " | ".join(header) + " |"]
        lines.append("| --- |" + " ---: |" * (len(header) - 1))
        for route in order["route"]:
            row = [f"`/{route}`"]
            medians = {}
            suspect = False
            client_cpu: list[float] = []
            server_cpu: list[float] = []
            for framework in frameworks:
                group = groups.get((config, framework, route), [])
                if not group:
                    row.append("-")
                    continue
                rps = [sample["rps"] for sample in group]
                medians[framework] = statistics.median(rps)
                cell = f"{thousands(medians[framework])} ({thousands(min(rps))}-{thousands(max(rps))})"
                if any(sample.get("non_200") for sample in group):
                    cell += "!"
                    suspect = True
                if any(sample.get("client_bound") for sample in group):
                    cell += "*"
                    suspect = True
                row.append(cell)
                client_cpu += [s["client_cpu"] for s in group if "client_cpu" in s]
                server_cpu += [s["server_cpu"] for s in group if "server_cpu" in s]
            for name in others:
                if "bessemer" in medians and medians.get(name):
                    ratio = medians["bessemer"] / medians[name]
                    text = f"{ratio:.1f}x" if ratio >= 1 else f"{ratio:.0%}"
                    row.append(text + ("*" if suspect else ""))
                else:
                    row.append("-")
            row.append(f"{statistics.median(client_cpu):.0%}" if client_cpu else "-")
            row.append(f"{statistics.median(server_cpu):.0%}" if server_cpu else "-")
            lines.append("| " + " | ".join(row) + " |")
        lines.append("")
    bad = [s for s in samples if s.get("non_200")]
    if bad:
        lines.append(
            f"! {len(bad)} samples had non-200 responses, so their throughput "
            "is not comparable; the ratios they feed are marked *."
        )
    if any(s.get("client_bound") for s in samples):
        lines.append(
            "\\* At least one sample was client-bound (load generator CPUs "
            "at 85% or more), so the server may be faster than shown."
        )
    return "\n".join(lines) + "\n"


def main():
    if len(sys.argv) != 2:
        sys.exit("usage: summarise.py results.json")
    sys.stdout.write(tables(*load(sys.argv[1])))


if __name__ == "__main__":
    main()
