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
from typing import Annotated

import bessemer
from bessemer import (
    APIRouter,
    App,
    BackgroundTasks,
    Body,
    Depends,
    File,
    Form,
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


if __name__ == "__main__":
    unittest.main()
