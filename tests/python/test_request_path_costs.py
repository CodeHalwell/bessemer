"""Cheaper request-path pieces keep the behaviour of the code they replaced."""

import asyncio
import gc
import importlib.util
import subprocess
import sys
import time
import unittest
from pathlib import Path
from typing import Annotated

from bessemer import App, Depends, HTTPException, Request
from bessemer.datastructures import URL, ImmutableMultiDict, QueryParams
from bessemer.request import RequestHeaders
from bessemer.responses import Headers

NATIVE = importlib.util.find_spec("bessemer._native") is not None


class RequestHeadersTests(unittest.TestCase):
    def test_lookups_are_case_insensitive_and_take_the_last_value(self):
        for source in (
            [("Host", "a"), ("x-a", "1"), ("X-A", "2")],
            (("Host", "a"), ("x-a", "1"), ("X-A", "2")),
            iter([("Host", "a"), ("x-a", "1"), ("X-A", "2")]),
        ):
            headers = RequestHeaders(source)
            self.assertEqual(headers["host"], "a")
            self.assertEqual(headers["X-a"], "2")
            self.assertEqual(headers.get("x-A"), "2")
            self.assertIsNone(headers.get("missing"))
            self.assertEqual(headers.get("missing", "default"), "default")
            self.assertIn("HOST", headers)
            self.assertNotIn("missing", headers)
            self.assertEqual(headers.getlist("x-a"), ["1", "2"])
            with self.assertRaises(KeyError):
                headers["missing"]

    def test_mappings_and_non_text_values_are_normalized(self):
        headers = RequestHeaders({"Content-Length": 3})
        self.assertEqual(headers["content-length"], "3")
        self.assertEqual(list(headers), ["content-length"])

    def test_request_id_falls_back_to_the_header(self):
        request = Request({"headers": [("x-request-id", "abc")]})
        self.assertEqual(request.request_id, "abc")
        request = Request({"headers": [("x-request-id", "abc")], "request_id": "n"})
        self.assertEqual(request.request_id, "n")

    def test_cookies_come_from_metadata_or_the_header(self):
        self.assertEqual(dict(Request({"cookies": {}}).cookies), {})
        self.assertEqual(dict(Request({"cookies": {"a": "1"}}).cookies), {"a": "1"})
        request = Request({"headers": [("cookie", "a=1; b=2")]})
        self.assertEqual(dict(request.cookies), {"a": "1", "b": "2"})


class DataStructureTests(unittest.TestCase):
    def test_query_params_get_matches_indexing(self):
        params = QueryParams("a=1&a=2&b=")
        self.assertEqual(params.get("a"), "2")
        self.assertEqual(params.get("b"), "")
        self.assertIsNone(params.get("c"))
        self.assertEqual(params.get("c", "d"), "d")
        self.assertEqual(params.getlist("a"), ["1", "2"])
        self.assertEqual(str(params), "a=1&a=2&b=")
        self.assertEqual(QueryParams(b"x=%20").get("x"), " ")

    def test_multi_dict_copies_a_list_of_pairs(self):
        pairs = [("a", 1)]
        values = ImmutableMultiDict(pairs)
        pairs.append(("a", 2))
        self.assertEqual(values.multi_items(), [("a", 1)])
        self.assertEqual(values.get("a"), 1)

    def test_url_parts_are_parsed_once(self):
        url = URL("http://example.com:8080/a/b?q=1")
        self.assertEqual(url.path, "/a/b")
        self.assertIs(url.components, url.components)
        self.assertEqual(
            (url.hostname, url.port, url.query), ("example.com", 8080, "q=1")
        )


class ResponseHeadersTests(unittest.TestCase):
    def test_setting_replaces_every_field_of_that_name(self):
        headers = Headers([("X-A", "1"), ("b", "2"), ("x-a", "3")])
        headers["x-A"] = "4"
        self.assertEqual(headers.raw, [("b", "2"), ("x-A", "4")])
        headers["c"] = "5"
        self.assertEqual(headers.raw, [("b", "2"), ("x-A", "4"), ("c", "5")])

    def test_setting_a_new_field_still_checks_limits(self):
        headers = Headers([(f"h{index}", "v") for index in range(64)])
        with self.assertRaises(ValueError):
            headers["extra"] = "v"
        self.assertEqual(len(headers), 64)
        with self.assertRaises(ValueError):
            headers["bad name"] = "v"


@unittest.skipUnless(NATIVE, "build the native extension first")
class SimpleBindingTests(unittest.TestCase):
    """Handlers taking only inputs, the request and BackgroundTasks."""

    def test_request_background_and_inputs_are_bound_without_a_resolver(self):
        from bessemer import BackgroundTasks
        from bessemer.request import current_request
        from bessemer.testclient import TestClient

        app = App()
        ran = []

        @app.get("/simple/{item}")
        async def simple(
            item: int, request: Request, tasks: BackgroundTasks, q: str = "d"
        ):
            tasks.add_task(ran.append, item)
            return {
                "item": item,
                "q": q,
                "path": request.url.path,
                "current": current_request.get() is request,
            }

        app.build()
        route = next(
            route for route in app._compiled if route.declaration["path"] != "/docs"
        )
        from bessemer.worker import Worker

        self.assertIsNotNone(Worker._simple_plan_bindings(route))
        with TestClient(app) as client:
            response = client.get("/simple/3?q=x")
        self.assertEqual(
            response.json(),
            {"item": 3, "q": "x", "path": "/simple/3", "current": True},
        )
        self.assertEqual(ran, [3])


class FlatDependencyTests(unittest.TestCase):
    """Flattened dependency graphs call what the resolver calls, in order."""

    def _run(self, flat):
        from bessemer import BackgroundTasks
        from bessemer.worker import Worker

        calls = []
        app = App()

        def record(name, value):
            calls.append((name, value))
            return value

        async def leaf(request: Request):
            return record("leaf", request.method)

        def sync_leaf(leaf_value: Annotated[str, Depends(leaf)]):
            return record("sync_leaf", leaf_value + "!")

        async def resource(value: Annotated[str, Depends(leaf)]):
            record("resource open", value)
            try:
                yield "resource"
            finally:
                record("resource closed", value)

        def sync_resource():
            record("sync open", None)
            try:
                yield "sync"
            finally:
                record("sync closed", None)

        async def guard(
            tasks: BackgroundTasks, value: Annotated[str, Depends(sync_leaf)]
        ):
            tasks.add_task(record, "task", value)
            return record("guard", value)

        async def fresh(value: Annotated[str, Depends(leaf, use_cache=False)]):
            return record("fresh", value)

        @app.get("/", dependencies=[Depends(guard)])
        async def index(
            a: Annotated[str, Depends(fresh)],
            b: Annotated[str, Depends(fresh, use_cache=False)],
            c: Annotated[str, Depends(resource)],
            d: Annotated[str, Depends(sync_resource)],
            e: Annotated[str, Depends(leaf)],
            q: int = 0,
        ):
            return record("handler", [q, a, b, c, d, e])

        with Worker(app) as worker:
            if not flat:
                worker._flat_plans.clear()
            else:
                self.assertTrue(worker._flat_plans)
            result = worker.submit(0, {"q": 3}).result(timeout=5)
        return calls, result

    def test_the_flat_plan_matches_the_resolver(self):
        flat_calls, flat_result = self._run(flat=True)
        calls, result = self._run(flat=False)
        self.assertEqual(flat_calls, calls)
        self.assertEqual(flat_result, result)
        self.assertEqual(calls.count(("leaf", "GET")), 3)


@unittest.skipUnless(NATIVE, "build the native extension first")
class NativeRequestTests(unittest.TestCase):
    """A native request converts its parts on first use, with the values a
    request built from the metadata mapping had."""

    def _app(self, **options):
        app = App(**options)

        @app.post("/echo/{name}")
        async def echo(name: str, request: Request):
            return {
                "name": name,
                "method": request.method,
                "path": request.path,
                "query": request.query_string,
                "scheme": request.scheme,
                "headers": [list(pair) for pair in request.headers._items],
                "repeated": request.headers.getlist("x-many"),
                "path_params": dict(request.path_params),
                "cookies": dict(request.cookies),
                "client": request.client.host if request.client else None,
                "certificate": request.client_certificate,
                "body": (await request.body()).decode(),
                "request_id": request.request_id,
                "missing": hasattr(request, "not_an_attribute"),
            }

        return app

    @staticmethod
    def _raw(client, headers):
        """POST /echo/abc with repeated header fields; the decoded body."""
        import http.client
        import json

        connection = http.client.HTTPConnection("127.0.0.1", client._port, timeout=5)
        try:
            connection.putrequest("POST", "/echo/abc")
            for name, value in headers:
                connection.putheader(name, value)
            connection.putheader("Content-Length", "0")
            connection.endheaders()
            return json.loads(connection.getresponse().read())
        finally:
            connection.close()

    def test_parts_match_the_request(self):
        from bessemer.testclient import TestClient

        with TestClient(self._app()) as client:
            response = client.post(
                "/echo/abc?x=1",
                content=b"payload",
                headers={"X-Many": "1", "Cookie": "a=1; b=2"},
            )
            second = self._raw(client, [("x-many", "1"), ("x-many", "2")])
        body = response.json()
        self.assertEqual(
            {key: body[key] for key in ("name", "method", "path", "query", "scheme")},
            {
                "name": "abc",
                "method": "POST",
                "path": "/echo/abc",
                "query": "x=1",
                "scheme": "http",
            },
        )
        self.assertIn(["x-many", "1"], body["headers"])
        self.assertTrue(all(name == name.lower() for name, _ in body["headers"]))
        self.assertEqual(body["path_params"], {"name": "abc"})
        self.assertEqual(body["cookies"], {"a": "1", "b": "2"})
        self.assertEqual(body["client"], "127.0.0.1")
        self.assertIsNone(body["certificate"])
        self.assertEqual(body["body"], "payload")
        self.assertEqual(body["request_id"], response.headers["x-request-id"])
        self.assertFalse(body["missing"])
        self.assertEqual(second["repeated"], ["1", "2"])

    def test_request_id_falls_back_to_the_last_header(self):
        from bessemer.testclient import TestClient

        with TestClient(self._app(request_id=False)) as client:
            sent = self._raw(client, [("x-request-id", "one"), ("x-request-id", "two")])
            none = client.post("/echo/x")
        self.assertEqual(sent["request_id"], "two")
        self.assertIsNone(none.json()["request_id"])


class WebSocketReceiveTests(unittest.TestCase):
    """Receives keep their order, types and errors across cancellation."""

    def test_receives_cancellation_and_close(self):
        from bessemer.websockets import WebSocket, WebSocketDisconnect

        class Channel:
            def __init__(self):
                self.acks = 0
                self.sent = []
                self.push = None

            def accept(self, push, subprotocol):
                self.push = push

            def ack(self, count):
                self.acks += count

            def send(self, opcode, data):
                self.sent.append((opcode, data))
                return False

        async def scenario():
            loop = asyncio.get_running_loop()
            channel = Channel()
            socket = WebSocket(channel, Request(), loop)
            with self.assertRaises(RuntimeError):
                await socket.receive()
            await socket.accept()
            # A cancelled receive leaves no waiter behind and loses nothing.
            pending = asyncio.ensure_future(socket.receive_text())
            await asyncio.sleep(0)
            pending.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await pending
            self.assertFalse(socket._waiters)
            first = asyncio.ensure_future(socket.receive_text())
            second = asyncio.ensure_future(socket.receive())
            await asyncio.sleep(0)
            channel.push([(1, "a"), (2, b"b")])
            self.assertEqual(await first, "a")
            self.assertEqual(await second, b"b")
            channel.push([(2, b"c"), (1, "d"), (8, 1001)])
            with self.assertRaises(TypeError):
                await socket.receive_text()
            with self.assertRaises(TypeError):
                await socket.receive_bytes()
            with self.assertRaises(WebSocketDisconnect) as caught:
                await socket.receive()
            self.assertEqual(caught.exception.code, 1001)
            with self.assertRaises(WebSocketDisconnect):
                await socket.receive_bytes()
            await socket.send_text("x")
            await socket.send_bytes(bytearray(b"y"))
            self.assertEqual(channel.sent, [(1, b"x"), (2, b"y")])
            self.assertEqual(channel.acks, 4)

        asyncio.run(scenario())


class ServerSentEventTests(unittest.TestCase):
    """format_event writes what json.dumps(jsonable(data)) wrote."""

    def test_event_data_matches_the_normalizing_encoder(self):
        import json
        from datetime import date

        from bessemer.responses import format_event, jsonable

        for data in (
            {"data": "e"},
            {"b": 1.5e-07, "a": [True, None, 2**70, -0.0, 1e16]},
            [1, "\u00e9\u2028\x00"],
            (1, 2),
            {"when": date(2026, 11, 8)},
            "plain\ntext",
            7,
        ):
            with self.subTest(data=data):
                text = (
                    data
                    if isinstance(data, str)
                    else json.dumps(
                        jsonable(data), ensure_ascii=False, separators=(",", ":")
                    )
                )
                expected = (
                    "\n".join(f"data: {line}" for line in text.split("\n")) + "\n\n"
                ).encode()
                self.assertEqual(format_event(data), expected)
        for data, error in (
            ({"k": float("nan")}, ValueError),
            ({1: 2}, TypeError),
            (_deep(70), ValueError),
        ):
            with self.subTest(data=str(data)[:20]), self.assertRaises(error):
                format_event(data)


def _deep(depth):
    value = []
    for _ in range(depth):
        value = [value]
    return value


@unittest.skipUnless(NATIVE, "build the native extension first")
class StreamProductionTests(unittest.TestCase):
    """Streams produce the same bytes whichever production path they take."""

    def test_chunk_kinds_iterators_and_limits(self):
        from bessemer import StreamingResponse
        from bessemer.testclient import TestClient

        app = App(openapi_url=None, docs_url=None)
        calls = []

        class Counter:
            """An async iterator whose __aiter__ must be called only once."""

            def __init__(self):
                self.items = [b"a", "b"]

            def __aiter__(self):
                calls.append("aiter")
                return self

            async def __anext__(self):
                if not self.items:
                    raise StopAsyncIteration
                await asyncio.sleep(0)
                return self.items.pop(0)

        @app.get("/kinds")
        async def kinds():
            async def body():
                yield b"x"
                yield "y"
                yield bytearray(b"z")
                yield memoryview(b"w")
                yield b""
                await asyncio.sleep(0)
                yield b"!"

            return StreamingResponse(body())

        @app.get("/iterator")
        async def iterator():
            return StreamingResponse(Counter())

        @app.get("/oversized")
        async def oversized():
            async def body():
                yield b"ok"
                yield b"x" * 65

            return StreamingResponse(body())

        @app.get("/wrong")
        async def wrong():
            async def body():
                yield b"ok"
                yield 1

            return StreamingResponse(body())

        with TestClient(app, server_options={"max_response_size": 64}) as client:
            self.assertEqual(client.get("/kinds").content, b"xyzw!")
            self.assertEqual(client.get("/iterator").content, b"ab")
            for path in ("/oversized", "/wrong"):
                with (
                    self.assertLogs("bessemer", "ERROR"),
                    self.assertRaises(Exception),  # noqa: B017 -- A truncated body.
                ):
                    client.get(path).content  # noqa: B018
        self.assertEqual(calls, ["aiter"])


@unittest.skipUnless(NATIVE, "build the native extension first")
class RequestPathGarbageTests(unittest.TestCase):
    """Requests leave no reference cycles for the collector to find."""

    def test_middleware_dependencies_and_errors_leave_no_cycles(self):
        # gc.collect() counts garbage process-wide. Other suites' stopped
        # threads and delayed finalizers can otherwise contaminate this
        # request-cycle measurement, especially on free-threaded CPython.
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "unittest",
                f"{__name__}.RequestPathGarbageTests._check_request_cycles",
            ],
            cwd=Path(__file__).resolve().parent,
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def _check_request_cycles(self):
        from bessemer.testclient import TestClient

        app = App()
        torn_down = []

        @app.middleware("http")
        async def outer(request, call_next):
            response = await call_next(request)
            response.headers["x-outer"] = "1"
            return response

        @app.middleware("http")
        async def inner(request, call_next):
            response = await call_next(request)
            response.headers["x-inner"] = "1"
            return response

        async def session():
            yield {"s": 1}
            torn_down.append(True)

        async def deny():
            raise HTTPException(403, "denied")

        @app.get("/yield")
        async def with_session(s: Annotated[dict, Depends(session)]):
            return s

        @app.get("/raise")
        async def raising():
            raise HTTPException(404, "nope", headers={"x-reason": "gone"})

        @app.get("/deny")
        async def denied(value: Annotated[int, Depends(deny)]):
            return {}

        with TestClient(app) as client:
            for path in ("/yield", "/raise", "/deny"):
                for _ in range(20):
                    client.get(path)
            gc.collect()
            gc.disable()
            try:
                responses = [
                    client.get(path)
                    for _ in range(50)
                    for path in ("/yield", "/raise", "/deny")
                ]
                garbage = gc.collect()
            finally:
                gc.enable()
        self.assertEqual(
            [
                (response.status_code, response.headers["x-inner"])
                for response in responses[:3]
            ],
            [(200, "1"), (404, "1"), (403, "1")],
        )
        self.assertEqual(responses[1].json(), {"detail": "nope"})
        self.assertEqual(responses[1].headers["x-reason"], "gone")
        self.assertEqual(len(torn_down), 70)
        # Before, each middleware request left about 40 objects in cycles.
        self.assertLess(garbage, 150)


@unittest.skipUnless(NATIVE, "build the native extension first")
class DependencyCleanupTests(unittest.TestCase):
    """Cleanup runs in the request's task only where nothing else cancels it."""

    @staticmethod
    def _slow_cleanup_app(events):
        app = App()

        async def resource():
            try:
                yield 1
            finally:
                events.append("cleanup started")
                await asyncio.sleep(0.2)
                events.append("cleanup finished")

        @app.get("/resource")
        async def with_resource(value: Annotated[int, Depends(resource)]):
            return {"value": value}

        return app

    @staticmethod
    def _wait_for(events, event):
        for _ in range(500):
            if event in events:
                return
            time.sleep(0.01)

    def test_worker_shutdown_waits_for_inline_cleanup(self):
        import concurrent.futures

        from bessemer.worker import Worker

        events = []
        app = self._slow_cleanup_app(events)
        with Worker(app) as worker:
            future = worker.submit(0, {})
            self._wait_for(events, "cleanup started")
            # Leaving the block shuts the worker down while cleanup awaits.
        self.assertEqual(events, ["cleanup started", "cleanup finished"])
        try:
            future.result(timeout=0)
        except concurrent.futures.CancelledError:
            pass

    def test_server_shutdown_and_disconnect_wait_for_inline_cleanup(self):
        import http.client

        from bessemer.testclient import TestClient

        for ending in ("shutdown", "disconnect"):
            with self.subTest(ending=ending):
                events = []
                app = self._slow_cleanup_app(events)
                client = TestClient(app)
                client.__enter__()
                try:
                    connection = http.client.HTTPConnection(
                        "127.0.0.1", client._port, timeout=5
                    )
                    connection.request("GET", "/resource")
                    self._wait_for(events, "cleanup started")
                    if ending == "disconnect":
                        connection.sock.close()
                        connection.close()
                        self._wait_for(events, "cleanup finished")
                finally:
                    # A graceful shutdown while the cleanup is still awaiting.
                    client.__exit__(None, None, None)
                    connection.close()
                self.assertEqual(events, ["cleanup started", "cleanup finished"])

    def test_a_middleware_timeout_does_not_interrupt_dependency_cleanup(self):
        from bessemer import JSONResponse
        from bessemer.testclient import TestClient

        app = App()
        closed = []

        @app.middleware("http")
        async def deadline(request, call_next):
            try:
                return await asyncio.wait_for(call_next(request), 0.05)
            except TimeoutError:
                return JSONResponse({"timeout": True}, status_code=503)

        async def resource():
            try:
                yield 1
            finally:
                await asyncio.sleep(0.2)
                closed.append(True)

        @app.get("/resource")
        async def with_resource(value: Annotated[int, Depends(resource)]):
            return {"value": value}

        with TestClient(app) as client:
            response = client.get("/resource")
        self.assertEqual(response.status_code, 503)
        # The timeout waited for the cleanup instead of interrupting it.
        self.assertEqual(closed, [True])

    def test_cancellation_during_inline_cleanup_waits_for_it(self):
        from bessemer.testclient import TestClient

        app = App()
        events = []

        async def resource():
            try:
                yield 1
            finally:
                events.append("cleanup started")
                await asyncio.sleep(0.15)
                events.append("cleanup finished")

        @app.get("/resource", timeout=0.05)
        async def with_resource(value: Annotated[int, Depends(resource)]):
            return {"value": value}

        with TestClient(app) as client:
            # The deadline passes during the cleanup: the request is answered
            # 504 and the cleanup still runs to its end.
            self.assertEqual(client.get("/resource").status_code, 504)
            for _ in range(200):
                if "cleanup finished" in events:
                    break
                time.sleep(0.01)
        self.assertEqual(events, ["cleanup started", "cleanup finished"])


if __name__ == "__main__":
    unittest.main()
