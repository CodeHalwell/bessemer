"""Bounded full-stream qualification guards without Docker or load generation."""

import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

DEPLOY = Path(__file__).resolve().parents[1] / "deploy"
sys.path.insert(0, str(DEPLOY))
try:
    SPEC = importlib.util.spec_from_file_location(
        "production_soak_tests", DEPLOY / "production_soak.py"
    )
    soak = importlib.util.module_from_spec(SPEC)
    SPEC.loader.exec_module(soak)
finally:
    sys.path.remove(str(DEPLOY))


PROCESS_ROLES = {
    "init": {"pid": 1, "comm": "tini", "fds": 3},
    "supervisors": [
        {
            "pid": 2,
            "ppid": 1,
            "supervisor_argv": True,
            "worker_argv": False,
            "fds": 6,
            "start_ticks": 100,
        }
    ],
    "workers": [
        {"pid": 7, "ppid": 2, "worker_argv": True, "start_ticks": 123, "fds": 8}
    ],
}


class ProductionLogGuardTests(unittest.TestCase):
    def test_supervisor_cannot_be_reported_as_a_worker(self):
        observed = dict(PROCESS_ROLES)
        observed["workers"] = PROCESS_ROLES["supervisors"]
        with self.assertRaises(AssertionError):
            soak.verify_process_roles(observed)

    def test_forced_shutdown_and_reap_errors_are_detected_across_chunks(self):
        value = b"worker exceeded drain deadline pid=7\nworker killed at shutdown deadline pid=7\nworker could not be reaped pid=7\n"
        with tempfile.TemporaryDirectory() as directory:
            sink = soak.RedactedLogSink(Path(directory, "errors.log"), "unprinted-key")
            for index in range(0, len(value), 2):
                sink.consume(value[index : index + 2])
            sink.close()
            self.assertTrue(
                {"retirement_kill", "shutdown_kill", "reap_failure"}.issubset(
                    sink.summary()["worker_error_flags"]
                )
            )

    def test_failed_log_process_start_closes_the_owned_sink(self):
        sink = Mock()
        container = Mock(cid="owned-container")
        container.docker.environment = {}
        with (
            patch.object(soak, "RedactedLogSink", return_value=sink),
            patch.object(soak.subprocess, "Popen", side_effect=OSError("start failed")),
            self.assertRaises(OSError),
        ):
            soak.LiveLogCapture(container, "unused.log", "test-secret")
        sink.close.assert_called_once()

    def test_secret_and_error_split_across_chunks_are_detected_and_redacted(self):
        secret = "test-only-key-not-for-output"
        value = ("prefix " + secret + " worker failed pid=42\n").encode()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory, "safe.log")
            sink = soak.RedactedLogSink(path, secret)
            for index in range(0, len(value), 3):
                sink.consume(value[index : index + 3])
            sink.close()
            self.assertNotIn(secret.encode(), path.read_bytes())
            self.assertIn(b"[redacted]", path.read_bytes())
            self.assertFalse(sink.summary()["secret_absent"])
            self.assertIn("worker_failure", sink.summary()["worker_error_flags"])

    def test_guard_checks_beyond_file_cap_and_last_thousand_lines(self):
        secret = "test-only-secret"
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory, "bounded.log")
            sink = soak.RedactedLogSink(path, secret, max_bytes=64)
            sink.consume(b"normal\n" * 1001)
            sink.consume(b"worker failed pid=7 " + secret.encode() + b"\n")
            sink.consume(b"normal\n" * 1001)
            sink.close()
            summary = sink.summary()
            self.assertLessEqual(path.stat().st_size, 64)
            self.assertTrue(summary["truncated"])
            self.assertFalse(summary["secret_absent"])
            self.assertIn("worker_failure", summary["worker_error_flags"])

    def test_ordinary_utf8_stream_is_preserved(self):
        value = "ordinary café 😀\n".encode()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory, "utf8.log")
            sink = soak.RedactedLogSink(path, "unprinted-key")
            for byte in value:
                sink.consume(bytes([byte]))
            sink.close()
            self.assertEqual(path.read_bytes(), value)
            self.assertTrue(sink.summary()["secret_absent"])

    def test_short_soak_keeps_contract_and_resource_summaries_bounded(self):
        class Docker:
            def json(self, command, *arguments):
                if command == "inspect":
                    return {
                        "running": True,
                        "oom_killed": False,
                        "restart_count": 0,
                        "health": "healthy",
                    }
                return {
                    "memory_current": 10_000,
                    "memory_peak": 20_000,
                    "memory_max": "536870912",
                    "pids_current": 4,
                    "pids_max": "128",
                    "cpu_max": "100000 100000",
                    "memory_events": {"oom_kill": 0},
                    **PROCESS_ROLES,
                }

        class Container:
            docker = Docker()
            cid = "owned-test"

            def request(self, method, path, **kwargs):
                return 200, b"ready"

        class Capture:
            def summary(self):
                return {
                    "secret_absent": True,
                    "worker_error_flags": [],
                    "truncated": False,
                    "stream_error": None,
                    "collector_exit_code": None,
                }

        contract = ("ready", "GET", "/readyz", 200, lambda value: value == b"ready", {})
        # A fake clock: on Windows the monotonic clock ticks in about 15 ms
        # steps, so a real 0.01 s soak can let a second request in.
        clock = [0.0]

        def sleep(seconds):
            clock[0] += seconds

        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(soak.time, "monotonic", side_effect=lambda: clock[0]),
            patch.object(soak.time, "sleep", side_effect=sleep),
        ):
            result = soak.run_production_soak(
                Container(), [contract], Path(directory), 0.01, Capture()
            )
            self.assertTrue(result["passed"])
            self.assertEqual(result["requests"], 1)
            self.assertEqual(result["resource_samples"], 2)
            self.assertEqual(result["errors"], 0)
            self.assertEqual(len(result["error_examples"]), 0)

    def test_elapsed_time_without_delivered_work_cannot_pass(self):
        clock = [0.0]
        container = Mock(cid="owned-container")

        def observe(command, *arguments):
            if command == "inspect":
                clock[0] += 20
                return {
                    "running": True,
                    "oom_killed": False,
                    "restart_count": 0,
                    "health": "healthy",
                }
            return {
                "memory_current": 10_000,
                "memory_peak": 20_000,
                "memory_max": "536870912",
                "pids_current": 4,
                "pids_max": "128",
                "cpu_max": "100000 100000",
                "memory_events": {"oom_kill": 0},
                **PROCESS_ROLES,
            }

        container.docker.json.side_effect = observe
        container.request.return_value = (200, b"ready")
        capture = Mock()
        capture.summary.return_value = {
            "secret_absent": True,
            "worker_error_flags": [],
            "truncated": False,
            "stream_error": None,
            "collector_exit_code": None,
        }
        contract = ("ready", "GET", "/readyz", 200, None, {})
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(soak.time, "monotonic", side_effect=lambda: clock[0]),
            patch.object(
                soak.time,
                "sleep",
                side_effect=lambda seconds: clock.__setitem__(0, clock[0] + seconds),
            ),
        ):
            result = soak.run_production_soak(
                container, [contract], Path(directory), 10, capture
            )
        self.assertFalse(result["passed"])
        self.assertGreaterEqual(result["elapsed_seconds"], 10)
        self.assertEqual(result["requests"], 1)
        self.assertEqual(result["delivery_fraction"], 0.1)
        self.assertIn(
            "insufficient_delivered_work",
            {item["kind"] for item in result["error_examples"]},
        )


if __name__ == "__main__":
    unittest.main()
