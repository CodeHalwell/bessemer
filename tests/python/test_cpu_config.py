"""The additive sync-worker setting preserves normal configuration precedence."""

import argparse
import io
import os
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import Mock, patch

from bessemer.cli import main
from bessemer.config import settings


class SyncWorkerConfigTests(unittest.TestCase):
    def test_cli_environment_project_and_auto_default(self):
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory) / "pyproject.toml"
            project.write_text("[tool.bessemer]\nsync_workers=3\n")
            with patch("bessemer.config.Path", return_value=project):
                with patch.dict(os.environ, {"BESSEMER_SYNC_WORKERS": "4"}, clear=True):
                    self.assertEqual(
                        settings(argparse.Namespace(sync_workers=2))["sync_workers"], 2
                    )
                    self.assertEqual(settings(argparse.Namespace())["sync_workers"], 4)
                with patch.dict(os.environ, {}, clear=True):
                    self.assertEqual(settings(argparse.Namespace())["sync_workers"], 3)
                    project.write_text("[tool.bessemer]\n")
                    self.assertEqual(settings(argparse.Namespace())["sync_workers"], 0)

    def test_range_and_type_are_checked_before_startup(self):
        with patch.dict(os.environ, {}, clear=True):
            for value in (-1, 257, True, 1.5, "2"):
                with self.subTest(value=value), self.assertRaises(ValueError):
                    settings(argparse.Namespace(sync_workers=value))
            for value in (0, 1, 256):
                self.assertEqual(
                    settings(argparse.Namespace(sync_workers=value))["sync_workers"],
                    value,
                )
        with (
            patch.dict(os.environ, {"BESSEMER_SYNC_WORKERS": "many"}, clear=True),
            self.assertRaisesRegex(ValueError, "BESSEMER_SYNC_WORKERS"),
        ):
            settings(argparse.Namespace())

    def test_cli_flag_reaches_supervisor_without_importing_the_app(self):
        with (
            patch("bessemer.cli.load_app") as load,
            patch("bessemer.supervisor.run", return_value=0) as supervise,
        ):
            self.assertEqual(main(["run", "sample:app", "--sync-workers", "2"]), 0)
        load.assert_not_called()
        self.assertEqual(supervise.call_args.args[1]["sync_workers"], 2)
        with (
            patch("bessemer.supervisor.run") as supervise,
            redirect_stderr(io.StringIO()),
        ):
            self.assertEqual(main(["run", "sample:app", "--sync-workers", "257"]), 1)
        supervise.assert_not_called()

    def test_worker_forwards_explicit_sync_size_and_process_share(self):
        application = Mock()
        application._export_settings.return_value = {}
        with (
            patch("bessemer.cli._configure_child"),
            patch("bessemer.cli.sys.platform", "linux"),
            patch("bessemer.cli.os.set_inheritable"),
            patch("bessemer.cli.load_app", return_value=application),
        ):
            status = main(
                [
                    "_worker",
                    "sample:app",
                    "--listener-fd",
                    "10",
                    "--ready-fd",
                    "11",
                    "--worker-count",
                    "3",
                    "--parent-pid",
                    "1",
                    "--settings",
                    '{"sync_workers":2}',
                ]
            )
        self.assertEqual(status, 0)
        self.assertEqual(application._worker_processes, 3)
        application.run.assert_called_once_with(
            sync_workers=2, listener_fd=10, ready_fd=11
        )


if __name__ == "__main__":
    unittest.main()
