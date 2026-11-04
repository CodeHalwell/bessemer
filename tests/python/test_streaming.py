"""Streaming responses through the installed native runtime."""

import http.client
import importlib.util
import json
import socket
import time
import unittest

from bessemer import App, EventSourceResponse, StreamingResponse
from bessemer.responses import format_event
from test_native_integration import Server

APPLICATION = """
import asyncio
from pathlib import Path
from bessemer import App, BackgroundTasks, Depends, EventSourceResponse, StreamingResponse

app = App()
app.health("/healthz")

async def resource():
    Path("resource_open").touch()
    try:
        yield "db"
    finally:
        Path("resource_closed").write_text("closed")

def note(name):
    Path(name).touch()

@app.get("/chunks")
async def chunks(tasks: BackgroundTasks, value=Depends(resource)):
    tasks.add_task(note, "background_ran")
    async def body():
        for index in range(3):
            assert not Path("resource_closed").exists()
            yield f"{value}-{index};"
            await asyncio.sleep(0.05)
    return StreamingResponse(body(), media_type="text/plain")

@app.get("/sync")
def sync_stream():
    return StreamingResponse((part for part in [b"a", "b", bytearray(b"c")]), headers={"X-Kind": "sync"})

@app.get("/slow")
async def slow():
    async def body():
        try:
            yield "first;"
            Path("first_sent").touch()
            await asyncio.sleep(30)
            yield "never"
        finally:
            await asyncio.sleep(0.05)
            Path("generator_closed").touch()
    return StreamingResponse(body())

@app.get("/broken")
async def broken():
    async def body():
        yield "partial;"
        raise RuntimeError("boom")
    return StreamingResponse(body())

@app.get("/events")
async def events():
    async def source():
        yield {"n": 1}
        yield "plain"
    return EventSourceResponse(source())

@app.get("/context")
async def context():
    from bessemer.request import current_request
    async def body():
        await asyncio.sleep(0.01)
        yield current_request.get().path
    return StreamingResponse(body())

@app.get("/big")
async def big():
    async def body():
        for _ in range(64):
            yield b"x" * 65536
    return StreamingResponse(body())
"""


class StreamingResponseUnitTests(unittest.TestCase):
    def test_constructor_and_event_encoding(self):
        with self.assertRaises(TypeError):
            StreamingResponse(b"buffered")
        with self.assertRaises(TypeError):
            StreamingResponse(42)
        response = StreamingResponse(iter([b"a"]), media_type="text/csv")
        self.assertEqual(response.headers["content-type"], "text/csv; charset=utf-8")
        self.assertEqual(
            format_event({"a": 1}, event="update", id="7", retry=10),
            b'event: update\nid: 7\nretry: 10\ndata: {"a":1}\n\n',
        )
        self.assertEqual(format_event("two\nlines"), b"data: two\ndata: lines\n\n")
        self.assertEqual(format_event(comment="ping"), b": ping\n\n")
        with self.assertRaises(ValueError):
            format_event("x", event="bad\nname")
        events = EventSourceResponse(iter(["x"]))
        self.assertEqual(
            events.headers["content-type"], "text/event-stream; charset=utf-8"
        )
        self.assertEqual(events.headers["cache-control"], "no-cache")
        self.assertEqual(list(events.body_iterator), [b"data: x\n\n"])

    def test_event_source_closes_the_iterator_its_source_returns(self):
        import asyncio

        events = []

        class Source:
            def __aiter__(self):
                async def generate():
                    try:
                        yield "one"
                        yield "two"
                    finally:
                        await asyncio.sleep(0.01)
                        events.append("closed")

                return generate()

        async def run():
            body = EventSourceResponse(Source()).body_iterator
            self.assertEqual(await anext(body), b"data: one\n\n")
            await body.aclose()
            # The source's async finalizer has run, not been left to the GC.
            self.assertEqual(events, ["closed"])

        asyncio.run(run())

    def test_event_source_closes_its_source_before_iteration_starts(self):
        import asyncio

        closed = []

        class AsyncSource:
            def __aiter__(self):
                return self

            async def __anext__(self):
                return "event"

            async def aclose(self):
                closed.append("async")

        class SyncSource:
            def __iter__(self):
                return self

            def __next__(self):
                return "event"

            def close(self):
                closed.append("sync")

        async def run():
            # A HEAD request closes the body without pulling a chunk.
            await EventSourceResponse(AsyncSource()).body_iterator.aclose()

        asyncio.run(run())
        EventSourceResponse(SyncSource()).body_iterator.close()
        self.assertEqual(closed, ["async", "sync"])

    def test_close_settles_a_read_waiting_on_the_iterator(self):
        import asyncio

        from bessemer.worker import Worker

        app = App()

        @app.get("/forever")
        async def forever():
            async def body():
                await asyncio.sleep(3600)
                yield b"never"

            return StreamingResponse(body())

        with Worker(app) as worker:
            envelope = worker.submit(0, {}).result(timeout=5)
            self.assertEqual(envelope[3], "stream")
            stream = envelope[1]
            pending = stream.next()
            time.sleep(0.05)
            stream.close().result(timeout=5)
            self.assertIsNone(pending.result(timeout=5))
            self.assertFalse(worker._stream_tasks)
            self.assertFalse(worker._streams)

    def test_a_second_close_does_not_interrupt_iterator_cleanup(self):
        import asyncio

        from bessemer.worker import Worker

        events = []
        app = App()

        @app.get("/slow-cleanup")
        async def slow_cleanup():
            async def body():
                try:
                    await asyncio.sleep(3600)
                    yield b"never"
                finally:
                    events.append("cleanup started")
                    await asyncio.sleep(0.2)
                    events.append("cleanup finished")

            return StreamingResponse(body())

        worker = Worker(app)
        with worker:
            stream = worker.submit(0, {}).result(timeout=5)[1]
            pending = stream.next()
            time.sleep(0.05)
            first = stream.close()
            deadline = time.monotonic() + 5
            while "cleanup started" not in events:
                self.assertLess(time.monotonic(), deadline)
                time.sleep(0.01)
            # Shutdown closes open streams again while cleanup is running.
            second = stream.close()
            first.result(timeout=5)
            second.result(timeout=5)
            self.assertIsNone(pending.result(timeout=5))
        self.assertEqual(events, ["cleanup started", "cleanup finished"])

    def test_empty_chunks_do_not_starve_the_event_loop(self):
        import asyncio

        from bessemer.worker import Worker

        app = App()
        ticks = []

        @app.get("/empty")
        async def empty():
            async def body():
                while True:
                    yield b""

            return StreamingResponse(body())

        @app.get("/tick")
        async def tick():
            await asyncio.sleep(0.01)
            ticks.append(True)
            return {}

        with Worker(app) as worker:
            stream = worker.submit(0, {}).result(timeout=5)[1]
            pending = stream.next()
            time.sleep(0.05)
            # An unrelated request still completes while the stream spins.
            worker.submit(1, {}).result(timeout=5)
            self.assertEqual(ticks, [True])
            stream.close().result(timeout=5)
            self.assertIsNone(pending.result(timeout=5))

    def test_shutdown_closes_streams_in_their_own_context(self):
        import contextvars

        from bessemer.worker import Worker

        events = []
        marker = contextvars.ContextVar("marker")
        app = App()

        @app.get("/full")
        async def full():
            async def body():
                token = marker.set("stream")
                try:
                    while True:
                        yield b"x" * 40_000
                finally:
                    marker.reset(token)
                    events.append("reset")

            return StreamingResponse(body())

        with Worker(app) as worker:
            stream = worker.submit(0, {}).result(timeout=5)[1]
            self.assertTrue(stream.next().result(timeout=5))
            time.sleep(0.05)  # The producer is now blocked on buffer space.
        self.assertEqual(events, ["reset"])

    def test_dependency_cleanup_failure_truncates_the_stream(self):
        from bessemer import Depends
        from bessemer.worker import Worker

        app = App()

        def transaction():
            yield "tx"
            raise RuntimeError("commit failed")

        @app.get("/rows")
        async def rows(tx=Depends(transaction)):  # noqa: B008
            return StreamingResponse(iter([b"row"]))

        with Worker(app) as worker:
            stream = worker.submit(0, {}).result(timeout=5)[1]
            self.assertEqual(stream.next().result(timeout=5), b"row")
            # The body must not end successfully when its cleanup failed.
            with self.assertRaises(RuntimeError):
                stream.next().result(timeout=5)

    def test_a_closed_stream_unwinds_dependencies_through_their_error_path(self):
        import asyncio

        from bessemer import Depends
        from bessemer.worker import Worker

        events = []
        app = App()

        async def transaction():
            try:
                yield "tx"
            except BaseException:
                events.append("rolled back")
                raise
            else:
                events.append("committed")

        @app.get("/rows")
        async def rows(tx=Depends(transaction)):  # noqa: B008
            async def body():
                yield b"row"
                await asyncio.sleep(3600)
                yield b"never"

            return StreamingResponse(body())

        with Worker(app) as worker:
            stream = worker.submit(0, {}).result(timeout=5)[1]
            self.assertEqual(stream.next().result(timeout=5), b"row")
            stream.close().result(timeout=5)  # The client disconnected.
        self.assertEqual(events, ["rolled back"])

    def test_close_interrupts_background_tasks_after_the_last_chunk(self):
        import asyncio

        from bessemer import BackgroundTasks, Depends
        from bessemer.worker import Worker

        events = []
        app = App()

        async def transaction():
            try:
                yield "tx"
            except BaseException:
                events.append("rolled back")
                raise

        async def forever():
            events.append("background started")
            await asyncio.sleep(3600)

        @app.get("/rows")
        async def rows(tasks: BackgroundTasks, tx=Depends(transaction)):  # noqa: B008
            tasks.add_task(forever)
            return StreamingResponse(iter([b"row"]))

        with Worker(app) as worker:
            stream = worker.submit(0, {}).result(timeout=5)[1]
            self.assertEqual(stream.next().result(timeout=5), b"row")
            # EOF starts the background task, which never finishes.
            pending = stream.next()
            deadline = time.monotonic() + 5
            while "background started" not in events:
                self.assertLess(time.monotonic(), deadline)
                time.sleep(0.01)
            stream.close().result(timeout=5)  # Disconnect or idle timeout.
            with self.assertRaises(RuntimeError):
                pending.result(timeout=5)
        self.assertEqual(events, ["background started", "rolled back"])

    def test_cancelled_sync_handler_does_not_start_its_stream(self):
        import concurrent.futures

        from bessemer import Depends
        from bessemer.worker import Worker

        events = []
        app = App()

        def resource():
            try:
                yield "db"
            finally:
                events.append("closed")

        @app.get("/slow")
        def slow(value=Depends(resource)):  # noqa: B008
            time.sleep(0.3)
            return StreamingResponse(iter([b"never"]))

        with Worker(app) as worker:
            future = worker.submit(0, {})
            time.sleep(0.1)
            # Running sync work cannot be interrupted, so cancel is declined.
            self.assertFalse(future.cancel())
            with self.assertRaises(concurrent.futures.CancelledError):
                future.result(timeout=5)
            self.assertFalse(worker._streams)
            self.assertEqual(events, ["closed"])

    def test_sync_iterators_keep_one_context_across_chunks(self):
        import contextvars

        from bessemer.worker import Worker

        variable = contextvars.ContextVar("step", default="unset")
        app = App()

        @app.get("/context")
        def scoped():
            def body():
                token = variable.set("inside")
                try:
                    yield b"a"
                    yield variable.get().encode()
                finally:
                    variable.reset(token)

            return StreamingResponse(body())

        with Worker(app) as worker:
            stream = worker.submit(0, {}).result(timeout=5)[1]
            chunks = []
            while (chunk := stream.next().result(timeout=5)) is not None:
                chunks.append(chunk)
            self.assertEqual(b"".join(chunks), b"ainside")

    def test_routes_returning_streams_build(self):
        app = App()

        @app.get("/stream")
        async def stream():
            return StreamingResponse(iter([b"a"]))

        app.build()


@unittest.skipUnless(
    importlib.util.find_spec("bessemer._native"), "build the native extension first"
)
class StreamingHTTPTests(unittest.TestCase):
    def setUp(self):
        self.server = Server(application=APPLICATION, max_body_size=1024)

    def tearDown(self):
        self.server.close()

    def connection(self):
        return http.client.HTTPConnection("127.0.0.1", self.server.port, timeout=5)

    def get(self, path, method="GET"):
        connection = self.connection()
        try:
            connection.request(method, path)
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()

    def wait_for(self, name, timeout=5.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if (self.server.root / name).exists():
                return True
            time.sleep(0.01)
        return False

    def test_chunks_are_chunked_and_cleanup_runs_after_the_last_chunk(self):
        status, headers, body = self.get("/chunks")
        self.assertEqual((status, body), (200, b"db-0;db-1;db-2;"))
        self.assertEqual(headers["transfer-encoding"], "chunked")
        self.assertNotIn("content-length", headers)
        self.assertEqual(headers["content-type"], "text/plain; charset=utf-8")
        self.assertTrue(self.wait_for("resource_closed"))
        self.assertTrue(self.wait_for("background_ran"))
        status, headers, body = self.get("/sync")
        self.assertEqual((status, body, headers["x-kind"]), (200, b"abc", "sync"))
        self.assertEqual(self.get("/context")[2], b"/context")

    def test_first_chunk_arrives_before_the_iterator_finishes(self):
        connection = self.connection()
        try:
            connection.request("GET", "/slow")
            response = connection.getresponse()
            self.assertEqual(response.status, 200)
            self.assertEqual(response.read1(64), b"first;")
        finally:
            connection.close()

    def test_disconnect_closes_the_iterator_and_releases_capacity(self):
        # The server admits one request at a time, so a follow-up succeeding
        # proves the slot was returned after the generator's finalizer ran.
        with socket.create_connection(("127.0.0.1", self.server.port)) as client:
            client.sendall(b"GET /slow HTTP/1.1\r\nHost: test\r\n\r\n")
            self.assertTrue(self.wait_for("first_sent"))
        self.assertTrue(self.wait_for("generator_closed"))
        deadline = time.monotonic() + 5
        while self.get("/sync")[0] != 200 and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertEqual(self.get("/sync")[0], 200)

    def test_failures_truncate_the_stream_and_head_closes_it(self):
        connection = self.connection()
        try:
            connection.request("GET", "/broken")
            response = connection.getresponse()
            self.assertEqual(response.status, 200)
            with self.assertRaises(http.client.IncompleteRead) as caught:
                response.read()
            self.assertEqual(caught.exception.partial, b"partial;")
        finally:
            connection.close()
        self.assertEqual(self.get("/healthz")[0], 200)
        # HEAD never iterates the body, but dependency cleanup still runs.
        status, _, body = self.get("/chunks", method="HEAD")
        self.assertEqual((status, body), (200, b""))
        self.assertTrue(self.wait_for("resource_closed"))
        self.assertFalse((self.server.root / "background_ran").exists())

    def test_server_sent_events_and_large_streams(self):
        status, headers, body = self.get("/events")
        self.assertEqual(status, 200)
        self.assertEqual(headers["content-type"], "text/event-stream; charset=utf-8")
        self.assertEqual(body, b'data: {"n":1}\n\ndata: plain\n\n')
        status, _, body = self.get("/big")
        self.assertEqual((status, len(body)), (200, 64 * 65536))
        self.assertEqual(json.loads(self.get("/healthz")[2]), {"ok": True})


if __name__ == "__main__":
    unittest.main()
