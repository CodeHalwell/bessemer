"""Guarded installed-wheel WebSocket/AG-UI regression check for PR candidates."""

import hashlib
import os
import sys
import unittest
from pathlib import Path


def main():
    os.environ["BESSEMER_REQUIRE_AG_UI"] = "1"
    import bessemer._native
    import bessemer.worker

    expected_gil = sys.argv[1] == "gil"
    worker = Path(bessemer.worker.__file__)
    assert "site-packages" in str(worker), worker
    assert worker.read_bytes() == Path("python/bessemer/worker.py").read_bytes()
    worker_hash = hashlib.sha256(worker.read_bytes()).hexdigest()
    adapter_hash = hashlib.sha256(
        Path("examples/ag_ui/adapter.py").read_bytes()
    ).hexdigest()
    assert (
        worker_hash
        == "b4ee6cd8807e303c4d70bf8ed4003814380726fdb66b6dac3c103b7e132acb8e"
    )
    assert (
        adapter_hash
        == "1289f4efcf663e7f9f17d28cf518d0d51f8d7520810c86f60bf157b89d7f8c25"
    )
    sys.path.insert(0, str(Path.cwd()))
    print("python:", sys.version, flush=True)
    print("installed_worker:", worker, flush=True)
    print("worker_sha256:", worker_hash, flush=True)
    print("adapter_sha256:", adapter_hash, flush=True)
    print("native_file:", bessemer._native.__file__, flush=True)
    print(
        "native_sha256:",
        hashlib.sha256(Path(bessemer._native.__file__).read_bytes()).hexdigest(),
        flush=True,
    )
    suite = unittest.TestSuite()
    for pattern in ("test_websocket_lanes.py", "test_ag_ui.py"):
        suite.addTests(
            unittest.defaultTestLoader.discover("tests/python", pattern=pattern)
        )
    assert getattr(sys, "_is_gil_enabled", lambda: True)() is expected_gil
    print("actual_gil_enabled:", expected_gil, flush=True)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    assert getattr(sys, "_is_gil_enabled", lambda: True)() is expected_gil
    assert result.testsRun == 24, result.testsRun
    assert not result.skipped, result.skipped
    return not result.wasSuccessful()


if __name__ == "__main__":
    raise SystemExit(main())
