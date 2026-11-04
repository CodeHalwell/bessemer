"""Bounded production-image soak and full-stream secret-safe log capture."""

from __future__ import annotations

import hashlib
import json
import math
import os
import subprocess
import threading
import time
from pathlib import Path

from qualify_container import DOCKER, atomic_json, utc_now

MAX_LOG_BYTES = 32 * 1024 * 1024
ERROR_PATTERNS = {
    "worker_failure": b"worker failed pid=",
    "restart_budget": b"worker restart limit exceeded",
    "heartbeat_failure": b"heartbeat channel",
    "broken_pipe": b"BrokenPipeError",
    "epipe": b"EPIPE",
    "drain_deadline": b"drain deadline exceeded",
    "retirement_kill": b"worker exceeded drain deadline",
    "shutdown_kill": b"worker killed at shutdown deadline",
    "reap_failure": b"worker could not be reaped",
    "application_exception": b"Unhandled ",
    "response_encoding": b"could not encode the response",
    "traceback": b"Traceback (most recent call last)",
    "worker_shutdown": b"application worker did not stop",
    "lifespan_failure": b"lifespan task exited with an error",
}
RESOURCE_INSPECT = """{"running":{{json .State.Running}},"oom_killed":{{json .State.OOMKilled}},"restart_count":{{json .RestartCount}},"health":{{json .State.Health.Status}}}"""
RESOURCE_PROBE = r"""
import json
from pathlib import Path
def text(path):
    try: return Path(path).read_text().strip()
    except OSError: return None
def number(path):
    value = text(path)
    return int(value) if value is not None and value.isdecimal() else None
workers = []
for path in Path('/proc').iterdir():
    if not path.name.isdecimal(): continue
    value = text(path / 'stat')
    if value is None: continue
    fields = value.rsplit(')', 1)[1].split()
    if int(fields[1]) == 1 and fields[0] != 'Z':
        try: count = len(list((path / 'fd').iterdir()))
        except OSError: count = None
        workers.append({'pid': int(path.name), 'start_ticks': int(fields[19]), 'fds': count})
events = {}
for line in (text('/sys/fs/cgroup/memory.events') or '').splitlines():
    name, value = line.split()
    events[name] = int(value)
print(json.dumps({
    'memory_current': number('/sys/fs/cgroup/memory.current'),
    'memory_peak': number('/sys/fs/cgroup/memory.peak'),
    'memory_max': text('/sys/fs/cgroup/memory.max'),
    'memory_events': events,
    'pids_current': number('/sys/fs/cgroup/pids.current'),
    'pids_max': text('/sys/fs/cgroup/pids.max'),
    'cpu_max': text('/sys/fs/cgroup/cpu.max'),
    'parent_fds': len(list(Path('/proc/1/fd').iterdir())),
    'workers': sorted(workers, key=lambda item: item['pid']),
}))
"""


class RedactedLogSink:
    """Inspect every byte while bounding buffers and the redacted evidence file."""

    def __init__(self, path, secret, *, max_bytes=MAX_LOG_BYTES):
        if not secret:
            raise ValueError("log capture requires a nonempty generated secret")
        self.path = Path(path)
        self.secret = secret.encode()
        self.limit = max_bytes
        self.stream = self.path.open("wb")
        self.lock = threading.Lock()
        self.pending = b""
        self.guard_tail = b""
        self.raw_bytes = 0
        self.written_bytes = 0
        self.secret_seen = False
        self.error_flags: set[str] = set()
        self.truncated = False
        self.closed = False
        self.sha256 = hashlib.sha256()
        self.guard_size = (
            max(len(self.secret), *(len(value) for value in ERROR_PATTERNS.values()))
            - 1
        )

    def _emit(self, value):
        safe = value.replace(self.secret, b"[redacted]")
        available = max(0, self.limit - self.written_bytes)
        if len(safe) > available:
            self.truncated = True
        safe = safe[:available]
        self.stream.write(safe)
        self.sha256.update(safe)
        self.written_bytes += len(safe)
        self.stream.flush()

    def consume(self, value):
        with self.lock:
            self.raw_bytes += len(value)
            observed = self.guard_tail + value
            self.secret_seen |= self.secret in observed
            self.error_flags.update(
                name for name, pattern in ERROR_PATTERNS.items() if pattern in observed
            )
            self.guard_tail = observed[-self.guard_size :]
            value = self.pending + value
            cut = max(0, len(value) - len(self.secret) + 1)
            # Hold a complete secret if its replacement would cross the cut.
            start = value.find(self.secret)
            while 0 <= start < cut:
                if start + len(self.secret) > cut:
                    cut = start
                    break
                start = value.find(self.secret, start + 1)
            self._emit(value[:cut])
            self.pending = value[cut:]

    def close(self):
        with self.lock:
            if not self.closed:
                self._emit(self.pending)
                self.pending = b""
                self.stream.close()
                self.closed = True

    def summary(self):
        with self.lock:
            return {
                "path": self.path.name,
                "raw_bytes_checked": self.raw_bytes,
                "written_bytes": self.written_bytes,
                "maximum_file_bytes": self.limit,
                "truncated": self.truncated,
                "secret_absent": not self.secret_seen,
                "worker_error_flags": sorted(self.error_flags),
                "sha256": self.sha256.hexdigest(),
                "closed": self.closed,
            }


class LiveLogCapture:
    def __init__(self, container, path, secret):
        self.sink = RedactedLogSink(path, secret)
        self.error = None
        self.finished = False
        self.forced_stop = False
        self.process = None

        def read():
            try:
                while value := os.read(self.process.stdout.fileno(), 4096):
                    self.sink.consume(value)
            except Exception as error:  # noqa: BLE001 -- Fail qualification if stream observation is incomplete.
                self.error = type(error).__name__
            finally:
                self.process.stdout.close()
                self.sink.close()

        try:
            self.process = subprocess.Popen(
                [*DOCKER, "logs", "--follow", "--tail", "all", container.cid],
                env=container.docker.environment,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
            )
            self.thread = threading.Thread(
                target=read, name="bessemer-soak-logs", daemon=True
            )
            self.thread.start()
        except BaseException:
            if self.process is not None:
                if self.process.poll() is None:
                    self.process.terminate()
                    try:
                        self.process.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        self.process.kill()
                        self.process.wait(timeout=2)
                if self.process.stdout is not None:
                    self.process.stdout.close()
            self.sink.close()
            raise

    def finish(self, timeout=5):
        if not self.finished:
            try:
                self.process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                self.forced_stop = True
                self.process.terminate()
                try:
                    self.process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait(timeout=2)
            self.thread.join(timeout=2)
            self.finished = True
        return self.summary()

    def summary(self):
        result = self.sink.summary()
        result.update(
            {
                "complete": self.finished
                and not self.forced_stop
                and not self.thread.is_alive()
                and self.error is None
                and self.process.returncode == 0,
                "stream_error": self.error,
                "collector_exit_code": self.process.poll(),
            }
        )
        return result


def run_production_soak(container, contracts, output, duration, capture):
    """Repeat the exact quick contracts at at most one request per second."""
    started = time.monotonic()
    deadline = started + duration
    progress = output / "production-soak-progress.json"
    samples_path = output / "production-soak-resources.jsonl"
    result = {
        "target_duration_seconds": duration,
        "target_requests_per_second": 1,
        "target_requests": max(1, math.ceil(duration)),
        "minimum_delivery_fraction": 0.97,
        "started_at": utc_now(),
        "passed": False,
        "requests": 0,
        "errors": 0,
        "error_examples": [],
        "contracts": {},
        "resource_samples": 0,
        "resource_samples_file": samples_path.name,
        "memory_current_min": None,
        "memory_current_max": 0,
        "memory_peak_max": 0,
        "pids_current_max": 0,
        "latency_seconds_sum": 0.0,
        "latency_seconds_max": 0.0,
        "latency_buckets": {
            str(value): 0 for value in (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0)
        },
    }
    initial_workers = None
    initial_restarts = None
    initial_oom = None

    def error(kind, name=None, status=None):
        result["errors"] += 1
        if len(result["error_examples"]) < 10:
            result["error_examples"].append(
                {"at": utc_now(), "kind": kind, "contract": name, "status": status}
            )

    def sample(stream):
        nonlocal initial_workers, initial_restarts, initial_oom
        inspected = container.docker.json(
            "inspect", "--format", RESOURCE_INSPECT, container.cid
        )
        assert inspected["running"], "container exited during soak"
        assert inspected["health"] in {"healthy", "starting"}, (
            "Docker health is unhealthy"
        )
        assert inspected["health"] == "healthy" or time.monotonic() - started <= 10, (
            "Docker health did not become healthy within10 seconds"
        )
        observed = container.docker.json(
            "exec", container.cid, "python", "-c", RESOURCE_PROBE
        )
        record = {
            "at": utc_now(),
            "elapsed_seconds": time.monotonic() - started,
            "inspect": inspected,
            "observed": observed,
        }
        stream.write(json.dumps(record, sort_keys=True) + "\n")
        stream.flush()
        result["resource_samples"] += 1
        result.setdefault("first_resource_sample", record)
        result["last_resource_sample"] = record
        memory = observed["memory_current"]
        assert memory is not None, "actual cgroup memory.current unavailable"
        quota, period = observed["cpu_max"].split()
        assert quota != "max" and 0 < int(quota) / int(period) <= 1
        assert int(observed["memory_max"]) == 512 * 1024 * 1024
        assert int(observed["pids_max"]) == 128
        result["memory_current_min"] = min(
            result["memory_current_min"] or memory, memory
        )
        result["memory_current_max"] = max(result["memory_current_max"], memory)
        result["memory_peak_max"] = max(
            result["memory_peak_max"], observed["memory_peak"] or 0
        )
        result["pids_current_max"] = max(
            result["pids_current_max"], observed["pids_current"] or 0
        )
        workers = [(item["pid"], item["start_ticks"]) for item in observed["workers"]]
        oom = observed["memory_events"].get("oom_kill")
        assert workers and oom is not None, "worker/cgroup identity unavailable"
        if initial_workers is None:
            initial_workers, initial_restarts, initial_oom = (
                workers,
                inspected["restart_count"],
                oom,
            )
        assert workers == initial_workers, "worker identity changed during soak"
        assert inspected["restart_count"] == initial_restarts, (
            "container restarted during soak"
        )
        assert not inspected["oom_killed"] and oom == initial_oom == 0, (
            "OOM event observed"
        )

    next_sample = started
    with samples_path.open("w") as stream:
        while time.monotonic() < deadline:
            if time.monotonic() >= next_sample:
                try:
                    sample(stream)
                except Exception as caught:  # noqa: BLE001 -- Bound retained diagnostics to safe types and counters.
                    error(type(caught).__name__, "resource_sample")
                    break
                next_sample = time.monotonic() + (
                    60
                    if result["last_resource_sample"]["inspect"]["health"] == "healthy"
                    else 1
                )
                result["elapsed_seconds"] = time.monotonic() - started
                result["log_capture"] = capture.summary()
                atomic_json(progress, result)
            name, method, path, expected, predicate, arguments = contracts[
                result["requests"] % len(contracts)
            ]
            counts = result["contracts"].setdefault(name, {"requests": 0, "errors": 0})
            request_started = time.monotonic()
            status = None
            try:
                status, body = container.request(method, path, **arguments)
                okay = status == expected and (predicate is None or predicate(body))
                if not okay:
                    error("contract_mismatch", name, status)
                    counts["errors"] += 1
            except Exception as caught:  # noqa: BLE001 -- No response body/headers/secrets are retained in diagnostics.
                error(type(caught).__name__, name, status)
                counts["errors"] += 1
            elapsed = time.monotonic() - request_started
            result["latency_seconds_sum"] += elapsed
            result["latency_seconds_max"] = max(result["latency_seconds_max"], elapsed)
            for bound in result["latency_buckets"]:
                if elapsed <= float(bound):
                    result["latency_buckets"][bound] += 1
            result["requests"] += 1
            counts["requests"] += 1
            guard = capture.summary()
            if (
                not guard["secret_absent"]
                or guard["worker_error_flags"]
                or guard["truncated"]
                or guard["stream_error"]
                or guard["collector_exit_code"] is not None
            ):
                error("full_stream_log_guard")
                break
            wait = min(deadline, request_started + 1) - time.monotonic()
            if wait > 0:
                time.sleep(wait)
        if result["errors"] == 0:
            try:
                sample(stream)
            except Exception as caught:  # noqa: BLE001 -- Final observations are part of qualification.
                error(type(caught).__name__, "final_resource_sample")
    result["elapsed_seconds"] = time.monotonic() - started
    result["delivery_fraction"] = result["requests"] / result["target_requests"]
    if result["delivery_fraction"] < result["minimum_delivery_fraction"]:
        error("insufficient_delivered_work")
    result["ended_at"] = utc_now()
    result["latency_seconds_mean"] = result["latency_seconds_sum"] / max(
        1, result["requests"]
    )
    result["log_capture"] = capture.summary()
    result["passed"] = result["errors"] == 0 and result["elapsed_seconds"] >= duration
    atomic_json(progress, result)
    return result
