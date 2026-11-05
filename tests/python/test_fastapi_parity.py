"""FastAPI-compatible names, options and behaviour (docs/fastapi-parity.md)."""

import asyncio
import dataclasses
import datetime as dt
import enum
import os
import tempfile
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
from bessemer.exceptions import RequestValidationError
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
        # Options FastAPI accepts but Bessemer cannot honour fail loudly.
        with self.assertRaises(TypeError):
            Query(default_factory=list)


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

    def test_unsupported_options_fail(self):
        with self.assertRaises(ValueError):
            App(redoc_url="/redoc")
        with self.assertRaises(ValueError):
            App(debug=True)
        with self.assertRaises(TypeError):
            App().add_middleware(type("ASGI", (), {}))
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

    def test_background_on_handler_and_middleware_responses(self):
        self.assertEqual(self.client.get("/handled").text, "conflict")
        response = self.client.get("/threads", headers={"x-block": "1"})
        self.assertEqual(response.text, "blocked")
        self.assertEqual(self.events, ["handler", "middleware"])
        self.events.clear()
        response = self.client.get("/background", headers={"x-audit": "1"})
        self.assertEqual(response.json(), {"ok": True})
        self.assertEqual(self.events, ["response task", "audit"])

    def test_injected_tasks_attached_to_response_run_once(self):
        self.assertEqual(self.client.get("/shared").json(), {"ok": True})
        self.assertEqual(self.events, ["once"])

    def test_response_background(self):
        self.assertEqual(self.client.get("/background").json(), {"ok": True})
        self.assertEqual(self.events, ["response task"])
        self.events.clear()
        self.assertEqual(self.client.get("/both").text, "x")
        self.assertEqual(self.events, ["request task", "second"])

    def test_file_response(self):
        response = self.client.get("/file")
        self.assertEqual(response.text, "file contents")
        self.assertEqual(
            response.headers["content-disposition"],
            "attachment; filename*=utf-8''na%C3%AFve.txt",
        )
        self.assertIn("etag", response.headers)
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
        self.assertIs(exceptions.WebSocketException, WebSocketException)


if __name__ == "__main__":
    unittest.main()
