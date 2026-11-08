"""Installed-wheel stress for overlapping native pool and delivery callbacks."""

import asyncio
import concurrent.futures
import contextvars
import http.client
import importlib.util
import json
import os
import sys
import sysconfig
import threading
import unittest

from bessemer import App, BackgroundTasks
from bessemer.request import Request, current_request
from bessemer.testclient import TestClient


class FreeThreadedStressTests(unittest.TestCase):
    @unittest.skipUnless(importlib.util.find_spec("pydantic"), "requires Pydantic")
    def test_model_dicts_and_nested_lists_can_be_mutated_during_encoding(self):
        from bessemer import _native
        from bessemer._model_plan import model_plan
        from pydantic import BaseModel

        class Child(BaseModel):
            id: int
            tags: list[str] = []

        class Parent(BaseModel):
            children: list[Child]

        plan = model_plan(Parent)
        child = {"id": 1, "tags": ["a"]}
        parent = {"children": [child]}
        value = [parent]
        barrier = threading.Barrier(6, timeout=10)

        def mutate():
            barrier.wait()
            for index in range(1000):
                child["id"] = index
                child["tags"] = ["a"] * (index % 4)
                parent["children"] = [child] * (index % 4)

        def encode():
            barrier.wait()
            for _ in range(300):
                encoded = _native._encode_model_list_json(plan, value, 1 << 20)
                self.assertIsNotNone(encoded)
                decoded = json.loads(encoded)
                self.assertEqual(len(decoded), 1)
                for item in decoded[0]["children"]:
                    self.assertIs(type(item["id"]), int)
                    self.assertTrue(all(tag == "a" for tag in item["tags"]))

        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as threads:
            futures = [threads.submit(mutate) for _ in range(2)]
            futures.extend(threads.submit(encode) for _ in range(4))
            for future in futures:
                future.result(20)

    def test_explicitly_disabled_gil_remains_disabled_after_native_import(self):
        if (
            not sysconfig.get_config_var("Py_GIL_DISABLED")
            or os.getenv("PYTHON_GIL") != "0"
        ):
            self.skipTest("requires PYTHON_GIL=0 on a free-threaded interpreter")
        import bessemer._native  # noqa: F401

        self.assertFalse(sys._is_gil_enabled())

    def test_overlapping_sync_contexts_and_ordered_background_completion(self):
        for loop in ("asyncio", "bessemer"):
            for lanes in (1, 2):
                with self.subTest(loop=loop, lanes=lanes):
                    self._overlap(loop, lanes)

    def _overlap(self, loop, lanes):
        app = App()
        marker = contextvars.ContextVar("stress_marker", default=None)
        barrier = threading.Barrier(4, timeout=10)
        lock = threading.Lock()
        events = {}
        completed = threading.Event()

        def first(n):
            with lock:
                events[n] = [(marker.get(), current_request.get().state.n)]
            marker.set(n)

        async def second(n):
            await asyncio.sleep(0)
            with lock:
                events[n].append((marker.get(), current_request.get().state.n))
                if len(events) == 64 and all(
                    len(value) == 2 for value in events.values()
                ):
                    completed.set()

        @app.get("/work/{n}")
        def work(n: int, background_tasks: BackgroundTasks):
            marker.set(n)
            current_request.get().state.n = n
            barrier.wait()
            background_tasks.add_task(first, n)
            background_tasks.add_task(second, n)
            return {
                "n": n,
                "context": marker.get(),
                "request_n": current_request.get().state.n,
            }

        with TestClient(
            app, server_options={"event_loop": loop, "sync_workers": 4, "loops": lanes}
        ) as client:

            def request(n):
                connection = http.client.HTTPConnection(
                    "127.0.0.1", client._port, timeout=15
                )
                try:
                    connection.request("GET", f"/work/{n}")
                    response = connection.getresponse()
                    self.assertEqual(response.status, 200)
                    self.assertEqual(
                        json.loads(response.read()),
                        {"n": n, "context": n, "request_n": n},
                    )
                finally:
                    connection.close()

            with concurrent.futures.ThreadPoolExecutor(max_workers=4) as clients:
                list(clients.map(request, range(64)))
            self.assertTrue(completed.wait(10))
        # Sync-thread ContextVar changes do not propagate back to the loop
        # or to later background tasks (the same rule as asyncio.to_thread).
        self.assertEqual(events, {n: [(None, n)] * 2 for n in range(64)})

    def test_disconnect_while_receive_waits_repeatedly(self):
        for loop in ("asyncio", "bessemer"):
            with self.subTest(loop=loop):
                self._disconnect(loop)

    def _disconnect(self, loop):
        app = App()
        started = threading.Event()
        finished = threading.Event()

        @app.get("/receive")
        async def receive(request: Request):
            await request.receive()
            started.set()
            message = await request.receive()
            if message["type"] == "http.disconnect" and await request.is_disconnected():
                finished.set()
            return {}

        with TestClient(app, server_options={"event_loop": loop}) as client:
            for _ in range(32):
                started.clear()
                finished.clear()
                connection = http.client.HTTPConnection(
                    "127.0.0.1", client._port, timeout=10
                )
                try:
                    connection.request("GET", "/receive")
                    self.assertTrue(started.wait(5))
                finally:
                    connection.close()
                self.assertTrue(finished.wait(5))
