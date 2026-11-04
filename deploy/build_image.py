#!/usr/bin/env python3
"""Build an image from a frozen, hashed source snapshot and record provenance."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
import tomllib
from pathlib import Path

REPOSITORY = Path(__file__).resolve().parents[1]
DOCKER = ["docker", "--host=unix:///var/run/docker.sock"]
SELECTORS = (
    "DOCKER_HOST",
    "DOCKER_CONTEXT",
    "DOCKER_TLS",
    "DOCKER_TLS_VERIFY",
    "DOCKER_CERT_PATH",
)
ROOT_INPUTS = (
    "Dockerfile",
    ".dockerignore",
    "Cargo.toml",
    "Cargo.lock",
    "build.rs",
    "pyproject.toml",
    "README.md",
    "deploy/requirements-installer.lock",
    "deploy/requirements-production.lock",
)
SOURCE_DIRECTORIES = ("src", "crates", "python", "examples")


def docker_environment() -> dict[str, str]:
    environment = os.environ.copy()
    for name in SELECTORS:
        environment.pop(name, None)
    return environment


def reject_source_symlinks(repository: Path, path: Path) -> None:
    current = repository
    for part in path.relative_to(repository).parts:
        current /= part
        if current.is_symlink():
            raise ValueError("image source cannot contain symbolic links")


def source_paths(repository: Path) -> list[Path]:
    paths = [repository / name for name in ROOT_INPUTS]
    for path in paths:
        reject_source_symlinks(repository, path)
    for directory in SOURCE_DIRECTORIES:
        source_directory = repository / directory
        reject_source_symlinks(repository, source_directory)
        for path in source_directory.rglob("*"):
            if any(
                part == ".env" or part.startswith(".env.")
                for part in path.relative_to(repository).parts
            ):
                continue
            if path.is_symlink():
                raise ValueError("image source cannot contain symbolic links")
            if (
                path.is_file()
                and not any(part in {"__pycache__", "target"} for part in path.parts)
                and path.suffix != ".pyc"
            ):
                paths.append(path)
    return sorted(paths, key=lambda path: path.relative_to(repository).as_posix())


def source_manifest(repository: Path, snapshot: Path | None = None) -> dict:
    files = []
    for path in source_paths(repository):
        reject_source_symlinks(repository, path)
        data = path.read_bytes()
        relative = path.relative_to(repository).as_posix()
        files.append(
            {
                "path": relative,
                "sha256": hashlib.sha256(data).hexdigest(),
                "bytes": len(data),
            }
        )
        if snapshot is not None:
            destination = snapshot / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(data)
    encoded = json.dumps(files, sort_keys=True, separators=(",", ":")).encode()
    return {"sha256": hashlib.sha256(encoded).hexdigest(), "files": files}


def run(arguments: list[str], *, environment: dict[str, str], **kwargs):
    return subprocess.run(arguments, env=environment, check=True, **kwargs)


def selected_image_metadata(image: str, environment: dict[str, str]) -> dict:
    result = run(
        DOCKER + ["image", "inspect", image],
        environment=environment,
        capture_output=True,
        text=True,
        timeout=30,
    )
    image_data = json.loads(result.stdout)[0]
    config = image_data["Config"]
    return {
        "id": image_data["Id"],
        "created": image_data["Created"],
        "os": image_data["Os"],
        "architecture": image_data["Architecture"],
        "size_bytes": image_data["Size"],
        "repo_digests": image_data.get("RepoDigests", []),
        "config": {
            key: config.get(key)
            for key in (
                "User",
                "Entrypoint",
                "Cmd",
                "StopSignal",
                "Healthcheck",
                "Labels",
            )
        },
    }


def inventory(image: str, environment: dict[str, str], output: Path) -> dict:
    restrictions = [
        "run",
        "--rm",
        "--network",
        "none",
        "--read-only",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges:true",
        "--memory",
        "128m",
        "--cpus",
        "0.5",
        "--pids-limit",
        "64",
    ]
    code = """import hashlib,importlib.metadata as m,json,os,platform,sys
import bessemer._native as native
from pathlib import Path
print(json.dumps({'python':platform.python_version(),'implementation':platform.python_implementation(),'uid':os.geteuid(),'gid':os.getegid(),'distributions':sorted([{'name':d.metadata['Name'],'version':d.version} for d in m.distributions()],key=lambda d:d['name'].lower()),'native_sha256':hashlib.sha256(Path(native.__file__).read_bytes()).hexdigest()}))
"""
    result = run(
        DOCKER + restrictions + ["--entrypoint", "python", image, "-c", code],
        environment=environment,
        capture_output=True,
        text=True,
        timeout=60,
    )
    python = json.loads(result.stdout)
    (output / "python-inventory.json").write_text(json.dumps(python, indent=2) + "\n")
    result = run(
        DOCKER
        + restrictions
        + [
            "--entrypoint",
            "dpkg-query",
            image,
            "-W",
            "-f=${binary:Package}\t${Version}\t${Architecture}\n",
        ],
        environment=environment,
        capture_output=True,
        text=True,
        timeout=60,
    )
    (output / "os-packages.tsv").write_text(result.stdout)
    return {
        "python": python,
        "os_package_count": len(result.stdout.splitlines()),
        "os_packages": "os-packages.tsv",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True)
    parser.add_argument(
        "--target", choices=("runtime", "production"), default="runtime"
    )
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    output = args.output.resolve()
    if output.exists() and any(output.iterdir()):
        parser.error("output must be new or empty")
    output.mkdir(parents=True, exist_ok=True)
    snapshot = output / "context"
    snapshot.mkdir(exist_ok=False)
    environment = docker_environment()
    manifest = {
        "schema_version": 1,
        "image_tag": args.image,
        "target": args.target,
        "platform": "linux/amd64",
        "started_at": time.time(),
        "status": "building",
        "builder_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
    }
    manifest_path = output / "manifest.json"
    try:
        manifest["source"] = source_manifest(REPOSITORY, snapshot)
        revision = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=REPOSITORY,
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
        manifest["git_revision"] = (
            revision.stdout.strip() if revision.returncode == 0 else "unknown"
        )
        dirty = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=REPOSITORY,
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
        manifest["git_dirty"] = bool(dirty.stdout) if dirty.returncode == 0 else None
        version = tomllib.loads((snapshot / "pyproject.toml").read_text())["project"][
            "version"
        ]
        arguments = DOCKER + [
            "build",
            "--platform",
            "linux/amd64",
            "--progress",
            "plain",
            "--target",
            args.target,
            "--tag",
            args.image,
            "--iidfile",
            str(output / "image-id.txt"),
            "--build-arg",
            f"VERSION={version}",
            "--build-arg",
            f"VCS_REF={manifest['git_revision']}",
            "--build-arg",
            f"SOURCE_SHA256={manifest['source']['sha256']}",
        ]
        certificate = os.environ.get("CODEX_PROXY_CERT")
        if certificate:
            arguments += ["--secret", f"id=proxy_ca,src={certificate}"]
        arguments.append(str(snapshot))
        manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
        with (output / "build.log").open("w") as log:
            run(
                arguments,
                environment=environment,
                stdout=log,
                stderr=subprocess.STDOUT,
                timeout=1800,
            )
        image_id = (output / "image-id.txt").read_text().strip()
        manifest["image"] = selected_image_metadata(image_id, environment)
        labels = manifest["image"]["config"]["Labels"] or {}
        if labels.get("org.bessemer.source.sha256") != manifest["source"]["sha256"]:
            raise ValueError("built image does not carry the frozen source digest")
        manifest["inventory"] = inventory(image_id, environment, output)
        manifest["current_source_matches"] = (
            source_manifest(REPOSITORY)["sha256"] == manifest["source"]["sha256"]
        )
        manifest["status"] = (
            "passed"
            if manifest["current_source_matches"]
            else "source_changed_after_snapshot"
        )
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        manifest["status"] = "failed"
        manifest["error_type"] = type(error).__name__
        print(
            "Container build qualification failed; inspect its manifest and build.log.",
            file=sys.stderr,
        )
    finally:
        manifest["finished_at"] = time.time()
        manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
    if manifest["status"] != "passed":
        return 1
    print(
        json.dumps(
            {
                "image": manifest["image"]["id"],
                "source_sha256": manifest["source"]["sha256"],
                "manifest": str(manifest_path),
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
