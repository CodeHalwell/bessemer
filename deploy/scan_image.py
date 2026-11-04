#!/usr/bin/env python3
"""Record an OS vulnerability scan of an immutable local image without socket access."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

TRIVY_IMAGE = "aquasec/trivy@sha256:790fd66d1697395d8693d1acaa1ea702406a20f109b6fc7bac8db623eca6c06c"
DOCKER = ["docker", "--host=unix:///var/run/docker.sock"]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--cache", type=Path, required=True)
    args = parser.parse_args()
    output, cache = args.output.resolve(), args.cache.resolve()
    output.mkdir(parents=True, exist_ok=False)
    cache.mkdir(parents=True, exist_ok=True)
    environment = os.environ.copy()
    for name in (
        "DOCKER_HOST",
        "DOCKER_CONTEXT",
        "DOCKER_TLS",
        "DOCKER_TLS_VERIFY",
        "DOCKER_CERT_PATH",
    ):
        environment.pop(name, None)
    manifest = {
        "schema_version": 1,
        "status": "running",
        "started_at": time.time(),
        "scanner_image": TRIVY_IMAGE,
        "scope": "installed OS packages; Python distributions audited separately",
        "scanner_version": "0.76.1",
    }
    try:
        image = subprocess.run(
            DOCKER + ["image", "inspect", "--format", "{{.Id}}", args.image],
            env=environment,
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        ).stdout.strip()
        manifest["image_id"] = image
        archive = output / "image.tar"
        subprocess.run(
            DOCKER + ["image", "save", "--output", str(archive), image],
            env=environment,
            check=True,
            timeout=120,
        )
        digest = hashlib.sha256()
        with archive.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
        manifest["archive_sha256"] = digest.hexdigest()
        reports = output / "reports"
        reports.mkdir()
        command = DOCKER + [
            "run",
            "--rm",
            "--pull",
            "never",
            "--user",
            f"{os.getuid()}:{os.getgid()}",
            "--read-only",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges:true",
            "--cpus",
            "1",
            "--memory",
            "1g",
            "--pids-limit",
            "128",
            "--tmpfs",
            "/tmp:rw,noexec,nosuid,size=256m",
            "--mount",
            f"type=bind,src={reports},dst=/scan-output",
            "--mount",
            f"type=bind,src={archive},dst=/image.tar,readonly",
            "--mount",
            f"type=bind,src={cache},dst=/cache",
        ]
        certificate = os.environ.get("CODEX_PROXY_CERT")
        if certificate:
            command += [
                "--mount",
                f"type=bind,src={certificate},dst=/run/proxy-ca.pem,readonly",
                "--env",
                "SSL_CERT_FILE=/run/proxy-ca.pem",
            ]
        command += [
            TRIVY_IMAGE,
            "image",
            "--cache-dir",
            "/cache",
            "--db-repository",
            "ghcr.io/aquasecurity/trivy-db:2",
            "--scanners",
            "vuln",
            "--pkg-types",
            "os",
            "--skip-version-check",
            "--no-progress",
            "--timeout",
            "5m",
            "--format",
            "json",
            "--output",
            "/scan-output/report.json",
            "--input",
            "/image.tar",
        ]
        with (output / "scanner.log").open("w") as log:
            subprocess.run(
                command,
                env=environment,
                check=True,
                stdout=log,
                stderr=subprocess.STDOUT,
                timeout=360,
            )
        report = json.loads((reports / "report.json").read_text())
        findings = [
            finding
            for result in report.get("Results", [])
            for finding in result.get("Vulnerabilities", [])
        ]
        severity = {
            name: sum(finding.get("Severity") == name for finding in findings)
            for name in ("CRITICAL", "HIGH", "MEDIUM", "LOW", "UNKNOWN")
        }
        fixable = {
            name: sum(
                finding.get("Severity") == name and bool(finding.get("FixedVersion"))
                for finding in findings
            )
            for name in severity
        }
        manifest.update(
            {
                "status": "completed_with_findings" if findings else "clean",
                "os": report.get("Metadata", {}).get("OS"),
                "severity_counts": severity,
                "fixable_severity_counts": fixable,
                "report": "reports/report.json",
            }
        )
        metadata = cache / "db" / "metadata.json"
        if metadata.is_file():
            manifest["database_metadata"] = json.loads(metadata.read_text())
        database = cache / "db" / "trivy.db"
        if database.is_file():
            digest = hashlib.sha256()
            with database.open("rb") as stream:
                for block in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(block)
            manifest["database_sha256"] = digest.hexdigest()
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        manifest["status"] = "failed"
        manifest["error_type"] = type(error).__name__
        print(
            "Image scan failed; inspect scanner.log and manifest.json.", file=sys.stderr
        )
    finally:
        manifest["finished_at"] = time.time()
        (output / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(
        json.dumps(
            {
                key: manifest.get(key)
                for key in (
                    "status",
                    "image_id",
                    "severity_counts",
                    "fixable_severity_counts",
                )
            }
        )
    )
    return 1 if manifest["status"] == "failed" else 0


if __name__ == "__main__":
    raise SystemExit(main())
