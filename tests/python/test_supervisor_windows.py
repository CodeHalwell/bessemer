"""The process supervisor on Windows: shared listener, drain and job cleanup.

These run real worker processes through the installed CLI. They are skipped
off Windows; test_supervisor.py covers the POSIX supervisor.
"""

import http.client
import importlib.util
import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

APPLICATION = """
import asyncio
from contextlib import asynccontextmanager
import os
from pathlib import Path
from bessemer import App

@asynccontextmanager
async def lifespan(app):
    import faulthandler, sys
    _dump = open(f'dump_{os.getpid()}.txt', 'w')
    faulthandler.dump_traceback_later(1, repeat=True, file=_dump)
    Path(f'started_{os.getpid()}').touch()
    yield
    Path(f'closed_{os.getpid()}').touch()

app = App(lifespan=lifespan)
app.health('/healthz')

@app.get('/pid')
async def pid():
    await asyncio.sleep(0.02)
    return {'pid': os.getpid()}

@app.get('/crash')
async def crash():
    os._exit(42)
"""


def _alive(pid: int) -> bool:
    import ctypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    synchronize = 0x00100000
    handle = kernel32.OpenProcess(synchronize, False, pid)
    if not handle:
        return False
    try:
        # WAIT_TIMEOUT (0x102): still running.
        return kernel32.WaitForSingleObject(handle, 0) == 0x102
    finally:
        kernel32.CloseHandle(handle)


def _native_stacks(pid: int) -> str:
    import glob

    found = glob.glob(r"C:\Program Files (x86)\Windows Kits\10\Debuggers\x64\cdb.exe")
    if not found:
        return "(no cdb)"
    try:
        result = subprocess.run(
            [
                found[0],
                "-pv",
                "-p",
                str(pid),
                "-c",
                ".symfix c:\\symbols; .reload; ~*k 40; q",
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=120,
            errors="replace",
        )
    except subprocess.TimeoutExpired as error:
        return f"(cdb timed out) {error.stdout}"
    return result.stdout[-20000:]


@unittest.skipUnless(
    sys.platform == "win32" and importlib.util.find_spec("bessemer._native"),
    "Windows supervisor; build and install the native extension first",
)
class WindowsSupervisorTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="bessemer-win-")
        self.root = Path(self.directory.name)
        (self.root / "fixture_app.py").write_text(APPLICATION, encoding="utf-8")
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            self.port = probe.getsockname()[1]
        self.log = tempfile.TemporaryFile(mode="w+")  # noqa: SIM115 -- closed in tearDown.
        self.process = None

    def tearDown(self):
        if self.process is not None and self.process.poll() is None:
            self.process.kill()
            self.process.wait(timeout=10)
        self.log.close()
        # Workers may still be releasing the directory for a moment.
        for _ in range(50):
            try:
                self.directory.cleanup()
                break
            except OSError:
                time.sleep(0.1)

    def start(self, workers: int, *extra: str) -> None:
        self.process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "bessemer",
                "run",
                "fixture_app:app",
                "--port",
                str(self.port),
                "--workers",
                str(workers),
                "--startup-timeout",
                "20",
                "--graceful-timeout",
                "5",
                *extra,
            ],
            cwd=self.root,
            stdout=self.log,
            stderr=self.log,
            # Its own group, so the test can send it CTRL_BREAK alone.
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
        )
        self.wait(lambda: self.request("/healthz")[0] == 200, timeout=40)

    def request(self, path: str):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            connection.request("GET", path)
            response = connection.getresponse()
            return response.status, response.read()
        finally:
            connection.close()

    def pid(self):
        status, body = self.request("/pid")
        return json.loads(body)["pid"] if status == 200 else None

    def wait(self, predicate, timeout=20):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                self.fail(f"supervisor exited early: {self.output()}")
            try:
                result = predicate()
            except (OSError, http.client.HTTPException):
                result = None
            if result:
                return result
            time.sleep(0.05)
        self.fail(f"condition not met in {timeout}s: {self.output()}")

    def output(self) -> str:
        self.log.seek(0)
        return self.log.read()

    def worker_pids(self, count: int) -> set[int]:
        seen: set[int] = set()

        def enough():
            for _ in range(20):
                pid = self.pid()
                if pid is not None:
                    seen.add(pid)
            return len(seen) >= count

        self.wait(enough, timeout=30)
        return seen

    def test_workers_share_the_listener(self):
        self.start(2)
        pids = self.worker_pids(2)
        self.assertEqual(len(pids), 2)
        self.assertNotIn(self.process.pid, pids)

    def test_ctrl_break_drains_workers_and_runs_lifespan_cleanup(self):
        for attempt in range(15):
            with self.subTest(attempt=attempt):
                self._drain_once()
            if self.process.poll() is None:
                self.process.kill()
                self.process.wait(timeout=10)
            self.log.truncate(0)
            self.log.seek(0)
            with socket.socket() as probe:
                probe.bind(("127.0.0.1", 0))
                self.port = probe.getsockname()[1]

    def _drain_once(self):
        self.start(2, "--graceful-timeout", "40")
        pids = self.worker_pids(2)
        os.kill(self.process.pid, signal.CTRL_BREAK_EVENT)
        deadline = time.monotonic() + 4
        while any(_alive(pid) for pid in pids) and time.monotonic() < deadline:
            time.sleep(0.05)
        native = ""
        for pid in pids:
            if _alive(pid):
                native += f"\n--- native stacks of {pid} ---\n" + _native_stacks(pid)
        self.assertEqual(self.process.wait(timeout=90), 0, self.output() + native)
        if native:
            self.fail(self.output() + native)
        for pid in pids:
            dump = self.root / f"dump_{pid}.txt"
            text = dump.read_text(errors="replace") if dump.exists() else "(no dump)"
            tail = text[-6000:]
            self.assertTrue(
                (self.root / f"closed_{pid}").exists(),
                self.output() + f"\n--- last stacks of {pid} ---\n" + tail,
            )
            self.assertFalse(_alive(pid))

    def test_crashed_worker_is_replaced(self):
        self.start(1)
        original = self.pid()
        with self.assertRaises((OSError, http.client.HTTPException)):
            self.request("/crash")
        replacement = self.wait(lambda: (pid := self.pid()) and pid != original and pid)
        self.assertNotEqual(replacement, original)

    def test_killed_supervisor_takes_its_workers_with_it(self):
        self.start(2)
        pids = self.worker_pids(2)
        self.process.kill()
        self.process.wait(timeout=10)
        deadline = time.monotonic() + 10
        while any(_alive(pid) for pid in pids) and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertFalse(any(_alive(pid) for pid in pids))


if __name__ == "__main__":
    unittest.main()
