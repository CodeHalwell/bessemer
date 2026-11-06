"""Reuse the concurrency reference routes and the multicore checksum unchanged."""

import hashlib
import importlib
import importlib.metadata
import os
import sys
import sysconfig
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from concurrency.app import app
from multicore.work import checksum

ITERATIONS = 50_000


@app.get("/mixed")
async def mixed():
    import asyncio

    first = checksum(ITERATIONS // 2)
    await asyncio.sleep(0.001)
    return {"checksum": first + checksum(ITERATIONS // 2)}


@app.get("/probe")
async def probe():
    framework = os.environ["BENCHMARK_FRAMEWORK"]
    package = importlib.import_module(
        "fastapi" if framework == "fastapi" else "bessemer"
    )
    root = Path(package.__file__).parent
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*.py")):
        digest.update(
            str(path.relative_to(root)).encode() + b"\0" + path.read_bytes() + b"\0"
        )
    result = {
        "framework": framework,
        "python": sys.version,
        "gil_enabled": getattr(sys, "_is_gil_enabled", lambda: True)(),
        "free_threaded_build": bool(sysconfig.get_config_var("Py_GIL_DISABLED")),
        "pid": os.getpid(),
        "package_file": package.__file__,
        "python_package_sha256": digest.hexdigest(),
        "versions": {
            name: importlib.metadata.version(name)
            for name in ("pydantic", "fastapi", "uvicorn")
        },
    }
    if framework == "bessemer":
        native = importlib.import_module("bessemer._native")
        result["native_file"] = native.__file__
        result["native_sha256"] = hashlib.sha256(
            Path(native.__file__).read_bytes()
        ).hexdigest()
    return result
