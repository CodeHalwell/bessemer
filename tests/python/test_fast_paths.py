"""Per-request fast paths must keep the general path's observable behaviour."""

from __future__ import annotations

import asyncio
import concurrent.futures
import enum
import importlib.util
import json
import socket
import threading
import time
import unittest

import test_native_integration
from bessemer import App, BackgroundTasks, Depends, Request
from bessemer.request import current_request
from bessemer.worker import Worker
from envelopes import payload

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

    def test_errors_in_routes_without_inputs_use_the_general_handling(self):
        from bessemer import HTTPException, JSONResponse

        app = App()

        @app.exception_handler(ValueError)
        async def handle_value_error(request, error):
            # The request is still current while the handler runs.
            current = current_request.get()
            return JSONResponse(
                {"handled": str(error), "same": current is request}, status_code=409
            )

        @app.get("/http")
        async def http_error():
            raise HTTPException(418, "teapot", headers={"x-reason": "tea"})

        @app.get("/handled")
        async def handled():
            raise ValueError("nope")

        @app.get("/unhandled")
        async def unhandled():
            raise KeyError("missing")

        app.build()
        ids = {
            route.declaration["path"]: index
            for index, route in enumerate(app._compiled)
        }
        with Worker(app) as worker:

            def call(path):
                return worker.submit(ids[path], {}).result(timeout=5)

            status, body, headers, kind = call("/http")
            self.assertEqual((status, body, kind), (418, {"detail": "teapot"}, "json"))
            self.assertIn(("x-reason", "tea"), list(headers))
            # Unhandled-error logging comes first, as for every route.
            with self.assertLogs("bessemer", "ERROR"):
                status, body = call("/handled")[:2]
            self.assertEqual(status, 409)
            if isinstance(body, bytes):
                body = json.loads(body)
            self.assertEqual(body, {"handled": "nope", "same": True})
            with self.assertLogs("bessemer", "ERROR") as logs:
                self.assertEqual(
                    call("/unhandled"), (500, {"detail": "Internal Server Error"})
                )
            self.assertIn("Unhandled KeyError in GET /unhandled", logs.output[0])
            # Nothing is left current once the call is answered.
            self.assertIsNone(current_request.get(None))

    def test_a_json_body_equal_to_stream_still_runs_cleanup(self):
        app = App()
        events = []

        async def resource():
            yield "resource"
            events.append("cleanup")

        @app.get("/text")
        async def text(background: BackgroundTasks, value: str = Depends(resource)):
            background.add_task(events.append, "background")
            return _Text("stream")

        app.build()
        with Worker(app) as worker:
            status, body = worker.submit(0, {}).result(timeout=5)[:2]
        self.assertEqual((status, body), (200, "stream"))
        self.assertEqual(events, ["background", "cleanup"])

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

    def test_bare_request_matches_one_built_from_minimal_metadata(self):
        app = object()
        built = Request({"path": "/a", "method": "POST"}, app=app)
        bare = Request._bare("POST", "/a", app)
        state = lambda request: {
            key: value for key, value in vars(request).items() if key != "state"
        }
        self.assertEqual(state(bare), state(built))
        self.assertIsNot(bare.state, Request._bare("POST", "/a", app).state)


class _Ticket:
    def __init__(self, identifier):
        self.id = identifier
        self.done = concurrent.futures.Future()

    def complete(self, task):
        try:
            self.done.set_result(task.result())
        except BaseException as error:  # noqa: BLE001 -- Hand back to the test.
            self.done.set_exception(error)

    def reject(self):
        self.done.set_exception(RuntimeError("rejected"))


class _Bridge:
    """Stand-in for the native bridge: a socket wake-up plus queued jobs."""

    def __init__(self):

        self._reader, self._writer = socket.socketpair()
        self._reader.setblocking(False)
        self._lock = threading.Lock()
        self._jobs = []
        self._cancels = []

    def fileno(self):
        return self._reader.fileno()

    def submit(self, identifier, route_id, arguments):
        ticket = _Ticket(identifier)
        with self._lock:
            self._jobs.append((ticket, route_id, arguments))
        self._writer.send(b"\0")
        return ticket

    def cancel(self, identifier):
        with self._lock:
            self._cancels.append(identifier)
        self._writer.send(b"\0")

    def drain(self):
        try:
            self._reader.recv(64)
        except BlockingIOError:
            pass
        with self._lock:
            jobs, self._jobs = self._jobs, []
            cancels, self._cancels = self._cancels, []
        return jobs, cancels

    def close(self):
        self._reader.close()
        self._writer.close()


class NativeDrainTests(unittest.TestCase):
    def setUp(self):

        app = App()
        self.factory_calls = 0
        self.release = None

        @app.get("/immediate")
        async def immediate():
            task = asyncio.current_task()
            return {"in_task": task is not None}

        @app.get("/suspends")
        async def suspends():
            await asyncio.sleep(0.01)
            return {"in_task": asyncio.current_task() is not None}

        @app.get("/fails")
        async def fails():
            raise ValueError("boom")

        @app.get("/blocked")
        async def blocked():
            self.release = asyncio.get_running_loop().create_future()
            await self.release
            return {"released": True}

        app.build()
        self.ids = {
            route.declaration["path"]: index
            for index, route in enumerate(app._compiled)
        }
        self.worker = Worker(app)
        self.worker.start()
        self.bridge = _Bridge()
        self.worker.attach_native(self.bridge)

    def tearDown(self):
        self.worker.close()

    def call(self, path, identifier=1):
        ticket = self.bridge.submit(identifier, self.ids[path], {})
        return payload(ticket.done.result(timeout=5))

    def test_handlers_run_in_a_task_whether_or_not_they_suspend(self):
        self.assertEqual(self.call("/immediate"), (200, {"in_task": True}))
        self.assertEqual(self.call("/suspends"), (200, {"in_task": True}))
        # Nothing is left tracked once both have answered.
        self.assertEqual(self.worker._native_invocations, {})

    def test_errors_in_an_eagerly_completed_handler_use_the_safe_boundary(self):
        with self.assertLogs("bessemer", "ERROR"):
            status, body = self.call("/fails")
        self.assertEqual((status, body), (500, {"detail": "Internal Server Error"}))

    def test_suspended_handler_can_be_cancelled_through_the_queue(self):

        ticket = self.bridge.submit(7, self.ids["/blocked"], {})
        deadline = time.monotonic() + 5
        while 7 not in self.worker._native_invocations:
            self.assertLess(time.monotonic(), deadline)
            time.sleep(0.005)
        self.bridge.cancel(7)
        with self.assertRaises(asyncio.CancelledError):
            ticket.done.result(timeout=5)
        self.assertEqual(self.worker._native_invocations, {})

    def test_an_application_task_factory_still_creates_every_task(self):

        created = []

        def factory(loop, coro, **kwargs):
            created.append(coro)
            return asyncio.Task(coro, loop=loop, **kwargs)

        installed = concurrent.futures.Future()
        self.worker._loop.call_soon_threadsafe(
            lambda: (
                self.worker._loop.set_task_factory(factory),
                installed.set_result(None),
            )
        )
        installed.result(timeout=5)
        self.assertEqual(self.call("/immediate"), (200, {"in_task": True}))
        self.assertEqual(len(created), 1)


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
