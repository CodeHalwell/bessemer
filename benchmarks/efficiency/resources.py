"""Sample every server descendant; keep exited children's observed CPU time."""

import os
import resource
import statistics
import subprocess
import sys
import threading
import time
from pathlib import Path

CGROUP = Path("/sys/fs/cgroup")


def text(path):
    try:
        return path.read_text().strip()
    except (OSError, UnicodeError):
        return ""


def fields(path):
    result = {}
    for line in text(path).splitlines():
        parts = line.split()
        if len(parts) >= 2:
            try:
                result[parts[0].rstrip(":")] = int(parts[1])
            except ValueError:
                pass
    return result


def number(path):
    value = text(path)
    return int(value) if value.isdigit() else value or None


def cgroup():
    if sys.platform != "linux":
        return None
    if not isinstance(number(CGROUP / "memory.current"), int):
        return None
    return {
        "cpu_max": text(CGROUP / "cpu.max"),
        "cpu_stat": fields(CGROUP / "cpu.stat"),
        "cpuset_effective": text(CGROUP / "cpuset.cpus.effective"),
        "memory_current_bytes": number(CGROUP / "memory.current"),
        "memory_peak_lifetime_bytes": number(CGROUP / "memory.peak"),
        "memory_max_bytes": number(CGROUP / "memory.max"),
        "memory_stat": fields(CGROUP / "memory.stat"),
        "memory_events": fields(CGROUP / "memory.events"),
        "pids_current": number(CGROUP / "pids.current"),
        "pids_max": number(CGROUP / "pids.max"),
        "pid1_comm": text(Path("/proc/1/comm")),
    }


def linux_processes(pss):
    rows = {}
    ticks, page = os.sysconf("SC_CLK_TCK"), os.sysconf("SC_PAGE_SIZE")
    for path in Path("/proc").iterdir():
        if not path.name.isdigit():
            continue
        parts = text(path / "stat").rpartition(")")[2].split()
        if len(parts) < 22:
            continue
        try:
            pid = int(path.name)
            rows[pid] = {
                "pid": pid,
                "ppid": int(parts[1]),
                "start": parts[19],
                "cpu_seconds": (int(parts[11]) + int(parts[12])) / ticks,
                "rss_bytes": int(parts[21]) * page,
                "threads": int(parts[17]),
                "pss_bytes": fields(path / "smaps_rollup").get("Pss", 0) * 1024
                if pss
                else None,
            }
        except ValueError:
            continue
    return rows


def cpu_seconds(value):
    days, separator, clock = value.partition("-")
    result = 0.0
    for part in (clock if separator else value).split(":"):
        result = result * 60 + float(part)
    return result + (int(days) * 86400 if separator else 0)


def portable_processes():
    output = subprocess.check_output(
        ["ps", "-axo", "pid=,ppid=,lstart=,time=,rss="], text=True
    )
    rows = {}
    for line in output.splitlines():
        parts = line.split()
        try:
            pid = int(parts[0])
            rows[pid] = {
                "pid": pid,
                "ppid": int(parts[1]),
                "start": " ".join(parts[2:7]),
                "cpu_seconds": cpu_seconds(parts[7]),
                "rss_bytes": int(parts[8]) * 1024,
                "threads": None,
                "pss_bytes": None,
            }
        except (IndexError, ValueError):
            continue
    return rows


def descendants(rows, root):
    members = {root}
    while True:
        expanded = members | {
            pid for pid, row in rows.items() if row["ppid"] in members
        }
        if expanded == members:
            return members & rows.keys()
        members = expanded


def connections(port):
    if sys.platform != "linux":
        return None
    count = 0
    for name in ("tcp", "tcp6"):
        for line in text(Path("/proc/net") / name).splitlines()[1:]:
            parts = line.split()
            if (
                len(parts) > 3
                and parts[3] == "01"
                and int(parts[1].split(":")[1], 16) == port
            ):
                count += 1
    return count


class Resources:
    def __init__(self, root, port, *, interval=0.25, pss=True):
        self.root, self.port, self.interval, self.pss = root, port, interval, pss
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.known, self.cpu = set(), {}
        self.samples, self.started = [], None
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread.start()

    def capture(self):
        rows = (
            linux_processes(self.pss)
            if sys.platform == "linux"
            else portable_processes()
        )
        members = descendants(rows, self.root)
        self.known.update((pid, rows[pid]["start"]) for pid in members)
        active = [row for pid, row in rows.items() if (pid, row["start"]) in self.known]
        for row in active:
            key = row["pid"], row["start"]
            self.cpu[key] = max(self.cpu.get(key, 0), row["cpu_seconds"])
        own = resource.getrusage(resource.RUSAGE_SELF)
        return {
            "time": time.time(),
            "cpu_tree_observed_seconds": sum(self.cpu.values()),
            "rss_tree_bytes": sum(row["rss_bytes"] for row in active),
            "pss_tree_bytes": sum(row["pss_bytes"] for row in active)
            if active and all(row["pss_bytes"] is not None for row in active)
            else None,
            "processes": len(active),
            "threads": sum(row["threads"] for row in active)
            if active and all(row["threads"] is not None for row in active)
            else None,
            "established_connections": connections(self.port),
            "agent_cpu_seconds": own.ru_utime + own.ru_stime,
            "cgroup": cgroup(),
            "process_tree": active,
        }

    def snapshot(self):
        with self.lock:
            return self.capture()

    def run(self):
        while not self.stop.wait(self.interval):
            with self.lock:
                sample = self.capture()
                if self.started is not None:
                    self.samples.append(sample)

    def begin(self):
        with self.lock:
            self.baseline = self.capture()
            self.started = self.baseline["time"]
            self.samples = [self.baseline]
            return self.baseline

    def end(self, duration):
        with self.lock:
            final = self.capture()
            self.samples.append(final)
            self.started = None
            result = summarize(self.baseline, final, self.samples, duration)
            result["samples"] = self.samples
            return result

    def close(self):
        self.stop.set()
        self.thread.join(timeout=2)


def med(samples, key):
    values = [row[key] for row in samples if row[key] is not None]
    return statistics.median(values) if values else None


def summarize(first, last, samples, duration):
    steady = [
        row
        for row in samples
        if duration * 0.25 <= row["time"] - first["time"] <= duration
    ]
    steady = steady or samples
    rss, established = (
        med(steady, "rss_tree_bytes"),
        med(steady, "established_connections"),
    )
    result = {
        "elapsed_seconds": last["time"] - first["time"],
        "server_cpu_seconds": last["cpu_tree_observed_seconds"]
        - first["cpu_tree_observed_seconds"],
        "agent_cpu_seconds": last["agent_cpu_seconds"] - first["agent_cpu_seconds"],
        "idle_rss_tree_bytes": first["rss_tree_bytes"],
        "steady_rss_tree_bytes": rss,
        "peak_sampled_rss_tree_bytes": max(row["rss_tree_bytes"] for row in samples),
        "steady_pss_tree_bytes": med(steady, "pss_tree_bytes"),
        "steady_processes": med(steady, "processes"),
        "peak_processes": max(row["processes"] for row in samples),
        "steady_threads": med(steady, "threads"),
        "steady_established_connections": established,
        "rss_bytes_per_established_connection": rss / established
        if established
        else None,
        "incremental_rss_bytes_per_established_connection": (
            rss - first["rss_tree_bytes"]
        )
        / established
        if established
        else None,
    }
    if first["cgroup"] and last["cgroup"]:
        result.update(cgroup_summary(first["cgroup"], last["cgroup"], samples, steady))
    return result


def cgroup_summary(first, last, samples, steady):
    return {
        "cgroup_cpu_seconds": (
            last["cpu_stat"].get("usage_usec", 0)
            - first["cpu_stat"].get("usage_usec", 0)
        )
        / 1e6,
        "steady_cgroup_memory_bytes": statistics.median(
            row["cgroup"]["memory_current_bytes"] for row in steady
        ),
        "peak_sampled_cgroup_memory_bytes": max(
            row["cgroup"]["memory_current_bytes"] for row in samples
        ),
        "cgroup_memory_peak_lifetime_bytes": last["memory_peak_lifetime_bytes"],
        "cgroup_memory_events": {
            key: value - first["memory_events"].get(key, 0)
            for key, value in last["memory_events"].items()
        },
        "steady_cgroup_anon_bytes": statistics.median(
            row["cgroup"]["memory_stat"].get("anon", 0) for row in steady
        ),
        "steady_cgroup_file_bytes": statistics.median(
            row["cgroup"]["memory_stat"].get("file", 0) for row in steady
        ),
    }
