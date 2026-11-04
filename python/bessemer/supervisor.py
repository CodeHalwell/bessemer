"""Re-exec workers with a parent-owned listener and bounded shutdown.

The parent never imports the user application. Every worker starts a fresh
interpreter, and its process group receives a hard shutdown deadline even if
Python handler code cannot cooperate. Native workers signal readiness through
an inherited pipe after application startup and listener initialization.
"""

from __future__ import annotations

import json
import logging
import math
import os
import selectors
import signal
import socket
import subprocess
import sys
import time
from collections import deque
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from .config import SERVER_DEFAULTS

_LOG = logging.getLogger("bessemer.supervisor")
# A worker's control pipe reaches EOF while the kernel is still tearing the
# process down, before its exit status can be observed. Wait this long for an
# exit before counting the EOF as a control-channel failure.
_CONTROL_EXIT_GRACE = 0.1


def cpu_quota_count(cgroup_root: Path = Path("/sys/fs/cgroup")) -> int:
    """Respect affinity and cgroup quota rather than host processor count."""
    try:
        count = len(os.sched_getaffinity(0))
    except (AttributeError, OSError):
        count = os.cpu_count() or 1
    cgroup_root = cgroup_root.resolve()
    limits = [float(max(1, count))]
    paths = [cgroup_root]
    v1_paths = [cgroup_root, cgroup_root / "cpu", cgroup_root / "cpu,cpuacct"]
    try:
        for entry in Path("/proc/self/cgroup").read_text().splitlines():
            hierarchy, controllers, group = entry.split(":", 2)
            if hierarchy == "0" and not controllers:
                candidate = (cgroup_root / group.lstrip("/")).resolve()
                if candidate.is_relative_to(cgroup_root):
                    paths.extend([candidate, *candidate.parents])
            elif "cpu" in controllers.split(","):
                for base in v1_paths.copy():
                    candidate = (base / group.lstrip("/")).resolve()
                    if candidate.is_relative_to(base):
                        v1_paths.extend([candidate, *candidate.parents])
    except (OSError, ValueError):
        pass
    for directory in dict.fromkeys(
        path for path in paths if path.is_relative_to(cgroup_root)
    ):
        try:
            quota, period = (directory / "cpu.max").read_text().split()
            if quota != "max" and int(quota) > 0 and int(period) > 0:
                limits.append(int(quota) / int(period))
        except (OSError, ValueError):
            pass
    for directory in dict.fromkeys(
        path for path in v1_paths if path.is_relative_to(cgroup_root)
    ):
        try:
            quota = int((directory / "cpu.cfs_quota_us").read_text())
            period = int((directory / "cpu.cfs_period_us").read_text())
            if quota > 0 and period > 0:
                limits.append(quota / period)
        except (OSError, ValueError):
            pass
    return max(1, min(256, math.ceil(min(limits))))


def _listen(host: str, port: int) -> socket.socket:
    last_error = None
    for family, socktype, protocol, _, address in socket.getaddrinfo(
        host, port, type=socket.SOCK_STREAM, flags=socket.AI_PASSIVE
    ):
        listener = socket.socket(family, socktype, protocol)
        try:
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.bind(address)
            listener.listen(2048)
            listener.setblocking(False)
            return listener
        except OSError as error:
            last_error = error
            listener.close()
    if last_error is not None:
        raise last_error
    raise OSError("host did not resolve to a TCP address")


@dataclass
class _Child:
    process: subprocess.Popen
    ready_fd: int
    started: float
    ready: bool = False
    ready_at: float = 0.0
    # A replacement is promoted only after a control message read after the
    # one that reported readiness, so an EOF racing readiness is never missed.
    confirmed: bool = False
    deadline: float | None = None
    last_heartbeat: float = 0.0
    control_failed: bool = False
    control_failed_at: float = 0.0
    killed: bool = False
    failure_recorded: bool = False
    draining: bool = False
    self_draining: bool = False


@dataclass
class _Slot:
    active: _Child | None = None
    pending: _Child | None = None
    retiring: list[_Child] = field(default_factory=list)
    failures: int = 0
    next_start: float = 0.0


class Supervisor:
    """A fixed number of independently started and monitored workers."""

    def __init__(
        self,
        target: str,
        settings: dict,
        *,
        command: Sequence[str] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ):
        if os.name != "posix":
            raise RuntimeError(
                "the process supervisor currently requires a POSIX platform"
            )
        if settings["max_rss"] and sys.platform != "linux":
            raise RuntimeError("--max-rss currently requires Linux /proc support")
        self.target = target
        self.settings = settings.copy()
        self.workers = self.settings["workers"] or cpu_quota_count()
        self.command = (
            tuple(command)
            if command is not None
            else (sys.executable, "-m", "bessemer", "_worker")
        )
        self.clock = clock
        self._slots = [_Slot() for _ in range(self.workers)]
        self._selector = selectors.DefaultSelector()
        self._listener: socket.socket | None = None
        self._stopping = False
        self._shutdown_deadline: float | None = None
        self._exit_code = 0
        self._reload_requested = False
        self._reload_queue: deque[int] = deque()
        self._rolling_slot: int | None = None
        self._previous_signals: dict[int, object] = {}

    @property
    def bound_address(self):
        return self._listener.getsockname() if self._listener is not None else None

    def _install_signals(self) -> None:
        def stop(signum, _frame):
            if self._stopping:
                self._shutdown_deadline = self.clock()
            else:
                self.request_stop(130 if signum == signal.SIGINT else 0)

        def reload_workers(_signum, _frame):
            self._reload_requested = True

        for signum, handler in (
            (signal.SIGTERM, stop),
            (signal.SIGINT, stop),
            (signal.SIGHUP, reload_workers),
        ):
            self._previous_signals[signum] = signal.signal(signum, handler)

    def _restore_signals(self) -> None:
        for signum, handler in self._previous_signals.items():
            signal.signal(signum, handler)
        self._previous_signals.clear()

    def request_stop(self, exit_code: int = 0) -> None:
        if not self._stopping:
            self._stopping = True
            self._exit_code = exit_code
            self._shutdown_deadline = self.clock() + self.settings["graceful_timeout"]

    def request_reload(self) -> None:
        self._reload_requested = True

    def _close_ready(self, child: _Child) -> None:
        if child.ready_fd >= 0:
            try:
                self._selector.unregister(child.ready_fd)
            except KeyError:
                pass
            os.close(child.ready_fd)
            child.ready_fd = -1

    @staticmethod
    def _signal(child: _Child, signum: int) -> None:
        if child.process.returncode is None:
            try:
                os.killpg(child.process.pid, signum)
            except ProcessLookupError:
                pass

    @classmethod
    def _poll(cls, child: _Child) -> int | None:
        if child.process.returncode is not None:
            return child.process.returncode
        if sys.platform == "linux":
            # Observe exit without reaping first: the zombie leader reserves its
            # PID/group ID, so group cleanup cannot target a reused identifier.
            # A crashed leader cannot clean up application subprocesses itself.
            try:
                exited = os.waitid(
                    os.P_PID,
                    child.process.pid,
                    os.WEXITED | os.WNOWAIT | os.WNOHANG,
                )
            except ChildProcessError:
                # Ownership was lost to an external waiter. Popen can record
                # ECHILD, but the group identifier is no longer safe to signal.
                return child.process.poll()
            if exited is None:
                # Calling poll here would introduce a race: the leader could
                # exit between waitid and poll and be reaped before group cleanup.
                return None
            cls._signal(child, signal.SIGKILL)
        return child.process.poll()

    def _spawn(self) -> _Child:
        assert self._listener is not None
        read_fd, write_fd = os.pipe()
        os.set_blocking(read_fd, False)
        server = {name: self.settings[name] for name in SERVER_DEFAULTS}
        server["port"] = self._listener.getsockname()[1]
        server["startup_timeout"] = self.settings["startup_timeout"]
        try:
            environment = os.environ.copy()
            environment["BESSEMER_SUPERVISED_WORKER"] = "1"
            process = subprocess.Popen(
                [
                    *self.command,
                    self.target,
                    "--listener-fd",
                    str(self._listener.fileno()),
                    "--ready-fd",
                    str(write_fd),
                    "--worker-count",
                    str(self.workers),
                    "--parent-pid",
                    str(os.getpid()),
                    "--settings",
                    json.dumps(server, separators=(",", ":")),
                ],
                pass_fds=(self._listener.fileno(), write_fd),
                start_new_session=True,
                env=environment,
            )
        except BaseException:
            os.close(read_fd)
            raise
        finally:
            os.close(write_fd)
        child = _Child(process, read_fd, self.clock())
        try:
            self._selector.register(read_fd, selectors.EVENT_READ, child)
        except BaseException:
            os.close(read_fd)
            self._signal(child, signal.SIGKILL)
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                _LOG.error("untracked worker could not be reaped pid=%s", process.pid)
            raise
        _LOG.info("worker started pid=%s", process.pid)
        return child

    def _retire(self, slot: _Slot, child: _Child, *, reason: str) -> None:
        child.deadline = (
            child.deadline or self.clock() + self.settings["graceful_timeout"]
        )
        child.draining = True
        self._signal(child, signal.SIGTERM)
        slot.retiring.append(child)
        _LOG.info("worker draining pid=%s reason=%s", child.process.pid, reason)

    def _failed(
        self,
        slot: _Slot,
        child: _Child,
        *,
        reason: str,
        replacement: bool = False,
        permanent: bool = False,
    ) -> None:
        self._close_ready(child)
        if child.ready and child.last_heartbeat - child.ready_at >= 30:
            slot.failures = 0
        slot.failures += 1
        if permanent:
            slot.failures = self.settings["restart_limit"] + 1
        _LOG.error(
            "worker failed pid=%s reason=%s failures=%s",
            child.process.pid,
            reason,
            slot.failures,
        )
        if slot.failures > self.settings["restart_limit"]:
            if replacement and slot.active is not None and self._healthy(slot.active):
                _LOG.error(
                    "replacement restart limit exceeded; keeping the ready worker"
                )
                slot.failures = 0
                slot.next_start = self.clock() + 30
                self._reload_queue.clear()
                self._rolling_slot = None
                return
            _LOG.error("worker restart limit exceeded")
            self.request_stop(1)
        slot.next_start = self.clock() + min(
            max(30.0, self.settings["restart_backoff"]),
            self.settings["restart_backoff"] * (2 ** min(slot.failures - 1, 10)),
        )

    def _healthy(self, child: _Child) -> bool:
        return (
            child.ready
            and not child.control_failed
            and not child.draining
            and self._poll(child) is None
            and (
                self.settings["worker_timeout"] == 0
                or self.clock() - child.last_heartbeat < self.settings["worker_timeout"]
            )
        )

    @staticmethod
    def _rss_mib(pid: int) -> float | None:
        try:
            for line in Path(f"/proc/{pid}/status").read_text().splitlines():
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) / 1024
        except (OSError, ValueError):
            pass
        return None

    def _read_control(self, child: _Child) -> None:
        # Drain both readiness and a following EOF before deciding whether a
        # replacement is healthy. Bound work if a broken child floods the pipe.
        # Readiness and a closing write end can reach the pipe on either side of
        # one wake-up, so only data read in a later call confirms the channel.
        was_ready = child.ready
        for _ in range(16):
            if child.ready_fd < 0:
                return
            try:
                data = os.read(child.ready_fd, 1024)
            except BlockingIOError:
                return
            if not data or any(value not in b"1hd" for value in data):
                self._close_ready(child)
                if child.draining:
                    return
                child.control_failed = True
                child.control_failed_at = self.clock()
                child.deadline = (
                    child.deadline or self.clock() + self.settings["graceful_timeout"]
                )
                self._signal(child, signal.SIGTERM)
                return
            if was_ready:
                child.confirmed = True
            if b"1" in data and not child.ready:
                child.ready = True
                child.ready_at = child.last_heartbeat = self.clock()
                _LOG.info("worker ready pid=%s", child.process.pid)
            if child.ready and b"h" in data:
                child.last_heartbeat = self.clock()
            if child.ready and b"d" in data and not child.draining:
                child.draining = True
                child.self_draining = True
                child.deadline = (
                    child.deadline or self.clock() + self.settings["graceful_timeout"]
                )
                _LOG.info("worker requested drain pid=%s", child.process.pid)

    def _ready_events(self, timeout: float) -> None:
        for key, _ in self._selector.select(timeout):
            self._read_control(key.data)

    def _children(self):
        for slot in self._slots:
            for child in (slot.active, slot.pending, *slot.retiring):
                if child is not None:
                    yield child

    def _poll_slots(self) -> None:
        now = self.clock()
        for index, slot in enumerate(self._slots):
            for name in ("active", "pending"):
                child = getattr(slot, name)
                if child is None:
                    continue
                returncode = self._poll(child)
                if (
                    name == "active"
                    and returncode is None
                    and child.ready
                    and not child.draining
                    and not child.failure_recorded
                    and (
                        (
                            child.control_failed
                            and now - child.control_failed_at >= _CONTROL_EXIT_GRACE
                        )
                        or (
                            self.settings["worker_timeout"] > 0
                            and now - child.last_heartbeat
                            >= self.settings["worker_timeout"]
                        )
                    )
                ):
                    reason = (
                        "control channel failed"
                        if child.control_failed
                        else "heartbeat stalled"
                    )
                    child.failure_recorded = True
                    child.control_failed = True
                    child.deadline = (
                        child.deadline or now + self.settings["graceful_timeout"]
                    )
                    self._failed(slot, child, reason=reason)
                    self._signal(child, signal.SIGTERM)
                if returncode is not None:
                    # Very short max-request cycles may exit before the parent
                    # has consumed their readiness byte.
                    self._read_control(child)
                    self._close_ready(child)
                    setattr(slot, name, None)
                    if child.failure_recorded:
                        pass
                    elif (returncode == 0 and child.ready) or (
                        child.self_draining and child.killed
                    ):
                        if child.last_heartbeat - child.ready_at >= 30:
                            slot.failures = 0
                        slot.next_start = now
                        _LOG.info(
                            "worker recycle completed pid=%s exit=%s",
                            child.process.pid,
                            returncode,
                        )
                    else:
                        self._failed(
                            slot,
                            child,
                            reason=f"exit {returncode}",
                            replacement=name == "pending",
                            permanent=returncode == 78,
                        )
                elif (
                    name == "pending"
                    and child.ready
                    and not child.draining
                    and self.settings["worker_timeout"] > 0
                    and now - child.last_heartbeat >= self.settings["worker_timeout"]
                    and not child.control_failed
                ):
                    child.control_failed = True
                    child.deadline = now + self.settings["graceful_timeout"]
                    self._close_ready(child)
                    self._signal(child, signal.SIGTERM)
                elif (
                    (child.control_failed or child.self_draining)
                    and child.deadline is not None
                    and now >= child.deadline
                    and not child.killed
                ):
                    _LOG.warning(
                        "worker control/drain deadline exceeded pid=%s",
                        child.process.pid,
                    )
                    self._signal(child, signal.SIGKILL)
                    child.killed = True
                elif (
                    not child.ready or (name == "pending" and not child.confirmed)
                ) and now - child.started >= self.settings["startup_timeout"]:
                    self._signal(child, signal.SIGKILL)
                    child.process.wait(timeout=1)
                    setattr(slot, name, None)
                    self._failed(
                        slot,
                        child,
                        reason="startup deadline exceeded",
                        replacement=name == "pending",
                    )
            if slot.pending is not None:
                self._read_control(slot.pending)
            if (
                slot.pending is not None
                and slot.pending.confirmed
                and self._healthy(slot.pending)
            ):
                old = slot.active
                slot.active, slot.pending = slot.pending, None
                if old is not None:
                    self._retire(slot, old, reason="replacement ready")
                if self._rolling_slot == index:
                    self._rolling_slot = None
            for child in list(slot.retiring):
                if self._poll(child) is not None:
                    self._close_ready(child)
                    slot.retiring.remove(child)
                elif (
                    child.deadline is not None
                    and now >= child.deadline
                    and not child.killed
                ):
                    _LOG.warning(
                        "worker exceeded drain deadline pid=%s", child.process.pid
                    )
                    self._signal(child, signal.SIGKILL)
                    child.killed = True
            if self._stopping:
                continue
            if slot.active is None and now >= slot.next_start:
                if slot.pending is not None:
                    slot.active, slot.pending = slot.pending, None
                    if self._rolling_slot == index:
                        self._rolling_slot = None
                else:
                    slot.active = self._spawn()
            child = slot.active
            if (
                child is not None
                and child.ready
                and slot.pending is None
                and now >= slot.next_start
            ):
                lifetime_due = (
                    self.settings["max_lifetime"] > 0
                    and now - child.started >= self.settings["max_lifetime"]
                )
                rss = (
                    self._rss_mib(child.process.pid)
                    if self.settings["max_rss"]
                    else None
                )
                memory_due = rss is not None and rss > self.settings["max_rss"]
                heartbeat_due = (
                    not child.draining
                    and self.settings["worker_timeout"] > 0
                    and now - child.last_heartbeat >= self.settings["worker_timeout"]
                )
                control_lost = (
                    child.control_failed
                    and now - child.control_failed_at >= _CONTROL_EXIT_GRACE
                )
                if (heartbeat_due or control_lost) and slot.retiring:
                    # An unhealthy active worker takes priority over an older
                    # worker's remaining drain time, without growing the pool.
                    for retiring in slot.retiring:
                        self._signal(retiring, signal.SIGKILL)
                        retiring.killed = True
                if (
                    lifetime_due
                    or memory_due
                    or heartbeat_due
                    or control_lost
                    or child.draining
                ) and not slot.retiring:
                    reason = (
                        "worker requested drain"
                        if child.draining
                        else "control channel failed"
                        if control_lost
                        else "heartbeat stalled"
                        if heartbeat_due
                        else "memory threshold"
                        if memory_due
                        else "lifetime threshold"
                    )
                    _LOG.info(
                        "worker replacement requested pid=%s reason=%s",
                        child.process.pid,
                        reason,
                    )
                    slot.pending = self._spawn()
            if (
                self._rolling_slot == index
                and slot.pending is None
                and slot.active is not None
                and slot.active.ready
                and not slot.active.draining
                and not slot.retiring
                and now >= slot.next_start
            ):
                slot.pending = self._spawn()

    def _shutdown(self) -> None:
        for child in self._children():
            child.draining = True
            self._signal(child, signal.SIGTERM)
        deadline = (
            self._shutdown_deadline or self.clock() + self.settings["graceful_timeout"]
        )
        children = list(self._children())
        while (
            any(self._poll(child) is None for child in children)
            and self.clock() < deadline
        ):
            deadline = min(deadline, self._shutdown_deadline or deadline)
            for child in children:
                if (
                    child.process.returncode is None
                    and child.deadline is not None
                    and self.clock() >= child.deadline
                    and not child.killed
                ):
                    self._signal(child, signal.SIGKILL)
                    child.killed = True
            self._ready_events(min(0.02, max(0, deadline - self.clock())))
        for child in children:
            if self._poll(child) is None:
                _LOG.warning(
                    "worker killed at shutdown deadline pid=%s", child.process.pid
                )
                self._signal(child, signal.SIGKILL)
        reap_deadline = self.clock() + 1
        for child in children:
            try:
                child.process.wait(timeout=max(0, reap_deadline - self.clock()))
            except subprocess.TimeoutExpired:
                _LOG.error("worker could not be reaped pid=%s", child.process.pid)
            finally:
                self._close_ready(child)

    def run(self, *, install_signal_handlers: bool = True) -> int:
        try:
            self._listener = _listen(self.settings["host"], self.settings["port"])
            _LOG.info(
                "listening address=%s workers=%s",
                self._listener.getsockname(),
                self.workers,
            )
            if install_signal_handlers:
                self._install_signals()
            while not self._stopping:
                if self._reload_requested:
                    self._reload_requested = False
                    if not self._reload_queue and self._rolling_slot is None:
                        self._reload_queue.extend(range(self.workers))
                if self._rolling_slot is None and self._reload_queue:
                    self._rolling_slot = self._reload_queue.popleft()
                self._poll_slots()
                if not self._stopping:
                    self._ready_events(0.05)
        except BaseException:
            self.request_stop(1)
            raise
        finally:
            self._shutdown()
            self._restore_signals()
            self._selector.close()
            if self._listener is not None:
                self._listener.close()
        return self._exit_code


def run(target: str, settings: dict) -> int:
    return Supervisor(target, settings).run()
