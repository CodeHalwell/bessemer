"""Print Markdown tables from run_matrix.sh/run_concurrency.sh JSON lines."""

import argparse
import json
import statistics
from collections import defaultdict
from pathlib import Path


def median(rows, key):
    values = [row[key] for row in rows if row.get(key) is not None]
    return f"{statistics.median(values):,.2f}" if values else "—"


def tables(samples):
    groups = defaultdict(list)
    for row in samples:
        groups[
            (
                row["mode"],
                row.get("mw", False),
                row["route"],
                row["workers"],
                row["conns"],
            )
        ].append(row)
    lines = []
    for (mode, middleware, route, workers, connections), group in sorted(
        groups.items()
    ):
        lines += [
            f"## {mode}: `/{route}` — {workers} worker(s), {connections} connections, MW={int(middleware)}",
            "",
            "| Framework | Samples | OK req/s | Total req/s | p50 ms | p99 ms | Non-200 | Errors |",
            "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
        ]
        for framework in sorted({row["fw"] for row in group}):
            rows = [row for row in group if row["fw"] == framework]

            non200 = sum(row["non200"] for row in rows)
            errors = sum(row["errors"] for row in rows)
            lines.append(
                f"| {framework} | {len(rows)} | {median(rows, 'ok_rps')} | {median(rows, 'rps')} | {median(rows, 'p50_ms')} | {median(rows, 'p99_ms')} | {non200} | {errors} |"
            )
        lines.append("")
    lines += [
        "Rates and latencies are medians across samples; status/error counts are sums.",
        "OK req/s counts only HTTP 200. `/raise` deliberately returns 404, so its OK rate is zero.",
        "Check non-200/errors before comparing rates. Shared-host rates are exploratory, not the M0 gate.",
    ]
    return "\n".join(lines) + "\n"


def main():
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("files", type=Path, nargs="+")
    args = cli.parse_args()
    rows = [
        json.loads(line)
        for path in args.files
        for line in path.read_text().splitlines()
        if line.strip()
    ]
    print(tables(rows), end="")


if __name__ == "__main__":
    main()
