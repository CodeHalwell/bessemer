"""ASGI middleware preserves native file selection and cancellation safety."""

import asyncio
import tempfile
import threading
import unittest
from pathlib import Path

from bessemer import App, FileResponse
from bessemer._asgi import send_envelope
from bessemer.testclient import TestClient
from bessemer.worker import Worker
from test_asgi import HeaderMiddleware


class ASGIFileResponseTests(unittest.TestCase):
    def test_shared_conditional_and_range_selection_precedes_start(self):
        for loop in ("asyncio", "bessemer"):
            with self.subTest(loop=loop):
                self._selection(loop)

    def _selection(self, loop):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "data.txt"
            path.write_bytes(b"file contents")
            app = App()
            app.add_middleware(HeaderMiddleware)

            @app.get("/file")
            async def file():
                return FileResponse(path)

            @app.get("/empty")
            async def empty():
                return FileResponse(path, status_code=204)

            with TestClient(app, server_options={"event_loop": loop}) as client:
                full = client.get("/file")
                self.assertEqual(full.content, b"file contents")
                etag = full.headers["etag"]
                modified = full.headers["last-modified"]
                for headers, status, content in (
                    ({"range": "bytes=0-3"}, 206, b"file"),
                    ({"range": "bytes=-8"}, 206, b"contents"),
                    ({"range": "bytes=100-200"}, 416, b""),
                    ({"if-none-match": etag}, 304, b""),
                    ({"if-modified-since": modified}, 304, b""),
                    ({"range": "bytes=0-3", "if-range": etag}, 206, b"file"),
                    (
                        {"range": "bytes=0-3", "if-range": '"other"'},
                        200,
                        b"file contents",
                    ),
                ):
                    with self.subTest(headers=headers):
                        response = client.get("/file", headers=headers)
                        self.assertEqual(
                            (response.status_code, response.content),
                            (status, content),
                        )
                        self.assertEqual(response.headers["x-asgi"], "1")
                head = client.head("/file", headers={"range": "bytes=0-3"})
                self.assertEqual(head.status_code, 206)
                self.assertEqual(head.headers["content-range"], "bytes 0-3/13")
                self.assertEqual(head.content, b"")
                self.assertEqual(client.get("/empty").status_code, 204)

    def test_cancellation_waits_for_pool_read_before_close_and_loop_keeps_running(self):
        for loop in ("asyncio", "bessemer"):
            with self.subTest(loop=loop):
                self._cancellation(loop)

    def _cancellation(self, loop):
        with Worker(App(), event_loop=loop) as worker:
            entered, release, closed = (threading.Event() for _ in range(3))
            observed = []

            class Body:
                def prepare(self, status, headers, request_headers, head):
                    return status, headers

                def read_chunk(self):
                    observed.append("read")
                    entered.set()
                    if not release.wait(5):
                        raise RuntimeError("test failed to release read")
                    observed.append("returned")
                    return b"x"

                def close(self):
                    observed.append("closed")
                    closed.set()

            async def run():
                async def send(message):
                    pass

                task = asyncio.create_task(
                    send_envelope(
                        (200, Body(), (), "file"),
                        send,
                        None,
                        None,
                        worker=worker,
                        scope={"headers": [], "method": "GET"},
                    )
                )
                while not entered.is_set():
                    await asyncio.sleep(0.001)
                task.cancel()
                await asyncio.sleep(0.01)
                self.assertFalse(task.done())
                self.assertFalse(closed.is_set())
                release.set()
                with self.assertRaises(asyncio.CancelledError):
                    await task
                self.assertEqual(observed, ["read", "returned", "closed"])

            try:
                asyncio.run_coroutine_threadsafe(run(), worker._loop).result(5)
            finally:
                release.set()
