"""Optional SDK adapter, including input/cancel during backpressured output."""

import asyncio
import contextvars
import importlib.util
import json
import os
import sys
import time
import unittest
from pathlib import Path

from bessemer import WebSocketDisconnect
from test_native_integration import Server
from test_websockets import Client

SDK = importlib.util.find_spec("ag_ui") is not None
NATIVE = importlib.util.find_spec("bessemer._native") is not None
if os.environ.get("BESSEMER_REQUIRE_AG_UI") == "1" and not (SDK and NATIVE):
    raise RuntimeError("AG-UI CI requires the installed native wheel and optional SDK")
if SDK:
    from ag_ui.core import (
        CustomEvent,
        RunAgentInput,
        RunFinishedEvent,
        RunStartedEvent,
    )
    from pydantic import ValidationError

    from examples.ag_ui.adapter import sse_events, websocket_session


def run_input(run_id="run-1"):
    return {"threadId": "thread-1", "runId": run_id, "messages": []}


class Socket:
    def __init__(self):
        self.inbound = asyncio.Queue()
        self.outbound = asyncio.Queue()
        self.gate = asyncio.Event()
        self.gate.set()
        self.closed = None

    async def accept(self):
        pass

    async def receive_json(self):
        value = await self.inbound.get()
        if isinstance(value, BaseException):
            raise value
        return value

    async def send_text(self, value):
        await self.gate.wait()
        self.outbound.put_nowait(json.loads(value))

    async def close(self, code, reason):
        self.closed = code


async def until(predicate):
    async with asyncio.timeout(2):
        while not predicate():
            await asyncio.sleep(0)


@unittest.skipUnless(SDK, "install the optional ag-ui extra")
class AGUIAdapterTests(unittest.IsolatedAsyncioTestCase):
    async def test_sse_uses_sdk_events_and_closes_generator(self):
        closed = asyncio.Event()

        async def agent(run, inputs):
            self.assertIsNone(inputs)
            try:
                yield RunStartedEvent(thread_id=run.thread_id, run_id=run.run_id)
                yield CustomEvent(
                    name="unicode", value="caf\N{LATIN SMALL LETTER E WITH ACUTE}"
                )
                await asyncio.Event().wait()
            finally:
                await asyncio.sleep(0)
                closed.set()

        events = sse_events(RunAgentInput.model_validate(run_input()), agent)
        first = await anext(events)
        self.assertTrue(first.startswith("data: "))
        self.assertTrue(first.endswith("\n\n"))
        self.assertEqual(json.loads(first[6:])["runId"], "run-1")
        self.assertEqual(
            json.loads((await anext(events))[6:])["value"],
            "caf\N{LATIN SMALL LETTER E WITH ACUTE}",
        )
        await events.aclose()
        self.assertTrue(closed.is_set())

    async def test_incomplete_event_is_rejected_and_generator_closed(self):
        closed = asyncio.Event()

        async def agent(run, inputs):
            try:
                yield {"type": "TEXT_MESSAGE_CONTENT", "messageId": "m"}
            finally:
                closed.set()

        events = sse_events(RunAgentInput.model_validate(run_input()), agent)
        with self.assertRaises(ValidationError):
            await anext(events)
        self.assertTrue(closed.is_set())

    async def test_live_input_event_order_and_second_run(self):
        async def agent(run, inputs):
            yield RunStartedEvent(thread_id=run.thread_id, run_id=run.run_id)
            yield CustomEvent(name="opening", value=1)
            value = await inputs.get()
            yield CustomEvent(name="input", value=value)
            yield RunFinishedEvent(thread_id=run.thread_id, run_id=run.run_id)

        socket = Socket()
        session = asyncio.create_task(websocket_session(socket, agent))
        try:
            for run_id in ("run-1", "run-2"):
                await socket.inbound.put(run_input(run_id))
                self.assertEqual((await socket.outbound.get())["type"], "RUN_STARTED")
                self.assertEqual((await socket.outbound.get())["name"], "opening")
                await socket.inbound.put(
                    {"control": "input", "runId": run_id, "value": "live"}
                )
                self.assertEqual((await socket.outbound.get())["value"], "live")
                last = await socket.outbound.get()
                self.assertEqual(
                    (last["type"], last["runId"]), ("RUN_FINISHED", run_id)
                )
        finally:
            await socket.inbound.put(WebSocketDisconnect(1000))
            await asyncio.wait_for(session, 2)

    async def test_input_and_cancel_are_processed_during_blocked_send(self):
        started, closed = asyncio.Event(), asyncio.Event()
        queues = []

        async def agent(run, inputs):
            queues.append(inputs)
            started.set()
            try:
                yield RunStartedEvent(thread_id=run.thread_id, run_id=run.run_id)
            finally:
                await asyncio.sleep(0)
                closed.set()

        socket = Socket()
        socket.gate.clear()
        session = asyncio.create_task(websocket_session(socket, agent))
        try:
            await socket.inbound.put(run_input())
            await asyncio.wait_for(started.wait(), 2)
            await socket.inbound.put(
                {"control": "input", "runId": "run-1", "value": "live"}
            )
            await until(lambda: queues[0].qsize() == 1)
            self.assertEqual(queues[0].maxsize, 1)
            await socket.inbound.put({"control": "cancel", "runId": "run-1"})
            await asyncio.wait_for(closed.wait(), 2)
            self.assertTrue(socket.outbound.empty())
            socket.gate.set()
            terminal = await asyncio.wait_for(socket.outbound.get(), 2)
            self.assertEqual(
                (terminal["type"], terminal["code"]), ("RUN_ERROR", "CLIENT_CANCELLED")
            )
        finally:
            socket.gate.set()
            await socket.inbound.put(WebSocketDisconnect(1000))
            await asyncio.wait_for(session, 2)

    async def test_full_input_queue_is_bounded_and_waits_for_cleanup(self):
        started, closed = asyncio.Event(), asyncio.Event()

        async def agent(run, inputs):
            started.set()
            try:
                yield RunStartedEvent(thread_id=run.thread_id, run_id=run.run_id)
            finally:
                await asyncio.sleep(0)
                closed.set()

        socket = Socket()
        socket.gate.clear()
        session = asyncio.create_task(websocket_session(socket, agent))
        await socket.inbound.put(run_input())
        await asyncio.wait_for(started.wait(), 2)
        for value in ("one", "two"):
            await socket.inbound.put(
                {"control": "input", "runId": "run-1", "value": value}
            )
        await asyncio.wait_for(session, 2)
        self.assertEqual(socket.closed, 1008)
        self.assertTrue(closed.is_set())

    async def test_session_cancel_during_client_cancel_waits_then_remains_cancelled(
        self,
    ):
        cleaning, release, closed = asyncio.Event(), asyncio.Event(), asyncio.Event()

        async def agent(run, inputs):
            try:
                yield RunStartedEvent(thread_id=run.thread_id, run_id=run.run_id)
                await asyncio.Event().wait()
            finally:
                cleaning.set()
                await release.wait()
                closed.set()

        socket = Socket()
        session = asyncio.create_task(websocket_session(socket, agent))
        try:
            await socket.inbound.put(run_input())
            await socket.outbound.get()
            await socket.inbound.put({"control": "cancel", "runId": "run-1"})
            await asyncio.wait_for(cleaning.wait(), 2)
            for _ in range(2):
                session.cancel()
                await asyncio.sleep(0)
            self.assertFalse(session.done())
            self.assertFalse(closed.is_set())
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(session, 2)
            self.assertTrue(closed.is_set())
            self.assertTrue(socket.outbound.empty())
        finally:
            release.set()
            if not session.done():
                session.cancel()
            await asyncio.gather(session, return_exceptions=True)

    async def test_disconnect_waits_for_agent_cleanup(self):
        closed = asyncio.Event()

        async def agent(run, inputs):
            try:
                yield RunStartedEvent(thread_id=run.thread_id, run_id=run.run_id)
                await asyncio.Event().wait()
            finally:
                await asyncio.sleep(0.01)
                closed.set()

        socket = Socket()
        session = asyncio.create_task(websocket_session(socket, agent))
        await socket.inbound.put(run_input())
        await socket.outbound.get()
        await socket.inbound.put(WebSocketDisconnect(1006))
        await asyncio.wait_for(session, 2)
        self.assertTrue(closed.is_set())

    async def test_disconnect_during_failed_send_preserves_running_cleanup(self):
        cleaning, release, closed = asyncio.Event(), asyncio.Event(), asyncio.Event()
        running = []
        context = contextvars.ContextVar("agent_cleanup", default="outside")

        async def agent(run, inputs):
            running.append(asyncio.current_task())
            token = context.set("agent")
            try:
                yield RunStartedEvent(thread_id=run.thread_id, run_id=run.run_id)
            finally:
                cleaning.set()
                await release.wait()
                context.reset(token)
                closed.set()

        class DisconnectedSocket(Socket):
            async def send_text(self, value):
                raise WebSocketDisconnect(1006)

        socket = DisconnectedSocket()
        session = asyncio.create_task(websocket_session(socket, agent))
        try:
            await socket.inbound.put(run_input())
            await asyncio.wait_for(cleaning.wait(), 2)
            await socket.inbound.put(WebSocketDisconnect(1006))
            await until(lambda: running[0].cancelling() > 0)
            self.assertFalse(session.done())
            self.assertFalse(closed.is_set())
            release.set()
            await asyncio.wait_for(session, 2)
            self.assertTrue(closed.is_set())
        finally:
            release.set()
            await asyncio.gather(session, return_exceptions=True)

    async def test_repeated_cancel_during_receive_cleanup_still_closes_agent(self):
        receiving, release_receive = asyncio.Event(), asyncio.Event()
        cleaning, release_agent, closed = (
            asyncio.Event(),
            asyncio.Event(),
            asyncio.Event(),
        )
        running = []

        async def agent(run, inputs):
            running.append(asyncio.current_task())
            try:
                yield RunStartedEvent(thread_id=run.thread_id, run_id=run.run_id)
                await asyncio.Event().wait()
            finally:
                cleaning.set()
                await release_agent.wait()
                closed.set()

        class DelayedReceiveSocket(Socket):
            async def receive_json(self):
                try:
                    return await super().receive_json()
                except asyncio.CancelledError:
                    receiving.set()
                    await release_receive.wait()
                    raise

        socket = DelayedReceiveSocket()
        session = asyncio.create_task(websocket_session(socket, agent))
        try:
            await socket.inbound.put(run_input())
            await socket.outbound.get()
            session.cancel()
            await asyncio.wait_for(receiving.wait(), 2)
            session.cancel()
            await asyncio.sleep(0)
            release_receive.set()
            await asyncio.wait_for(cleaning.wait(), 2)
            self.assertFalse(session.done())
            self.assertFalse(closed.is_set())
            release_agent.set()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(session, 2)
            self.assertTrue(closed.is_set())
        finally:
            release_receive.set()
            release_agent.set()
            for task in [session, *running]:
                if not task.done():
                    task.cancel()
            await asyncio.gather(session, *running, return_exceptions=True)

    async def test_terminal_event_waits_for_generator_cleanup(self):
        cleaning, release, closed = asyncio.Event(), asyncio.Event(), asyncio.Event()

        async def agent(run, inputs):
            try:
                yield RunStartedEvent(thread_id=run.thread_id, run_id=run.run_id)
                yield RunFinishedEvent(thread_id=run.thread_id, run_id=run.run_id)
            finally:
                cleaning.set()
                await release.wait()
                closed.set()

        socket = Socket()
        session = asyncio.create_task(websocket_session(socket, agent))
        try:
            await socket.inbound.put(run_input())
            self.assertEqual((await socket.outbound.get())["type"], "RUN_STARTED")
            await asyncio.wait_for(cleaning.wait(), 2)
            self.assertTrue(socket.outbound.empty())
            release.set()
            terminal = await asyncio.wait_for(socket.outbound.get(), 2)
            self.assertEqual(terminal["type"], "RUN_FINISHED")
            self.assertTrue(closed.is_set())
        finally:
            release.set()
            await socket.inbound.put(WebSocketDisconnect(1000))
            await asyncio.wait_for(session, 2)

    async def test_disconnect_after_terminal_preserves_natural_cleanup(self):
        cleaning, release, closed = asyncio.Event(), asyncio.Event(), asyncio.Event()
        running = []
        context = contextvars.ContextVar("terminal_cleanup", default="outside")

        async def agent(run, inputs):
            running.append(asyncio.current_task())
            token = context.set("agent")
            try:
                yield RunStartedEvent(thread_id=run.thread_id, run_id=run.run_id)
                yield RunFinishedEvent(thread_id=run.thread_id, run_id=run.run_id)
            finally:
                cleaning.set()
                await release.wait()
                context.reset(token)
                closed.set()

        socket = Socket()
        session = asyncio.create_task(websocket_session(socket, agent))
        try:
            await socket.inbound.put(run_input())
            self.assertEqual((await socket.outbound.get())["type"], "RUN_STARTED")
            await asyncio.wait_for(cleaning.wait(), 2)
            await socket.inbound.put(WebSocketDisconnect(1006))
            await until(lambda: running[0].cancelling() > 0)
            self.assertFalse(session.done())
            self.assertFalse(closed.is_set())
            release.set()
            await asyncio.wait_for(session, 2)
            self.assertTrue(closed.is_set())
            self.assertTrue(socket.outbound.empty())
        finally:
            release.set()
            await asyncio.gather(session, return_exceptions=True)

    async def test_cancel_after_terminal_wins_over_cleanup_error(self):
        cleaning, release, closed = asyncio.Event(), asyncio.Event(), asyncio.Event()
        running = []

        async def agent(run, inputs):
            running.append(asyncio.current_task())
            try:
                yield RunStartedEvent(thread_id=run.thread_id, run_id=run.run_id)
                yield RunFinishedEvent(thread_id=run.thread_id, run_id=run.run_id)
            finally:
                cleaning.set()
                await release.wait()
                closed.set()
                raise ValueError("private cleanup error")

        socket = Socket()
        session = asyncio.create_task(websocket_session(socket, agent))
        try:
            await socket.inbound.put(run_input())
            self.assertEqual((await socket.outbound.get())["type"], "RUN_STARTED")
            await asyncio.wait_for(cleaning.wait(), 2)
            await socket.inbound.put({"control": "cancel", "runId": "run-1"})
            await until(lambda: running[0].cancelling() > 0)
            release.set()
            terminal = await asyncio.wait_for(socket.outbound.get(), 2)
            self.assertEqual(
                (terminal["type"], terminal["code"]), ("RUN_ERROR", "CLIENT_CANCELLED")
            )
            self.assertTrue(closed.is_set())
            self.assertTrue(socket.outbound.empty())
        finally:
            release.set()
            await socket.inbound.put(WebSocketDisconnect(1000))
            await asyncio.wait_for(session, 2)

    async def test_event_after_terminal_becomes_generic_error_after_cleanup(self):
        closed = asyncio.Event()

        async def agent(run, inputs):
            try:
                yield RunStartedEvent(thread_id=run.thread_id, run_id=run.run_id)
                yield RunFinishedEvent(thread_id=run.thread_id, run_id=run.run_id)
                yield CustomEvent(name="too_late", value="private")
            finally:
                await asyncio.sleep(0)
                closed.set()

        socket = Socket()
        with self.assertLogs("examples.ag_ui.adapter", level="ERROR") as logs:
            session = asyncio.create_task(websocket_session(socket, agent))
            try:
                await socket.inbound.put(run_input())
                self.assertEqual((await socket.outbound.get())["type"], "RUN_STARTED")
                terminal = await asyncio.wait_for(socket.outbound.get(), 2)
                self.assertEqual(
                    (terminal["type"], terminal["code"]), ("RUN_ERROR", "AGENT_ERROR")
                )
                self.assertTrue(closed.is_set())
                self.assertNotIn("private", json.dumps(terminal))
            finally:
                await socket.inbound.put(WebSocketDisconnect(1000))
                await asyncio.wait_for(session, 2)
        self.assertNotIn("private", " ".join(logs.output))

    async def test_invalid_input_and_controls_close_with_policy_error(self):
        async def agent(run, inputs):
            yield RunStartedEvent(thread_id=run.thread_id, run_id=run.run_id)
            await asyncio.Event().wait()

        for invalid in (
            {},
            {"control": "cancel", "runId": "missing"},
            {"control": "unknown", "runId": "run-1"},
        ):
            with self.subTest(invalid=invalid):
                socket = Socket()
                session = asyncio.create_task(websocket_session(socket, agent))
                if invalid.get("runId") == "run-1":
                    await socket.inbound.put(run_input())
                    await socket.outbound.get()
                await socket.inbound.put(invalid)
                await asyncio.wait_for(session, 2)
                self.assertEqual(socket.closed, 1008)


APPLICATION = f"""
import asyncio
import sys
from pathlib import Path
sys.path.insert(0, {str(Path(__file__).resolve().parents[2])!r})
from examples.ag_ui.adapter import create_app
from examples.ag_ui.app import demo_agent

async def agent(run, inputs):
    source = demo_agent(run, inputs)
    try:
        async for event in source:
            yield event
    finally:
        await source.aclose()
        await asyncio.sleep(0.02)
        Path('agent_closed').write_text(run.run_id)

app = create_app(agent, origin='http://127.0.0.1:8000')
"""


@unittest.skipUnless(
    SDK and NATIVE, "install the native wheel and optional ag-ui extra"
)
class AGUINativeTests(unittest.TestCase):
    event_loop = "asyncio"

    def setUp(self):
        self.server = Server(
            application=APPLICATION,
            max_body_size=4096,
            extra_args=("--event-loop", self.event_loop),
        )

    def tearDown(self):
        self.server.close()

    def receive(self, client):
        opcode, payload = client.receive()
        self.assertEqual(opcode, 1)
        return json.loads(payload)

    def wait_closed(self):
        closed = self.server.root / "agent_closed"
        deadline = time.monotonic() + 2
        while not closed.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertTrue(closed.exists())

    def test_sse_sdk_payload_and_input_validation(self):
        status, headers, body = self.server.request(
            "POST",
            "/agent",
            json.dumps(run_input()),
        )
        self.assertEqual(status, 200)
        self.assertEqual(headers["content-type"], "text/event-stream; charset=utf-8")
        events = [
            json.loads(frame[6:]) for frame in body.decode().strip().split("\n\n")
        ]
        self.assertEqual(
            [event["type"] for event in events],
            [
                "RUN_STARTED",
                "TEXT_MESSAGE_START",
                "TEXT_MESSAGE_CONTENT",
                "TEXT_MESSAGE_CONTENT",
                "TEXT_MESSAGE_CONTENT",
                "TEXT_MESSAGE_END",
                "RUN_FINISHED",
            ],
        )
        self.wait_closed()
        self.assertEqual(
            self.server.request("POST", "/agent", "{}")[0],
            422,
        )

    def test_duplex_input_and_cancel_cleanup_over_native_websocket(self):
        client = Client(self.server.port, "/agent/ws")
        try:
            self.assertEqual(client.status, 101)
            client.send(1, json.dumps(run_input()))
            self.assertEqual(self.receive(client)["type"], "RUN_STARTED")
            self.assertEqual(self.receive(client)["type"], "TEXT_MESSAGE_START")
            self.assertEqual(self.receive(client)["delta"], "Hello ")
            # The agent is still emitting its opening response.
            client.send(
                1,
                json.dumps(
                    {"control": "input", "runId": "run-1", "value": "live input"}
                ),
            )
            events = []
            while not events or events[-1]["type"] != "RUN_FINISHED":
                events.append(self.receive(client))
            self.assertEqual(
                [event.get("delta") for event in events if "delta" in event],
                ["from ", "Bessemer. ", "live input"],
            )
            self.wait_closed()
            (self.server.root / "agent_closed").unlink()
            client.send(1, json.dumps(run_input("run-2")))
            self.assertEqual(self.receive(client)["runId"], "run-2")
            client.send(1, json.dumps({"control": "cancel", "runId": "run-2"}))
            while (event := self.receive(client))["type"] != "RUN_ERROR":
                pass
            self.assertEqual(event["code"], "CLIENT_CANCELLED")
            self.assertEqual((self.server.root / "agent_closed").read_text(), "run-2")
        finally:
            client.close()

    def test_native_disconnect_closes_agent(self):
        client = Client(self.server.port, "/agent/ws")
        client.send(1, json.dumps(run_input()))
        self.assertEqual(self.receive(client)["type"], "RUN_STARTED")
        client.close()
        self.wait_closed()

    def test_browser_origin_requires_exact_allowlist_match(self):
        allowed = Client(
            self.server.port,
            "/agent/ws",
            headers="Origin: http://127.0.0.1:8000\r\n",
        )
        try:
            self.assertEqual(allowed.status, 101)
        finally:
            allowed.close()
        blocked = Client(
            self.server.port,
            "/agent/ws",
            headers="Origin: http://localhost:8000\r\n",
        )
        try:
            self.assertEqual(blocked.status, 403)
        finally:
            blocked.close()


@unittest.skipUnless(sys.platform != "win32", "Rust loop is not available on Windows")
class AGUIRustNativeTests(AGUINativeTests):
    event_loop = "bessemer"
