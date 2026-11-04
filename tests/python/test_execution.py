from __future__ import annotations

# ruff: noqa: B008 -- Dependency marker defaults are the public application API.
import asyncio
import concurrent.futures
import contextvars
import inspect
import os
import threading
import unittest
import warnings
from contextlib import asynccontextmanager
from unittest.mock import patch

from bessemer import App, Depends, Query
from bessemer.request import (
    BackgroundTasks,
    Request,
    RequestValidationError,
    State,
    current_request,
)
from bessemer.responses import HTTPException, JSONResponse, PlainTextResponse, Response
from bessemer.worker import Worker
from envelopes import payload

try:
    from pydantic import BaseModel, Field, field_validator
except ImportError:
    BaseModel = None
else:

    class PublicItem(BaseModel):
        name: str = Field(alias="public_name")

    class SensitiveInput(BaseModel):
        password: str

        @field_validator("password")
        @classmethod
        def reject_password(cls, value):
            raise ValueError(f"rejected secret {value}")

    class ManyInvalidInputs(BaseModel):
        values: list[int]

    class CountResponse(BaseModel):
        count: int


class DependencyExecutionTests(unittest.TestCase):
    def test_cancelled_queued_sync_handler_does_not_run_and_waits_for_queue_processing(
        self,
    ):
        blocking = threading.Event()
        release = threading.Event()
        queued = threading.Event()
        called = threading.Event()

        async def mark_queued():
            # This callback runs after _call_sync has enqueued its wrapper and
            # yielded, making the test independent of executor scheduling speed.
            asyncio.get_running_loop().call_soon(queued.set)

        app = App()

        @app.get("/blocker")
        def blocker():
            blocking.set()
            release.wait(2)
            return "completed"

        @app.get("/queued")
        def queued_handler(unused=Depends(mark_queued)):
            called.set()
            return "must not execute"

        with Worker(app, sync_workers=1) as worker:
            first = worker.submit(0, {})
            self.assertTrue(blocking.wait(2))
            second = worker.submit(1, {})
            self.assertTrue(queued.wait(2))
            self.assertTrue(second.cancel())
            with self.assertRaises(concurrent.futures.TimeoutError):
                second.result(timeout=0.01)
            self.assertFalse(called.is_set())
            release.set()
            self.assertEqual(payload(first.result(timeout=2)), (200, "completed"))
            with self.assertRaises(concurrent.futures.CancelledError):
                second.result(timeout=2)
        self.assertFalse(called.is_set())

    def test_cancelled_sync_callable_closes_an_unstarted_returned_coroutine(self):
        started = threading.Event()
        release = threading.Event()
        returned = []

        cancel_sent = threading.Event()

        async def cancel_before_sync_return():
            task = asyncio.current_task()

            def cancel():
                task.cancel()
                cancel_sent.set()

            asyncio.get_running_loop().call_soon(cancel)

        async def followup():
            self.fail("cancelled work must not start a returned coroutine")

        def dependency(unused=Depends(cancel_before_sync_return)):
            started.set()
            release.wait(2)
            # Return only once the cancellation is in flight. Otherwise the
            # loop thread may not run before this returns, and the request
            # finishes without ever seeing the cancellation.
            cancel_sent.wait(2)
            coroutine = followup()
            returned.append(coroutine)
            return coroutine

        app = App()

        @app.get("/")
        async def index(value=Depends(dependency)):
            self.fail("cancelled work must not start its handler")

        with Worker(app) as worker:
            future = worker.submit(0, {})
            self.assertTrue(started.wait(2))
            self.assertFalse(future.done())
            release.set()
            with self.assertRaises(concurrent.futures.CancelledError):
                future.result(timeout=2)
        self.assertEqual(len(returned), 1)
        self.assertEqual(inspect.getcoroutinestate(returned[0]), inspect.CORO_CLOSED)

    @unittest.skipIf(BaseModel is None, "Pydantic v2 is optional")
    def test_serialization_mismatches_fail_without_printing_private_value_warnings(
        self,
    ):
        invalid = CountResponse.model_construct(count="private-serialization-value")
        app = App()

        @app.get("/typed", response_model=CountResponse)
        async def typed():
            return invalid

        @app.get("/untyped", response_model=None)
        async def untyped():
            return invalid

        with (
            Worker(app) as worker,
            warnings.catch_warnings(record=True) as emitted,
            self.assertLogs("bessemer", level="ERROR") as logs,
        ):
            warnings.simplefilter("always")
            for route_id in (0, 1):
                self.assertEqual(
                    payload(worker.submit(route_id, {}).result(timeout=2)),
                    (500, {"detail": "Internal Server Error"}),
                )
        self.assertEqual(emitted, [])
        self.assertNotIn("private-serialization-value", "\n".join(logs.output))

    def test_resource_base_exceptions_return_safe_errors_and_keep_worker_available(
        self,
    ):
        async def asynchronous_resource():
            yield "resource"
            raise SystemExit("private async finalizer failure")

        def synchronous_resource():
            yield "resource"
            raise KeyboardInterrupt("private sync finalizer failure")

        app = App()

        @app.get("/async")
        async def asynchronous(value=Depends(asynchronous_resource)):
            return value

        @app.get("/sync")
        async def synchronous(value=Depends(synchronous_resource)):
            return value

        @app.get("/ready")
        async def ready():
            return {"ready": True}

        with Worker(app) as worker, self.assertLogs("bessemer", level="ERROR") as logs:
            for route_id in (0, 1):
                self.assertEqual(
                    payload(worker.submit(route_id, {}).result(timeout=2)),
                    (500, {"detail": "Internal Server Error"}),
                )
                self.assertEqual(
                    payload(worker.submit(2, {}).result(timeout=2)),
                    (200, {"ready": True}),
                )
        self.assertNotIn("private", "\n".join(logs.output))

    def test_nested_async_context_tokens_unwind_on_success_and_cancellation(self):
        def exercise(cancel_request):
            marker = contextvars.ContextVar("nested_context_marker", default=None)
            entered = threading.Event()
            observed = []

            async def outer():
                token = marker.set("outer")
                try:
                    yield "outer"
                finally:
                    self.assertEqual(marker.get(), "outer")
                    await asyncio.sleep(0)
                    marker.reset(token)
                    observed.append(marker.get())

            async def inner(value=Depends(outer)):
                token = marker.set("inner")
                try:
                    yield value
                finally:
                    self.assertEqual(marker.get(), "inner")
                    await asyncio.sleep(0)
                    marker.reset(token)
                    observed.append(marker.get())

            app = App()

            @app.get("/")
            async def index(value=Depends(inner)):
                self.assertEqual(marker.get(), "inner")
                entered.set()
                if cancel_request:
                    await asyncio.Event().wait()
                return value

            with Worker(app) as worker:
                future = worker.submit(0, {})
                self.assertTrue(entered.wait(2))
                if cancel_request:
                    future.cancel()
                    with self.assertRaises(concurrent.futures.CancelledError):
                        future.result(timeout=2)
                else:
                    self.assertEqual(payload(future.result(timeout=2)), (200, "outer"))
            self.assertEqual(observed, ["outer", None])
            self.assertIsNone(marker.get())

        for cancel_request in (False, True):
            with self.subTest(cancel_request=cancel_request):
                exercise(cancel_request)

    def test_generator_context_tokens_can_be_reset_during_shielded_cleanup(self):
        marker = contextvars.ContextVar("generator_cleanup_marker", default=None)
        observed = []

        async def async_resource(request: Request):
            token = marker.set(request.request_id)
            try:
                yield request.request_id
            finally:
                self.assertEqual(marker.get(), request.request_id)
                marker.reset(token)
                observed.append(("async closed", request.request_id, marker.get()))

        def sync_resource(identifier=Depends(async_resource)):
            token = marker.set("sync " + identifier)
            try:
                yield identifier
            finally:
                self.assertEqual(marker.get(), "sync " + identifier)
                marker.reset(token)
                observed.append(("sync closed", identifier, marker.get()))

        app = App()

        @app.get("/")
        async def index(identifier=Depends(sync_resource)):
            self.assertEqual(marker.get(), identifier)
            await asyncio.sleep(0.001)
            return identifier

        with Worker(app) as worker:
            futures = [
                worker.submit(0, {"__bessemer_request__": {"request_id": identifier}})
                for identifier in ("first", "second")
            ]
            self.assertEqual(
                [payload(future.result(timeout=2)) for future in futures],
                [(200, "first"), (200, "second")],
            )
        for identifier in ("first", "second"):
            self.assertIn(("sync closed", identifier, identifier), observed)
            self.assertIn(("async closed", identifier, None), observed)
        self.assertIsNone(marker.get())

    def test_lifespan_and_application_dependency_context_tokens_survive_cleanup(self):
        marker = contextvars.ContextVar("application_cleanup_marker", default=None)
        observed = []

        @asynccontextmanager
        async def lifespan(app):
            token = marker.set("lifespan")
            try:
                yield
            finally:
                self.assertEqual(marker.get(), "lifespan")
                marker.reset(token)
                observed.append("lifespan closed")

        async def resource():
            self.assertEqual(marker.get(), "lifespan")
            token = marker.set("application")
            try:
                yield "resource"
            finally:
                self.assertEqual(marker.get(), "application")
                marker.reset(token)
                observed.append("application closed")

        app = App(lifespan=lifespan)

        @app.get("/")
        async def index(value=Depends(resource, scope="app")):
            self.assertIsNone(marker.get())
            return value

        with Worker(app) as worker:
            self.assertEqual(
                payload(worker.submit(0, {}).result(timeout=2)), (200, "resource")
            )
        self.assertEqual(observed, ["application closed", "lifespan closed"])
        self.assertIsNone(marker.get())

    @unittest.skipIf(BaseModel is None, "Pydantic v2 is optional")
    def test_model_validation_errors_hide_custom_messages_and_bound_detail_count(self):
        app = App()

        @app.post("/secret")
        async def secret(payload: SensitiveInput):
            self.fail("invalid input reached the handler")

        @app.post("/many")
        async def many(payload: ManyInvalidInputs):
            self.fail("invalid input reached the handler")

        with Worker(app) as worker:
            status, body = payload(
                worker.submit(0, {"payload": {"password": "private-password"}}).result(
                    timeout=2
                )
            )
            self.assertEqual(status, 422)
            self.assertNotIn("private-password", repr(body))
            self.assertEqual(body["detail"][0]["loc"], ["body", "password"])
            self.assertEqual(body["detail"][0]["msg"], "Invalid request body")
            status, body = payload(
                worker.submit(
                    1, {"payload": {"values": ["private-invalid-value"] * 101}}
                ).result(timeout=2)
            )
            self.assertEqual(status, 422)
            self.assertEqual(
                body["detail"],
                [
                    {
                        "loc": ["body"],
                        "type": "too_many_errors",
                        "msg": "Invalid request body",
                    }
                ],
            )

    def test_shared_dependencies_cache_per_request_and_opt_out(self):
        calls = []

        def shared(value: int = Query(2)):
            calls.append(value)
            return value

        async def left(value=Depends(shared)):
            return value + 1

        def right(value=Depends(shared)):
            return value + 2

        app = App()

        @app.get("/")
        async def index(
            first=Depends(left),
            second=Depends(right),
            fresh=Depends(shared, use_cache=False),
        ):
            return [first, second, fresh]

        with Worker(app) as worker:
            for _ in range(2):
                self.assertEqual(
                    payload(worker.submit(0, {}).result(timeout=2)), (200, [3, 4, 2])
                )
        self.assertEqual(calls, [2, 2, 2, 2])

    def test_generator_dependencies_close_in_reverse_order_and_receive_error(self):
        observed = []

        async def outer():
            observed.append("outer open")
            try:
                yield "resource"
            except ValueError:
                observed.append("outer error")
                raise
            finally:
                observed.append("outer close")

        def inner(value=Depends(outer)):
            observed.append("inner open")
            try:
                yield value
            finally:
                observed.append("inner close")

        app = App()

        @app.get("/")
        async def fail(value=Depends(inner)):
            self.assertEqual(value, "resource")
            raise ValueError("private dependency failure")

        with Worker(app) as worker, self.assertLogs("bessemer", level="ERROR") as logs:
            self.assertEqual(
                payload(worker.submit(0, {}).result(timeout=2)),
                (500, {"detail": "Internal Server Error"}),
            )
        self.assertEqual(
            observed,
            ["outer open", "inner open", "inner close", "outer error", "outer close"],
        )
        self.assertNotIn("private dependency failure", "\n".join(logs.output))

    def test_async_cancellation_waits_for_sync_generator_cleanup(self):
        opened = threading.Event()
        cleaning = threading.Event()
        release = threading.Event()

        def resource():
            try:
                yield "resource"
            finally:
                cleaning.set()
                release.wait(2)

        app = App()

        @app.get("/")
        async def index(value=Depends(resource)):
            opened.set()
            await asyncio.Event().wait()

        with Worker(app) as worker:
            future = worker.submit(0, {})
            self.assertTrue(opened.wait(2))
            future.cancel()
            self.assertTrue(cleaning.wait(2))
            self.assertFalse(future.done())
            future.cancel()
            release.set()
            with self.assertRaises(concurrent.futures.CancelledError):
                future.result(timeout=2)

    def test_first_cancellation_during_async_finalizer_does_not_abandon_cleanup(self):
        cleaning = threading.Event()
        release = threading.Event()
        cleaned = threading.Event()

        async def resource():
            try:
                yield "resource"
            finally:
                cleaning.set()
                while not release.is_set():
                    await asyncio.sleep(0.001)
                cleaned.set()

        app = App()

        @app.get("/")
        async def index(value=Depends(resource)):
            return value

        with Worker(app) as worker:
            future = worker.submit(0, {})
            self.assertTrue(cleaning.wait(2))
            future.cancel()
            self.assertFalse(future.done())
            release.set()
            with self.assertRaises(concurrent.futures.CancelledError):
                future.result(timeout=2)
            self.assertTrue(cleaned.is_set())

    def test_cancelled_sync_dependency_does_not_start_handler(self):
        started = threading.Event()
        release = threading.Event()
        called = []

        def resource():
            started.set()
            release.wait(2)
            return "resource"

        app = App()

        @app.get("/")
        async def index(value=Depends(resource)):
            called.append(True)
            return value

        with Worker(app) as worker:
            future = worker.submit(0, {})
            self.assertTrue(started.wait(2))
            self.assertFalse(future.cancel())
            release.set()
            with self.assertRaises(concurrent.futures.CancelledError):
                future.result(timeout=2)
        self.assertEqual(called, [])

    def test_task_cancellation_during_sync_resource_acquisition_closes_resource(self):
        acquiring = threading.Event()
        cancelled = threading.Event()
        release = threading.Event()
        closed = threading.Event()

        async def cancel_during_acquisition():
            task = asyncio.current_task()

            def cancel():
                task.cancel()
                cancelled.set()

            asyncio.get_running_loop().call_soon(cancel)

        def resource(unused=Depends(cancel_during_acquisition)):
            acquiring.set()
            release.wait(2)
            try:
                yield "resource"
            finally:
                closed.set()

        app = App()

        @app.get("/")
        async def index(value=Depends(resource)):
            self.fail("a cancelled acquisition must not execute the handler")

        with Worker(app) as worker:
            future = worker.submit(0, {})
            self.assertTrue(acquiring.wait(2))
            self.assertTrue(cancelled.wait(2))
            self.assertFalse(future.done())
            release.set()
            with self.assertRaises(concurrent.futures.CancelledError):
                future.result(timeout=2)
            self.assertTrue(closed.is_set())

    def test_application_dependencies_initialize_once_and_close_before_lifespan(self):
        events = []

        @asynccontextmanager
        async def lifespan(app):
            app.state.open = True
            events.append("lifespan open")
            yield
            app.state.open = False
            events.append("lifespan close")

        async def configuration(state: State):
            self.assertTrue(state.open)
            events.append("dependency open")
            yield {"value": 7}
            self.assertTrue(state.open)
            events.append("dependency close")

        app = App(lifespan=lifespan)

        @app.get("/first")
        async def first(value=Depends(configuration, scope="app")):
            return value

        @app.get("/second")
        async def second(value=Depends(configuration, scope="app")):
            return value

        with Worker(app) as worker:
            self.assertEqual(events, ["lifespan open", "dependency open"])
            for route_id in (0, 1, 0):
                self.assertEqual(
                    payload(worker.submit(route_id, {}).result(timeout=2)),
                    (200, {"value": 7}),
                )
        self.assertEqual(
            events,
            ["lifespan open", "dependency open", "dependency close", "lifespan close"],
        )

    def test_application_dependency_failures_unwind_through_the_lifespan(self):
        events = []

        @asynccontextmanager
        async def lifespan(app):
            events.append("lifespan open")
            try:
                yield
            except ValueError:
                events.append("lifespan observed failure")
                raise
            finally:
                events.append("lifespan close")

        async def broken_dependency():
            raise ValueError("failed to acquire application resource")

        app = App(lifespan=lifespan)

        @app.get("/")
        async def index(value=Depends(broken_dependency, scope="app")):
            return value

        worker = Worker(app)
        with self.assertRaisesRegex(ValueError, "failed to acquire"):
            worker.start()
        worker.close()
        self.assertEqual(
            events, ["lifespan open", "lifespan observed failure", "lifespan close"]
        )

    def test_application_dependency_cleanup_failure_reaches_lifespan(self):
        observed = []

        @asynccontextmanager
        async def lifespan(app):
            try:
                yield
            except ValueError:
                observed.append("lifespan observed cleanup failure")
                raise
            finally:
                observed.append("lifespan closed")

        def resource():
            yield "resource"
            raise ValueError("failed to close application resource")

        app = App(lifespan=lifespan)

        @app.get("/")
        async def index(value=Depends(resource, scope="app")):
            return value

        worker = Worker(app)
        worker.start()
        with self.assertRaisesRegex(ValueError, "failed to close"):
            worker.close()
        self.assertTrue(worker._loop.is_closed())
        self.assertEqual(
            observed, ["lifespan observed cleanup failure", "lifespan closed"]
        )

    def test_background_tasks_and_sync_dependencies_preserve_request_context(self):
        events = []

        def resource(request: Request):
            self.assertIs(current_request.get(), request)
            events.append("open " + request.request_id)
            yield request.request_id
            self.assertIs(current_request.get(), request)
            events.append("close " + request.request_id)

        def background(identifier):
            self.assertEqual(current_request.get().request_id, identifier)
            events.append("background " + identifier)

        app = App()

        @app.get("/")
        async def index(tasks: BackgroundTasks, identifier=Depends(resource)):
            tasks.add_task(background, identifier)
            await asyncio.sleep(0.001)
            return identifier

        with Worker(app) as worker:
            futures = [
                worker.submit(0, {"__bessemer_request__": {"request_id": name}})
                for name in ("first", "second")
            ]
            self.assertEqual(
                [payload(future.result(timeout=2)) for future in futures],
                [(200, "first"), (200, "second")],
            )
        for name in ("first", "second"):
            self.assertLess(
                events.index("open " + name), events.index("background " + name)
            )
            self.assertLess(
                events.index("background " + name), events.index("close " + name)
            )
        self.assertIsNone(current_request.get())

    def test_raising_dependency_prevents_handler_and_preserves_error_headers(self):
        called = []

        def authenticated():
            raise HTTPException(
                401, "Authentication required", headers={"WWW-Authenticate": "Bearer"}
            )

        app = App(dependencies=[Depends(authenticated)])

        @app.get("/")
        async def index():
            called.append(True)
            return {}

        with Worker(app) as worker:
            result = payload(worker.submit(0, {}).result(timeout=2))
        self.assertEqual(
            result,
            (
                401,
                {"detail": "Authentication required"},
                [("WWW-Authenticate", "Bearer")],
                "json",
            ),
        )
        self.assertEqual(called, [])

    def test_explicit_response_and_custom_exception_handlers(self):
        app = App()

        @app.exception_handler(ValueError)
        def invalid(request, error):
            return JSONResponse(
                {"path": request.path, "error": "invalid"}, status_code=409
            )

        @app.get("/bytes")
        async def body():
            return PlainTextResponse(
                "ready", status_code=202, headers={"X-Test": "yes"}
            )

        @app.get("/failure")
        async def failure():
            raise ValueError("private detail")

        with Worker(app) as worker:
            self.assertEqual(
                payload(worker.submit(0, {}).result(timeout=2)),
                (
                    202,
                    b"ready",
                    [("X-Test", "yes"), ("Content-Type", "text/plain; charset=utf-8")],
                    "bytes",
                ),
            )
            with self.assertLogs("bessemer", level="ERROR"):
                result = payload(worker.submit(1, {}).result(timeout=2))
            self.assertEqual(
                result[0:2], (409, {"path": "/failure", "error": "invalid"})
            )

    def test_worker_validation_errors_can_be_handled_without_input_leak(self):
        app = App()

        @app.exception_handler(RequestValidationError)
        async def invalid(request, error):
            return JSONResponse({"invalid": True}, status_code=422)

        @app.get("/")
        async def index(name: str = Query(min_length=3)):
            return name

        with Worker(app) as worker:
            result = payload(worker.submit(0, {"name": "ab"}).result(timeout=2))
        self.assertEqual(result[0:2], (422, {"invalid": True}))

    @unittest.skipIf(BaseModel is None, "requires optional Pydantic")
    def test_response_model_filters_fields_uses_aliases_and_rejects_invalid_output(
        self,
    ):
        app = App()

        @app.get("/good", response_model=PublicItem)
        async def good():
            return {"public_name": "visible", "secret": "private"}

        @app.get("/bad", response_model=PublicItem)
        async def bad():
            return {"unexpected": "private"}

        with Worker(app) as worker:
            self.assertEqual(
                payload(worker.submit(0, {}).result(timeout=2)),
                (200, {"public_name": "visible"}),
            )
            with self.assertLogs("bessemer", level="ERROR"):
                self.assertEqual(
                    payload(worker.submit(1, {}).result(timeout=2)),
                    (500, {"detail": "Internal Server Error"}),
                )

    def test_response_normalization_budget_limits_json_and_explicit_bytes(self):
        app = App()

        @app.get("/json")
        async def json_body():
            return ["abcdefghij"] * 1000

        @app.get("/bytes")
        async def byte_body():
            return Response(b"x" * 129)

        with (
            Worker(app, max_response_size=128) as worker,
            self.assertLogs("bessemer", level="ERROR"),
        ):
            for route_id in (0, 1):
                self.assertEqual(
                    payload(worker.submit(route_id, {}).result(timeout=2)),
                    (500, {"detail": "Internal Server Error"}),
                )


class HeartbeatTests(unittest.TestCase):
    @unittest.skipUnless(os.name == "posix", "requires POSIX descriptors")
    def test_heartbeat_uses_async_loop_and_does_not_own_caller_descriptor(self):
        reader, writer = os.pipe()
        try:
            with Worker(App(), heartbeat_fd=writer):
                self.assertEqual(os.read(reader, 1), b"h")
            os.fstat(writer)
        finally:
            os.close(reader)
            os.close(writer)

    @unittest.skipUnless(os.name == "posix", "requires POSIX descriptors")
    def test_broken_heartbeat_does_not_kill_unsupervised_caller(self):
        reader, writer = os.pipe()
        os.close(reader)
        try:
            worker = Worker(App(), heartbeat_fd=writer)
            with (
                patch.dict(os.environ, {"BESSEMER_SUPERVISED_WORKER": "0"}),
                patch("bessemer.worker.os.kill") as kill,
            ):
                with self.assertLogs("bessemer", level="ERROR"):
                    worker.start()
                    worker._thread.join(timeout=2)
                self.assertFalse(worker._thread.is_alive())
                with self.assertRaisesRegex(RuntimeError, "heartbeat channel"):
                    worker.close()
                kill.assert_not_called()
        finally:
            os.close(writer)


if __name__ == "__main__":
    unittest.main()
