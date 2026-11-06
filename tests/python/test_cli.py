"""Configuration and parent/worker boundary behavior."""

import argparse
import io
import os
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import Mock, patch

from bessemer.cli import _configure_child, main
from bessemer.config import settings


class ConfigTests(unittest.TestCase):
    def test_configuration_precedence_and_native_limits(self):
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory, "pyproject.toml")
            project.write_text(
                "[tool.bessemer]\nworkers=2\nport=9000\nmax_response_size=128\n"
            )
            with (
                patch(
                    "bessemer.config.Path",
                    side_effect=lambda name: (
                        project if name == "pyproject.toml" else Path(name)
                    ),
                ),
                patch.dict(
                    os.environ,
                    {"BESSEMER_WORKERS": "3", "BESSEMER_PORT": "9001"},
                    clear=True,
                ),
            ):
                result = settings(argparse.Namespace(workers=4))
            self.assertEqual(result["workers"], 4)
            self.assertEqual(result["port"], 9001)
            self.assertEqual(result["max_response_size"], 128)

    def test_rejects_invalid_values_before_binding_or_importing(self):
        invalid = {
            "workers": [-1, True, 257],
            "max_requests": [-1, True],
            "restart_limit": [-1],
            "max_body_size": [0],
            "header_timeout": [0, float("nan"), float("inf"), 0.0000000001],
            "max_lifetime": [-1],
            "restart_backoff": [0],
            "worker_timeout": [0.1],
        }
        with patch.dict(os.environ, {}, clear=True):
            for name, values in invalid.items():
                for value in values:
                    with (
                        self.subTest(name=name, value=value),
                        self.assertRaises(ValueError),
                    ):
                        settings(argparse.Namespace(**{name: value}))

    def test_invalid_environment_and_unknown_project_settings_are_errors(self):
        with (
            patch.dict(os.environ, {"BESSEMER_WORKERS": "many"}, clear=True),
            self.assertRaisesRegex(ValueError, "BESSEMER_WORKERS"),
        ):
            settings(argparse.Namespace())
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory, "pyproject.toml")
            project.write_text("[tool.bessemer]\nunknown_setting=4\n")
            with (
                patch("bessemer.config.Path", return_value=project),
                self.assertRaisesRegex(ValueError, "unknown_setting"),
            ):
                settings(argparse.Namespace())


class SupervisedCliTests(unittest.TestCase):
    def test_parent_does_not_import_application(self):
        with (
            patch("bessemer.cli.load_app") as load,
            patch("bessemer.supervisor.run", return_value=0) as supervise,
        ):
            self.assertEqual(
                main(["run", "sample:app", "--workers", "2", "--max-requests", "100"]),
                0,
            )
        load.assert_not_called()
        self.assertEqual(supervise.call_args.args[0], "sample:app")
        self.assertEqual(supervise.call_args.args[1]["workers"], 2)
        self.assertEqual(supervise.call_args.args[1]["max_requests"], 100)

    def test_invalid_config_does_not_start_supervisor(self):
        with (
            patch("bessemer.supervisor.run") as supervise,
            redirect_stderr(io.StringIO()),
        ):
            self.assertEqual(main(["run", "sample:app", "--workers", "-1"]), 1)
        supervise.assert_not_called()

    def test_worker_clears_descriptor_inheritance_before_loading_application(self):
        events = []
        application = Mock()
        application._export_settings.return_value = {}
        with (
            patch("bessemer.cli._configure_child"),
            # The POSIX path; on Windows the worker receives OS handles, which
            # test_supervisor_windows covers with real processes.
            patch("bessemer.cli.sys.platform", "linux"),
            patch(
                "bessemer.cli.os.set_inheritable",
                side_effect=lambda descriptor, value: events.append(
                    (descriptor, value)
                ),
            ),
            patch(
                "bessemer.cli.load_app",
                side_effect=lambda target: events.append("load") or application,
            ),
        ):
            result = main(
                [
                    "_worker",
                    "sample:app",
                    "--listener-fd",
                    "10",
                    "--ready-fd",
                    "11",
                    "--worker-count",
                    "2",
                    "--parent-pid",
                    "1",
                    "--settings",
                    "{}",
                ]
            )
        self.assertEqual(result, 0)
        self.assertEqual(events, [(10, False), (11, False), "load"])
        application.run.assert_called_once_with(listener_fd=10, ready_fd=11)

    def test_process_local_metrics_reject_multiple_workers(self):
        application = Mock()
        application._export_settings.return_value = {"metrics_path": "/metrics"}
        with (
            patch("bessemer.cli._configure_child"),
            patch("bessemer.cli.os.set_inheritable"),
            patch("bessemer.cli.load_app", return_value=application),
            redirect_stderr(io.StringIO()) as errors,
        ):
            result = main(
                [
                    "_worker",
                    "sample:app",
                    "--listener-fd",
                    "10",
                    "--ready-fd",
                    "11",
                    "--worker-count",
                    "2",
                    "--parent-pid",
                    "1",
                    "--settings",
                    "{}",
                ]
            )
        self.assertEqual(result, 78)
        self.assertIn("--workers 1", errors.getvalue())
        application.run.assert_not_called()

    def test_container_init_pid_is_a_valid_supervisor(self):
        with (
            patch("bessemer.cli.sys.platform", "darwin"),
            patch("bessemer.cli.os.getppid", return_value=1),
        ):
            _configure_child(1)


if __name__ == "__main__":
    unittest.main()
