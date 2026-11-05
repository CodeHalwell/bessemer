#!/usr/bin/env python3
"""Fetch CPython's asyncio test suite for the running interpreter's version.

The interpreter usually ships without the ``test`` package, so this downloads
``Lib/test/__init__.py``, ``Lib/test/support/**`` and ``Lib/test/test_asyncio/**``
from the matching ``v{major}.{minor}.{micro}`` tag, plus every ``test.*``
module those files import and the ``Lib/test/certdata`` files they name.

The result lands in ``<cache>/v{X.Y.Z}/test/`` and is importable as ``test``
once ``<cache>/v{X.Y.Z}`` is first on ``sys.path``.

Two sources are tried, in order:

1. the codeload tarball of the tag (one request, has directory listings);
2. raw.githubusercontent.com, one file at a time, following imports from a
   list of seed modules (for networks that only allow raw file access).

The download is treated as untrusted data: nothing from it is executed here
(imports are found with ``ast``), members are filtered by path, size and type,
and everything is written into a fresh directory that replaces the cache entry
only once complete.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import io
import json
import os
import re
import shutil
import sys
import tarfile
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

REPO = "python/cpython"
DEFAULT_CACHE = Path(__file__).resolve().parent / ".cpython"
TARBALL_URL = "https://codeload.github.com/{repo}/tar.gz/refs/tags/{tag}"
RAW_URL = "https://raw.githubusercontent.com/{repo}/{tag}/{path}"

# test_asyncio modules the compat runner may load. Only the raw fallback needs
# these (it cannot list directories); the tarball path takes all of
# test_asyncio. Names missing from a given CPython version are ignored.
SEED_MODULES = (
    "utils",
    "functional",
    "test_buffered_proto",
    "test_context",
    "test_eager_task_factory",
    "test_events",
    "test_futures",
    "test_futures2",
    "test_locks",
    "test_pep492",
    "test_protocols",
    "test_queues",
    "test_runners",
    "test_sendfile",
    "test_server",
    "test_sock_lowlevel",
    "test_ssl",
    "test_staggered",
    "test_streams",
    "test_subprocess",
    "test_taskgroups",
    "test_tasks",
    "test_threads",
    "test_timeouts",
    "test_waitfor",
)

# Always extracted, whatever imports them.
ROOT_PREFIXES = ("test/support/", "test/test_asyncio/")
ROOT_FILES = ("test/__init__.py",)
# Data files the asyncio tests open: ssl certificates and keys from certdata,
# and helper scripts next to the tests (echo.py, ...) run as subprocesses.
CERTDATA_DIR = "test/certdata/"
CERT_NAME_RE = re.compile(r"""['"]([A-Za-z0-9_.-]+\.(?:pem|crt|key|der))['"]""")
SCRIPT_NAME_RE = re.compile(r"""['"]([A-Za-z0-9_-]+\.py)['"]""")

SAFE_PATH_RE = re.compile(r"^test/[A-Za-z0-9_./-]+$")
MAX_FILE_BYTES = 4 * 1024 * 1024
MAX_TARBALL_BYTES = 200 * 1024 * 1024
MANIFEST = "MANIFEST.json"
TIMEOUT = 60


def version_tag(version: tuple[int, int, int] | None = None) -> str:
    major, minor, micro = version or sys.version_info[:3]
    return f"v{major}.{minor}.{micro}"


def safe_relpath(path: str) -> bool:
    if not SAFE_PATH_RE.match(path):
        return False
    return all(part not in ("", ".", "..") for part in path.split("/"))


def wanted_root(path: str) -> bool:
    return path in ROOT_FILES or path.startswith(ROOT_PREFIXES)


def module_candidates(module: str) -> list[str]:
    base = module.replace(".", "/")
    return [f"{base}.py", f"{base}/__init__.py"]


def package_inits(path: str) -> list[str]:
    parts = path.split("/")[:-1]
    return ["/".join(parts[: i + 1]) + "/__init__.py" for i in range(len(parts))]


def imported_test_modules(path: str, source: bytes) -> set[str]:
    """Return every ``test.*`` module name ``source`` may import.

    ``from test.support import os_helper`` yields both ``test.support`` and
    ``test.support.os_helper``; callers keep whichever exists.
    """
    try:
        tree = ast.parse(source, filename=path)
    except (SyntaxError, ValueError):
        return set()
    package = path.removesuffix(".py").replace("/", ".")
    if not path.endswith("/__init__.py"):
        package = package.rpartition(".")[0]
    else:
        package = package.removesuffix(".__init__")

    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = package.split(".")
                if node.level > 1:
                    base = base[: -(node.level - 1)]
                module = ".".join(base + ([node.module] if node.module else []))
            else:
                module = node.module or ""
            found.add(module)
            found.update(f"{module}.{alias.name}" for alias in node.names)
    return {name for name in found if name == "test" or name.startswith("test.")}


def referenced_data(files: dict[str, bytes]) -> set[str]:
    """Paths of data files named by string literals in the fetched tests."""
    paths: set[str] = set()
    for path, data in files.items():
        if not path.endswith(".py"):
            continue
        text = data.decode("utf-8", "replace")
        paths.update(CERTDATA_DIR + name for name in CERT_NAME_RE.findall(text))
        if path.startswith("test/test_asyncio/"):
            paths.update(
                "test/test_asyncio/" + name for name in SCRIPT_NAME_RE.findall(text)
            )
    return {p for p in paths if p not in files}


def close_over_imports(files: dict[str, bytes], get) -> None:
    """Add to ``files`` every test module they import, using ``get(path)``.

    ``get`` returns the file's bytes, or None when it does not exist.
    """
    pending = list(files)
    seen_modules: set[str] = set()
    while pending:
        path = pending.pop()
        if not path.endswith(".py"):
            continue
        for module in imported_test_modules(path, files[path]):
            if module in seen_modules:
                continue
            seen_modules.add(module)
            for candidate in module_candidates(module):
                if candidate in files:
                    break
                data = get(candidate)
                if data is not None:
                    for extra in [*package_inits(candidate), candidate]:
                        if extra not in files:
                            blob = data if extra == candidate else get(extra)
                            if blob is not None:
                                files[extra] = blob
                                pending.append(extra)
                    break


def http_get(url: str, limit: int) -> bytes | None:
    request = urllib.request.Request(url, headers={"User-Agent": "bessemer-compat"})
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            data = response.read(limit + 1)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        raise
    if len(data) > limit:
        raise ValueError(f"{url}: response larger than {limit} bytes")
    return data


def from_tarball(tag: str) -> dict[str, bytes]:
    data = http_get(TARBALL_URL.format(repo=REPO, tag=tag), MAX_TARBALL_BYTES)
    if data is None:
        raise LookupError(f"no tarball for {tag}")
    available: dict[str, bytes] = {}
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
        for member in archive:
            # Names look like "cpython-3.13.1/Lib/test/...": drop the top dir.
            _, _, rest = member.name.partition("/")
            if not rest.startswith("Lib/test/") or not member.isreg():
                continue
            path = rest.removeprefix("Lib/")
            if member.size > MAX_FILE_BYTES or not safe_relpath(path):
                continue
            if path.endswith(".py") or path.startswith(CERTDATA_DIR):
                handle = archive.extractfile(member)
                if handle is not None:
                    available[path] = handle.read()

    files = {path: blob for path, blob in available.items() if wanted_root(path)}
    if "test/test_asyncio/__init__.py" not in files:
        raise LookupError(f"{tag} tarball has no Lib/test/test_asyncio")
    close_over_imports(files, available.get)
    for extra in referenced_data(files):
        if extra in available:
            files[extra] = available[extra]
    return files


def from_raw(tag: str, seeds: tuple[str, ...]) -> dict[str, bytes]:
    cache: dict[str, bytes | None] = {}

    def get(path: str) -> bytes | None:
        if path not in cache:
            url = RAW_URL.format(repo=REPO, tag=tag, path="Lib/" + path)
            cache[path] = http_get(url, MAX_FILE_BYTES) if safe_relpath(path) else None
        return cache[path]

    files: dict[str, bytes] = {}
    roots = [*ROOT_FILES, "test/support/__init__.py", "test/test_asyncio/__init__.py"]
    roots += [f"test/test_asyncio/{name}.py" for name in seeds]
    for path in roots:
        data = get(path)
        if data is not None:
            files[path] = data
    if "test/test_asyncio/__init__.py" not in files:
        raise LookupError(f"{tag} has no Lib/test/test_asyncio")
    close_over_imports(files, get)
    for extra in referenced_data(files):
        data = get(extra)
        if data is not None:
            files[extra] = data
    return files


def write_tree(dest: Path, tag: str, source: str, files: dict[str, bytes]) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{tag}-", dir=dest.parent))
    try:
        for path, data in sorted(files.items()):
            target = staging / path
            target.parent.mkdir(parents=True, exist_ok=True)
            with open(target, "xb") as handle:
                handle.write(data)
        manifest = {
            "tag": tag,
            "source": source,
            "files": {
                p: hashlib.sha256(d).hexdigest() for p, d in sorted(files.items())
            },
        }
        (staging / MANIFEST).write_text(json.dumps(manifest, indent=1) + "\n")
        if dest.exists():
            shutil.rmtree(dest)
        os.replace(staging, dest)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def has_modules(root: Path, modules: tuple[str, ...]) -> bool:
    """True when the cached tree is complete for the requested modules.

    A raw-mode cache only holds the modules it was seeded with, so a module
    that is in neither the tree nor the tarball listing forces a refetch.
    """
    manifest_path = root / MANIFEST
    if not manifest_path.is_file():
        return False
    manifest = json.loads(manifest_path.read_text())
    if manifest.get("source") == "tarball":
        return True
    return all(f"test/test_asyncio/{name}.py" in manifest["files"] for name in modules)


def ensure_tests(
    cache: Path = DEFAULT_CACHE,
    modules: tuple[str, ...] = SEED_MODULES,
    *,
    force: bool = False,
    quiet: bool = False,
) -> Path:
    """Return a directory to put first on ``sys.path`` to import ``test``."""
    tag = version_tag()
    root = cache / tag
    if not force and has_modules(root, modules):
        return root

    def say(message: str) -> None:
        if not quiet:
            print(message, file=sys.stderr)

    try:
        say(f"fetching CPython {tag} tests (tarball)")
        files, source = from_tarball(tag), "tarball"
    except (OSError, LookupError, ValueError, tarfile.TarError) as exc:
        say(
            f"tarball unavailable ({exc}); fetching files from raw.githubusercontent.com"
        )
        files, source = (
            from_raw(tag, tuple(dict.fromkeys(SEED_MODULES + modules))),
            "raw",
        )
    write_tree(root, tag, source, files)
    say(f"wrote {len(files)} files to {root}")
    return root


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--cache", type=Path, default=DEFAULT_CACHE)
    parser.add_argument("--force", action="store_true", help="refetch even if cached")
    args = parser.parse_args()
    print(ensure_tests(args.cache, force=args.force))
    return 0


if __name__ == "__main__":
    sys.exit(main())
