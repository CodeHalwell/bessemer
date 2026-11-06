"""How the native invoker calls a lean handler.

The handler is called by keyword through vectorcall, with no kwargs dict, so
what it receives must match what ``Worker._invoke`` passes, for every parameter
kind and for inputs the request omits. Each request still runs in its own real
``asyncio.Task`` and a fresh ``contextvars.Context``.
"""

from __future__ import annotations

import asyncio
import contextvars
import importlib.util
import unittest

from bessemer import App, Depends, Query
from bessemer.worker import Worker

NATIVE = importlib.util.find_spec("bessemer._native") is not None
PYDANTIC = importlib.util.find_spec("pydantic") is not None

marker = contextvars.ContextVar("marker", default="unset")


def _facts() -> dict:
    task = asyncio.current_task()
    return {
        "task": isinstance(task, asyncio.Task),
        "native": type(task.get_coro()).__name__ == "Call",
    }


def _dependency() -> int:
    return 1


def build_app() -> App:
    app = App()

    @app.get("/none")
    async def none():
        return {**_facts(), "args": []}

    @app.get("/positional/{a}")
    async def positional(a: int, b: str, c: int = 3):
        return {**_facts(), "args": [a, b, c]}

    @app.get("/keyword-only")
    async def keyword_only(*, a: int, b: str = "dflt"):
        return {**_facts(), "args": [a, b]}

    @app.get("/mixed/{a}")
    async def mixed(a: int, c: str = "c", *, b: int, d: int = 4):
        return {**_facts(), "args": [a, c, b, d]}

    @app.get("/context")
    async def context():
        before = marker.get()
        marker.set("set by the request")
        return {"before": before, "after": marker.get()}

    @app.get("/primitives")
    async def primitives():
        # asyncio's own helpers work against the request's task.
        async def child():
            await asyncio.sleep(0)
            return asyncio.current_task() is not asyncio.current_task().get_loop()

        async with asyncio.timeout(5):
            async with asyncio.TaskGroup() as group:
                work = group.create_task(child())
        return {**_facts(), "child": work.result()}

    @app.get("/optional/{item_id}")
    async def optional(item_id: int, q: str = "x", n: int = Query(2, ge=1)):
        return {"id": item_id, "q": q, "n": n}

    @app.get("/nullable")
    async def nullable(q: str | None = None, flag: bool = True, ratio: float = 0.5):
        return {"q": q, "flag": flag, "ratio": ratio}

    # The same handler with a dependency, which keeps it on the Python path.
    @app.get("/python/{item_id}")
    async def through_python(
        item_id: int,
        q: str = "x",
        n: int = Query(2, ge=1),
        dependency: int = Depends(_dependency),
    ):
        return {"id": item_id, "q": q, "n": n}

    return app


def _native_paths(app: App) -> set[str]:
    app.build()
    worker = Worker(app)
    try:
        return {
            route.declaration["path"]
            for route, spec in zip(app._compiled, worker._native_routes, strict=True)
            if spec is not None
        }
    finally:
        for lane in worker._lanes:
            lane.loop.close()
        worker._executor.shutdown()


@unittest.skipUnless(NATIVE, "build the native extension first")
class NativeCallTests(unittest.TestCase):
    event_loop = "asyncio"

    @classmethod
    def setUpClass(cls):
        from bessemer.testclient import TestClient

        cls.client = TestClient(
            build_app(), server_options={"event_loop": cls.event_loop}
        )
        cls.client.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.client.__exit__(None, None, None)

    def check(self, path, args, **params):
        response = self.client.get(path, params=params)
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["args"], args)
        self.assertTrue(body["task"], "the handler runs in an asyncio.Task")
        self.assertTrue(body["native"], "the route takes the native path")

    def test_a_handler_without_parameters(self):
        self.check("/none", [])

    def test_positional_parameters(self):
        self.check("/positional/7", [7, "x", 3], b="x")
        self.check("/positional/7", [7, "y", 9], b="y", c="9")

    def test_keyword_only_parameters(self):
        self.check("/keyword-only", [1, "dflt"], a="1")
        self.check("/keyword-only", [2, "given"], a="2", b="given")

    def test_mixed_parameters(self):
        self.check("/mixed/5", [5, "c", 6, 4], b="6")
        self.check("/mixed/5", [5, "given", 6, 8], b="6", c="given", d="8")

    def test_asyncio_primitives_work_in_the_request_task(self):
        response = self.client.get("/primitives")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json(), {"task": True, "native": True, "child": True})

    def test_a_context_variable_does_not_reach_the_next_request(self):
        for _ in range(3):
            self.assertEqual(
                self.client.get("/context").json(),
                {"before": "unset", "after": "set by the request"},
            )
        self.assertEqual(marker.get(), "unset")

    def test_an_omitted_optional_input_stays_native_and_answers_as_python_does(self):
        omitted = self.client.get("/optional/7")
        python = self.client.get("/python/7")
        self.assertEqual(omitted.status_code, 200)
        self.assertEqual(omitted.content, python.content)
        self.assertEqual(
            omitted.headers["content-type"], python.headers["content-type"]
        )
        self.assertEqual(omitted.json(), {"id": 7, "q": "x", "n": 2})
        # Supplying the default explicitly gives the same bytes.
        self.assertEqual(
            self.client.get("/optional/7", params={"q": "x", "n": "2"}).content,
            omitted.content,
        )
        self.assertEqual(
            self.client.get("/optional/7", params={"q": "y"}).json(),
            {"id": 7, "q": "y", "n": 2},
        )
        nullable = self.client.get("/nullable")
        self.assertEqual(nullable.json(), {"q": None, "flag": True, "ratio": 0.5})

    def test_an_omitted_input_with_a_bound_default_is_checked_like_python(self):
        for params in ({"n": "0"}, {"n": "abc"}):
            native = self.client.get("/optional/7", params=params)
            python = self.client.get("/python/7", params=params)
            self.assertEqual(native.status_code, 422)
            self.assertEqual(native.content, python.content)


class NativeCallRegistrationTests(unittest.TestCase):
    def test_which_routes_are_native(self):
        if not NATIVE:
            self.skipTest("build the native extension first")
        native = _native_paths(build_app())
        for path in (
            "/none",
            "/positional/{a}",
            "/keyword-only",
            "/mixed/{a}",
            "/optional/{item_id}",
            "/nullable",
        ):
            self.assertIn(path, native)
        # A dependency keeps a route on the Python path.
        self.assertNotIn("/python/{item_id}", native)

    def test_a_default_that_violates_its_constraint_is_rejected_at_registration(self):
        app = App()

        @app.get("/bad")
        async def bad(q: str = Query("x", min_length=3)):
            return {}

        with self.assertRaisesRegex(
            ValueError, r"/bad: default for 'q' violates its declared constraints"
        ):
            app.build()

    @unittest.skipUnless(PYDANTIC, "needs pydantic")
    def test_an_annotated_field_default_that_violates_its_constraint(self):
        from typing import Annotated

        from pydantic import Field

        app = App()

        @app.get("/bad")
        async def bad(q: Annotated[str, Field(min_length=3)] = "x"):
            return {}

        with self.assertRaises(ValueError):
            app.build()

    def test_a_mutable_default_is_rejected_at_registration(self):
        app = App()

        @app.get("/mutable")
        async def mutable(tags: list = []):  # noqa: B006
            return {}

        with self.assertRaisesRegex(ValueError, "defaults must be None"):
            app.build()


@unittest.skipUnless(NATIVE, "build the native extension first")
class NativeCallBessemerLoopTests(NativeCallTests):
    event_loop = "bessemer"


if __name__ == "__main__":
    unittest.main()
