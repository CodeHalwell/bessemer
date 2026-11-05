"""Standard ASGI middleware and mounted ASGI applications."""

import gzip
import json
import unittest
from contextlib import asynccontextmanager
from contextvars import ContextVar

from bessemer import App, Depends, HTTPException, Request, StreamingResponse
from bessemer.middleware import Middleware
from bessemer.testclient import TestClient

try:
    import itsdangerous  # noqa: F401 -- Needed by Starlette's SessionMiddleware.
    import starlette
except ImportError:  # pragma: no cover - Starlette is an optional test dependency.
    starlette = None


class HeaderMiddleware:
    """Pure ASGI middleware adding a response header and a scope entry."""

    def __init__(self, app, name="x-asgi", value="1", events=None):
        self.app = app
        self.name = name.encode()
        self.value = value.encode()
        self.events = events if events is not None else []

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        self.events.append(self.name.decode())
        scope = dict(scope)
        scope.setdefault("seen", []).append(self.name.decode())

        async def wrapped(message):
            if message["type"] == "http.response.start":
                message = dict(message)
                message["headers"] = [*message["headers"], (self.name, self.value)]
            await send(message)

        await self.app(scope, receive, wrapped)


class DenyMiddleware:
    """Answers itself without calling the app when a header is present."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if (b"x-deny", b"1") in scope["headers"]:
            await send(
                {
                    "type": "http.response.start",
                    "status": 403,
                    "headers": [(b"content-type", b"text/plain")],
                }
            )
            await send({"type": "http.response.body", "body": b"denied"})
            return
        await self.app(scope, receive, send)


class FailingMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["path"] == "/explode":
            raise RuntimeError("boom")
        if scope["path"] == "/explode-stream":

            async def failing_send(message):
                raise RuntimeError("boom at " + message["type"])

            await self.app(scope, receive, failing_send)
            return
        await self.app(scope, receive, send)


class BodyReader:
    """Reads the body through receive, as body-inspecting middleware does."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        message = await receive()
        body = message.get("body", b"")
        replayed = False

        async def replay():
            nonlocal replayed
            if not replayed:
                replayed = True
                return {"type": "http.request", "body": body, "more_body": False}
            return await receive()

        scope["state"]["body_length"] = len(body)
        await self.app(scope, replay, send)


current_user = ContextVar("current_user", default=None)


async def bound_user():
    token = current_user.set("ada")
    try:
        yield "ada"
    finally:
        current_user.reset(token)


stream_cleanups = []


async def stream_resource():
    try:
        yield "resource"
    finally:
        stream_cleanups.append("closed")


def build_middleware_app(events):
    @asynccontextmanager
    async def lifespan(app):
        yield {"name": "lifespan"}

    app = App(lifespan=lifespan, middleware=[Middleware(DenyMiddleware)])
    app.add_middleware(HeaderMiddleware, name="x-inner", events=events)
    app.add_middleware(HeaderMiddleware, name="x-outer", events=events)
    app.add_middleware(FailingMiddleware)
    app.add_middleware(BodyReader)

    @app.middleware("http")
    async def http_middleware(request, call_next):
        response = await call_next(request)
        response.headers["x-http"] = ",".join(request.scope.get("seen", []))
        return response

    @app.get("/items/{item_id}")
    async def read_item(item_id: int, request: Request):
        return {"item_id": item_id, "state": request.state.name}

    @app.post("/echo")
    async def echo(payload: dict, request: Request):
        return {"payload": payload, "length": request.state.body_length}

    @app.get("/stream")
    async def stream():
        async def chunks():
            for index in range(3):
                yield f"chunk{index};"

        return StreamingResponse(chunks(), media_type="text/plain")

    @app.get("/missing")
    async def missing():
        raise HTTPException(404, "nope")

    @app.get("/explode")
    async def explode():
        return {}

    @app.get("/context")
    async def context(user=Depends(bound_user)):  # noqa: B008
        return {"user": user, "bound": current_user.get()}

    @app.get("/context-stream")
    async def context_stream(user=Depends(bound_user)):  # noqa: B008
        async def chunks():
            yield f"{user}:{current_user.get()}"

        return StreamingResponse(chunks(), media_type="text/plain")

    @app.get("/explode-stream")
    async def explode_stream(resource=Depends(stream_resource)):  # noqa: B008
        async def chunks():
            yield resource

        return StreamingResponse(chunks(), media_type="text/plain")

    @app.get("/session")
    async def session(request: Request):
        return {"session": request.session}

    return app


class ASGIMiddlewareTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.events = []
        cls.client = TestClient(build_middleware_app(cls.events))
        cls.client.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.client.__exit__(None, None, None)

    def test_middleware_wraps_routes_in_order(self):
        self.events.clear()
        response = self.client.get("/items/3")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"item_id": 3, "state": "lifespan"})
        self.assertEqual(response.headers["x-inner"], "1")
        self.assertEqual(response.headers["x-outer"], "1")
        # The last added runs outermost; HTTP middleware runs inside them all.
        self.assertEqual(self.events, ["x-outer", "x-inner"])
        self.assertEqual(response.headers["x-http"], "x-outer,x-inner")

    def test_body_reaches_middleware_and_route(self):
        response = self.client.post("/echo", json={"a": 1})
        self.assertEqual(response.json(), {"payload": {"a": 1}, "length": 7})

    def test_streams_pass_through(self):
        response = self.client.get("/stream")
        self.assertEqual(response.text, "chunk0;chunk1;chunk2;")
        self.assertEqual(response.headers["x-outer"], "1")

    def test_route_errors_are_normal_responses(self):
        response = self.client.get("/missing")
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json(), {"detail": "nope"})
        self.assertEqual(response.headers["x-outer"], "1")

    def test_context_variables_survive_dependency_cleanup(self):
        response = self.client.get("/context")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"user": "ada", "bound": "ada"})
        response = self.client.get("/context-stream")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.text, "ada:ada")

    def test_middleware_can_answer_itself(self):
        response = self.client.get("/items/1", headers={"x-deny": "1"})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.text, "denied")
        # As in Starlette, middleware added later wraps the constructor's.
        self.assertEqual(response.headers["x-outer"], "1")

    def test_middleware_errors_become_500(self):
        response = self.client.get("/explode")
        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.json(), {"detail": "Internal Server Error"})

    def test_failed_sends_close_the_route_stream(self):
        stream_cleanups.clear()
        response = self.client.get("/explode-stream")
        self.assertEqual(response.status_code, 500)
        self.assertEqual(stream_cleanups, ["closed"])

    def test_session_without_middleware_fails_like_starlette(self):
        response = self.client.get("/session")
        self.assertEqual(response.status_code, 500)


class ASGIMiddlewareConfigurationTests(unittest.TestCase):
    def test_mounted_apps_cannot_have_asgi_middleware(self):
        child = App()
        child.add_middleware(HeaderMiddleware)
        with self.assertRaises(ValueError):
            App().mount("/child", child)
        mounted = App()
        App().mount("/m", mounted)
        with self.assertRaises(ValueError):
            mounted.add_middleware(HeaderMiddleware)

    def test_routes_without_asgi_middleware_stay_bare(self):
        app = App()

        @app.get("/")
        async def root():
            return {}

        app.build()
        self.assertFalse(app._compiled[0].declaration["needs_request"])
        app = App()
        app.add_middleware(HeaderMiddleware)

        @app.get("/")
        async def again():
            return {}

        app.build()
        self.assertTrue(app._compiled[0].declaration["needs_request"])


async def raw_app(scope, receive, send):
    """A plain ASGI app reporting what it was called with."""
    assert scope["type"] == "http"
    message = await receive()
    body = (
        f"{scope['method']} path={scope['path']} root={scope['root_path']}"
        f" query={scope['query_string'].decode()} body={message['body'].decode()}"
    ).encode()
    await send(
        {
            "type": "http.response.start",
            "status": 201,
            "headers": [
                (b"content-type", b"text/plain"),
                (b"content-length", str(len(body)).encode()),
                (b"x-raw", b"yes"),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})


async def streaming_app(scope, receive, send):
    await send({"type": "http.response.start", "status": 200, "headers": []})
    for index in range(3):
        await send(
            {
                "type": "http.response.body",
                "body": b"part%d;" % index,
                "more_body": True,
            }
        )
    await send({"type": "http.response.body", "body": b"", "more_body": False})


async def silent_app(scope, receive, send):
    return None


async def failing_app(scope, receive, send):
    raise RuntimeError("mounted app failed")


def build_mount_app():
    app = App()

    @app.get("/native")
    async def native():
        return {"native": True}

    @app.middleware("http")
    async def tag(request, call_next):
        response = await call_next(request)
        response.headers["x-parent"] = "1"
        return response

    app.mount("/raw", raw_app, name="raw")
    app.mount("/streaming", streaming_app)
    app.mount("/silent", silent_app)
    app.mount("/failing", failing_app)
    child = App()
    child.mount("/inner", raw_app)
    app.mount("/child", child)
    return app


class ASGIMountTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(build_mount_app())
        cls.client.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.client.__exit__(None, None, None)

    def test_requests_reach_the_mounted_app(self):
        response = self.client.post("/raw/a/b?x=1", content=b"hello")
        self.assertEqual(response.status_code, 201)
        self.assertEqual(
            response.text, "POST path=/raw/a/b root=/raw query=x=1 body=hello"
        )
        self.assertEqual(response.headers["x-raw"], "yes")
        self.assertEqual(response.headers["x-parent"], "1")

    def test_every_method_and_the_bare_prefix(self):
        for method in ("GET", "PUT", "PATCH", "DELETE", "OPTIONS"):
            response = self.client.request(method, "/raw")
            self.assertEqual(response.status_code, 201, method)
            self.assertTrue(response.text.startswith(f"{method} path=/raw "))
        self.assertIn("path=/raw/ ", self.client.get("/raw/").text)
        self.assertEqual(self.client.head("/raw/x").status_code, 201)

    def test_native_routes_are_unaffected(self):
        self.assertEqual(self.client.get("/native").json(), {"native": True})

    def test_streamed_responses(self):
        response = self.client.get("/streaming/x")
        self.assertEqual(response.text, "part0;part1;part2;")

    def test_app_errors_become_500(self):
        self.assertEqual(self.client.get("/silent").status_code, 500)
        self.assertEqual(self.client.get("/failing").status_code, 500)

    def test_paths_are_decoded_with_raw_path_kept(self):
        response = self.client.get("/raw/hello%20world")
        self.assertIn("path=/raw/hello world ", response.text)

    def test_nested_mount_root_path(self):
        response = self.client.get("/child/inner/z")
        self.assertEqual(
            response.text, "GET path=/child/inner/z root=/child/inner query= body="
        )

    def test_url_path_for_a_named_mount(self):
        app = build_mount_app()
        self.assertEqual(app.url_path_for("raw", path="/a/b"), "/raw/a/b")

    def test_invalid_mounts(self):
        app = App()
        app.mount("/raw", raw_app)
        with self.assertRaises(ValueError):
            app.mount("/raw", raw_app)
        with self.assertRaises(ValueError):
            app.mount("/", raw_app)
        with self.assertRaises(TypeError):
            app.mount("/x", object())


@unittest.skipIf(starlette is None, "Starlette is not installed")
class StarletteInteropTests(unittest.TestCase):
    def test_session_middleware_and_a_mounted_starlette_app(self):
        from starlette.applications import Starlette
        from starlette.middleware.gzip import GZipMiddleware as StarletteGZip
        from starlette.middleware.sessions import SessionMiddleware
        from starlette.responses import PlainTextResponse
        from starlette.routing import Route

        async def hello(request):
            return PlainTextResponse(
                f"{request.url.path} {request.url_for('hello')}"
                f" {request.session.get('n')}"
            )

        star = Starlette(routes=[Route("/hello", hello, name="hello")])
        app = App()
        app.add_middleware(SessionMiddleware, secret_key="test")
        app.add_middleware(StarletteGZip, minimum_size=10)

        @app.get("/count")
        async def count(request: Request):
            request.session["n"] = request.session.get("n", 0) + 1
            return {"n": request.session["n"], "padding": "x" * 100}

        app.mount("/star", star)
        with TestClient(app) as client:
            self.assertEqual(client.get("/count").json()["n"], 1)
            response = client.get("/count", headers={"accept-encoding": "gzip"})
            self.assertEqual(response.headers["content-encoding"], "gzip")
            self.assertEqual(json.loads(gzip.decompress(response.content))["n"], 2)
            self.assertEqual(
                client.get("/star/hello").text,
                "/star/hello http://testserver/star/hello 2",
            )


if __name__ == "__main__":
    unittest.main()
