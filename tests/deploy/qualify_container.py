#!/usr/bin/env python3
"""Qualify an existing Bessemer image on the explicitly selected local daemon.

No image builds, pulls, credential/environment dumps, or broad container cleanup.
Only containers created by this invocation are removed.
"""

from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import os
import subprocess
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

DOCKER = ["docker", "--host=unix:///var/run/docker.sock"]
SELECTORS = (
    "DOCKER_HOST",
    "DOCKER_CONTEXT",
    "DOCKER_TLS",
    "DOCKER_TLS_VERIFY",
    "DOCKER_CERT_PATH",
)
PROCESS_ROLES_SOURCE = r"""
import json
from pathlib import Path
def process_roles():
    rows = {}
    for path in Path('/proc').iterdir():
        if not path.name.isdecimal(): continue
        try:
            value = (path / 'stat').read_text()
            fields = value.rsplit(')', 1)[1].split()
            arguments = (path / 'cmdline').read_bytes().split(b'\0')
        except OSError: continue
        if fields[0] == 'Z': continue
        try: fds = len(list((path / 'fd').iterdir()))
        except OSError: fds = None
        known = any(arg == b'bessemer' or arg.endswith(b'/bessemer') for arg in arguments[:4])
        rows[int(path.name)] = {
            'pid': int(path.name), 'ppid': int(fields[1]),
            'start_ticks': int(fields[19]), 'fds': fds,
            'comm': value.split('(', 1)[1].rsplit(')', 1)[0],
            'worker_argv': b'_worker' in arguments,
            'supervisor_argv': known and b'run' in arguments and b'_worker' not in arguments and not arguments[0].endswith(b'tini'),
        }
    workers = [row for row in rows.values() if row['worker_argv'] and rows.get(row['ppid'], {}).get('supervisor_argv')]
    parents = {row['ppid'] for row in workers}
    return {'init': rows.get(1), 'supervisors': [rows[pid] for pid in sorted(parents)],
            'workers': sorted(workers, key=lambda row: row['pid'])}
"""
PROCESS_ROLES_PROBE = PROCESS_ROLES_SOURCE + "\nprint(json.dumps(process_roles()))\n"
IMAGE_FORMAT = """{"id":{{json .Id}},"os":{{json .Os}},"architecture":{{json .Architecture}},"user":{{json .Config.User}},"working_directory":{{json .Config.WorkingDir}},"entrypoint":{{json .Config.Entrypoint}},"command":{{json .Config.Cmd}},"stop_signal":{{json .Config.StopSignal}},"healthcheck":{{json .Config.Healthcheck}}}"""
CONTAINER_FORMAT = """{"id":{{json .Id}},"image_id":{{json .Image}},"configured_user":{{json .Config.User}},"healthcheck":{{json .Config.Healthcheck}},"state":{"running":{{json .State.Running}},"pid":{{json .State.Pid}},"exit_code":{{json .State.ExitCode}},"oom_killed":{{json .State.OOMKilled}}},"limits":{"nano_cpus":{{json .HostConfig.NanoCpus}},"memory":{{json .HostConfig.Memory}},"memory_swap":{{json .HostConfig.MemorySwap}},"pids_limit":{{json .HostConfig.PidsLimit}},"read_only":{{json .HostConfig.ReadonlyRootfs}},"cap_drop":{{json .HostConfig.CapDrop}},"security_options":{{json .HostConfig.SecurityOpt}},"tmpfs":{{json .HostConfig.Tmpfs}}},"mounts":{{json .Mounts}}}"""


def utc_now():
    return datetime.now(UTC).isoformat()


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def atomic_json(path, value):
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")
    temporary.replace(path)


class Docker:
    def __init__(self):
        self.environment = os.environ.copy()
        for name in SELECTORS:
            self.environment.pop(name, None)

    def call(self, *arguments, timeout=20, check=True):
        result = subprocess.run(
            [*DOCKER, *arguments],
            env=self.environment,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        if check and result.returncode:
            raise RuntimeError(
                f"local Docker {arguments[0]} failed ({result.returncode}): {result.stderr[-3000:]}"
            )
        return result

    def json(self, *arguments):
        return json.loads(self.call(*arguments).stdout)


class Container:
    def __init__(
        self,
        docker,
        image,
        output,
        scenario,
        memory_mib,
        *,
        fixture=False,
        grace=1,
        fixture_path=None,
    ):
        self.docker, self.output, self.scenario = docker, output, scenario
        self.cid = None
        self.port = None
        self.result = {"name": scenario, "passed": False, "started_at": utc_now()}
        name = f"bessemer-qualification-{uuid.uuid4().hex[:16]}"
        arguments = [
            "create",
            "--pull=never",
            "--name",
            name,
            "--label",
            f"bessemer.qualification={name}",
            "--cpus",
            "1",
            "--memory",
            f"{memory_mib}m",
            "--memory-swap",
            f"{memory_mib}m",
            "--pids-limit",
            "128",
            "--read-only",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--tmpfs",
            "/tmp:rw,noexec,nosuid,nodev,size=16777216,mode=1777",
            "--publish",
            "127.0.0.1::8000",
            "--health-interval",
            "1s",
            "--health-start-period",
            "1s",
            "--health-start-interval",
            "1s",
            "--health-timeout",
            "3s",
            "--health-retries",
            "3",
        ]
        if fixture:
            source = (
                fixture_path or Path(__file__).with_name("fixture_app.py")
            ).resolve()
            arguments.extend(
                [
                    "--mount",
                    f"type=bind,source={source},destination=/app/fixture_app.py,readonly",
                    "--env",
                    "BESSEMER_QUALIFICATION_FAULTS=1",
                ]
            )
        arguments.append(image)
        if fixture:
            arguments.extend(
                [
                    "run",
                    "fixture_app:app",
                    "--host",
                    "0.0.0.0",
                    "--port",
                    "8000",
                    "--workers",
                    "1",
                    "--startup-timeout",
                    "10",
                    "--worker-timeout",
                    "0",
                    "--max-in-flight",
                    "4",
                    "--request-timeout",
                    "60",
                    "--graceful-timeout",
                    str(grace),
                ]
            )
        try:
            self.cid = docker.call(*arguments).stdout.strip()
        except BaseException:
            # A daemon may have created the object before the CLI lost its
            # response. Only remove it after checking this invocation's label.
            ownership = docker.call(
                "inspect",
                "--format",
                '{{index .Config.Labels "bessemer.qualification"}}',
                name,
                check=False,
            )
            if ownership.returncode == 0 and ownership.stdout.strip() == name:
                docker.call("rm", "--force", "--volumes", name, check=False)
            raise
        self.result["container_id"] = self.cid

    def inspect(self):
        return self.docker.json("inspect", "--format", CONTAINER_FORMAT, self.cid)

    def logs(self):
        result = self.docker.call("logs", "--tail", "2000", self.cid, check=False)
        return result.stdout + result.stderr

    def events(self, name):
        found = []
        for line in self.logs().splitlines():
            try:
                item = json.loads(line)
            except ValueError:
                continue
            if isinstance(item, dict) and item.get("event") == name:
                found.append(item)
        return found

    def worker_pids(self):
        roles = self.docker.json("exec", self.cid, "python", "-c", PROCESS_ROLES_PROBE)
        verify_process_roles(roles)
        return [row["pid"] for row in roles["workers"]]

    def start(self):
        self.docker.call("start", self.cid)
        self.port = int(
            self.docker.call(
                "inspect",
                "--format",
                '{{(index (index .NetworkSettings.Ports "8000/tcp") 0).HostPort}}',
                self.cid,
            ).stdout
        )
        self.wait(lambda: self.request("GET", "/healthz")[0] == 200, timeout=15)
        self.wait(
            lambda: (
                self.docker.call(
                    "inspect", "--format", "{{.State.Health.Status}}", self.cid
                ).stdout.strip()
                == "healthy"
            ),
            timeout=10,
        )
        self.result["running_inspect"] = self.inspect()
        self.result["readiness"] = {
            "http_health_status": 200,
            "docker_health_status": "healthy",
        }

    @staticmethod
    def wait(predicate, *, timeout=8):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                result = predicate()
                if result:
                    return result
            except (OSError, ValueError, http.client.HTTPException):
                pass
            time.sleep(0.03)
        raise AssertionError("container qualification condition timed out")

    def request(self, method, path):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=4)
        try:
            connection.request(method, path, headers={"Connection": "close"})
            response = connection.getresponse()
            return response.status, response.read(65536)
        finally:
            connection.close()

    def identity(self):
        status, body = self.request("GET", "/identity")
        return json.loads(body) if status == 200 else None

    def stop(self, *, signal="TERM", expected=0, budget=4):
        started = time.monotonic()
        self.docker.call("kill", "--signal", signal, self.cid)
        status = int(self.docker.call("wait", self.cid, timeout=budget + 2).stdout)
        elapsed = time.monotonic() - started
        inspected = self.inspect()
        self.result["shutdown"] = {
            "signal": signal,
            "exit_code": status,
            "seconds": elapsed,
            "budget": budget,
            "inspect": inspected,
        }
        assert status == expected, f"expected exit {expected}, got {status}"
        assert elapsed <= budget, f"shutdown exceeded {budget}s: {elapsed}"
        assert (
            not inspected["state"]["running"] and not inspected["state"]["oom_killed"]
        )
        assert inspected["state"]["pid"] == 0

    def close(self):
        if self.cid is not None:
            try:
                self.result["ended_at"] = utc_now()
                self.result["final_inspect"] = self.inspect()
                log = self.output / f"{self.scenario}.log"
                log.write_text(self.logs())
                self.result["log"] = log.name
            finally:
                removed = self.docker.call(
                    "rm", "--force", "--volumes", self.cid, check=False
                )
                self.result["removed"] = removed.returncode == 0
                if removed.returncode:
                    raise RuntimeError(f"failed to remove owned container {self.cid}")


def verify_limits(inspected, memory_mib, result):
    limits = inspected["limits"]
    checks = {
        "cpu_limit_one": limits["nano_cpus"] == 1_000_000_000,
        "memory_limit": limits["memory"] == memory_mib * 1024 * 1024,
        "swap_disabled": limits["memory_swap"] == limits["memory"],
        "pids_limit_128": limits["pids_limit"] == 128,
        "root_filesystem_read_only": limits["read_only"],
        "all_capabilities_dropped": limits["cap_drop"] == ["ALL"],
        "no_new_privileges": any(
            value.split(":")[0] == "no-new-privileges"
            for value in limits["security_options"] or []
        ),
        "temporary_filesystem": "/tmp" in limits["tmpfs"],
    }
    result["configured_restriction_checks"] = checks
    assert all(checks.values()), (
        f"configured restrictions failed: {[key for key, value in checks.items() if not value]}"
    )


def verify_identity(identity, args, result):
    checks = {
        "non_root_uid": identity["uid"] == args.expected_uid,
        "non_root_gid": identity["gid"] == args.expected_uid,
        "tini_is_pid1": identity["init_comm"] == "tini",
        "supervisor_is_init_child": identity["ppid"] != 1
        and identity["supervisor_ppid"] == 1,
        "worker_argv_identified": identity["worker_argv"],
        "supervisor_argv_identified": identity["supervisor_argv"],
        "effective_capabilities_zero": int(identity["cap_effective"], 16) == 0,
        "no_new_privileges_observed": identity["no_new_privileges"] == "1",
        "application_directory_read_only": not identity["application_directory"][
            "writable"
        ],
        "temporary_directory_writable": identity["temporary_directory"]["writable"],
        "expected_version": not args.expected_version
        or identity["version"] == args.expected_version,
    }
    group = identity["cgroup"]
    checks.update(
        {
            "cpu_cgroup_observed": False,
            "memory_cgroup_observed": False,
            "pids_cgroup_observed": False,
        }
    )
    try:
        if group["cpu_max"] is not None:
            quota, period = group["cpu_max"].split()
            checks["cpu_cgroup_observed"] = (
                quota != "max" and 0 < int(quota) / int(period) <= 1
            )
            checks["memory_cgroup_observed"] = (
                0 < int(group["memory_max"]) <= args.memory_mib * 1024 * 1024
            )
            checks["pids_cgroup_observed"] = 0 < int(group["pids_max"]) <= 128
        else:
            checks["cpu_cgroup_observed"] = (
                0 < int(group["v1_cpu_quota"]) / int(group["v1_cpu_period"]) <= 1
            )
            checks["memory_cgroup_observed"] = (
                0 < int(group["v1_memory_limit"]) <= args.memory_mib * 1024 * 1024
            )
            checks["pids_cgroup_observed"] = 0 < int(group["v1_pids_max"]) <= 128
    except (TypeError, ValueError, ZeroDivisionError):
        pass
    result.setdefault("observed_identities", []).append(
        {"identity": identity, "checks": checks}
    )
    assert all(checks.values()), (
        f"observed restrictions failed: {[key for key, value in checks.items() if not value]}"
    )


def verify_process_roles(roles):
    assert (
        roles["init"] and roles["init"]["pid"] == 1 and roles["init"]["comm"] == "tini"
    ), "qualified image requires tini as PID1"
    assert len(roles["supervisors"]) == 1, "expected one Bessemer supervisor"
    supervisor = roles["supervisors"][0]
    assert (
        supervisor["pid"] != 1
        and supervisor["ppid"] == 1
        and supervisor["supervisor_argv"]
    ), "invalid supervisor role"
    assert roles["workers"] and all(
        row["worker_argv"] and row["ppid"] == supervisor["pid"]
        for row in roles["workers"]
    ), "invalid worker role"


def default_application(container, args):
    container.start()
    verify_limits(
        container.result["running_inspect"], args.memory_mib, container.result
    )
    status, body = container.request("GET", "/")
    assert status == 200 and json.loads(body) == {"message": "Hello from Bessemer"}
    probe = "import json,os;print(json.dumps({'uid':os.getuid(),'gid':os.getgid()}))"
    identity = container.docker.json("exec", container.cid, "python", "-c", probe)
    assert identity == {"uid": args.expected_uid, "gid": args.expected_uid}
    container.result["identity"] = identity
    roles = container.docker.json(
        "exec", container.cid, "python", "-c", PROCESS_ROLES_PROBE
    )
    verify_process_roles(roles)
    container.result["process_roles"] = roles
    container.stop(budget=4)


def lifecycle(container, args):
    container.start()
    verify_limits(
        container.result["running_inspect"], args.memory_mib, container.result
    )
    identity = container.wait(container.identity)
    verify_identity(identity, args, container.result)
    container.result["identity"] = identity
    started = time.monotonic()
    try:
        container.request("POST", "/__qualification/crash?enabled=true")
    except (OSError, http.client.HTTPException):
        pass
    else:
        raise AssertionError("fault-enabled worker did not exit")
    container.wait(
        lambda: any(
            item.get("pid") == identity["pid"]
            for item in container.events("worker_crash")
        )
    )
    replacement = container.wait(
        lambda: (
            (item := container.identity()) and item["pid"] != identity["pid"] and item
        )
    )
    container.result["worker_recovery"] = {
        "original_pid": identity["pid"],
        "requested_exit_code": 41,
        "replacement_pid": replacement["pid"],
        "seconds": time.monotonic() - started,
    }
    verify_identity(replacement, args, container.result)
    reload_tag = uuid.uuid4().hex
    with ThreadPoolExecutor(max_workers=1) as executor:
        old_request = executor.submit(
            container.request, "GET", f"/hold?tag={reload_tag}&seconds=2"
        )
        container.wait(
            lambda: any(
                item.get("tag") == reload_tag
                for item in container.events("hold_started")
            )
        )
        reload_started = time.monotonic()
        container.docker.call("kill", "--signal", "HUP", container.cid)
        reloaded = container.wait(
            lambda: (
                (item := container.identity())
                and item["pid"] != replacement["pid"]
                and item
            )
        )
        drain_marker = (
            f"worker draining pid={replacement['pid']} reason=replacement ready"
        )
        container.wait(lambda: drain_marker in container.logs())
        overlap = container.worker_pids()
        assert sorted(overlap) == sorted([replacement["pid"], reloaded["pid"]]), (
            f"expected two live overlapping workers, observed {overlap}"
        )
        assert not old_request.done(), (
            "old request ended before replacement overlap was observed"
        )
        verify_identity(reloaded, args, container.result)
        container.result["rolling_reload"] = {
            "old_pid": replacement["pid"],
            "replacement_pid": reloaded["pid"],
            "overlap_worker_pids": overlap,
            "replacement_ready_seconds": time.monotonic() - reload_started,
        }
        status, body = old_request.result(timeout=3)
        assert status == 200 and json.loads(body)["pid"] == replacement["pid"], (
            "old worker did not finish its draining request"
        )
    tag = uuid.uuid4().hex
    with ThreadPoolExecutor(max_workers=1) as executor:
        held = executor.submit(
            container.request, "GET", f"/hold?tag={tag}&seconds=0.35"
        )
        container.wait(
            lambda: any(
                item.get("tag") == tag for item in container.events("hold_started")
            )
        )
        container.stop(budget=4)
        status, body = held.result(timeout=2)
        assert status == 200 and json.loads(body)["completed"]
    assert any(item.get("tag") == tag for item in container.events("hold_completed")), (
        "final in-flight request completion was not logged"
    )
    assert any(
        item.get("pid") == reloaded["pid"]
        for item in container.events("lifespan_closed")
    ), "active worker lifespan cleanup was not logged"
    container.result["in_flight_request_completed"] = True
    container.result["lifespan_cleanup_observed"] = True


def blocked_shutdown(container, args):
    container.start()
    verify_limits(
        container.result["running_inspect"], args.memory_mib, container.result
    )
    verify_identity(container.wait(container.identity), args, container.result)
    with ThreadPoolExecutor(max_workers=1) as executor:
        blocked = executor.submit(
            container.request, "GET", "/__qualification/block?enabled=true"
        )
        container.wait(lambda: '"event": "loop_blocked"' in container.logs())
        container.stop(budget=3)
        try:
            blocked.result(timeout=2)
        except (OSError, http.client.HTTPException):
            pass
        else:
            raise AssertionError("blocked request unexpectedly completed")
    container.result["blocked_loop_hard_shutdown"] = True


def process_identity(pid):
    try:
        parts = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        return {"state": parts[0], "start_ticks": int(parts[19])}
    except (OSError, ValueError, IndexError):
        return None


def namespace_pid(pid):
    try:
        for line in Path(f"/proc/{pid}/status").read_text().splitlines():
            if line.startswith("NSpid:"):
                return int(line.split()[-1])
    except (OSError, ValueError):
        pass
    return None


def parent_death(container, args):
    container.start()
    verify_limits(
        container.result["running_inspect"], args.memory_mib, container.result
    )
    identity = container.wait(container.identity)
    verify_identity(identity, args, container.result)
    before = container.docker.call(
        "top", container.cid, "-eo", "pid,ppid"
    ).stdout.splitlines()[1:]
    processes = [
        {
            "pid": int(line.split()[0]),
            "ppid": int(line.split()[1]),
            "namespace_pid": namespace_pid(int(line.split()[0])),
            "identity": process_identity(int(line.split()[0])),
        }
        for line in before
        if line.strip()
    ]
    container.result["host_processes_before"] = processes
    with ThreadPoolExecutor(max_workers=1) as executor:
        blocked = executor.submit(
            container.request, "GET", "/__qualification/block?enabled=true"
        )
        container.wait(lambda: '"event": "loop_blocked"' in container.logs())
        container.stop(signal="KILL", expected=137, budget=3)
        try:
            blocked.result(timeout=2)
        except (OSError, http.client.HTTPException):
            pass
        else:
            raise AssertionError("blocked request survived container PID1 death")
    observed = [item for item in processes if item["identity"] is not None]
    assert any(item["namespace_pid"] == identity["pid"] for item in observed), (
        "worker host identity was not observable"
    )
    assert any(item["namespace_pid"] == 1 for item in observed), (
        "supervisor host identity was not observable"
    )
    assert any(item["namespace_pid"] == identity["ppid"] for item in observed), (
        "Bessemer supervisor host identity was not observable"
    )
    for item in observed:
        container.wait(
            lambda item=item: (
                (current := process_identity(item["pid"])) is None
                or current["state"] == "Z"
                or current["start_ticks"] != item["identity"]["start_ticks"]
            ),
            timeout=3,
        )
    container.result["host_processes_after"] = [
        {"pid": item["pid"], "identity": process_identity(item["pid"])}
        for item in observed
    ]
    container.result["no_live_host_processes"] = True


def orphan_reaping(container, args):
    container.start()
    verify_limits(
        container.result["running_inspect"], args.memory_mib, container.result
    )
    original = container.wait(container.identity)
    verify_identity(original, args, container.result)
    status, body = container.request(
        "POST", "/__qualification/spawn-child?enabled=true"
    )
    child = json.loads(body)
    assert status == 200 and child["worker_pid"] == original["pid"]
    assert child["child_ppid"] == original["pid"]
    assert child["child_pid"] not in {1, original["pid"], original["ppid"]}
    container.wait(
        lambda: any(
            event.get("child_pid") == child["child_pid"]
            for event in container.events("grandchild_started")
        )
    )
    started = time.monotonic()
    try:
        container.request("POST", "/__qualification/crash?enabled=true")
    except (OSError, http.client.HTTPException):
        pass
    else:
        raise AssertionError("worker crash injection did not exit")
    replacement = container.wait(
        lambda: (
            (item := container.identity()) and item["pid"] != original["pid"] and item
        )
    )
    verify_identity(replacement, args, container.result)
    assert replacement["ppid"] == original["ppid"], (
        "supervisor changed during orphan reaping"
    )
    probe = """import json,sys
from pathlib import Path
try:
    fields=Path(f'/proc/{int(sys.argv[1])}/stat').read_text().rsplit(')',1)[1].split()
    result={'exists':True,'state':fields[0],'start_ticks':int(fields[19])}
except FileNotFoundError:
    result={'exists':False}
print(json.dumps(result))
"""
    final_child = None

    def reaped():
        nonlocal final_child
        final_child = container.docker.json(
            "exec", container.cid, "python", "-c", probe, str(child["child_pid"])
        )
        # A zombie with the original identity is a failure. PID reuse proves
        # that the original process was reaped, rather than merely terminated.
        return (
            not final_child["exists"]
            or final_child["start_ticks"] != child["child_start_ticks"]
        )

    container.wait(reaped, timeout=4)
    roles = container.docker.json(
        "exec", container.cid, "python", "-c", PROCESS_ROLES_PROBE
    )
    verify_process_roles(roles)
    assert roles["supervisors"][0]["pid"] == original["ppid"]
    container.result["orphan_reaping"] = {
        "injected_child": child,
        "original_worker_pid": original["pid"],
        "replacement_worker_pid": replacement["pid"],
        "final_child_observation": final_child,
        "seconds": time.monotonic() - started,
        "init_and_supervisor_still_alive": True,
        "zombie_reaped": True,
        "process_roles": roles,
    }
    container.stop(budget=4)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--memory-mib", type=int, choices=(256, 512), default=512)
    parser.add_argument("--expected-uid", type=int, default=10001)
    parser.add_argument("--expected-version")
    args = parser.parse_args()
    if not __debug__:
        parser.error("qualification requires assertions; run Python without -O")
    if args.expected_uid <= 0:
        parser.error("expected UID must be a positive non-root numeric UID")
    if args.output.exists() and (
        not args.output.is_dir() or any(args.output.iterdir())
    ):
        parser.error("output must be a new or empty directory for immutable evidence")
    args.output.mkdir(parents=True, exist_ok=True)
    fixture = Path(__file__).with_name("fixture_app.py")
    staged_fixture = args.output / "fixture_app.py"
    staged_fixture.write_bytes(fixture.read_bytes())
    staged_fixture.chmod(0o444)
    docker = Docker()
    manifest = {
        "started_at": utc_now(),
        "passed": False,
        "image": args.image,
        "docker_endpoint": "unix:///var/run/docker.sock",
        "cleared_docker_selectors": list(SELECTORS),
        "memory_mib": args.memory_mib,
        "expected_uid": args.expected_uid,
        "expected_version": args.expected_version,
        "fixture_sha256": digest(staged_fixture),
        "staged_fixture": staged_fixture.name,
        "staged_fixture_mode": oct(staged_fixture.stat().st_mode & 0o777),
        "harness_sha256": digest(__file__),
        "scenarios": [],
    }
    try:
        manifest["docker_server_version"] = docker.call(
            "info", "--format", "{{.ServerVersion}}"
        ).stdout.strip()
        manifest["image_inspect"] = docker.json(
            "image", "inspect", "--format", IMAGE_FORMAT, args.image
        )
        image = manifest["image_inspect"]
        assert image["user"].split(":")[0] == str(args.expected_uid), (
            "image must declare a numeric non-root USER"
        )
        assert image["stop_signal"] == "SIGTERM"
        assert image["working_directory"] == "/app" and image["healthcheck"]
        for name, action, fixture_enabled, grace in (
            ("default-application", default_application, False, 1),
            ("worker-recovery-and-graceful-drain", lifecycle, True, 3),
            ("blocked-loop-hard-shutdown", blocked_shutdown, True, 0.4),
            ("container-parent-death", parent_death, True, 0.4),
            ("worker-crash-orphan-reaping", orphan_reaping, True, 1),
        ):
            container = Container(
                docker,
                image["id"],
                args.output,
                name,
                args.memory_mib,
                fixture=fixture_enabled,
                grace=grace,
                fixture_path=staged_fixture,
            )
            manifest["scenarios"].append(container.result)
            try:
                action(container, args)
                assert container.result["running_inspect"]["image_id"] == image["id"]
                container.result["passed"] = True
            except Exception as error:  # noqa: BLE001 -- Preserve qualification failure and container evidence.
                container.result["error"] = f"{type(error).__name__}: {error}"
            finally:
                container.close()
                atomic_json(args.output / "manifest.json", manifest)
        manifest["passed"] = all(item["passed"] for item in manifest["scenarios"])
    except Exception as error:  # noqa: BLE001 -- Record setup failures without dumping environment/config.
        manifest["error"] = f"{type(error).__name__}: {error}"
    finally:
        manifest["ended_at"] = utc_now()
        atomic_json(args.output / "manifest.json", manifest)
    print(
        json.dumps(
            {
                "passed": manifest["passed"],
                "manifest": str(args.output / "manifest.json"),
            }
        )
    )
    return 0 if manifest["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
