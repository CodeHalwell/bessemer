"""The native invoker runs routes whose only non-scalar input is one Pydantic
body model, and every response matches the worker's Python path.

The body reaches the invoker as raw JSON bytes. A body the model plan builds
exactly is passed straight to the handler; anything else (a value Pydantic
would coerce or reject, an empty or null body) takes ``_invoke``, and the
server answers wrong content types, invalid JSON and oversized bodies before
either path, so those responses cannot differ.
"""

import asyncio
import importlib.util
import json
import sys
import unittest
from unittest import mock

from bessemer import App, Body, HTTPException
from bessemer.request import current_request
from bessemer.worker import Worker

NATIVE = importlib.util.find_spec("bessemer._native") is not None
PYDANTIC = importlib.util.find_spec("pydantic") is not None
LOOPS = ("asyncio", "bessemer") if sys.platform in ("linux", "darwin") else ("asyncio",)

ORDER = {
    "items": [
        {"id": i, "name": f"item-{i}", "price": i * 1.25, "tags": ["a", "b"]}
        for i in range(3)
    ]
}


def _native() -> bool:
    """Whether the running handler was started by the native invoker."""
    return type(asyncio.current_task().get_coro()).__name__ == "Call"


def _native_routes(app):
    worker = Worker(app)
    try:
        return worker._native_routes
    finally:
        for lane in worker._lanes:
            lane.loop.close()
        worker._executor.shutdown()


def build_app(events: list) -> App:
    from pydantic import BaseModel, field_validator

    class Item(BaseModel):
        id: int
        name: str
        price: float
        tags: list[str]

    class Order(BaseModel):
        items: list[Item]

    class Checked(BaseModel):
        name: str

        @field_validator("name")
        @classmethod
        def upper(cls, value: str) -> str:
            return value.upper()

    app = App()

    @app.post("/echo")
    async def echo(order: Order) -> Order:
        return order

    @app.post("/inspect/{n}")
    async def inspect_order(n: int, order: Order, q: str = "x"):
        request = current_request.get()
        task = asyncio.current_task()
        native = _native()
        await asyncio.sleep(0)
        return {
            "n": n,
            "q": q,
            "count": len(order.items),
            "model": type(order) is Order,
            "native": native,
            "path": request.path,
            "same_request": current_request.get() is request,
            "same_task": asyncio.current_task() is task,
        }

    @app.post("/with/{n}")
    async def with_inputs(n: int, order: Order, q: str = "x"):
        return {"n": n, "q": q, "order": order.model_dump()}

    @app.post("/conflict")
    async def conflict(order: Order):
        raise HTTPException(409, "conflict", headers={"x-items": str(len(order.items))})

    @app.post("/boom")
    async def boom(order: Order):
        events.append(len(order.items))
        raise ValueError("boom")

    @app.post("/optional")
    async def optional(order: Order | None = None):
        return {"empty": order is None}

    @app.post("/embedded")
    async def embedded(order: Order = Body(embed=True)):  # noqa: B008
        return {"count": len(order.items)}

    @app.post("/checked")
    async def checked(value: Checked):
        return value

    return app


# (description, path, headers, body): each request is answered identically on
# the native and Python paths.
JSON = {"content-type": "application/json"}
# A body sent without a Content-Type is refused (strict_content_type), so
# every case that means to reach the parser says what it is sending.
CASES = [
    ("exact", "/echo", {"content-type": "application/json"}, ORDER),
    ("suffix type", "/echo", {"content-type": "application/vnd.api+json"}, ORDER),
    ("utf-8", "/echo", {"content-type": "application/json; charset=utf-8"}, ORDER),
    ("no content type", "/echo", {}, ORDER),
    ("extra field", "/echo", JSON, {"items": [], "other": 1}),
    ("coerced", "/echo", JSON, {"items": [{**ORDER["items"][0], "id": "1"}]}),
    ("int price", "/echo", JSON, {"items": [{**ORDER["items"][0], "price": 2}]}),
    ("missing field", "/echo", JSON, {"items": [{"id": 1}]}),
    ("wrong type", "/echo", JSON, {"items": {"id": 1}}),
    ("bad float", "/echo", JSON, {"items": [{**ORDER["items"][0], "price": "x"}]}),
    ("missing items", "/echo", JSON, {}),
    ("array", "/echo", JSON, []),
    ("null", "/echo", {"content-type": "application/json"}, b"null"),
    ("empty", "/echo", {"content-type": "application/json"}, b""),
    ("empty untyped", "/echo", {}, b""),
    ("invalid json", "/echo", {"content-type": "application/json"}, b'{"items": ['),
    ("text", "/echo", {"content-type": "text/plain"}, ORDER),
    ("latin-1", "/echo", {"content-type": "application/json; charset=latin-1"}, ORDER),
    ("form", "/echo", {"content-type": "application/x-www-form-urlencoded"}, b"a=1"),
    ("too large", "/echo", JSON, {"items": ORDER["items"] * 40}),
    ("inputs", "/with/4?q=abc", JSON, ORDER),
    ("bad path", "/with/x", JSON, ORDER),
    ("bad path and body", "/with/x", JSON, {"items": 1}),
    ("conflict", "/conflict", JSON, ORDER),
    ("conflict invalid", "/conflict", JSON, {"items": 1}),
    ("optional", "/optional", JSON, ORDER),
    ("optional empty", "/optional", JSON, b""),
    ("optional null", "/optional", {"content-type": "application/json"}, b"null"),
    ("embedded", "/embedded", JSON, {"order": ORDER}),
    ("embedded bare", "/embedded", JSON, ORDER),
    ("validator", "/checked", JSON, {"name": "abc"}),
]


def _send(client, path, headers, body):
    content = body if isinstance(body, bytes) else json.dumps(body).encode()
    response = client.post(path, headers=headers, content=content)
    return (
        response.status_code,
        response.headers.get("content-type"),
        response.headers.get("x-items"),
        response.content,
    )


@unittest.skipUnless(NATIVE and PYDANTIC, "needs the native extension and pydantic")
class NativeBodySelectionTests(unittest.TestCase):
    def test_a_single_planned_body_model_is_native(self):
        app = build_app([])
        app.build()
        routes = _native_routes(app)
        native = {
            route.declaration["path"]
            for route, spec in zip(app._compiled, routes, strict=True)
            if spec is not None
        }
        self.assertIn("/echo", native)
        self.assertIn("/inspect/{n}", native)
        self.assertIn("/with/{n}", native)
        self.assertIn("/conflict", native)
        # Embedded bodies and models with validators keep Python's path.
        self.assertNotIn("/embedded", native)
        self.assertNotIn("/checked", native)

    def test_body_dependencies_keep_the_python_path(self):
        from bessemer import Depends
        from pydantic import BaseModel

        class Payload(BaseModel):
            name: str

        def dependency(payload: Payload) -> str:  # pragma: no cover
            return payload.name

        app = App()

        @app.post("/depends")
        async def depends(name: str = Depends(dependency)):  # pragma: no cover
            return {"name": name}

        @app.post("/two")
        async def two(a: Payload, b: Payload):  # pragma: no cover
            return {}

        app.build()
        self.assertEqual(set(_native_routes(app)), {None})


@unittest.skipUnless(NATIVE and PYDANTIC, "needs the native extension and pydantic")
class NativeBodyTests(unittest.TestCase):
    def _clients(self, loop):
        from bessemer.testclient import TestClient

        options = {"event_loop": loop, "max_body_size": 4096}
        native = TestClient(build_app([]), server_options=options)
        python = TestClient(build_app([]), server_options=options)
        return native, python

    def test_responses_match_the_python_path(self):
        for loop in LOOPS:
            native, python = self._clients(loop)
            with native:
                answers = {
                    name: _send(native, path, headers, body)
                    for name, path, headers, body in CASES
                }
            with (
                mock.patch.object(Worker, "_native_route", lambda self, _: None),
                python,
            ):
                expected = {
                    name: _send(python, path, headers, body)
                    for name, path, headers, body in CASES
                }
            for name, *_ in CASES:
                with self.subTest(loop=loop, case=name):
                    self.assertEqual(answers[name], expected[name])
            # Spot checks that the cases exercise what they claim.
            self.assertEqual(answers["exact"][0], 200)
            self.assertEqual(json.loads(answers["exact"][3]), ORDER)
            self.assertEqual(answers["coerced"][0], 200)
            self.assertEqual(answers["no content type"][0], 415)
            self.assertEqual(answers["missing field"][0], 422)
            self.assertEqual(answers["empty"][0], 422)
            self.assertEqual(answers["invalid json"][0], 422)
            self.assertEqual(answers["text"][0], 415)
            self.assertEqual(answers["too large"][0], 413)
            self.assertEqual(answers["conflict"][:3], (409, "application/json", "3"))
            self.assertEqual(json.loads(answers["inputs"][3])["q"], "abc")

    def test_a_valid_body_runs_natively_in_its_own_task_and_request(self):
        from bessemer.testclient import TestClient

        for loop in LOOPS:
            with (
                self.subTest(loop=loop),
                TestClient(
                    build_app([]), server_options={"event_loop": loop}
                ) as client,
            ):
                for _ in range(2):
                    response = client.post("/inspect/4", params={"q": "y"}, json=ORDER)
                    self.assertEqual(
                        response.json(),
                        {
                            "n": 4,
                            "q": "y",
                            "count": 3,
                            "model": True,
                            "native": True,
                            "path": "/inspect/{n}",
                            "same_request": True,
                            "same_task": True,
                        },
                    )
                # A body Pydantic must coerce (a string id) takes the Python path.
                coerced = {"items": [{**ORDER["items"][0], "id": "1"}]}
                response = client.post("/inspect/4", json=coerced)
                self.assertEqual(response.json()["native"], False)
                self.assertEqual(response.json()["count"], 1)

    def test_handler_exceptions_take_the_same_boundary(self):
        from bessemer.testclient import TestClient

        events = []
        for loop in LOOPS:
            with (
                self.subTest(loop=loop),
                TestClient(
                    build_app(events), server_options={"event_loop": loop}
                ) as client,
            ):
                with self.assertLogs("bessemer", "ERROR") as logs:
                    response = client.post("/boom", json=ORDER)
                self.assertEqual(
                    (response.status_code, response.json()),
                    (500, {"detail": "Internal Server Error"}),
                )
                self.assertIn("Unhandled ValueError in POST /boom", logs.output[0])
                response = client.post("/conflict", json=ORDER)
                self.assertEqual(response.status_code, 409)
                self.assertEqual(response.json(), {"detail": "conflict"})
                self.assertEqual(response.headers["x-items"], "3")
        self.assertEqual(events, [3] * len(LOOPS))

    def test_app_exception_handlers_still_see_body_routes(self):
        from bessemer import JSONResponse
        from bessemer.testclient import TestClient

        for loop in LOOPS:
            app = build_app([])

            @app.exception_handler(ValueError)
            async def handled(request, error):
                return JSONResponse(
                    {"handled": str(error), "path": request.url.path}, status_code=418
                )

            with (
                self.subTest(loop=loop),
                TestClient(app, server_options={"event_loop": loop}) as client,
            ):
                response = client.post("/boom", json=ORDER)
                self.assertEqual(
                    (response.status_code, response.json()),
                    (418, {"handled": "boom", "path": "/boom"}),
                )


if __name__ == "__main__":
    unittest.main()
