"""Native WebSocket callbacks stay on their session's event loop."""

import asyncio
import threading
import unittest
from unittest.mock import patch

from bessemer.websockets import WebSocket, WebSocketDisconnect


class Channel:
    def __init__(self):
        self.deliver = None
        self.drain = None
        self.acks = 0

    def accept(self, deliver, subprotocol):
        self.deliver = deliver

    def ack(self, count):
        self.acks += count

    def send(self, opcode, data):
        return True

    def on_drain(self, callback):
        self.drain = callback


class WebSocketCallbackTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.loop = asyncio.get_running_loop()
        self.channel = Channel()
        self.websocket = WebSocket(self.channel, None, self.loop)
        await self.websocket.accept()

    async def test_delivery_on_session_loop_needs_no_threadsafe_scheduling(self):
        with patch.object(self.loop, "call_soon_threadsafe") as schedule:
            self.channel.deliver([(1, "first"), (1, "second")])
            self.assertEqual(await self.websocket.receive_text(), "first")
            self.assertEqual(await self.websocket.receive_text(), "second")
        schedule.assert_not_called()
        self.assertEqual(self.channel.acks, 2)

    async def test_delivery_from_io_thread_wakes_all_receivers_and_closes(self):
        first = asyncio.create_task(self.websocket.receive_text())
        second = asyncio.create_task(self.websocket.receive_text())
        await asyncio.sleep(0)
        thread = threading.Thread(
            target=self.channel.deliver,
            args=([(1, "first"), (1, "second")],),
        )
        thread.start()
        thread.join(timeout=1)
        self.assertFalse(thread.is_alive())
        self.assertFalse(self.websocket._queue)
        self.assertEqual(await asyncio.gather(first, second), ["first", "second"])
        self.channel.deliver([(8, 1001)])
        with self.assertRaises(WebSocketDisconnect) as caught:
            await self.websocket.receive()
        self.assertEqual(caught.exception.code, 1001)
        self.assertEqual(self.channel.acks, 2)

    async def test_drain_on_session_loop_resolves_without_extra_callback(self):
        sent = asyncio.create_task(self.websocket.send_bytes(b"data"))
        await asyncio.sleep(0)
        self.assertFalse(sent.done())
        with patch.object(self.loop, "call_soon_threadsafe") as schedule:
            self.channel.drain()
            await sent
        schedule.assert_not_called()

    async def test_drain_from_io_thread_and_after_sender_cancellation(self):
        sent = asyncio.create_task(self.websocket.send_bytes(b"data"))
        await asyncio.sleep(0)
        thread = threading.Thread(target=self.channel.drain)
        thread.start()
        thread.join(timeout=1)
        self.assertFalse(thread.is_alive())
        await sent
        sent = asyncio.create_task(self.websocket.send_bytes(b"data"))
        await asyncio.sleep(0)
        sent.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await sent
        # Native drain can race with a cancelled sender.
        self.channel.drain()
