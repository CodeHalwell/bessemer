"""Installed native HTTP requests retain CPU work through cancellation."""

import asyncio
import concurrent.futures
import contextlib
import http.client
import json
import os
import socket
import sys
import threading
import time
import unittest
from unittest.mock import patch

from bessemer import App
from bessemer.executors import ProcessPoolExecutor, ThreadPoolExecutor
from bessemer.testclient import TestClient

LOOPS = ("asyncio", "bessemer") if sys.platform != "win32" else ("asyncio",)


def add(value, *, increment=1):
    return value + increment


def whereabouts():
    return os.getpid()


def fetch(client, path):
    connection = http.client.HTTPConnection("127.0.0.1", client._port, timeout=5)
    try:
        connection.request("GET", path)
        response = connection.getresponse()
        return response.status, json.loads(response.read())
    finally:
        connection.close()


class ObservedPool(ThreadPoolExecutor):
    def __init__(self):
        super().__init__(max_workers=1, max_pending=0)
        self.cancelled = threading.Event()

    def cancel(self, slot, work):
        super().cancel(slot, work)
        self.cancelled.set()


class CPUHttpTests(unittest.TestCase):
    def test_run_cpu_without_an_executor_leaves_the_worker_process(self):
        # On every interpreter, free-threaded ones included: see
        # `automatic_executor`.
        app = App()

        @app.get("/where")
        async def where():
            return {"pid": await app.run_cpu(whereabouts)}

        with TestClient(app) as client:
            response = client.get("/where")
            self.assertEqual(response.status_code, 200, response.text)
            self.assertNotEqual(response.json()["pid"], os.getpid())
            self.assertIs(type(app._cpu_offload.executor), ProcessPoolExecutor)

    def test_native_handler_returns_cpu_result_and_keyword_arguments(self):
        for event_loop in LOOPS:
            with self.subTest(event_loop=event_loop):
                self._check_native_result(event_loop)

    def _check_native_result(self, event_loop):
        pool = ThreadPoolExecutor(max_workers=1, max_pending=0)
        app = App(executor=pool)

        @app.get("/cpu")
        async def cpu(value: int):
            return {
                "answer": await app.run_cpu(add, value, increment=2),
                "native": type(asyncio.current_task().get_coro()).__name__ == "Call",
            }

        with TestClient(app, server_options={"event_loop": event_loop}) as client:
            self.assertEqual(
                client.get("/cpu?value=40").json(), {"answer": 42, "native": True}
            )
        self.assertTrue(pool.closed)

    def test_request_deadline_keeps_admission_until_running_cpu_work_finishes(self):
        for event_loop in LOOPS:
            with self.subTest(event_loop=event_loop):
                self._check_running_work(event_loop, "request_deadline")

    def test_cpu_timeout_answers_504_after_running_work_finishes(self):
        for event_loop in LOOPS:
            with self.subTest(event_loop=event_loop):
                self._check_running_work(event_loop, "cpu_timeout")

    def test_disconnect_keeps_admission_until_running_cpu_work_finishes(self):
        for event_loop in LOOPS:
            with self.subTest(event_loop=event_loop):
                self._check_running_work(event_loop, "disconnect")

    def _check_running_work(self, event_loop, mode):
        pool = ObservedPool()
        app = App(executor=pool)
        started, release, finished, cleaned = (threading.Event() for _ in range(4))

        def blocked():
            started.set()
            try:
                if not release.wait(5):
                    raise RuntimeError("test did not release CPU work")
                return "finished"
            finally:
                finished.set()

        @app.get("/work")
        async def work():
            try:
                result = await app.run_cpu(
                    blocked, timeout=0.02 if mode == "cpu_timeout" else None
                )
                return {"result": result}
            finally:
                cleaned.set()

        @app.get("/probe")
        async def probe():
            return {"ok": True}

        options = {
            "event_loop": event_loop,
            "max_in_flight": 1,
            "request_timeout": 0.05 if mode == "request_deadline" else 2,
        }
        with (
            concurrent.futures.ThreadPoolExecutor(max_workers=1) as requester,
            TestClient(app, server_options=options) as client,
        ):
            connection = None
            try:
                if mode == "disconnect":
                    connection = socket.create_connection(
                        ("127.0.0.1", client._port), 5
                    )
                    connection.sendall(b"GET /work HTTP/1.1\r\nHost: test\r\n\r\n")
                    response = None
                else:
                    response = requester.submit(fetch, client, "/work")
                self.assertTrue(started.wait(5))
                if connection is not None:
                    connection.close()
                self.assertTrue(pool.cancelled.wait(5))
                if mode == "request_deadline":
                    self.assertEqual(response.result(timeout=5)[0], 504)
                elif response is not None:
                    self.assertFalse(response.done())
                self.assertFalse(finished.is_set())
                self.assertFalse(cleaned.is_set())
                self.assertEqual(fetch(client, "/probe")[0], 503)
                self.assertEqual(len(pool.pending), 1)
                release.set()
                self.assertTrue(finished.wait(5))
                self.assertTrue(cleaned.wait(5))
                if response is not None:
                    self.assertEqual(response.result(timeout=5)[0], 504)
                self._assert_admission_recovers(client)
            finally:
                release.set()
                if connection is not None:
                    connection.close()
        self.assertTrue(pool.closed)
        self.assertFalse(pool.pending)

    def _assert_admission_recovers(self, client):
        deadline = time.monotonic() + 5
        while True:
            status, body = fetch(client, "/probe")
            if status == 200:
                self.assertEqual(body, {"ok": True})
                return
            self.assertEqual(status, 503)
            self.assertLess(
                time.monotonic(), deadline, "request capacity was not released"
            )
            time.sleep(0.002)

    def test_mounted_defaults_share_one_budget_and_reopen_after_lifecycle_shutdown(
        self,
    ):
        automatic = []
        lifecycle = []
        root_pool = ThreadPoolExecutor(max_workers=1, max_pending=0)

        def default_pool(processes):
            pool = ThreadPoolExecutor(max_workers=1, max_pending=0)
            pool.configure(processes)
            automatic.append(pool)
            return pool

        @contextlib.asynccontextmanager
        async def lifespan(app):
            lifecycle.append(await app.run_cpu(add, 1, increment=2))
            lifecycle.append(await child.run_cpu(add, 2, increment=3))
            yield
            lifecycle.append(await app.run_cpu(add, 3, increment=4))

        app = App(executor=root_pool, lifespan=lifespan)
        child, sibling = App(), App()

        @app.get("/root")
        async def root():
            return {"result": await app.run_cpu(add, 40, increment=2)}

        @child.get("/cpu")
        async def child_cpu():
            return {"result": await child.run_cpu(add, 20, increment=2)}

        @sibling.get("/cpu")
        async def sibling_cpu():
            return {"result": await sibling.run_cpu(add, 30, increment=2)}

        app.mount("/child", child)
        app.mount("/sibling", sibling)
        root_pools = []
        with patch("bessemer._offload.automatic_executor", side_effect=default_pool):
            for _ in range(2):
                with TestClient(app) as client:
                    shared = app._cpu_offload.default
                    self.assertIs(app._cpu_offload.executor, root_pool)
                    self.assertIs(child._cpu_offload.executor, shared)
                    self.assertIs(sibling._cpu_offload.executor, shared)
                    self.assertEqual(client.get("/root").json(), {"result": 42})
                    self.assertEqual(client.get("/child/cpu").json(), {"result": 22})
                    self.assertEqual(client.get("/sibling/cpu").json(), {"result": 32})
                    root_pools.append(root_pool.pool)
                self.assertTrue(shared.closed)
                self.assertTrue(root_pool.closed)
        self.assertEqual(lifecycle, [3, 5, 7] * 2)
        self.assertEqual(len(automatic), 2)
        self.assertIsNot(root_pools[0], root_pools[1])
        self.assertTrue(all(pool.closed for pool in automatic))


if __name__ == "__main__":
    unittest.main()
