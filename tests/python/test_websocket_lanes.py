"""WebSocket ownership, affinity and cleanup across worker event-loop lanes."""

import asyncio
import concurrent.futures
import contextlib
import contextvars
import sys
import threading
import unittest
from types import SimpleNamespace

from bessemer import App, Depends, State, WebSocket
from bessemer.request import current_request
from bessemer.testclient import TestClient
from bessemer.worker import Worker

LOOPS = ("asyncio", "bessemer") if sys.platform != "win32" else ("asyncio",)
THREADS = {"bessemer-asyncio", "bessemer-asyncio-1", "bessemer-asyncio-2"}


class WebSocketLaneTests(unittest.TestCase):
    def test_stateless_sessions_spread_and_keep_their_context_and_callback_loop(self):
        for event_loop in LOOPS:
            with self.subTest(event_loop=event_loop):
                self._check_sessions(event_loop)

    def _check_sessions(self, event_loop):
        app = App()
        marker = contextvars.ContextVar("session", default="clean")
        cleaned = []

        async def resource(websocket: WebSocket):
            before = marker.get()
            token = marker.set(websocket.query_params["id"])
            try:
                yield before
            finally:
                await asyncio.sleep(0)
                cleaned.append((marker.get(), threading.current_thread().name))
                marker.reset(token)

        dependency = Depends(resource)

        @app.websocket("/ws")
        async def session(websocket: WebSocket, before=dependency):
            await websocket.accept()
            await websocket.send_json(
                {
                    "before": before,
                    "thread": threading.current_thread().name,
                    "owns_loop": websocket._loop is asyncio.get_running_loop(),
                    "owns_request": current_request.get() is websocket.request,
                }
            )
            value = await websocket.receive_text()
            await asyncio.sleep(0)
            await websocket.send_json({"value": value, "context": marker.get()})
            await websocket.receive_text()

        options = {"loops": 3, "event_loop": event_loop}
        with (
            TestClient(app, server_options=options) as client,
            contextlib.ExitStack() as stack,
        ):
            sessions = [
                stack.enter_context(client.websocket_connect("/ws", params={"id": i}))
                for i in range(3)
            ]
            started = [socket.receive_json() for socket in sessions]
            self.assertEqual({value["thread"] for value in started}, THREADS)
            for value in started:
                self.assertEqual(value["before"], "clean")
                self.assertTrue(value["owns_loop"])
                self.assertTrue(value["owns_request"])
            for index, socket in enumerate(sessions):
                socket.send_text(f"echo-{index}")
            for index, socket in enumerate(sessions):
                self.assertEqual(
                    socket.receive_json(),
                    {"value": f"echo-{index}", "context": str(index)},
                )
        self.assertEqual({name for _, name in cleaned}, THREADS)
        self.assertEqual({value for value, _ in cleaned}, {"0", "1", "2"})

    def test_application_resources_keep_sessions_on_the_lifespan_loop(self):
        for kind in (
            "lifespan",
            "state",
            "app_dependency",
            "state_dependency",
            "mounted",
        ):
            with self.subTest(kind=kind):
                self._check_affinity(kind)

    def _check_affinity(self, kind):
        @contextlib.asynccontextmanager
        async def lifespan(app):
            app.state.loop = asyncio.get_running_loop()
            yield

        app = App(lifespan=lifespan if kind == "lifespan" else None)
        owner = App() if kind == "mounted" else app
        if kind in ("state", "mounted"):
            owner.state.resource = object()

        async def application_resource():
            return asyncio.get_running_loop()

        async def state_resource(state: State):
            return state

        async def request_resource():
            return None

        if kind == "app_dependency":
            dependency = Depends(application_resource, scope="app")
        elif kind == "state_dependency":
            dependency = Depends(state_resource)
        else:
            dependency = Depends(request_resource)

        @owner.websocket("/ws")
        async def session(websocket: WebSocket, resource=dependency):
            await websocket.accept()
            await websocket.send_json(
                {
                    "thread": threading.current_thread().name,
                    "resource_loop": kind != "app_dependency"
                    or resource is asyncio.get_running_loop(),
                    "lifespan_loop": kind != "lifespan"
                    or app.state.loop is asyncio.get_running_loop(),
                }
            )
            await websocket.receive_text()

        if owner is not app:
            app.mount("/child", owner)
        path = "/child/ws" if kind == "mounted" else "/ws"
        with (
            TestClient(app, server_options={"loops": 3}) as client,
            contextlib.ExitStack() as stack,
        ):
            sessions = [
                stack.enter_context(client.websocket_connect(path)) for _ in range(3)
            ]
            for socket in sessions:
                self.assertEqual(
                    socket.receive_json(),
                    {
                        "thread": "bessemer-asyncio",
                        "resource_loop": True,
                        "lifespan_loop": True,
                    },
                )

    def test_cancellation_retains_lane_load_until_context_cleanup_finishes(self):
        from bessemer import _native

        app = App()
        marker = contextvars.ContextVar("submission")
        entered = threading.Semaphore(0)
        cleaning = threading.Semaphore(0)
        cleaned = []
        gates = {str(index): threading.Event() for index in range(4)}
        for index in ("1", "2", "3"):
            gates[index].set()

        @app.websocket("/ws")
        async def session(websocket: WebSocket):
            identity = marker.get()
            await websocket.accept()
            entered.release()
            try:
                await asyncio.Event().wait()
            finally:
                cleaning.release()
                await asyncio.to_thread(gates[identity].wait)
                cleaned.append((marker.get(), asyncio.get_running_loop()))

        worker = Worker(app, loops=3)
        worker.start()
        try:
            worker.attach_native(
                _native.Bridge(worker.submit), _native.Bridge(), _native.Bridge()
            )
            route_id = next(
                index
                for index, route in enumerate(worker._compiled)
                if route.declaration["kind"] == "websocket"
            )

            def submit(identity):
                token = marker.set(identity)
                try:
                    return worker.submit(
                        route_id,
                        {
                            "__bessemer_request__": {"method": "GET", "path": "/ws"},
                            "__bessemer_websocket__": _Channel(),
                        },
                    )
                finally:
                    marker.reset(token)

            futures = [submit(str(index)) for index in range(3)]
            for _ in futures:
                self.assertTrue(entered.acquire(timeout=5))
            self.assertEqual(len({future._loop for future in futures}), 3)
            for future in futures:
                self.assertTrue(future.cancel())
            for _ in futures:
                self.assertTrue(cleaning.acquire(timeout=5))
            self.assertFalse(futures[0].done())
            for future in futures[1:]:
                with self.assertRaises(concurrent.futures.CancelledError):
                    future.result(timeout=5)
            replacement = submit("3")
            self.assertTrue(entered.acquire(timeout=5))
            self.assertIsNot(replacement._loop, futures[0]._loop)
            replacement.cancel()
            with self.assertRaises(concurrent.futures.CancelledError):
                replacement.result(timeout=5)
            gates["0"].set()
            with self.assertRaises(concurrent.futures.CancelledError):
                futures[0].result(timeout=5)
            self.assertEqual({identity for identity, _ in cleaned}, set(gates))
            self.assertEqual(
                {loop for _, loop in cleaned}, {future._loop for future in futures}
            )
            self.assertEqual(
                sum(
                    len(lane.tasks) + lane.pending_submissions for lane in worker._lanes
                ),
                0,
            )
        finally:
            gates["0"].set()
            worker.close()

    def test_custom_state_keeps_root_and_mounted_sessions_on_the_main_loop(self):
        class SlotState:
            __slots__ = ("resource",)

            def __init__(self):
                self.resource = object()

        class CustomState:
            pass

        class CustomNamespace(SimpleNamespace):
            pass

        for state in ({}, SlotState(), CustomState(), CustomNamespace()):
            for mounted in (False, True):
                with self.subTest(state=type(state).__name__, mounted=mounted):
                    self._check_custom_state(state, mounted)

    def _check_custom_state(self, state, mounted):
        app = App()
        owner = App() if mounted else app
        owner.state = state

        @owner.websocket("/ws")
        async def session(websocket: WebSocket):
            await websocket.accept()
            await websocket.send_json(
                {
                    "thread": threading.current_thread().name,
                    "state": websocket.app.state is state,
                }
            )
            await websocket.receive_text()

        if mounted:
            app.mount("/child", owner)
        path = "/child/ws" if mounted else "/ws"
        with (
            TestClient(app, server_options={"loops": 2}) as client,
            contextlib.ExitStack() as stack,
        ):
            sessions = [
                stack.enter_context(client.websocket_connect(path)) for _ in range(2)
            ]
            for socket in sessions:
                self.assertEqual(
                    socket.receive_json(), {"thread": "bessemer-asyncio", "state": True}
                )

    def test_shutdown_finishes_sessions_on_every_lane(self):
        app = App()
        cleaned = []

        @app.websocket("/ws")
        async def session(websocket: WebSocket):
            await websocket.accept()
            await websocket.send_text(threading.current_thread().name)
            try:
                await websocket.receive_text()
            finally:
                await asyncio.sleep(0.01)
                cleaned.append(threading.current_thread().name)

        client = TestClient(app, server_options={"loops": 3, "graceful_timeout": 0.1})
        client.start()
        self.addCleanup(client.close)
        sessions = [client.websocket_connect("/ws") for _ in range(3)]
        for socket in sessions:
            self.addCleanup(socket.close)
        self.assertEqual({socket.receive_text() for socket in sessions}, THREADS)
        client.close()
        self.assertEqual(set(cleaned), THREADS)

    def test_extra_loop_cleanup_keeps_cpu_pool_until_actual_completion(self):
        from bessemer import _native

        cpu_stopped = threading.Event()
        finish_cleanup = threading.Event()
        cleaning = threading.Semaphore(0)
        entered = threading.Semaphore(0)
        held = set()

        class TrackedApp(App):
            async def _shutdown_cpu(self):
                cpu_stopped.set()
                await super()._shutdown_cpu()

        app = TrackedApp()

        @app.websocket("/ws")
        async def session(websocket: WebSocket):
            async def orphan():
                try:
                    await asyncio.Event().wait()
                finally:
                    cleaning.release()
                    await asyncio.to_thread(finish_cleanup.wait)

            if asyncio.get_running_loop() is not worker._loop:
                held.add(asyncio.create_task(orphan()))
                await asyncio.sleep(0)
            await websocket.accept()
            entered.release()
            await asyncio.Event().wait()

        worker = Worker(app, loops=3, graceful_timeout=0.05)
        worker.start()
        try:
            worker.attach_native(
                _native.Bridge(worker.submit), _native.Bridge(), _native.Bridge()
            )
            futures = [
                worker.submit(
                    0,
                    {
                        "__bessemer_request__": {"method": "GET", "path": "/ws"},
                        "__bessemer_websocket__": _Channel(),
                    },
                )
                for _ in range(3)
            ]
            for _ in futures:
                self.assertTrue(entered.acquire(timeout=5))
            with self.assertRaises(TimeoutError):
                worker.close()
            for _ in range(2):
                self.assertTrue(cleaning.acquire(timeout=5))
            self.assertFalse(cpu_stopped.is_set())
            finish_cleanup.set()
            self.assertTrue(cpu_stopped.wait(5))
            worker._thread.join(5)
            self.assertFalse(worker._thread.is_alive())
        finally:
            finish_cleanup.set()
            worker.close()


class _Channel:
    def accept(self, callback, subprotocol):
        pass

    def close(self, code, reason):
        pass


if __name__ == "__main__":
    unittest.main()
