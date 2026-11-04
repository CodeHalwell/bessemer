"""Per-request fast paths must keep the general path's observable behaviour."""

from __future__ import annotations

import concurrent.futures
import enum
import importlib.util
import json
import unittest

import test_native_integration
from bessemer import App, Depends, Request
from bessemer.request import current_request
from bessemer.worker import Worker

NATIVE = importlib.util.find_spec("bessemer._native") is not None


class _Text(str):
    pass


class _Mapping(dict):
    pass


class _Number(enum.IntEnum):
    ONE = 1


def _nested(depth):
    value = []
    for _ in range(depth):
        value = [value]
    return value


@unittest.skipUnless(NATIVE, "build the native extension first")
class PlainJSONEncoderTests(unittest.TestCase):
    def setUp(self):
        from bessemer._native import _encode_plain_json

        self.encode = _encode_plain_json

    def test_exact_values_are_encoded_with_sorted_keys(self):
        value = {
            "z": [1, -2, 2**63, 1.5, None, True, False],
            "a": {"nested": 'caf\N{LATIN SMALL LETTER E WITH ACUTE}\n"\\'},
            "m": [],
            "k": {},
        }
        encoded = self.encode(value, 4096)
        self.assertIsInstance(encoded, bytes)
        self.assertEqual(json.loads(encoded), value)
        # The general path builds a key-sorted serde_json map; keep that order.
        self.assertEqual(list(json.loads(encoded)), ["a", "k", "m", "z"])
        self.assertEqual(self.encode("text", 16), b'"text"')
        self.assertEqual(self.encode(None, 16), b"null")

    def test_values_needing_normalization_or_errors_use_the_general_path(self):
        for value in (
            (1, 2),
            {"key": (1,)},
            _Mapping(a=1),
            [_Text("x")],
            _Number.ONE,
            float("nan"),
            [float("inf")],
            2**64,
            -(2**63) - 1,
            {1: "non-string key"},
            {_Text("k"): 1},
            "\ud800",
            {"\ud800": 1},
            _nested(66),
            object(),
        ):
            with self.subTest(value=repr(value)[:40]):
                self.assertIsNone(self.encode(value, 4096))

    def test_byte_limit_is_exact_and_falls_back_when_exceeded(self):
        value = {"key": "x" * 10}
        size = len(json.dumps(value, separators=(",", ":")))
        self.assertIsNotNone(self.encode(value, size))
        self.assertIsNone(self.encode(value, size - 1))
        self.assertIsNotNone(self.encode(_nested(64), 4096))
        # Oversized strings and keys are rejected before any copy is made.
        large = "\N{LATIN SMALL LETTER E WITH ACUTE}" * 100_000
        self.assertIsNone(self.encode(large, 1024))
        self.assertIsNone(self.encode({large: 1}, 1024))
        self.assertIsNone(self.encode(["x" * 1023], 1024))


class WorkerFastPathTests(unittest.TestCase):
    def test_routes_without_inputs_and_with_inputs_or_dependencies(self):
        app = App()

        async def dependency():
            return "dependency"

        @app.get("/plain")
        async def plain():
            request = current_request.get()
            return {
                "path": request.path,
                "headers": len(request.headers),
                "query": dict(request.query_params),
            }

        @app.get("/items/{item_id}")
        async def item(item_id: int, flag: bool = False):
            return {"id": item_id, "flag": flag}

        @app.get("/dependency")
        async def with_dependency(value: str = Depends(dependency)):
            return {"value": value}

        @app.get("/tuple")
        async def tuple_result():
            return {"items": (1, 2)}

        app.build()
        ids = {
            route.declaration["path"]: index
            for index, route in enumerate(app._compiled)
        }
        with Worker(app) as worker:

            def call(path, arguments):
                return worker.submit(ids[path], arguments).result(timeout=5)

            def payload(result):
                # Exact values are encoded natively; source-only runs keep the
                # general (status, value) envelope.
                if len(result) == 4:
                    self.assertEqual(result[2:], ((), "json_bytes"))
                    return result[0], json.loads(result[1])
                return result

            self.assertEqual(
                payload(call("/plain", {})),
                (200, {"path": "/plain", "headers": 0, "query": {}}),
            )
            arguments = {"item_id": 4}
            self.assertEqual(
                payload(call("/items/{item_id}", arguments)),
                (200, {"id": 4, "flag": False}),
            )
            # The caller's dict is not mutated; the default is still filled in.
            self.assertEqual(arguments, {"item_id": 4})
            self.assertEqual(
                payload(call("/dependency", {})), (200, {"value": "dependency"})
            )
            # Tuples need normalization and keep the original envelope.
            self.assertEqual(call("/tuple", {}), (200, {"items": [1, 2]}))

    def test_request_without_metadata_has_independent_state(self):
        first = Request({"path": "/a", "method": "GET"})
        second = Request({"path": "/b", "method": "GET"})
        self.assertEqual(len(first.headers), 0)
        self.assertEqual(dict(first.query_params), {})
        self.assertEqual(dict(first.path_params), {})
        self.assertIsNone(first.request_id)
        first.state.value = 1
        self.assertFalse(hasattr(second.state, "value"))
        with self.assertRaises(TypeError):
            first.query_params["x"] = "y"  # type: ignore[index]
        populated = Request(
            {
                "path": "/c",
                "query_string": "a=1",
                "headers": [("X-Request-Id", "abc")],
                "path_params": {"id": "7"},
            }
        )
        self.assertEqual(dict(populated.query_params), {"a": "1"})
        self.assertEqual(populated.request_id, "abc")
        self.assertEqual(dict(populated.path_params), {"id": "7"})


FAST_PATH_ROUTES = """

class _Subclass(dict):
    pass

PAYLOAD = {"z": [1, 2.5, None, True], "a": {"text": "caf\\u00e9\\n"}, "n": -3}

@app.get("/plain-json")
async def plain_json():
    return PAYLOAD

@app.get("/normalized-json")
async def normalized_json():
    return _Subclass(PAYLOAD)

@app.get("/plain-created", status_code=201)
async def plain_created():
    return {"created": True}

import contextvars
_REQUEST_LOCAL = contextvars.ContextVar("request_local", default="unset")

@app.get("/context")
async def context_isolation():
    previous = _REQUEST_LOCAL.get()
    _REQUEST_LOCAL.set("leaked")
    await asyncio.sleep(0)
    return {"previous": previous}

@app.get("/concurrent/{value}")
async def concurrent(value: int):
    await asyncio.sleep(0.005)
    return {"value": value}
"""


@unittest.skipUnless(NATIVE, "build the native extension first")
class FastPathHTTPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = test_native_integration.Server(
            application=test_native_integration.APPLICATION + FAST_PATH_ROUTES
        )

    @classmethod
    def tearDownClass(cls):
        cls.server.close()

    def test_plain_and_normalized_results_produce_identical_responses(self):
        plain = self.server.request("GET", "/plain-json")
        normalized = self.server.request("GET", "/normalized-json")
        self.assertEqual(plain[0], 200)
        self.assertEqual(plain[2], normalized[2])
        self.assertEqual(plain[1]["content-type"], "application/json")
        self.assertEqual(plain[1]["content-type"], normalized[1]["content-type"])
        self.assertEqual(plain[1]["content-length"], normalized[1]["content-length"])
        status, _, body = self.server.request("GET", "/plain-created")
        self.assertEqual((status, json.loads(body)), (201, {"created": True}))
        status, _, body = self.server.request("HEAD", "/plain-json")
        self.assertEqual((status, body), (200, b""))

    def test_native_queue_starts_each_request_in_a_fresh_context(self):
        for _ in range(3):
            status, _, body = self.server.request("GET", "/context")
            self.assertEqual((status, json.loads(body)), (200, {"previous": "unset"}))

    def test_native_queue_serves_concurrent_requests_correctly(self):
        # The fixture admits one request at a time; a separate server allows
        # real concurrency through the batched queue.
        server = test_native_integration.Server(
            application=test_native_integration.APPLICATION + FAST_PATH_ROUTES,
            extra_args=("--max-in-flight", "64"),
        )
        try:
            with concurrent.futures.ThreadPoolExecutor(16) as pool:
                results = list(
                    pool.map(
                        lambda value: server.request("GET", f"/concurrent/{value}"),
                        range(200),
                    )
                )
        finally:
            server.close()
        for value, (status, _, body) in enumerate(results):
            self.assertEqual((status, json.loads(body)), (200, {"value": value}))

    def test_many_sequential_requests_reuse_native_threads(self):
        # Repeated native-thread entry into Python must stay stable.
        for _ in range(200):
            self.assertEqual(self.server.request("GET", "/plain-created")[0], 201)


if __name__ == "__main__":
    unittest.main()
