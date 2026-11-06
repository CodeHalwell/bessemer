"""CPU entitlement shared by automatic worker and executor sizing."""

from __future__ import annotations

import os
import re
from fractions import Fraction
from math import ceil
from pathlib import Path, PurePosixPath

_CGROUP_ROOT = Path("/sys/fs/cgroup")
_MAX_CPUS = 256


def _read(path: Path) -> str:
    try:
        return path.read_text().strip()
    except (OSError, UnicodeError):
        return ""


def _ranges(values):
    merged = []
    for start, end in sorted(values):
        if start < 0 or end < start:
            raise ValueError("invalid CPU range")
        if merged and start <= merged[-1][1] + 1:
            merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
        else:
            merged.append((start, end))
    return merged


def _cpuset(text: str):
    if not text:
        return None  # An empty configured cpuset inherits its parent.
    values = []
    try:
        for entry in text.split(","):
            ends = entry.split("-")
            if len(ends) not in (1, 2):
                return None
            start, end = int(ends[0]), int(ends[-1])
            values.append((start, end))
        return _ranges(values)
    except ValueError:
        return None


def _intersection(left, right):
    result, first, second = [], 0, 0
    while first < len(left) and second < len(right):
        low = max(left[first][0], right[second][0])
        high = min(left[first][1], right[second][1])
        if low <= high:
            result.append((low, high))
        if left[first][1] < right[second][1]:
            first += 1
        else:
            second += 1
    return result


def _ancestors(base: Path, relative: str):
    candidate = (base / relative.lstrip("/")).resolve()
    if not candidate.is_relative_to(base):
        return []
    return [candidate, *candidate.parents[: len(candidate.parts) - len(base.parts)]]


def _memberships(proc_root):
    result = []
    for line in _read(proc_root / "self/cgroup").splitlines():
        try:
            hierarchy, controllers, group = line.split(":", 2)
        except ValueError:
            continue
        if group.startswith("/"):
            result.append((hierarchy, set(controllers.split(",")) - {""}, group))
    return result


def _unescape_mount(text):
    return re.sub(r"\\([0-7]{3})", lambda match: chr(int(match[1], 8)), text)


def _mount_record(line, root):
    try:
        before, after = line.split(" - ", 1)
        mount, filesystem = before.split(), after.split()
        kind = filesystem[0]
        if kind not in {"cgroup", "cgroup2"}:
            return None
        mount_root = PurePosixPath(_unescape_mount(mount[3]))
        base = Path(_unescape_mount(mount[4])).resolve()
        if root != _CGROUP_ROOT.resolve() and not base.is_relative_to(root):
            return None
        return kind, mount_root, base, set(filesystem[2].split(","))
    except (IndexError, ValueError, OSError):
        return None


def _mounted_controllers(kind, available, hierarchy, controllers):
    if kind == "cgroup2":
        return ("v2", "cpuset") if hierarchy == "0" and not controllers else ()
    matched = available & controllers
    return tuple(
        name
        for controller, name in (("cpu", "v1"), ("cpuset", "cpuset"))
        if controller in matched
    )


def _mount_membership(record, membership):
    kind, mount_root, base, available = record
    hierarchy, controllers, group = membership
    kinds = _mounted_controllers(kind, available, hierarchy, controllers)
    if not kinds:
        return []
    try:
        relative = str(PurePosixPath(group).relative_to(mount_root))
    except ValueError:
        return []
    directories = _ancestors(base, relative)
    return [(controller, directories) for controller in kinds]


def _mount_paths(root, proc_root, memberships):
    """Resolve controller mounts, including bind-mounted/delegated roots."""
    for line in _read(proc_root / "self/mountinfo").splitlines():
        record = _mount_record(line, root)
        if record is not None:
            for membership in memberships:
                yield from _mount_membership(record, membership)


def _cgroup_paths(root, proc_root):
    v2 = [root]
    v1 = [root, root / "cpu", root / "cpu,cpuacct"]
    cpuset = [root]
    memberships = _memberships(proc_root)
    for hierarchy, controllers, group in memberships:
        if hierarchy == "0" and not controllers:
            directories = _ancestors(root, group)
            v2.extend(directories)
            cpuset.extend(directories)
        if "cpu" in controllers:
            for base in [root, root / "cpu", root / "cpu,cpuacct"]:
                v1.extend(_ancestors(base, group))
        if "cpuset" in controllers:
            for base in [root, root / "cpuset"]:
                cpuset.extend(_ancestors(base, group))
    for controller, directories in _mount_paths(root, proc_root, memberships):
        {"v2": v2, "v1": v1, "cpuset": cpuset}[controller].extend(directories)
    return dict.fromkeys(v2), dict.fromkeys(v1), dict.fromkeys(cpuset)


def _processor_counts():
    limits = []
    for count in (getattr(os, "process_cpu_count", lambda: None), os.cpu_count):
        try:
            value = count()
            if value is not None and value > 0:
                limits.append(value)
        except (OSError, ValueError):
            pass
    return limits


def _affinity():
    try:
        affinity = os.sched_getaffinity(0)
        if affinity:
            return _ranges((cpu, cpu) for cpu in affinity)
    except (AttributeError, OSError, ValueError):
        pass
    return None


def _quota_fraction(quota, period):
    quota, period = int(quota), int(period)
    return Fraction(quota, period) if quota > 0 and period > 0 else None


def _v2_quota(directory):
    try:
        quota, period = _read(directory / "cpu.max").split()
        return None if quota == "max" else _quota_fraction(quota, period)
    except ValueError:
        return None


def _v1_quota(directory):
    try:
        return _quota_fraction(
            _read(directory / "cpu.cfs_quota_us"),
            _read(directory / "cpu.cfs_period_us"),
        )
    except ValueError:
        return None


def _quotas(v2_paths, v1_paths):
    for paths, parse in ((v2_paths, _v2_quota), (v1_paths, _v1_quota)):
        for directory in paths:
            quota = parse(directory)
            if quota is not None:
                yield quota


def _allowed_cpus(paths, allowed):
    for directory in paths:
        for name in ("cpuset.cpus.effective", "cpuset.effective_cpus", "cpuset.cpus"):
            cpus = _cpuset(_read(directory / name))
            if cpus is not None:
                allowed = cpus if allowed is None else _intersection(allowed, cpus)
    return allowed


def cpu_budget(
    cgroup_root: Path = _CGROUP_ROOT, *, proc_root: Path = Path("/proc")
) -> int:
    """Available CPUs, rounded up for fractional quotas and capped at 256.

    Affinity, interpreter/process CPU counts and visible enclosing cgroup
    quota/cpuset limits all constrain automatic sizing. Explicit worker/pool
    settings remain the caller's choice. Missing or malformed probes are ignored.
    """
    limits = _processor_counts()
    v2_paths, v1_paths, cpuset_paths = _cgroup_paths(cgroup_root.resolve(), proc_root)
    limits.extend(_quotas(v2_paths, v1_paths))
    allowed = _allowed_cpus(cpuset_paths, _affinity())
    if allowed is not None:
        limits.append(sum(end - start + 1 for start, end in allowed))
    return max(1, min(_MAX_CPUS, ceil(min(limits or [1]))))
