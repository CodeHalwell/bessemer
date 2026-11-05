"""app.run(workers=...) serves through the process supervisor."""

import http.client
import os
import signal
import socket
import subprocess
import sys
import tempfile
import textwrap
import time
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from bessemer import App
from bessemer.application import _importable_target

SCRIPT = """
import os
from bessemer import App

app = App()

@app.get("/pid")
async def pid():
    return {{"pid": os.getpid()}}

if __name__ == "__main__":
    app.run(port={port}, workers={workers}, graceful_timeout=2.0)
"""


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _get_pid(port: int) -> int | None:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=1)
    try:
        connection.request("GET", "/pid")
        response = connection.getresponse()
        body = response.read()
        if response.status != 200:
            return None
        return int(body.split(b":")[1].strip(b"} "))
    except OSError:
        return None
    finally:
        connection.close()


class ImportableTargetTests(unittest.TestCase):
    def test_module_level_application_is_named_by_module(self):
        module = types.ModuleType("sample_service")
        module.application = App()
        with patch.dict(sys.modules, {"sample_service": module}):
            target, _ = _importable_target(module.application)
        self.assertEqual(target, "sample_service:application")

    def test_script_application_is_named_by_file_and_directory(self):
        app = App()
        main = types.ModuleType("__main__")
        main.__file__ = "/srv/service/server.py"
        main.__spec__ = None
        main.app = app
        with patch.dict(sys.modules, {"__main__": main}):
            self.assertEqual(
                _importable_target(app),
                ("server:app", str(Path("/srv/service").resolve())),
            )

    def test_defining_module_is_preferred_over_a_launcher_alias(self):
        app = App()
        service = types.ModuleType("sample_service")
        service.app = app
        main = types.ModuleType("__main__")
        main.__file__ = "/srv/service/launcher.py"
        main.__spec__ = None
        main.app = app  # bound under the launcher's __main__ guard
        with patch.dict(sys.modules, {"__main__": main, "sample_service": service}):
            target, _ = _importable_target(app)
        self.assertEqual(target, "sample_service:app")

    def test_module_run_with_dash_m_uses_its_real_name(self):
        app = App()
        main = types.ModuleType("__main__")
        main.__file__ = "/srv/service/pkg/server.py"
        main.__spec__ = types.SimpleNamespace(name="pkg.server")
        main.app = app
        with patch.dict(sys.modules, {"__main__": main}):
            self.assertEqual(_importable_target(app), ("pkg.server:app", None))

    def test_application_without_a_module_variable_is_refused(self):
        with self.assertRaisesRegex(RuntimeError, "module-level variable"):
            _importable_target(App())


class RunWorkersValidationTests(unittest.TestCase):
    def test_workers_must_be_in_range(self):
        for workers in (-1, 257, 2.0, True):
            with (
                self.subTest(workers=workers),
                self.assertRaisesRegex(ValueError, "workers must be"),
            ):
                App().run(workers=workers)

    def test_run_during_worker_import_points_at_main_guard(self):
        with (
            patch.dict(os.environ, {"BESSEMER_SUPERVISED_WORKER": "1"}),
            self.assertRaisesRegex(ValueError, "__name__"),
        ):
            App().run(workers=2)

    def test_settings_are_checked_before_starting_the_supervisor(self):
        global validated_app
        validated_app = App()
        try:
            with patch("bessemer.supervisor.Supervisor") as supervisor:
                with self.assertRaisesRegex(ValueError, "max_body_size"):
                    validated_app.run(workers=2, max_body_size=0)
                with self.assertRaisesRegex(ValueError, "tls_cert does not exist"):
                    validated_app.run(
                        workers=2, tls_cert="missing.pem", tls_key="missing.key"
                    )
            supervisor.assert_not_called()
        finally:
            del validated_app

    def test_supervisor_gets_target_settings_and_import_path(self):
        global supervised_app
        supervised_app = App()
        try:
            with patch("bessemer.supervisor.Supervisor") as supervisor:
                supervisor.return_value.workers = 3
                supervisor.return_value.run.return_value = 0
                supervised_app.run(workers=3, port=9000, loops=2)
            target, settings = supervisor.call_args.args
            self.assertEqual(target, f"{__name__}:supervised_app")
            self.assertEqual(settings["workers"], 3)
            self.assertEqual(settings["port"], 9000)
            self.assertEqual(settings["loops"], 2)
            self.assertIn("worker_timeout", settings)
        finally:
            del supervised_app

    def test_process_local_metrics_reject_multiple_workers(self):
        global metrics_app
        metrics_app = App(metrics_path="/metrics", metrics_token_env="METRICS_TOKEN")
        try:
            with patch("bessemer.supervisor.Supervisor") as supervisor:
                supervisor.return_value.workers = 2
                with self.assertRaisesRegex(ValueError, "workers=1"):
                    metrics_app.run(workers=2)
            supervisor.return_value.run.assert_not_called()
        finally:
            del metrics_app

    def test_failed_supervisor_raises(self):
        global failing_app
        failing_app = App()
        try:
            with patch("bessemer.supervisor.Supervisor") as supervisor:
                supervisor.return_value.workers = 2
                supervisor.return_value.run.return_value = 1
                with self.assertRaisesRegex(RuntimeError, "exit code 1"):
                    failing_app.run(workers=2)
        finally:
            del failing_app


@unittest.skipUnless(sys.platform == "linux", "the supervisor is qualified on Linux")
class RunWorkersIntegrationTests(unittest.TestCase):
    def test_script_serves_from_several_worker_processes(self):
        try:
            import bessemer._native  # noqa: F401
        except ImportError:
            self.skipTest("native extension is not built")
        port = _free_port()
        with (
            tempfile.TemporaryDirectory() as scripts,
            tempfile.TemporaryDirectory() as elsewhere,
        ):
            script = Path(scripts, "service.py")
            script.write_text(textwrap.dedent(SCRIPT.format(port=port, workers=2)))
            environment = os.environ.copy()
            environment.pop("BESSEMER_SUPERVISED_WORKER", None)
            # Run from another directory: workers must still find the script.
            process = subprocess.Popen(
                [sys.executable, str(script)],
                cwd=elsewhere,
                env=environment,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )
            try:
                pids = set()
                deadline = time.monotonic() + 30
                while len(pids) < 2 and time.monotonic() < deadline:
                    if process.poll() is not None:
                        self.fail(process.stderr.read().decode())
                    pid = _get_pid(port)
                    if pid is None:
                        time.sleep(0.05)
                    else:
                        pids.add(pid)
                self.assertEqual(len(pids), 2, "requests did not reach two workers")
                self.assertNotIn(process.pid, pids)
            finally:
                process.send_signal(signal.SIGTERM)
                try:
                    returncode = process.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
                    raise
                process.stderr.close()
            self.assertEqual(returncode, 0)


if __name__ == "__main__":
    unittest.main()
