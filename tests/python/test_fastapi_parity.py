"""FastAPI-compatible names, options and behaviour (docs/fastapi-parity.md)."""

import asyncio
import dataclasses
import datetime as dt
import enum
import os
import tempfile
import time
import unittest
import uuid
from decimal import Decimal
from pathlib import PurePosixPath
from typing import Annotated, Any

import bessemer
from bessemer import (
    APIRouter,
    App,
    BackgroundTasks,
    Body,
    Cookie,
    Depends,
    File,
    Form,
    Header,
    HTTPException,
    Query,
    Request,
    WebSocket,
    WebSocketException,
    status,
)
from bessemer.background import BackgroundTask
from bessemer.concurrency import run_in_threadpool
from bessemer.datastructures import URL, QueryParams
from bessemer.encoders import jsonable_encoder
from bessemer.exceptions import RequestValidationError, ResponseValidationError
from bessemer.middleware import (
    BaseHTTPMiddleware,
    CORSMiddleware,
    GZipMiddleware,
    Middleware,
    TrustedHostMiddleware,
)
from bessemer.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    ORJSONResponse,
    PlainTextResponse,
    RedirectResponse,
    Response,
    StreamingResponse,
)
from bessemer.routing import APIRoute
from bessemer.staticfiles import StaticFiles
from bessemer.testclient import TestClient
from bessemer.utils import generate_unique_id

try:
    import pydantic
except ImportError:  # pragma: no cover - the CI matrix installs Pydantic.
    pydantic = None


class NamesTests(unittest.TestCase):
    def test_fastapi_import_names(self):
        self.assertIs(APIRouter, bessemer.Router)
        self.assertIs(bessemer.FastAPI, App)
        self.assertIn("FastAPI", bessemer.__all__)
        self.assertIn("APIRouter", bessemer.__all__)
        self.assertIn("WebSocketException", bessemer.__all__)
        self.assertEqual(status.HTTP_201_CREATED, 201)
        self.assertEqual(status.HTTP_422_UNPROCESSABLE_ENTITY, 422)
        self.assertEqual(status.WS_1008_POLICY_VIOLATION, 1008)
        self.assertTrue(issubclass(ORJSONResponse, JSONResponse))

    def test_http_exception_default_detail(self):
        self.assertEqual(HTTPException(404).detail, "Not Found")
        self.assertEqual(HTTPException(418, None).detail, "I'm a Teapot")

    def test_partitioned_cookie(self):
        response = Response()
        response.set_cookie("partitioned-session", "abc", secure=True, partitioned=True)
        (cookie,) = response.headers.getlist("set-cookie")
        self.assertIn("; Partitioned", cookie)
        with self.assertRaises(ValueError):
            Response().set_cookie("a", "b", partitioned=True)

    def test_depends_scope_spellings(self):
        self.assertEqual(Depends(lambda: 1, scope=None).scope, "request")
        self.assertEqual(Depends(lambda: 1, scope="function").scope, "request")
        with self.assertRaises(ValueError):
            Depends(lambda: 1, scope="session")

    def test_parameter_deprecated_spellings(self):
        self.assertEqual(Query(regex="^a").pattern, "^a")
        self.assertEqual(Query(example=3).examples, [3])
        with self.assertRaises(ValueError):
            Query(regex="a", pattern="b")
        with self.assertRaises(ValueError):
            Body(media_type="text/plain")
        self.assertIs(Query(default_factory=list).default_factory, list)
        # Options FastAPI accepts but Bessemer cannot honour fail loudly.
        with self.assertRaises(TypeError):
            Query(max_digits=5)


class OptionsTests(unittest.TestCase):
    def test_route_and_app_openapi_options(self):
        app = App(
            title="Shop",
            summary="A shop",
            terms_of_service="https://example.com/terms",
            contact={"name": "Ops"},
            license_info={"name": "MIT"},
            openapi_tags=[{"name": "items", "description": "Items"}],
            servers=[{"url": "https://api.example.com"}],
            generate_unique_id_function=generate_unique_id,
        )
        router = APIRouter(prefix="/items", tags=["items"], deprecated=True)

        @router.get(
            "/{item_id}",
            responses={404: {"description": "Missing"}},
            response_description="The item",
            openapi_extra={"x-cost": 2},
        )
        async def read_item(
            item_id: int,
            q: Annotated[
                str | None,
                Query(title="Search", examples=["shoe"], json_schema_extra={"x-a": 1}),
            ] = None,
            hidden: Annotated[int, Query(include_in_schema=False)] = 0,
        ):
            return {"id": item_id}

        app.include_router(router, responses={500: {"description": "Boom"}})
        document = app.openapi()
        info = document["info"]
        self.assertEqual(info["summary"], "A shop")
        self.assertEqual(info["license"], {"name": "MIT"})
        self.assertEqual(info["termsOfService"], "https://example.com/terms")
        self.assertEqual(document["servers"], [{"url": "https://api.example.com"}])
        self.assertEqual(document["tags"][0]["name"], "items")
        operation = document["paths"]["/items/{item_id}"]["get"]
        self.assertEqual(operation["operationId"], "read_item_items__item_id__get")
        self.assertTrue(operation["deprecated"])
        self.assertEqual(operation["x-cost"], 2)
        self.assertEqual(operation["responses"]["200"]["description"], "The item")
        self.assertEqual(operation["responses"]["404"]["description"], "Missing")
        self.assertEqual(operation["responses"]["500"]["description"], "Boom")
        names = [parameter["name"] for parameter in operation["parameters"]]
        self.assertEqual(names, ["item_id", "q"])
        q = operation["parameters"][1]["schema"]
        self.assertEqual(q["title"], "Search")
        self.assertEqual(q["examples"], ["shoe"])
        self.assertEqual(q["x-a"], 1)
        (route,) = app.routes
        self.assertIsInstance(route, APIRoute)
        self.assertEqual(route.methods, frozenset({"GET"}))
        with self.assertRaises(dataclasses.FrozenInstanceError):
            route.operation_id = "x"

    def test_extra_success_response_keeps_the_generated_schema(self):
        app = App()
        example = {"application/json": {"example": {"id": 1}}}

        @app.get(
            "/item",
            response_model=dict[str, int],
            responses={200: {"content": example}},
        )
        async def item():
            return {"id": 1}

        response = app.openapi()["paths"]["/item"]["get"]["responses"]["200"]
        media = response["content"]["application/json"]
        self.assertEqual(media["example"], {"id": 1})
        self.assertIn("schema", media)
        self.assertEqual(response["description"], "Successful Response")

    def test_form_media_type_sets_the_documented_encoding(self):
        app = App()

        @app.post("/signup")
        async def signup(
            name: Annotated[str, Form(media_type="multipart/form-data")],
        ):
            return {"name": name}

        body = app.openapi()["paths"]["/signup"]["post"]["requestBody"]
        self.assertEqual(list(body["content"]), ["multipart/form-data"])
        with self.assertRaises(ValueError):
            Form(media_type="text/plain")
        with self.assertRaises(ValueError):
            File(media_type="application/x-www-form-urlencoded")

    def test_compiled_patterns_keep_their_meaning(self):
        import re

        self.assertEqual(Query(pattern=re.compile("^a+$")).pattern, "^a+$")
        with self.assertRaises(ValueError):
            Query(pattern=re.compile("^foo$", re.IGNORECASE))
        with self.assertRaises(ValueError):
            Query(pattern=re.compile(b"^foo$"))

    def test_unsupported_options_fail(self):
        with self.assertRaises(ValueError):
            App(redoc_url="/redoc")
        with self.assertRaises(ValueError):
            App(debug=True)
        with self.assertRaises(ValueError):
            App().add_middleware(CORSMiddleware, allow_origin_regex=".*")

    def test_lifespan_and_event_handlers_are_exclusive(self):
        async def lifespan(app):  # pragma: no cover - never entered
            yield

        app = App(lifespan=lifespan)
        app.on_event("startup")(lambda: None)
        with self.assertRaises(ValueError):
            app.build()

    def test_mounted_app_event_handlers_fail(self):
        outer, inner = App(), App()
        outer.mount("/inner", inner)
        inner.on_event("shutdown")(lambda: None)
        with self.assertRaises(ValueError):
            outer.build()

    def test_constructor_middleware_order(self):
        calls = []

        def recorder(name):
            class Recorder(BaseHTTPMiddleware):
                async def dispatch(self, request, call_next):
                    calls.append(name)
                    return await call_next(request)

            return Recorder

        app = App(
            middleware=[Middleware(recorder("first")), Middleware(recorder("second"))]
        )

        @app.get("/")
        async def root():
            return {}

        with TestClient(app) as client:
            client.get("/")
        self.assertEqual(calls, ["first", "second"])

    def test_native_middleware_classes_configure_settings(self):
        app = App(
            middleware=[
                Middleware(
                    CORSMiddleware,
                    allow_origins=["https://a.example"],
                    allow_methods=["*"],
                )
            ]
        )
        app.add_middleware(GZipMiddleware, minimum_size=100)
        app.add_middleware(TrustedHostMiddleware, allowed_hosts=["testserver"])
        settings = app._export_settings()
        self.assertEqual(settings["cors"]["origins"], ["https://a.example"])
        self.assertEqual(settings["compression"]["min_size"], 100)
        self.assertFalse(settings["compression"]["br"])
        self.assertEqual(settings["allowed_hosts"], ["testserver"])
        with self.assertRaises(ValueError):
            app.add_middleware(CORSMiddleware, allow_origins=["https://b.example"])


def parity_app(events, directory):
    started = []
    app = App(on_startup=[lambda: started.append("sync")])

    @app.on_event("startup")
    async def ready():
        started.append("async")

    @app.get("/started")
    async def startup_state():
        return started

    @app.get("/request")
    async def inspect(request: Request):
        return {
            "path": request.url.path,
            "host": request.url.hostname,
            "tags": request.query_params.getlist("tag"),
            "last": request.query_params["tag"],
            "client": request.client.host,
            "base": str(request.base_url),
        }

    @app.post("/form")
    async def form(request: Request):
        data = await request.form()
        upload = data.get("upload")
        return {
            "name": data["name"],
            "names": data.getlist("name"),
            "file": None
            if upload is None
            else [upload.filename, (await upload.read()).decode()],
        }

    @app.post("/stream")
    async def stream(request: Request):
        chunks = [chunk async for chunk in request.stream()]
        return {"chunks": [chunk.decode() for chunk in chunks]}

    @app.get("/page", response_class=HTMLResponse)
    async def page():
        return "<h1>hi</h1>"

    @app.get("/text", response_class=PlainTextResponse, status_code=201)
    def text():
        return "plain"

    async def deferred_text():
        return "deferred"

    @app.get("/sync-awaitable", response_class=PlainTextResponse)
    def sync_awaitable():
        return deferred_text()

    @app.get("/go", response_class=RedirectResponse)
    async def go():
        return "/page"

    @app.get("/count", response_class=PlainTextResponse, response_model=str)
    async def count(bad: bool = False):
        return 3 if bad else "three"

    plain = APIRouter(default_response_class=PlainTextResponse)

    class VendorJSON(JSONResponse):
        media_type = "application/vnd.example+json"

    class CreatedJSON(JSONResponse):
        def __init__(self, content, status_code=201, **options):
            super().__init__(content, status_code=status_code, **options)

    @app.get("/created", response_class=CreatedJSON)
    async def created():
        return {"created": True}

    @app.get("/vendor", response_class=VendorJSON)
    async def vendor():
        return {"vendor": True}

    @plain.get("/plain-json", response_class=JSONResponse)
    async def plain_json():
        return {"json": True}

    app.include_router(plain)

    @app.get("/shared")
    async def shared(tasks: BackgroundTasks):
        tasks.add_task(events.append, "once")
        return JSONResponse({"ok": True}, background=tasks)

    @app.get("/background")
    async def background():
        task = BackgroundTask(events.append, "response task")
        return JSONResponse({"ok": True}, background=task)

    @app.get("/both")
    async def both(tasks: BackgroundTasks):
        tasks.add_task(events.append, "request task")
        return Response("x", background=BackgroundTask(events.append, "second"))

    @app.get("/file")
    async def file():
        return FileResponse(os.path.join(directory, "data.txt"), filename="naïve.txt")

    @app.get("/file-temp")
    def file_temp():
        path = os.path.join(directory, "ephemeral.txt")
        with open(path, "w") as handle:
            handle.write("ephemeral payload")
        return FileResponse(
            path,
            background=BackgroundTask(os.unlink, path),
        )

    @app.get("/teapot")
    async def teapot():
        raise HTTPException(status.HTTP_418_IM_A_TEAPOT)

    @app.get("/gone")
    async def gone():
        raise HTTPException(410, "gone")

    @app.get("/crash")
    async def crash():
        raise RuntimeError("bug")

    @app.exception_handler(410)
    async def gone_handler(request, error):
        return JSONResponse({"status": error.status_code}, status_code=410)

    app.add_exception_handler(
        500, lambda request, error: PlainTextResponse("sorry", status_code=500)
    )

    @app.get("/threads")
    async def threads():
        return {"value": await run_in_threadpool(sum, [1, 2, 3])}

    class Stamp(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            response = await call_next(request)
            response.headers["X-Stamp"] = "1"
            return response

    app.add_middleware(Stamp)

    @app.middleware("http")
    async def short_circuit(request, call_next):
        if request.headers.get("x-block"):
            return PlainTextResponse(
                "blocked", background=BackgroundTask(events.append, "middleware")
            )
        response = await call_next(request)
        if request.headers.get("x-audit"):
            response.background = BackgroundTask(events.append, "audit")
        return response

    @app.get("/stream-out")
    async def stream_out():
        async def body():
            yield b"a"
            yield b"b"
            events.append("streamed")

        return StreamingResponse(body(), media_type="text/plain")

    @app.get("/handled")
    async def handled():
        raise HTTPException(409)

    @app.exception_handler(409)
    async def conflict(request, error):
        return PlainTextResponse(
            "conflict",
            status_code=409,
            background=BackgroundTask(events.append, "handler"),
        )

    app.mount("/static", StaticFiles(directory=directory), name="static")

    @app.websocket("/ws")
    async def ws(websocket: WebSocket):
        if websocket.query_params.get("deny"):
            raise WebSocketException(status.WS_1008_POLICY_VIOLATION)
        await websocket.accept()
        await websocket.send_json({"path": websocket.url.path})
        raise WebSocketException(4001, "done")

    def add(a: int, b: int = 1):
        return {"sum": a + b}

    app.add_api_route("/add", add, methods=["GET", "POST"])
    return app


class BehaviourTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory()
        with open(os.path.join(cls.directory.name, "data.txt"), "w") as file:
            file.write("file contents")
        cls.events = []
        cls.client = TestClient(parity_app(cls.events, cls.directory.name))
        cls.client.start()

    @classmethod
    def tearDownClass(cls):
        cls.client.close()
        cls.directory.cleanup()

    def setUp(self):
        self.events.clear()

    def test_startup_handlers_run(self):
        self.assertEqual(self.client.get("/started").json(), ["sync", "async"])

    def test_request_url_query_params_and_client(self):
        body = self.client.get("/request?tag=a&tag=b").json()
        self.assertEqual(body["path"], "/request")
        self.assertEqual(body["host"], "testserver")
        self.assertEqual(body["tags"], ["a", "b"])
        self.assertEqual(body["last"], "b")
        self.assertEqual(body["client"], "127.0.0.1")
        self.assertEqual(body["base"], "http://testserver/")

    def test_request_form_and_stream(self):
        response = self.client.post("/form", data={"name": "x"})
        self.assertEqual(response.json(), {"name": "x", "names": ["x"], "file": None})
        response = self.client.post(
            "/form",
            data={"name": "y"},
            files={"upload": ("a.txt", b"abc", "text/plain")},
        )
        self.assertEqual(response.json()["file"], ["a.txt", "abc"])
        # "--<boundary>" inside a file is content, not a delimiter.
        tricky = b"hello--bworld\r\n--bx"
        response = self.client.post(
            "/form",
            content=b'--b\r\ncontent-disposition: form-data; name="name"\r\n\r\nz'
            b'\r\n--b\r\ncontent-disposition: form-data; name="upload"; '
            b'filename="t.txt"\r\ncontent-type: text/plain\r\n\r\n'
            + tricky
            + b"\r\n--b--\r\n",
            headers={"content-type": "multipart/form-data; boundary=b"},
        )
        self.assertEqual(response.json()["file"], ["t.txt", tricky.decode()])
        many = b"".join(
            b'--b\r\ncontent-disposition: form-data; name="f"\r\n\r\nv\r\n'
            for _ in range(1001)
        )
        response = self.client.post(
            "/form",
            content=many + b"--b--\r\n",
            headers={"content-type": "multipart/form-data; boundary=b"},
        )
        self.assertEqual(response.status_code, 400)
        # LF-only line endings are not multipart's grammar, as in the native
        # parser, so they cannot slip past the part limit.
        response = self.client.post(
            "/form",
            content=many.replace(b"\r\n", b"\n") + b"--b--\n",
            headers={"content-type": "multipart/form-data; boundary=b"},
        )
        self.assertEqual(response.status_code, 400)
        bloated = b"--b\r\nx-pad: " + b"a" * 9000 + b"\r\n\r\nv\r\n--b--\r\n"
        response = self.client.post(
            "/form",
            content=bloated,
            headers={"content-type": "multipart/form-data; boundary=b"},
        )
        self.assertEqual(response.status_code, 400)
        response = self.client.post(
            "/form",
            content="&".join(["f=v"] * 1001),
            headers={"content-type": "application/x-www-form-urlencoded"},
        )
        self.assertEqual(response.status_code, 400)
        response = self.client.post("/stream", content=b"body")
        self.assertEqual(response.json(), {"chunks": ["body", ""]})

    def test_response_class(self):
        response = self.client.get("/page")
        self.assertEqual(response.text, "<h1>hi</h1>")
        self.assertTrue(response.headers["content-type"].startswith("text/html"))
        response = self.client.get("/text")
        self.assertEqual((response.status_code, response.text), (201, "plain"))
        self.assertEqual(self.client.get("/sync-awaitable").text, "deferred")
        response = self.client.get("/go", follow_redirects=False)
        self.assertEqual(response.status_code, 307)
        paths = self.client.get("/openapi.json").json()["paths"]
        self.assertEqual(list(paths["/go"]["get"]["responses"]), ["307"])
        self.assertIn("201", paths["/text"]["get"]["responses"])
        self.assertEqual(response.headers["location"], "/page")

    def test_response_class_with_model_and_json_override(self):
        self.assertEqual(self.client.get("/count").text, "three")
        self.assertEqual(self.client.get("/count?bad=true").status_code, 500)
        self.assertEqual(self.client.get("/plain-json").json(), {"json": True})
        response = self.client.get("/vendor")
        self.assertEqual(response.json(), {"vendor": True})
        self.assertEqual(
            response.headers["content-type"], "application/vnd.example+json"
        )
        response = self.client.get("/created")
        self.assertEqual(
            (response.status_code, response.json()), (201, {"created": True})
        )

    def assert_background(self, expected):
        deadline = time.monotonic() + 2
        while len(self.events) < len(expected) and time.monotonic() < deadline:
            time.sleep(0.001)
        self.assertEqual(self.events, expected)

    def test_background_on_handler_and_middleware_responses(self):
        self.assertEqual(self.client.get("/handled").text, "conflict")
        response = self.client.get("/threads", headers={"x-block": "1"})
        self.assertEqual(response.text, "blocked")
        self.assert_background(["handler", "middleware"])
        self.events.clear()
        response = self.client.get("/background", headers={"x-audit": "1"})
        self.assertEqual(response.json(), {"ok": True})
        self.assert_background(["response task", "audit"])
        self.events.clear()
        response = self.client.get("/stream-out", headers={"x-audit": "1"})
        self.assertEqual(response.text, "ab")
        self.assert_background(["streamed", "audit"])

    def test_injected_tasks_attached_to_response_run_once(self):
        self.assertEqual(self.client.get("/shared").json(), {"ok": True})
        self.assert_background(["once"])

    def test_response_background(self):
        self.assertEqual(self.client.get("/background").json(), {"ok": True})
        self.assert_background(["response task"])
        self.events.clear()
        self.assertEqual(self.client.get("/both").text, "x")
        self.assert_background(["request task", "second"])

    def test_file_response(self):
        response = self.client.get("/file")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.text, "file contents")
        self.assertEqual(
            response.headers["content-disposition"],
            "attachment; filename*=utf-8''na%C3%AFve.txt",
        )
        self.assertEqual(response.headers["accept-ranges"], "bytes")
        etag = response.headers["etag"]
        last_modified = response.headers["last-modified"]
        head = self.client.head("/file")
        self.assertEqual((head.status_code, head.text), (200, ""))
        self.assertEqual(head.headers["content-length"], "13")
        not_mod_etag = self.client.get("/file", headers={"if-none-match": etag})
        self.assertEqual((not_mod_etag.status_code, not_mod_etag.text), (304, ""))
        not_mod_date = self.client.get(
            "/file", headers={"if-modified-since": last_modified}
        )
        self.assertEqual((not_mod_date.status_code, not_mod_date.text), (304, ""))
        partial = self.client.get("/file", headers={"range": "bytes=0-3"})
        self.assertEqual((partial.status_code, partial.text), (206, "file"))
        self.assertEqual(partial.headers["content-range"], "bytes 0-3/13")
        suffix = self.client.get("/file", headers={"range": "bytes=-8"})
        self.assertEqual((suffix.status_code, suffix.text), (206, "contents"))
        self.assertEqual(suffix.headers["content-range"], "bytes 5-12/13")
        if_range_match = self.client.get(
            "/file", headers={"range": "bytes=0-3", "if-range": etag}
        )
        self.assertEqual(
            (if_range_match.status_code, if_range_match.text), (206, "file")
        )
        if_range_miss = self.client.get(
            "/file", headers={"range": "bytes=0-3", "if-range": '"mismatch"'}
        )
        self.assertEqual(
            (if_range_miss.status_code, if_range_miss.text), (200, "file contents")
        )
        unsat = self.client.get("/file", headers={"range": "bytes=100-200"})
        self.assertEqual((unsat.status_code, unsat.text), (416, ""))
        self.assertEqual(unsat.headers["content-range"], "bytes */13")
        ephemeral = self.client.get("/file-temp")
        self.assertEqual(
            (ephemeral.status_code, ephemeral.text), (200, "ephemeral payload")
        )
        # Background cleanup follows delivery, so receipt can precede unlink.
        ephemeral_path = os.path.join(self.directory.name, "ephemeral.txt")
        deadline = time.monotonic() + 2
        while os.path.exists(ephemeral_path) and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertFalse(os.path.exists(ephemeral_path))
        self.assertEqual(self.client.get("/static/data.txt").text, "file contents")

    def test_exception_handlers_by_status(self):
        response = self.client.get("/teapot")
        self.assertEqual(response.json(), {"detail": "I'm a Teapot"})
        self.assertEqual(self.client.get("/gone").json(), {"status": 410})
        response = self.client.get("/crash")
        self.assertEqual((response.status_code, response.text), (500, "sorry"))

    def test_middleware_and_threadpool(self):
        response = self.client.get("/threads")
        self.assertEqual(response.json(), {"value": 6})
        self.assertEqual(response.headers["x-stamp"], "1")

    def test_add_api_route(self):
        self.assertEqual(self.client.get("/add?a=2").json(), {"sum": 3})
        self.assertEqual(self.client.post("/add?a=2&b=5").json(), {"sum": 7})

    def test_websocket_exception(self):
        with self.client.websocket_connect("/ws") as websocket:
            self.assertEqual(websocket.receive_json(), {"path": "/ws"})
            with self.assertRaises(bessemer.WebSocketDisconnect) as caught:
                websocket.receive_text()
            self.assertEqual(caught.exception.code, 4001)
        with (
            self.assertRaises(bessemer.WebSocketDisconnect),
            self.client.websocket_connect("/ws?deny=1"),
        ):
            pass


class DataStructureTests(unittest.TestCase):
    def test_url_parts(self):
        url = URL("https://user@example.com:8443/a/b?x=1#frag")
        self.assertEqual(url, "https://user@example.com:8443/a/b?x=1#frag")
        self.assertEqual(
            (url.scheme, url.hostname, url.port, url.path, url.query, url.fragment),
            ("https", "example.com", 8443, "/a/b", "x=1", "frag"),
        )
        self.assertTrue(url.is_secure)
        self.assertEqual(url.replace(path="/c").path, "/c")
        self.assertEqual(url.include_query_params(y=2).query, "x=1&y=2")
        self.assertEqual(url.remove_query_params("x").query, "")
        self.assertEqual(
            url.replace(port=9000), "https://user@example.com:9000/a/b?x=1#frag"
        )
        ipv6 = URL("http://[::1]:8000/").replace(port=9000)
        self.assertEqual((ipv6, ipv6.port), ("http://[::1]:9000/", 9000))

    def test_query_params(self):
        params = QueryParams("a=1&a=2&b=")
        self.assertEqual(params["a"], "2")
        self.assertEqual(params.getlist("a"), ["1", "2"])
        self.assertEqual(dict(params), {"a": "2", "b": ""})
        self.assertEqual(str(params), "a=1&a=2&b=")


class Color(enum.Enum):
    RED = "red"


@dataclasses.dataclass
class Point:
    x: int
    when: dt.date


class EncoderTests(unittest.TestCase):
    def test_jsonable_encoder(self):
        identifier = uuid.uuid4()
        value = {
            "color": Color.RED,
            "point": Point(1, dt.date(2026, 1, 2)),
            "ids": {identifier},
            "price": Decimal("1.50"),
            "count": Decimal(3),
            "path": PurePosixPath("/tmp/x"),
            "none": None,
            "delta": dt.timedelta(seconds=90),
        }
        self.assertEqual(
            jsonable_encoder(value, exclude_none=True),
            {
                "color": "red",
                "point": {"x": 1, "when": "2026-01-02"},
                "ids": [str(identifier)],
                "price": 1.5,
                "count": 3,
                "path": "/tmp/x",
                "delta": 90.0,
            },
        )
        self.assertEqual(jsonable_encoder({"a": 1, "b": 2}, include={"a"}), {"a": 1})

    @unittest.skipIf(pydantic is None, "Pydantic is not installed")
    def test_jsonable_encoder_models(self):
        class Item(pydantic.BaseModel):
            name: str
            tags: list[str] = pydantic.Field(default_factory=list)
            note: str | None = None

        self.assertEqual(
            jsonable_encoder(Item(name="a"), exclude_unset=True), {"name": "a"}
        )
        self.assertEqual(
            jsonable_encoder([Item(name="a")], exclude_none=True),
            [{"name": "a", "tags": []}],
        )

    def test_jsonable_encoder_filters_sequence_items(self):
        self.assertEqual(
            jsonable_encoder([{"public": 1, "secret": "t"}], exclude={"secret"}),
            [{"public": 1}],
        )

    def test_run_in_threadpool(self):
        self.assertEqual(asyncio.run(run_in_threadpool(max, 1, 4)), 4)

    def test_cancelled_threadpool_call_waits_for_the_thread(self):
        import threading

        release = threading.Event()
        finished = []

        def blocking():
            release.wait(5)
            finished.append(True)

        async def main():
            task = asyncio.ensure_future(run_in_threadpool(blocking))
            await asyncio.sleep(0.05)
            task.cancel()
            await asyncio.sleep(0.05)
            self.assertFalse(task.done())
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertEqual(finished, [True])

        asyncio.run(main())


class ExceptionsModuleTests(unittest.TestCase):
    def test_exceptions_module(self):
        from bessemer import exceptions

        self.assertIs(exceptions.RequestValidationError, RequestValidationError)
        self.assertIs(exceptions.ResponseValidationError, ResponseValidationError)
        self.assertIs(bessemer.ResponseValidationError, ResponseValidationError)
        self.assertIs(exceptions.WebSocketException, WebSocketException)

    def test_response_validation_error_handler_and_default_500(self):
        app = App()

        @app.get("/bad", response_model=dict[str, int])
        async def bad():
            return {"count": "not-an-int"}

        @app.get("/bad-text", response_class=PlainTextResponse, response_model=str)
        async def bad_text():
            return 123

        with TestClient(app) as client:
            self.assertEqual(client.get("/bad").status_code, 500)
            self.assertEqual(client.get("/bad-text").status_code, 500)

        handled_app = App()

        @handled_app.exception_handler(ResponseValidationError)
        async def handle_response_error(request, exc: ResponseValidationError):
            return JSONResponse(
                {"errors": exc.errors(), "body": exc.body}, status_code=422
            )

        @handled_app.get("/bad", response_model=dict[str, int])
        async def bad_handled():
            return {"count": "not-an-int"}

        @handled_app.get(
            "/bad-text", response_class=PlainTextResponse, response_model=str
        )
        async def bad_text_handled():
            return 123

        with TestClient(handled_app) as client:
            response = client.get("/bad")
            self.assertEqual(response.status_code, 422)
            payload = response.json()
            self.assertEqual(payload["body"], {"count": "not-an-int"})
            self.assertEqual(payload["errors"][0]["loc"], ["response", "count"])
            text_response = client.get("/bad-text")
            self.assertEqual(text_response.status_code, 422)
            self.assertEqual(text_response.json()["body"], 123)
            self.assertEqual(text_response.json()["errors"][0]["loc"], ["response"])

    def test_websocket_exception_accepts_native_close_codes(self):
        for code in (1012, 1013, 1014):
            self.assertEqual(WebSocketException(code).code, code)
        with self.assertRaises(ValueError):
            WebSocketException(1005)


class EventHandlerTests(unittest.TestCase):
    def test_shutdown_hooks_run_when_teardown_fails(self):
        events = []
        app = App(on_startup=[lambda: events.append("up")])
        app.on_event("shutdown")(lambda: events.append("down"))

        async def run():
            context = app.lifespan(app)
            await context.__aenter__()
            error = RuntimeError("teardown")
            # The error is not swallowed: __aexit__ declines to suppress it.
            self.assertFalse(await context.__aexit__(RuntimeError, error, None))

        asyncio.run(run())
        self.assertEqual(events, ["up", "down"])


class ParameterAndFieldParityTests(unittest.TestCase):
    def test_annotated_field_and_parameter_options(self):
        from typing import Literal

        from pydantic import BaseModel, Field

        class Cat(BaseModel):
            pet_type: Literal["cat"]
            meows: int

        class Dog(BaseModel):
            pet_type: Literal["dog"]
            barks: float

        counter = 0

        def next_id() -> int:
            nonlocal counter
            counter += 1
            return counter

        app = App()

        @app.get("/field/{item_id}")
        async def read_field(
            item_id: Annotated[int, Field(gt=0, multiple_of=2)],
            q: Annotated[
                str,
                Field(
                    min_length=2,
                    validation_alias="q-in",
                    serialization_alias="q-out",
                    description="Query text",
                ),
            ] = "ok",
            seq: Annotated[int, Query(default_factory=next_id)] = ...,
            strict_int: Annotated[int, Query(strict=True)] = 7,
            strict_str: Annotated[str, Query(strict=True)] = "yes",
        ):
            return {
                "item_id": item_id,
                "q": q,
                "seq": seq,
                "strict_int": strict_int,
                "strict_str": strict_str,
            }

        @app.post("/pets")
        async def create_pet(
            pet: Annotated[Cat | Dog, Body(discriminator="pet_type")],
        ):
            return {"type": pet.pet_type}

        @app.post("/pets-embed")
        async def create_pet_embed(
            pet: Annotated[Cat | Dog, Body(embed=True, discriminator="pet_type")],
            count: Annotated[
                int,
                Body(
                    embed=True,
                    multiple_of=3,
                    strict=True,
                    validation_alias="cnt",
                    default_factory=lambda: 6,
                ),
            ] = ...,
        ):
            return {"type": pet.pet_type, "count": count}

        openapi = app.openapi()
        params = {
            p["name"]: p
            for p in openapi["paths"]["/field/{item_id}"]["get"]["parameters"]
        }
        self.assertEqual(params["item_id"]["schema"]["multipleOf"], 2)
        self.assertIn("q-in", params)
        self.assertNotIn("default", params["seq"]["schema"])
        self.assertEqual(params["strict_int"]["schema"]["default"], 7)

        with TestClient(app) as client:
            r = client.get("/field/4?q-in=hello&strict_str=abc")
            self.assertEqual(r.status_code, 200)
            self.assertEqual(
                r.json(),
                {
                    "item_id": 4,
                    "q": "hello",
                    "seq": 1,
                    "strict_int": 7,
                    "strict_str": "abc",
                },
            )
            r2 = client.get("/field/4")
            self.assertEqual(r2.json()["seq"], 2)
            # multiple_of failure
            self.assertEqual(client.get("/field/3").status_code, 422)
            # strict=True on int query param rejects string input from query
            strict_fail = client.get("/field/4?strict_int=9")
            self.assertEqual(strict_fail.status_code, 422)
            self.assertEqual(
                strict_fail.json()["detail"][0]["loc"], ["query", "strict_int"]
            )
            # discriminated union body
            cat_resp = client.post("/pets", json={"pet_type": "cat", "meows": 3})
            self.assertEqual(cat_resp.status_code, 200)
            self.assertEqual(cat_resp.json(), {"type": "cat"})
            bad_pet = client.post("/pets", json={"pet_type": "bird", "meows": 3})
            self.assertEqual(bad_pet.status_code, 422)
            # embedded discriminated union + multiple_of + strict + validation_alias + default_factory
            embed_ok = client.post(
                "/pets-embed",
                json={"pet": {"pet_type": "dog", "barks": 1.5}},
            )
            self.assertEqual(embed_ok.status_code, 200)
            self.assertEqual(embed_ok.json(), {"type": "dog", "count": 6})
            embed_strict_fail = client.post(
                "/pets-embed",
                json={"pet": {"pet_type": "dog", "barks": 1.5}, "cnt": "9"},
            )
            self.assertEqual(embed_strict_fail.status_code, 422)
            embed_mult_fail = client.post(
                "/pets-embed",
                json={"pet": {"pet_type": "dog", "barks": 1.5}, "cnt": 4},
            )
            self.assertEqual(embed_mult_fail.status_code, 422)

        bad_app = App()

        @bad_app.get("/unsupported")
        async def unsupported(x: Annotated[float, Field(allow_inf_nan=False)]):
            return {"x": x}

        with self.assertRaises(ValueError):
            bad_app.build()

    def test_query_pydantic_model(self):
        from typing import Literal

        from pydantic import BaseModel, Field

        class FilterParams(BaseModel):
            model_config = {"extra": "forbid"}
            limit: int = Field(100, gt=0, le=100, description="Max results")
            offset: int = Field(0, ge=0, validation_alias="off")
            order_by: Literal["created_at", "updated_at"] = "created_at"
            tags: list[str] = Field(default_factory=list)

        app = App()

        @app.get("/items")
        async def read_items(filter_query: Annotated[FilterParams, Query()]):
            return filter_query.model_dump()

        openapi_params = {
            p["name"]: p for p in app.openapi()["paths"]["/items"]["get"]["parameters"]
        }
        self.assertEqual(set(openapi_params), {"limit", "off", "order_by", "tags"})
        self.assertEqual(openapi_params["limit"]["description"], "Max results")
        self.assertEqual(openapi_params["limit"]["schema"]["default"], 100)
        self.assertNotIn("default", openapi_params["tags"]["schema"])

        with TestClient(app) as client:
            r = client.get("/items?limit=10&off=5&tags=a&tags=b")
            self.assertEqual(r.status_code, 200)
            self.assertEqual(
                r.json(),
                {
                    "limit": 10,
                    "offset": 5,
                    "order_by": "created_at",
                    "tags": ["a", "b"],
                },
            )
            # Defaults when omitted
            r_def = client.get("/items")
            self.assertEqual(r_def.status_code, 200)
            self.assertEqual(
                r_def.json(),
                {
                    "limit": 100,
                    "offset": 0,
                    "order_by": "created_at",
                    "tags": [],
                },
            )
            # Extra forbidden query parameter -> 422 with loc ["query", "unknown"]
            r_extra = client.get("/items?unknown=1")
            self.assertEqual(r_extra.status_code, 422)
            self.assertEqual(r_extra.json()["detail"][0]["loc"], ["query", "unknown"])
            # Constraint violation -> 422 with loc ["query", "limit"]
            r_bad = client.get("/items?limit=0")
            self.assertEqual(r_bad.status_code, 422)
            self.assertEqual(r_bad.json()["detail"][0]["loc"], ["query", "limit"])

    def test_redirect_slashes_app_and_router(self):
        app = App()

        @app.get("/bare")
        async def bare(q: str = "x"):
            return {"route": "bare", "q": q}

        @app.get("/slashed/")
        async def slashed():
            return {"route": "slashed"}

        strict_router = APIRouter(prefix="/strict", redirect_slashes=False)

        @strict_router.get("/items")
        async def strict_items():
            return {"strict": True}

        app.include_router(strict_router)

        with TestClient(app) as client:
            r1 = client.get("/bare/?q=hello", follow_redirects=False)
            self.assertEqual(r1.status_code, 307)
            self.assertEqual(r1.headers["location"], "/bare?q=hello")
            self.assertEqual(
                client.get("/bare/?q=hello").json(), {"route": "bare", "q": "hello"}
            )
            r2 = client.get("/slashed", follow_redirects=False)
            self.assertEqual(r2.status_code, 307)
            self.assertEqual(r2.headers["location"], "/slashed/")
            self.assertEqual(client.get("/slashed").json(), {"route": "slashed"})
            # Router with redirect_slashes=False does not redirect
            self.assertEqual(client.get("/strict/items/").status_code, 404)
            self.assertEqual(client.get("/strict/items").json(), {"strict": True})

        no_redirect = App(redirect_slashes=False)

        @no_redirect.get("/bare")
        async def nr_bare():
            return {"ok": True}

        with TestClient(no_redirect) as client:
            self.assertEqual(client.get("/bare/").status_code, 404)
            self.assertEqual(client.get("/bare").json(), {"ok": True})

        with self.assertRaises(TypeError):
            App(redirect_slashes="yes")
        with self.assertRaises(TypeError):
            APIRouter(redirect_slashes="yes")

    def test_root_path_and_root_path_in_servers(self):
        app = App(root_path="/api/v1", docs_url="/docs")

        @app.get("/info", name="info")
        async def info(request: Request):
            return {
                "root_path": request.scope["root_path"],
                "base_url": str(request.base_url),
                "url_for": str(request.url_for("info")),
            }

        openapi = app.openapi()
        self.assertEqual(openapi["servers"], [{"url": "/api/v1"}])

        with TestClient(app) as client:
            r = client.get("/info")
            self.assertEqual(
                r.json(),
                {
                    "root_path": "/api/v1",
                    "base_url": "http://testserver/api/v1/",
                    "url_for": "http://testserver/api/v1/info",
                },
            )
            docs = client.get("/docs")
            self.assertIn("/api/v1/openapi.json", docs.text)

        no_server_app = App(
            root_path="/api/v1",
            root_path_in_servers=False,
            servers=[{"url": "https://example.com"}],
        )

        @no_server_app.get("/ping")
        async def ping():
            return {"ok": True}

        self.assertEqual(
            no_server_app.openapi()["servers"], [{"url": "https://example.com"}]
        )
        with self.assertRaises(TypeError):
            App(root_path=123)
        with self.assertRaises(TypeError):
            App(root_path_in_servers="no")

    def test_request_receive_and_is_disconnected(self):
        import asyncio
        import http.client
        import threading

        events = []
        app = App()

        @app.post("/recv")
        async def recv_endpoint(request: Request):
            disc = await request.is_disconnected()
            fn = request.receive
            msg = await fn()
            return {
                "disconnected": disc,
                "type": msg["type"],
                "body": msg["body"].decode(),
                "more_body": msg["more_body"],
            }

        @app.get("/watch-disconnect")
        async def watch_disconnect(request: Request):
            first = await request.receive()
            events.append(("started", first["type"], await request.is_disconnected()))
            second = await request.receive()
            events.append(
                ("disconnected", second["type"], await request.is_disconnected())
            )
            return {"ok": True}

        @app.get("/poll-disconnect")
        async def poll_disconnect(request: Request):
            events.append("poll-started")
            while not await request.is_disconnected():
                pass
            events.append(("poll-disconnected", await request.is_disconnected()))
            return {"ok": True}

        @app.get("/deadline-is-disconnected", timeout=0.05)
        async def deadline_is_disconnected(request: Request):
            try:
                while not await request.is_disconnected():
                    pass
            except asyncio.CancelledError:
                events.append(
                    ("deadline-is-disconnected-cancelled", request._disconnected)
                )
                raise

        @app.get("/deadline-receive", timeout=0.05)
        async def deadline_receive(request: Request):
            try:
                while True:
                    await request.receive()
            except asyncio.CancelledError:
                events.append(("deadline-receive-cancelled", request._disconnected))
                raise

        with TestClient(app, server_options={"max_in_flight": 1}) as client:
            r = client.post("/recv", content=b"hello-world")
            self.assertEqual(
                r.json(),
                {
                    "disconnected": False,
                    "type": "http.request",
                    "body": "hello-world",
                    "more_body": False,
                },
            )

            waiter = threading.Event()
            conn = http.client.HTTPConnection("127.0.0.1", client._port)
            conn.request("GET", "/watch-disconnect")
            for _ in range(500):
                if events:
                    break
                waiter.wait(0.01)
            conn.sock.close()
            conn.close()
            for _ in range(500):
                if len(events) >= 2:
                    break
                waiter.wait(0.01)
            self.assertEqual(
                events,
                [
                    ("started", "http.request", False),
                    ("disconnected", "http.disconnect", True),
                ],
            )

            events.clear()
            conn = http.client.HTTPConnection("127.0.0.1", client._port)
            conn.request("GET", "/poll-disconnect")
            for _ in range(500):
                if events:
                    break
                waiter.wait(0.01)
            conn.sock.close()
            conn.close()
            for _ in range(500):
                if len(events) >= 2:
                    break
                waiter.wait(0.01)
            self.assertEqual(
                events,
                [
                    "poll-started",
                    ("poll-disconnected", True),
                ],
            )

            # A deadline expiring while awaiting is_disconnected() or receive()
            # in a loop still produces 504, raises CancelledError without setting
            # disconnected, and ends the task inside capacity accounting.
            events.clear()
            self.assertEqual(client.get("/deadline-is-disconnected").status_code, 504)
            for _ in range(500):
                if events:
                    break
                waiter.wait(0.01)
            self.assertEqual(events, [("deadline-is-disconnected-cancelled", False)])
            self.assertEqual(client.post("/recv", content=b"ok").status_code, 200)

            events.clear()
            self.assertEqual(client.get("/deadline-receive").status_code, 504)
            for _ in range(500):
                if events:
                    break
                waiter.wait(0.01)
            self.assertEqual(events, [("deadline-receive-cancelled", False)])
            self.assertEqual(client.post("/recv", content=b"ok").status_code, 200)

        # Worker shutdown is not swallowed while awaiting is_disconnected() or receive().
        def check_shutdown(mode: str) -> None:
            shutdown_events = []
            shutdown_app = App()

            @shutdown_app.get("/hold")
            async def hold(request: Request):
                shutdown_events.append("started")
                try:
                    if mode == "is_disconnected":
                        while not await request.is_disconnected():
                            pass
                    else:
                        while True:
                            await request.receive()
                except asyncio.CancelledError:
                    shutdown_events.append(("cancelled", request._disconnected))
                    raise

            client = TestClient(
                shutdown_app,
                server_options={"graceful_timeout": 0.05},
            )
            client.__enter__()
            conn = http.client.HTTPConnection("127.0.0.1", client._port, timeout=5)
            try:
                conn.request("GET", "/hold")
                for _ in range(500):
                    if shutdown_events:
                        break
                    time.sleep(0.01)
                self.assertEqual(shutdown_events, ["started"])
            finally:
                # Close the worker while the handler is still looping; if
                # shutdown cancellation were swallowed, this would stall.
                started_close = time.monotonic()
                client.__exit__(None, None, None)
                elapsed_close = time.monotonic() - started_close
                conn.close()
            self.assertEqual(
                shutdown_events,
                ["started", ("cancelled", False)],
            )
            self.assertLess(elapsed_close, 2.5)

        for mode in ("is_disconnected", "receive"):
            with self.subTest(shutdown_mode=mode):
                check_shutdown(mode)

    def test_websocket_accept_headers_raw_send_and_states(self):
        from bessemer.websockets import WebSocketDisconnect, WebSocketState

        seen_states = []
        app = App()

        @app.websocket("/ws-parity")
        async def ws_parity(websocket: WebSocket):
            seen_states.append(
                (
                    websocket.client_state,
                    websocket.application_state,
                    websocket.scope["type"],
                )
            )
            await websocket.accept(
                subprotocol="chat",
                headers=[
                    (b"x-ws-custom", b"accepted"),
                    (b"x-ws-multi", b"one"),
                    (b"x-ws-multi", b"two"),
                ],
            )
            seen_states.append((websocket.client_state, websocket.application_state))
            bin_msg = await websocket.receive_json(mode="binary")
            txt_msg = await websocket.receive_json(mode="text")
            await websocket.send(
                {"type": "websocket.send", "text": f"{bin_msg['a']}:{txt_msg['b']}"}
            )
            await websocket.send({"type": "websocket.send", "bytes": b"raw-bytes"})
            await websocket.send(
                {"type": "websocket.close", "code": 1000, "reason": "done"}
            )
            seen_states.append((websocket.client_state, websocket.application_state))

        with (
            TestClient(app) as client,
            client.websocket_connect("/ws-parity", subprotocols=["chat"]) as session,
        ):
            self.assertEqual(session.accepted_subprotocol, "chat")
            self.assertEqual(session.headers["x-ws-custom"], "accepted")
            self.assertIn((b"x-ws-custom", b"accepted"), session.extra_headers)
            session.send_bytes(b'{"a": 1}')
            session.send_text('{"b": 2}')
            self.assertEqual(session.receive_text(), "1:2")
            self.assertEqual(session.receive_bytes(), b"raw-bytes")
            with self.assertRaises(WebSocketDisconnect) as ctx:
                session.receive_text()
            self.assertEqual(ctx.exception.code, 1000)

        self.assertEqual(
            seen_states,
            [
                (
                    WebSocketState.CONNECTING,
                    WebSocketState.CONNECTING,
                    "websocket",
                ),
                (WebSocketState.CONNECTED, WebSocketState.CONNECTED),
                (WebSocketState.CONNECTED, WebSocketState.DISCONNECTED),
            ],
        )

    def test_app_router_and_router_lifespan(self):
        from contextlib import asynccontextmanager

        from bessemer import FastAPI

        events = []

        @asynccontextmanager
        async def app_lifespan(app_instance):
            events.append(("app_start", isinstance(app_instance, App)))
            yield {"from_app": 1, "shared": "app"}
            events.append("app_stop")

        @asynccontextmanager
        async def outer_router_lifespan(app_instance):
            events.append(("outer_router_start", isinstance(app_instance, App)))
            yield {"from_outer": 2, "shared": "outer", "router_shared": "outer"}
            events.append("outer_router_stop")

        @asynccontextmanager
        async def inner_router_lifespan(app_instance):
            events.append(("inner_router_start", isinstance(app_instance, App)))
            yield {"from_inner": 3, "shared": "inner", "router_shared": "inner"}
            events.append("inner_router_stop")

        app = FastAPI(lifespan=app_lifespan)
        self.assertIs(app.router, app)

        outer_router = APIRouter(prefix="/outer", lifespan=outer_router_lifespan)
        inner_router = APIRouter(prefix="/inner", lifespan=inner_router_lifespan)

        @inner_router.get("/state", name="inner_state")
        async def read_state(request: Request):
            return {
                "from_app": request.state.from_app,
                "from_outer": request.state.from_outer,
                "from_inner": request.state.from_inner,
                "shared": request.state.shared,
                "router_shared": request.state.router_shared,
            }

        outer_router.include_router(inner_router)
        app.router.include_router(outer_router)
        self.assertEqual(app.router.url_path_for("inner_state"), "/outer/inner/state")
        self.assertEqual(len(app.router.routes), 1)

        with TestClient(app) as client:
            r = client.get("/outer/inner/state")
            self.assertEqual(
                r.json(),
                {
                    "from_app": 1,
                    "from_outer": 2,
                    "from_inner": 3,
                    "shared": "app",
                    "router_shared": "outer",
                },
            )
        self.assertEqual(
            events,
            [
                ("app_start", True),
                ("outer_router_start", True),
                ("inner_router_start", True),
                "inner_router_stop",
                "outer_router_stop",
                "app_stop",
            ],
        )

        bad_router = APIRouter(lifespan=outer_router_lifespan)
        bad_router.on_event("startup")(lambda: None)
        with self.assertRaises(ValueError):
            _ = bad_router.lifespan
        with self.assertRaises(TypeError):
            APIRouter(lifespan="not_callable")


class TutorialRealAppParityTests(unittest.TestCase):
    def test_query_and_header_sequences_and_after_validator(self):
        from bessemer import Header
        from pydantic import AfterValidator

        def check_valid_id(val: str) -> str:
            if not val.startswith("isbn-"):
                raise ValueError("Invalid ID format")
            return val

        app = App()

        @app.get("/items")
        async def read_items(
            q: Annotated[list[str] | None, Query(min_length=1, max_length=3)] = None,
            nums: list[int] = Query(default=[1, 2]),  # noqa: B008
            bare: list = Query(default=[]),  # noqa: B008
            tags: set[str] = Query(default=set()),  # noqa: B008
            x_token: Annotated[list[str] | None, Header()] = None,
            item_id: Annotated[str | None, AfterValidator(check_valid_id)] = None,
        ):
            return {
                "q": q,
                "nums": nums,
                "bare": bare,
                "tags": sorted(tags),
                "x_token": x_token,
                "item_id": item_id,
            }

        with TestClient(app) as client:
            r = client.get(
                "/items?q=foo&q=bar&nums=10&nums=20&bare=x&tags=b&tags=a&tags=b&item_id=isbn-123",
                headers=[("x-token", "tok1"), ("x-token", "tok2")],
            )
            self.assertEqual(r.status_code, 200)
            self.assertEqual(
                r.json(),
                {
                    "q": ["foo", "bar"],
                    "nums": [10, 20],
                    "bare": ["x"],
                    "tags": ["a", "b"],
                    "x_token": ["tok1", "tok2"],
                    "item_id": "isbn-123",
                },
            )
            r_def = client.get("/items")
            self.assertEqual(r_def.status_code, 200)
            self.assertEqual(
                r_def.json(),
                {
                    "q": None,
                    "nums": [1, 2],
                    "bare": [],
                    "tags": [],
                    "x_token": None,
                    "item_id": None,
                },
            )
            # Exceeding max_length on sequence Query fails with 422
            self.assertEqual(client.get("/items?q=a&q=b&q=c&q=d").status_code, 422)
            # Failing AfterValidator fails with 422
            self.assertEqual(client.get("/items?item_id=bad-123").status_code, 422)

    def test_explicit_json_null_runs_optional_body_validators(self):
        from pydantic import AfterValidator, BaseModel, BeforeValidator

        seen = []

        def reject_null(value):
            seen.append(value)
            if value is None:
                raise ValueError("null is not allowed")
            return value

        def fill(value):
            seen.append(value)
            return {"a": 1} if value is None else value

        class Item(BaseModel):
            a: int

        app = App()

        @app.post("/scalar")
        async def scalar(
            x: Annotated[int | None, Body(), AfterValidator(reject_null)] = None,
        ):
            return {"x": x}

        @app.post("/model")
        async def model(
            x: Annotated[Item | None, Body(), BeforeValidator(fill)] = None,
        ):
            return {"x": None if x is None else x.a}

        @app.post("/container")
        async def container(
            x: Annotated[dict | None, Body(), AfterValidator(reject_null)] = None,
        ):
            return {"x": x}

        json = {"content-type": "application/json"}
        with TestClient(app) as client:
            # An explicit null runs the validators, as in FastAPI.
            r = client.post("/scalar", content=b"null", headers=json)
            self.assertEqual(r.status_code, 422)
            self.assertEqual(r.json()["detail"][0]["loc"], ["body", "x"])
            self.assertEqual(seen, [None])
            seen.clear()
            r = client.post("/model", content=b"null", headers=json)
            self.assertEqual((r.status_code, r.json()), (200, {"x": 1}))
            self.assertEqual(seen, [None])
            seen.clear()
            r = client.post("/container", content=b"null", headers=json)
            self.assertEqual(r.status_code, 422)
            self.assertEqual(r.json()["detail"][0]["loc"], ["body"])
            self.assertEqual(seen, [None])
            # An absent body keeps its default without running them.
            for path in ("/scalar", "/model", "/container"):
                seen.clear()
                r = client.post(path)
                self.assertEqual((r.status_code, r.json()), (200, {"x": None}))
                self.assertEqual(seen, [])
            # A value runs them once.
            seen.clear()
            r = client.post("/scalar", content=b"5", headers=json)
            self.assertEqual((r.status_code, r.json()), (200, {"x": 5}))
            self.assertEqual(seen, [5])
            seen.clear()
            r = client.post("/model", content=b'{"a": 3}', headers=json)
            self.assertEqual((r.status_code, r.json()), (200, {"x": 3}))
            self.assertEqual(seen, [{"a": 3}])

    def test_param_models_for_query_header_cookie_and_form(self):
        from datetime import date

        from bessemer import Cookie, Form, Header
        from pydantic import BaseModel, model_validator

        class DateRange(BaseModel):
            start_date: date
            end_date: date

            @model_validator(mode="after")
            def check_dates(self):
                if self.start_date > self.end_date:
                    raise ValueError("start_date must be before end_date")
                return self

        class CommonHeaders(BaseModel):
            model_config = {"extra": "forbid"}
            host: str
            save_data: bool
            if_modified_since: str | None = None
            x_tag: list[str] = []

        class CookiesModel(BaseModel):
            model_config = {"extra": "forbid"}
            session_id: str
            fatebook_tracker: str | None = None

        class FormData(BaseModel):
            model_config = {"extra": "forbid"}
            username: str
            password: str

        app = App()

        @app.get("/range")
        async def get_range(
            date_range: Annotated[DateRange | None, Query()] = None,
        ):
            return date_range

        @app.get("/headers")
        async def get_headers(headers: Annotated[CommonHeaders, Header()]):
            return headers

        @app.get("/cookies")
        async def get_cookies(cookies: Annotated[CookiesModel, Cookie()]):
            return cookies

        @app.post("/login")
        async def login(data: Annotated[FormData, Form()]):
            return data

        with TestClient(app) as client:
            # Optional Query model omitted -> None
            self.assertIsNone(client.get("/range").json())
            # Valid Query model
            r_range = client.get("/range?start_date=2026-01-01&end_date=2026-01-05")
            self.assertEqual(r_range.status_code, 200)
            self.assertEqual(
                r_range.json(),
                {"start_date": "2026-01-01", "end_date": "2026-01-05"},
            )
            # Incomplete Query model -> 422
            self.assertEqual(
                client.get("/range?start_date=2026-01-01").status_code, 422
            )

            # Header model with convert_underscores, list field, and extra="forbid"
            client.headers.clear()
            r_hdr = client.get(
                "/headers",
                headers=[
                    ("save-data", "true"),
                    ("if-modified-since", "yesterday"),
                    ("x-tag", "one"),
                    ("x-tag", "two"),
                ],
            )
            self.assertEqual(r_hdr.status_code, 200)
            self.assertEqual(
                r_hdr.json(),
                {
                    "host": "testserver",
                    "save_data": True,
                    "if_modified_since": "yesterday",
                    "x_tag": ["one", "two"],
                },
            )
            self.assertEqual(
                client.get(
                    "/headers",
                    headers=[("save-data", "true"), ("x-extra", "nope")],
                ).status_code,
                422,
            )

            # Cookie model and client.cookies.set(...)
            client.cookies.set("session_id", "abc-123")
            r_cookie = client.get("/cookies")
            self.assertEqual(r_cookie.status_code, 200)
            self.assertEqual(
                r_cookie.json(),
                {"session_id": "abc-123", "fatebook_tracker": None},
            )
            client.cookies.set("extra_cookie", "bad")
            self.assertEqual(client.get("/cookies").status_code, 422)
            del client.cookies["extra_cookie"]

            # Form model
            r_form = client.post(
                "/login", data={"username": "alice", "password": "secret"}
            )
            self.assertEqual(r_form.status_code, 200)
            self.assertEqual(r_form.json(), {"username": "alice", "password": "secret"})
            self.assertEqual(
                client.post(
                    "/login",
                    data={"username": "alice", "password": "secret", "extra": "1"},
                ).status_code,
                422,
            )

    def test_body_model_containers_and_extra_data_types(self):
        from datetime import time, timedelta
        from decimal import Decimal

        from pydantic import BaseModel

        class Image(BaseModel):
            url: str
            name: str

        app = App()

        @app.post("/images/multiple")
        async def create_multiple_images(images: list[Image]):
            return images

        @app.post("/images/by-key")
        async def create_images_by_key(images: dict[str, Image]):
            return images

        @app.put("/schedule")
        async def update_schedule(
            start_process: Annotated[time, Body()],
            process_after: Annotated[timedelta, Body()],
            price: Annotated[Decimal, Query()] = Decimal("1.50"),
        ):
            return {
                "start_process": start_process,
                "process_after": process_after,
                "price": str(price),
            }

        with TestClient(app) as client:
            imgs = [{"url": "https://example.com/1.png", "name": "one"}]
            r_list = client.post("/images/multiple", json=imgs)
            self.assertEqual(r_list.status_code, 200)
            self.assertEqual(r_list.json(), imgs)
            self.assertEqual(
                client.post("/images/multiple", json={"not": "a list"}).status_code,
                422,
            )

            by_key = {"first": {"url": "https://example.com/1.png", "name": "one"}}
            r_dict = client.post("/images/by-key", json=by_key)
            self.assertEqual(r_dict.status_code, 200)
            self.assertEqual(r_dict.json(), by_key)

            r_sched = client.put(
                "/schedule?price=19.99",
                json={"start_process": "14:30:00", "process_after": 300},
            )
            self.assertEqual(r_sched.status_code, 200)
            self.assertEqual(
                r_sched.json(),
                {
                    "start_process": "14:30:00",
                    "process_after": 300.0,
                    "price": "19.99",
                },
            )

    def test_response_parameter_injection_and_middleware_exception_exports(self):
        from enum import Enum

        from bessemer import Response
        from bessemer.exception_handlers import (
            http_exception_handler,
            request_validation_exception_handler,
        )
        from bessemer.exceptions import FastAPIError, RequestValidationError
        from bessemer.middleware.cors import CORSMiddleware
        from bessemer.middleware.gzip import GZipMiddleware
        from bessemer.middleware.httpsredirect import HTTPSRedirectMiddleware
        from bessemer.middleware.trustedhost import TrustedHostMiddleware
        from starlette.exceptions import HTTPException as StarletteHTTPException

        self.assertTrue(issubclass(FastAPIError, RuntimeError))
        self.assertIsNotNone(CORSMiddleware)
        self.assertIsNotNone(GZipMiddleware)
        self.assertIsNotNone(TrustedHostMiddleware)

        class Tags(Enum):
            items = "items"

        app = App()

        def add_dep_header(response: Response):
            response.headers["X-Dep-Header"] = "from-dep"
            return "dep-ok"

        @app.get("/inject-response", tags=[Tags.items], status_code=200)
        async def inject_response(
            response: Response,
            dep: Annotated[str, Depends(add_dep_header)],
            created: bool = False,
        ):
            response.set_cookie(key="fakesession", value="cookie-val")
            response.headers["X-Handler-Header"] = "from-handler"
            if created:
                response.status_code = 201
            return {"dep": dep, "created": created}

        @app.post("/validate-body")
        async def validate_body(item: dict):
            return item

        @app.get("/boom/{code}")
        async def boom(code: int):
            raise StarletteHTTPException(status_code=code, detail="starlette boom")

        captured_bodies = []

        @app.exception_handler(RequestValidationError)
        async def custom_validation_handler(
            request: Request, exc: RequestValidationError
        ):
            captured_bodies.append(exc.body)
            return await request_validation_exception_handler(request, exc)

        @app.exception_handler(StarletteHTTPException)
        async def custom_http_handler(request: Request, exc: Exception):
            resp = await http_exception_handler(request, exc)
            resp.headers["X-Custom-Http"] = "handled"
            return resp

        openapi = app.openapi()
        self.assertEqual(openapi["paths"]["/inject-response"]["get"]["tags"], ["items"])

        with TestClient(app) as client:
            r = client.get("/inject-response?created=true")
            self.assertEqual(r.status_code, 201)
            self.assertEqual(r.headers["x-dep-header"], "from-dep")
            self.assertEqual(r.headers["x-handler-header"], "from-handler")
            self.assertEqual(r.cookies.get("fakesession"), "cookie-val")
            self.assertEqual(r.json(), {"dep": "dep-ok", "created": True})

            r_val = client.post("/validate-body", json=["not-a-dict"])
            self.assertEqual(r_val.status_code, 422)
            self.assertEqual(captured_bodies, [["not-a-dict"]])

            r_http = client.get("/boom/418")
            self.assertEqual(r_http.status_code, 418)
            self.assertEqual(r_http.headers["x-custom-http"], "handled")
            self.assertEqual(r_http.json(), {"detail": "starlette boom"})

        # HTTPSRedirectMiddleware
        https_app = App()
        https_app.add_middleware(HTTPSRedirectMiddleware)

        @https_app.get("/secure")
        async def secure_endpoint():
            return {"secure": True}

        with TestClient(https_app, base_url="https://testserver") as https_client:
            r_sec = https_client.get("/secure")
            self.assertEqual(r_sec.status_code, 200)
            self.assertEqual(r_sec.json(), {"secure": True})

        with TestClient(https_app) as http_client:
            r_redir = http_client.get("/secure", follow_redirects=False)
            self.assertEqual(r_redir.status_code, 307)
            self.assertEqual(r_redir.headers["location"], "https://testserver/secure")
            r_followed = http_client.get("/secure")
            self.assertEqual(r_followed.status_code, 200)
            self.assertEqual(r_followed.json(), {"secure": True})

        for base_url, expected_loc in (
            ("http://[::1]", "https://[::1]/secure"),
            ("http://[::1]:80", "https://[::1]/secure"),
            ("http://[::1]:8080", "https://[::1]:8080/secure"),
        ):
            with TestClient(https_app, base_url=base_url) as ipv6_client:
                r_ipv6 = ipv6_client.get("/secure", follow_redirects=False)
                self.assertEqual(r_ipv6.status_code, 307)
                self.assertEqual(r_ipv6.headers["location"], expected_loc)

        # Behind a prefix-stripping proxy, the redirect keeps root_path, as
        # the slash redirects do, including from a mounted application.
        rooted_app = App(root_path="/api/v1")
        rooted_app.add_middleware(HTTPSRedirectMiddleware)

        @rooted_app.get("/items")
        async def rooted_items():
            return {"ok": True}

        mounted = App()

        @mounted.get("/thing")
        async def mounted_thing():
            return {"ok": True}

        rooted_app.mount("/sub", mounted)

        with TestClient(rooted_app) as rooted_client:
            for path, expected_loc in (
                ("/items?q=1", "https://testserver/api/v1/items?q=1"),
                ("/sub/thing", "https://testserver/api/v1/sub/thing"),
            ):
                r_rooted = rooted_client.get(path, follow_redirects=False)
                self.assertEqual(r_rooted.status_code, 307)
                self.assertEqual(r_rooted.headers["location"], expected_loc)

    def test_param_model_validators_and_scalar_raw_validators_and_root_path_redirects(
        self,
    ) -> None:
        from pydantic import (
            AfterValidator,
            BaseModel,
            BeforeValidator,
            PlainValidator,
            WrapValidator,
        )

        class LimitModel(BaseModel):
            limit: int = 5

        def check_limit(m: LimitModel) -> LimitModel:
            if m.limit > 10:
                raise ValueError("limit must not exceed 10")
            return m

        app = App(root_path="/api/v1")

        @app.get("/model-query")
        async def model_query(
            q: Annotated[LimitModel, Query(), AfterValidator(check_limit)],
        ):
            return {"limit": q.limit}

        @app.get("/model-header")
        async def model_header(
            h: Annotated[LimitModel, Header(), AfterValidator(check_limit)],
        ):
            return {"limit": h.limit}

        @app.get("/model-cookie")
        async def model_cookie(
            c: Annotated[LimitModel, Cookie(), AfterValidator(check_limit)],
        ):
            return {"limit": c.limit}

        @app.post("/model-form")
        async def model_form(
            f: Annotated[LimitModel, Form(), AfterValidator(check_limit)],
        ):
            return {"limit": f.limit}

        def strip_dollar(v: Any) -> Any:
            return v.removeprefix("$")

        def plain_dollar(v: Any) -> int:
            return int(v.removeprefix("$"))

        def wrap_dollar(v: Any, handler: Any) -> int:
            return handler(v.removeprefix("$"))

        @app.get("/scalar-validators/{p}")
        async def scalar_validators(
            p: Annotated[int, BeforeValidator(strip_dollar)],
            b: Annotated[int, Query(ge=2), BeforeValidator(strip_dollar)] = 5,
            pl: Annotated[int, PlainValidator(plain_dollar)] = "$7",
            w: Annotated[int, WrapValidator(wrap_dollar)] = 9,
        ):
            return {"p": p, "b": b, "pl": pl, "w": w}

        @app.get("/items/")
        async def items():
            return {"items": True}

        @app.get("/{a}/{b}/")
        async def two_seg(a: str, b: str):
            return {"a": a, "b": b}

        with TestClient(app) as client:
            # (1) Model-valued parameters with AfterValidator: passing & failing for Query, Header, Cookie, Form
            self.assertEqual(client.get("/model-query?limit=8").json(), {"limit": 8})
            r_qf = client.get("/model-query?limit=20")
            self.assertEqual(r_qf.status_code, 422)
            self.assertEqual(r_qf.json()["detail"][0]["loc"], ["query"])

            self.assertEqual(
                client.get("/model-header", headers={"limit": "8"}).json(),
                {"limit": 8},
            )
            r_hf = client.get("/model-header", headers={"limit": "20"})
            self.assertEqual(r_hf.status_code, 422)
            self.assertEqual(r_hf.json()["detail"][0]["loc"], ["header"])

            client.cookies.set("limit", "8")
            self.assertEqual(client.get("/model-cookie").json(), {"limit": 8})
            client.cookies.set("limit", "20")
            r_cf = client.get("/model-cookie")
            self.assertEqual(r_cf.status_code, 422)
            self.assertEqual(r_cf.json()["detail"][0]["loc"], ["cookie"])
            client.cookies.clear()

            self.assertEqual(
                client.post("/model-form", data={"limit": "8"}).json(),
                {"limit": 8},
            )
            r_ff = client.post("/model-form", data={"limit": "20"})
            self.assertEqual(r_ff.status_code, 422)
            self.assertEqual(r_ff.json()["detail"][0]["loc"], ["body"])

            # (2) Scalar parameters with BeforeValidator, PlainValidator, WrapValidator
            self.assertEqual(
                client.get("/scalar-validators/$2?b=$3&pl=$4&w=$6").json(),
                {"p": 2, "b": 3, "pl": 4, "w": 6},
            )
            self.assertEqual(
                client.get("/scalar-validators/12?b=15&pl=18&w=21").json(),
                {"p": 12, "b": 15, "pl": 18, "w": 21},
            )
            self.assertEqual(
                client.get("/scalar-validators/$2").json(),
                {"p": 2, "b": 5, "pl": 7, "w": 9},
            )
            self.assertEqual(client.get("/scalar-validators/$bad").status_code, 422)
            self.assertEqual(client.get("/scalar-validators/$2?b=$1").status_code, 422)
            self.assertEqual(
                client.get("/scalar-validators/$2?pl=$bad").status_code, 422
            )
            self.assertEqual(
                client.get("/scalar-validators/$2?w=$bad").status_code, 422
            )

            # (3) Slash redirects include root_path, keep query string, and keep // guard
            r_slash = client.get("/items?a=1", follow_redirects=False)
            self.assertEqual(r_slash.status_code, 307)
            self.assertEqual(r_slash.headers["location"], "/api/v1/items/?a=1")
            r_slash_plain = client.get("/items", follow_redirects=False)
            self.assertEqual(r_slash_plain.status_code, 307)
            self.assertEqual(r_slash_plain.headers["location"], "/api/v1/items/")
            r_evil = client.get(
                "http://testserver//evil.example", follow_redirects=False
            )
            self.assertEqual(r_evil.status_code, 404)
            self.assertNotIn("location", r_evil.headers)

    def test_parameter_markers_preserve_positional_arguments(self) -> None:
        from bessemer import Path as PathParam

        for marker_cls in (Query, PathParam, Header, Cookie, Body, Form, File):
            m = marker_cls(
                None,
                "alias-val",
                "desc-val",
                1,
                0,
                10,
                11,
                2,
                8,
                "^a",
                True,
                "title-val",
                ["ex1"],
                {"ex": {"value": "ex1"}},
                {"x-extra": 1},
                False,
            )
            self.assertIsNone(m.default)
            self.assertEqual(m.alias, "alias-val")
            self.assertEqual(m.description, "desc-val")
            self.assertEqual(m.ge, 1)
            self.assertEqual(m.gt, 0)
            self.assertEqual(m.le, 10)
            self.assertEqual(m.lt, 11)
            self.assertEqual(m.min_length, 2)
            self.assertEqual(m.max_length, 8)
            self.assertEqual(m.pattern, "^a")
            self.assertTrue(m.deprecated)
            self.assertEqual(m.title, "title-val")
            self.assertEqual(m.examples, ["ex1"])
            self.assertEqual(m.openapi_examples, {"ex": {"value": "ex1"}})
            self.assertEqual(m.json_schema_extra, {"x-extra": 1})
            self.assertFalse(m.include_in_schema)
            self.assertIsNone(m.default_factory)
            self.assertIsNone(m.validation_alias)
            self.assertIsNone(m.serialization_alias)
            self.assertIsNone(m.multiple_of)
            self.assertIsNone(m.strict)
            self.assertIsNone(m.discriminator)

    def test_oauth2_base_flow_validation(self) -> None:
        from bessemer.security import OAuth2

        valid = OAuth2(
            flows={
                "implicit": {"authorizationUrl": "/auth"},
                "password": {"tokenUrl": "/token", "refreshUrl": "/refresh"},
                "clientCredentials": {"tokenUrl": "/token"},
                "authorizationCode": {
                    "authorizationUrl": "/auth",
                    "tokenUrl": "/token",
                },
            }
        )
        self.assertEqual(
            set(valid.model["flows"]),
            {"implicit", "password", "clientCredentials", "authorizationCode"},
        )

        for bad_flows in (
            {"implicit": {}},
            {"implicit": {"authorizationUrl": 123}},
            {"password": {}},
            {"password": {"tokenUrl": 123}},
            {"password": {"tokenUrl": ""}},
            {"password": {"tokenUrl": "/token", "refreshUrl": 123}},
            {"clientCredentials": {}},
            {"clientCredentials": {"tokenUrl": 123}},
            {"authorizationCode": {"authorizationUrl": "/auth"}},
            {"authorizationCode": {"tokenUrl": "/token"}},
            {"authorizationCode": {"authorizationUrl": 123, "tokenUrl": "/token"}},
        ):
            with self.assertRaises(ValueError, msg=f"accepted {bad_flows!r}"):
                OAuth2(flows=bad_flows)

    def test_request_receive_delegates_multi_chunk_asgi_middleware(self) -> None:
        class ChunkingMiddleware:
            def __init__(self, app):
                self.app = app

            async def __call__(self, scope, receive, send):
                msg = await receive()
                full = msg.get("body", b"")
                chunks = [
                    {"type": "http.request", "body": full[:3], "more_body": True},
                    {"type": "http.request", "body": full[3:], "more_body": False},
                ]
                idx = 0

                async def chunked_receive():
                    nonlocal idx
                    if idx < len(chunks):
                        item = chunks[idx]
                        idx += 1
                        return item
                    return await receive()

                await self.app(scope, chunked_receive, send)

        app = App()
        app.add_middleware(ChunkingMiddleware)

        @app.post("/chunks")
        async def read_chunks(request: Request):
            parts = []
            while True:
                msg = await request.receive()
                parts.append(msg["body"].decode())
                if not msg.get("more_body", False):
                    break
            return {"parts": parts}

        with TestClient(app) as client:
            r = client.post("/chunks", content=b"abcdef")
            self.assertEqual(r.status_code, 200)
            self.assertEqual(r.json(), {"parts": ["abc", "def"]})

    def test_strict_query_time_timedelta_decimal(self) -> None:
        app = App()

        @app.get("/strict-scalars")
        async def strict_scalars(
            t: Annotated[dt.time, Query(strict=True)] = dt.time(12, 30),
            td: Annotated[dt.timedelta, Query(strict=True)] = dt.timedelta(seconds=15),
            dec: Annotated[Decimal, Query(strict=True)] = Decimal("2.5"),
        ):
            return {
                "t": t.isoformat(),
                "td": td.total_seconds(),
                "dec": str(dec),
            }

        with TestClient(app) as client:
            self.assertEqual(
                client.get("/strict-scalars").json(),
                {"t": "12:30:00", "td": 15.0, "dec": "2.5"},
            )
            self.assertEqual(client.get("/strict-scalars?t=12:30:00").status_code, 422)
            self.assertEqual(client.get("/strict-scalars?td=PT15S").status_code, 422)
            self.assertEqual(client.get("/strict-scalars?dec=2.5").status_code, 422)

    def test_body_container_and_file_validators(self) -> None:
        from bessemer import Response, UploadFile
        from pydantic import AfterValidator, BeforeValidator

        def require_ok_key(d: dict) -> dict:
            if d.get("ok") is not True:
                raise ValueError("missing ok=True")
            return {**d, "validated": True}

        def require_non_empty_even_len(items: list) -> list:
            if not items or len(items) % 2 != 0:
                raise ValueError("list must have a positive even length")
            return items

        def coerce_pairs(v: Any) -> Any:
            if isinstance(v, list):
                return dict(v)
            return v

        def require_magic_bytes(data: bytes) -> bytes:
            if not data.startswith(b"OK:"):
                raise ValueError("must start with OK:")
            return data

        def require_txt_upload(upload: UploadFile) -> UploadFile:
            if not (upload.filename or "").endswith(".txt"):
                raise ValueError("filename must end with .txt")
            return upload

        app = App()

        @app.post("/dict-body")
        async def dict_body(
            payload: Annotated[dict, Body(), AfterValidator(require_ok_key)],
        ):
            return payload

        @app.post("/list-body")
        async def list_body(
            payload: Annotated[
                list, Body(), AfterValidator(require_non_empty_even_len)
            ],
        ):
            return payload

        @app.post("/dict-before")
        async def dict_before(
            payload: Annotated[
                dict[str, Any],
                Body(),
                BeforeValidator(coerce_pairs),
                AfterValidator(require_ok_key),
            ],
        ):
            return payload

        @app.post("/file-bytes")
        async def file_bytes(
            raw: Annotated[bytes, File(), AfterValidator(require_magic_bytes)],
        ):
            return {"text": raw.decode()}

        @app.post("/file-upload")
        async def file_upload(
            up: Annotated[UploadFile, File(), AfterValidator(require_txt_upload)],
        ):
            return {"filename": up.filename}

        with TestClient(app) as client:
            r_ok = client.post("/dict-body", json={"ok": True, "x": 1})
            self.assertEqual(r_ok.status_code, 200)
            self.assertEqual(r_ok.json(), {"ok": True, "x": 1, "validated": True})
            self.assertEqual(
                client.post("/dict-body", json={"ok": False}).status_code, 422
            )
            self.assertEqual(client.post("/dict-body", json=[1, 2]).status_code, 422)

            r_list = client.post("/list-body", json=[1, 2])
            self.assertEqual(r_list.status_code, 200)
            self.assertEqual(r_list.json(), [1, 2])
            self.assertEqual(client.post("/list-body", json=[1]).status_code, 422)
            self.assertEqual(client.post("/list-body", json={"a": 1}).status_code, 422)

            r_before = client.post("/dict-before", json=[["ok", True], ["k", "v"]])
            self.assertEqual(r_before.status_code, 200)
            self.assertEqual(r_before.json(), {"ok": True, "k": "v", "validated": True})
            self.assertEqual(
                client.post("/dict-before", json=[["ok", False]]).status_code, 422
            )

            r_fb = client.post("/file-bytes", files={"raw": ("a.bin", b"OK:hello")})
            self.assertEqual(r_fb.status_code, 200)
            self.assertEqual(r_fb.json(), {"text": "OK:hello"})
            self.assertEqual(
                client.post(
                    "/file-bytes", files={"raw": ("a.bin", b"BAD:hello")}
                ).status_code,
                422,
            )

            r_fu = client.post("/file-upload", files={"up": ("note.txt", b"hi")})
            self.assertEqual(r_fu.status_code, 200)
            self.assertEqual(r_fu.json(), {"filename": "note.txt"})
            self.assertEqual(
                client.post(
                    "/file-upload", files={"up": ("note.bin", b"hi")}
                ).status_code,
                422,
            )

        with self.assertRaises(ValueError):
            bad_app = App()

            @bad_app.get("/bad-dep-validator")
            async def bad_dep(
                x: Annotated[int, Depends(lambda: 1), AfterValidator(lambda v: v)],
            ):
                return x

            bad_app.build()

        with self.assertRaises(ValueError):
            bad_app = App()

            @bad_app.get("/bad-req-validator")
            async def bad_req(
                req: Annotated[Request, AfterValidator(lambda v: v)],
            ):
                return {}

            bad_app.build()

        with self.assertRaises(ValueError):
            bad_app = App()

            @bad_app.get("/bad-resp-validator")
            async def bad_resp(
                resp: Annotated[Response, AfterValidator(lambda v: v)],
            ):
                return {}

            bad_app.build()

    def test_header_model_convert_underscores_distinct_identity_both_orders(
        self,
    ) -> None:
        from pydantic import BaseModel

        class HdrModel(BaseModel):
            x_val: str = "missing"

        app = App()

        @app.get("/converted-first")
        async def converted_first(
            converted: Annotated[HdrModel, Header()],
            raw: Annotated[HdrModel, Header(convert_underscores=False)],
        ):
            return {"converted": converted.x_val, "raw": raw.x_val}

        @app.get("/raw-first")
        async def raw_first(
            raw: Annotated[HdrModel, Header(convert_underscores=False)],
            converted: Annotated[HdrModel, Header()],
        ):
            return {"converted": converted.x_val, "raw": raw.x_val}

        with TestClient(app) as client:
            headers = {"x-val": "from-dash", "x_val": "from-underscore"}
            r1 = client.get("/converted-first", headers=headers)
            self.assertEqual(r1.status_code, 200)
            self.assertEqual(
                r1.json(), {"converted": "from-dash", "raw": "from-underscore"}
            )
            r2 = client.get("/raw-first", headers=headers)
            self.assertEqual(r2.status_code, 200)
            self.assertEqual(
                r2.json(), {"converted": "from-dash", "raw": "from-underscore"}
            )


if __name__ == "__main__":
    unittest.main()
