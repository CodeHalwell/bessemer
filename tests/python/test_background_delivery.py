"""Response delivery precedes background work without releasing admission."""

import asyncio
import contextvars
import http.client
import subprocess
import threading
import time
import unittest

from bessemer import App, BackgroundTasks, Depends, PlainTextResponse, StreamingResponse
from bessemer.background import BackgroundTask
from bessemer.request import current_request
from bessemer.testclient import TestClient
from test_http2 import curl_speaks_http2


class BackgroundDeliveryTests(unittest.TestCase):
    def test_buffered_response_precedes_ordered_tasks_and_dependency_cleanup(self):
        for loop in ("asyncio", "bessemer"):
            with self.subTest(loop=loop):
                self._check_delivery(loop, False, "GET")

    def test_streamed_response_precedes_tasks_and_retains_dependency_context(self):
        for loop in ("asyncio", "bessemer"):
            with self.subTest(loop=loop):
                self._check_delivery(loop, True, "GET")

    def test_head_background_runs_after_headers_without_releasing_capacity(self):
        self._check_delivery("bessemer", False, "HEAD")

    @unittest.skipUnless(curl_speaks_http2(), "requires curl with HTTP/2")
    def test_http2_delivery_precedes_background_for_buffered_and_streamed_bodies(self):
        for loop in ("asyncio", "bessemer"):
            for streaming in (False, True):
                with self.subTest(loop=loop, streaming=streaming):
                    self._check_delivery(loop, streaming, "GET", http2=True)

    def _check_delivery(self, loop, streaming, method, *, http2=False):
        marker = contextvars.ContextVar("delivery_context", default="outside")
        started, release, finished = (
            threading.Event(),
            threading.Event(),
            threading.Event(),
        )
        events = []
        app = App()

        async def resource():
            token = marker.set("bound")
            try:
                yield "resource"
            finally:
                events.append(("closed", marker.get(), current_request.get().path))
                marker.reset(token)
                finished.set()

        def first():
            events.append(("first", marker.get(), current_request.get().path))
            started.set()
            release.wait(5)

        async def second():
            await asyncio.sleep(0)
            events.append(("second", marker.get(), current_request.get().path))
            marker.set("later")

        def third():
            events.append(("third", marker.get(), current_request.get().path))

        dependency = Depends(resource)

        @app.get("/bg")
        async def background(tasks: BackgroundTasks, unused=dependency):
            tasks.add_task(first)
            tasks.add_task(second)
            tasks.add_task(third)
            if streaming:

                async def chunks():
                    yield b"a"
                    yield b"b"

                return StreamingResponse(chunks(), media_type="text/plain")
            return {"ok": True}

        @app.get("/next")
        async def next_request():
            return {"next": True}

        with TestClient(
            app, timeout=2, server_options={"event_loop": loop, "max_in_flight": 1}
        ) as client:
            try:
                if http2:
                    result = subprocess.run(
                        [
                            "curl",
                            "--silent",
                            "--show-error",
                            "--fail",
                            "--http2-prior-knowledge",
                            "--max-time",
                            "2",
                            "-w",
                            "\n%{http_version}",
                            f"http://127.0.0.1:{client._port}/bg",
                        ],
                        capture_output=True,
                        text=True,
                        check=True,
                        timeout=5,
                    )
                    body, _, version = result.stdout.rpartition("\n")
                    self.assertEqual(version, "2")
                    self.assertEqual(body, "ab" if streaming else '{"ok":true}')
                else:
                    response = client.request(method, "/bg")
                    self.assertEqual(response.status_code, 200)
                    if method == "GET":
                        self.assertEqual(
                            response.text, "ab" if streaming else '{"ok":true}'
                        )
                self.assertTrue(started.wait(2))
                self.assertFalse(finished.is_set())
                self.assertEqual(client.get("/next").status_code, 503)
                release.set()
                self.assertTrue(finished.wait(2))
                self.assertEqual(
                    events,
                    [
                        ("first", "bound", "/bg"),
                        ("second", "bound", "/bg"),
                        ("third", "later", "/bg"),
                        ("closed", "later", "/bg"),
                    ],
                )
            finally:
                release.set()

    def test_sync_returned_awaitable_keeps_loop_context_and_task_order(self):
        for loop in ("asyncio", "bessemer"):
            with self.subTest(loop=loop):
                self._check_sync_returned_awaitable_keeps_loop_context_and_task_order(
                    loop
                )

    def _check_sync_returned_awaitable_keeps_loop_context_and_task_order(self, loop):
        marker = contextvars.ContextVar("background_native_context", default="outside")
        finished = threading.Event()
        events = []
        app = App()

        def first():
            events.append(("sync", marker.get(), current_request.get().path))
            marker.set("thread-only")

            async def resume():
                events.append(("awaited", marker.get(), current_request.get().path))
                marker.set("loop-update")

            return resume()

        def second():
            events.append(("second", marker.get(), current_request.get().path))
            finished.set()

        @app.get("/")
        async def route(tasks: BackgroundTasks):
            marker.set("handler")
            tasks.add_task(first)
            tasks.add_task(second)
            return {"ok": True}

        with TestClient(app, server_options={"event_loop": loop}) as client:
            self.assertEqual(client.get("/").json(), {"ok": True})
            self.assertTrue(finished.wait(2))
        self.assertEqual(
            events,
            [
                ("sync", "handler", "/"),
                ("awaited", "handler", "/"),
                ("second", "loop-update", "/"),
            ],
        )

    def test_sync_stop_iteration_is_logged_and_stops_remaining_tasks(self):
        for loop in ("asyncio", "bessemer"):
            with self.subTest(loop=loop):
                self._check_sync_stop_iteration_is_logged_and_stops_remaining_tasks(
                    loop
                )

    def _check_sync_stop_iteration_is_logged_and_stops_remaining_tasks(self, loop):
        finished = threading.Event()
        ran = threading.Event()
        app = App()

        def fail():
            finished.set()
            raise StopIteration("background failed")

        @app.get("/")
        async def route(tasks: BackgroundTasks):
            tasks.add_task(fail)
            tasks.add_task(ran.set)
            return {"sent": True}

        with (
            self.assertLogs("bessemer", "ERROR") as logs,
            TestClient(app, server_options={"event_loop": loop}) as client,
        ):
            self.assertEqual(client.get("/").json(), {"sent": True})
            self.assertTrue(finished.wait(2))
        self.assertFalse(ran.is_set())
        self.assertTrue(
            any("RuntimeError after a response" in line for line in logs.output)
        )

    def test_errors_after_delivery_are_logged_and_do_not_change_response(self):
        completed = threading.Event()
        app = App()

        async def fail():
            completed.set()
            raise ValueError("background failed")

        @app.get("/")
        async def route(tasks: BackgroundTasks):
            tasks.add_task(fail)
            return {"sent": True}

        with self.assertLogs("bessemer", "ERROR") as logs, TestClient(app) as client:
            self.assertEqual(client.get("/").json(), {"sent": True})
            self.assertTrue(completed.wait(2))
        self.assertTrue(
            any("ValueError after a response" in line for line in logs.output)
        )

    def test_shutdown_cancels_async_background_and_awaits_dependency_cleanup(self):
        started, closed = threading.Event(), threading.Event()
        app = App()

        async def dependency():
            try:
                yield None
            finally:
                await asyncio.sleep(0)
                closed.set()

        async def forever():
            started.set()
            await asyncio.sleep(30)

        depends = Depends(dependency)

        @app.get("/")
        async def route(tasks: BackgroundTasks, unused=depends):
            tasks.add_task(forever)
            return {"ok": True}

        with TestClient(app) as client:
            self.assertEqual(client.get("/").status_code, 200)
            self.assertTrue(started.wait(2))
        self.assertTrue(closed.is_set())

    def test_replaced_middleware_response_preserves_route_and_response_tasks(self):
        events = []
        finished = threading.Event()
        app = App()

        def middleware_task():
            events.append("middleware")
            finished.set()

        @app.middleware("http")
        async def replace(request, call_next):
            await call_next(request)
            return PlainTextResponse(
                "replacement", background=BackgroundTask(middleware_task)
            )

        @app.get("/")
        async def route(tasks: BackgroundTasks):
            tasks.add_task(events.append, "route")
            return {"replaced": True}

        with TestClient(app) as client:
            self.assertEqual(client.get("/").text, "replacement")
            self.assertTrue(finished.wait(2))
            self.assertEqual(events, ["route", "middleware"])

    def test_abandoned_stream_skips_tasks_and_holds_capacity_through_cleanup(self):
        closing, release, closed, ran = (
            threading.Event(),
            threading.Event(),
            threading.Event(),
            threading.Event(),
        )
        app = App()

        async def resource():
            try:
                yield None
            finally:
                closing.set()
                while not release.is_set():
                    await asyncio.sleep(0.001)
                closed.set()

        depends = Depends(resource)

        @app.get("/stream")
        async def route(tasks: BackgroundTasks, unused=depends):
            tasks.add_task(ran.set)

            async def chunks():
                yield b"first"
                while True:
                    await asyncio.sleep(0.01)
                    yield b"later"

            return StreamingResponse(chunks())

        @app.get("/next")
        async def next_request():
            return {"next": True}

        with TestClient(app, server_options={"max_in_flight": 1}) as client:
            connection = http.client.HTTPConnection(
                "127.0.0.1", client._port, timeout=2
            )
            try:
                connection.request("GET", "/stream")
                response = connection.getresponse()
                self.assertEqual(response.read1(5), b"first")
                response.close()
                connection.close()
                self.assertTrue(closing.wait(2))
                self.assertEqual(client.get("/next").status_code, 503)
                release.set()
                self.assertTrue(closed.wait(2))
                deadline = time.monotonic() + 2
                while (
                    client.get("/next").status_code == 503
                    and time.monotonic() < deadline
                ):
                    time.sleep(0.001)
                self.assertEqual(client.get("/next").status_code, 200)
                self.assertFalse(ran.is_set())
            finally:
                release.set()
                connection.close()
