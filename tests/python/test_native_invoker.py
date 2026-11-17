"""The native invoker runs lean routes without the worker's Python code.

Everything a handler or client can observe must match the Python path: the
task and context each handler runs in, ``current_request``, exception
handlers, cancellation, validation errors and the encoded responses.
"""

from __future__ import annotations

import asyncio
import contextvars
import importlib.util
import inspect
import sys
import threading
import time
import types
import unittest
from unittest import mock

from bessemer import (
    App,
    BackgroundTasks,
    Depends,
    HTTPException,
    JSONResponse,
    Query,
    StreamingResponse,
)
from bessemer.background import BackgroundTask
from bessemer.request import Request, current_request
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

    @app.get("/independent")
    async def independent():
        request = current_request.get()
        # Each request gets its own object and attributes: what one handler
        # sets is never seen by the next.
        seen = {
            "type": type(request) is Request,
            "leftover": hasattr(request, "marker"),
            "method": request.method,
            "native": _native(),
        }
        request.marker = True
        request.method = "CHANGED"
        return seen

    @app.get("/bounded")
    async def bounded(
        n: int = Query(1, ge=1, lt=10),
        ratio: float = Query(0.5, gt=0, le=1.5),
        name: str = Query("ab", min_length=2, max_length=3),
    ):
        return {"n": n, "ratio": ratio, "name": name, "native": _native()}

    @app.get("/suspends")
    async def suspends():
        # A cycle through the request back to the task must stay collectable.
        current_request.get().state.task = asyncio.current_task()
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

    @types.coroutine
    def generator_based():
        yield
        return {"generator": True}

    app.get("/generator")(inspect.markcoroutinefunction(lambda: generator_based()))

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

    def test_user_done_callbacks_keep_registration_context_after_resume(self):
        from bessemer.testclient import TestClient

        marker = contextvars.ContextVar("completion_marker", default="unset")
        seen = []
        app = App()

        @app.get("/suspended")
        async def suspended():
            task = asyncio.current_task()
            request = current_request.get()
            marker.set("registered")

            def completed(finished):
                seen.append(
                    (
                        finished is task,
                        finished.done(),
                        finished.cancelled(),
                        marker.get(),
                        current_request.get() is request,
                    )
                )

            task.add_done_callback(completed)
            marker.set("resumed")
            await asyncio.sleep(0.001)
            return {"ok": True}

        for event_loop in ("asyncio", "bessemer"):
            with self.subTest(event_loop=event_loop):
                seen.clear()
                with TestClient(
                    app, server_options={"event_loop": event_loop}
                ) as client:
                    self.assertEqual(client.get("/suspended").json(), {"ok": True})
                    self.assertEqual(seen, [(True, True, False, "registered", True)])

    def test_a_suspended_task_is_released_while_the_server_runs(self):
        import gc
        import weakref

        from bessemer.testclient import TestClient

        app = App()
        seen = []

        @app.get("/suspended")
        async def suspended():
            self.assertTrue(_native())
            seen.append(weakref.ref(asyncio.current_task()))
            await asyncio.sleep(0)
            return {"ok": True}

        for event_loop in ("asyncio", "bessemer"):
            with self.subTest(event_loop=event_loop):
                seen.clear()
                with TestClient(
                    app, server_options={"event_loop": event_loop}
                ) as client:
                    self.assertEqual(client.get("/suspended").json(), {"ok": True})
                    deadline = time.monotonic() + 1
                    while seen[0]() is not None and time.monotonic() < deadline:
                        gc.collect()
                        time.sleep(0.01)
                    self.assertIsNone(seen[0]())

    def test_a_finished_request_releases_its_context_while_the_server_runs(self):
        import gc
        import weakref

        from bessemer.testclient import TestClient

        app = App()
        seen = []

        @app.get("/probe")
        async def probe():
            task = asyncio.current_task()
            seen.append(
                (
                    weakref.ref(current_request.get()),
                    type(task.get_coro()).__name__,
                    task.get_coro()._context is task.get_context(),
                )
            )
            return {"ok": True}

        for event_loop in ("asyncio", "bessemer"):
            with self.subTest(event_loop=event_loop):
                seen.clear()
                with TestClient(
                    app, server_options={"event_loop": event_loop}
                ) as client:
                    self.assertEqual(client.get("/probe").json(), {"ok": True})
                    gc.collect()
                    # Nothing the invoker reuses (its eager task options)
                    # keeps the last request's context, and with it the
                    # request, alive.
                    ((request, coroutine, own_context),) = seen
                    self.assertIsNone(request())
                    self.assertEqual(coroutine, "Call")
                    self.assertTrue(own_context)


@unittest.skipUnless(NATIVE, "build the native extension first")
class NativeSyncTests(unittest.TestCase):
    def test_cancelled_queued_native_sync_never_starts_and_keeps_admission(self):
        for loop in ("asyncio", "bessemer"):
            with self.subTest(loop=loop):
                self._check_cancelled_native_sync(loop)

    def _check_cancelled_native_sync(self, loop):
        import concurrent.futures

        from bessemer.testclient import TestClient

        app = App()
        started, release, called = (threading.Event() for _ in range(3))

        @app.get("/block", timeout=0.05)
        def block():
            started.set()
            release.wait(5)
            return {"done": True}

        @app.get("/queued", timeout=0.05)
        def queued():
            called.set()
            return {"unexpected": True}

        @app.get("/probe")
        async def probe():
            return {"ok": True}

        with (
            self.subTest(loop=loop),
            TestClient(
                app,
                server_options={
                    "event_loop": loop,
                    "sync_workers": 1,
                    "max_in_flight": 2,
                },
            ) as client,
        ):
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as requests:
                first = requests.submit(client.get, "/block")
                try:
                    self.assertTrue(started.wait(2))
                    self.assertEqual(client.get("/queued").status_code, 504)
                    self.assertEqual(first.result(2).status_code, 504)
                    self.assertEqual(client.get("/probe").status_code, 503)
                    self.assertFalse(called.is_set())
                finally:
                    release.set()
            deadline = time.monotonic() + 2
            while client.get("/probe").status_code != 200:
                self.assertLess(time.monotonic(), deadline)
                time.sleep(0.005)
            self.assertFalse(called.is_set())

    def test_a_sync_call_whose_request_was_answered_never_starts(self):
        self._check_answered_sync_call("lean")

    def test_a_dependency_sync_call_whose_request_was_answered_never_starts(self):
        self._check_answered_sync_call("dependency")

    def test_a_sync_dependency_whose_request_was_answered_never_starts(self):
        self._check_answered_sync_call("sync_dependency")

    def test_a_request_sync_call_whose_request_was_answered_never_starts(self):
        self._check_answered_sync_call("request")

    def test_a_middleware_sync_call_whose_request_was_answered_never_starts(self):
        self._check_answered_sync_call("middleware")

    def test_a_disconnected_sync_call_never_starts(self):
        for route_kind in (
            "lean",
            "dependency",
            "sync_dependency",
            "request",
            "middleware",
        ):
            with self.subTest(route_kind=route_kind):
                self._check_answered_sync_call(route_kind, disconnect=True)

    def _check_answered_sync_call(self, route_kind, *, disconnect=False):
        # A sync call waiting for a pool thread learns of cancellation when
        # the loop next takes its batch. Hold that loop while the request is
        # answered 504, or its client closes, then free the pool thread first.
        # Both lean calls and the generic path used by dependencies, Request
        # injection and middleware must ask the reply waiter for themselves.
        # The asyncio loop only: a one-loop Rust worker also serves its own
        # connections, and while it is held neither event can be observed.
        import concurrent.futures
        import http.client

        from bessemer.testclient import TestClient

        app = App()
        started, release, called, holding = (threading.Event() for _ in range(4))

        @app.get("/block")
        def block():
            started.set()
            release.wait(5)
            return {"done": True}

        async def dependency():
            return 1

        def mark_called():
            called.set()
            return {"unexpected": True}

        if route_kind == "dependency":
            dependency_argument = Depends(dependency)

            def queued(value=dependency_argument):
                return mark_called()

        elif route_kind == "sync_dependency":
            dependency_argument = Depends(mark_called)

            async def queued(value=dependency_argument):
                return value

        elif route_kind == "request":

            def queued(request: Request):
                return mark_called()

        else:
            queued = mark_called
        app.get("/queued", timeout=5 if disconnect else 0.05)(queued)

        if route_kind == "middleware":

            @app.middleware("http")
            async def pass_through(request, call_next):
                return await call_next(request)

        @app.get("/hold")
        async def hold():
            holding.set()
            time.sleep(0.4)  # noqa: ASYNC251 - holding the loop is the test
            return {"held": True}

        @app.get("/probe")
        async def probe():
            return {"ok": True}

        with (
            TestClient(
                app,
                server_options={
                    "event_loop": "asyncio",
                    "sync_workers": 1,
                    "max_in_flight": 3,
                },
            ) as client,
            concurrent.futures.ThreadPoolExecutor(max_workers=3) as requests,
        ):
            # Each on a connection of its own: the test client sends one
            # request at a time.
            def connection():
                return http.client.HTTPConnection("127.0.0.1", client._port, timeout=10)

            def status(path):
                transport = connection()
                try:
                    transport.request("GET", path)
                    response = transport.getresponse()
                    response.read()
                    return response.status
                finally:
                    transport.close()

            abandoned = connection() if disconnect else None
            blocked = requests.submit(status, "/block")
            try:
                self.assertTrue(started.wait(2))
                if abandoned is not None:
                    abandoned.request("GET", "/queued")
                    waiting = None
                else:
                    waiting = requests.submit(status, "/queued")
                # Give the call to the pool behind /block before holding
                # the event loop. It cannot run until release is set.
                time.sleep(0.02)
                held = requests.submit(status, "/hold")
                self.assertTrue(holding.wait(2))
                if abandoned is not None:
                    abandoned.close()
                    # Let the transport see EOF while the loop stays held.
                    time.sleep(0.02)
                else:
                    self.assertEqual(waiting.result(2), 504)
                # The answered request still owns its admission until its
                # queued pool work has been cancelled and cleaned up.
                self.assertEqual(client.get("/probe").status_code, 503)
            finally:
                if abandoned is not None:
                    abandoned.close()
                release.set()
            self.assertEqual(blocked.result(5), 200)
            self.assertEqual(held.result(5), 200)
            deadline = time.monotonic() + 2
            while client.get("/probe").status_code != 200:
                self.assertLess(time.monotonic(), deadline)
                time.sleep(0.005)
        self.assertFalse(called.is_set())

    def test_explicit_sync_cancellation_bypasses_application_exception_handlers(self):
        from bessemer.testclient import TestClient

        app = App()
        handled = []

        @app.get("/cancel")
        def cancel():
            raise asyncio.CancelledError

        @app.exception_handler(500)
        async def server_error(request, error):
            handled.append(error)
            return JSONResponse({"unexpected": True}, status_code=599)

        for loop in ("asyncio", "bessemer"):
            with (
                self.subTest(loop=loop),
                TestClient(app, server_options={"event_loop": loop}) as client,
            ):
                self.assertEqual(client.get("/cancel").status_code, 500)
                self.assertEqual(handled, [])

    def test_ordinary_sync_does_not_allocate_a_python_future(self):
        from bessemer.testclient import TestClient

        app = App()

        @app.get("/sync")
        def sync():
            return {"path": current_request.get().path}

        for loop in ("asyncio", "bessemer"):
            with (
                self.subTest(loop=loop),
                mock.patch("bessemer.worker._SyncFuture", side_effect=AssertionError),
                TestClient(app, server_options={"event_loop": loop}) as client,
            ):
                self.assertEqual(client.get("/sync").json(), {"path": "/sync"})

    def test_sync_fallback_awaitables_and_errors_restore_the_request_context(self):
        from bessemer.testclient import TestClient

        variable = contextvars.ContextVar("native_sync_context", default="loop")
        app = App()

        @app.get("/awaitable")
        def awaitable():
            request = current_request.get()
            variable.set("pool")

            async def response():
                await asyncio.sleep(0)
                self.assertIs(current_request.get(), request)
                return {"context": variable.get()}

            return response()

        @app.get("/failure")
        def failure():
            variable.set("pool")
            raise LookupError("native pool failure")

        @app.exception_handler(LookupError)
        async def handled(request, error):
            await asyncio.sleep(0)
            self.assertEqual(str(error), "native pool failure")
            self.assertIs(current_request.get(), request)
            return JSONResponse({"context": variable.get()}, status_code=409)

        for loop in ("asyncio", "bessemer"):
            with (
                self.subTest(loop=loop),
                TestClient(
                    app, server_options={"event_loop": loop, "sync_workers": 1}
                ) as client,
            ):
                self.assertEqual(client.get("/awaitable").json(), {"context": "loop"})
                result = client.get("/failure")
                self.assertEqual(result.status_code, 409)
                self.assertEqual(result.json(), {"context": "loop"})

    def test_generic_sync_dispatch_keeps_bindings_context_and_cleanup(self):
        for loop in ("asyncio", "bessemer"):
            with self.subTest(loop=loop):
                self._check_generic_sync_dispatch(loop)

    def _check_generic_sync_dispatch(self, loop):
        from bessemer.testclient import TestClient

        app = App()
        marker = contextvars.ContextVar("generic_sync_dispatch", default="unset")
        events = []
        finished = threading.Event()
        loop_threads = []

        async def prepare():
            marker.set("request")
            loop_threads.append(threading.get_ident())

        def plain_dependency():
            self.assertEqual(marker.get(), "request")
            self.assertNotIn(threading.get_ident(), loop_threads)
            events.append("plain")
            return object()

        def resource():
            token = marker.set("resource")
            try:
                yield marker.get()
            finally:
                # Generator cleanup uses positional arguments and has
                # no invocation gate: it must run in its own context.
                self.assertEqual(marker.get(), "resource")
                marker.reset(token)
                events.append("closed")
                finished.set()

        def background(prefix, *, suffix):
            self.assertEqual(marker.get(), "request")
            events.append(prefix + suffix)

        prepare_binding = Depends(prepare)
        resource_binding = Depends(resource)
        first_binding = Depends(plain_dependency)
        second_binding = Depends(plain_dependency)

        @app.get("/generic/{n}", dependencies=[prepare_binding])
        def generic(
            request: Request,
            tasks: BackgroundTasks,
            n: int,
            session=resource_binding,
            first=first_binding,
            second=second_binding,
        ):
            # Generic keyword arguments run on the pool with a copy
            # of the request context. Mutating it stays on this call.
            self.assertEqual(marker.get(), "request")
            self.assertNotIn(threading.get_ident(), loop_threads)
            marker.set("handler")
            tasks.add_task(background, "back", suffix="ground")
            return {
                "path": request.path,
                "n": n,
                "session": session,
                "cached": first is second,
            }

        with TestClient(app, server_options={"event_loop": loop}) as client:
            response = client.get("/generic/7")
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(
                response.json(),
                {
                    "path": "/generic/7",
                    "n": 7,
                    "session": "resource",
                    "cached": True,
                },
            )
            self.assertTrue(finished.wait(2))
        self.assertEqual(events, ["plain", "background", "closed"])

    def test_sync_uses_native_binding_and_encoding_on_both_loops(self):
        from bessemer.testclient import TestClient

        seen = []
        for loop in ("asyncio", "bessemer"):
            with self.subTest(loop=loop):
                app = App()
                seen.clear()

                @app.get("/sync/{n}")
                def sync(n: int, q: str = "x"):
                    seen.append(threading.get_ident())
                    request = current_request.get()
                    return {"n": n, "q": q, "path": request.path}

                @app.get("/loop-thread")
                async def loop_thread():
                    return threading.get_ident()

                # A lean sync request must neither invoke the Python fallback
                # nor encode its plain dict through _plain_response.
                with (
                    mock.patch.object(Worker, "_invoke", side_effect=AssertionError),
                    mock.patch.object(
                        Worker, "_plain_response", side_effect=AssertionError
                    ),
                    TestClient(app, server_options={"event_loop": loop}) as client,
                ):
                    response = client.get("/sync/7?q=abc")
                    self.assertEqual(response.status_code, 200)
                    self.assertEqual(
                        response.json(), {"n": 7, "q": "abc", "path": "/sync/{n}"}
                    )
                    self.assertNotEqual(seen[0], client.get("/loop-thread").json())


class GenericResultDispatchTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    async def _sync_plan_result(result, future=None):
        from contextlib import AsyncExitStack

        class SyncWorker:
            _await_result = staticmethod(Worker._await_result)

            async def _call_sync(self, function, args, kwargs, invocation):
                return result

        plan = types.SimpleNamespace(kind="sync", call=None)
        return await Worker._execute_plan(
            SyncWorker(), plan, {}, AsyncExitStack(), future
        )

    async def test_sync_result_keeps_awaitable_container_subclasses(self):
        class AwaitableDict(dict):
            def __await__(self):
                async def awaited():
                    await asyncio.sleep(0)
                    return {"awaited": True}

                return awaited().__await__()

        self.assertEqual(
            await self._sync_plan_result(AwaitableDict(raw=True)), {"awaited": True}
        )

    async def test_sync_result_keeps_live_inspection_for_builtin_values(self):
        original = inspect.isawaitable
        result = {"raw": True}
        observed = []

        def forced(value):
            if value is result:
                observed.append(value)
                return True
            return original(value)

        # Inspection can be changed by an application, including to classify
        # a built-in result as awaitable. It must still be consulted once.
        with (
            mock.patch("bessemer.worker.inspect.isawaitable", side_effect=forced),
            self.assertRaises(TypeError),
        ):
            await self._sync_plan_result(result)
        self.assertEqual(len(observed), 1)

    async def test_sync_result_keeps_virtual_awaitable_registration(self):
        from collections.abc import Awaitable

        class Registered:
            pass

        Awaitable.register(Registered)
        result = Registered()
        self.assertTrue(inspect.isawaitable(result))
        # Registration does not supply __await__: awaiting still raises,
        # rather than returning the object as an ordinary sync result.
        with self.assertRaises(TypeError):
            await self._sync_plan_result(result)

    async def test_cancelled_sync_result_closes_its_unstarted_coroutine(self):
        class Cancelled:
            @staticmethod
            def _raise_if_cancel_requested():
                raise asyncio.CancelledError

        async def result():
            return {"unexpected": True}

        coroutine = result()
        with self.assertRaises(asyncio.CancelledError):
            await self._sync_plan_result(coroutine, Cancelled())
        self.assertEqual(inspect.getcoroutinestate(coroutine), inspect.CORO_CLOSED)

    async def test_flat_async_handler_keeps_none_invocation_support(self):
        from contextlib import AsyncExitStack

        class FlatWorker:
            app = types.SimpleNamespace(state=None)
            _flat_arguments = staticmethod(Worker._flat_arguments)
            _execute_plan = Worker._execute_plan

        async def handler():
            await asyncio.sleep(0)
            return {"ok": True}

        route = types.SimpleNamespace(
            app=None, plan=types.SimpleNamespace(kind="async", call=handler)
        )
        result = await Worker._run_flat(
            FlatWorker(), route, ((), ()), {}, None, None, AsyncExitStack(), None
        )
        self.assertEqual(result, {"ok": True})


@unittest.skipUnless(NATIVE, "build the native extension first")
class NativeBackgroundTests(unittest.TestCase):
    def test_native_background_binding_merges_response_tasks_in_delivery_order(self):
        for loop in ("asyncio", "bessemer"):
            for streaming in (False, True):
                with self.subTest(loop=loop, streaming=streaming):
                    self._check_native_background(loop, streaming)

    def _check_native_background(self, loop, streaming):
        from bessemer.testclient import TestClient

        events = []
        finished = threading.Event()
        app = App()

        def first():
            events.append(("route", current_request.get().path))

        def second():
            events.append(("response", current_request.get().path))
            finished.set()

        @app.get("/")
        async def route(tasks: BackgroundTasks):
            tasks.add_task(first)
            self.assertTrue(_native())
            if streaming:

                async def body():
                    yield b"sent"

                return StreamingResponse(body(), background=BackgroundTask(second))
            return JSONResponse({"sent": True}, background=BackgroundTask(second))

        with TestClient(app, server_options={"event_loop": loop}) as client:
            response = client.get("/")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.text, "sent" if streaming else '{"sent":true}')
            self.assertTrue(finished.wait(2))
        self.assertEqual(events, [("route", "/"), ("response", "/")])


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
        self.assertIn("/sync", native)

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
    event_loop = "asyncio"

    @classmethod
    def setUpClass(cls):
        from bessemer.testclient import TestClient

        cls.events = []
        cls.client = TestClient(
            build_app(cls.events), server_options={"event_loop": cls.event_loop}
        )
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
        for _ in range(3):
            self.assertEqual(
                self.client.get("/independent").json(),
                {"type": True, "leftover": False, "method": "GET", "native": True},
            )
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
        deadline = time.monotonic() + 2
        while not self.events and time.monotonic() < deadline:
            time.sleep(0.001)
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
        self.assertEqual(self.client.get("/generator").json(), {"generator": True})
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


@unittest.skipUnless(
    NATIVE and sys.platform in ("linux", "darwin"), "the Rust loop needs Linux or macOS"
)
class NativeInvokerRustLoopTests(NativeInvokerTests):
    event_loop = "bessemer"


@unittest.skipUnless(NATIVE, "build the native extension first")
class NativeInvokerWorkerStateTests(unittest.TestCase):
    event_loops = (
        ("asyncio", "bessemer") if sys.platform in ("linux", "darwin") else ("asyncio",)
    )

    def test_a_handler_that_stops_accepting_stops_the_jobs_after_it(self):
        import http.client

        from bessemer.testclient import TestClient

        workers = []
        initialize = Worker.__init__

        def record(self, *args, **kwargs):
            initialize(self, *args, **kwargs)
            workers.append(self)

        app = App()

        @app.get("/hold")
        async def hold():
            # Blocks the loop so that the next requests queue up and are
            # started in one batch, in the order they arrived.
            time.sleep(0.4)  # noqa: ASYNC251 -- Blocking the loop is the point.
            return {"held": True}

        @app.get("/stop")
        async def stop():
            workers[-1]._accepting = False
            return {"stopped": _native()}

        @app.get("/after")
        async def after():
            return {"native": _native()}

        def get(port, path, results):
            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
            try:
                connection.request("GET", path)
                results[path] = connection.getresponse().status
            finally:
                connection.close()

        for event_loop in self.event_loops:
            with self.subTest(event_loop=event_loop):
                workers.clear()
                with mock.patch.object(Worker, "__init__", record):
                    client = TestClient(app, server_options={"event_loop": event_loop})
                    client.__enter__()
                try:
                    worker = workers[-1]
                    self.assertTrue(worker._accepting)
                    results = {}
                    threads = []
                    for path in ("/hold", "/stop", "/after"):
                        thread = threading.Thread(
                            target=get, args=(client._port, path, results)
                        )
                        thread.start()
                        threads.append(thread)
                        time.sleep(0.1)
                    for thread in threads:
                        thread.join()
                    self.assertEqual(
                        results, {"/hold": 200, "/stop": 200, "/after": 500}
                    )
                    self.assertFalse(worker._accepting)
                    worker._accepting = True
                    self.assertEqual(client.get("/after").json(), {"native": True})
                finally:
                    client.__exit__(None, None, None)

    def test_work_whose_deadline_passed_while_the_batch_ran_does_not_start(self):
        import http.client

        from bessemer.testclient import TestClient

        TIMEOUT = 1.0
        ran = []
        started = []
        entered = threading.Event()
        release = threading.Event()
        finished = threading.Event()
        app = App()

        @app.get("/block")
        async def block():
            # Keeps the loop busy until /hold and the marks have been sent,
            # so all of them are waiting when the loop next collects its jobs
            # and they form a single batch, in the order they arrived.
            entered.set()
            release.wait(10)  # Blocking the loop is the point.
            return {"blocked": True}

        @app.get("/hold")
        async def hold():
            # Runs first in that batch and blocks until every mark's deadline
            # has passed.
            started.append("hold")
            try:
                time.sleep(TIMEOUT + 0.3)  # noqa: ASYNC251 -- Blocking the loop is the point.
            finally:
                finished.set()
            return {"held": True}

        @app.get("/mark/{n}")
        async def mark(n: int):
            ran.append(n)
            return {"n": n}

        @app.get("/after")
        async def after():
            return {"after": True}

        def send(port, path):
            # Returns once the request is written. There is no thread whose
            # start could be late, so what is sent first is at the server
            # first: client threads were starved by more than their head
            # start on a busy runner.
            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
            connection.request("GET", path)
            return connection

        for event_loop in self.event_loops:
            with self.subTest(event_loop=event_loop):
                ran.clear()
                started.clear()
                for event in (entered, release, finished):
                    event.clear()
                client = TestClient(
                    app,
                    server_options={
                        "event_loop": event_loop,
                        "request_timeout": TIMEOUT,
                    },
                )
                client.__enter__()
                connections = []
                try:
                    connections.append(send(client._port, "/block"))
                    self.assertTrue(entered.wait(5))
                    connections.append(send(client._port, "/hold"))
                    # The asyncio layout reads requests on another thread.
                    # Give it time to hand /hold to the bridge before the
                    # marks, and the marks before the loop is free. All of
                    # this is well inside the request timeout: nothing may
                    # have expired when the batch is collected, or the
                    # collection answers it and nothing here is tested.
                    time.sleep(0.1)
                    marks = {n: send(client._port, f"/mark/{n}") for n in range(1, 4)}
                    connections.extend(marks.values())
                    time.sleep(0.2)
                    release.set()
                    for connection in marks.values():
                        self.assertEqual(connection.getresponse().status, 504)
                    # A timeout can be answered while /hold still blocks the
                    # loop. Only when /hold has returned and a request made
                    # after it has been served has the loop been past the
                    # rest of the batch. A loop that was just blocked this
                    # long sheds requests as overloaded for a moment first.
                    self.assertTrue(finished.wait(10))
                    deadline = time.monotonic() + 10
                    while client.get("/after").status_code != 200:
                        self.assertLess(
                            time.monotonic(), deadline, "the loop did not serve again"
                        )
                        time.sleep(0.05)
                    # /hold was collected before its own deadline and ran.
                    self.assertEqual(started, ["hold"])
                    # The marks' deadlines passed while /hold ran, so none of
                    # them may start.
                    self.assertEqual(ran, [])
                finally:
                    release.set()
                    for connection in connections:
                        connection.close()
                    client.__exit__(None, None, None)

    def test_an_installed_task_factory_gets_every_lean_task(self):
        from contextlib import asynccontextmanager

        from bessemer.testclient import TestClient

        created = []

        def factory(loop, coroutine, **kwargs):
            task = asyncio.Task(coroutine, loop=loop, **kwargs)
            created.append(task)
            return task

        @asynccontextmanager
        async def lifespan(app):
            asyncio.get_running_loop().set_task_factory(factory)
            yield

        app = App(lifespan=lifespan)

        @app.get("/items/{item_id}")
        async def item(item_id: int):
            task = asyncio.current_task()
            return {
                "id": item_id,
                "native": _native(),
                "from_factory": task in created,
                "current": current_request.get().path,
            }

        @app.get("/suspends")
        async def suspends():
            await asyncio.sleep(0.001)
            return {"from_factory": asyncio.current_task() in created}

        for event_loop in self.event_loops:
            with self.subTest(event_loop=event_loop):
                created.clear()
                with TestClient(
                    app, server_options={"event_loop": event_loop}
                ) as client:
                    for item_id in (1, 2):
                        self.assertEqual(
                            client.get(f"/items/{item_id}").json(),
                            {
                                "id": item_id,
                                "native": True,
                                "from_factory": True,
                                "current": "/items/{item_id}",
                            },
                        )
                    self.assertEqual(
                        client.get("/suspends").json(), {"from_factory": True}
                    )


if __name__ == "__main__":
    unittest.main()
