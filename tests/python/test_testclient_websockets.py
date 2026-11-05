"""WebSocket sessions through ``TestClient.websocket_connect``."""

import socket
import struct
import time
import unittest

from bessemer import App, HTTPException, WebSocket, WebSocketDisconnect
from bessemer.testclient import TestClient, WebSocketDenied, WebSocketTestSession


def build_app():
    app = App()

    @app.websocket("/echo/{room}")
    async def echo(websocket: WebSocket):
        protocol = websocket.headers.get("sec-websocket-protocol")
        await websocket.accept(subprotocol=protocol.split(",")[0] if protocol else None)
        await websocket.send_json(
            {
                "room": websocket.path_params["room"],
                "q": websocket.query_params.get("q"),
                "token": websocket.headers.get("x-token"),
                "session": websocket.cookies.get("session"),
            }
        )
        while True:
            try:
                message = await websocket.receive()
            except WebSocketDisconnect:
                return
            if message == "close":
                await websocket.close(4000, "bye")
                return
            if isinstance(message, str):
                await websocket.send_text(message)
            else:
                await websocket.send_bytes(message)

    @app.websocket("/deny")
    async def deny(websocket: WebSocket):
        await websocket.close()

    @app.websocket("/forbidden")
    async def forbidden(websocket: WebSocket):
        raise HTTPException(403, "Forbidden")

    @app.websocket("/count")
    async def count(websocket: WebSocket):
        await websocket.accept()
        received = 0
        try:
            while True:
                await websocket.receive_bytes()
                received += 1
        except WebSocketDisconnect as error:
            app.state.closed = (received, error.code)

    return app


def raw_session(sock):
    """A session around an already-upgraded socket, for transport tests."""
    session = WebSocketTestSession.__new__(WebSocketTestSession)
    session._socket = sock
    session._buffer = bytearray()
    session._fragments = []
    session._fragment_opcode = None
    session._closed = session._close_sent = False
    return session


class TestClientWebSocketTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = build_app()
        cls.client = TestClient(
            cls.app, headers={"x-token": "t"}, cookies={"session": "s"}
        )
        cls.client.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.client.__exit__(None, None, None)

    def test_messages_round_trip(self):
        with self.client.websocket_connect(
            "/echo/lobby?q=1", subprotocols=["chat", "other"]
        ) as websocket:
            self.assertEqual(websocket.accepted_subprotocol, "chat")
            self.assertEqual(
                websocket.receive_json(),
                {"room": "lobby", "q": "1", "token": "t", "session": "s"},
            )
            websocket.send_text("hello")
            self.assertEqual(websocket.receive_text(), "hello")
            websocket.send_bytes(b"\x00\x01")
            self.assertEqual(websocket.receive_bytes(), b"\x00\x01")
            large = "x" * 200_000
            websocket.send_text(large)
            self.assertEqual(websocket.receive_text(), large)
            websocket.send_json({"a": 1}, mode="binary")
            self.assertEqual(websocket.receive_json(mode="binary"), {"a": 1})

    def test_server_close_raises_disconnect(self):
        with self.client.websocket_connect("/echo/a") as websocket:
            websocket.receive_json()
            websocket.send_text("close")
            with self.assertRaises(WebSocketDisconnect) as caught:
                websocket.receive_text()
            self.assertEqual(
                (caught.exception.code, caught.exception.reason), (4000, "bye")
            )
            with self.assertRaises(RuntimeError):
                websocket.receive()

    def test_denied_handshakes(self):
        with self.assertRaises(WebSocketDenied) as caught:
            self.client.websocket_connect("/deny")
        self.assertEqual(caught.exception.response.status_code, 403)
        self.assertIsInstance(caught.exception, WebSocketDisconnect)
        with self.assertRaises(WebSocketDenied) as caught:
            self.client.websocket_connect("/forbidden")
        self.assertEqual(caught.exception.response.status_code, 403)
        with self.assertRaises(WebSocketDenied) as caught:
            self.client.websocket_connect("/missing")
        self.assertEqual(caught.exception.response.status_code, 404)

    def test_client_close_reaches_the_application(self):
        with self.client.websocket_connect("ws://testserver/count") as websocket:
            for _ in range(3):
                websocket.send_bytes(b"x")
            websocket.close(4100)
        # The handler records the close after the session ends.
        deadline = time.monotonic() + 5
        while getattr(self.app.state, "closed", None) is None:
            self.assertLess(time.monotonic(), deadline)
            time.sleep(0.01)
        self.assertEqual(self.app.state.closed, (3, 4100))

    def test_a_reset_connection_raises_disconnect(self):
        listener = socket.create_server(("127.0.0.1", 0))
        client = socket.create_connection(listener.getsockname(), timeout=5)
        peer, _ = listener.accept()
        listener.close()
        peer.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
        peer.close()  # Sends a reset instead of a clean close.
        session = raw_session(client)
        with self.assertRaises(WebSocketDisconnect) as caught:
            session.receive()
        self.assertEqual(caught.exception.code, 1006)
        with self.assertRaises(RuntimeError):
            session.receive()

    def test_timeouts_keep_the_session_usable(self):
        listener = socket.create_server(("127.0.0.1", 0))
        client = socket.create_connection(listener.getsockname(), timeout=0.05)
        peer, _ = listener.accept()
        listener.close()
        session = raw_session(client)
        with self.assertRaises(TimeoutError):
            session.receive()
        # Half a frame, then a timeout, then the rest of the message, which
        # arrives fragmented.
        first = b"\x01\x03hel"
        last = b"\x80\x02lo"
        peer.sendall(first[:3])
        with self.assertRaises(TimeoutError):
            session.receive()
        peer.sendall(first[3:])
        with self.assertRaises(TimeoutError):
            session.receive()
        peer.sendall(last)
        self.assertEqual(session.receive(), "hello")
        peer.close()
        client.close()

    def test_requires_a_running_client(self):
        with self.assertRaises(RuntimeError):
            TestClient(build_app()).websocket_connect("/echo/a")


if __name__ == "__main__":
    unittest.main()
