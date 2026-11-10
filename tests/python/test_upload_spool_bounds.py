"""Installed uploads bound paused files and keep retained readers charged."""

import gc
import http.client
import importlib.util
import io
import json
import socket
import time
import unittest

from test_native_integration import Server

NATIVE = importlib.util.find_spec("bessemer._native") is not None
H2 = importlib.util.find_spec("h2") is not None
APPLICATION = """
from bessemer import App, UploadFile
app = App()
app.health('/healthz')
retained = []
@app.get('/pid')
async def pid():
    return {'ok': True}
@app.post('/upload')
async def upload(upload: UploadFile):
    size = len(await upload.read())
    await upload.close()
    return {'size': size}
@app.post('/retain')
async def retain(upload: UploadFile):
    retained.append(upload)
    return {'size': upload.size}
@app.post('/close')
async def close():
    await retained.pop().close()
    return {'closed': True}
"""
BODY = (
    b'--b\r\nContent-Disposition: form-data; name="upload"; filename="x.bin"\r\n'
    b"Content-Type: application/octet-stream\r\n\r\n" + b"x" * 1024 + b"\r\n--b--\r\n"
)
TAIL = BODY[-9:]


class _H2Peer:
    def __init__(self, peer):
        from h2.config import H2Configuration
        from h2.connection import H2Connection

        self.peer = peer
        self.connection = H2Connection(
            config=H2Configuration(client_side=True, header_encoding="utf-8")
        )
        self.connection.initiate_connection()
        peer.sendall(self.connection.data_to_send())

    def send(self, stream, data, *, start=False, end=False):
        if start:
            self.connection.send_headers(
                stream,
                [
                    (":method", "POST"),
                    (":scheme", "http"),
                    (":authority", "t"),
                    (":path", "/upload"),
                    ("content-type", "multipart/form-data; boundary=b"),
                    ("content-length", str(len(BODY))),
                ],
            )
        self.connection.send_data(stream, data, end_stream=end)
        self.peer.sendall(self.connection.data_to_send())

    def answer(self, stream):
        from h2.events import DataReceived, ResponseReceived, StreamEnded

        status, body = None, bytearray()
        while True:
            chunk = self.peer.recv(65536)
            if not chunk:
                raise AssertionError("HTTP/2 connection ended before its response")
            for event in self.connection.receive_data(chunk):
                if getattr(event, "stream_id", 0) != stream:
                    continue
                if isinstance(event, ResponseReceived):
                    status = dict(event.headers)[":status"]
                elif isinstance(event, DataReceived):
                    body.extend(event.data)
                elif isinstance(event, StreamEnded):
                    return int(status), json.loads(body)
            self.peer.sendall(self.connection.data_to_send())


class ReaderLeaseTests(unittest.TestCase):
    def test_private_reader_releases_owner_after_closing_its_handle(self):
        from bessemer.request import _SpooledPart

        source = io.BytesIO(b"data")
        closed = []

        class Lease:
            def __del__(self):
                closed.append(source.closed)

        source._bessemer_spool_lease = Lease()
        reader = io.BufferedReader(_SpooledPart(source, 0, 4))
        reader.close()
        gc.collect()
        self.assertEqual(closed, [True])
        self.assertFalse(hasattr(source, "_bessemer_spool_lease"))
        reader.close()
        self.assertEqual(closed, [True])


@unittest.skipUnless(NATIVE, "build the native extension first")
class UploadSpoolBoundTests(unittest.TestCase):
    def setUp(self):
        self.server = Server(
            application=APPLICATION,
            max_body_size=128,
            extra_args=(
                "--max-in-flight",
                "2",
                "--max-upload-size",
                "1048576",
                "--request-timeout",
                "10",
            ),
        )
        self.addCleanup(self.server.close)

    def connect(self):
        peer = socket.create_connection(("127.0.0.1", self.server.port), timeout=3)
        self.addCleanup(peer.close)
        return peer

    def assert_serving(self):
        deadline = time.monotonic() + 3
        while self.server.request("GET", "/pid")[0] != 200:
            self.assertLess(time.monotonic(), deadline)
            time.sleep(0.01)
        self.assertEqual(self.server.request("GET", "/healthz")[0], 200)

    def partial(self):
        peer = self.connect()
        peer.sendall(
            b"POST /upload HTTP/1.1\r\nHost: t\r\nConnection: close\r\n"
            b"Content-Type: multipart/form-data; boundary=b\r\n"
            b"Expect: 100-continue\r\n" + b"Content-Length: %d\r\n\r\n" % len(BODY)
        )
        invited = bytearray()
        while b"\r\n\r\n" not in invited:
            chunk = peer.recv(4096)
            self.assertTrue(chunk, "connection ended before 100-continue")
            invited.extend(chunk)
            self.assertLess(len(invited), 16384)
        self.assertTrue(invited.startswith(b"HTTP/1.1 100 Continue"), invited)
        peer.sendall(BODY[:-9])
        return peer

    def answer(self, peer):
        response = http.client.HTTPResponse(peer)
        response.begin()
        return response.status, json.loads(response.read())

    def upload(self, path="/upload"):
        deadline = time.monotonic() + 3
        while True:
            status, _, body = self.server.request(
                "POST", path, BODY, content_type="multipart/form-data; boundary=b"
            )
            result = status, json.loads(body)
            if result != (503, {"detail": "Server is at capacity"}):
                return result
            self.assertLess(time.monotonic(), deadline)
            time.sleep(0.01)

    def test_partial_upload_limit_completion_and_disconnect_recovery(self):
        first, second = self.partial(), self.partial()
        self.assertEqual(self.server.request("GET", "/healthz")[0], 200)
        self.assert_serving()
        self.assertEqual(self.upload(), (503, {"detail": "Upload capacity is full"}))
        first.sendall(TAIL)
        self.assertEqual(self.answer(first), (200, {"size": 1024}))
        self.assertEqual(self.upload("/retain"), (200, {"size": 1024}))
        self.assertEqual(self.upload()[0], 503)
        second.close()
        deadline = time.monotonic() + 3
        while self.upload()[0] != 200:
            self.assertLess(time.monotonic(), deadline)
            time.sleep(0.01)
        self.assertEqual(self.server.request("POST", "/close")[0], 200)

    def test_retained_python_readers_hold_capacity_until_closed(self):
        self.assertEqual(self.upload("/retain")[0], 200)
        self.assertEqual(self.upload("/retain")[0], 200)
        self.assertEqual(self.upload(), (503, {"detail": "Upload capacity is full"}))
        self.assert_serving()
        self.assertEqual(self.server.request("POST", "/close")[0], 200)
        self.assertEqual(self.upload(), (200, {"size": 1024}))
        self.assertEqual(self.server.request("POST", "/close")[0], 200)

    @unittest.skipUnless(H2, "requires optional h2")
    def test_http2_streams_share_the_spool_lifetime_limit(self):
        client = _H2Peer(self.connect())

        client.send(1, BODY[:-9], start=True)
        time.sleep(0.15)
        client.send(3, BODY[:-9], start=True)
        time.sleep(0.15)
        self.assert_serving()
        client.send(5, BODY, start=True, end=True)
        self.assertEqual(client.answer(5), (503, {"detail": "Upload capacity is full"}))
        client.send(1, TAIL, end=True)
        self.assertEqual(client.answer(1), (200, {"size": 1024}))
        client.send(7, BODY, start=True, end=True)
        self.assertEqual(client.answer(7), (200, {"size": 1024}))
