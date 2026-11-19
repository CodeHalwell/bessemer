"""WebSockets over HTTP/2 extended CONNECT (RFC 8441).

No HTTP/2 client ships with Python, so this speaks the few frames involved
directly: HPACK literals out, and only the indexed ``:status 200`` checked in.
"""

import asyncio
import collections
import contextlib
import os
import socket
import struct
import threading
import time
import unittest

from bessemer import App, WebSocket, WebSocketDisconnect
from bessemer.testclient import TestClient

PREFACE = b"PRI * HTTP/2.0\r\n\r\nSM\r\n\r\n"
DATA, HEADERS, RST_STREAM, SETTINGS, PING = 0, 1, 3, 4, 6
END_STREAM, END_HEADERS, ACK = 0x1, 0x4, 0x1
ENABLE_CONNECT_PROTOCOL = 0x8
STATUS_200 = 0x88  # HPACK static table entry 8, ":status: 200"
CANCEL = 0x8
# The push handlers still running, by name.
PUSHING = set()
# Calls of the ordinary route.
PLAIN_CALLS = []
API_KEY_ENV = "BESSEMER_TEST_H2_WEBSOCKET_KEY"


def build_app(**settings):
    app = App(**settings)
    app.health("/healthz")

    @app.get("/plain")
    async def plain():
        PLAIN_CALLS.append("plain")
        return {"ok": True}

    @app.websocket("/echo/{room}")
    async def echo(websocket: WebSocket):
        await websocket.accept()
        await websocket.send_json(
            {
                "room": websocket.path_params["room"],
                "q": websocket.query_params.get("q"),
            }
        )
        try:
            while True:
                await websocket.send_text(await websocket.receive_text())
        except WebSocketDisconnect:
            return

    @app.websocket("/push/{name}")
    async def push(websocket: WebSocket):
        # A server-push endpoint: it never receives, so inbound credit runs out.
        await websocket.accept()
        name = websocket.path_params["name"]
        PUSHING.add(name)
        try:
            await asyncio.Event().wait()
        finally:
            PUSHING.discard(name)

    @app.websocket("/deny")
    async def deny(websocket: WebSocket):
        await websocket.close()

    @app.websocket("/deny-then-work")
    async def deny_then_work(websocket: WebSocket):
        await websocket.close()
        await asyncio.sleep(30)

    return app


def literal(name, value):
    """An HPACK literal header field without indexing, new name, no Huffman."""
    name, value = name.encode(), value.encode()
    assert len(name) < 127 and len(value) < 127
    return b"\x00" + bytes([len(name)]) + name + bytes([len(value)]) + value


def frame(kind, flags, stream, payload=b""):
    return (
        struct.pack(">I", len(payload))[1:]
        + bytes([kind, flags])
        + struct.pack(">I", stream)
        + payload
    )


def websocket_frame(opcode, payload):
    """A masked client frame (RFC 6455 framing is unchanged over HTTP/2)."""
    mask = os.urandom(4)
    assert len(payload) < 126
    masked = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
    return bytes([0x80 | opcode, 0x80 | len(payload)]) + mask + masked


class Connection:
    def __init__(self, port):
        self.socket = socket.create_connection(("127.0.0.1", port), timeout=10)
        self.buffer = b""
        # Frames that arrived for one stream while reading for another. The
        # server may answer concurrent streams in either order.
        self.pending = collections.defaultdict(collections.deque)
        self.socket.sendall(PREFACE + frame(SETTINGS, 0, 0))
        self.settings = {}
        while True:
            kind, flags, _, payload = self.read_frame()
            if kind == SETTINGS and not flags & ACK:
                for offset in range(0, len(payload), 6):
                    key, value = struct.unpack(">HI", payload[offset : offset + 6])
                    self.settings[key] = value
                self.socket.sendall(frame(SETTINGS, ACK, 0))
                break

    def read_exact(self, count):
        while len(self.buffer) < count:
            chunk = self.socket.recv(65536)
            if not chunk:
                raise ConnectionError("connection closed")
            self.buffer += chunk
        data, self.buffer = self.buffer[:count], self.buffer[count:]
        return data

    def read_frame(self):
        head = self.read_exact(9)
        length = int.from_bytes(head[:3], "big")
        kind, flags = head[3], head[4]
        stream = int.from_bytes(head[5:9], "big") & 0x7FFFFFFF
        return kind, flags, stream, self.read_exact(length)

    def stream_frame(self, stream):
        """The next frame on ``stream``, keeping other streams' frames queued."""
        if self.pending[stream]:
            return self.pending[stream].popleft()
        while True:
            kind, flags, number, payload = self.read_frame()
            if kind == PING and not flags & ACK:
                # A graceful GOAWAY waits for this acknowledgement.
                self.socket.sendall(frame(PING, ACK, 0, payload))
            if number == stream:
                return kind, flags, payload
            if number != 0:
                self.pending[number].append((kind, flags, payload))

    def open(self, stream, path, headers=()):
        block = b"".join(
            literal(name, value)
            for name, value in (
                (":method", "CONNECT"),
                (":protocol", "websocket"),
                (":scheme", "http"),
                (":path", path),
                (":authority", "localhost"),
                ("sec-websocket-version", "13"),
            )
            + tuple(headers)
        )
        self.socket.sendall(frame(HEADERS, END_HEADERS, stream, block))
        kind, flags, payload = self.stream_frame(stream)
        assert kind == HEADERS, kind
        return payload[0] == STATUS_200, bool(flags & END_STREAM)

    def refusal(self, stream, path, headers=()):
        """Open ``stream``, expecting a refusal, and return the response body."""
        accepted, ended = self.open(stream, path, headers)
        assert not accepted
        body = b""
        while not ended:
            kind, flags, payload = self.stream_frame(stream)
            if kind == DATA:
                body += payload
            ended = bool(flags & END_STREAM) or kind == RST_STREAM
        return body

    def send(self, stream, opcode, payload):
        self.socket.sendall(frame(DATA, 0, stream, websocket_frame(opcode, payload)))

    def receive(self, stream):
        """One unfragmented server WebSocket frame: (opcode, payload)."""
        data = b""
        while True:
            if len(data) >= 2:
                length = data[1] & 0x7F
                start = 2
                if length == 126:
                    length = int.from_bytes(data[2:4], "big")
                    start = 4
                if len(data) >= start + length:
                    return data[0] & 0x0F, data[start : start + length]
            kind, _, payload = self.stream_frame(stream)
            if kind == RST_STREAM:
                raise ConnectionError("stream reset")
            if kind == DATA:
                data += payload

    def close(self):
        self.socket.close()


class HTTP2WebSocketTests(unittest.TestCase):
    def test_browser_origin_requires_allowlist(self):
        with TestClient(build_app()) as client:
            connection = Connection(client._port)
            try:
                body = connection.refusal(
                    1,
                    "/echo/lobby",
                    [("origin", "https://other.test"), ("cookie", "session=secret")],
                )
                self.assertIn(b"WebSocket origin is not allowed", body)
            finally:
                connection.close()

    def test_extended_connect_opens_a_websocket(self):
        with TestClient(build_app()) as client:
            connection = Connection(client._port)
            try:
                self.assertEqual(connection.settings.get(ENABLE_CONNECT_PROTOCOL), 1)
                self.assertEqual(connection.open(1, "/echo/lobby?q=1"), (True, False))
                opcode, payload = connection.receive(1)
                self.assertEqual(opcode, 1)
                self.assertIn(b'"room":"lobby"', payload)
                self.assertIn(b'"q":"1"', payload)
                # Two sessions share one connection, each on its own stream.
                self.assertEqual(connection.open(3, "/echo/other"), (True, False))
                connection.receive(3)
                connection.send(1, 1, b"hello")
                connection.send(3, 1, b"there")
                self.assertEqual(connection.receive(1), (1, b"hello"))
                self.assertEqual(connection.receive(3), (1, b"there"))
                connection.send(1, 8, struct.pack(">H", 1000))
                self.assertEqual(connection.receive(1)[0], 8)
                # The other stream is unaffected by the first closing.
                connection.send(3, 1, b"still here")
                self.assertEqual(connection.receive(3), (1, b"still here"))
            finally:
                connection.close()

    def test_extended_connect_to_an_ordinary_route_is_refused(self):
        PLAIN_CALLS.clear()
        with TestClient(build_app()) as client:
            connection = Connection(client._port)
            try:
                # Refused before the handler runs, with a status that is not a
                # successful handshake; a 2xx would read as an open tunnel.
                for stream, path in ((1, "/plain"), (3, "/healthz")):
                    body = connection.refusal(stream, path)
                    self.assertIn(b"Not a WebSocket route", body)
                self.assertEqual(PLAIN_CALLS, [])
                # The connection and the route are otherwise unaffected.
                self.assertEqual(connection.open(5, "/echo/after"), (True, False))
                self.assertEqual(client.get("/plain").status_code, 200)
                self.assertEqual(PLAIN_CALLS, ["plain"])
            finally:
                connection.close()

    def test_authentication_answers_extended_connect_first(self):
        PLAIN_CALLS.clear()
        os.environ[API_KEY_ENV] = "h2-websocket-key"
        try:
            app = build_app(api_key={"env": API_KEY_ENV})
            with TestClient(app) as client:
                connection = Connection(client._port)
                try:
                    body = connection.refusal(1, "/plain")
                    self.assertIn(b"Unauthorized", body)
                    key = [("x-api-key", "h2-websocket-key")]
                    body = connection.refusal(3, "/plain", key)
                    self.assertIn(b"Not a WebSocket route", body)
                    self.assertEqual(PLAIN_CALLS, [])
                finally:
                    connection.close()
        finally:
            del os.environ[API_KEY_ENV]

    def test_sessions_count_against_max_connections_across_connections(self):
        """However many sessions one HTTP/2 connection could carry, the worker
        admits no more than ``max_connections`` in all."""
        options = {"max_connections": 3}
        with TestClient(build_app(), server_options=options) as client:
            first = Connection(client._port)
            second = Connection(client._port)
            try:
                for stream in (1, 3):
                    self.assertEqual(
                        first.open(stream, f"/echo/{stream}"), (True, False)
                    )
                    first.receive(stream)
                self.assertEqual(second.open(1, "/echo/second"), (True, False))
                second.receive(1)
                for connection, stream in ((second, 3), (first, 5)):
                    body = connection.refusal(stream, "/echo/over")
                    self.assertIn(b"Too many WebSocket sessions", body)
                    self.assertNotIn(b"on this connection", body)
                # A session that ends returns its admission.
                first.send(1, 8, struct.pack(">H", 1000))
                self.assertEqual(first.receive(1)[0], 8)
                streams = iter(range(5, 1_000, 2))
                deadline = time.monotonic() + 20
                while True:
                    if second.open(next(streams), "/echo/again")[0]:
                        break
                    self.assertLess(time.monotonic(), deadline)
                    time.sleep(0.25)
            finally:
                first.close()
                second.close()

    def test_a_denied_handshake_ends_only_its_stream(self):
        with TestClient(build_app()) as client:
            connection = Connection(client._port)
            try:
                self.assertIn(b"denied", connection.refusal(1, "/deny"))
                self.assertEqual(connection.open(3, "/echo/after"), (True, False))
            finally:
                connection.close()

    def test_refused_handlers_still_count_until_they_finish(self):
        """A denied stream ends at once, but its handler keeps running until it
        is cancelled; the connection admits no more handlers than streams."""
        with TestClient(build_app()) as client:
            connection = Connection(client._port)
            try:
                streams = iter(range(1, 1_000, 2))
                for _ in range(100):
                    body = connection.refusal(next(streams), "/deny-then-work")
                    self.assertIn(b"denied", body)
                body = connection.refusal(next(streams), "/echo/full")
                self.assertIn(b"Too many WebSocket sessions", body)
                # Refused handlers are cancelled after the close wait; their
                # admission returns with them.
                deadline = time.monotonic() + 20
                while True:
                    # Frames left on a refused stream are skipped by later reads.
                    if connection.open(next(streams), "/echo/x")[0]:
                        break
                    self.assertLess(time.monotonic(), deadline)
                    time.sleep(0.25)
            finally:
                connection.close()

    def test_an_open_session_is_not_closed_as_idle(self):
        options = {"header_timeout": 0.3}
        with TestClient(build_app(), server_options=options) as client:
            connection = Connection(client._port)
            try:
                self.assertEqual(connection.open(1, "/echo/idle"), (True, False))
                connection.receive(1)
                # Several idle checks pass with nothing but the session open.
                time.sleep(1.2)
                connection.send(1, 1, b"awake")
                self.assertEqual(connection.receive(1), (1, b"awake"))
            finally:
                connection.close()

    def test_peers_that_leave_after_exhausting_credit_cancel_their_handlers(self):
        def wait_until(condition):
            deadline = time.monotonic() + 30
            while not condition():
                self.assertLess(time.monotonic(), deadline)
                time.sleep(0.1)

        with TestClient(build_app()) as client:
            connection = Connection(client._port)
            names = {stream: f"h2-{stream}" for stream in (1, 3, 5)}
            try:
                for stream, name in names.items():
                    self.assertEqual(
                        connection.open(stream, f"/push/{name}"), (True, False)
                    )
                    for index in range(70):
                        connection.send(stream, 1, str(index).encode())
                wait_until(lambda: set(names.values()) <= PUSHING)
                # A reset stream ends its session; the others carry on.
                connection.socket.sendall(
                    frame(RST_STREAM, 0, 1, struct.pack(">I", CANCEL))
                )
                wait_until(lambda: names[1] not in PUSHING)
                self.assertLessEqual({names[3], names[5]}, PUSHING)
            finally:
                connection.close()
            # Closing the connection ends the rest.
            wait_until(lambda: not set(names.values()) & PUSHING)

    def test_shutdown_closes_open_sessions_promptly(self):
        options = {"graceful_timeout": 10}
        client = TestClient(build_app(), server_options=options)
        client.start()
        connection = Connection(client._port)
        try:
            self.assertEqual(connection.open(1, "/echo/stop"), (True, False))
            connection.receive(1)
            started = time.monotonic()
            stopping = threading.Thread(target=client.close)
            stopping.start()
            # The session is closed with "going away", not left to the deadline.
            opcode, payload = connection.receive(1)
            self.assertEqual((opcode, payload[:2]), (8, struct.pack(">H", 1001)))
            connection.send(1, 8, payload[:2])
            # Keep reading so the server's GOAWAY ping is acknowledged, as any
            # HTTP/2 client does, until the server closes the connection.
            with contextlib.suppress(ConnectionError, OSError):
                while True:
                    connection.stream_frame(0)
            stopping.join(15)
            self.assertLess(time.monotonic() - started, 5)
        finally:
            connection.close()


if __name__ == "__main__":
    unittest.main()
