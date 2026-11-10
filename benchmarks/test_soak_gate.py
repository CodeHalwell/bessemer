"""Check that the soak gate fails what it cannot measure as well as what it can."""

import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

import soak_gate
from soak_gate import evaluate, markdown, memory_growth

LIMITS = {
    "warmup_seconds": 600,
    "max_rss_growth_mib": 8,
    "min_delivery_fraction": 0.97,
}


def report(rss_mib, *, interval=30, rate=100, workers=2, errors=0):
    """A report with one sample per entry of `rss_mib`, evenly spaced."""
    samples = []
    for index, value in enumerate(rss_mib):
        samples.append(
            {
                "elapsed_seconds": (index + 1) * interval,
                "rss_kib": round(value * 1024),
                "anonymous_rss_kib": round(value * 512),
                "process_count": workers + 1,
                "processes": [
                    {"pid": 100 + pid, "start_ticks": 7, "cpu_seconds": index}
                    for pid in range(workers + 1)
                ],
            }
        )
    elapsed = len(rss_mib) * interval
    count = round(rate * elapsed)
    return {
        "samples": samples,
        "workload": {
            "elapsed_seconds": elapsed,
            "request_latency": {"count": count},
            "generator_lag": {"count": count},
            "statuses": {"200": count - errors, "transport_error": errors},
            "unexpected_errors": {"RemoteDisconnected": errors} if errors else {},
        },
        "target_requests_per_second": rate,
        "server_workers": workers,
        "completed_duration": True,
        "qualification_exit_code": 1 if errors else 0,
        "error": None,
        "wheel_sha256": "0" * 64,
    }


def failed(result):
    return [item["name"] for item in result["checks"] if not item["passed"]]


class SoakGateTests(unittest.TestCase):
    def test_a_steady_run_passes_and_reports_what_it_measured(self):
        # Growth while warming up is not held against the run.
        values = [60 + min(index, 20) * 0.5 for index in range(180)]
        result = evaluate(report(values), **LIMITS)
        self.assertTrue(result["passed"], failed(result))
        self.assertFalse(result["formal_acceptance"])
        self.assertEqual(result["rss"]["warm_samples"], 161)
        self.assertEqual(result["rss"]["window_samples"], 16)
        self.assertAlmostEqual(result["rss"]["growth_mib"], 0)
        self.assertAlmostEqual(result["anonymous_rss"]["first_window_median_mib"], 35)
        # Ninety minutes at 100 requests a second: 480,000 requests in the 80
        # minutes after warm-up, so 8 MiB is about 17 bytes for each.
        self.assertAlmostEqual(result["limit_bytes_per_request"], 17.476, places=3)
        self.assertIn("about 17 bytes per request", markdown(result))

    def test_growth_after_warmup_beyond_the_limit_fails(self):
        # 0.1 MiB per 30 s sample: 16 MiB across the 161 warm samples, and
        # 14.5 MiB between the medians of their first and last sixteen.
        values = [70 + index * 0.1 for index in range(180)]
        result = evaluate(report(values), **LIMITS)
        self.assertEqual(failed(result), ["resident memory growth after warm-up"])
        self.assertAlmostEqual(result["rss"]["growth_mib"], 14.5, places=3)
        # The same run passes a limit above what it grew by.
        relaxed = evaluate(report(values), **{**LIMITS, "max_rss_growth_mib": 15})
        self.assertTrue(relaxed["passed"])

    def test_a_run_too_short_to_measure_growth_cannot_pass(self):
        # 24 samples of 30 s leave five after a 600 s warm-up, one short of
        # the two windows of three that a growth figure needs.
        result = evaluate(report([70] * 24), **LIMITS)
        self.assertEqual(failed(result), ["resident memory growth after warm-up"])
        self.assertIsNone(result["rss"])
        self.assertIsNone(memory_growth(report([70] * 24)["samples"], 600, "rss_kib"))
        self.assertTrue(evaluate(report([70] * 25), **LIMITS)["passed"])

    def test_a_replaced_worker_fails_even_with_flat_memory(self):
        replaced = report([70] * 60)
        for sample in replaced["samples"][30:]:
            sample["processes"][2] = {"pid": 999, "start_ticks": 9, "cpu_seconds": 1}
        result = evaluate(replaced, **LIMITS)
        self.assertEqual(failed(result), ["process population"])
        missing = report([70] * 60)
        missing["samples"][-1]["process_count"] = 2
        self.assertEqual(failed(evaluate(missing, **LIMITS)), ["process population"])

    def test_undelivered_load_fails(self):
        slow = report([70] * 60)
        slow["workload"]["request_latency"]["count"] = 170_000
        slow["workload"]["statuses"] = {"200": 170_000}
        result = evaluate(slow, **LIMITS)
        self.assertEqual(failed(result), ["delivered load"])

    def test_harness_failures_are_not_overridden_by_flat_memory(self):
        result = evaluate(report([70] * 60, errors=3), **LIMITS)
        self.assertEqual(failed(result), ["harness verdict", "unexpected responses"])
        stopped = report([70] * 60)
        stopped["completed_duration"] = False
        stopped["error"] = "candidate root process exited unexpectedly:-9"
        self.assertEqual(failed(evaluate(stopped, **LIMITS)), ["harness verdict"])

    def test_summary_names_the_verdict_and_each_check(self):
        text = markdown(evaluate(report([70] * 60), **LIMITS))
        self.assertIn("### Soak: passed", text)
        self.assertIn("| resident memory growth after warm-up | pass | 0.0 |", text)
        text = markdown(evaluate(report([70] * 60, errors=1), **LIMITS))
        self.assertIn("### Soak: FAILED", text)
        self.assertIn("| unexpected responses | FAIL | 1 | 0 |", text)

    def test_command_exits_nonzero_for_a_failure_or_a_missing_report(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name, value, expected in (
                ("steady.json", report([70] * 60), 0),
                ("growing.json", report([70 + i for i in range(60)]), 1),
                ("empty.json", {"samples": []}, 1),
            ):
                (root / name).write_text(json.dumps(value))
                arguments = ["soak_gate.py", str(root / name)]
                arguments += ["--json", str(root / "verdict.json")]
                arguments += ["--markdown", str(root / "summary.md")]
                with (
                    patch.object(sys, "argv", arguments),
                    redirect_stdout(io.StringIO()),
                ):
                    self.assertEqual(soak_gate.main(), expected, name)
            verdict = json.loads((root / "verdict.json").read_text())
            self.assertFalse(verdict["passed"])
            self.assertEqual((root / "summary.md").read_text().count("### Soak"), 2)
            with (
                patch.object(sys, "argv", ["soak_gate.py", str(root / "absent.json")]),
                redirect_stdout(io.StringIO()) as output,
            ):
                self.assertEqual(soak_gate.main(), 1)
            self.assertIn("no usable report", output.getvalue())


if __name__ == "__main__":
    unittest.main()
