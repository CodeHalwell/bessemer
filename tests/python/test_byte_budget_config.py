"""Aggregate retained-payload limits reject unsafe settings before worker startup."""

from __future__ import annotations

import argparse
import contextlib
import io
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import bessemer
from bessemer import App
from bessemer.cli import main
from bessemer.config import SERVER_DEFAULTS, SERVER_INTEGER_LIMITS, settings

BUDGETS = (
    ("max_request_bytes_in_flight", "max_body_size"),
    ("max_response_bytes_in_flight", "max_response_size"),
)


class ByteBudgetConfigTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="bessemer-budget-config-")
        self.previous = Path.cwd()
        os.chdir(self.directory.name)
        self.addCleanup(self.directory.cleanup)
        self.addCleanup(os.chdir, self.previous)
        self.environment = patch.dict(os.environ, {}, clear=True)
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def test_native_limit_ranges_reject_before_app_import_or_lifespan(self):
        expected = {
            "max_body_size": (1, 1_073_741_824),
            "max_request_bytes_in_flight": (1, 1_073_741_824),
            "max_in_flight": (1, 1_000_000),
            "max_response_size": (1, 67_108_864),
            "max_response_bytes_in_flight": (1, 1_073_741_824),
            "max_connections": (1, 100_000),
            "max_headers": (1, 1024),
            "max_header_size": (8192, 1_048_576),
            "max_request_target_size": (1, 1_048_576),
            "max_upload_size": (0, 68_719_476_736),
            "max_upload_bytes_in_flight": (1, 1_099_511_627_776),
        }
        self.assertEqual(SERVER_INTEGER_LIMITS, expected)
        native = types.SimpleNamespace(serve=Mock())
        with (
            patch.dict(sys.modules, {"bessemer._native": native}),
            patch.object(bessemer, "_native", native, create=True),
            patch("bessemer.worker.Worker") as worker,
            patch("bessemer.supervisor.run") as supervise,
            patch("bessemer.cli.load_app") as load_app,
            contextlib.redirect_stderr(io.StringIO()),
        ):
            for name, (minimum, maximum) in expected.items():
                for value in (minimum - 1, maximum + 1):
                    with self.subTest(name=name, value=value):
                        with self.assertRaisesRegex(ValueError, name):
                            settings(argparse.Namespace(**{name: value}))
                        with self.assertRaisesRegex(ValueError, name):
                            App().run(**{name: value})
                        self.assertEqual(
                            main(
                                [
                                    "run",
                                    "side_effects:app",
                                    "--" + name.replace("_", "-"),
                                    str(value),
                                ]
                            ),
                            1,
                        )
                for value in (minimum, maximum):
                    overrides = {
                        "max_request_bytes_in_flight": 1_073_741_824,
                        "max_response_bytes_in_flight": 1_073_741_824,
                        "max_body_size": 1,
                        "max_response_size": 1,
                        "max_upload_bytes_in_flight": 1_099_511_627_776,
                        name: value,
                    }
                    with self.subTest(name=name, boundary=value):
                        self.assertEqual(
                            settings(argparse.Namespace(**overrides))[name], value
                        )
            worker.assert_not_called()
            supervise.assert_not_called()
            load_app.assert_not_called()
            native.serve.assert_not_called()

    def test_defaults_and_exact_individual_limit_boundaries(self):
        result = settings(argparse.Namespace())
        for name, individual in BUDGETS:
            with self.subTest(name=name):
                self.assertEqual(result[name], 33_554_432)
                self.assertEqual(SERVER_DEFAULTS[name], 33_554_432)
                override = settings(argparse.Namespace(**{name: result[individual]}))
                self.assertEqual(override[name], result[individual])
                maximum = settings(argparse.Namespace(**{name: 1_073_741_824}))
                self.assertEqual(maximum[name], 1_073_741_824)

    def test_rejects_noninteger_nonpositive_oversized_and_undersized_budgets(self):
        for name, individual in BUDGETS:
            for value in (
                False,
                True,
                0,
                -1,
                1.5,
                "1024",
                1_073_741_825,
                SERVER_DEFAULTS[individual] - 1,
            ):
                with (
                    self.subTest(name=name, value=value),
                    self.assertRaisesRegex(ValueError, name),
                ):
                    settings(argparse.Namespace(**{name: value}))

    def test_environment_and_toml_budgets_use_cli_precedence(self):
        Path("pyproject.toml").write_text(
            "[tool.bessemer]\nmax_request_bytes_in_flight=4194304\nmax_response_bytes_in_flight=8388608\n"
        )
        os.environ["BESSEMER_MAX_REQUEST_BYTES_IN_FLIGHT"] = "16777216"
        os.environ["BESSEMER_MAX_RESPONSE_BYTES_IN_FLIGHT"] = "16777216"
        result = settings(argparse.Namespace(max_response_bytes_in_flight=33_554_432))
        self.assertEqual(result["max_request_bytes_in_flight"], 16_777_216)
        self.assertEqual(result["max_response_bytes_in_flight"], 33_554_432)
        del os.environ["BESSEMER_MAX_REQUEST_BYTES_IN_FLIGHT"]
        self.assertEqual(
            settings(argparse.Namespace())["max_request_bytes_in_flight"], 4_194_304
        )

    def test_invalid_environment_and_toml_fail_closed(self):
        for name, _ in BUDGETS:
            variable = "BESSEMER_" + name.upper()
            for value in ("false", "-1", "0", "1073741825"):
                with (
                    self.subTest(name=name, value=value),
                    patch.dict(os.environ, {variable: value}),
                    self.assertRaises(ValueError),
                ):
                    settings(argparse.Namespace())
            Path("pyproject.toml").write_text(f"[tool.bessemer]\n{name}=true\n")
            with self.assertRaisesRegex(ValueError, name):
                settings(argparse.Namespace())
            Path("pyproject.toml").unlink()

    def test_cli_flags_reach_supervisor_and_invalid_values_do_not_start_it(self):
        with patch("bessemer.supervisor.run", return_value=0) as supervise:
            result = main(
                [
                    "run",
                    "fixture:app",
                    "--max-request-bytes-in-flight",
                    "4194304",
                    "--max-response-bytes-in-flight",
                    "8388608",
                ]
            )
            self.assertEqual(result, 0)
            self.assertEqual(
                supervise.call_args.args[1]["max_request_bytes_in_flight"], 4_194_304
            )
            self.assertEqual(
                supervise.call_args.args[1]["max_response_bytes_in_flight"], 8_388_608
            )
        for name, _ in BUDGETS:
            with (
                patch("bessemer.supervisor.run") as supervise,
                contextlib.redirect_stderr(io.StringIO()),
            ):
                self.assertEqual(
                    main(["run", "fixture:app", "--" + name.replace("_", "-"), "0"]), 1
                )
                supervise.assert_not_called()

    def test_app_run_forwards_exact_budgets_to_native_only(self):
        serve = Mock()
        native = types.SimpleNamespace(serve=serve, Bridge=Mock())
        with (
            patch.dict(sys.modules, {"bessemer._native": native}),
            patch.object(bessemer, "_native", native, create=True),
            patch("bessemer.worker.Worker") as worker,
        ):
            App().run(
                max_request_bytes_in_flight=4_194_304,
                max_response_bytes_in_flight=8_388_608,
            )
        self.assertEqual(
            serve.call_args.kwargs["max_request_bytes_in_flight"], 4_194_304
        )
        self.assertEqual(
            serve.call_args.kwargs["max_response_bytes_in_flight"], 8_388_608
        )
        self.assertNotIn("max_request_bytes_in_flight", worker.call_args.kwargs)
        self.assertNotIn("max_response_bytes_in_flight", worker.call_args.kwargs)

    def test_invalid_app_budgets_reject_before_worker_and_native_start(self):
        serve = Mock()
        native = types.SimpleNamespace(serve=serve, Bridge=Mock())
        with (
            patch.dict(sys.modules, {"bessemer._native": native}),
            patch.object(bessemer, "_native", native, create=True),
            patch("bessemer.worker.Worker") as worker,
        ):
            for name, individual in BUDGETS:
                for value in (
                    False,
                    True,
                    None,
                    0,
                    -1,
                    1.5,
                    "1024",
                    1_073_741_825,
                    SERVER_DEFAULTS[individual] - 1,
                ):
                    with (
                        self.subTest(name=name, value=value),
                        self.assertRaisesRegex(ValueError, name),
                    ):
                        App().run(**{name: value})
            worker.assert_not_called()
            serve.assert_not_called()


if __name__ == "__main__":
    unittest.main()
