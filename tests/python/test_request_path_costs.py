"""Cheaper request-path pieces keep the behaviour of the code they replaced."""

import asyncio
import gc
import importlib.util
import time
import unittest
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
class RequestPathGarbageTests(unittest.TestCase):
    """Requests leave no reference cycles for the collector to find."""

    def test_middleware_dependencies_and_errors_leave_no_cycles(self):
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
