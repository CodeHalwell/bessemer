#!/usr/bin/env python3
"""Check that every version declaration agrees and extract release notes.

Used by the release workflow and by the test suite, so a version bump that
misses a file fails before anything is tagged.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# PEP 440 subset Bessemer releases use: X.Y.Z with an optional aN/bN/rcN suffix.
PEP440 = re.compile(r"^(\d+)\.(\d+)\.(\d+)(?:(a|b|rc)(\d+))?$")
CARGO_PRE = {"a": "alpha", "b": "beta", "rc": "rc"}


def cargo_version(version: str) -> str:
    """Map a PEP 440 release version to its SemVer spelling for Cargo."""
    match = PEP440.fullmatch(version)
    if match is None:
        raise ValueError(f"unsupported release version {version!r}")
    major, minor, patch, pre, number = match.groups()
    base = f"{major}.{minor}.{patch}"
    return f"{base}-{CARGO_PRE[pre]}.{number}" if pre else base


def is_prerelease(version: str) -> bool:
    match = PEP440.fullmatch(version)
    if match is None:
        raise ValueError(f"unsupported release version {version!r}")
    return match.group(4) is not None


def _search(path: str, pattern: str) -> str:
    text = (ROOT / path).read_text(encoding="utf-8")
    match = re.search(pattern, text, re.MULTILINE)
    if match is None:
        raise ValueError(f"no version declaration found in {path}")
    return match.group(1)


def declared_versions() -> dict[str, str]:
    """Return each file's declared version, normalised to PEP 440 where needed."""
    return {
        "pyproject.toml": _search("pyproject.toml", r'^version = "([^"]+)"'),
        "python/bessemer/__init__.py": _search(
            "python/bessemer/__init__.py", r'^__version__ = "([^"]+)"'
        ),
        "Dockerfile": _search("Dockerfile", r"^ARG VERSION=(\S+)"),
        "tests/deploy/qualify_production.py": _search(
            "tests/deploy/qualify_production.py",
            r'"--expected-version", default="([^"]+)"',
        ),
    }


def check(tag: str | None = None) -> str:
    versions = declared_versions()
    distinct = set(versions.values())
    if len(distinct) != 1:
        details = ", ".join(f"{path}={value}" for path, value in versions.items())
        raise ValueError(f"version declarations disagree: {details}")
    version = distinct.pop()
    expected_cargo = cargo_version(version)
    actual_cargo = _search("Cargo.toml", r'^version = "([^"]+)"')
    if actual_cargo != expected_cargo:
        raise ValueError(f"Cargo.toml has {actual_cargo}, expected {expected_cargo}")
    if tag is not None and tag != f"v{version}":
        raise ValueError(f"tag {tag!r} does not match package version v{version}")
    return version


def release_notes(version: str) -> str:
    """Return the CHANGELOG section for one version, without its heading."""
    text = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    heading = re.compile(rf"^## \[{re.escape(version)}\][^\n]*\n", re.MULTILINE)
    match = heading.search(text)
    if match is None:
        raise ValueError(f"CHANGELOG.md has no section for {version}")
    following = re.compile(r"^## \[", re.MULTILINE).search(text, match.end())
    end = following.start() if following else len(text)
    notes = text[match.end() : end].strip()
    if not notes:
        raise ValueError(f"CHANGELOG.md section for {version} is empty")
    return notes + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tag", help="git tag that must match, such as v1.0.0")
    parser.add_argument("--github-output", type=Path, help="append outputs for Actions")
    parser.add_argument(
        "--notes", type=Path, help="write this version's changelog section"
    )
    arguments = parser.parse_args(argv)
    try:
        version = check(arguments.tag)
        notes = release_notes(version)
    except ValueError as error:
        print(f"release check failed: {error}", file=sys.stderr)
        return 1
    if arguments.github_output:
        with arguments.github_output.open("a", encoding="utf-8") as output:
            output.write(f"version={version}\n")
            output.write(
                f"prerelease={'true' if is_prerelease(version) else 'false'}\n"
            )
    if arguments.notes:
        arguments.notes.write_text(notes, encoding="utf-8")
    print(version)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
