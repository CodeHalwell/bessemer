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
from bessemer.config import DEFAULTS, settings


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


class ParentCheckTests(unittest.TestCase):
    def test_posix_worker_refuses_a_parent_that_is_not_its_supervisor(self):
        with (
            patch("bessemer.cli.sys.platform", "darwin"),
            patch("bessemer.cli.os.getppid", return_value=4242),
        ):
            _configure_child(4242)
            with self.assertRaisesRegex(RuntimeError, "supervisor exited"):
                _configure_child(4243)
        with self.assertRaises(ValueError):
            _configure_child(0)

    def test_windows_worker_started_through_a_venv_launcher_is_accepted(self):
        # A Windows virtual environment's python.exe starts the real
        # interpreter as its own child, so the parent is the launcher.
        with (
            patch("bessemer.cli.sys.platform", "win32"),
            patch("bessemer.cli.os.getppid", return_value=9999),
        ):
            _configure_child(4242)


class MistypedEnvironmentTests(unittest.TestCase):
    def test_a_variable_one_slip_from_a_setting_is_reported(self):
        from bessemer.config import mistyped_environment

        slips = mistyped_environment(
            {
                "BESSEMER_WORKER": "4",
                "BESSEMER_GRACEFUL_TIMOUT": "20",
                "BESSEMER_WORKERS": "2",
                # An application's own variables and Bessemer's internal ones.
                "BESSEMER_EXAMPLE_API_KEY": "secret",
                "BESSEMER_SUPERVISED_WORKER": "1",
                "BESSEMER_REQUIRE_NATIVE": "1",
                "HOME": "/root",
            }
        )
        self.assertEqual(
            slips,
            [
                ("BESSEMER_GRACEFUL_TIMOUT", "BESSEMER_GRACEFUL_TIMEOUT"),
                ("BESSEMER_WORKER", "BESSEMER_WORKERS"),
            ],
        )

    def test_settings_warn_and_carry_on(self):
        with (
            patch.dict(os.environ, {"BESSEMER_WORKER": "4"}, clear=True),
            self.assertLogs("bessemer", level="WARNING") as logs,
        ):
            resolved = settings(argparse.Namespace())
        self.assertEqual(resolved["workers"], DEFAULTS["workers"])
        self.assertIn("BESSEMER_WORKER is not a Bessemer setting", logs.output[0])
        self.assertIn("BESSEMER_WORKERS", logs.output[0])
        # The value never appears: it may be a secret under a similar name.
        self.assertNotIn("4?", logs.output[0])

    def test_every_setting_name_is_its_own_closest_match(self):
        from bessemer.config import mistyped_environment

        names = {"BESSEMER_" + name.upper(): "x" for name in DEFAULTS}
        self.assertEqual(mistyped_environment(names), [])


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
            # The POSIX path: on Windows the worker first waits on its start
            # gate, which only a supervisor opens.
            patch("bessemer.cli.sys.platform", "linux"),
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
