"""Comparison harness checks without modifying host affinity or running benchmarks."""

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent / "compare"))

import cpu_isolation
import matrix
import summarise_matrix


class MatrixTests(unittest.TestCase):
    def args(self, mode="feature", *extra):
        with mock.patch.dict(os.environ, {}, clear=True):
            return matrix.parser().parse_args([mode, "--out", "result.jsonl", *extra])

    def test_defaults_and_explicit_paths_work_outside_the_apps_directory(self):
        with mock.patch("os.sched_getaffinity", return_value=set(range(8))):
            feature = matrix.configure(self.args())
            concurrency = matrix.configure(self.args("concurrency"))
        self.assertEqual(feature.server_cpus, [0])
        self.assertEqual(feature.workers, [1])
        self.assertEqual(len(feature.routes), 17)
        self.assertEqual(concurrency.workers, [1, 2, 4])
        self.assertEqual(concurrency.connections, [64, 256, 1024])
        self.assertEqual(concurrency.routes, ["json", "io"])
        self.assertEqual(concurrency.server_cpus, [0, 1, 2, 3])
        args = self.args(
            "feature",
            "--python",
            "venv/bin/python",
            "--server-cpus",
            "2",
            "--client-cpus",
            "3",
        )
        configured = matrix.configure(args)
        self.assertEqual(
            configured.bessemer_python, str(Path("venv/bin/python").absolute())
        )

    def test_invalid_comparisons_fail_before_starting_servers(self):
        for extra in (
            ["--server-cpus", "0", "--client-cpus", "0"],
            ["--duration", "0"],
            ["--connections", "0"],
            ["--workers", "2"],
            ["--frameworks", "axum"],
            ["--routes", "DELETE:x"],
        ):
            with self.subTest(extra=extra), self.assertRaises(ValueError):
                matrix.configure(self.args("feature", *extra))
        with self.assertRaises(ValueError):
            matrix.configure(self.args("concurrency", "--middleware"))

    def test_oha_totals_do_not_count_transport_errors_as_success(self):
        row = matrix.sample(
            {
                "summary": {"requestsPerSec": 1000, "total": 0.1},
                "statusCodeDistribution": {"200": 60, "503": 20},
                "errorDistribution": {"connection refused": 20},
                "latencyPercentiles": {"p50": 0.001, "p99": 0.01},
            },
            framework="bessemer",
            workers=1,
            route="json",
            connections=64,
            middleware=False,
            mode="concurrency",
        )
        self.assertEqual((row["ok_rps"], row["non200"], row["errors"]), (600, 20, 20))
        self.assertEqual((row["p50_ms"], row["p99_ms"]), (1, 10))
        text = summarise_matrix.tables([row, row | {"ok_rps": 800}])
        self.assertIn("| bessemer | 2 | 700.00 |", text)
        self.assertIn("| 40 | 40 |", text)
        self.assertIn("MW=0", text)

    def test_an_empty_duration_sample_has_zero_ok_rate_and_unknown_latency(self):
        row = matrix.sample(
            {
                "summary": {"requestsPerSec": 640, "total": 0.1},
                "errorDistribution": {"aborted due to deadline": 64},
            },
            framework="fastapi",
            workers=1,
            route="io",
            connections=64,
            middleware=False,
            mode="concurrency",
        )
        self.assertEqual(row["ok_rps"], 0)
        self.assertIsNone(row["p99_ms"])
        self.assertIn("—", summarise_matrix.tables([row]))

    def test_request_bodies_match_the_source_workloads(self):
        self.assertEqual(
            matrix.request_args("POSTF:form")[1][-1],
            "application/x-www-form-urlencoded",
        )
        route, args = matrix.request_args("POSTJ:item")
        self.assertEqual(route, "item")
        self.assertEqual(json.loads(args[3]), {"id": 1, "name": "x", "price": 2})
        self.assertEqual(
            matrix.request_args("GET:query?a=2&d=7"), ("query?a=2&d=7", [])
        )

    def test_start_failure_stops_the_process_group_and_writes_no_sample(self):
        with tempfile.TemporaryDirectory() as directory:
            args = matrix.configure(
                self.args(
                    "feature",
                    "--out",
                    str(Path(directory) / "out.jsonl"),
                    "--server-cpus",
                    "0",
                    "--client-cpus",
                    "1",
                )
            )
            with (
                mock.patch.object(matrix.subprocess, "Popen") as start,
                mock.patch.object(
                    matrix, "wait_ready", side_effect=RuntimeError("not ready")
                ),
                mock.patch.object(matrix, "stop") as stop,
            ):
                with self.assertRaisesRegex(RuntimeError, "not ready"):
                    matrix.run(args)
                stop.assert_called_once_with(start.return_value)
            self.assertEqual(args.out.read_text(), "")

    def test_restore_checks_thread_identity_and_restores_original_masks(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "state.json"
            state.write_text(
                json.dumps(
                    [
                        {"tid": 123, "start": "old", "cpus": [2, 3]},
                        {"tid": 124, "start": "same", "cpus": [1, 4]},
                    ]
                )
            )
            with (
                mock.patch.object(
                    cpu_isolation, "identity", side_effect=["reused", "same"]
                ),
                mock.patch("os.sched_setaffinity") as affinity,
            ):
                self.assertEqual(cpu_isolation.restore(state), 1)
                affinity.assert_called_once_with(124, [1, 4])
            self.assertFalse(state.exists())

    def test_restore_retains_failed_entries_for_retry(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "state.json"
            entry = {"tid": 123, "start": "same", "cpus": [2, 3]}
            state.write_text(json.dumps([entry]))
            with (
                mock.patch.object(cpu_isolation, "identity", return_value="same"),
                mock.patch(
                    "os.sched_setaffinity", side_effect=PermissionError("denied")
                ),
                self.assertRaisesRegex(OSError, "retry"),
            ):
                cpu_isolation.restore(state)
            self.assertEqual(json.loads(state.read_text()), [entry])

    def test_feature_app_builds_for_both_frameworks_and_rejects_unknown_names(self):
        if not all(
            importlib.util.find_spec(name)
            for name in ("bessemer", "pydantic", "fastapi", "multipart")
        ):
            self.skipTest(
                "requires installed Bessemer, FastAPI, Pydantic and python-multipart"
            )
        # FastAPI resolves the locally imported Request annotation at route
        # registration; importing this module must not postpone annotations.
        import feature_matrix_app

        for framework in ("bessemer", "fastapi"):
            app = feature_matrix_app.build(framework, middleware=True)
            paths = {getattr(route, "path", None) for route in app.routes}
            self.assertIn("/form", paths)
            self.assertIn("/request", paths)
        with self.assertRaises(ValueError):
            feature_matrix_app.build("typo")

    def test_shell_entry_points_forward_help_from_any_directory(self):
        for script in ("run_matrix.sh", "run_concurrency.sh", "isolate_cpus.sh"):
            path = Path(__file__).resolve().parent / "compare" / script
            result = subprocess.run(
                ["bash", str(path), "--help"],
                cwd="/",
                capture_output=True,
                text=True,
                check=False,
                env=os.environ | {"HARNESS_PYTHON": sys.executable},
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("usage:", result.stdout)


if __name__ == "__main__":
    unittest.main()
