#!/usr/bin/env python3
"""Qualify an immutable production image using owned, bounded local containers."""

from __future__ import annotations

import argparse
import http.client
import json
import math
import secrets
import time
import uuid
from pathlib import Path

from production_soak import LiveLogCapture, run_production_soak
from qualify_container import (
    CONTAINER_FORMAT,
    IMAGE_FORMAT,
    PROCESS_ROLES_PROBE,
    SELECTORS,
    Docker,
    atomic_json,
    digest,
    utc_now,
    verify_limits,
    verify_process_roles,
)

KEY_NAME = "BESSEMER_EXAMPLE_API_KEY"


class ProductionContainer:
    def __init__(self, image, *, key=None):
        self.docker = Docker()
        self.cid = None
        self.port = None
        self.name = "bessemer-production-http-" + uuid.uuid4().hex[:16]
        arguments = [
            "create",
            "--pull=never",
            "--name",
            self.name,
            "--label",
            f"bessemer.production-qualification={self.name}",
            "--cpus",
            "1",
            "--memory",
            "512m",
            "--memory-swap",
            "512m",
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
            "--env",
            "BESSEMER_EXAMPLE_DOCS=0",
        ]
        self.docker.environment.pop(KEY_NAME, None)
        if key is not None:
            self.docker.environment[KEY_NAME] = key
            arguments += ["--env", KEY_NAME]
        arguments += [
            image,
            "run",
            "examples.production:app",
            "--host",
            "0.0.0.0",
            "--port",
            "8000",
            "--workers",
            "1",
            "--startup-timeout",
            "5",
            "--worker-timeout",
            "0",
            "--graceful-timeout",
            "1",
            "--restart-limit",
            "0",
        ]
        try:
            self.cid = self.docker.call(*arguments).stdout.strip()
        except BaseException:
            owner = self.docker.call(
                "inspect",
                "--format",
                '{{index .Config.Labels "bessemer.production-qualification"}}',
                self.name,
                check=False,
            )
            if owner.returncode == 0 and owner.stdout.strip() == self.name:
                self.docker.call("rm", "--force", "--volumes", self.name, check=False)
            raise

    def inspect(self):
        return self.docker.json("inspect", "--format", CONTAINER_FORMAT, self.cid)

    def start(self):
        self.docker.call("start", self.cid)
        ports = self.docker.call(
            "inspect", "--format", "{{json .NetworkSettings.Ports}}", self.cid
        ).stdout
        binding = json.loads(ports).get("8000/tcp")
        if binding:
            assert binding[0]["HostIp"] == "127.0.0.1"
            self.port = int(binding[0]["HostPort"])

    def request(self, method, path, *, key=None, body=None, host=None):
        headers = {"Connection": "close"}
        if key is not None:
            headers["X-API-Key"] = key
        if host is not None:
            headers["Host"] = host
        if body is not None:
            headers["Content-Type"] = "application/json"
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=1)
        try:
            connection.request(method, path, body, headers)
            response = connection.getresponse()
            return response.status, response.read(65536)
        finally:
            connection.close()

    def logs(self):
        value = self.docker.call("logs", "--tail", "1000", self.cid, check=False)
        return value.stdout + value.stderr

    def close(self):
        if self.cid is not None:
            result = self.docker.call(
                "rm", "--force", "--volumes", self.cid, check=False
            )
            if result.returncode:
                raise RuntimeError("failed to remove an owned qualification container")
            self.docker.environment.pop(KEY_NAME, None)


def wait_health(container):
    deadline = time.monotonic() + 12
    while time.monotonic() < deadline:
        assert container.inspect()["state"]["running"], (
            "production container exited before readiness"
        )
        try:
            if container.request("GET", "/healthz")[0] == 200:
                return
        except (OSError, http.client.HTTPException):
            pass
        time.sleep(0.03)
    raise AssertionError("production image never became ready")


def has_lifecycle_error(logs):
    return any(
        value in logs
        for value in ("BrokenPipeError", "lifespan task exited with an error", "EPIPE")
    )


def run_scenario(
    image, output, secret, missing=False, expected_version="0.2.0a3", soak_duration=0
):
    result = {
        "name": "missing-key" if missing else "production-http",
        "passed": False,
        "started_at": utc_now(),
    }
    container = ProductionContainer(image, key=None if missing else secret)
    result["container_id"] = container.cid
    result["ownership_label"] = container.name
    capture = None
    try:
        container.start()
        if soak_duration and not missing:
            capture = LiveLogCapture(
                container, output / "production-http.full.log", secret
            )
        if missing:
            served = False
            observed_statuses = []
            deadline = time.monotonic() + 8
            while (
                container.inspect()["state"]["running"] and time.monotonic() < deadline
            ):
                if container.port is not None:
                    try:
                        observed = container.request("GET", "/healthz")[0]
                        observed_statuses.append(observed)
                        served |= observed == 200
                    except (OSError, http.client.HTTPException):
                        pass
                time.sleep(0.03)
            status = int(
                container.docker.call("wait", container.cid, timeout=10).stdout
            )
            result["exit_code"] = status
            result["observed_http_statuses"] = observed_statuses
            result["checks"] = {
                "missing_key_exits_nonzero": status != 0,
                "never_served_http_200": not served,
            }
            assert status != 0, "missing API-key environment did not fail startup"
            assert not served, "missing-key container served HTTP before failing"
            assert not container.inspect()["state"]["running"]
        else:
            wait_health(container)
            result["running_inspect"] = container.inspect()
            verify_limits(result["running_inspect"], 512, result)
            assert result["running_inspect"]["image_id"] == image
            identity_code = (
                "import hashlib,json,os,pathlib,bessemer,pydantic;"
                "p=pathlib.Path('/app/examples/production.py');"
                "print(json.dumps({'uid':os.getuid(),'gid':os.getgid(),"
                "'version':bessemer.__version__,'pydantic':pydantic.__version__,"
                "'example_sha256':hashlib.sha256(p.read_bytes()).hexdigest()}))"
            )
            result["identity"] = container.docker.json(
                "exec", container.cid, "python", "-c", identity_code
            )
            assert (
                result["identity"]["uid"] == 10001
                and result["identity"]["gid"] == 10001
            )
            assert result["identity"]["version"] == expected_version
            roles = container.docker.json(
                "exec", container.cid, "python", "-c", PROCESS_ROLES_PROBE
            )
            verify_process_roles(roles)
            result["process_roles"] = roles
            checks = {}
            contracts = []

            def check(name, method, path, expected, *, predicate=None, **kwargs):
                status, body = container.request(method, path, **kwargs)
                okay = status == expected and (predicate is None or predicate(body))
                checks[name] = {"status": status, "expected": expected, "passed": okay}
                assert okay, f"production HTTP check failed: {name}"
                contracts.append((name, method, path, expected, predicate, kwargs))

            result["checks"] = checks
            check(
                "public_health",
                "GET",
                "/healthz",
                200,
                predicate=lambda b: json.loads(b) == {"ok": True},
            )
            check(
                "public_readiness",
                "GET",
                "/readyz",
                200,
                predicate=lambda b: b == b"ready",
            )
            check("unauthenticated_denied", "GET", "/items/1", 401)
            check(
                "authenticated_model",
                "GET",
                "/items/1",
                200,
                key=secret,
                predicate=lambda b: (
                    json.loads(b)
                    == {"id": 1, "name": "Example item", "price_cents": None}
                ),
            )
            check(
                "typed_query",
                "GET",
                "/items/1?include_price=true",
                200,
                key=secret,
                predicate=lambda b: json.loads(b)["price_cents"] == 1250,
            )
            check(
                "quote_request_response",
                "POST",
                "/items/quote",
                200,
                key=secret,
                body=b'{"item_id":1,"quantity":3}',
                predicate=lambda b: (
                    json.loads(b) == {"item_id": 1, "quantity": 3, "total_cents": 3750}
                ),
            )
            check(
                "typed_query_invalid",
                "GET",
                "/items/1?include_price=private-invalid",
                422,
                key=secret,
                predicate=lambda b: b"private-invalid" not in b and b'"input"' not in b,
            )
            check(
                "model_invalid",
                "POST",
                "/items/quote",
                422,
                key=secret,
                body=b'{"item_id":1,"quantity":0}',
            )
            check("host_spoof_denied", "GET", "/healthz", 400, host="attacker.invalid")
            check("docs_disabled", "GET", "/docs", 404, key=secret)
            check("schema_disabled", "GET", "/openapi.json", 404, key=secret)
            if soak_duration:
                result["soak"] = run_production_soak(
                    container, contracts, output, soak_duration, capture
                )
                assert result["soak"]["passed"], "production-image soak failed"
                assert capture.summary()["collector_exit_code"] is None, (
                    "full log stream ended before shutdown"
                )
            started = time.monotonic()
            container.docker.call("kill", "--signal", "TERM", container.cid)
            exit_code = int(
                container.docker.call("wait", container.cid, timeout=6).stdout
            )
            result["shutdown"] = {
                "signal": "TERM",
                "exit_code": exit_code,
                "seconds": time.monotonic() - started,
            }
            assert exit_code == 0 and not container.inspect()["state"]["running"]
        logs = container.logs()
        assert secret not in logs, "generated API key leaked into container logs"
        result["secret_absent_from_logs"] = True
        lifecycle_defect = has_lifecycle_error(logs)
        result["known_heartbeat_lifecycle_defect_observed"] = lifecycle_defect
        result["http_contract_passed"] = True
        result["passed"] = not lifecycle_defect
        if lifecycle_defect:
            result["limitation"] = (
                "Known heartbeat reader closure defect observed; final lifecycle qualification requires rebuilt image."
            )
    except Exception as error:  # noqa: BLE001 -- Preserve evidence and still remove this invocation's container.
        result["error"] = f"{type(error).__name__}: {error}".replace(
            secret, "[redacted]"
        )
    finally:
        try:
            logs = container.logs()
            result["secret_absent_from_logs"] = secret not in logs
            result["known_heartbeat_lifecycle_defect_observed"] = has_lifecycle_error(
                logs
            )
            result["passed"] = (
                result["passed"]
                and result["secret_absent_from_logs"]
                and not result["known_heartbeat_lifecycle_defect_observed"]
            )
            if capture is not None:
                stopped = not container.inspect()["state"]["running"]
                full = capture.finish(timeout=5 if stopped else 0.2)
                result["full_log_capture"] = full
                result["secret_absent_from_logs"] &= full["secret_absent"]
                result["passed"] = (
                    result["passed"]
                    and result["secret_absent_from_logs"]
                    and full["complete"]
                    and not full["truncated"]
                    and not full["worker_error_flags"]
                )
            log_path = output / (result["name"] + ".log")
            log_path.write_text(logs.replace(secret, "[redacted]"))
            result["log"] = log_path.name
            result["log_sha256"] = digest(log_path)
            result["final_inspect"] = container.inspect()
        finally:
            try:
                container.close()
                result["removed"] = True
                result["ended_at"] = utc_now()
            finally:
                if capture is not None and not capture.finished:
                    capture.finish(timeout=1)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--expected-version", default="0.2.0a3")
    parser.add_argument(
        "--soak-duration",
        type=float,
        default=0,
        help="optional production contract soak in seconds, at most one request/sec; 0 disables",
    )
    args = parser.parse_args()
    if not __debug__:
        parser.error("assertions must be enabled")
    if not math.isfinite(args.soak_duration) or not 0 <= args.soak_duration <= 10_800:
        parser.error("soak duration must be finite and between 0 and 10800 seconds")
    if not args.image.startswith("sha256:") or len(args.image) != 71:
        parser.error("select an immutable sha256 image ID")
    if args.output.exists() and any(args.output.iterdir()):
        parser.error("output must be new or empty")
    args.output.mkdir(parents=True, exist_ok=True)
    for name in ("qualify_production.py", "qualify_container.py", "production_soak.py"):
        staged = args.output / name
        staged.write_bytes(Path(__file__).with_name(name).read_bytes())
        staged.chmod(0o444)
    secret = secrets.token_urlsafe(32)
    docker = Docker()
    manifest = {
        "started_at": utc_now(),
        "image_id": args.image,
        "expected_version": args.expected_version,
        "passed": False,
        "docker_endpoint": "unix:///var/run/docker.sock",
        "cleared_docker_selectors": list(SELECTORS),
        "harness_sha256": digest(__file__),
        "stable_docker_helper_sha256": digest(
            Path(__file__).with_name("qualify_container.py")
        ),
        "secret_transport": "generated in memory; Docker --env NAME only",
        "soak_duration_seconds": args.soak_duration,
        "soak_helper_sha256": digest(Path(__file__).with_name("production_soak.py")),
        "scenarios": [],
    }
    try:
        manifest["docker_server_version"] = docker.call(
            "info", "--format", "{{.ServerVersion}}"
        ).stdout.strip()
        manifest["image_inspect"] = docker.json(
            "image", "inspect", "--format", IMAGE_FORMAT, args.image
        )
        assert manifest["image_inspect"]["id"] == args.image
        assert manifest["image_inspect"]["entrypoint"] == [
            "/usr/bin/tini",
            "-s",
            "--",
            "bessemer",
        ], "qualified image must use its pinned init entrypoint"
        for missing in (False, True):
            manifest["scenarios"].append(
                run_scenario(
                    args.image,
                    args.output,
                    secret,
                    missing,
                    args.expected_version,
                    soak_duration=args.soak_duration if not missing else 0,
                )
            )
            atomic_json(args.output / "manifest.json", manifest)
        manifest["passed"] = all(
            item["passed"] and item["removed"] and item["secret_absent_from_logs"]
            for item in manifest["scenarios"]
        )
    except Exception as error:  # noqa: BLE001 -- Avoid raw Docker environment/config dumps on failure.
        manifest["error"] = f"{type(error).__name__}: {error}".replace(
            secret, "[redacted]"
        )
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
