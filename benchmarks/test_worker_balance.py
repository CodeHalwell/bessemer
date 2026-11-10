"""No-server checks for the worker-balance diagnostic's failure accounting."""

from __future__ import annotations

import importlib.util
import io
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import closing, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent / "concurrency"))

import worker_balance
from server import Server, Variant


class BalanceTests(unittest.TestCase):
    def test_native_health_is_only_a_bessemer_tier_zero_route(self):
        path = worker_balance.HERE / "app.py"
        routes = {}
        frameworks = ["bessemer"]
        if importlib.util.find_spec("fastapi") is not None:
            frameworks.append("fastapi")
        for framework in frameworks:
            spec = importlib.util.spec_from_file_location(
                f"benchmark_app_{framework}", path
            )
            module = importlib.util.module_from_spec(spec)
            with patch.dict(os.environ, {"BENCHMARK_FRAMEWORK": framework}):
                spec.loader.exec_module(module)
            if framework == "bessemer":
                routes[framework] = [
                    (route["kind"], route["tier"])
                    for route in module.app._export_routes()
                    if route["path"] == "/native-healthz"
                ]
            else:
                routes[framework] = [
                    route.path
                    for route in module.app.routes
                    if route.path == "/native-healthz"
                ]
        expected = {"bessemer": [("health", 0)]}
        if "fastapi" in frameworks:
            expected["fastapi"] = []
        self.assertEqual(routes, expected)

    def test_default_cli_workload_remains_json(self):
        result = {"valid": True, "cleanup": {}}
        argv = [
            "worker_balance.py",
            "--python",
            "python",
            "--oha",
            "oha",
            "--out",
            "/tmp/unused-worker-balance.json",
        ]
        with (
            patch.object(sys, "argv", argv),
            patch.object(worker_balance, "run", return_value=result) as run,
            redirect_stdout(io.StringIO()),
        ):
            worker_balance.main()
        args = run.call_args.args[0]
        self.assertEqual(args.workload, "json")
        self.assertIs(worker_balance.selected_workload(args), worker_balance.JSON)

    def test_native_control_uses_native_health_path_and_app_source_hash(self):
        args = SimpleNamespace(
            oha="oha", client_threads=2, connections=256, workload="native"
        )
        workload = worker_balance.selected_workload(args)
        self.assertEqual(
            (workload.path, workload.status, workload.expected),
            ("/native-healthz", 200, {"ok": True}),
        )
        self.assertEqual(
            worker_balance.oha_command(args, 8000, 2, None)[-1],
            "http://127.0.0.1:8000/native-healthz",
        )
        self.assertNotIn(workload, worker_balance.WORKLOADS)
        source = worker_balance.source_hashes()
        self.assertIn(str(worker_balance.HERE / "app.py"), source)
        self.assertEqual(len(source[str(worker_balance.HERE / "app.py")]), 64)

    def test_native_control_checks_body_and_sqlite_response_length(self):
        class FakeResponse(io.BytesIO):
            status = 200

        workload = worker_balance.NATIVE
        body = b'{"ok":true}'
        opener = SimpleNamespace(open=lambda *_args, **_kwargs: FakeResponse(body))
        workload.validate(opener, "http://127.0.0.1:8000")
        wrong = SimpleNamespace(
            open=lambda *_args, **_kwargs: FakeResponse(b'{"ok":false}')
        )
        with self.assertRaisesRegex(ValueError, "unexpected response body"):
            workload.validate(wrong, "http://127.0.0.1:8000")
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "native.sqlite"
            with closing(sqlite3.connect(database)) as connection, connection:
                connection.execute(
                    "CREATE TABLE oha(status INTEGER, duration REAL, len_bytes INTEGER, start REAL, end REAL)"
                )
                connection.executemany(
                    "INSERT INTO oha VALUES(?, ?, ?, ?, ?)",
                    [
                        (200, 0.01, len(body), 0.0, 0.01),
                        (200, 0.02, len(body) + 1, 0.1, 0.12),
                    ],
                )
            parsed = worker_balance.parse_measured(
                {
                    "database": str(database),
                    "oha": {
                        "statusCodeDistribution": {"200": 2},
                        "errorDistribution": {},
                        "summary": {"total": 2.0},
                    },
                },
                len(body),
            )
        self.assertEqual(
            (parsed["completed"], parsed["successful"], parsed["wrong_body_length"]),
            (2, 1, 1),
        )

    def test_default_settings_are_additive_to_existing_server_variant(self):
        variant = Variant(
            "default", sys.executable, loop=None, workers=4, tuned_limits=False
        )
        with (
            patch("server.subprocess.Popen") as popen,
            patch.object(Server, "wait_ready"),
        ):
            popen.return_value.poll.return_value = 0
            server = Server(variant, io.StringIO())
            server.__enter__()
        command = server.command
        environment = popen.call_args.kwargs["env"]
        self.assertEqual(command[-2:], ["--workers", "4"])
        self.assertNotIn("--max-in-flight", command)
        self.assertNotIn("BESSEMER_EVENT_LOOP", environment)
        self.assertEqual(environment["BENCHMARK_FRAMEWORK"], "bessemer")
        old = Variant("legacy", sys.executable)
        self.assertEqual(
            (old.loop, old.workers, old.tuned_limits), ("asyncio", 1, True)
        )
        with (
            patch("server.subprocess.Popen") as popen,
            patch.object(Server, "wait_ready"),
        ):
            popen.return_value.poll.return_value = 0
            legacy = Server(old, io.StringIO())
            legacy.__enter__()
        self.assertEqual(legacy.command[-2:], ["--workers", "1"])
        self.assertIn("--max-in-flight", legacy.command)
        self.assertEqual(
            popen.call_args.kwargs["env"]["BESSEMER_EVENT_LOOP"], "asyncio"
        )

    def test_executable_normalisation_keeps_venv_symlink(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            interpreter = root / "base-python"
            interpreter.touch()
            venv = root / "venv" / "bin"
            venv.mkdir(parents=True)
            python = venv / "python"
            python.symlink_to(interpreter)
            args = SimpleNamespace(python=str(python), oha="oha")
            worker_balance.normalise_executables(args)
            self.assertEqual(args.python, str(python))
            self.assertEqual(args.oha, "oha")

    def test_direct_entry_loads_shared_records_parser_outside_checkout(self):
        directory = Path(__file__).resolve().parent / "concurrency"
        script = (
            f"import sys; sys.path.insert(0, {str(directory)!r}); "
            "import worker_balance; "
            "print(worker_balance.records_parser().__code__.co_filename)"
        )
        environment = {
            key: value for key, value in os.environ.items() if key != "PYTHONPATH"
        }
        with tempfile.TemporaryDirectory() as outside:
            output = subprocess.check_output(
                [sys.executable, "-c", script],
                cwd=outside,
                env=environment,
                text=True,
            )
        self.assertEqual(
            Path(output.strip()).resolve(),
            directory.parent / "efficiency" / "measure.py",
        )

    def test_denied_socket_inspection_stays_null_when_lsof_fails(self):
        class FakeProcess:
            def __init__(self, _pid):
                pass

            def net_connections(self, **_kwargs):
                raise PermissionError("denied")

        psutil = SimpleNamespace(Process=FakeProcess, Error=OSError)
        with (
            patch.object(worker_balance, "psutil_module", return_value=psutil),
            patch.object(
                worker_balance, "lsof_counts", return_value=({}, "lsof exit 1: denied")
            ),
        ):
            counts, errors = worker_balance.socket_counts([11, 12], 8000)
        self.assertEqual(counts, {"11": None, "12": None})
        self.assertIn("11", errors)
        self.assertIn("12", errors)
        self.assertIn("lsof", errors)

    def test_partial_lsof_fallback_does_not_invent_zero_for_unseen_worker(self):
        class FakeProcess:
            def __init__(self, _pid):
                pass

            def net_connections(self, **_kwargs):
                raise PermissionError("denied")

        psutil = SimpleNamespace(Process=FakeProcess, Error=OSError)
        with (
            patch.object(worker_balance, "psutil_module", return_value=psutil),
            patch.object(worker_balance, "lsof_counts", return_value=({11: 20}, None)),
        ):
            counts, errors = worker_balance.socket_counts([11, 12], 8000)
        self.assertEqual(counts, {"11": 20, "12": None})
        self.assertNotIn("11", errors)
        self.assertIn("12", errors)

    def test_ownership_requires_observed_sockets_for_every_worker(self):
        classify = worker_balance.ownership_status
        self.assertEqual(classify({"11": None, "12": 20}, 20), "unavailable")
        self.assertEqual(classify({"11": 0, "12": 0}, 20), "empty")
        self.assertEqual(classify({"11": 5, "12": 10}, 20), "partial")
        self.assertEqual(classify({"11": 5, "12": 15}, 20), "complete")

    def test_cleanup_fallback_terminates_tracked_worker_group(self):
        server = SimpleNamespace(
            process=SimpleNamespace(pid=11, returncode=0), forced_kill=False
        )
        with (
            patch.object(worker_balance, "live_pids", side_effect=[[12], [], []]),
            patch.object(
                worker_balance, "signal_owned", return_value=None
            ) as signal_owned,
        ):
            receipt = worker_balance.cleanup_receipt(server, {11: 1.0, 12: 2.0})
        self.assertEqual(receipt["fallback_sigterm_pids"], [12])
        self.assertEqual(receipt["remaining_pids"], [])
        signal_owned.assert_called_once_with(12, 2.0, worker_balance.signal.SIGTERM)

    def test_signal_owned_handles_worker_exit_between_scan_and_signal(self):
        class NoSuchProcess(Exception):
            pass

        class AccessDenied(Exception):
            pass

        class FakeProcess:
            def __init__(self, _pid):
                raise NoSuchProcess()

        psutil = SimpleNamespace(
            Process=FakeProcess,
            NoSuchProcess=NoSuchProcess,
            Error=(NoSuchProcess, AccessDenied),
        )
        with (
            patch.object(worker_balance, "psutil_module", return_value=psutil),
            patch.object(worker_balance.os, "killpg") as killpg,
            patch.object(worker_balance.os, "kill") as kill,
        ):
            self.assertIsNone(
                worker_balance.signal_owned(12, 2.0, worker_balance.signal.SIGTERM)
            )
        killpg.assert_not_called()
        kill.assert_not_called()

    def test_nonzero_supervisor_exit_invalidates_receipt(self):
        with tempfile.TemporaryDirectory() as directory:
            args = SimpleNamespace(out=Path(directory) / "result.json", connections=2)
            result = {
                "measurement": {
                    "snapshots": {"middle": {"established_sockets": {"11": 2}}}
                }
            }
            cleanup = {
                "supervisor_exit_code": 1,
                "forced_kill": False,
                "fallback_sigterm_pids": [],
                "remaining_pids": [],
                "signal_errors": {},
            }
            with patch.object(worker_balance, "cleanup_receipt", return_value=cleanup):
                worker_balance.finish_receipt(result, args, SimpleNamespace(), {})
        self.assertEqual(result["ownership_status"], "complete")
        self.assertFalse(result["valid"])

    def test_completed_response_rate_excludes_status_and_transport_failures(self):
        raw = {
            "statusCodeDistribution": {"200": 2, "503": 1},
            "errorDistribution": {"timeout": 1},
            "summary": {"total": 2.0},
        }
        records = {
            "completed": 3,
            "successful": 2,
            "unexpected_status": 1,
            "wrong_body_length": 0,
        }
        result = worker_balance.reconcile(raw, records)
        self.assertEqual(
            (result["successful_rps"], result["transport_errors"]), (1.0, 1)
        )
        with self.assertRaisesRegex(ValueError, "SQLite rows"):
            worker_balance.reconcile(raw, records | {"completed": 4})

    def test_parse_measured_reads_sqlite_path_and_reconciles_rows(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "oha.sqlite"
            with closing(sqlite3.connect(database)) as connection, connection:
                connection.execute(
                    "CREATE TABLE oha(status INTEGER, duration REAL, len_bytes INTEGER, start REAL, end REAL)"
                )
                connection.executemany(
                    "INSERT INTO oha VALUES(?, ?, ?, ?, ?)",
                    [
                        (200, 0.01, 22, 0.0, 0.01),
                        (200, 0.02, 21, 0.1, 0.12),
                        (503, 0.03, 0, 0.2, 0.23),
                    ],
                )
            result = worker_balance.parse_measured(
                {
                    "database": str(database),
                    "oha": {
                        "statusCodeDistribution": {"200": 2, "503": 1},
                        "errorDistribution": {"timeout": 1},
                        "summary": {"total": 2.0},
                    },
                },
                22,
            )
        self.assertEqual(result["completed"], 3)
        self.assertEqual(result["successful"], 1)
        self.assertEqual(result["unexpected_status"], 1)
        self.assertEqual(result["wrong_body_length"], 1)
        self.assertEqual(result["transport_errors"], 1)
        self.assertEqual(result["successful_rps"], 0.5)


if __name__ == "__main__":
    unittest.main()
