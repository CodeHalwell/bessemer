"""The native invoker runs lean routes without the worker's Python code.

Everything a handler or client can observe must match the Python path: the
task and context each handler runs in, ``current_request``, exception
handlers, cancellation, validation errors and the encoded responses.
"""

from __future__ import annotations

import asyncio
import importlib.util
import inspect
import threading
import unittest

from bessemer import (
    App,
    HTTPException,
    JSONResponse,
    Query,
    StreamingResponse,
)
from bessemer.background import BackgroundTask
from bessemer.request import current_request
from bessemer.worker import Worker

NATIVE = importlib.util.find_spec("bessemer._native") is not None
PYDANTIC = importlib.util.find_spec("pydantic") is not None


def _native_routes(app):
    """The native invoker's route specs of a worker that is never started."""
    worker = Worker(app)
    try:
        return worker._native_routes
    finally:
        for lane in worker._lanes:
            lane.loop.close()
        worker._executor.shutdown()


def _native() -> bool:
    """Whether the running handler was started by the native invoker."""
    return type(asyncio.current_task().get_coro()).__name__ == "Call"


def build_app(events: list) -> App:
    app = App()

    @app.get("/items/{item_id}")
    async def item(item_id: int, q: str = "x"):
        request = current_request.get()
        request.state.seen = True
        return {
            "id": item_id,
            "q": q,
            "native": _native(),
            "path": request.path,
            "fresh_state": vars(request.state) == {"seen": True},
        }

    @app.get("/bounded")
    async def bounded(
        n: int = Query(1, ge=1, lt=10),
        ratio: float = Query(0.5, gt=0, le=1.5),
        name: str = Query("ab", min_length=2, max_length=3),
    ):
        return {"n": n, "ratio": ratio, "name": name, "native": _native()}

    @app.get("/suspends")
    async def suspends():
        await asyncio.sleep(0)
        await asyncio.sleep(0.001)
        coroutine = asyncio.current_task().get_coro()
        return {
            "native": _native(),
            "current": current_request.get() is not None,
            # As libraries such as anyio inspect a task's coroutine.
            "state": inspect.getcoroutinestate(coroutine),
            "name": coroutine.__qualname__,
        }

    @app.get("/fails")
    async def fails():
        raise ValueError("boom")

    @app.get("/lookup")
    async def lookup_route():
        raise KeyError("missing")

    @app.get("/http")
    async def http():
        raise HTTPException(404, "nope", headers={"x-reason": "test"})

    @app.get("/exits")
    async def exits():
        raise SystemExit(3)

    @app.get("/background")
    async def background():
        return JSONResponse(
            {"ok": True}, background=BackgroundTask(events.append, "background ran")
        )

    @app.get("/stream")
    async def stream():
        async def body():
            yield b"a"
            yield b"b"

        return StreamingResponse(body(), media_type="text/plain")

    if PYDANTIC:
        from pydantic import BaseModel

        class Item(BaseModel):
            id: int
            name: str

        @app.get("/model", response_model=Item)
        async def model():
            return Item(id=1, name="a")

        @app.get("/model-dict", response_model=Item)
        async def model_dict():
            return {"id": 2, "name": "b", "extra": True}

    @app.get("/tuple")
    async def as_tuple():
        return (1, 2)

    @app.get("/empty", status_code=204)
    async def empty():
        return None

    def returns_future():
        # Marked as a coroutine function, but returns another awaitable, and
        # runs code when called: inside the task, with the request current.
        future = asyncio.get_running_loop().create_future()
        future.set_result(
            {"future": True, "current": current_request.get(None) is not None}
        )
        return future

    app.get("/future")(inspect.markcoroutinefunction(returns_future))

    class NoThrow:
        """An await iterator without ``throw``, which ``await`` allows."""

        def __init__(self, iterator):
            self._iterator = iterator

        def __iter__(self):
            return self

        def __next__(self):
            return next(self._iterator)

        def send(self, value):
            return self._iterator.send(value)

    class FailsLater:
        def __await__(self):
            loop = asyncio.get_running_loop()
            future = loop.create_future()
            loop.call_soon(future.set_exception, HTTPException(418, "late"))
            return NoThrow(future.__await__())

    app.get("/fails-later")(inspect.markcoroutinefunction(lambda: FailsLater()))

    def raises_on_call():
        raise HTTPException(418, "teapot")

    app.get("/raises-on-call")(inspect.markcoroutinefunction(raises_on_call))

    @app.get("/blocked")
    async def blocked():
        events.append("blocked started")
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            events.append("blocked cancelled")
            raise
        return {"finished": True}

    return app


@unittest.skipUnless(NATIVE, "build the native extension first")
class NativeInvokerLifetimeTests(unittest.TestCase):
    def test_a_finished_client_leaves_nothing_alive(self):
        import gc
        import weakref

        from bessemer.testclient import TestClient

        app = build_app([])
        with TestClient(app) as client:
            self.assertEqual(client.get("/suspends").status_code, 200)
            self.assertEqual(client.get("/items/1").status_code, 200)
        reference = weakref.ref(app)
        del app, client
        gc.collect()
        self.assertIsNone(reference())


class NativeRouteSelectionTests(unittest.TestCase):
    def test_only_routes_without_python_work_are_native(self):
        app = build_app([])

        if PYDANTIC:  # Regular expressions need the optional dependency.

            @app.get("/pattern")
            async def pattern(code: str = Query("ab", pattern="^[a-z]+$")):
                return {"code": code}

        @app.get("/sync")
        def sync():
            return {}

        app.build()
        routes = _native_routes(app)
        native = {
            route.declaration["path"]
            for route, spec in zip(app._compiled, routes, strict=True)
            if spec is not None
        }
        self.assertIn("/items/{item_id}", native)
        self.assertIn("/bounded", native)
        if PYDANTIC:
            self.assertIn("/model", native)
        if PYDANTIC:
            # A regular expression is still checked by Python.
            self.assertNotIn("/pattern", native)
        self.assertNotIn("/sync", native)

    def test_asgi_middleware_keeps_every_route_on_the_python_path(self):
        app = build_app([])

        class Passthrough:
            def __init__(self, app):
                self.app = app

            async def __call__(self, scope, receive, send):  # pragma: no cover
                await self.app(scope, receive, send)

        app.add_middleware(Passthrough)
        app.build()
        self.assertEqual(set(_native_routes(app)), {None})

    def test_exception_handlers_keep_every_route_on_the_python_path(self):
        # Handlers receive the full request, so its metadata is always sent.
        app = build_app([])

        @app.exception_handler(LookupError)
        async def lookup(request, error):
            return JSONResponse({"handled": True}, status_code=418)  # pragma: no cover

        app.build()
        self.assertEqual(set(_native_routes(app)), {None})


@unittest.skipUnless(NATIVE, "build the native extension first")
class NativeInvokerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from bessemer.testclient import TestClient

        cls.events = []
        cls.client = TestClient(build_app(cls.events))
        cls.client.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.client.__exit__(None, None, None)

    def setUp(self):
        self.events.clear()

    def test_scalar_inputs_reach_the_handler_in_its_own_task_and_request(self):
        for _ in range(2):
            response = self.client.get("/items/7", params={"q": "abc"})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(
                response.json(),
                {
                    "id": 7,
                    "q": "abc",
                    "native": True,
                    "path": "/items/{item_id}",
                    "fresh_state": True,
                },
            )
        self.assertEqual(self.client.get("/items/8").json()["q"], "x")
        response = self.client.get("/items/x")
        self.assertEqual(response.status_code, 422)

    def test_bounds_are_checked_natively_and_reported_by_python(self):
        response = self.client.get(
            "/bounded", params={"n": "9", "ratio": "1.5", "name": "abc"}
        )
        self.assertEqual(
            response.json(),
            {"n": 9, "ratio": 1.5, "name": "abc", "native": True},
        )
        self.assertTrue(self.client.get("/bounded").json()["native"])
        for params, location in (
            ({"n": "10"}, "n"),
            ({"n": "0"}, "n"),
            ({"ratio": "0"}, "ratio"),
            ({"name": "a"}, "name"),
            ({"name": "abcd"}, "name"),
        ):
            with self.subTest(params=params):
                response = self.client.get("/bounded", params=params)
                self.assertEqual(response.status_code, 422)
                self.assertEqual(
                    response.json()["detail"],
                    [
                        {
                            "loc": ["query", location],
                            "type": "value_error",
                            "msg": "Invalid request input",
                        }
                    ],
                )

    def test_a_suspending_handler_finishes_through_its_task(self):
        response = self.client.get("/suspends")
        self.assertEqual(
            response.json(),
            {
                "native": True,
                "current": True,
                "state": "CORO_RUNNING",
                "name": "build_app.<locals>.suspends",
            },
        )

    def test_errors_take_the_same_boundary_and_handlers(self):
        with self.assertLogs("bessemer", "ERROR"):
            response = self.client.get("/fails")
        self.assertEqual(
            (response.status_code, response.json()),
            (500, {"detail": "Internal Server Error"}),
        )
        with self.assertLogs("bessemer", "ERROR") as logs:
            response = self.client.get("/lookup")
        self.assertEqual(response.status_code, 500)
        self.assertIn("Unhandled KeyError in GET /lookup", logs.output[0])
        response = self.client.get("/http")
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json(), {"detail": "nope"})
        self.assertEqual(response.headers["x-reason"], "test")

    def test_a_base_exception_answers_500_and_leaves_the_loop_running(self):
        with self.assertLogs("bessemer", "ERROR"):
            response = self.client.get("/exits")
        self.assertEqual(response.status_code, 500)
        self.assertEqual(self.client.get("/items/1").status_code, 200)

    def test_responses_with_background_tasks_and_streams(self):
        response = self.client.get("/background")
        self.assertEqual(response.json(), {"ok": True})
        self.assertEqual(self.events, ["background ran"])
        response = self.client.get("/stream")
        self.assertEqual((response.status_code, response.content), (200, b"ab"))

    def test_results_encode_as_on_the_python_path(self):
        if PYDANTIC:
            response = self.client.get("/model")
            self.assertEqual(response.json(), {"id": 1, "name": "a"})
            self.assertEqual(response.headers["content-type"], "application/json")
            response = self.client.get("/model-dict")
            self.assertEqual(response.json(), {"id": 2, "name": "b"})
        self.assertEqual(self.client.get("/tuple").json(), [1, 2])
        response = self.client.get("/empty")
        self.assertEqual((response.status_code, response.content), (204, b""))
        self.assertNotIn("content-type", response.headers)
        response = self.client.request("HEAD", "/items/3")
        self.assertEqual((response.status_code, response.content), (200, b""))

    def test_handlers_are_called_inside_their_task_like_any_awaitable(self):
        response = self.client.get("/future")
        self.assertEqual(response.json(), {"future": True, "current": True})
        response = self.client.get("/fails-later")
        self.assertEqual(
            (response.status_code, response.json()), (418, {"detail": "late"})
        )
        response = self.client.get("/raises-on-call")
        self.assertEqual(
            (response.status_code, response.json()), (418, {"detail": "teapot"})
        )

    def test_a_dropped_request_cancels_its_suspended_handler(self):
        import http.client

        connection = http.client.HTTPConnection("127.0.0.1", self.client._port)
        connection.request("GET", "/blocked")
        deadline = threading.Event()
        for _ in range(500):
            if "blocked started" in self.events:
                break
            deadline.wait(0.01)
        connection.sock.close()
        connection.close()
        for _ in range(500):
            if "blocked cancelled" in self.events:
                break
            deadline.wait(0.01)
        self.assertEqual(self.events, ["blocked started", "blocked cancelled"])


if __name__ == "__main__":
    unittest.main()
