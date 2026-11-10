"""Response classes through the server: ``render`` overrides, responses
rebuilt from another response's headers, and ``http.HTTPStatus`` status
codes."""

from __future__ import annotations

import datetime as dt
import json
import unittest
from http import HTTPStatus

from bessemer import App, HTTPException, JSONResponse, Request, Response
from bessemer.exception_handlers import http_exception_handler
from bessemer.testclient import TestClient

try:
    from pydantic import BaseModel
except ImportError:
    BaseModel = None
else:

    class Item(BaseModel):
        name: str
        price: float


class Pretty(JSONResponse):
    """Starlette's extension point: the class encodes the body itself."""

    def render(self, content) -> bytes:
        return json.dumps(content, indent=1, sort_keys=True).encode()


class VendorPretty(Pretty):
    media_type = "application/vnd.example+json"


class Vendor(JSONResponse):
    media_type = "application/vnd.example+json"


class Passing:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        await self.app(scope, receive, send)


def no_middleware(app):
    pass


def http_middleware(app):
    @app.middleware("http")
    async def passing(request, call_next):
        return await call_next(request)


def asgi_middleware(app):
    app.add_middleware(Passing)


def pretty(value) -> str:
    return json.dumps(value, indent=1, sort_keys=True)


class RenderTests(unittest.TestCase):
    def build(self, around):
        app = App()
        around(app)

        @app.get("/class", response_class=Pretty)
        async def by_class():
            return {"b": 2, "a": 1}

        @app.get("/sync", response_class=Pretty, status_code=201)
        def sync():
            return {"b": 2, "a": 1}

        @app.get("/request", response_class=VendorPretty)
        async def with_request(request: Request):
            return {"path": request.path}

        @app.get("/direct")
        async def direct():
            return Pretty({"b": 2, "a": 1}, status_code=202, headers={"X-A": "1"})

        @app.get("/dates", response_class=Pretty)
        async def dates():
            # Normalized before render sees it, as FastAPI does.
            return {"day": dt.date(2026, 11, 9), "values": (1, 2)}

        @app.get("/vendor", response_class=Vendor)
        async def vendor():
            return {"b": 2, "a": 1}

        if BaseModel is not None:

            @app.get("/model", response_class=Pretty, response_model=Item)
            async def model():
                return {"name": "pen", "price": 2, "secret": "dropped"}

        return app

    def test_a_render_override_is_what_the_server_sends(self):
        for around in (no_middleware, http_middleware, asgi_middleware):
            with (
                self.subTest(around=around.__name__),
                TestClient(self.build(around)) as client,
            ):
                response = client.get("/class")
                self.assertEqual(
                    (response.status_code, response.text),
                    (200, pretty({"a": 1, "b": 2})),
                )
                self.assertEqual(response.headers["content-type"], "application/json")
                response = client.get("/sync")
                self.assertEqual(
                    (response.status_code, response.text),
                    (201, pretty({"a": 1, "b": 2})),
                )
                response = client.get("/request")
                self.assertEqual(response.text, pretty({"path": "/request"}))
                self.assertEqual(
                    response.headers["content-type"], "application/vnd.example+json"
                )
                response = client.get("/direct")
                self.assertEqual(
                    (response.status_code, response.text),
                    (202, pretty({"a": 1, "b": 2})),
                )
                self.assertEqual(response.headers["x-a"], "1")
                self.assertEqual(
                    client.get("/dates").text,
                    pretty({"day": "2026-11-09", "values": [1, 2]}),
                )
                # A subclass that does not override render is encoded natively.
                response = client.get("/vendor")
                self.assertEqual(response.text, '{"b":2,"a":1}')
                self.assertEqual(
                    response.headers["content-type"], "application/vnd.example+json"
                )
                if BaseModel is not None:
                    self.assertEqual(
                        client.get("/model").text, pretty({"name": "pen", "price": 2.0})
                    )

    def test_body_matches_what_the_native_encoder_sends(self):
        content = {
            "text": "caf\N{LATIN SMALL LETTER E WITH ACUTE} \N{SNOWMAN}",
            "numbers": [1, -2.5, 1e30, 10**20],
            "nested": {"none": None, "flags": [True, False]},
        }
        app = App()

        @app.get("/")
        async def index():
            return JSONResponse(content)

        with TestClient(app) as client:
            self.assertEqual(client.get("/").content, JSONResponse(content).body)


class HeaderCopyTests(unittest.TestCase):
    def test_a_response_rebuilt_from_headers_keeps_repeated_fields(self):
        app = App()

        @app.middleware("http")
        async def rebuild(request, call_next):
            response = await call_next(request)
            return Response(b"rebuilt", headers=response.headers)

        @app.get("/")
        async def index():
            response = JSONResponse({"ok": True})
            response.set_cookie("a", "1")
            response.set_cookie("b", "2")
            return response

        with TestClient(app) as client:
            response = client.get("/")
        self.assertEqual(response.text, "rebuilt")
        cookies = response.headers.get_all("set-cookie")
        self.assertEqual(len(cookies), 2, cookies)
        self.assertTrue(cookies[0].startswith("a=1"))
        self.assertTrue(cookies[1].startswith("b=2"))

    def test_the_default_http_exception_handler_keeps_repeated_fields(self):
        app = App()

        @app.exception_handler(HTTPException)
        async def delegate(request, error):
            return await http_exception_handler(request, error)

        @app.get("/")
        async def index():
            raise HTTPException(
                401,
                "Sign in",
                headers=[("WWW-Authenticate", "Basic"), ("WWW-Authenticate", "Bearer")],
            )

        with TestClient(app) as client:
            response = client.get("/")
        self.assertEqual(
            (response.status_code, response.json()), (401, {"detail": "Sign in"})
        )
        self.assertEqual(
            response.headers.get_all("www-authenticate"), ["Basic", "Bearer"]
        )


class StatusEnumTests(unittest.TestCase):
    """``http.HTTPStatus`` members wherever a status code goes."""

    def test_http_status_members_work_as_status_codes(self):
        app = App()

        @app.exception_handler(HTTPStatus.GONE)
        async def gone(request, error):
            return JSONResponse({"gone": True}, status_code=HTTPStatus.GONE)

        @app.middleware("http")
        async def accept(request, call_next):
            response = await call_next(request)
            if request.path == "/accepted":
                response.status_code = HTTPStatus.ACCEPTED
            return response

        @app.get("/missing")
        async def missing():
            raise HTTPException(status_code=HTTPStatus.NOT_FOUND)

        @app.get("/gone")
        async def gone_route():
            raise HTTPException(HTTPStatus.GONE)

        @app.get("/created", status_code=HTTPStatus.CREATED)
        async def created():
            return {"created": True}

        @app.get("/json")
        async def json_response():
            return JSONResponse({"ok": True}, status_code=HTTPStatus.CREATED)

        @app.get("/accepted")
        async def accepted():
            return {"accepted": True}

        self.assertIs(type(app._routes[2].status_code), int)
        with TestClient(app) as client, self.assertNoLogs("bessemer", "ERROR"):
            response = client.get("/missing")
            self.assertEqual(
                (response.status_code, response.json()), (404, {"detail": "Not Found"})
            )
            self.assertEqual(client.get("/gone").json(), {"gone": True})
            self.assertEqual(client.get("/created").status_code, 201)
            self.assertEqual(client.get("/json").status_code, 201)
            self.assertEqual(client.get("/accepted").status_code, 202)
        document = app.openapi()
        self.assertIn("201", document["paths"]["/created"]["get"]["responses"])


if __name__ == "__main__":
    unittest.main()
