"""Render before/after throughput and CPU costs from the recorded samples."""

import argparse
import json
import statistics
from pathlib import Path


class Summary:
    def __init__(self, path):
        self.data = json.loads(Path(path).read_text())
        self.samples = self.data["samples"]
        self.workloads = list(
            dict.fromkeys(sample["workload"] for sample in self.samples)
        )

    def rows(self, workload, variant):
        return [
            sample
            for sample in self.samples
            if sample["workload"] == workload and sample["variant"] == variant
        ]

    def median(self, workload, variant, field):
        return statistics.median(
            sample[field] for sample in self.rows(workload, variant)
        )

    def throughput(self, workload, variant):
        field = "messages_per_second" if workload == "websocket" else "rps"
        values = [sample[field] for sample in self.rows(workload, variant)]
        return (
            f"{statistics.median(values):,.0f} [{min(values):,.0f}–{max(values):,.0f}]"
        )

    def changes(self, mode):
        before, after = f"rc1-{mode}", f"candidate-{mode}"
        print(f"\n## {mode.capitalize()} loop\n")
        print(
            "| Capability | rc1 throughput [range] | Candidate throughput [range] | Change | CPU µs/response, rc1 → candidate |"
        )
        print("| --- | ---: | ---: | ---: | ---: |")
        for workload in self.workloads:
            field = "messages_per_second" if workload == "websocket" else "rps"
            ratio = self.median(workload, after, field) / self.median(
                workload, before, field
            )
            cpu_before = self.median(workload, before, "server_cpu_us_per_response")
            cpu_after = self.median(workload, after, "server_cpu_us_per_response")
            print(
                f"| {workload} | {self.throughput(workload, before)} | {self.throughput(workload, after)} | {(ratio - 1) * 100:+.1f}% | {cpu_before:.1f} → {cpu_after:.1f} |"
            )

    def fastapi(self):
        print("\n## Candidate compared with FastAPI\n")
        print(
            "| Capability | FastAPI throughput [range] | Default / FastAPI | Rust loop / FastAPI |"
        )
        print("| --- | ---: | ---: | ---: |")
        for workload in self.workloads:
            field = "messages_per_second" if workload == "websocket" else "rps"
            baseline = self.median(workload, "fastapi", field)
            default = self.median(workload, "candidate-default", field) / baseline
            rust = self.median(workload, "candidate-rust", field) / baseline
            print(
                f"| {workload} | {self.throughput(workload, 'fastapi')} | {default:.2f}× | {rust:.2f}× |"
            )

    def render(self):
        count = sum(sample["responses"] for sample in self.samples)
        errors = sum(sum(sample["errors"].values()) for sample in self.samples)
        statuses = sum(sample.get("unexpected_status", 0) for sample in self.samples)
        bound = sum(sample["client_bound"] for sample in self.samples)
        print("# Broad API comparison results\n")
        print(
            f"{len(self.samples)} measured samples; {count:,} completed responses/messages; "
            f"{errors} reported errors; {statuses} unexpected HTTP statuses. "
            f"{bound} samples flagged as client-limited."
        )
        print(
            "\nThroughput is requests/s, except WebSocket messages/s. Values are medians "
            "of per-sample values, with minimum/maximum ranges. CPU costs use "
            "process-tree CPU time divided by completed responses/messages."
        )
        variants = {sample["variant"] for sample in self.samples}
        for mode in ("default", "rust"):
            if {f"rc1-{mode}", f"candidate-{mode}"} <= variants:
                self.changes(mode)
        if {"fastapi", "candidate-default", "candidate-rust"} <= variants:
            self.fastapi()
        print(
            "\nSame-host, unpinned, short exploratory samples. WebSocket figures can "
            "be limited by the Python client. See raw.json for per-sample CPU, "
            "latency, errors, versions and native hashes."
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("results")
    Summary(parser.parse_args().results).render()
