"""WebSocket sessions through the installed native runtime, with a raw client."""

import base64
import hashlib
import importlib.util
import json
import os
import socket
import struct
import time
import unittest

from bessemer import App, Depends, WebSocket
from test_native_integration import Server

APPLICATION = """
import asyncio
import gc
import weakref
from pathlib import Path
from bessemer import App, Depends, HTTPException, WebSocket, WebSocketDisconnect

app = App()
app.health("/healthz")
live = weakref.WeakSet()

@app.get("/live")
def live_sessions():
    gc.collect()
    return {"live": len(live)}

async def resource():
    try:
        yield "dep"
    finally:
        Path("dependency_closed").touch()

@app.websocket("/echo/{room}")
async def echo(websocket: WebSocket, value=Depends(resource)):
    live.add(websocket)
    await websocket.accept(subprotocol=websocket.headers.get("sec-websocket-protocol"))
    await websocket.send_json({"room": websocket.path_params["room"], "dep": value,
                               "q": websocket.query_params.get("q")})
    try:
        while True:
            message = await websocket.receive()
            if message == "json":
                await websocket.send_json(await websocket.receive_json())
            elif message == "close":
                await websocket.close(4000, "bye")
                return
            elif isinstance(message, str):
                await websocket.send_text(message)
            else:
                await websocket.send_bytes(message)
    except WebSocketDisconnect as error:
        Path("disconnected").write_text(str(error.code))

@app.websocket("/deny")
async def deny(websocket: WebSocket):
    await websocket.close()

@app.websocket("/crash")
async def crash(websocket: WebSocket):
    await websocket.accept()
    raise RuntimeError("boom")

@app.websocket("/burst")
async def burst(websocket: WebSocket):
    await websocket.accept()
    count = int(await websocket.receive_text())
    for index in range(count):
        await websocket.send_bytes(index.to_bytes(4, "big") * 256)
    await websocket.send_text("done")
    await websocket.receive_text()

@app.websocket("/close-then-wait")
async def close_then_wait(websocket: WebSocket):
    await websocket.accept()
    await websocket.close(4001)
    try:
        await websocket.receive()
    except WebSocketDisconnect as error:
        Path("close_wait.tmp").write_text(str(error.code))
        Path("close_wait.tmp").rename("close_wait")

@app.websocket("/deny-then-work")
async def deny_then_work(websocket: WebSocket):
    await websocket.close()
    try:
        await asyncio.sleep(30)
    finally:
        Path("denied_cancelled").touch()

@app.websocket("/busy")
async def busy(websocket: WebSocket):
    await websocket.accept()
    try:
        await asyncio.sleep(30)
    finally:
        Path("busy_cancelled").touch()

@app.websocket("/concurrent-burst")
async def concurrent_burst(websocket: WebSocket):
    await websocket.accept()
    async def sender(tag):
        for _ in range(2048):
            await websocket.send_bytes(tag * 1024)
    await asyncio.gather(sender(b"a"), sender(b"b"))
    await websocket.send_text("done")
    await websocket.receive_text()

async def transaction():
    try:
        yield "tx"
    except HTTPException:
        Path("rolled_back").touch()
        raise
    else:
        Path("committed").touch()

@app.websocket("/deny-in-transaction")
async def deny_in_transaction(websocket: WebSocket, tx=Depends(transaction)):
    raise HTTPException(403, "Forbidden")

async def session_transaction():
    try:
        yield "tx"
    except WebSocketDisconnect:
        Path("session_rolled_back").touch()
        raise
    else:
        Path("session_committed").touch()

@app.websocket("/disconnect-in-transaction")
async def disconnect_in_transaction(
    websocket: WebSocket, tx=Depends(session_transaction)
):
    await websocket.accept()
    await websocket.send_text("ready")
    await websocket.receive_text()

async def failing_commit():
    yield "tx"
    raise RuntimeError("commit failed")

@app.websocket("/cleanup-fails")
async def cleanup_fails(websocket: WebSocket, tx=Depends(failing_commit)):
    await websocket.accept()
    await websocket.send_text("hi")

@app.websocket("/never-accepts")
async def never_accepts(websocket: WebSocket):
    try:
        await asyncio.sleep(30)
    finally:
        Path("handshake_cancelled").touch()
"""


class Client:
    def __init__(self, port, path, protocol=None):
        self.socket = socket.create_connection(("127.0.0.1", port), timeout=5)
        key = base64.b64encode(os.urandom(16)).decode()
        extra = f"Sec-WebSocket-Protocol: {protocol}\r\n" if protocol else ""
        self.socket.sendall(
            (
                f"GET {path} HTTP/1.1\r\nHost: test\r\nUpgrade: websocket\r\n"
                f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\n"
                f"Sec-WebSocket-Version: 13\r\n{extra}\r\n"
            ).encode()
        )
        self.buffer = b""
        while b"\r\n\r\n" not in self.buffer:
            data = self.socket.recv(4096)
            if not data:
                break
            self.buffer += data
        head, _, self.buffer = self.buffer.partition(b"\r\n\r\n")
        lines = head.decode().split("\r\n")
        self.status = int(lines[0].split()[1])
        self.headers = {
            name.lower(): value
            for name, value in (line.split(": ", 1) for line in lines[1:])
        }
        expected = base64.b64encode(
            hashlib.sha1(
                key.encode() + b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
            ).digest()
        ).decode()
        if self.status == 101:
            assert self.headers["sec-websocket-accept"] == expected

    def send(self, opcode, payload, fin=True, mask=True):
        if isinstance(payload, str):
            payload = payload.encode()
        header = bytes([(0x80 if fin else 0) | opcode])
        length = len(payload)
        bit = 0x80 if mask else 0
        if length < 126:
            header += bytes([bit | length])
        elif length < 65536:
            header += bytes([bit | 126]) + struct.pack(">H", length)
        else:
            header += bytes([bit | 127]) + struct.pack(">Q", length)
        if mask:
            key = os.urandom(4)
            payload = bytes(byte ^ key[index % 4] for index, byte in enumerate(payload))
            header += key
        self.socket.sendall(header + payload)

    def read_exact(self, count):
        while len(self.buffer) < count:
            data = self.socket.recv(65536)
            if not data:
                raise ConnectionError("closed")
            self.buffer += data
        data, self.buffer = self.buffer[:count], self.buffer[count:]
        return data

    def receive(self):
        first, second = self.read_exact(2)
        length = second & 0x7F
        if length == 126:
            length = struct.unpack(">H", self.read_exact(2))[0]
        elif length == 127:
            length = struct.unpack(">Q", self.read_exact(8))[0]
        assert second & 0x80 == 0, "server frames are never masked"
        return first & 0x0F, self.read_exact(length)

    def close(self):
        self.socket.close()


class WebSocketBuildTests(unittest.TestCase):
    def test_websocket_routes_validate_handlers(self):
        app = App()
        with self.assertRaises(TypeError):

            @app.websocket("/sync")
            def sync(websocket: WebSocket):
                pass

        @app.websocket("/bad")
        async def bad(websocket: WebSocket, limit: int = 3):
            pass

        with self.assertRaisesRegex(ValueError, "WebSocket object"):
            app.build()
        app = App()

        @app.get("/http")
        async def http(websocket: WebSocket):
            return {}

        with self.assertRaisesRegex(ValueError, "only injected"):
            app.build()
        app = App()

        @app.websocket("/ws/{room}")
        async def ok(websocket: WebSocket):
            pass

        app.build()
        self.assertEqual(app._compiled[0].declaration["kind"], "websocket")
        self.assertNotIn("/ws/{room}", app.openapi()["paths"])
        app = App()

        def session_resource(websocket: WebSocket):
            return websocket

        with self.assertRaisesRegex(ValueError, "request-scoped values"):

            @app.websocket("/scoped")
            async def scoped(
                websocket: WebSocket,
                value=Depends(session_resource, scope="app"),  # noqa: B008
            ):
                pass

            app.build()


@unittest.skipUnless(
    importlib.util.find_spec("bessemer._native"), "build the native extension first"
)
class WebSocketHTTPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = Server(application=APPLICATION, max_body_size=1 << 20)

    @classmethod
    def tearDownClass(cls):
        cls.server.close()

    def wait_for(self, name, timeout=5.0):
        deadline = time.monotonic() + timeout
        path = self.server.root / name
        while time.monotonic() < deadline:
            if path.exists():
                return path
            time.sleep(0.01)
        return None

    def test_echo_session_with_fragments_ping_and_close(self):
        client = Client(self.server.port, "/echo/lobby?q=1", protocol="chat")
        try:
            self.assertEqual(client.status, 101)
            self.assertEqual(client.headers["sec-websocket-protocol"], "chat")
            opcode, payload = client.receive()
            self.assertEqual(
                (opcode, json.loads(payload)),
                (1, {"room": "lobby", "dep": "dep", "q": "1"}),
            )
            client.send(1, "héllo")
            self.assertEqual(client.receive(), (1, "héllo".encode()))
            client.send(2, b"\x00\x01" * 40000)
            self.assertEqual(client.receive(), (2, b"\x00\x01" * 40000))
            # A fragmented text message with a ping between its fragments.
            client.send(1, "frag", fin=False)
            client.send(9, b"ping!")
            client.send(0, "mented")
            self.assertEqual(client.receive(), (10, b"ping!"))
            self.assertEqual(client.receive(), (1, b"fragmented"))
            client.send(1, "json")
            client.send(1, '{"a": [1, 2.5, null]}')
            opcode, payload = client.receive()
            self.assertEqual(json.loads(payload), {"a": [1, 2.5, None]})
            client.send(1, "close")
            opcode, payload = client.receive()
            self.assertEqual((opcode, payload), (8, struct.pack(">H", 4000) + b"bye"))
            client.send(8, struct.pack(">H", 4000))
        finally:
            client.close()
        self.assertIsNotNone(self.wait_for("dependency_closed"))

    def test_client_close_and_protocol_errors_reach_the_handler(self):
        client = Client(self.server.port, "/echo/a")
        try:
            client.receive()
            client.send(8, struct.pack(">H", 1001))
            self.assertEqual(client.receive(), (8, struct.pack(">H", 1001)))
        finally:
            client.close()
        # write_text creates the file before writing; wait for the content.
        deadline = time.monotonic() + 5
        path = self.wait_for("disconnected")
        while path.read_text() == "" and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(path.read_text(), "1001")
        for frames, code in [
            ([(1, b"x", False)], 1002),  # Unmasked client frame.
            ([(1, b"\xff", True)], 1007),  # Invalid UTF-8.
            ([(0, b"x", True)], 1002),  # Continuation without a message.
        ]:
            client = Client(self.server.port, "/echo/b")
            try:
                client.receive()
                for opcode, payload, mask in frames:
                    client.send(opcode, payload, mask=mask)
                self.assertEqual(client.receive(), (8, struct.pack(">H", code)))
            finally:
                client.close()

    def test_denied_crashed_and_invalid_handshakes(self):
        client = Client(self.server.port, "/deny")
        self.assertEqual(client.status, 403)
        client.close()
        client = Client(self.server.port, "/crash")
        try:
            self.assertEqual(client.status, 101)
            self.assertEqual(client.receive(), (8, struct.pack(">H", 1011)))
        finally:
            client.close()
        with socket.create_connection(
            ("127.0.0.1", self.server.port), timeout=5
        ) as raw:
            raw.sendall(b"GET /deny HTTP/1.1\r\nHost: test\r\n\r\n")
            self.assertIn(b" 400 ", raw.recv(4096))

    def test_application_close_is_bounded_when_the_peer_never_answers(self):
        client = Client(self.server.port, "/close-then-wait")
        try:
            self.assertEqual(client.receive(), (8, struct.pack(">H", 4001)))
            # Never answer the close: the server gives up after its close wait.
            path = self.wait_for("close_wait", timeout=10)
            self.assertIsNotNone(path)
            self.assertEqual(path.read_text(), "1006")
        finally:
            client.close()

    def test_malformed_cookies_reject_the_handshake(self):
        connection = socket.create_connection(
            ("127.0.0.1", self.server.port), timeout=5
        )
        with connection:
            key = base64.b64encode(os.urandom(16)).decode()
            connection.sendall(
                (
                    "GET /echo/lobby HTTP/1.1\r\nHost: t\r\nUpgrade: websocket\r\n"
                    f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\n"
                    "Sec-WebSocket-Version: 13\r\n"
                    "Cookie: session=valid; theme=a; theme=b\r\n\r\n"
                ).encode()
            )
            response = connection.recv(4096)
        self.assertTrue(response.startswith(b"HTTP/1.1 400"), response[:40])

    def test_http_exception_unwinds_dependencies_through_their_error_path(self):
        client = Client(self.server.port, "/deny-in-transaction")
        try:
            self.assertEqual(client.status, 403)
        finally:
            client.close()
        self.assertIsNotNone(self.wait_for("rolled_back"))
        self.assertFalse((self.server.root / "committed").exists())

    def test_disconnect_unwinds_dependencies_through_their_error_path(self):
        client = Client(self.server.port, "/disconnect-in-transaction")
        try:
            self.assertEqual(client.status, 101)
            self.assertEqual(client.receive(), (1, b"ready"))
        finally:
            client.close()  # The peer leaves while the handler awaits a message.
        self.assertIsNotNone(self.wait_for("session_rolled_back"))
        self.assertFalse((self.server.root / "session_committed").exists())

    def test_failed_dependency_cleanup_closes_with_an_error(self):
        client = Client(self.server.port, "/cleanup-fails")
        try:
            self.assertEqual(client.status, 101)
            self.assertEqual(client.receive(), (1, b"hi"))
            opcode, payload = client.receive()
            self.assertEqual((opcode, payload[:2]), (8, struct.pack(">H", 1011)))
        finally:
            client.close()

    def test_handshake_timeout_cancels_the_handler(self):
        client = Client(self.server.port, "/never-accepts")
        try:
            self.assertEqual(client.status, 504)
            self.assertEqual(client.headers.get("connection"), "close")
        finally:
            client.close()
        self.assertIsNotNone(self.wait_for("handshake_cancelled"))

    def test_finished_sessions_release_their_websocket(self):
        for _ in range(3):
            client = Client(self.server.port, "/echo/gc")
            try:
                client.receive()
                client.send(1, "close")
                client.receive()
                client.send(8, struct.pack(">H", 4000))
            finally:
                client.close()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            connection = socket.create_connection(
                ("127.0.0.1", self.server.port), timeout=5
            )
            with connection:
                connection.sendall(
                    b"GET /live HTTP/1.1\r\nHost: t\r\nConnection: close\r\n\r\n"
                )
                response = b""
                while chunk := connection.recv(4096):
                    response += chunk
            if json.loads(response.partition(b"\r\n\r\n")[2]) == {"live": 0}:
                break
            time.sleep(0.05)
        self.assertEqual(json.loads(response.partition(b"\r\n\r\n")[2]), {"live": 0})

    def test_denied_handshake_cancels_remaining_handler_work(self):
        client = Client(self.server.port, "/deny-then-work")
        self.assertEqual(client.status, 403)
        # The handler is still cleaning up on this connection's slot, so the
        # connection is closed rather than reused for another handshake.
        self.assertEqual(client.headers.get("connection"), "close")
        client.close()
        self.assertIsNotNone(self.wait_for("denied_cancelled", timeout=10))

    def test_disconnect_cancels_a_busy_handler(self):
        client = Client(self.server.port, "/busy")
        self.assertEqual(client.status, 101)
        client.close()  # No close frame: the peer just vanishes.
        # The handler gets the close wait to notice, then is cancelled.
        self.assertIsNotNone(self.wait_for("busy_cancelled", timeout=10))

    def test_concurrent_senders_all_resume_after_backpressure(self):
        client = Client(self.server.port, "/concurrent-burst")
        try:
            counts = {b"a": 0, b"b": 0}
            for _ in range(4096):
                _, payload = client.receive()
                counts[payload[:1]] += 1
            self.assertEqual(counts, {b"a": 2048, b"b": 2048})
            self.assertEqual(client.receive(), (1, b"done"))
            client.send(1, "bye")
        finally:
            client.close()

    def test_large_outbound_bursts_apply_backpressure_and_complete(self):
        client = Client(self.server.port, "/burst")
        try:
            client.send(1, "4096")  # 4096 messages of 1 KiB, 4 MiB in total.
            for index in range(4096):
                _, payload = client.receive()
                self.assertEqual(payload[:4], index.to_bytes(4, "big"))
            self.assertEqual(client.receive(), (1, b"done"))
            client.send(1, "bye")
        finally:
            client.close()


ABANDONED_APPLICATION = """
import asyncio
from pathlib import Path
from bessemer import App, WebSocket

app = App()
app.health("/healthz")

@app.get("/plain")
async def plain():
    return {"cleaned": Path("abandon_cleaned").exists()}

@app.websocket("/abandon")
async def abandon(websocket: WebSocket):
    Path("abandon_started").touch()
    try:
        await asyncio.sleep(30)
    finally:
        await asyncio.sleep(1)
        Path("abandon_cleaned").touch()
"""


@unittest.skipUnless(
    importlib.util.find_spec("bessemer._native"), "build the native extension first"
)
class WebSocketAbandonedHandshakeTests(unittest.TestCase):
    def test_abandoned_handshake_holds_its_slot_through_cleanup(self):
        server = Server(
            application=ABANDONED_APPLICATION,
            extra_args=("--max-connections", "1", "--max-in-flight", "8"),
        )
        try:
            client = socket.create_connection(("127.0.0.1", server.port), timeout=5)
            key = base64.b64encode(os.urandom(16)).decode()
            client.sendall(
                (
                    "GET /abandon HTTP/1.1\r\nHost: t\r\nUpgrade: websocket\r\n"
                    f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\n"
                    "Sec-WebSocket-Version: 13\r\n\r\n"
                ).encode()
            )
            deadline = time.monotonic() + 5
            while not (server.root / "abandon_started").exists():
                self.assertLess(time.monotonic(), deadline)
                time.sleep(0.01)
            client.close()  # Abandon the handshake before the handler accepts.
            time.sleep(0.2)

            def plain():
                try:
                    with socket.create_connection(
                        ("127.0.0.1", server.port), timeout=5
                    ) as connection:
                        connection.sendall(
                            b"GET /plain HTTP/1.1\r\nHost: t\r\n"
                            b"Connection: close\r\n\r\n"
                        )
                        response = b""
                        while chunk := connection.recv(4096):
                            response += chunk
                except OSError:
                    return None
                body = response.partition(b"\r\n\r\n")[2]
                return json.loads(body) if body else None

            # The only connection slot stays held while the cancelled
            # handler's async cleanup runs, and is released once it finishes.
            self.assertIsNone(plain())
            deadline = time.monotonic() + 5
            while (result := plain()) is None and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertEqual(result, {"cleaned": True})
        finally:
            server.close()


@unittest.skipUnless(
    importlib.util.find_spec("bessemer._native"), "build the native extension first"
)
class WebSocketShutdownTests(unittest.TestCase):
    def test_shutdown_closes_open_sessions_with_going_away(self):
        server = Server(application=APPLICATION, max_body_size=1 << 20)
        try:
            client = Client(server.port, "/echo/shutdown")
            try:
                client.receive()
                server.process.terminate()
                # The drain waits for the session, so the 1001 close is sent.
                self.assertEqual(client.receive(), (8, struct.pack(">H", 1001)))
                client.send(8, struct.pack(">H", 1001))
            finally:
                client.close()
            server.process.wait(timeout=10)
        finally:
            server.close()


if __name__ == "__main__":
    unittest.main()
