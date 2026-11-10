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
