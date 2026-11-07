"""Asyncio task and context behavior of native HTTP handlers."""

from __future__ import annotations

import asyncio
import contextvars
import http.client
import importlib.util
import inspect
import json
import sys
import unittest
from concurrent.futures import ThreadPoolExecutor

from bessemer import App
from bessemer.request import current_request
from bessemer.testclient import TestClient

NATIVE = importlib.util.find_spec("bessemer._native") is not None


def build_app() -> App:
    app = App()
    marker = contextvars.ContextVar("native_async_marker", default="unset")
    arrivals = 0
    both_arrived = asyncio.Event()

    @app.get("/task")
    async def task_identity():
        task = asyncio.current_task()
        coroutine = task.get_coro()
        before = task in asyncio.all_tasks()
        await asyncio.sleep(0)
        return {
            "real_task": isinstance(task, asyncio.Task),
            "native": type(coroutine).__name__ == "Call",
            "registered_before": before,
            "registered_after": task in asyncio.all_tasks(),
            "stable_task": asyncio.current_task() is task,
            "stable_coroutine": task.get_coro() is coroutine,
            "running_coroutine": inspect.getcoroutinestate(coroutine)
            == inspect.CORO_RUNNING,
            "own_context": coroutine._context is task.get_context(),
        }

    @app.get("/timeout")
    async def timeout_then_continue():
        task = asyncio.current_task()
        try:
            async with asyncio.timeout(0.01):
                await asyncio.sleep(0.1)
        except TimeoutError:
            timed_out = True
        else:
            timed_out = False
        cancellation_count = task.cancelling()
        await asyncio.sleep(0)
        return {
            "native": type(task.get_coro()).__name__ == "Call",
            "timed_out": timed_out,
            "cancellation_count": cancellation_count,
            "continued": asyncio.current_task() is task,
        }

    @app.get("/context/{label}")
    async def context_across_requests(label: str):
        nonlocal arrivals
        before = marker.get()
        request = current_request.get()
        marker.set(label)
        arrivals += 1
        if arrivals == 2:
            both_arrived.set()
        await asyncio.wait_for(both_arrived.wait(), 2.0)

        async def child():
            await asyncio.sleep(0)
            return marker.get(), current_request.get() is request

        child_marker, child_request = await asyncio.create_task(child())
        return {
            "native": type(asyncio.current_task().get_coro()).__name__ == "Call",
            "before": before,
            "after": marker.get(),
            "child_marker": child_marker,
            "child_request": child_request,
            "own_request": current_request.get() is request,
            "request_id": id(request),
        }

    return app


class _NativeAsyncExecutionTests:
    event_loop: str

    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(
            build_app(), server_options={"event_loop": cls.event_loop}
        )
        cls.client.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.client.__exit__(None, None, None)

    def test_request_uses_a_stable_registered_asyncio_task(self):
        response = self.client.get("/task")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(
            response.json(),
            {
                "real_task": True,
                "native": True,
                "registered_before": True,
                "registered_after": True,
                "stable_task": True,
                "stable_coroutine": True,
                "running_coroutine": True,
                "own_context": True,
            },
        )

    def test_expired_timeout_uncancels_and_handler_can_await_again(self):
        response = self.client.get("/timeout")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(
            response.json(),
            {
                "native": True,
                "timed_out": True,
                "cancellation_count": 0,
                "continued": True,
            },
        )

    def test_suspended_siblings_and_children_keep_their_own_context(self):
        def get(label: str):
            connection = http.client.HTTPConnection(
                "127.0.0.1", self.client._port, timeout=4.0
            )
            try:
                connection.request("GET", f"/context/{label}")
                response = connection.getresponse()
                return response.status, response.read()
            finally:
                connection.close()

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(get, label) for label in ("first", "second")]
            results = [future.result(timeout=4.0) for future in futures]
        self.assertEqual([status for status, _ in results], [200, 200], results)
        bodies = [json.loads(content) for _, content in results]
        self.assertNotEqual(bodies[0]["request_id"], bodies[1]["request_id"])
        for label, body in zip(("first", "second"), bodies, strict=True):
            self.assertIsInstance(body["request_id"], int)
            self.assertEqual(
                {key: value for key, value in body.items() if key != "request_id"},
                {
                    "native": True,
                    "before": "unset",
                    "after": label,
                    "child_marker": label,
                    "child_request": True,
                    "own_request": True,
                },
            )


@unittest.skipUnless(NATIVE, "build the native extension first")
class NativeAsyncioExecutionTests(_NativeAsyncExecutionTests, unittest.TestCase):
    event_loop = "asyncio"


@unittest.skipUnless(
    NATIVE and sys.platform in ("linux", "darwin"),
    "the Rust loop needs Linux or macOS",
)
class NativeRustLoopExecutionTests(_NativeAsyncExecutionTests, unittest.TestCase):
    event_loop = "bessemer"


if __name__ == "__main__":
    unittest.main()
