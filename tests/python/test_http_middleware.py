"""User-defined HTTP middleware: ``@app.middleware("http")``."""

import tempfile
import time
import unittest

from bessemer import (
    App,
    Depends,
    HTTPException,
    JSONResponse,
    Request,
    StreamingResponse,
)
from bessemer.testclient import TestClient


def build_app(events):
    app = App()

    @app.middleware("http")
    async def inner(request, call_next):
        events.append("inner:before")
        request.state.user = request.headers.get("x-user", "anonymous")
        response = await call_next(request)
        events.append("inner:after")
        response.headers["X-Inner"] = "1"
        return response

    @app.middleware("http")
    async def outer(request, call_next):
        events.append("outer:before")
        if request.headers.get("x-block") == "1":
            return JSONResponse({"detail": "blocked"}, status_code=403)
        if request.headers.get("x-fail") == "http":
            raise HTTPException(418, "teapot", headers={"X-Why": "middleware"})
        if request.headers.get("x-fail") == "crash":
            raise RuntimeError("middleware bug")
        response = await call_next(request)
        events.append("outer:after")
        response.headers["X-Outer"] = "1"
        if request.headers.get("x-status"):
            response.status_code = int(request.headers["x-status"])
        return response

    @app.get("/items/{item_id}")
    async def read_item(item_id: int):
        events.append("handler")
        return {"id": item_id}

    @app.get("/whoami")
    async def whoami(request: Request):
        return {"user": request.state.user}

    @app.get("/missing")
    async def missing():
        raise HTTPException(404, "nope")

    @app.get("/stream")
    async def stream():
        async def chunks():
            yield b"a"
            yield b"b"

        return StreamingResponse(chunks(), media_type="text/plain")

    return app


class HTTPMiddlewareTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.events = []
        cls.client = TestClient(build_app(cls.events))
        cls.client.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.client.__exit__(None, None, None)

    def setUp(self):
        self.events.clear()

    def test_the_last_registered_middleware_runs_outermost(self):
        response = self.client.get("/items/3")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"id": 3})
        self.assertEqual(response.headers["x-inner"], "1")
        self.assertEqual(response.headers["x-outer"], "1")
        self.assertEqual(response.headers["content-type"], "application/json")
        self.assertEqual(
            self.events,
            [
                "outer:before",
                "inner:before",
                "handler",
                "inner:after",
                "outer:after",
            ],
        )

    def test_request_state_reaches_the_handler(self):
        response = self.client.get("/whoami", headers={"x-user": "ada"})
        self.assertEqual(response.json(), {"user": "ada"})

    def test_middleware_can_short_circuit(self):
        response = self.client.get("/items/1", headers={"x-block": "1"})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json(), {"detail": "blocked"})
        self.assertNotIn("handler", self.events)

    def test_middleware_can_change_the_status(self):
        response = self.client.get("/items/1", headers={"x-status": "201"})
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json(), {"id": 1})

    def test_route_errors_pass_through_middleware(self):
        response = self.client.get("/missing")
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json(), {"detail": "nope"})
        self.assertEqual(response.headers["x-outer"], "1")

    def test_middleware_errors(self):
        teapot = self.client.get("/items/1", headers={"x-fail": "http"})
        self.assertEqual(teapot.status_code, 418)
        self.assertEqual(teapot.json(), {"detail": "teapot"})
        self.assertEqual(teapot.headers["x-why"], "middleware")
        with self.assertLogs("bessemer", level="ERROR"):
            crash = self.client.get("/items/1", headers={"x-fail": "crash"})
        self.assertEqual(crash.status_code, 500)
        self.assertEqual(crash.json(), {"detail": "Internal Server Error"})

    def test_streams_pass_through_with_headers(self):
        response = self.client.get("/stream")
        self.assertEqual(response.content, b"ab")
        self.assertEqual(response.headers["x-outer"], "1")


class DiscardedStreamTests(unittest.TestCase):
    def test_a_replaced_stream_still_runs_its_cleanup(self):
        cleaned = []
        app = App()

        @app.middleware("http")
        async def replace(request, call_next):
            await call_next(request)
            return JSONResponse({"replaced": True})

        async def resource():
            try:
                yield "resource"
            finally:
                cleaned.append("dependency")

        class Chunks:
            def __aiter__(self):
                return self

            async def __anext__(self):
                return b"never read"

            async def aclose(self):
                cleaned.append("iterator")

        @app.get("/stream")
        async def stream(value=Depends(resource)):  # noqa: B008
            return StreamingResponse(Chunks())

        with TestClient(app) as client:
            self.assertEqual(client.get("/stream").json(), {"replaced": True})
            # Cleanup happens with the request, not only at worker shutdown.
            deadline = time.monotonic() + 2
            while len(cleaned) < 2 and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertEqual(sorted(cleaned), ["dependency", "iterator"])


class MiddlewareDeclarationTests(unittest.TestCase):
    def test_invalid_declarations(self):
        app = App()
        with self.assertRaises(ValueError):
            app.middleware("websocket")
        with self.assertRaises(TypeError):

            @app.middleware("http")
            def sync(request, call_next):
                return call_next(request)

        with self.assertRaises(ValueError):

            @app.middleware("http")
            async def one(request):
                return None

        app.build()
        with self.assertRaises(RuntimeError):

            @app.middleware("http")
            async def late(request, call_next):
                return await call_next(request)

    def test_non_response_results_are_errors(self):
        app = App()

        @app.middleware("http")
        async def wrong(request, call_next):
            await call_next(request)
            return {"not": "a response"}

        @app.get("/")
        async def root():
            return {}

        with TestClient(app) as client, self.assertLogs("bessemer", level="ERROR"):
            self.assertEqual(client.get("/").status_code, 500)

    def test_native_static_routes_do_not_enter_middleware(self):
        seen = []
        app = App()

        @app.middleware("http")
        async def record(request, call_next):
            seen.append(request.path)
            return await call_next(request)

        root = tempfile.mkdtemp()
        with open(f"{root}/a.txt", "w") as file:
            file.write("a")
        app.static("/files", root)
        with TestClient(app) as client:
            self.assertEqual(client.get("/files/a.txt").text, "a")
        self.assertEqual(seen, [])


if __name__ == "__main__":
    unittest.main()
