"""Which handler answers an error, which errors middleware sees raised, and
what the error log records of one that nothing handled."""

from __future__ import annotations

# ruff: noqa: B008 -- Dependency marker defaults are the public application API.
import time
import unittest

from bessemer import (
    App,
    BackgroundTasks,
    Depends,
    HTTPException,
    JSONResponse,
    PlainTextResponse,
    Request,
    RequestValidationError,
    ResponseValidationError,
    StreamingResponse,
)
from bessemer.background import BackgroundTask
from bessemer.middleware import BaseHTTPMiddleware
from bessemer.request import current_request
from bessemer.testclient import TestClient
from bessemer.worker import Worker, _trace
from envelopes import payload

try:
    from pydantic import BaseModel
except ImportError:
    BaseModel = None
else:

    class Item(BaseModel):
        name: str
        price: float


try:
    from starlette.middleware.base import (
        BaseHTTPMiddleware as StarletteHTTPMiddleware,
    )
except ImportError:  # pragma: no cover - Starlette is an optional test dependency.
    StarletteHTTPMiddleware = None

INTERNAL = {"detail": "Internal Server Error"}
# One failing route in each shape the worker runs differently.
PATHS = ("/bare", "/sync", "/input/7", "/request", "/dependency", "/response")


class Passing:
    """ASGI middleware that changes nothing."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        await self.app(scope, receive, send)


class Watching:
    """ASGI middleware recording what the application raised into it."""

    def __init__(self, app, events, answer=False):
        self.app = app
        self.events = events
        self.answer = answer

    async def __call__(self, scope, receive, send):
        try:
            await self.app(scope, receive, send)
        except Exception as error:
            self.events.append(f"asgi saw {type(error).__name__}")
            if not self.answer:
                raise
            await send(
                {
                    "type": "http.response.start",
                    "status": 503,
                    "headers": [(b"content-type", b"text/plain")],
                }
            )
            await send({"type": "http.response.body", "body": b"asgi answered"})


def no_middleware(app):
    pass


def http_middleware(app):
    @app.middleware("http")
    async def passing(request, call_next):
        return await call_next(request)


def asgi_middleware(app):
    app.add_middleware(Passing)


def both_middleware(app):
    http_middleware(app)
    asgi_middleware(app)


AROUND = (no_middleware, http_middleware, asgi_middleware, both_middleware)


def settled(events, expected, timeout=2.0):
    """``events`` once work that follows delivery of a response has run."""
    deadline = time.monotonic() + timeout
    while events != expected and time.monotonic() < deadline:
        time.sleep(0.005)
    return list(events)


def add_failing_routes(app, fail):
    """Routes that call ``fail``: without inputs, synchronous, with a path
    input, with the Request, with a dependency and with an explicit response
    (which a route with HTTP middleware finishes on its own path)."""

    async def resource():
        yield "resource"

    @app.get("/bare")
    async def bare():
        fail()

    @app.get("/sync")
    def sync():
        fail()

    @app.get("/input/{item_id}")
    async def with_input(item_id: int):
        fail()

    @app.get("/request")
    async def with_request(request: Request):
        fail()

    @app.get("/dependency")
    async def with_dependency(value=Depends(resource)):
        fail()

    @app.get("/response")
    async def with_response():
        fail()
        return PlainTextResponse("unreachable")


def fail_with_missing():
    raise HTTPException(404, "missing", headers={"X-Why": "test"})


def fail_with_bug():
    raise RuntimeError("bug")


class Conflict(Exception):
    pass


def fail_with_conflict():
    raise Conflict("taken")


class HandlerLookupTests(unittest.TestCase):
    """A handler for a base class does not replace built-in handling."""

    @unittest.skipIf(BaseModel is None, "requires optional Pydantic")
    def test_a_catch_all_handler_leaves_http_and_validation_errors_alone(self):
        caught = []
        for around in AROUND:
            app = App()
            around(app)
            caught.clear()

            @app.exception_handler(Exception)
            async def catch_all(request, error):
                caught.append(type(error).__name__)
                return JSONResponse({"caught": True}, status_code=500)

            add_failing_routes(app, fail_with_missing)

            @app.post("/items")
            async def create(item: Item):
                return item

            @app.get("/bug")
            async def bug():
                fail_with_bug()

            with self.subTest(around=around.__name__), TestClient(app) as client:
                for path in PATHS:
                    response = client.get(path)
                    self.assertEqual(
                        (response.status_code, response.json()),
                        (404, {"detail": "missing"}),
                        path,
                    )
                    self.assertEqual(response.headers["x-why"], "test")
                response = client.post("/items", json={"name": "pen", "price": "x"})
                self.assertEqual(response.status_code, 422)
                self.assertEqual(response.json()["detail"][0]["loc"][0], "body")
                self.assertEqual(caught, [])
                # The catch-all still takes what has no handling of its own.
                with self.assertLogs("bessemer", "ERROR"):
                    response = client.get("/bug")
                self.assertEqual(
                    (response.status_code, response.json()), (500, {"caught": True})
                )
                self.assertEqual(caught, ["RuntimeError"])

    @unittest.skipIf(BaseModel is None, "requires optional Pydantic")
    def test_a_value_error_handler_takes_neither_validation_error(self):
        handled = []
        for around in AROUND:
            app = App()
            around(app)
            handled.clear()

            @app.exception_handler(ValueError)
            def invalid(request, error):
                handled.append(type(error).__name__)
                return JSONResponse({"invalid": True}, status_code=409)

            @app.post("/items")
            async def create(item: Item):
                return item

            @app.get("/mismatch", response_model=Item)
            async def mismatch():
                return {"name": "pen"}

            @app.get("/value")
            async def value():
                raise UnicodeError("a ValueError subclass")

            with self.subTest(around=around.__name__), TestClient(app) as client:
                response = client.post("/items", json={"name": "pen", "price": "x"})
                self.assertEqual(response.status_code, 422)
                self.assertEqual(response.json()["detail"][0]["loc"][0], "body")
                with self.assertLogs("bessemer", "ERROR") as logs:
                    response = client.get("/mismatch")
                self.assertEqual(
                    (response.status_code, response.json()), (500, INTERNAL)
                )
                self.assertIn("Unhandled ResponseValidationError", logs.output[0])
                self.assertEqual(handled, [])
                # It still handles the errors it was registered for.
                response = client.get("/value")
                self.assertEqual(
                    (response.status_code, response.json()), (409, {"invalid": True})
                )
                self.assertEqual(handled, ["UnicodeError"])

    @unittest.skipIf(BaseModel is None, "requires optional Pydantic")
    def test_validation_errors_go_to_handlers_registered_for_them(self):
        seen = []
        for registered in (ResponseValidationError, Exception):
            app = App()
            seen.clear()

            @app.exception_handler(RequestValidationError)
            async def request_invalid(request, error):
                return JSONResponse({"request": "invalid"}, status_code=400)

            @app.exception_handler(registered)
            async def response_invalid(request, error):
                seen.append(type(error))
                return JSONResponse({"response": "invalid"}, status_code=502)

            @app.post("/items")
            async def create(item: Item):
                return item

            @app.get("/mismatch", response_model=Item)
            async def mismatch():
                return {"name": "pen"}

            with (
                self.subTest(registered=registered.__name__),
                TestClient(app) as client,
            ):
                response = client.post("/items", json={"name": "pen", "price": "x"})
                self.assertEqual(
                    (response.status_code, response.json()),
                    (400, {"request": "invalid"}),
                )
                self.assertEqual(seen, [])
                # Its own handler has handled it; the catch-all answers an
                # error that is still logged as unhandled.
                logged = (
                    self.assertLogs("bessemer", "ERROR")
                    if registered is Exception
                    else self.assertNoLogs("bessemer", "ERROR")
                )
                with logged:
                    response = client.get("/mismatch")
                self.assertEqual(
                    (response.status_code, response.json()),
                    (502, {"response": "invalid"}),
                )
                self.assertEqual(seen, [ResponseValidationError])

    def test_http_exception_handlers_by_status_class_and_subclass(self):
        class Teapot(HTTPException):
            def __init__(self):
                super().__init__(418, "teapot")

        app = App()

        @app.exception_handler(404)
        async def by_status(request, error):
            return JSONResponse({"by": "status"}, status_code=404)

        @app.exception_handler(HTTPException)
        async def by_class(request, error):
            return JSONResponse({"by": "class"}, status_code=error.status_code)

        @app.exception_handler(Teapot)
        async def by_subclass(request, error):
            return JSONResponse({"by": "subclass"}, status_code=error.status_code)

        @app.exception_handler(Exception)
        async def catch_all(request, error):
            return JSONResponse({"by": "catch-all"}, status_code=500)

        @app.get("/status/{code}")
        async def raise_status(code: int):
            raise HTTPException(code)

        @app.get("/teapot")
        async def teapot():
            raise Teapot

        with TestClient(app) as client:
            self.assertEqual(client.get("/status/404").json(), {"by": "status"})
            response = client.get("/status/403")
            self.assertEqual(
                (response.status_code, response.json()), (403, {"by": "class"})
            )
            self.assertEqual(client.get("/teapot").json(), {"by": "subclass"})

    def test_the_native_path_keeps_the_built_in_handling(self):
        # No handler and no middleware: the route runs on the native invoker.
        app = App()
        add_failing_routes(app, fail_with_missing)

        @app.get("/bug")
        async def bug():
            fail_with_bug()

        with TestClient(app) as client:
            for path in PATHS:
                with self.assertNoLogs("bessemer", "ERROR"):
                    response = client.get(path)
                self.assertEqual(
                    (response.status_code, response.json()),
                    (404, {"detail": "missing"}),
                    path,
                )
            with self.assertLogs("bessemer", "ERROR") as logs:
                response = client.get("/bug")
            self.assertEqual((response.status_code, response.json()), (500, INTERNAL))
            self.assertIn("Unhandled RuntimeError in GET /bug\n", logs.output[0])
            self.assertIn("in fail_with_bug", logs.output[0])


def fail_with_secret():
    try:
        raise KeyError("hunter2-in-the-cause")
    except KeyError as error:
        raise RuntimeError("hunter2-in-the-message") from error


class ErrorLogTests(unittest.TestCase):
    """Only an error nothing handled is logged, with where it was
    raised and without any exception message."""

    def test_a_handled_error_logs_nothing(self):
        for around in AROUND:
            for fail in (fail_with_conflict, fail_with_missing):
                app = App()
                around(app)

                @app.exception_handler(Conflict)
                async def conflict(request, error):
                    return JSONResponse({"conflict": str(error)}, status_code=409)

                add_failing_routes(app, fail)
                expected = 409 if fail is fail_with_conflict else 404
                with (
                    self.subTest(around=around.__name__, fail=fail.__name__),
                    TestClient(app) as client,
                    self.assertNoLogs("bessemer", "ERROR"),
                ):
                    for path in PATHS:
                        self.assertEqual(client.get(path).status_code, expected, path)

    def test_an_unhandled_error_is_logged_once_with_frames_and_no_messages(self):
        for around in AROUND:
            app = App()
            around(app)
            add_failing_routes(app, fail_with_secret)
            with self.subTest(around=around.__name__), TestClient(app) as client:
                for path in PATHS:
                    with self.assertLogs("bessemer", "ERROR") as logs:
                        response = client.get(path)
                    self.assertEqual(
                        (response.status_code, response.json()), (500, INTERNAL)
                    )
                    self.assertEqual(len(logs.records), 1, path)
                    record = logs.records[0]
                    text = record.getMessage()
                    route = "/input/{item_id}" if path == "/input/7" else path
                    self.assertTrue(
                        text.startswith(f"Unhandled RuntimeError in GET {route}\n"),
                        text,
                    )
                    # Where it was raised, and the cause it is chained to:
                    # the frame of each, then the class of each.
                    raised = f'  File "{__file__}", line '
                    self.assertEqual(
                        sum(
                            line.startswith(raised)
                            and line.endswith(", in fail_with_secret")
                            for line in text.split("\n")
                        ),
                        2,
                        text,
                    )
                    cause, _, error = text.partition(
                        "\nThe above exception was the direct cause of the"
                        " following exception:\n"
                    )
                    self.assertTrue(cause.endswith(", in fail_with_secret\nKeyError\n"))
                    self.assertTrue(
                        error.endswith(", in fail_with_secret\nRuntimeError"), text
                    )
                    # Neither message, in the record or however it is formatted.
                    self.assertIsNone(record.exc_info)
                    self.assertNotIn("hunter2", text)
                    self.assertNotIn("hunter2", logs.output[0])
                    # No source lines either: a literal there is not logged.
                    self.assertNotIn("raise ", text)

    def test_a_failing_handler_is_logged_and_answered_with_500(self):
        app = App()

        @app.exception_handler(Conflict)
        async def broken(request, error):
            raise ZeroDivisionError("hunter2-in-the-handler")

        add_failing_routes(app, fail_with_conflict)
        with TestClient(app) as client:
            for path in PATHS:
                with self.assertLogs("bessemer", "ERROR") as logs:
                    response = client.get(path)
                self.assertEqual(
                    (response.status_code, response.json()), (500, INTERNAL)
                )
                self.assertEqual(len(logs.records), 1, path)
                text = logs.records[0].getMessage()
                self.assertTrue(
                    text.startswith(
                        "Unhandled ZeroDivisionError in an application"
                        " exception handler\n"
                    ),
                    text,
                )
                self.assertIn(".<locals>.broken\n", text)
                # The error it was handling is the context.
                self.assertIn("\ntest_exception_boundaries.Conflict\n", text)
                self.assertIn("During handling of the above exception", text)
                self.assertNotIn("hunter2", text)

    def test_the_catch_all_handler_answers_an_error_that_is_still_logged(self):
        for registered in (Exception, 500):
            app = App()

            @app.exception_handler(registered)
            async def catch_all(request, error):
                return JSONResponse({"sorry": type(error).__name__}, status_code=500)

            add_failing_routes(app, fail_with_bug)
            with self.subTest(registered=registered), TestClient(app) as client:
                for path in PATHS:
                    with self.assertLogs("bessemer", "ERROR") as logs:
                        response = client.get(path)
                    self.assertEqual(
                        (response.status_code, response.json()),
                        (500, {"sorry": "RuntimeError"}),
                    )
                    self.assertEqual(len(logs.records), 1, path)
                    self.assertIn("Unhandled RuntimeError in GET ", logs.output[0])
                    self.assertIn("in fail_with_bug", logs.output[0])

    def test_trace_orders_chains_and_bounds_what_it_records(self):
        def deep(depth):
            if depth:
                deep(depth - 1)
            raise ValueError("hunter2")

        try:
            try:
                deep(200)
            except ValueError:
                raise LookupError("hunter2") from None
        except LookupError as error:
            suppressed = _trace(error)
        # ``from None`` hides the context, as in a Python traceback.
        self.assertNotIn("ValueError", suppressed)
        self.assertTrue(suppressed.endswith("\nLookupError"))

        try:
            try:
                deep(200)
            except ValueError:
                # Raised while handling: the context is implicit.
                raise LookupError("hunter2")
        except LookupError as error:
            chained = _trace(error)
        lines = chained.split("\n")
        self.assertEqual(lines[0], "Traceback (most recent call last):")
        # The innermost frames of a deep traceback are the ones kept.
        self.assertEqual(lines[1], "  ... 138 outer frames omitted")
        self.assertEqual(sum(".<locals>.deep" in line for line in lines), 64)
        self.assertLess(lines.index("ValueError"), lines.index("LookupError"))
        self.assertIn(
            "During handling of the above exception, another exception occurred:",
            lines,
        )
        self.assertNotIn("hunter2", chained)

        # A cycle of causes ends, and a group lists its members.
        first, second = KeyError("hunter2"), IndexError("hunter2")
        first.__cause__, second.__cause__ = second, first
        self.assertEqual(_trace(first).count("Traceback"), 2)
        group = ExceptionGroup("hunter2", [first, OSError("hunter2")])
        grouped = _trace(group)
        self.assertIn("ExceptionGroup\n  | Traceback", grouped)
        self.assertIn("\n  | OSError", grouped)
        self.assertNotIn("hunter2", grouped)
        many = ExceptionGroup("hunter2", [OSError(str(n)) for n in range(40)])
        self.assertIn("  | ... 25 more omitted", _trace(many))


def transaction(app, events):
    @app.middleware("http")
    async def unit_of_work(request, call_next):
        try:
            response = await call_next(request)
        except Exception as error:
            # The request is still the current one while the chain unwinds.
            assert current_request.get() is request
            events.append(f"rollback after {type(error).__name__}")
            raise
        events.append(f"commit after {response.status_code}")
        return response


class HttpMiddlewareBoundaryTests(unittest.TestCase):
    """An error no handler took is raised from ``call_next``."""

    def test_an_unhandled_route_error_is_raised_from_call_next(self):
        events = []
        app = App()
        transaction(app, events)
        add_failing_routes(app, fail_with_bug)
        with TestClient(app) as client:
            for path in PATHS:
                events.clear()
                with self.assertLogs("bessemer", "ERROR") as logs:
                    response = client.get(path)
                self.assertEqual(
                    (response.status_code, response.json()), (500, INTERNAL)
                )
                self.assertEqual(events, ["rollback after RuntimeError"], path)
                self.assertEqual(len(logs.records), 1)
                # Named for the route that raised it, not for the middleware.
                self.assertIn("Unhandled RuntimeError in GET /", logs.output[0])
                self.assertIn(".<locals>.unit_of_work\n", logs.output[0])

    @unittest.skipIf(BaseModel is None, "requires optional Pydantic")
    def test_handled_errors_still_come_back_as_responses(self):
        events = []
        app = App()
        transaction(app, events)

        @app.exception_handler(Conflict)
        async def conflict(request, error):
            return JSONResponse({"conflict": True}, status_code=409)

        @app.get("/missing")
        async def missing():
            fail_with_missing()

        @app.get("/conflict/{item_id}")
        def conflicting(item_id: int):
            fail_with_conflict()

        @app.post("/items")
        async def create(item: Item):
            return item

        with TestClient(app) as client, self.assertNoLogs("bessemer", "ERROR"):
            self.assertEqual(client.get("/missing").status_code, 404)
            self.assertEqual(client.get("/conflict/1").status_code, 409)
            self.assertEqual(client.post("/items", json={"name": 1}).status_code, 422)
        self.assertEqual(
            events, ["commit after 404", "commit after 409", "commit after 422"]
        )

    def test_teardown_has_run_and_queued_tasks_are_dropped_before_the_error(self):
        events = []
        app = App()
        transaction(app, events)

        async def session():
            events.append("open")
            try:
                yield "session"
            except RuntimeError:
                events.append("teardown saw the error")
                raise
            finally:
                events.append("closed")

        @app.get("/items/{item_id}")
        async def read(item_id: int, tasks: BackgroundTasks, value=Depends(session)):
            tasks.add_task(events.append, "background task")
            fail_with_bug()

        with TestClient(app) as client:
            for _ in range(2):
                events.clear()
                with self.assertLogs("bessemer", "ERROR"):
                    self.assertEqual(client.get("/items/1").status_code, 500)
                self.assertEqual(
                    events,
                    [
                        "open",
                        "teardown saw the error",
                        "closed",
                        "rollback after RuntimeError",
                    ],
                )

    def test_a_middleware_can_answer_the_error_itself(self):
        events = []
        app = App()

        @app.middleware("http")
        async def translate(request, call_next):
            try:
                return await call_next(request)
            except RuntimeError:
                return JSONResponse({"detail": "try again"}, status_code=503)

        # Registered last, so outermost: it sees the response, not the error.
        transaction(app, events)
        add_failing_routes(app, fail_with_bug)
        with TestClient(app) as client, self.assertNoLogs("bessemer", "ERROR"):
            for path in PATHS:
                response = client.get(path)
                self.assertEqual(
                    (response.status_code, response.json()),
                    (503, {"detail": "try again"}),
                )
        self.assertEqual(events, ["commit after 503"] * len(PATHS))

    def test_each_middleware_sees_the_error_before_the_catch_all_answers(self):
        events = []
        app = App()

        @app.exception_handler(Exception)
        async def catch_all(request, error):
            events.append("catch-all")
            return JSONResponse({"sorry": type(error).__name__}, status_code=500)

        class Inner(BaseHTTPMiddleware):
            async def dispatch(self, request, call_next):
                try:
                    return await call_next(request)
                finally:
                    events.append("inner")

        app.add_middleware(Inner)
        transaction(app, events)
        add_failing_routes(app, fail_with_bug)
        with TestClient(app) as client:
            for path in PATHS:
                events.clear()
                with self.assertLogs("bessemer", "ERROR"):
                    response = client.get(path)
                self.assertEqual(
                    (response.status_code, response.json()),
                    (500, {"sorry": "RuntimeError"}),
                )
                self.assertEqual(
                    events, ["inner", "rollback after RuntimeError", "catch-all"]
                )

    def test_a_middleware_error_reaches_the_catch_all_and_is_named_in_the_log(self):
        app = App()
        seen = []

        @app.exception_handler(Exception)
        async def catch_all(request, error):
            seen.append(type(error).__name__)
            return JSONResponse({"sorry": True}, status_code=500)

        @app.exception_handler(LookupError)
        async def lookup(request, error):
            seen.append("route handler")
            return JSONResponse({"lookup": True}, status_code=404)

        @app.middleware("http")
        async def broken(request, call_next):
            if request.headers.get("x-fail") == "http":
                raise HTTPException(418, "teapot")
            raise KeyError("middleware bug")

        @app.get("/")
        async def index():
            return {"ok": True}

        with TestClient(app) as client:
            with self.assertLogs("bessemer", "ERROR") as logs:
                response = client.get("/")
            # Handlers for an error's class apply to routes, as in Starlette.
            self.assertEqual(
                (response.status_code, response.json()), (500, {"sorry": True})
            )
            self.assertEqual(seen, ["KeyError"])
            self.assertIn(
                "Unhandled KeyError in HTTP middleware for GET /\n", logs.output[0]
            )
            self.assertIn(".<locals>.broken\n", logs.output[0])
            # An HTTPException from middleware is still its own response.
            with self.assertNoLogs("bessemer", "ERROR"):
                response = client.get("/", headers={"x-fail": "http"})
            self.assertEqual(
                (response.status_code, response.json()), (418, {"detail": "teapot"})
            )

    def test_a_mounted_apps_catch_all_answers_under_the_outer_middleware(self):
        events = []
        parent = App()
        transaction(parent, events)
        child = App()

        @child.exception_handler(Exception)
        async def catch_all(request, error):
            return JSONResponse({"child": "sorry"}, status_code=500)

        add_failing_routes(child, fail_with_bug)
        add_failing_routes(parent, fail_with_bug)
        parent.mount("/child", child)
        with TestClient(parent) as client, self.assertLogs("bessemer", "ERROR"):
            response = client.get("/child/bare")
            self.assertEqual(
                (response.status_code, response.json()), (500, {"child": "sorry"})
            )
            response = client.get("/bare")
            self.assertEqual((response.status_code, response.json()), (500, INTERNAL))
        self.assertEqual(events, ["rollback after RuntimeError"] * 2)

    def test_requests_that_fail_through_middleware_release_their_capacity(self):
        events = []
        app = App()
        transaction(app, events)
        add_failing_routes(app, fail_with_bug)

        @app.get("/ok")
        async def ok():
            return {"ok": True}

        with (
            TestClient(app, server_options={"max_in_flight": 1}) as client,
            self.assertLogs("bessemer", "ERROR"),
        ):
            for _ in range(3):
                for path in PATHS:
                    self.assertEqual(client.get(path).status_code, 500)
            self.assertEqual(client.get("/ok").json(), {"ok": True})

    def test_the_worker_answers_an_error_that_left_the_chain(self):
        # Without the native server: what the invocation's future receives.
        events = []
        app = App()
        transaction(app, events)
        add_failing_routes(app, fail_with_bug)
        app.build()
        with Worker(app) as worker, self.assertLogs("bessemer", "ERROR"):
            for route_id in (0, 1):
                self.assertEqual(
                    payload(worker.submit(route_id, {}).result(timeout=5)),
                    (500, INTERNAL),
                )
        self.assertEqual(events, ["rollback after RuntimeError"] * 2)


class AsgiMiddlewareBoundaryTests(unittest.TestCase):
    """An error no handler took is raised into standard ASGI middleware."""

    def test_an_unhandled_route_error_is_raised_into_asgi_middleware(self):
        for inner in (no_middleware, http_middleware):
            events = []
            app = App()
            inner(app)
            app.add_middleware(Watching, events)
            add_failing_routes(app, fail_with_bug)
            with self.subTest(inner=inner.__name__), TestClient(app) as client:
                for path in PATHS:
                    events.clear()
                    with self.assertLogs("bessemer", "ERROR") as logs:
                        response = client.get(path)
                    self.assertEqual(
                        (response.status_code, response.json()), (500, INTERNAL)
                    )
                    self.assertEqual(events, ["asgi saw RuntimeError"], path)
                    self.assertEqual(len(logs.records), 1)
                    self.assertIn("Unhandled RuntimeError in GET /", logs.output[0])
                    self.assertIn(", in Watching.__call__\n", logs.output[0])

    def test_handled_errors_are_ordinary_responses_to_asgi_middleware(self):
        events = []
        app = App()
        app.add_middleware(Watching, events)

        @app.exception_handler(Conflict)
        async def conflict(request, error):
            return JSONResponse({"conflict": True}, status_code=409)

        add_failing_routes(app, fail_with_conflict)

        @app.get("/missing")
        async def missing():
            fail_with_missing()

        with TestClient(app) as client, self.assertNoLogs("bessemer", "ERROR"):
            for path in PATHS:
                self.assertEqual(client.get(path).status_code, 409)
            self.assertEqual(client.get("/missing").status_code, 404)
        self.assertEqual(events, [])

    def test_asgi_middleware_can_answer_the_error_itself(self):
        events = []
        app = App()
        app.add_middleware(Watching, events, answer=True)
        add_failing_routes(app, fail_with_bug)
        with TestClient(app) as client, self.assertNoLogs("bessemer", "ERROR"):
            for path in PATHS:
                response = client.get(path)
                self.assertEqual(
                    (response.status_code, response.text), (503, "asgi answered")
                )
        self.assertEqual(events, ["asgi saw RuntimeError"] * len(PATHS))

    def test_every_layer_sees_the_error_before_the_catch_all_answers(self):
        events = []
        app = App()

        @app.exception_handler(Exception)
        async def catch_all(request, error):
            events.append("catch-all")
            return JSONResponse({"sorry": type(error).__name__}, status_code=500)

        transaction(app, events)
        app.add_middleware(Watching, events)
        add_failing_routes(app, fail_with_bug)
        with TestClient(app) as client:
            for path in PATHS:
                events.clear()
                with self.assertLogs("bessemer", "ERROR"):
                    response = client.get(path)
                self.assertEqual(
                    (response.status_code, response.json()),
                    (500, {"sorry": "RuntimeError"}),
                )
                self.assertEqual(
                    events,
                    [
                        "rollback after RuntimeError",
                        "asgi saw RuntimeError",
                        "catch-all",
                    ],
                )

    def test_middleware_errors_are_named_for_the_layer_that_raised_them(self):
        class Broken:
            def __init__(self, app):
                self.app = app

            async def __call__(self, scope, receive, send):
                if scope["path"] == "/asgi":
                    raise KeyError("asgi middleware bug")
                await self.app(scope, receive, send)

        app = App()
        app.add_middleware(Broken)
        seen = []

        @app.exception_handler(Exception)
        async def catch_all(request, error):
            seen.append(type(error).__name__)
            return JSONResponse({"sorry": True}, status_code=500)

        @app.middleware("http")
        async def broken(request, call_next):
            if request.path == "/http":
                raise IndexError("http middleware bug")
            return await call_next(request)

        @app.get("/{name}")
        async def index(name: str):
            return {"ok": True}

        with TestClient(app) as client:
            for path, origin in (
                ("/asgi", "Unhandled KeyError in ASGI middleware for GET /{name}\n"),
                ("/http", "Unhandled IndexError in HTTP middleware for GET /{name}\n"),
            ):
                with self.assertLogs("bessemer", "ERROR") as logs:
                    response = client.get(path)
                self.assertEqual(
                    (response.status_code, response.json()), (500, {"sorry": True})
                )
                self.assertIn(origin, logs.output[0])
            self.assertEqual(client.get("/fine").json(), {"ok": True})
        self.assertEqual(seen, ["KeyError", "IndexError"])

    @unittest.skipIf(StarletteHTTPMiddleware is None, "requires optional Starlette")
    def test_starlettes_base_http_middleware_sees_the_error_from_call_next(self):
        events = []

        class Rollback(StarletteHTTPMiddleware):
            async def dispatch(self, request, call_next):
                try:
                    response = await call_next(request)
                except RuntimeError:
                    events.append("rollback")
                    raise
                events.append(f"commit after {response.status_code}")
                return response

        app = App()
        app.add_middleware(Rollback)
        add_failing_routes(app, fail_with_bug)

        @app.get("/missing")
        async def missing():
            fail_with_missing()

        with TestClient(app) as client:
            for path in PATHS:
                with self.assertLogs("bessemer", "ERROR"):
                    self.assertEqual(client.get(path).status_code, 500)
            self.assertEqual(client.get("/missing").status_code, 404)
        self.assertEqual(events, ["rollback"] * len(PATHS) + ["commit after 404"])


class HandlerStreamTests(unittest.TestCase):
    """An exception handler may answer with a stream."""

    def build(self, events, around=no_middleware):
        app = App()
        around(app)

        async def chunks():
            try:
                yield b"partial "
                yield b"content"
            finally:
                events.append("stream closed")

        @app.exception_handler(Conflict)
        async def streamed(request, error):
            return StreamingResponse(
                chunks(),
                status_code=409,
                media_type="text/plain",
                background=BackgroundTask(events.append, "after the stream"),
            )

        add_failing_routes(app, fail_with_conflict)
        return app

    def test_a_handlers_stream_is_delivered_with_its_background_task(self):
        for around in AROUND:
            events = []
            with (
                self.subTest(around=around.__name__),
                TestClient(self.build(events, around)) as client,
                self.assertNoLogs("bessemer", "ERROR"),
            ):
                for path in PATHS:
                    events.clear()
                    response = client.get(path)
                    self.assertEqual(
                        (response.status_code, response.text),
                        (409, "partial content"),
                        path,
                    )
                    self.assertEqual(
                        response.headers["content-type"], "text/plain; charset=utf-8"
                    )
                    # Its task runs once the stream has been delivered.
                    self.assertEqual(
                        settled(events, ["stream closed", "after the stream"]),
                        ["stream closed", "after the stream"],
                    )

    def test_a_handlers_stream_that_middleware_replaces_is_closed(self):
        events = []
        app = self.build(events)

        @app.middleware("http")
        async def replace(request, call_next):
            response = await call_next(request)
            return PlainTextResponse("replaced", status_code=response.status_code)

        with TestClient(app) as client, self.assertNoLogs("bessemer", "ERROR"):
            for path in PATHS:
                events.clear()
                response = client.get(path)
                self.assertEqual(
                    (response.status_code, response.text), (409, "replaced")
                )
                # The discarded stream never started, so its generator has no
                # frame to finalize. Its task still runs after delivery, as a
                # replaced route response's tasks do.
                self.assertEqual(
                    settled(events, ["after the stream"]), ["after the stream"]
                )


if __name__ == "__main__":
    unittest.main()
