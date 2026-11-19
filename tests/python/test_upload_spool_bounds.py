"""Installed uploads bound paused files and keep retained readers charged."""

import gc
import http.client
import importlib.util
import io
import json
import socket
import sys
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
DROPPED = """
import gc
from bessemer import App, UploadFile
# Only reference counts free anything here, unless a collection is asked for.
gc.disable()
app = App()
app.health('/healthz')
@app.post('/upload')
async def upload(upload: UploadFile):
    return {'size': len(await upload.read())}
@app.post('/failing')
def failing(upload: UploadFile):
    upload.file.read()
    raise RuntimeError('handler bug')
@app.post('/offloaded')
async def offloaded(upload: UploadFile):
    import asyncio
    def work(part):
        part.file.read()
        raise RuntimeError('worker bug')
    await asyncio.get_running_loop().run_in_executor(None, work, upload)
@app.post('/cycle')
async def cycle(upload: UploadFile):
    size = len(await upload.read())
    try:
        raise ValueError('kept')
    except ValueError as error:
        kept = error  # Its traceback holds this frame, which holds it.
    return {'size': size}
"""
SHARED = """
import asyncio, os, sys
from typing import Annotated
from bessemer import App, File, UploadFile
app = App()
app.health('/healthz')
kept = []
def descriptors():
    return len(os.listdir('/proc/self/fd'))
@app.get('/descriptors')
async def idle():
    return {'open': descriptors()}
@app.post('/parts')
async def parts(files: Annotated[list[UploadFile], File()]):
    held = descriptors()
    first, second = files[0], files[1]
    # Two parts of one request read at once, each at its own position.
    a, b = await asyncio.gather(first.read(), second.read())
    await first.seek(2)
    tail = await first.read()
    await first.close()
    # Closing one part leaves the others readable.
    await second.seek(0)
    again = await second.read()
    rest = [(await part.read()).decode() for part in files[2:]]
    kept.extend(files[1:])
    return {
        'open': held, 'first': a.decode(), 'second': b.decode(), 'tail': tail.decode(),
        'again': again.decode(), 'rest': rest == [f'part {i}' for i in range(2, len(files))],
        'closed': [first.file.closed, second.file.closed],
    }
@app.post('/release')
async def release():
    while kept:
        await kept.pop().close()
    return {}
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

    def test_a_declared_length_over_the_upload_limit_is_refused_unread(self):
        peer = self.connect()
        peer.sendall(
            b"POST /upload HTTP/1.1\r\nHost: t\r\n"
            b"Content-Type: multipart/form-data; boundary=b\r\n"
            b"Expect: 100-continue\r\nContent-Length: 1048577\r\n\r\n"
        )
        # Answered at once: no invitation to send it, nothing read.
        peer.settimeout(5)
        response = http.client.HTTPResponse(peer)
        response.begin()
        self.assertEqual(
            (response.status, json.loads(response.read())),
            (413, {"detail": "Request body exceeds the configured upload limit"}),
        )
        self.assertEqual(response.getheader("connection"), "close")
        self.assert_serving()

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


@unittest.skipUnless(NATIVE, "build the native extension first")
class DroppedReaderTests(unittest.TestCase):
    """Uploads nobody holds any more do not keep their capacity."""

    def setUp(self):
        self.server = Server(
            application=DROPPED,
            max_body_size=128,
            extra_args=("--max-in-flight", "2", "--max-upload-size", "1048576"),
        )
        self.addCleanup(self.server.close)

    def post(self, path):
        status, _, body = self.server.request(
            "POST", path, BODY, content_type="multipart/form-data; boundary=b"
        )
        return status, json.loads(body)

    def test_a_failing_handler_does_not_keep_its_upload(self):
        # With two spool slots, a third request shows whether the first two
        # let go. A failed sync call used to leave its future, its error and
        # the waiting frame holding each other, and the upload with them.
        counted = getattr(sys, "_is_gil_enabled", lambda: True)()
        for path in ("/failing", "/offloaded"):
            with self.subTest(path=path):
                if counted:
                    # Reference counts alone free it: no request is refused.
                    for _ in range(4):
                        self.assertEqual(self.post(path)[0], 500)
                    self.assertEqual(self.post("/upload"), (200, {"size": 1024}))
                    continue
                # Without the GIL an object last released by another thread
                # is freed when its own thread next runs Python or at a
                # collection, so a request may be refused once; that refusal
                # asks for the collection that frees it.
                for _ in range(4):
                    self.assertIn(self.post(path)[0], (500, 503))
                deadline = time.monotonic() + 10
                while self.post("/upload") != (200, {"size": 1024}):
                    self.assertLess(
                        time.monotonic(), deadline, "capacity never came back"
                    )
                    time.sleep(0.05)

    def test_uploads_dropped_in_a_cycle_are_collected_when_capacity_runs_out(self):
        self.assertEqual(self.post("/cycle"), (200, {"size": 1024}))
        self.assertEqual(self.post("/cycle"), (200, {"size": 1024}))
        # Both slots are held by readers only the collector can free, and a
        # refused upload runs no Python that would start one. The refusal
        # itself asks for a collection.
        deadline = time.monotonic() + 5
        refused = 0
        while True:
            status, body = self.post("/upload")
            if status == 200:
                break
            self.assertEqual(
                (status, body), (503, {"detail": "Upload capacity is full"})
            )
            refused += 1
            self.assertLess(time.monotonic(), deadline, "capacity never came back")
            time.sleep(0.05)
        self.assertGreaterEqual(refused, 1, "the cycle did not hold a slot at all")
        self.assertEqual(body, {"size": 1024})


@unittest.skipUnless(NATIVE, "build the native extension first")
@unittest.skipUnless(sys.platform == "linux", "counts descriptors in /proc")
class SharedSpoolHandleTests(unittest.TestCase):
    """A spooled request's file parts share its one spool descriptor."""

    def setUp(self):
        self.server = Server(
            application=SHARED,
            max_body_size=128,
            extra_args=("--max-upload-size", "1048576"),
        )
        self.addCleanup(self.server.close)

    def post_parts(self, count):
        body = b"".join(
            b'--b\r\nContent-Disposition: form-data; name="files"; '
            b'filename="f%d"\r\n\r\npart %d\r\n' % (index, index)
            for index in range(count)
        )
        status, _, answer = self.server.request(
            "POST",
            "/parts",
            body + b"--b--\r\n",
            content_type="multipart/form-data; boundary=b",
        )
        return status, json.loads(answer)

    def test_parts_share_one_descriptor_and_keep_their_own_readers(self):
        idle = json.loads(self.server.request("GET", "/descriptors")[2])["open"]
        status, result = self.post_parts(300)
        self.assertEqual(status, 200, result)
        # One descriptor per part was 300 more than idle.
        self.assertLess(result.pop("open") - idle, 16)
        self.assertEqual(
            result,
            {
                "first": "part 0",
                "second": "part 1",
                "tail": "rt 0",
                "again": "part 1",
                "rest": True,
                "closed": [True, False],
            },
        )
        # The parts still held keep the request's spool slot (one, with
        # --max-in-flight 1) until the last of them is closed.
        self.assertEqual(
            self.post_parts(2),
            (503, {"detail": "Upload capacity is full"}),
        )
        self.assertEqual(self.server.request("POST", "/release")[0], 200)
        deadline = time.monotonic() + 5
        while (result := self.post_parts(2))[0] != 200:
            self.assertLess(time.monotonic(), deadline, result)
            time.sleep(0.05)
        self.assertEqual(self.server.request("POST", "/release")[0], 200)
