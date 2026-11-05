"""WebSocket per-message compression (RFC 7692) through the native server."""

import argparse
import base64
import os
import socket
import struct
import unittest
import zlib

from bessemer import App, WebSocket, WebSocketDisconnect
from bessemer.config import settings
from bessemer.testclient import TestClient

TAIL = b"\x00\x00\xff\xff"


def build_app():
    app = App()

    @app.websocket("/echo")
    async def echo(websocket: WebSocket):
        await websocket.accept()
        try:
            while True:
                message = await websocket.receive()
                if isinstance(message, str):
                    await websocket.send_text(message)
                else:
                    await websocket.send_bytes(message)
        except WebSocketDisconnect:
            return

    return app


def deflate(data):
    compressor = zlib.compressobj(wbits=-15)
    output = compressor.compress(data) + compressor.flush(zlib.Z_SYNC_FLUSH)
    assert output.endswith(TAIL)
    return output[: -len(TAIL)]


def inflate(data):
    return zlib.decompressobj(wbits=-15).decompress(data + TAIL)


class Client:
    def __init__(self, port, extensions=None):
        self.socket = socket.create_connection(("127.0.0.1", port), timeout=5)
        key = base64.b64encode(os.urandom(16)).decode()
        offer = f"Sec-WebSocket-Extensions: {extensions}\r\n" if extensions else ""
        self.socket.sendall(
            (
                "GET /echo HTTP/1.1\r\nHost: test\r\nUpgrade: websocket\r\n"
                f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\n"
                f"Sec-WebSocket-Version: 13\r\n{offer}\r\n"
            ).encode()
        )
        self.buffer = b""
        while b"\r\n\r\n" not in self.buffer:
            self.buffer += self.socket.recv(4096)
        head, _, self.buffer = self.buffer.partition(b"\r\n\r\n")
        lines = head.decode().split("\r\n")
        self.status = int(lines[0].split()[1])
        self.headers = {
            name.lower(): value
            for name, value in (line.split(": ", 1) for line in lines[1:])
        }

    def send(self, opcode, payload, rsv1=False):
        key = os.urandom(4)
        first = 0x80 | (0x40 if rsv1 else 0) | opcode
        length = len(payload)
        if length < 126:
            header = bytes([first, 0x80 | length])
        else:
            header = bytes([first, 0x80 | 126]) + struct.pack(">H", length)
        masked = bytes(byte ^ key[index % 4] for index, byte in enumerate(payload))
        self.socket.sendall(header + key + masked)

    def read_exact(self, count):
        while len(self.buffer) < count:
            data = self.socket.recv(65536)
            if not data:
                raise ConnectionError("closed")
            self.buffer += data
        data, self.buffer = self.buffer[:count], self.buffer[count:]
        return data

    def receive(self):
        """One server frame: (rsv1, opcode, payload)."""
        first, second = self.read_exact(2)
        length = second & 0x7F
        if length == 126:
            length = struct.unpack(">H", self.read_exact(2))[0]
        elif length == 127:
            length = struct.unpack(">Q", self.read_exact(8))[0]
        return bool(first & 0x40), first & 0x0F, self.read_exact(length)

    def close(self):
        self.socket.close()


class WebSocketCompressionTests(unittest.TestCase):
    def test_compressed_messages_round_trip(self):
        options = {"websocket_compression": "deflate"}
        with TestClient(build_app(), server_options=options) as server:
            client = Client(server._port, "permessage-deflate; client_max_window_bits")
            try:
                self.assertEqual(client.status, 101)
                self.assertEqual(
                    client.headers["sec-websocket-extensions"],
                    "permessage-deflate; server_no_context_takeover; "
                    "client_no_context_takeover; client_max_window_bits=15",
                )
                text = "a compressible message " * 50
                for _ in range(3):
                    client.send(1, deflate(text.encode()), rsv1=True)
                    rsv1, opcode, payload = client.receive()
                    self.assertEqual((rsv1, opcode), (True, 1))
                    self.assertLess(len(payload), len(text) // 4)
                    self.assertEqual(inflate(payload).decode(), text)
                # Uncompressed messages are still accepted, and short replies
                # are sent uncompressed.
                client.send(1, b"hi")
                self.assertEqual(client.receive(), (False, 1, b"hi"))
                client.send(2, deflate(bytes(range(256)) * 4), rsv1=True)
                rsv1, opcode, payload = client.receive()
                self.assertEqual(opcode, 2)
                received = inflate(payload) if rsv1 else payload
                self.assertEqual(received, bytes(range(256)) * 4)
            finally:
                client.close()

    def test_invalid_compressed_data_closes_with_1007(self):
        options = {"websocket_compression": "deflate"}
        with TestClient(build_app(), server_options=options) as server:
            client = Client(server._port, "permessage-deflate")
            try:
                client.send(1, b"\xff\xff\xff\xff", rsv1=True)
                _, opcode, payload = client.receive()
                self.assertEqual((opcode, payload[:2]), (8, struct.pack(">H", 1007)))
            finally:
                client.close()

    def test_compression_is_off_unless_enabled_or_offered(self):
        with TestClient(build_app()) as server:
            client = Client(server._port, "permessage-deflate")
            try:
                self.assertEqual(client.status, 101)
                self.assertNotIn("sec-websocket-extensions", client.headers)
                # RSV1 was never agreed, so it is a protocol error.
                client.send(1, deflate(b"x" * 200), rsv1=True)
                _, opcode, payload = client.receive()
                self.assertEqual((opcode, payload[:2]), (8, struct.pack(">H", 1002)))
            finally:
                client.close()
        options = {"websocket_compression": "deflate"}
        with TestClient(build_app(), server_options=options) as server:
            client = Client(server._port)
            try:
                self.assertNotIn("sec-websocket-extensions", client.headers)
                client.send(1, b"y" * 500)
                self.assertEqual(client.receive(), (False, 1, b"y" * 500))
            finally:
                client.close()

    def test_setting_validation(self):
        with self.assertRaisesRegex(ValueError, "websocket_compression"):
            build_app().run(websocket_compression="gzip")
        with self.assertRaisesRegex(ValueError, "websocket_compression"):
            settings(argparse.Namespace(websocket_compression="gzip"))
        resolved = settings(argparse.Namespace(websocket_compression="deflate"))
        self.assertEqual(resolved["websocket_compression"], "deflate")


if __name__ == "__main__":
    unittest.main()
