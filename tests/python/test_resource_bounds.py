"""Conditions a client or an application bug must not turn into an outage.

Each test here reproduces something that used to stop a worker, hold memory
without bound, or send a client somewhere it should not go.
"""

import http.client
import importlib.util
import json
import os
import socket
import sys
import time
import unittest

from test_native_integration import Server
from test_websockets import Client

NATIVE = importlib.util.find_spec("bessemer._native") is not None

APPLICATION = """
import asyncio
import os
from contextlib import asynccontextmanager
from pathlib import Path
from bessemer import App, Request, WebSocket

Path("public").mkdir(exist_ok=True)
Path("public/index.html").write_text("<h1>index</h1>")

@asynccontextmanager
async def lifespan(app):
    app.state.pool = object()
    yield
    if os.environ.get("TEARDOWN_FAILS"):
        # An ordinary teardown bug: the attribute does not exist.
        app.state.pool.close()

app = App(lifespan=lifespan, strict_content_type=os.environ.get("LENIENT_JSON") is None)
app.health("/healthz")
release = asyncio.Event()

@app.get("/pid")
async def pid():
    return {"pid": os.getpid()}

@app.post("/echo")
async def echo(payload: dict):
    return payload

@app.get("/slow")
async def slow():
    Path("slow-started").touch()
    await asyncio.sleep(1.0)
    return {"slow": True}

@app.get("/release")
async def let_go():
    release.set()
    return {"released": True}

@app.websocket("/hold")
async def hold(websocket: WebSocket):
    # Accepts, then reads nothing until told to.
    await websocket.accept()
    await release.wait()
    total = count = 0
    while count < int(websocket.query_params["messages"]):
        total += len(await websocket.receive_bytes())
        count += 1
    await websocket.send_json({"messages": count, "bytes": total})

app.static("/", "public")
"""


WAITING = """
import asyncio
import os
import random
from pathlib import Path
from bessemer import App, Compression, Response, UploadFile

Path("public").mkdir(exist_ok=True)
# Text that compresses to a little over half its size.
noise = random.Random(7)
Path("public/small.css").write_text(noise.randbytes(32_768).hex())
Path("public/large.css").write_text(noise.randbytes(204_800).hex())

app = App(compression=Compression(min_size=512))
app.health("/healthz")

@app.get("/pid")
async def pid():
    return {"pid": os.getpid()}

@app.post("/echo")
async def echo(payload: dict):
    return payload

@app.get("/slow")
async def slow():
    with open("slow-started", "a") as started:
        started.write("x")
    await asyncio.sleep(1.0)
    return {"slow": True}

@app.post("/upload")
async def upload(upload: UploadFile):
    data = await upload.read()
    return {"size": len(data), "sum": sum(data) % 65536}

@app.get("/large")
async def large() -> Response:
    return Response(bytes(32 * 1024 * 1024), media_type="application/octet-stream")

app.static("/static", "public")
"""


def raw(port, request, *, wait=1.0):
    """Send bytes on a new connection and return everything received."""
    with socket.create_connection(("127.0.0.1", port), timeout=5) as connection:
        connection.sendall(request)
        connection.settimeout(wait)
        received = b""
        try:
            while chunk := connection.recv(65536):
                received += chunk
        except TimeoutError:
            pass
        return received


def lower_descriptor_limit():
    import resource

    resource.setrlimit(resource.RLIMIT_NOFILE, (96, 96))


@unittest.skipUnless(NATIVE, "build the native extension first")
class RequestBoundTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = Server(
            application=APPLICATION,
            max_body_size=1_048_576,
            extra_args=("--max-in-flight", "16"),
        )

    @classmethod
    def tearDownClass(cls):
        cls.server.close()

    def test_root_static_mount_never_redirects_to_another_host(self):
        for path in ("//evil.example/%ff", "/assets/%ff", "//evil.example/"):
            with self.subTest(path=path):
                response = raw(
                    self.server.port,
                    f"GET {path} HTTP/1.1\r\nHost: t\r\nConnection: close\r\n\r\n".encode(),
                )
                self.assertTrue(response.startswith(b"HTTP/1.1 404 "), response[:60])
                self.assertNotIn(b"location:", response.lower())
        self.assertEqual(self.server.request("GET", "/index.html")[0], 200)

    def test_json_body_without_a_content_type_is_refused(self):
        body = b'{"a": 1}'
        head = b"POST /echo HTTP/1.1\r\nHost: t\r\nConnection: close\r\n"
        length = b"Content-Length: %d\r\n" % len(body)
        refused = raw(self.server.port, head + length + b"\r\n" + body)
        self.assertTrue(refused.startswith(b"HTTP/1.1 415 "), refused[:60])
        for content_type in (b"application/json", b"application/vnd.api+json"):
            with self.subTest(content_type=content_type):
                accepted = raw(
                    self.server.port,
                    head
                    + length
                    + b"Content-Type: "
                    + content_type
                    + b"\r\n\r\n"
                    + body,
                )
                self.assertTrue(accepted.startswith(b"HTTP/1.1 200 "), accepted[:60])
        # A route with no body parameter does not care what a body claims.
        ignored = raw(
            self.server.port,
            b"GET /pid HTTP/1.1\r\nHost: t\r\nConnection: close\r\n"
            + length
            + b"\r\n"
            + body,
        )
        self.assertTrue(ignored.startswith(b"HTTP/1.1 200 "), ignored[:60])

    def test_oversize_declared_body_is_refused_before_it_is_invited(self):
        started = time.monotonic()
        response = raw(
            self.server.port,
            b"POST /echo HTTP/1.1\r\nHost: t\r\nContent-Type: application/json\r\n"
            b"Content-Length: 99999999\r\nExpect: 100-continue\r\n\r\n",
        )
        self.assertTrue(response.startswith(b"HTTP/1.1 413 "), response[:60])
        self.assertNotIn(b"100 Continue", response)
        self.assertIn(b"connection: close", response.lower())
        # Refused on the declared length, not after waiting for the body.
        self.assertLess(time.monotonic() - started, 1.5)
        # A body within the limit is still invited and read.
        with socket.create_connection(("127.0.0.1", self.server.port), timeout=5) as c:
            c.sendall(
                b"POST /echo HTTP/1.1\r\nHost: t\r\nContent-Type: application/json\r\n"
                b"Content-Length: 2\r\nExpect: 100-continue\r\nConnection: close\r\n\r\n"
            )
            self.assertTrue(c.recv(64).startswith(b"HTTP/1.1 100 Continue"))
            c.sendall(b"{}")
            self.assertIn(b"HTTP/1.1 200 ", c.recv(4096))

    def test_websocket_messages_nobody_reads_are_bounded_in_bytes(self):
        # 24 messages of the largest size (1 MiB). Message credit alone let 64
        # wait in memory; now the server stops reading at about two, and the
        # rest stay in socket buffers until the handler consumes.
        messages, size = 24, 1_048_576
        client = Client(self.server.port, f"/hold?messages={messages}")
        self.assertEqual(client.status, 101)
        payload = bytes(size)
        client.socket.settimeout(1.0)
        sent = 0
        try:
            while sent < messages:
                self._send(client, payload)
                sent += 1
        except TimeoutError:
            pass
        self.assertLess(sent, messages, "the server kept reading with nobody consuming")
        # Once the handler reads, everything flows and arrives intact.
        self.assertEqual(self.server.request("GET", "/release")[0], 200)
        client.socket.settimeout(10.0)
        self._finish(client, payload)
        for _ in range(sent + 1, messages):
            self._send(client, payload)
        opcode, body = client.receive()
        self.assertEqual(opcode, 0x1)
        self.assertEqual(
            json.loads(body), {"messages": messages, "bytes": messages * size}
        )
        client.close()

    def _send(self, client, payload):
        # A zero mask leaves the payload as it is and keeps the frame cheap to
        # build; the pending tail of an interrupted frame is kept for _finish.
        header = b"\x82\xff" + len(payload).to_bytes(8, "big") + b"\x00\x00\x00\x00"
        frame = header + payload
        self._pending = frame
        view = memoryview(frame)
        while view:
            written = client.socket.send(view)
            view = view[written:]
            self._pending = bytes(view)
        self._pending = b""

    def _finish(self, client, payload):
        if getattr(self, "_pending", b""):
            client.socket.sendall(self._pending)
            self._pending = b""


def answer(connection, *, timeout=5.0):
    """Read one response with a Content-Length from a raw connection."""
    connection.settimeout(timeout)
    received = b""
    while b"\r\n\r\n" not in received:
        chunk = connection.recv(65536)
        if not chunk:
            return received, b""
        received += chunk
    head, _, body = received.partition(b"\r\n\r\n")
    length = 0
    for line in head.split(b"\r\n")[1:]:
        name, _, value = line.partition(b":")
        if name.strip().lower() == b"content-length":
            length = int(value)
    while len(body) < length:
        chunk = connection.recv(65536)
        if not chunk:
            break
        body += chunk
    return head, body


@unittest.skipUnless(NATIVE, "build the native extension first")
class WaitingForTheClientTests(unittest.TestCase):
    """A request holds an admission slot for its work, not for its client."""

    ECHO = (
        b"POST /echo HTTP/1.1\r\nHost: t\r\nContent-Type: application/json\r\n"
        b"Content-Length: 8\r\n\r\n"
    )

    def setUp(self):
        self.server = Server(
            application=WAITING,
            max_body_size=65_536,
            extra_args=(
                "--max-in-flight",
                "2",
                "--request-timeout",
                "5",
                "--max-upload-size",
                "8388608",
            ),
        )
        self.addCleanup(self.server.close)
        self.held = []
        self.addCleanup(lambda: [connection.close() for connection in self.held])

    def connect(self, head):
        connection = socket.create_connection(
            ("127.0.0.1", self.server.port), timeout=5
        )
        self.held.append(connection)
        connection.sendall(head)
        return connection

    def assert_serving(self):
        for _ in range(4):
            status, _, body = self.server.request("GET", "/pid")
            self.assertEqual(status, 200, body)

    def test_bodies_that_are_declared_and_not_sent_hold_no_slot(self):
        # Twice as many as there are slots, each a head with no body.
        waiting = [self.connect(self.ECHO) for _ in range(4)]
        time.sleep(0.3)
        self.assert_serving()
        # Each is still answered once its body arrives.
        for connection in waiting:
            connection.sendall(b'{"a": 1}')
            head, body = answer(connection)
            self.assertTrue(head.startswith(b"HTTP/1.1 200 "), head[:60])
            self.assertEqual(json.loads(body), {"a": 1})

    def test_a_body_that_arrives_when_every_slot_is_busy_is_refused(self):
        waiting = self.connect(self.ECHO)
        time.sleep(0.2)
        busy = [
            self.connect(b"GET /slow HTTP/1.1\r\nHost: t\r\n\r\n") for _ in range(2)
        ]
        started = self.server.root / "slow-started"
        deadline = time.monotonic() + 5
        while not (started.exists() and len(started.read_text()) == 2):
            self.assertLess(
                time.monotonic(), deadline, "the slow requests did not start"
            )
            time.sleep(0.01)
        waiting.sendall(b'{"a": 1}')
        head, _ = answer(waiting)
        self.assertTrue(head.startswith(b"HTTP/1.1 503 "), head[:60])
        self.assertIn(b"retry-after: 1", head.lower())
        for connection in busy:
            head, _ = answer(connection)
            self.assertTrue(head.startswith(b"HTTP/1.1 200 "), head[:60])

    def test_uploads_that_stop_part_way_hold_no_slot_and_finish_in_turn(self):
        data = bytes(index % 251 for index in range(200_000))
        body = (
            b'--b\r\nContent-Disposition: form-data; name="upload"; filename="x.bin"\r\n'
            b"Content-Type: application/octet-stream\r\n\r\n" + data + b"\r\n--b--\r\n"
        )
        head = (
            b"POST /upload HTTP/1.1\r\nHost: t\r\n"
            b"Content-Type: multipart/form-data; boundary=b\r\n"
            b"Content-Length: %d\r\n\r\n" % len(body)
        )
        # Larger than the body limit, so each is spooled to disk as it arrives.
        self.assertGreater(len(body), 65_536)
        # The two spool lifetimes remain bounded while their request execution
        # slots are free, so unrelated requests can still run.
        uploads = []
        for _ in range(2):
            uploads.append(self.connect(head + body[:50_000]))
            time.sleep(0.15)
        self.assert_serving()
        for connection in uploads:
            connection.sendall(body[50_000:])
        for connection in uploads:
            head, result = answer(connection)
            self.assertTrue(head.startswith(b"HTTP/1.1 200 "), head[:60])
            self.assertEqual(
                json.loads(result), {"size": len(data), "sum": sum(data) % 65536}
            )


@unittest.skipUnless(NATIVE, "build the native extension first")
class CompressedFileBudgetTests(unittest.TestCase):
    def fetch(self, server, path):
        connection = http.client.HTTPConnection("127.0.0.1", server.port, timeout=5)
        try:
            connection.request("GET", path, headers={"Accept-Encoding": "gzip"})
            response = connection.getresponse()
            return response, response.read()
        finally:
            connection.close()

    def test_a_file_compressed_whole_must_fit_the_response_byte_budget(self):
        import gzip

        server = Server(
            application=WAITING,
            extra_args=(
                "--max-response-size",
                "262144",
                "--max-response-bytes-in-flight",
                "300000",
            ),
        )
        try:
            public = server.root / "public"
            for _ in range(2):
                # 64 KiB of text fits the budget: read whole, compressed
                # whole, sent with its length.
                response, body = self.fetch(server, "/static/small.css")
                self.assertEqual(response.status, 200)
                self.assertEqual(response.getheader("Content-Encoding"), "gzip")
                self.assertEqual(response.getheader("Content-Length"), str(len(body)))
                self.assertEqual(
                    gzip.decompress(body), (public / "small.css").read_bytes()
                )
                # 400 KiB does not: it used to be read into memory anyway.
                # Now it is compressed a chunk at a time, like a larger file.
                response, body = self.fetch(server, "/static/large.css")
                self.assertEqual(response.status, 200)
                self.assertEqual(response.getheader("Content-Encoding"), "gzip")
                self.assertIsNone(response.getheader("Content-Length"))
                self.assertEqual(response.getheader("Transfer-Encoding"), "chunked")
                self.assertEqual(
                    gzip.decompress(body), (public / "large.css").read_bytes()
                )
        finally:
            server.close()


@unittest.skipUnless(NATIVE, "build the native extension first")
class SendFloorTests(unittest.TestCase):
    SIZE = 32 * 1024 * 1024

    def delivered(self, floor):
        """Read a 32 MiB response at about 3 MB a second for a while, then
        as fast as it comes. Returns the body bytes that arrived in all.

        A server that gives up on the response stops writing, but what the
        kernel had already taken still arrives, so the count is what tells.
        """
        server = Server(
            application=WAITING,
            extra_args=(
                "--request-timeout",
                "1",
                "--min-send-rate",
                str(floor),
                "--max-response-size",
                "34000000",
                "--max-response-bytes-in-flight",
                "34000000",
            ),
        )
        try:
            with socket.create_connection(
                ("127.0.0.1", server.port), timeout=10
            ) as connection:
                connection.sendall(
                    b"GET /large HTTP/1.1\r\nHost: t\r\nConnection: close\r\n\r\n"
                )
                received = b""
                while b"\r\n\r\n" not in received:
                    received += connection.recv(65536)
                count = len(received.partition(b"\r\n\r\n")[2])
                # Never idle: a read every 20 ms, far inside the timeout.
                slow_until = time.monotonic() + 2.5
                while True:
                    if time.monotonic() < slow_until:
                        time.sleep(0.02)
                    try:
                        chunk = connection.recv(65536)
                    except ConnectionResetError:
                        return count
                    if not chunk:
                        return count
                    count += len(chunk)
        finally:
            server.close()

    @unittest.skipUnless(
        sys.platform == "linux", "depends on how much the kernel buffers"
    )
    def test_a_reader_below_the_floor_loses_its_response_and_one_above_keeps_it(self):
        # Twelve megabytes a second asked of a reader taking about three: it
        # is cut off within a couple of seconds, well short of the body.
        self.assertLess(self.delivered(12_000_000), self.SIZE // 2)
        # The default floor is far below that reader, which gets everything.
        self.assertEqual(self.delivered(240), self.SIZE)
        # So does a reader with no floor at all.
        self.assertEqual(self.delivered(0), self.SIZE)

    def test_the_floor_is_a_checked_setting(self):
        import argparse
        from unittest.mock import patch

        from bessemer import App
        from bessemer.config import settings

        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(settings(argparse.Namespace())["min_send_rate"], 240)
            self.assertEqual(
                settings(argparse.Namespace(min_send_rate=0))["min_send_rate"], 0
            )
        with patch.dict(os.environ, {"BESSEMER_MIN_SEND_RATE": "4096"}, clear=True):
            self.assertEqual(settings(argparse.Namespace())["min_send_rate"], 4096)
        for value in (-1, True, 1.5, "240", 2**31):
            with (
                self.subTest(value=value),
                self.assertRaisesRegex(ValueError, "min_send_rate"),
            ):
                App().run(min_send_rate=value)


@unittest.skipUnless(NATIVE, "build the native extension first")
class LenientContentTypeTests(unittest.TestCase):
    def test_strict_content_type_false_reads_an_undeclared_body_as_json(self):
        server = Server(application=APPLICATION, environment={"LENIENT_JSON": "1"})
        try:
            body = b'{"a": 1}'
            response = raw(
                server.port,
                b"POST /echo HTTP/1.1\r\nHost: t\r\nConnection: close\r\n"
                b"Content-Length: %d\r\n\r\n" % len(body) + body,
            )
            self.assertTrue(response.startswith(b"HTTP/1.1 200 "), response[:60])
            # A type that is not JSON is refused either way.
            response = raw(
                server.port,
                b"POST /echo HTTP/1.1\r\nHost: t\r\nConnection: close\r\n"
                b"Content-Type: text/plain\r\nContent-Length: %d\r\n\r\n"
                % len(body)
                + body,
            )
            self.assertTrue(response.startswith(b"HTTP/1.1 415 "), response[:60])
        finally:
            server.close()

    def test_the_option_must_be_a_boolean(self):
        from bessemer import App

        with self.assertRaisesRegex(TypeError, "strict_content_type"):
            App(strict_content_type="yes")
        self.assertTrue(App().strict_content_type)


@unittest.skipUnless(
    NATIVE and os.name == "posix", "needs the native extension and POSIX limits"
)
class WorkerSurvivalTests(unittest.TestCase):
    def worker_pid(self, server):
        status, _, body = server.request("GET", "/pid")
        self.assertEqual(status, 200)
        return json.loads(body)["pid"]

    def test_running_out_of_descriptors_does_not_end_the_worker(self):
        server = Server(application=APPLICATION, preexec_fn=lower_descriptor_limit)
        try:
            pid = self.worker_pid(server)
            held = []
            try:
                for _ in range(160):
                    held.append(
                        socket.create_connection(("127.0.0.1", server.port), timeout=2)
                    )
                # The worker is refused descriptors by now. It must wait, not exit.
                time.sleep(0.5)
            finally:
                for connection in held:
                    connection.close()
            deadline = time.monotonic() + 5
            while True:
                try:
                    if self.worker_pid(server) == pid:
                        break
                except (OSError, http.client.HTTPException, AssertionError):
                    pass
                self.assertLess(
                    time.monotonic(), deadline, "the worker did not recover"
                )
                time.sleep(0.05)
            server.log.seek(0)
            log = server.log.read()
            self.assertIn("could not accept a connection", log)
            self.assertNotIn("worker failed", log)
        finally:
            server.close()

    def test_a_teardown_error_during_recycling_does_not_stop_the_service(self):
        server = Server(
            application=APPLICATION,
            environment={"TEARDOWN_FAILS": "1"},
            extra_args=("--max-requests", "5", "--graceful-timeout", "2"),
        )
        try:
            pids = set()
            deadline = time.monotonic() + 15
            while len(pids) < 3:
                self.assertLess(time.monotonic(), deadline, f"only saw workers {pids}")
                try:
                    pids.add(self.worker_pid(server))
                except (OSError, http.client.HTTPException, AssertionError):
                    time.sleep(0.02)
            self.assertIsNone(server.process.poll(), "the supervisor exited")
            server.log.seek(0)
            self.assertNotIn("restart limit exceeded", server.log.read())
        finally:
            server.close()

    @unittest.skipUnless(
        sys.platform == "linux", "Linux refuses once every listener is closed"
    )
    def test_new_connections_are_refused_while_the_server_drains(self):
        server = Server(application=APPLICATION, extra_args=("--graceful-timeout", "5"))
        try:
            with socket.create_connection(
                ("127.0.0.1", server.port), timeout=5
            ) as busy:
                busy.sendall(b"GET /slow HTTP/1.1\r\nHost: t\r\n\r\n")
                deadline = time.monotonic() + 5
                while not (server.root / "slow-started").exists():
                    self.assertLess(time.monotonic(), deadline)
                    time.sleep(0.01)
                server.stop()
                # Refused within the drain, not queued until it ends.
                deadline = time.monotonic() + 0.8
                while True:
                    try:
                        socket.create_connection(
                            ("127.0.0.1", server.port), timeout=1
                        ).close()
                    except ConnectionRefusedError:
                        break
                    self.assertLess(
                        time.monotonic(), deadline, "still accepting during the drain"
                    )
                    time.sleep(0.01)
                # The request already in flight is still answered.
                busy.settimeout(5)
                self.assertTrue(busy.recv(4096).startswith(b"HTTP/1.1 200 "))
            self.assertEqual(server.process.wait(timeout=10), 0)
        finally:
            server.close()


if __name__ == "__main__":
    unittest.main()
