"""Native code under real thread parallelism.

On free-threaded CPython (3.13t/3.14t) these threads run Python and native
code at the same time; with the GIL they interleave. Either way the results
must be correct and the process must not crash.
"""

import json
import sys
import threading
import time
import unittest

from bessemer import App, _native
from bessemer.testclient import TestClient

FREE_THREADED = hasattr(sys, "_is_gil_enabled") and not sys._is_gil_enabled()


def run_threads(targets, seconds):
    stop = threading.Event()
    errors = []

    def guarded(target):
        try:
            while not stop.is_set():
                target()
        except BaseException as error:  # noqa: BLE001 - reported below
            errors.append(error)
            stop.set()

    threads = [threading.Thread(target=guarded, args=(target,)) for target in targets]
    for thread in threads:
        thread.start()
    time.sleep(seconds)
    stop.set()
    for thread in threads:
        thread.join(10)
    return errors


class FreeThreadingTests(unittest.TestCase):
    def test_the_gil_stays_disabled_on_free_threaded_builds(self):
        # The extension declares free-threading support, so importing it
        # must not re-enable the GIL.
        if hasattr(sys, "_is_gil_enabled"):
            import sysconfig

            if sysconfig.get_config_var("Py_GIL_DISABLED"):
                self.assertFalse(sys._is_gil_enabled())

    def test_encoding_containers_other_threads_mutate(self):
        shared = {"items": [{"id": index} for index in range(64)], "meta": {}}

        def mutate():
            items = shared["items"]
            items.append({"id": len(items), "name": "x" * 8})
            if len(items) > 128:
                del items[:64]
            shared["meta"][str(len(items))] = [1, 2.5, None, True]
            if len(shared["meta"]) > 64:
                shared["meta"] = {}
            shared["items"] = list(items)

        def encode():
            body = _native._encode_plain_json(shared, 1 << 22)
            # A snapshot is always complete, valid JSON.
            if body is not None:
                decoded = json.loads(body)
                self.assertEqual(set(decoded), {"items", "meta"})

        errors = run_threads([mutate] * 3 + [encode] * 5, 1.5)
        self.assertEqual(errors, [])

    def test_oversized_containers_are_refused_before_they_are_copied(self):
        import resource

        huge = [None] * 10_000_000
        mapping = dict.fromkeys(range(2_000_000))
        before = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        self.assertIsNone(_native._encode_plain_json(huge, 1024))
        self.assertIsNone(_native._encode_plain_json({"a": huge}, 1024))
        self.assertIsNone(_native._encode_plain_json(mapping, 1024))
        grown = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss - before
        # A snapshot of the list alone would be about 80 MB. ru_maxrss is in
        # KiB on Linux and in bytes on macOS.
        unit = 1 if sys.platform == "darwin" else 1024
        self.assertLess(grown * unit, 16 * 1024 * 1024)

    def test_parsing_on_many_threads(self):
        document = json.dumps(
            {
                "key": [
                    {"a": index, "b": "text", "c": [1.5, None]} for index in range(50)
                ]
            }
        ).encode()
        expected = json.loads(document)

        def parse():
            self.assertEqual(_native._parse_json(document), expected)

        self.assertEqual(run_threads([parse] * 8, 0.5), [])

    def test_sync_handlers_run_in_parallel_without_the_gil(self):
        app = App()

        @app.get("/spin")
        def spin(n: int):
            total = 0
            for value in range(n):
                total += value
            return {"total": total}

        with TestClient(app) as client:
            errors = []
            results = []

            def call():
                try:
                    response = client.get("/spin", params={"n": 20000})
                    results.append(response.json()["total"])
                except BaseException as error:  # noqa: BLE001
                    errors.append(error)

            threads = [threading.Thread(target=call) for _ in range(16)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(30)
        self.assertEqual(errors, [])
        self.assertEqual(results, [sum(range(20000))] * 16)


if __name__ == "__main__":
    unittest.main()
