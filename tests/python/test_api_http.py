"""Public API contracts exercised through the installed native HTTP pipeline."""

import concurrent.futures
import http.client
import importlib.util
import json
import os
import time
import unittest
from pathlib import Path
from urllib.parse import quote

from test_native_integration import Server

APPLICATION = """
import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated
from bessemer import (
    App, BackgroundTasks, Body, Cookie, Depends, Header, HTMLResponse,
    HTTPException, JSONResponse, Path as PathInput, PlainTextResponse, Query,
    RedirectResponse, Request, Response, State,
)
from bessemer.request import current_request

@asynccontextmanager
async def lifespan(app):
    app.state.counter = 0
    app.state.events = []
    app.state.app_starts = 0
    yield
    Path("lifespan_closed").write_text(
        "closed" if Path("app_dependency_closed").exists() else "wrong-order"
    )

app = App(title="Installed API fixture", docs_url="/docs", lifespan=lifespan)
app.health("/healthz")

async def resource(state: State):
    state.counter += 1
    identifier = state.counter
    state.events.append(["enter", identifier])
    try:
        yield identifier
    finally:
        await asyncio.sleep(0.01)
        state.events.append(["exit", identifier])

async def singleton(state: State):
    state.app_starts += 1
    try:
        yield {"identifier": state.app_starts}
    finally:
        Path("app_dependency_closed").write_text("closed")

@app.get("/resources")
async def resources(
    first=Depends(resource), second=Depends(resource),
    fresh=Depends(resource, use_cache=False), configuration=Depends(singleton, scope="app"),
):
    return {"first": first, "second": second, "fresh": fresh, "app": configuration}

@app.get("/other-singleton")
async def other_singleton(configuration=Depends(singleton, scope="app")):
    return configuration

@app.get("/events")
async def events(state: State):
    return state.events

@app.get("/busy")
async def busy():
    Path("busy_started").touch()
    await asyncio.sleep(0.5)
    return {"done": True}

def permitted(value: Annotated[str, Header(alias="X-Permit")]):
    if value != "yes":
        raise HTTPException(401, "Permission required", headers={"WWW-Authenticate": "APIKey"})

@app.get("/guarded", dependencies=[Depends(permitted)])
async def guarded():
    return {"allowed": True}

@app.get("/inputs/{identifier}")
async def inputs(
    identifier: Annotated[int, PathInput(ge=1)],
    agent: Annotated[str, Header(alias="X-Agent", min_length=2)],
    page: Annotated[int, Query(alias="page-size", ge=1, le=5)] = 2,
    session: Annotated[str | None, Cookie(alias="sid")] = None,
):
    return {"identifier": identifier, "page": page, "agent": agent, "session": session}

@app.get("/quoted-cookie")
async def quoted_cookie(request: Request, session: Annotated[str, Cookie(alias="sid")]):
    return {"bound": session, "request": request.cookies.get("sid")}

@app.post("/scalar")
async def scalar(value: Annotated[int, Body(ge=1, le=10)]):
    return {"value": value}

@app.post("/nullable")
async def nullable(values: list | None = None):
    return values

def context(request: Request):
    return current_request.get() is request

def record_context():
    request = current_request.get()
    Path("context_task").write_text(request.path)

@app.get("/echo/{name}")
async def echo(
    name: str, request: Request, background: BackgroundTasks, in_context=Depends(context),
):
    background.add_task(record_context)
    return {
        "name": name, "method": request.method, "path": request.path,
        "path_params": dict(request.path_params), "query": dict(request.query_params),
        "header": request.headers.get("x-agent"), "client": request.client,
        "request_id": request.request_id, "in_context": in_context,
    }

@app.post("/request")
async def raw_request(request: Request):
    return {"body": (await request.body()).decode(), "json": await request.json()}

@app.get("/text")
async def text() -> PlainTextResponse:
    return PlainTextResponse("hello café", headers={"X-Answer": "yes"})

@app.get("/html")
async def html() -> HTMLResponse:
    return HTMLResponse("<p>ready</p>")

@app.get("/explicit-json")
async def explicit_json() -> JSONResponse:
    return JSONResponse({"ok": True}, status_code=202, headers={"X-Answer": "yes"})

@app.get("/redirect", status_code=307)
async def redirect() -> RedirectResponse:
    return RedirectResponse("/text?q=café")

@app.get("/cookies")
async def cookies() -> Response:
    response = Response(b"cookies", media_type="application/octet-stream")
    response.set_cookie("sid", "abc", httponly=True, secure=True, samesite="strict")
    response.set_cookie("theme", "dark")
    return response

@app.get("/clear-cookie")
async def clear_cookie() -> Response:
    response = Response(b"")
    response.delete_cookie("sid", secure=True, httponly=True)
    return response

try:
    from pydantic import BaseModel, Field, field_validator
except ImportError:
    pass
else:
    class InputModel(BaseModel):
        name: str = Field(min_length=2)
        count: int = Field(ge=1)

        @field_validator("count")
        @classmethod
        def validate_count(cls, value):
            if value == 42:
                raise ValueError("private-validator-detail")
            return value

    class PublicModel(BaseModel):
        identifier: int
        label: str = Field(serialization_alias="itemLabel")

    @app.post("/model", response_model=PublicModel)
    async def model(value: InputModel):
        return {"identifier": value.count, "label": value.name, "secret": "private-output"}

    @app.get("/invalid-model", response_model=PublicModel)
    async def invalid_model():
        return {"identifier": "private-output", "label": "valid"}
"""


def request(server, method, path, body=None, headers=None):
    connection = http.client.HTTPConnection("127.0.0.1", server.port, timeout=5)
    try:
        connection.request(method, path, body, {} if headers is None else headers)
        response = connection.getresponse()
        return response.status, response.getheaders(), response.read()
    finally:
        connection.close()


@unittest.skipUnless(
    importlib.util.find_spec("bessemer._native"), "build the native extension first"
)
class PublicAPIHTTPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = Server(application=APPLICATION, max_body_size=2048)

    @classmethod
    def tearDownClass(cls):
        cls.server.close()

    def test_dependency_cache_and_cleanup_complete_before_next_request(self):
        status, _, body = request(self.server, "GET", "/resources")
        self.assertEqual(status, 200)
        result = json.loads(body)
        self.assertEqual(result["first"], result["second"])
        self.assertNotEqual(result["first"], result["fresh"])
        events = json.loads(request(self.server, "GET", "/events")[2])
        self.assertEqual(
            events[-4:],
            [
                ["enter", result["first"]],
                ["enter", result["fresh"]],
                ["exit", result["fresh"]],
                ["exit", result["first"]],
            ],
        )
        other = json.loads(request(self.server, "GET", "/other-singleton")[2])
        self.assertEqual(result["app"], {"identifier": 1})
        self.assertEqual(other, result["app"])

    def test_route_dependencies_block_handlers_and_preserve_error_headers(self):
        denied, headers, body = request(
            self.server, "GET", "/guarded", headers={"X-Permit": "no"}
        )
        self.assertEqual(denied, 401)
        self.assertEqual(dict(headers)["www-authenticate"], "APIKey")
        self.assertEqual(json.loads(body), {"detail": "Permission required"})
        self.assertEqual(
            request(self.server, "GET", "/guarded", headers={"X-Permit": "yes"})[0], 200
        )

    def test_path_query_header_cookie_metadata_and_validation_privacy(self):
        status, _, body = request(
            self.server,
            "GET",
            "/inputs/7?page-size=3",
            headers={"x-agent": "test", "Cookie": "sid=abc; theme=dark"},
        )
        self.assertEqual(status, 200)
        self.assertEqual(
            json.loads(body),
            {"identifier": 7, "page": 3, "agent": "test", "session": "abc"},
        )
        # A repeated key binds its last value, after percent-decoding both
        # the key and the value.
        status, _, body = request(
            self.server,
            "GET",
            "/inputs/7?page-size=1&page%2Dsize=%32",
            headers={"x-agent": "test"},
        )
        self.assertEqual((status, json.loads(body)["page"]), (200, 2))
        large = 2**130 + 1
        status, _, body = request(
            self.server, "GET", f"/inputs/{large}", headers={"X-Agent": "test"}
        )
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["identifier"], large)
        for target in (
            "/inputs/0",
            "/inputs/7?page-size=6",
            "/inputs/7?page-size=private-input",
        ):
            status, _, body = request(
                self.server, "GET", target, headers={"X-Agent": "test"}
            )
            self.assertEqual(status, 422)
            self.assertIsInstance(json.loads(body)["detail"], list)
            self.assertNotIn(b"private-input", body)
        self.assertEqual(
            request(self.server, "GET", "/inputs/7", headers={"X-Agent": "x"})[0], 422
        )
        self.assertEqual(request(self.server, "GET", "/inputs/7")[0], 422)

    def test_cookie_binding_and_request_view_agree_for_quoted_values(self):
        status, _, body = request(
            self.server, "GET", "/quoted-cookie", headers={"Cookie": 'sid="abc"'}
        )
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), {"bound": "abc", "request": "abc"})

    def test_scalar_and_nullable_json_bodies(self):
        headers = {"Content-Type": "application/json"}
        status, _, body = request(self.server, "POST", "/scalar", b"3", headers)
        self.assertEqual((status, json.loads(body)), (200, {"value": 3}))
        for invalid in (b"0", b"11", b'"3"', b"true", b"null"):
            self.assertEqual(
                request(self.server, "POST", "/scalar", invalid, headers)[0], 422
            )
        self.assertEqual(request(self.server, "POST", "/nullable")[0], 200)
        self.assertEqual(json.loads(request(self.server, "POST", "/nullable")[2]), None)
        self.assertEqual(
            json.loads(request(self.server, "POST", "/nullable", b"[1,2]", headers)[2]),
            [1, 2],
        )

    def test_request_context_unicode_parameters_and_background_work(self):
        target = "/echo/" + quote("café") + "?q=first&q=last"
        status, headers, body = request(
            self.server, "GET", target, headers={"X-Agent": "context"}
        )
        self.assertEqual(status, 200)
        value = json.loads(body)
        self.assertEqual(value["name"], "café")
        self.assertEqual(value["path_params"], {"name": "café"})
        self.assertEqual(value["query"], {"q": "last"})
        self.assertTrue(value["in_context"])
        self.assertEqual(value["header"], "context")
        self.assertIsNotNone(value["client"])
        self.assertEqual(value["request_id"], dict(headers)["x-request-id"])
        task_file = self.server.root / "context_task"
        deadline = time.monotonic() + 2
        while (
            not task_file.exists() or task_file.read_text() != value["path"]
        ) and time.monotonic() < deadline:
            time.sleep(0.001)
        self.assertEqual(task_file.read_text(), value["path"])
        status, _, body = request(
            self.server,
            "POST",
            "/request",
            b'{"ok":true}',
            {"Content-Type": "application/json"},
        )
        self.assertEqual(status, 200)
        self.assertEqual(
            json.loads(body), {"body": '{"ok":true}', "json": {"ok": True}}
        )

    def test_explicit_response_classes_headers_redirects_and_cookies(self):
        status, headers, body = request(self.server, "GET", "/text")
        self.assertEqual((status, body.decode()), (200, "hello café"))
        self.assertEqual(dict(headers)["content-type"], "text/plain; charset=utf-8")
        self.assertEqual(dict(headers)["x-answer"], "yes")
        self.assertEqual(request(self.server, "GET", "/html")[2], b"<p>ready</p>")
        status, headers, body = request(self.server, "GET", "/explicit-json")
        self.assertEqual((status, json.loads(body)), (202, {"ok": True}))
        status, headers, body = request(self.server, "GET", "/redirect")
        self.assertEqual((status, body), (307, b""))
        self.assertEqual(dict(headers)["location"], "/text?q=caf%C3%A9")
        status, headers, body = request(self.server, "GET", "/cookies")
        self.assertEqual((status, body), (200, b"cookies"))
        cookies = [value for name, value in headers if name == "set-cookie"]
        self.assertEqual(len(cookies), 2)
        self.assertIn("HttpOnly", cookies[0])
        self.assertIn("Secure", cookies[0])
        self.assertIn("samesite=strict", cookies[0].lower())
        status, _, body = request(
            self.server,
            "GET",
            "/quoted-cookie",
            headers={"Cookie": cookies[0].split(";", 1)[0]},
        )
        self.assertEqual(
            (status, json.loads(body)), (200, {"bound": "abc", "request": "abc"})
        )
        deleted = dict(request(self.server, "GET", "/clear-cookie")[1])["set-cookie"]
        self.assertIn("Max-Age=0", deleted)
        self.assertIn("expires=", deleted)

    def test_openapi_and_bundled_docs_are_native_static_routes_with_head(self):
        status, headers, body = request(self.server, "GET", "/openapi.json")
        self.assertEqual(status, 200)
        document = json.loads(body)
        self.assertEqual(document["openapi"], "3.1.0")
        operation = document["paths"]["/inputs/{identifier}"]["get"]
        self.assertIn(
            ("header", "x-agent"),
            {(p["in"], p["name"]) for p in operation["parameters"]},
        )
        self.assertNotIn("/openapi.json", document["paths"])
        self.assertNotIn("/healthz", document["paths"])
        head_status, head_headers, head_body = request(
            self.server, "HEAD", "/openapi.json"
        )
        self.assertEqual((head_status, head_body), (200, b""))
        self.assertEqual(
            dict(head_headers)["content-length"], dict(headers)["content-length"]
        )
        status, headers, body = request(self.server, "GET", "/docs")
        self.assertEqual(status, 200)
        self.assertIn("text/html", dict(headers)["content-type"])
        self.assertNotIn(b"<script src=", body)
        self.assertEqual(request(self.server, "HEAD", "/docs")[2], b"")
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(request, self.server, "GET", "/busy")
            deadline = time.monotonic() + 2
            while (
                not (self.server.root / "busy_started").exists()
                and time.monotonic() < deadline
            ):
                time.sleep(0.005)
            self.assertTrue((self.server.root / "busy_started").exists())
            self.assertEqual(request(self.server, "GET", "/resources")[0], 503)
            self.assertEqual(request(self.server, "GET", "/openapi.json")[0], 200)
            self.assertEqual(request(self.server, "HEAD", "/docs")[0], 200)
            self.assertEqual(pending.result(timeout=2)[0], 200)

    @unittest.skipUnless(
        importlib.util.find_spec("pydantic"), "optional Pydantic v2 is not installed"
    )
    def test_pydantic_input_response_aliases_filtering_and_safe_errors(self):
        headers = {"Content-Type": "application/json"}
        status, _, body = request(
            self.server, "POST", "/model", b'{"name":"widget","count":2}', headers
        )
        self.assertEqual(
            (status, json.loads(body)), (200, {"identifier": 2, "itemLabel": "widget"})
        )
        self.assertNotIn(b"private-output", body)
        status, _, body = request(
            self.server,
            "POST",
            "/model",
            b'{"name":"ok","count":"private-input"}',
            headers,
        )
        self.assertEqual(status, 422)
        self.assertNotIn(b"private-input", body)
        self.assertNotIn(b'"input"', body)
        status, _, body = request(
            self.server, "POST", "/model", b'{"name":"ok","count":42}', headers
        )
        self.assertEqual(status, 422)
        self.assertNotIn(b"private-validator-detail", body)
        self.assertNotIn(b'"ctx"', body)
        status, _, body = request(self.server, "GET", "/invalid-model")
        self.assertEqual(
            (status, json.loads(body)), (500, {"detail": "Internal Server Error"})
        )
        self.assertNotIn(b"private-output", body)
        self.server.log.seek(0)
        self.assertNotIn("private-output", self.server.log.read())

    @unittest.skipUnless(
        importlib.util.find_spec("pydantic"), "optional Pydantic v2 is not installed"
    )
    def test_production_example_has_public_probes_and_authenticated_model_routes(self):
        application = Path(__file__).resolve().parents[2] / "examples" / "production.py"
        key = "bessemer-example-test-key-32-characters"
        server = Server(
            application=application.read_text(),
            max_body_size=2048,
            environment={"BESSEMER_EXAMPLE_API_KEY": key, "BESSEMER_EXAMPLE_DOCS": "0"},
        )
        try:
            self.assertEqual(request(server, "GET", "/readyz")[0], 200)
            self.assertEqual(request(server, "GET", "/items/1")[0], 401)
            headers = {"X-API-Key": key}
            status, _, body = request(
                server, "GET", "/items/1?include_price=true", headers=headers
            )
            self.assertEqual(status, 200)
            self.assertEqual(json.loads(body)["price_cents"], 1250)
            status, _, body = request(
                server,
                "POST",
                "/items/quote",
                b'{"item_id":1,"quantity":3}',
                headers | {"Content-Type": "application/json"},
            )
            self.assertEqual(
                (status, json.loads(body)),
                (200, {"item_id": 1, "quantity": 3, "total_cents": 3750}),
            )
            self.assertEqual(
                request(server, "GET", "/openapi.json", headers=headers)[0], 404
            )
        finally:
            server.close()

    @unittest.skipUnless(os.name == "posix", "requires process termination")
    def test_app_dependencies_close_before_lifespan_shutdown(self):
        server = Server(application=APPLICATION, max_body_size=2048)
        try:
            self.assertEqual(request(server, "GET", "/other-singleton")[0], 200)
            server.process.terminate()
            self.assertEqual(server.process.wait(timeout=5), 0)
            self.assertEqual(
                (server.root / "app_dependency_closed").read_text(), "closed"
            )
            self.assertEqual((server.root / "lifespan_closed").read_text(), "closed")
        finally:
            server.close()


if __name__ == "__main__":
    unittest.main()
