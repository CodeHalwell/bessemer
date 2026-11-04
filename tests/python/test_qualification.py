"""Checks that qualification tools reject misleading evidence and count real work."""

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "benchmarks"))
from qualify_latency import parse_wrk_output
from soak import (
    Latencies,
    Workload,
    cleanup_complete,
    openapi_has_routes,
    process_tree_snapshot,
    same_json,
)
from summarize_soak import summarize

CORRECTED = """
  Latency Distribution (HdrHistogram - Recorded Latency)
 50.000% 100.00us
 99.000% 1.20ms
 99.900% 2.40ms
  Detailed Percentile spectrum:
       Value   Percentile TotalCount 1/(1-Percentile)
       0.100   0.500000 1000 2.00
       0.500   0.950000 1900 20.00
----------------------------------------------------------
  2000 requests in 2.00s, 1.00MB read
Requests/sec: 1000.00
"""


class LatencyEvidenceTests(unittest.TestCase):
    def test_reads_corrected_latency_and_all_requested_population(self):
        value = parse_wrk_output(CORRECTED, 1000)
        self.assertTrue(value["delivery_valid"])
        self.assertEqual(value["requests"], 2000)
        self.assertAlmostEqual(value["p50_seconds"], 0.0001)
        self.assertAlmostEqual(value["p95_seconds"], 0.0005)
        self.assertAlmostEqual(value["p999_seconds"], 0.0024)

    def test_uncorrected_latency_cannot_masquerade_as_corrected(self):
        with self.assertRaisesRegex(ValueError, "Recorded Latency"):
            parse_wrk_output(
                CORRECTED.replace("Recorded Latency", "Uncorrected Latency"), 1000
            )

    def test_exact_corrected_footer_takes_precedence_over_rounded_spectrum(self):
        output = CORRECTED + "QUALIFICATION_PERCENTILES_US 101 499 1201 2401\n"
        result = parse_wrk_output(output, 1000)
        self.assertAlmostEqual(result["p95_seconds"], 0.000499)
        self.assertIn("direct corrected", result["percentile_method"])
        for broken in (
            output + "QUALIFICATION_PERCENTILES_US 101 499 1201 2401\n",
            CORRECTED + "QUALIFICATION_PERCENTILES_US 1000 499 1201 2401\n",
            CORRECTED + "QUALIFICATION_PERCENTILES_US bad footer\n",
        ):
            with self.subTest(output=broken), self.assertRaises(ValueError):
                parse_wrk_output(broken, 1000)

    def test_errors_and_under_delivery_invalidate_latency_evidence(self):
        value = parse_wrk_output(
            CORRECTED
            + "Socket errors: connect 1, read 0, write 0, timeout 0\nNon-2xx or 3xx responses: 4\n",
            2000,
        )
        self.assertFalse(value["delivery_valid"])
        self.assertEqual(
            set(value["invalid_reasons"]),
            {"socket_errors", "non_success_statuses", "requested_rate_not_achieved"},
        )

    def test_incomplete_or_zero_population_is_rejected(self):
        for output in (
            "",
            CORRECTED.replace("2000 requests", "0 requests"),
            CORRECTED.replace("99.900%", "95.000%"),
            CORRECTED.replace("1900 20.00", "0 20.00"),
        ):
            with self.subTest(output=output), self.assertRaises(ValueError):
                parse_wrk_output(output, 1000)


class SoakEvidenceTests(unittest.TestCase):
    def test_exact_json_checks_preserve_number_and_boolean_types(self):
        expected = {"ok": True, "count": 42, "nested": [1, "value"]}
        self.assertTrue(same_json(expected, expected.copy()))
        for invalid in (
            {**expected, "ok": 1},
            {**expected, "count": 42.0},
            {**expected, "nested": [True, "value"]},
            {**expected, "private": "must-be-filtered"},
        ):
            with self.subTest(value=invalid):
                self.assertFalse(same_json(invalid, expected))
        self.assertTrue(
            openapi_has_routes({"paths": {"/typed": {}, "/dependency": {}}})
        )
        for invalid in (
            {"paths": ["/typed", "/dependency"]},
            {"paths": {"/typed": {}, "/dependency": "not a PathItem object"}},
            {"paths": {}},
            {"paths": None},
        ):
            with self.subTest(document=invalid):
                self.assertFalse(openapi_has_routes(invalid))

    def test_workload_clock_stops_before_post_load_probes_and_shutdown(self):
        with patch("soak.time.monotonic", side_effect=[10, 20]):
            workload = Workload("127.0.0.1", 1, 1)
            workload.close()
        self.assertEqual(workload.snapshot()["elapsed_seconds"], 10)
        workload.close()
        self.assertEqual(workload.snapshot()["elapsed_seconds"], 10)

    def test_summary_keeps_incomplete_runs_open_and_counts_replacement_cpu(self):
        workload = {
            "elapsed_seconds": 20,
            "request_latency": {"count": 190},
            "generator_lag": {},
            "statuses": {"200": 189, "transport_error": 1},
            "unexpected_errors": {"ConnectionResetError": 1},
        }
        report = {
            "target_requests_per_second": 10,
            "wheel_sha256": "test-wheel",
            "samples": [
                {
                    **workload,
                    "elapsed_seconds": 10,
                    "rss_kib": 1024,
                    "process_count": 1,
                    "processes": [{"pid": 10, "start_ticks": 1, "cpu_seconds": 5}],
                },
                {
                    **workload,
                    "rss_kib": 2048,
                    "process_count": 1,
                    "processes": [{"pid": 10, "start_ticks": 2, "cpu_seconds": 2}],
                },
            ],
        }
        result = summarize(report, 0)
        self.assertFalse(result["completed_requested_duration"])
        self.assertFalse(result["formal_acceptance"])
        self.assertEqual(result["distinct_process_identities_observed"], 2)
        self.assertEqual(result["sampled_cpu_seconds"], 7)
        self.assertEqual(result["request_attempts"], 190)
        self.assertEqual(result["completed_http_responses"], 189)
        self.assertAlmostEqual(result["completion_fraction_of_schedule"], 0.945)
        self.assertEqual(result["rss_slope_mib_per_hour_after_warmup"], 360)

    def test_timeout_cleanup_needs_real_balanced_counters(self):
        valid = {
            "async_started": 7,
            "async_completed": 7,
            "async_cancelled": 7,
            "sync_started": 8,
            "sync_completed": 8,
        }
        self.assertTrue(cleanup_complete(valid))
        self.assertTrue(
            cleanup_complete({**valid, "resource_started": 3, "resource_completed": 3})
        )
        for invalid in (
            {},
            {**valid, "async_cancelled": 6},
            {**valid, "async_completed": True},
            {**valid, "sync_completed": 7},
            {**valid, "sync_started": -1},
            {**valid, "resource_started": 3},
            {**valid, "resource_started": 3, "resource_completed": 2},
            {**valid, "background_started": 4, "background_completed": 3},
            {
                **valid,
                "resource_started": 3,
                "resource_completed": 3,
                "background_started": 2,
                "background_completed": 2,
            },
        ):
            with self.subTest(state=invalid):
                self.assertFalse(cleanup_complete(invalid))

    def test_histogram_retains_tail_population_and_rejects_invalid_values(self):
        histogram = Latencies()
        for _ in range(99):
            histogram.add(0.001)
        histogram.add(2)
        self.assertEqual(histogram.count, 100)
        self.assertEqual(histogram.percentile(0.99), 0.001)
        self.assertEqual(histogram.percentile(0.999), 2)
        for value in (-1, float("nan"), float("inf")):
            with self.subTest(value=value), self.assertRaises(ValueError):
                histogram.add(value)

    def test_process_tree_includes_replacement_children_and_excludes_others(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for pid, ppid, rss in (
                (100, 1, 10),
                (101, 100, 20),
                (102, 101, 30),
                (200, 1, 999),
            ):
                entry = root / str(pid)
                entry.mkdir()
                (entry / "status").write_text(
                    f"PPid:\t{ppid}\nVmRSS:\t{rss} kB\nRssAnon:\t{rss // 2} kB\nThreads:\t2\n"
                )
                tail = ["S", str(ppid), *(["0"] * 22)]
                tail[11], tail[12] = "2", "3"
                (entry / "stat").write_text(f"{pid} (test process) " + " ".join(tail))
            snapshot = process_tree_snapshot(100, root)
            self.assertEqual(snapshot["pids"], [100, 101, 102])
            self.assertEqual(snapshot["rss_kib"], 60)
            self.assertEqual(snapshot["threads"], 6)
            self.assertEqual(process_tree_snapshot(999, root)["process_count"], 0)
            (root / "101" / "status").unlink()
            replacement = root / "103"
            replacement.mkdir()
            (replacement / "status").write_text(
                "PPid:\t100\nVmRSS:\t40 kB\nThreads:\t1\n"
            )
            (replacement / "stat").write_text(
                "103 (replacement) " + " ".join(["S", "100", *(["0"] * 22)])
            )
            self.assertEqual(process_tree_snapshot(100, root)["pids"], [100, 103])


if __name__ == "__main__":
    unittest.main()
