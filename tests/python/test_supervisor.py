"""Real subprocess proofs for supervision, recovery and hard shutdown."""

import http.client
import importlib.util
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import Mock, patch

from bessemer.config import DEFAULTS
from bessemer.supervisor import Supervisor, _Child, cpu_quota_count

SUPERVISED_APPLICATION = """
import asyncio
from contextlib import asynccontextmanager
import os
from pathlib import Path
import time
from bessemer import App

@asynccontextmanager
async def lifespan(app):
    Path(f'started_{os.getpid()}').touch()
    yield
    Path(f'closed_{os.getpid()}').touch()

app = App(lifespan=lifespan)
app.health('/healthz')

@app.get('/pid')
async def pid():
    return {'pid': os.getpid()}

@app.get('/crash')
async def crash():
    os._exit(42)

@app.get('/stall')
async def stall():
    Path('blocked').touch()
    time.sleep(120)
    return {'done': True}

@app.get('/blocking')
def blocking():
    Path('blocked').touch()
    time.sleep(120)
    return {'done': True}

@app.get('/draining')
async def draining():
    Path('draining').touch()
    await asyncio.sleep(0.8)
    return {'drained': True}
"""

PARENT_DEATH_PROBE = r"""
import http.client
import json
import os
from pathlib import Path
import signal
import sys
import time
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, sys.argv[1])
from test_native_integration import Server
from test_supervisor import SUPERVISED_APPLICATION

def process_identity(pid):
    try:
        fields = Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()
        return fields[0], fields[19]
    except (FileNotFoundError, ProcessLookupError):
        # The process exited (or was reaped mid-read): it has no identity.
        return None

server = Server(
    application=SUPERVISED_APPLICATION,
    extra_args=('--worker-timeout', '0', '--graceful-timeout', '0.2'),
)
worker_pid = None
identity = None
try:
    status, _, body = server.request('GET', '/pid')
    assert status == 200
    worker_pid = json.loads(body)['pid']
    identity = process_identity(worker_pid)
    print(f'worker_pid={worker_pid}', flush=True)
    with ThreadPoolExecutor(max_workers=1) as executor:
        blocked = executor.submit(server.request, 'GET', '/stall')
        deadline = time.monotonic() + 3
        while not (server.root / 'blocked').exists():
            assert time.monotonic() < deadline, 'worker did not block its event loop'
            time.sleep(0.01)
        server.process.kill()
        server.process.wait(timeout=2)
        deadline = time.monotonic() + 2
        while True:
            current = process_identity(worker_pid)
            if current is None or current[0] == 'Z' or current[1] != identity[1]:
                break
            assert time.monotonic() < deadline, 'blocked worker survived supervisor SIGKILL'
            time.sleep(0.01)
        try:
            blocked.result(timeout=2)
        except (OSError, http.client.HTTPException):
            pass
        else:
            raise AssertionError('blocked request unexpectedly completed')
finally:
    if worker_pid is not None:
        current = process_identity(worker_pid)
        if identity is not None and current is not None and current[0] != 'Z' and current[1] == identity[1]:
            os.kill(worker_pid, signal.SIGKILL)
    server.close()
"""

DESCENDANT_PROBE = r"""
import os
from pathlib import Path
import signal
import sys
import time
sys.path.insert(0, sys.argv[1])
from test_supervisor import Harness

def alive(pid):
    try:
        return Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()[0] != 'Z'
    except (FileNotFoundError, ProcessLookupError):
        # Gone, or reaped while /proc was being read.
        return False

harness = Harness(sys.argv[2], restart_limit=0)
descendant = None
try:
    harness.wait(lambda: harness.records('descendant'))
    descendant = harness.records('descendant')[0][1]
    print(f'descendant_pid={descendant}', flush=True)
    if sys.argv[2] == 'stop-with-descendant':
        harness.wait(lambda: harness.records('ready'))
        harness.supervisor.request_stop()
    harness.thread.join(timeout=3)
    assert not harness.thread.is_alive(), 'supervisor failed to stop'
    assert harness.result == (1 if sys.argv[2] == 'crash-with-descendant' else 0)
    deadline = time.monotonic() + 2
    while alive(descendant):
        assert time.monotonic() < deadline, 'worker descendant survived its leader'
        time.sleep(0.01)
finally:
    if descendant is not None and alive(descendant):
        os.kill(descendant, signal.SIGKILL)
    harness.close()
"""


def run_subreaper(probe, *arguments):
    compiler = shutil.which("cc") or shutil.which("gcc")
    if compiler is None:
        raise unittest.SkipTest(
            "C compiler required for the isolated Linux subreaper harness"
        )
    tests = Path(__file__).resolve().parent
    fixture = tests.parent / "security" / "parent_death_harness.c"
    with tempfile.TemporaryDirectory(prefix="bessemer-parent-death-") as directory:
        harness = str(Path(directory, "parent_death_harness"))
        subprocess.run(
            [
                compiler,
                "-std=c11",
                "-Wall",
                "-Wextra",
                "-Werror",
                str(fixture),
                "-o",
                harness,
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
        result = subprocess.run(
            [harness, sys.executable, "-c", probe, str(tests), *arguments],
            check=False,
            capture_output=True,
            text=True,
            timeout=25,
        )
    if result.returncode == 77:
        raise unittest.SkipTest(f"Linux subreaper unavailable: {result.stderr}")
    return result


FAKE_WORKER = r"""
import argparse
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import time

parser = argparse.ArgumentParser()
parser.add_argument("--events", required=True)
parser.add_argument("mode")
parser.add_argument("--listener-fd", type=int, required=True)
parser.add_argument("--ready-fd", type=int, required=True)
parser.add_argument("--worker-count", type=int, required=True)
parser.add_argument("--parent-pid", type=int, required=True)
parser.add_argument("--settings", required=True)
args = parser.parse_args()
events = Path(args.events)

def record(event):
    with events.open("a") as stream:
        stream.write(f"{event} {os.getpid()} {time.monotonic()}\n")

record("started")
if args.mode == 'check-startup-budget':
    settings = json.loads(args.settings)
    if settings.get('startup_timeout') != 2 or settings.get('graceful_timeout') != 0.1:
        record('bad-startup-budget')
        os._exit(78)
stopping = False
def terminate(signum, frame):
    global stopping
    record("term")
    stopping = True
signal.signal(signal.SIGTERM, terminate)
drain_on_ready = False
if args.mode == 'reload-self-drain-once':
    counter = Path(str(events) + '.count')
    generation = int(counter.read_text()) + 1 if counter.exists() else 1
    counter.write_text(str(generation))
    drain_on_ready = generation == 2
    if drain_on_ready:
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
if args.mode in {'crash-with-descendant', 'stop-with-descendant'}:
    marker = str(events) + '.descendant-ready'
    descendant = subprocess.Popen([
        sys.executable, '-c',
        'import signal,sys,time;from pathlib import Path;signal.signal(signal.SIGTERM,signal.SIG_IGN);Path(sys.argv[1]).touch();time.sleep(120)',
        marker,
    ])
    while not Path(marker).exists():
        time.sleep(0.005)
    with events.open('a') as stream:
        stream.write(f'descendant {descendant.pid} {time.monotonic()}\n')
    if args.mode == 'crash-with-descendant':
        os._exit(13)
if args.mode == "crash-always":
    os._exit(13)
if args.mode == "bad-config":
    os._exit(78)
if args.mode == "reload-fails":
    try:
        descriptor = os.open(str(events) + ".once", os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        os._exit(13)
    else:
        os.close(descriptor)
if args.mode == "no-ready":
    while not stopping:
        time.sleep(0.01)
    raise SystemExit(0)
if args.mode == "ignore-term":
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
lose_control = False
if args.mode == "reload-loses-control":
    try:
        descriptor = os.open(str(events) + ".once", os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        lose_control = True
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
    else:
        os.close(descriptor)
if args.mode == "lost-control-once":
    try:
        descriptor = os.open(str(events) + ".once", os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        pass
    else:
        os.close(descriptor)
        lose_control = True
        signal.signal(signal.SIGTERM, signal.SIG_IGN)

listener = socket.socket(fileno=args.listener_fd)
listener.settimeout(0.05)
record("ready")
os.write(args.ready_fd, b"1")
self_draining = False
if lose_control:
    os.close(args.ready_fd)
if args.mode == "recycle-once":
    try:
        descriptor = os.open(str(events) + ".once", os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        pass
    else:
        os.close(descriptor)
        os._exit(0)
if args.mode == "crash-once":
    try:
        descriptor = os.open(str(events) + ".once", os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        pass
    else:
        os.close(descriptor)
        time.sleep(0.05)
        os._exit(13)
while not stopping:
    if drain_on_ready or Path(str(events) + f'.drain-{os.getpid()}').exists():
        if not self_draining:
            listener.close()
            self_draining = True
        try:
            os.write(args.ready_fd, b'dh')
        except OSError:
            pass
        time.sleep(0.01)
        continue
    if args.mode == "slow-heartbeat":
        # A real worker heartbeats every 0.5s, longer than this startup budget.
        time.sleep(0.45)
    if args.mode != "stalled":
        try:
            os.write(args.ready_fd, b"h")
        except OSError:
            if args.mode != "ignore-term" and not lose_control:
                break
    try:
        connection, address = listener.accept()
    except socket.timeout:
        continue
    with connection:
        connection.settimeout(0.2)
        try:
            connection.recv(8192)
            payload = str(os.getpid()).encode()
            connection.sendall(b"HTTP/1.1 200 OK\r\nConnection: close\r\nContent-Length: " + str(len(payload)).encode() + b"\r\n\r\n" + payload)
        except OSError:
            pass
record("stopped")
"""


class Harness:
    def __init__(self, mode="normal", **overrides):
        self.directory = tempfile.TemporaryDirectory(prefix="bessemer-supervisor-")
        self.root = Path(self.directory.name)
        script = self.root / "worker.py"
        script.write_text(FAKE_WORKER)
        self.events = self.root / "events"
        settings = (
            DEFAULTS
            | {
                "workers": 1,
                "host": "127.0.0.1",
                "port": 0,
                "graceful_timeout": 0.15,
                "startup_timeout": 0.3,
                "restart_limit": 2,
                "restart_backoff": 0.03,
            }
            | overrides
        )
        self.supervisor = Supervisor(
            mode,
            settings,
            command=(sys.executable, str(script), "--events", str(self.events)),
        )
        self.result = None
        self.error = None

        def run():
            try:
                self.result = self.supervisor.run(install_signal_handlers=False)
            except BaseException as error:  # noqa: BLE001 -- Propagate background failures to the test thread.
                self.error = error

        self.thread = threading.Thread(target=run, daemon=True)
        self.thread.start()

    def records(self, kind=None):
        try:
            records = [
                (parts[0], int(parts[1]), float(parts[2]))
                for line in self.events.read_text().splitlines()
                if len(parts := line.split()) == 3
            ]
        except FileNotFoundError:
            return []
        return [record for record in records if kind is None or record[0] == kind]

    def wait(self, predicate, *, timeout=4):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.error is not None:
                raise self.error
            value = predicate()
            if value:
                return value
            time.sleep(0.01)
        raise AssertionError(
            f"supervisor condition did not become true; events={self.records()}"
        )

    def request(self):
        address = self.wait(lambda: self.supervisor.bound_address)
        connection = http.client.HTTPConnection(address[0], address[1], timeout=1)
        try:
            connection.request("GET", "/")
            response = connection.getresponse()
            if response.status != 200:
                raise AssertionError(f"unexpected status {response.status}")
            return int(response.read())
        finally:
            connection.close()

    def close(self):
        self.supervisor.request_stop()
        self.thread.join(timeout=3)
        try:
            if self.thread.is_alive():
                raise AssertionError("supervisor did not honor shutdown deadline")
            if self.error is not None:
                raise self.error
        finally:
            self.directory.cleanup()


@unittest.skipUnless(
    sys.platform == "linux", "cgroup quotas and CPU affinity are Linux-only"
)
class QuotaTests(unittest.TestCase):
    def test_nested_cgroup_uses_tightest_ancestor_quota(self):
        original_read = Path.read_text
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            child = root / "group" / "worker"
            child.mkdir(parents=True)
            (root / "cpu.max").write_text("400000 100000\n")
            (child / "cpu.max").write_text("150000 100000\n")

            def read(path, *args, **kwargs):
                if str(path) == "/proc/self/cgroup":
                    return "0::/group/worker\n"
                return original_read(path, *args, **kwargs)

            with (
                patch.object(Path, "read_text", read),
                patch(
                    "bessemer.supervisor.os.sched_getaffinity",
                    return_value=set(range(16)),
                ),
            ):
                self.assertEqual(cpu_quota_count(root), 2)

    def test_cgroup_v2_quota_and_affinity_bound_worker_defaults(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            patch(
                "bessemer.supervisor.os.sched_getaffinity", return_value=set(range(16))
            ),
        ):
            root = Path(directory)
            (root / "cpu.max").write_text("150000 100000\n")
            self.assertEqual(cpu_quota_count(root), 2)
            (root / "cpu.max").write_text("50000 100000\n")
            self.assertEqual(cpu_quota_count(root), 1)
            (root / "cpu.max").write_text("max 100000\n")
            self.assertEqual(cpu_quota_count(root), 16)

    def test_cgroup_v1_quota_and_malformed_values(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            patch(
                "bessemer.supervisor.os.sched_getaffinity", return_value=set(range(16))
            ),
        ):
            root = Path(directory)
            cpu = root / "cpu"
            cpu.mkdir()
            (cpu / "cpu.cfs_quota_us").write_text("250000\n")
            (cpu / "cpu.cfs_period_us").write_text("100000\n")
            self.assertEqual(cpu_quota_count(root), 3)
            (cpu / "cpu.cfs_period_us").write_text("broken\n")
            self.assertEqual(cpu_quota_count(root), 16)


@unittest.skipUnless(os.name == "posix", "requires inherited POSIX file descriptors")
class SupervisorTests(unittest.TestCase):
    def test_self_drain_keeps_first_deadline_and_prevents_promotion(self):
        supervisor = Supervisor(
            "normal", DEFAULTS | {"workers": 1, "worker_timeout": 0}
        )
        reader, writer = os.pipe()
        os.set_blocking(reader, False)
        child = _Child(Mock(returncode=None, pid=123), reader, 0, ready=True)
        supervisor._selector.register(reader, 1, child)
        try:
            os.write(writer, b"d")
            supervisor._ready_events(0)
            deadline = child.deadline
            os.write(writer, b"dhdd")
            supervisor._ready_events(0)
            self.assertTrue(child.self_draining)
            self.assertEqual(child.deadline, deadline)
            self.assertFalse(supervisor._healthy(child))
        finally:
            os.close(writer)
            supervisor._close_ready(child)
            supervisor._selector.close()

    def test_replacement_needs_a_later_control_read_before_promotion(self):
        supervisor = Supervisor(
            "normal", DEFAULTS | {"workers": 1, "worker_timeout": 0}
        )
        supervisor._signal = Mock()
        children = []
        try:
            for follow_up in (None, b"h"):
                reader, writer = os.pipe()
                os.set_blocking(reader, False)
                child = _Child(Mock(returncode=None, pid=123), reader, 0)
                children.append(child)
                supervisor._selector.register(reader, 1, child)
                # Readiness alone, even with an EOF about to follow, is not
                # enough: the EOF may land just after this wake-up.
                os.write(writer, b"1")
                supervisor._ready_events(0)
                self.assertTrue(child.ready)
                self.assertFalse(child.confirmed)
                if follow_up is None:
                    os.close(writer)
                    supervisor._ready_events(0)
                    self.assertTrue(child.control_failed)
                    self.assertFalse(child.confirmed)
                else:
                    os.write(writer, follow_up)
                    supervisor._ready_events(0)
                    os.close(writer)
                    self.assertTrue(child.confirmed)
                    self.assertFalse(child.control_failed)
        finally:
            for child in children:
                supervisor._close_ready(child)
            supervisor._selector.close()

    def test_replacement_confirmation_is_not_charged_to_startup_budget(self):
        harness = Harness("slow-heartbeat", restart_limit=0, worker_timeout=0)
        try:
            harness.wait(lambda: harness.records("ready"))
            harness.supervisor.request_reload()
            harness.wait(lambda: len(harness.records("ready")) == 2)
            replacement = harness.records("ready")[1][1]
            harness.wait(
                lambda: harness.supervisor._slots[0].active.process.pid == replacement
            )
            self.assertEqual(len(harness.records("started")), 2)
            self.assertTrue(harness.thread.is_alive())
        finally:
            harness.close()

    def test_rolling_reload_retries_self_draining_pending_worker(self):
        harness = Harness(
            "reload-self-drain-once",
            graceful_timeout=0.12,
            worker_timeout=0,
            restart_limit=0,
        )
        try:
            harness.wait(lambda: harness.records("ready"))
            original = harness.records("ready")[0][1]
            harness.supervisor.request_reload()
            harness.wait(lambda: len(harness.records("ready")) >= 3)
            harness.wait(lambda: harness.supervisor._rolling_slot is None)
            harness.wait(
                lambda: original in {row[1] for row in harness.records("stopped")}
            )
            self.assertEqual(harness.request(), harness.records("ready")[-1][1])
            self.assertTrue(harness.thread.is_alive())
        finally:
            harness.close()

    def test_active_self_drain_with_retiree_does_not_grow_pool(self):
        harness = Harness(
            "ignore-term",
            max_lifetime=0.02,
            graceful_timeout=0.6,
            worker_timeout=0,
            restart_limit=0,
        )
        try:
            harness.wait(lambda: len(harness.records("ready")) >= 2)
            harness.supervisor.settings["max_lifetime"] = 0
            active = harness.records("ready")[1][1]
            # Promotion follows the replacement's first post-readiness message.
            harness.wait(
                lambda: harness.supervisor._slots[0].active.process.pid == active
            )
            Path(str(harness.events) + f".drain-{active}").touch()
            harness.wait(lambda: harness.supervisor._slots[0].active.self_draining)
            time.sleep(0.2)
            self.assertEqual(len(harness.records("started")), 2)
            harness.wait(lambda: len(harness.records("ready")) >= 3)
            self.assertTrue(harness.thread.is_alive())
        finally:
            harness.close()

    def test_child_receives_distinct_startup_and_shutdown_budgets(self):
        harness = Harness(
            "check-startup-budget",
            startup_timeout=2,
            graceful_timeout=0.1,
            restart_limit=0,
        )
        try:
            harness.wait(
                lambda: harness.records("ready") or not harness.thread.is_alive()
            )
            self.assertTrue(harness.records("ready"), harness.records())
            self.assertEqual(harness.request(), harness.records("ready")[0][1])
        finally:
            harness.close()

    def test_draining_control_eof_preserves_deadline_without_health_failure(self):
        supervisor = Supervisor("normal", DEFAULTS | {"workers": 1})
        reader, writer = os.pipe()
        os.set_blocking(reader, False)
        child = _Child(Mock(returncode=None, pid=123), reader, 0, draining=True)
        child.deadline = time.monotonic() + 1
        deadline = child.deadline
        supervisor._selector.register(reader, 1, child)
        os.close(writer)
        try:
            with patch.object(supervisor, "_signal") as signals:
                supervisor._ready_events(0)
            signals.assert_not_called()
            self.assertEqual(child.ready_fd, -1)
            self.assertFalse(child.control_failed)
            self.assertEqual(child.deadline, deadline)
        finally:
            supervisor._close_ready(child)
            supervisor._selector.close()

    @unittest.skipUnless(sys.platform == "linux", "Linux race-free group cleanup")
    def test_live_waitid_does_not_reap_concurrent_leader_exit(self):
        process = Mock(returncode=None, pid=123)
        child = _Child(process, -1, 0)
        with patch("bessemer.supervisor.os.waitid", return_value=None):
            self.assertIsNone(Supervisor._poll(child))
        process.poll.assert_not_called()

    @unittest.skipUnless(sys.platform == "linux", "Linux process-group cleanup")
    def test_worker_group_descendants_are_killed_after_leader_exit(self):
        for mode in ("crash-with-descendant", "stop-with-descendant"):
            with self.subTest(mode=mode):
                result = run_subreaper(DESCENDANT_PROBE, mode)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                descendant = next(
                    line.removeprefix("descendant_pid=")
                    for line in result.stdout.splitlines()
                    if line.startswith("descendant_pid=")
                )
                self.assertIn(
                    f"adopted_pid={descendant} signal={signal.SIGKILL}", result.stdout
                )

    def test_shared_listener_recovers_after_worker_crash(self):
        harness = Harness("crash-once")
        try:
            harness.wait(lambda: len(harness.records("ready")) >= 2)
            ready = harness.records("ready")
            self.assertNotEqual(ready[0][1], ready[-1][1])
            self.assertEqual(harness.request(), ready[-1][1])
        finally:
            harness.close()
        self.assertEqual(harness.result, 0)

    def test_crash_loop_has_backoff_and_finite_restart_budget(self):
        harness = Harness("crash-always", restart_limit=2, restart_backoff=0.04)
        try:
            harness.thread.join(timeout=4)
            self.assertFalse(harness.thread.is_alive())
            self.assertEqual(harness.result, 1)
            starts = harness.records("started")
            self.assertEqual(len(starts), 3)
            self.assertGreaterEqual(starts[-1][2] - starts[0][2], 0.1)
        finally:
            harness.close()

    def test_startup_deadline_kills_unready_worker(self):
        harness = Harness("no-ready", restart_limit=0, startup_timeout=0.12)
        try:
            harness.thread.join(timeout=3)
            self.assertFalse(harness.thread.is_alive())
            self.assertEqual(harness.result, 1)
            self.assertEqual(len(harness.records("started")), 1)
            pid = harness.records("started")[0][1]
            with self.assertRaises(ProcessLookupError):
                os.kill(pid, 0)
        finally:
            harness.close()

    def test_configuration_failure_is_not_retried(self):
        harness = Harness("bad-config", restart_limit=5)
        try:
            harness.thread.join(timeout=3)
            self.assertFalse(harness.thread.is_alive())
            self.assertEqual(harness.result, 1)
            self.assertEqual(len(harness.records("started")), 1)
        finally:
            harness.close()

    def test_hard_shutdown_deadline_kills_noncooperative_worker(self):
        harness = Harness("ignore-term", graceful_timeout=0.12)
        try:
            harness.wait(lambda: harness.records("ready"))
            pid = harness.records("ready")[0][1]
            started = time.monotonic()
            harness.supervisor.request_stop()
            harness.thread.join(timeout=2)
            self.assertFalse(harness.thread.is_alive())
            self.assertLess(time.monotonic() - started, 0.75)
            with self.assertRaises(ProcessLookupError):
                os.kill(pid, 0)
        finally:
            harness.close()

    def test_rolling_reload_waits_for_replacement_readiness(self):
        harness = Harness(workers=2)
        try:
            harness.wait(lambda: len(harness.records("ready")) == 2)
            original = {record[1] for record in harness.records("ready")}
            harness.supervisor.request_reload()
            harness.wait(lambda: len(harness.records("ready")) >= 4)
            harness.wait(lambda: len(harness.records("term")) >= 2)
            harness.wait(
                lambda: original.issubset(
                    {record[1] for record in harness.records("stopped")}
                )
            )
            records = harness.records()
            for pid in original:
                term_index = next(
                    index
                    for index, record in enumerate(records)
                    if record[:2] == ("term", pid)
                )
                self.assertTrue(
                    any(
                        record[0] == "ready" and record[1] not in original
                        for record in records[:term_index]
                    )
                )
            self.assertNotIn(harness.request(), original)
        finally:
            harness.close()

    def test_lifetime_recycling_starts_replacement_before_drain(self):
        harness = Harness(max_lifetime=0.15)
        try:
            harness.wait(lambda: len(harness.records("ready")) >= 2)
            harness.wait(lambda: harness.records("term"))
            original = harness.records("ready")[0][1]
            harness.wait(
                lambda: original in {record[1] for record in harness.records("stopped")}
            )
            self.assertNotEqual(
                harness.records("ready")[0][1], harness.records("ready")[-1][1]
            )
            # With a 150 ms lifetime the replacement is itself recycled, so
            # the request may reach any later generation, never the original.
            served = harness.request()
            later = {record[1] for record in harness.records("ready")[1:]}
            self.assertIn(served, later)
            self.assertNotEqual(served, original)
        finally:
            harness.close()

    def test_stalled_heartbeat_recycles_worker(self):
        harness = Harness("stalled", worker_timeout=0.12)
        try:
            harness.wait(lambda: len(harness.records("ready")) >= 2)
            harness.wait(lambda: harness.records("term"))
            self.assertNotEqual(
                harness.records("ready")[0][1], harness.records("ready")[-1][1]
            )
        finally:
            harness.close()

    def test_repeated_stalls_exhaust_restart_budget(self):
        harness = Harness("stalled", worker_timeout=0.12, restart_limit=1)
        try:
            harness.thread.join(timeout=3)
            self.assertFalse(harness.thread.is_alive())
            self.assertEqual(harness.result, 1)
            self.assertEqual(len(harness.records("started")), 2)
        finally:
            harness.close()

    def test_repeated_stop_shortens_in_progress_shutdown(self):
        harness = Harness("ignore-term", graceful_timeout=2)
        try:
            harness.wait(lambda: harness.records("ready"))
            started = time.monotonic()
            harness.supervisor.request_stop()
            time.sleep(0.1)
            # This is the second SIGTERM/SIGINT handler's action. The supervisor
            # must consult the updated deadline while already draining workers.
            harness.supervisor._shutdown_deadline = time.monotonic()
            harness.thread.join(timeout=1)
            self.assertFalse(harness.thread.is_alive())
            self.assertLess(time.monotonic() - started, 0.8)
        finally:
            harness.close()

    def test_failed_reload_keeps_existing_healthy_worker(self):
        harness = Harness("reload-fails", restart_limit=0)
        try:
            harness.wait(lambda: harness.records("ready"))
            original = harness.records("ready")[0][1]
            harness.supervisor.request_reload()
            harness.wait(lambda: len(harness.records("started")) >= 2)
            time.sleep(0.15)
            self.assertTrue(harness.thread.is_alive())
            self.assertEqual(harness.request(), original)
        finally:
            harness.close()

    def test_reload_control_failure_preserves_healthy_original(self):
        harness = Harness("reload-loses-control", restart_limit=0, graceful_timeout=0.1)
        try:
            harness.wait(lambda: harness.records("ready"))
            original = harness.records("ready")[0][1]
            harness.supervisor.request_reload()
            harness.wait(lambda: len(harness.records("ready")) == 2)
            harness.wait(lambda: harness.supervisor._slots[0].pending is None)
            self.assertEqual(harness.request(), original)
            self.assertNotIn(
                original, {record[1] for record in harness.records("term")}
            )
        finally:
            harness.close()

    def test_immediate_ready_normal_exit_does_not_consume_restart_budget(self):
        harness = Harness("recycle-once", restart_limit=0)
        try:
            harness.wait(lambda: len(harness.records("ready")) == 2)
            self.assertEqual(harness.request(), harness.records("ready")[-1][1])
            self.assertTrue(harness.thread.is_alive())
        finally:
            harness.close()

    def test_recycling_does_not_accumulate_draining_workers(self):
        harness = Harness(
            "ignore-term", max_lifetime=0.02, graceful_timeout=0.6, worker_timeout=0
        )
        try:
            harness.wait(lambda: len(harness.records("ready")) >= 2)
            time.sleep(0.3)
            self.assertEqual(len(harness.records("started")), 2)
        finally:
            harness.close()

    def test_lost_control_channel_has_deadline_with_heartbeat_disabled(self):
        harness = Harness("lost-control-once", worker_timeout=0, graceful_timeout=0.12)
        try:
            harness.wait(lambda: len(harness.records("ready")) >= 2)
            original = harness.records("ready")[0][1]

            def gone():
                try:
                    os.kill(original, 0)
                except ProcessLookupError:
                    return True
                return False

            harness.wait(gone)
            self.assertNotEqual(harness.request(), original)
        finally:
            harness.close()

    def test_registration_failure_cleans_untracked_child(self):
        with tempfile.TemporaryDirectory() as directory:
            script = Path(directory, "worker.py")
            script.write_text(FAKE_WORKER)
            settings = DEFAULTS | {"workers": 1, "port": 0, "graceful_timeout": 0.1}
            supervisor = Supervisor(
                "normal",
                settings,
                command=(
                    sys.executable,
                    str(script),
                    "--events",
                    str(Path(directory, "events")),
                ),
            )
            children = []
            original_popen = subprocess.Popen

            def launch(*args, **kwargs):
                process = original_popen(*args, **kwargs)
                children.append(process)
                return process

            with (
                patch.object(
                    supervisor._selector,
                    "register",
                    side_effect=OSError("registration failed"),
                ),
                patch("bessemer.supervisor.subprocess.Popen", side_effect=launch),
                self.assertRaisesRegex(OSError, "registration failed"),
            ):
                supervisor.run(install_signal_handlers=False)
            self.assertEqual(len(children), 1)
            self.assertIsNotNone(children[0].poll())


@unittest.skipUnless(
    os.name == "posix" and importlib.util.find_spec("bessemer._native"),
    "build and install the native extension first",
)
class NativeSupervisorTests(unittest.TestCase):
    """Exercise real worker processes through the installed CLI and HTTP stack."""

    @staticmethod
    def server(*arguments):
        from test_native_integration import Server

        return Server(
            application=SUPERVISED_APPLICATION,
            extra_args=(
                "--startup-timeout",
                "5",
                "--graceful-timeout",
                "0.2",
                *arguments,
            ),
        )

    @staticmethod
    def wait(predicate, timeout=6):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                result = predicate()
            except (OSError, http.client.HTTPException):
                result = None
            if result:
                return result
            time.sleep(0.03)
        raise AssertionError("native supervisor condition did not become true")

    @staticmethod
    def pid(server):
        status, _, body = server.request("GET", "/pid")
        return json.loads(body)["pid"] if status == 200 else None

    def test_native_worker_recovers_from_hard_application_exit(self):
        server = self.server()
        try:
            original = self.pid(server)
            self.assertIsNotNone(original)
            with self.assertRaises((OSError, http.client.HTTPException)):
                server.request("GET", "/crash")
            replacement = self.wait(
                lambda: (pid := self.pid(server)) and pid != original and pid
            )
            self.assertNotEqual(replacement, original)
            self.assertIsNone(server.process.poll())
        finally:
            server.close()

    def test_native_graceful_stop_preserves_lifespan_cleanup(self):
        server = self.server("--graceful-timeout", "2")
        try:
            pid = self.pid(server)
            with ThreadPoolExecutor(max_workers=1) as executor:
                draining = executor.submit(server.request, "GET", "/draining")
                self.wait(lambda: (server.root / "draining").exists())
                server.process.terminate()
                self.assertEqual(draining.result(timeout=3)[0], 200)
                server.process.wait(timeout=3)
            self.assertTrue((server.root / f"closed_{pid}").exists())
            server.log.seek(0)
            self.assertNotIn("Supervisor heartbeat channel failed", server.log.read())
        finally:
            server.close()

    def test_native_startup_budget_is_independent_of_shutdown_grace(self):
        from test_native_integration import Server

        application = SUPERVISED_APPLICATION.replace(
            "async def lifespan(app):\n",
            "async def lifespan(app):\n    await asyncio.sleep(0.25)\n",
            1,
        )
        self.assertNotEqual(application, SUPERVISED_APPLICATION)
        server = Server(
            application=application,
            extra_args=("--startup-timeout", "2", "--graceful-timeout", "0.1"),
        )
        try:
            self.assertIsNotNone(self.pid(server))
            self.assertIsNone(server.process.poll())
        finally:
            server.close()

    @unittest.skipUnless(sys.platform == "linux", "Linux parent-death signaling")
    def test_supervisor_sigkill_kills_blocked_worker_without_orphans(self):
        result = run_subreaper(PARENT_DEATH_PROBE)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        worker = next(
            line.removeprefix("worker_pid=")
            for line in result.stdout.splitlines()
            if line.startswith("worker_pid=")
        )
        self.assertIn(f"adopted_pid={worker} signal={signal.SIGKILL}", result.stdout)

    def test_native_max_requests_recycles_without_consuming_restart_budget(self):
        server = self.server("--max-requests", "3", "--restart-limit", "0")
        try:
            original = self.wait(lambda: self.pid(server))
            replacement = self.wait(
                lambda: (pid := self.pid(server)) and pid != original and pid
            )
            self.assertNotEqual(replacement, original)
            self.assertIsNone(server.process.poll())
        finally:
            server.close()

    def test_max_requests_kills_hung_sync_cleanup_with_heartbeat_disabled(self):
        server = self.server(
            "--max-requests",
            "2",
            "--restart-limit",
            "0",
            "--worker-timeout",
            "0",
            "--request-timeout",
            "0.05",
            "--graceful-timeout",
            "0.15",
        )
        try:
            marker = self.wait(lambda: next(iter(server.root.glob("started_*")), None))
            original = int(marker.name.removeprefix("started_"))
            started = time.monotonic()
            self.assertEqual(server.request("GET", "/blocking")[0], 504)

            def gone():
                try:
                    os.kill(original, 0)
                except ProcessLookupError:
                    return True
                return False

            self.wait(gone, timeout=1)
            self.assertLess(time.monotonic() - started, 0.8)
            replacement = self.wait(
                lambda: (pid := self.pid(server)) and pid != original and pid
            )
            self.assertNotEqual(replacement, original)
            self.assertIsNone(server.process.poll())
        finally:
            server.close()

    def test_native_sighup_reloads_worker_without_rebinding_listener(self):
        server = self.server("--graceful-timeout", "2")
        try:
            original = self.pid(server)
            with ThreadPoolExecutor(max_workers=1) as executor:
                draining = executor.submit(server.request, "GET", "/draining")
                self.wait(lambda: (server.root / "draining").exists())
                server.process.send_signal(signal.SIGHUP)
                replacement = self.wait(
                    lambda: (pid := self.pid(server)) and pid != original and pid
                )
                self.assertEqual(draining.result(timeout=3)[0], 200)
            self.wait(lambda: (server.root / f"closed_{original}").exists())
            self.assertNotEqual(replacement, original)
            self.assertIsNone(server.process.poll())
        finally:
            server.close()

    def test_native_event_loop_stall_gets_replaced(self):
        server = self.server("--worker-timeout", "1")
        try:
            original = self.pid(server)
            with ThreadPoolExecutor(max_workers=1) as executor:
                blocked = executor.submit(server.request, "GET", "/stall")
                self.wait(lambda: (server.root / "blocked").exists())
                replacement = self.wait(
                    lambda: (pid := self.pid(server)) and pid != original and pid
                )
                self.assertNotEqual(replacement, original)
                with self.assertRaises((OSError, http.client.HTTPException)):
                    blocked.result(timeout=3)
            self.assertIsNone(server.process.poll())
        finally:
            server.close()

    def test_native_noncooperative_sync_handler_has_hard_shutdown_deadline(self):
        server = self.server("--worker-timeout", "0")
        try:
            with ThreadPoolExecutor(max_workers=1) as executor:
                blocked = executor.submit(server.request, "GET", "/blocking")
                self.wait(lambda: (server.root / "blocked").exists())
                started = time.monotonic()
                server.process.terminate()
                server.process.wait(timeout=2)
                self.assertLess(time.monotonic() - started, 1.5)
                with self.assertRaises((OSError, http.client.HTTPException)):
                    blocked.result(timeout=2)
        finally:
            server.close()


if __name__ == "__main__":
    unittest.main()
