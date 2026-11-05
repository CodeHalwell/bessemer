"""Several asyncio event loops in one worker process (``loops``)."""

import asyncio
import contextlib
import http.client
import json
import os
import threading
import time
import unittest

from bessemer import App, BackgroundTasks
from bessemer.config import settings
from bessemer.responses import StreamingResponse
from bessemer.testclient import TestClient
from bessemer.worker import Worker


def parallel(function, count):
    results = [None] * count
    errors = []

    def call(index):
        try:
            results[index] = function()
        except BaseException as error:  # noqa: BLE001 - reported below
            errors.append(error)

    threads = [threading.Thread(target=call, args=(i,)) for i in range(count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)
    if errors:
        raise errors[0]
    return results


def fetch(client, method, path):
    """One request on its own connection, so requests run concurrently."""
    connection = http.client.HTTPConnection("127.0.0.1", client._port, timeout=10)
    try:
        connection.request(method, path)
        response = connection.getresponse()
        return response.status, response.read()
    finally:
        connection.close()


class EventLoopTests(unittest.TestCase):
    def test_requests_spread_over_every_loop(self):
        app = App()

        @app.get("/where")
        async def where():
            await asyncio.sleep(0.05)
            return {"thread": threading.current_thread().name}

        with TestClient(app, server_options={"loops": 3}) as client:

            def call():
                return json.loads(fetch(client, "GET", "/where")[1])["thread"]

            names = set(parallel(call, 24))
        self.assertEqual(
            names, {"bessemer-asyncio", "bessemer-asyncio-1", "bessemer-asyncio-2"}
        )

    def test_a_blocked_loop_does_not_hold_up_the_others(self):
        app = App()
        entered = threading.Event()

        @app.get("/block")
        async def block():
            entered.set()
            time.sleep(1.0)  # noqa: ASYNC251 -- Blocks this loop on purpose.
            return {"ok": True}

        @app.get("/quick")
        async def quick():
            return {"ok": True}

        with TestClient(app, server_options={"loops": 2}) as client:
            blocked = threading.Thread(target=fetch, args=(client, "GET", "/block"))
            blocked.start()
            self.assertTrue(entered.wait(5))
            started = time.monotonic()
            status, _ = fetch(client, "GET", "/quick")
            elapsed = time.monotonic() - started
            blocked.join(10)
        self.assertEqual(status, 200)
        self.assertLess(elapsed, 0.8)

    def test_streams_sync_handlers_and_background_tasks_on_extra_loops(self):
        app = App()
        done = []

        @app.get("/stream")
        async def stream(n: int):
            async def chunks():
                for index in range(n):
                    await asyncio.sleep(0)
                    yield f"{index},".encode()

            return StreamingResponse(chunks(), media_type="text/plain")

        @app.get("/sync")
        def sync(value: int):
            return {"value": value * 2}

        @app.post("/later")
        async def later(background: BackgroundTasks):
            background.add_task(done.append, threading.current_thread().name)
            return {"ok": True}

        with TestClient(app, server_options={"loops": 4}) as client:
            bodies = parallel(lambda: fetch(client, "GET", "/stream?n=50"), 16)
            expected = "".join(f"{i}," for i in range(50)).encode()
            self.assertEqual(set(bodies), {(200, expected)})
            values = parallel(lambda: fetch(client, "GET", "/sync?value=21"), 16)
            self.assertEqual(values, [(200, b'{"value":42}')] * 16)
            statuses = parallel(lambda: fetch(client, "POST", "/later")[0], 16)
            self.assertEqual(statuses, [200] * 16)
        self.assertEqual(len(done), 16)

    def test_shutdown_finishes_every_loop_before_lifespan_cleanup(self):
        order = []

        @contextlib.asynccontextmanager
        async def lifespan(app):
            yield
            order.append("lifespan")

        app = App(lifespan=lifespan)
        entered = threading.Semaphore(0)

        @app.get("/wait")
        async def wait():
            entered.release()
            try:
                await asyncio.Event().wait()
            finally:
                await asyncio.sleep(0.05)
                order.append(threading.current_thread().name)

        client = TestClient(app, server_options={"loops": 3, "graceful_timeout": 0.5})
        client.start()
        waiting = [
            threading.Thread(target=_quietly, args=(fetch, client, "GET", "/wait"))
            for _ in range(3)
        ]
        for thread in waiting:
            thread.start()
        # Equal loads rotate, so the three requests land on three loops.
        for _ in waiting:
            self.assertTrue(entered.acquire(timeout=5))
        client.close()
        for thread in waiting:
            thread.join(10)
        self.assertEqual(order[-1], "lifespan")
        self.assertEqual(
            sorted(order[:-1]),
            ["bessemer-asyncio", "bessemer-asyncio-1", "bessemer-asyncio-2"],
        )

    def test_task_cleanup_on_every_loop_can_use_threads_at_shutdown(self):
        app = App()
        cleaned = []
        held = set()

        @app.get("/spawn")
        async def spawn():
            async def linger():
                try:
                    await asyncio.Event().wait()
                finally:
                    # The shared executor must outlive every loop's cleanup.
                    await asyncio.to_thread(time.sleep, 0.01)
                    cleaned.append(threading.current_thread().name)

            task = asyncio.get_running_loop().create_task(linger())
            held.add(task)
            await asyncio.sleep(0)
            return {"thread": threading.current_thread().name}

        with TestClient(app, server_options={"loops": 3}) as client:
            spawned = {
                json.loads(fetch(client, "GET", "/spawn")[1])["thread"]
                for _ in range(6)
            }
        self.assertEqual(len(spawned), 3)
        self.assertEqual(len(cleaned), 6)
        self.assertEqual(set(cleaned), spawned)

    def test_a_stalled_extra_loop_withholds_the_heartbeat(self):
        from bessemer import _native

        read_end, write_end = os.pipe()
        self.addCleanup(os.close, read_end)
        self.addCleanup(os.close, write_end)
        os.set_blocking(read_end, False)
        worker = Worker(App(), loops=2, heartbeat_fd=write_end)
        worker.start()
        try:
            worker.attach_native(_native.Bridge(worker.submit), _native.Bridge())

            def beats(seconds):
                time.sleep(seconds)
                try:
                    return os.read(read_end, 4096).count(b"h")
                except BlockingIOError:
                    return 0

            beats(0.6)  # Discard beats from startup.
            self.assertGreater(beats(1.2), 0)
            stalled = worker._lanes[1]
            stalled.loop.call_soon_threadsafe(time.sleep, 3.5)
            beats(1.8)  # The last tick ages past the stall limit.
            self.assertEqual(beats(1.2), 0)
            time.sleep(1.0)
            self.assertGreater(beats(1.2), 0)
        finally:
            worker.close()

    def test_settings(self):
        app = App()
        for value in (0, -1, 257, 1.5, True):
            with self.subTest(value=value), self.assertRaises(ValueError):
                Worker(app, loops=value)
        with self.assertRaises(ValueError):
            settings(_Arguments(loops=0))
        self.assertEqual(settings(_Arguments())["loops"], 1)
        self.assertEqual(settings(_Arguments(loops=8))["loops"], 8)
        worker = Worker(app, loops=2)
        with self.assertRaisesRegex(ValueError, "one bridge per event loop"):
            worker.attach_native(object())
        worker.close()


class _Arguments:
    def __init__(self, **values):
        self.__dict__.update(values)


def _quietly(function, *args):
    with contextlib.suppress(OSError, http.client.HTTPException):
        function(*args)


if __name__ == "__main__":
    unittest.main()
