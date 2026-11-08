"""Bounded sync stream batches preserve delivery, cancellation and contexts."""

import contextvars
import socket
import sys
import threading
import time
import unittest
from unittest.mock import patch

from bessemer import App, StreamingResponse
from bessemer.testclient import TestClient
from bessemer.worker import Worker


def collect(stream):
    chunks = []
    while (chunk := stream.next().result(timeout=5)) is not None:
        if isinstance(chunk, tuple):
            chunks.extend(chunk)
            break
        chunks.append(chunk)
    return b"".join(chunks)


class SyncStreamTests(unittest.TestCase):
    def test_small_chunks_use_bounded_batches_instead_of_one_submission_per_item(self):
        app = App()
        count, size = 4096, 64

        @app.get("/stream")
        async def route():
            return StreamingResponse(b"x" * size for _ in range(count))

        with Worker(app, sync_workers=1) as worker:
            stream = worker.submit(0, {}).result(timeout=5)[1]
            original, submissions = worker._executor.submit, []

            def submit(function, /, *args, **kwargs):
                submissions.append(None)
                return original(function, *args, **kwargs)

            with patch.object(worker._executor, "submit", new=submit):
                self.assertEqual(collect(stream), b"x" * (count * size))
            self.assertLess(len(submissions), count // 4)

    def test_first_and_later_chunks_arrive_before_a_following_step_unblocks(self):
        app = App()
        blocked, release, closed = (threading.Event() for _ in range(3))
        variable = contextvars.ContextVar("sync_stream", default="outside")
        observed = []

        def body():
            token = variable.set("inside")
            try:
                yield b"first"
                yield variable.get()
                blocked.set()
                if not release.wait(5):
                    raise RuntimeError("test did not release iterator")
                yield b"discarded after close"
            finally:
                observed.append(variable.get())
                variable.reset(token)
                closed.set()

        @app.get("/stream")
        async def route():
            return StreamingResponse(body())

        @app.get("/probe")
        async def probe():
            return "responsive"

        with Worker(app, sync_workers=1) as worker:
            try:
                stream = worker.submit(0, {}).result(timeout=5)[1]
                # Both chunks arrive while the following step is blocked; a
                # read may return them in one batch or two.
                received = stream.next().result(timeout=5)
                self.assertTrue(received.startswith(b"first"))
                self.assertTrue(blocked.wait(5))
                if received == b"first":
                    received += stream.next().result(timeout=5)
                self.assertEqual(received, b"firstinside")
                self.assertEqual(worker.submit(1, {}).result(timeout=5)[0], 200)
                first_close, second_close = stream.close(), stream.close()
                self.assertFalse(first_close.done())
                self.assertFalse(closed.is_set())
                self.assertIn(stream, worker._streams)
                release.set()
                first_close.result(timeout=5)
                second_close.result(timeout=5)
                self.assertTrue(closed.is_set())
                self.assertNotIn(stream, worker._streams)
            finally:
                release.set()
        self.assertEqual(observed, ["inside"])

    def test_close_waits_for_a_blocked_first_step_before_closing_the_iterator(self):
        app = App()
        blocked, release, closed = (threading.Event() for _ in range(3))

        def body():
            try:
                blocked.set()
                if not release.wait(5):
                    raise RuntimeError("test did not release iterator")
                yield b"discarded"
            finally:
                closed.set()

        @app.get("/stream")
        async def route():
            return StreamingResponse(body())

        with Worker(app, sync_workers=1) as worker:
            try:
                stream = worker.submit(0, {}).result(timeout=5)[1]
                reading = stream.next()
                self.assertTrue(blocked.wait(5))
                closing = stream.close()
                self.assertFalse(closing.done())
                self.assertFalse(closed.is_set())
                self.assertIn(stream, worker._streams)
                release.set()
                closing.result(timeout=5)
                self.assertIsNone(reading.result(timeout=5))
                self.assertTrue(closed.is_set())
            finally:
                release.set()

    def test_infinite_empty_iterator_does_not_monopolize_the_executor(self):
        app = App()
        entered, closed = threading.Event(), threading.Event()

        class Empty:
            def __iter__(self):
                return self

            def __next__(self):
                entered.set()
                return b""

            def close(self):
                closed.set()

        @app.get("/stream")
        async def route():
            return StreamingResponse(Empty())

        @app.get("/probe")
        def probe():
            return "not starved"

        with Worker(app, sync_workers=1) as worker:
            stream = worker.submit(0, {}).result(timeout=5)[1]
            reading = stream.next()
            self.assertTrue(entered.wait(5))
            self.assertEqual(worker.submit(1, {}).result(timeout=5)[0], 200)
            stream.close().result(timeout=5)
            self.assertIsNone(reading.result(timeout=5))
            self.assertTrue(closed.is_set())

    def test_unconsumed_bytes_stay_within_the_existing_prefetch_bound(self):
        app = App()
        filled, closed = threading.Event(), threading.Event()
        produced = []

        class Source:
            def __iter__(self):
                return self

            def __next__(self):
                produced.append(None)
                if len(produced) >= 65:
                    filled.set()
                return b"x" * 1024

            def close(self):
                closed.set()

        @app.get("/stream")
        async def route():
            return StreamingResponse(Source())

        with Worker(app, sync_workers=1) as worker:
            stream = worker.submit(0, {}).result(timeout=5)[1]
            first = stream.next().result(timeout=5)
            # Whole chunks, at most the 64 KiB buffer allowance in one read.
            self.assertTrue(first and first == b"x" * len(first))
            self.assertEqual(len(first) % 1024, 0)
            self.assertLessEqual(len(first), 65536)
            self.assertTrue(filled.wait(5))
            # The read the runtime holds plus the 64 KiB buffer, and the step
            # that found it full.
            self.assertLessEqual(len(produced), len(first) // 1024 + 65)
            stream.close().result(timeout=5)
            self.assertTrue(closed.is_set())

    def test_exception_after_data_preserves_partial_body_and_closes_once(self):
        app = App()
        closed = []

        def body():
            try:
                yield bytearray(b"first")
                yield memoryview(b"second")
                raise ValueError("broken iterator")
            finally:
                closed.append(None)

        @app.get("/stream")
        async def route():
            return StreamingResponse(body())

        with Worker(app) as worker, self.assertLogs("bessemer", level="ERROR"):
            stream = worker.submit(0, {}).result(timeout=5)[1]
            chunks = []
            with self.assertRaises(RuntimeError):
                while (chunk := stream.next().result(timeout=5)) is not None:
                    chunks.append(chunk)
            self.assertEqual(b"".join(chunks), b"firstsecond")
            self.assertEqual(closed, [None])
            self.assertNotIn(stream, worker._streams)

    def test_head_closes_without_advancing_the_sync_iterator(self):
        app = App()
        advanced, closed = [], threading.Event()

        class Source:
            def __iter__(self):
                return self

            def __next__(self):
                advanced.append(None)
                return b"unexpected"

            def close(self):
                closed.set()

        @app.get("/stream")
        async def route():
            return StreamingResponse(Source())

        with TestClient(app) as client:
            response = client.head("/stream")
            self.assertEqual((response.status_code, response.content), (200, b""))
            self.assertTrue(closed.wait(5))
            self.assertEqual(advanced, [])

    def test_disconnect_keeps_native_admission_through_blocked_next_and_close(self):
        loops = (
            ("asyncio", "bessemer")
            if sys.platform in ("linux", "darwin")
            else ("asyncio",)
        )
        for event_loop in loops:
            with self.subTest(event_loop=event_loop):
                self._check_disconnect(event_loop)

    def _check_disconnect(self, event_loop):
        app = App()
        started, release_next, closing, release_close, closed = (
            threading.Event() for _ in range(5)
        )

        def body():
            try:
                started.set()
                if not release_next.wait(5):
                    raise RuntimeError("test did not release iterator next")
                yield b"discarded"
            finally:
                closing.set()
                if not release_close.wait(5):
                    raise RuntimeError("test did not release iterator close")
                closed.set()

        @app.get("/stream")
        async def route():
            return StreamingResponse(body())

        @app.get("/probe")
        async def probe():
            return {"ok": True}

        options = {"max_in_flight": 1, "sync_workers": 1, "event_loop": event_loop}
        with TestClient(app, server_options=options) as client:
            connection = socket.create_connection(("127.0.0.1", client._port), 5)
            try:
                connection.sendall(b"GET /stream HTTP/1.1\r\nHost: test\r\n\r\n")
                self.assertTrue(started.wait(5))
                connection.close()
                self.assertEqual(client.get("/probe").status_code, 503)
                release_next.set()
                self.assertTrue(closing.wait(5))
                self.assertFalse(closed.is_set())
                self.assertEqual(client.get("/probe").status_code, 503)
                release_close.set()
                self.assertTrue(closed.wait(5))
                deadline = time.monotonic() + 5
                while (response := client.get("/probe")).status_code == 503:
                    self.assertLess(time.monotonic(), deadline)
                    time.sleep(0.002)
                self.assertEqual(
                    (response.status_code, response.json()), (200, {"ok": True})
                )
            finally:
                release_next.set()
                release_close.set()
                connection.close()


if __name__ == "__main__":
    unittest.main()
