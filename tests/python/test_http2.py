"""HTTP/2 through the native transport, over TLS and by prior knowledge."""

import argparse
import importlib.util
import json
import os
import select
import shutil
import socket
import ssl
import subprocess
import tempfile
import time
import unittest

from bessemer import App, Compression, PlainTextResponse, Request
from bessemer.config import settings
from bessemer.responses import JSONResponse
from bessemer.testclient import TestClient
from test_tls import certificate

H2 = importlib.util.find_spec("h2") is not None


def curl_speaks_http2():
    curl = shutil.which("curl")
    if curl is None:
        return False
    version = subprocess.run(
        [curl, "--version"], capture_output=True, text=True, check=False
    ).stdout
    return "HTTP2" in version


def build_app():
    app = App()

    @app.get("/where")
    async def where(request: Request):
        return {"scheme": request.scheme, "host": request.headers["host"]}

    @app.post("/echo")
    async def echo(request: Request):
        return {"length": len(await request.body())}

    @app.get("/framed")
    async def framed():
        # The security and request-id controls replace these two.
        return JSONResponse(
            {"ok": True},
            headers={
                "X-Frame-Options": "SAMEORIGIN",
                "X-Request-ID": "from-app",
                "X-Other": "kept",
            },
        )

    return app


def header_lines(head):
    """The (lower-case name, value) pairs of a response head."""
    pairs = []
    for line in head.split("\r\n")[1:]:
        if line:
            name, _, value = line.partition(":")
            pairs.append((name.strip().lower(), value.strip()))
    return pairs


CONTROL_HEADERS = {
    "x-request-id": "client-1",
    "x-content-type-options": "nosniff",
    "x-frame-options": "DENY",
    "referrer-policy": "no-referrer",
    "x-other": "kept",
}


def curl(*arguments):
    result = subprocess.run(
        ["curl", "--silent", "--show-error", "--max-time", "10", *arguments],
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    body, _, version = result.stdout.rpartition("\n")
    return json.loads(body), version


class _H2Client:
    """A prior-knowledge HTTP/2 connection that can leave request bodies open."""

    def __init__(self, port):
        from h2.config import H2Configuration
        from h2.connection import H2Connection

        self.socket = socket.create_connection(("127.0.0.1", port), timeout=10)
        self.connection = H2Connection(
            config=H2Configuration(client_side=True, header_encoding="utf-8")
        )
        self.connection.initiate_connection()
        self.statuses = {}
        self.ended = set()
        self.flush()

    def flush(self):
        self.socket.sendall(self.connection.data_to_send())

    def open(self, path="/where", *, end=False):
        """Send a request head; without ``end`` its body never arrives."""
        stream = self.connection.get_next_available_stream_id()
        self.connection.send_headers(
            stream,
            [
                (":method", "GET"),
                (":scheme", "http"),
                (":authority", "t"),
                (":path", path),
            ],
            end_stream=end,
        )
        self.flush()
        return self, stream

    def request(self, path):
        own = [self.open(path, end=True)]
        found = _answered(own, lambda found: len(found) == 1)
        return found[own[0]], own[0]

    def reset(self, stream):
        self.connection.reset_stream(stream)
        self.flush()

    def receive(self):
        from h2.events import ResponseReceived, StreamEnded, StreamReset

        chunk = self.socket.recv(65536)
        if not chunk:
            raise AssertionError("HTTP/2 connection ended")
        for event in self.connection.receive_data(chunk):
            if isinstance(event, ResponseReceived):
                self.statuses[event.stream_id] = int(dict(event.headers)[":status"])
            elif isinstance(event, (StreamEnded, StreamReset)):
                self.ended.add(event.stream_id)
        self.flush()

    def close(self):
        self.socket.close()


def _answered(streams, done, timeout=30):
    """Read until ``done`` holds for the streams answered so far, by status."""
    peers = {peer for peer, _ in streams}
    deadline = time.monotonic() + timeout
    while True:
        found = {
            (peer, stream): peer.statuses[stream]
            for peer, stream in streams
            if stream in peer.ended and stream in peer.statuses
        }
        if done(found):
            return found
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise AssertionError(f"streams not answered as expected: {found}")
        readable, _, _ = select.select(
            [peer.socket for peer in peers], [], [], remaining
        )
        for peer in peers:
            if peer.socket in readable:
                peer.receive()


def _all_answered(streams):
    return _answered(streams, lambda found: len(found) == len(streams))


class HTTP2Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.mkdtemp()
        cls.cert, cls.key = certificate(cls.directory)

    def alpn(self, port, offered):
        context = ssl.create_default_context(cafile=self.cert)
        context.set_alpn_protocols(offered)
        with (
            socket.create_connection(("localhost", port), timeout=10) as raw,
            context.wrap_socket(raw, server_hostname="localhost") as tls,
        ):
            return tls.selected_alpn_protocol()

    def test_tls_offers_h2_unless_turned_off(self):
        options = {"tls_cert": self.cert, "tls_key": self.key}
        with TestClient(build_app(), server_options=options) as client:
            self.assertEqual(self.alpn(client._port, ["h2", "http/1.1"]), "h2")
            self.assertEqual(self.alpn(client._port, ["http/1.1"]), "http/1.1")
        options = {**options, "http": "1.1"}
        with TestClient(build_app(), server_options=options) as client:
            self.assertEqual(self.alpn(client._port, ["h2", "http/1.1"]), "http/1.1")

    @unittest.skipUnless(curl_speaks_http2(), "requires curl with HTTP/2")
    def test_requests_reach_the_application_over_http2(self):
        options = {"tls_cert": self.cert, "tls_key": self.key}
        with TestClient(build_app(), server_options=options) as client:
            url = f"https://localhost:{client._port}"
            body, version = curl(
                "--http2", "--cacert", self.cert, "-w", "\n%{http_version}",
                f"{url}/where",
            )  # fmt: skip
            self.assertEqual(version, "2")
            self.assertEqual(
                body, {"scheme": "https", "host": f"localhost:{client._port}"}
            )
            body, version = curl(
                "--http2", "--cacert", self.cert, "-w", "\n%{http_version}",
                "--data-binary", "x" * 70_000, f"{url}/echo",
            )  # fmt: skip
            self.assertEqual((body, version), ({"length": 70_000}, "2"))
        with TestClient(build_app()) as client:
            body, version = curl(
                "--http2-prior-knowledge", "-w", "\n%{http_version}",
                f"http://127.0.0.1:{client._port}/where",
            )  # fmt: skip
            self.assertEqual(version, "2")
            self.assertEqual(body["scheme"], "http")
            # HTTP/1.1 clients share the port.
            self.assertEqual(client.get("/where").json()["scheme"], "http")

    @unittest.skipUnless(curl_speaks_http2(), "requires curl with HTTP/2")
    def test_head_length_with_compression_over_http2(self):
        app = App(compression=Compression(br=False, min_size=64))
        text = "compressible response " * 200

        @app.get("/large-json")
        async def large_json():
            return {"text": text}

        @app.get("/large-text")
        async def large_text():
            return PlainTextResponse(text)

        with TestClient(app) as client:

            def headers(method, path, encoding):
                command = [
                    "curl",
                    "--silent",
                    "--show-error",
                    "--max-time",
                    "10",
                    "--http2-prior-knowledge",
                    "-H",
                    f"Accept-Encoding: {encoding}",
                    "-w",
                    "\n%{http_version}",
                ]
                if method == "HEAD":
                    command.append("--head")
                else:
                    command.extend(("-D", "-", "-o", os.devnull))
                command.append(f"http://127.0.0.1:{client._port}{path}")
                result = subprocess.run(
                    command, capture_output=True, text=True, timeout=30, check=True
                )
                head, _, version = result.stdout.rpartition("\n")
                self.assertEqual(version.strip(), "2")
                head = head.replace("\r\n", "\n").strip().replace("\n", "\r\n")
                self.assertTrue(head.startswith("HTTP/2 200"), head)
                return dict(header_lines(head))

            for path in ("/large-json", "/large-text"):
                with self.subTest(path=path):
                    identity_get = headers("GET", path, "identity")
                    gzip_get = headers("GET", path, "gzip")
                    self.assertEqual(gzip_get["content-encoding"], "gzip")
                    self.assertLess(
                        int(gzip_get["content-length"]),
                        int(identity_get["content-length"]),
                    )
                    gzip_head = headers("HEAD", path, "gzip")
                    self.assertNotIn("content-length", gzip_head)
                    identity_head = headers("HEAD", path, "identity")
                    self.assertEqual(
                        identity_head["content-length"], identity_get["content-length"]
                    )

    def assert_control_headers(self, pairs):
        for name, value in CONTROL_HEADERS.items():
            self.assertEqual(
                [found for key, found in pairs if key == name], [value], name
            )

    def test_control_headers_replace_the_applications_on_both_protocols(self):
        with TestClient(build_app()) as client:
            # HTTP/1.1 writes these headers straight into the response head.
            with socket.create_connection(("127.0.0.1", client._port)) as raw:
                raw.sendall(
                    b"GET /framed HTTP/1.1\r\nHost: a\r\nX-Request-ID: client-1\r\n"
                    b"Connection: close\r\n\r\n"
                )
                output = b""
                while chunk := raw.recv(65536):
                    output += chunk
            head, _, body = output.decode().partition("\r\n\r\n")
            self.assertTrue(head.startswith("HTTP/1.1 200 OK"), head)
            self.assertEqual(json.loads(body), {"ok": True})
            self.assert_control_headers(header_lines(head))
            if not curl_speaks_http2():
                return
            # HTTP/2 keeps them in the response's header map.
            result = subprocess.run(
                [
                    "curl", "--silent", "--show-error", "--max-time", "10",
                    "--http2-prior-knowledge", "-D", "-", "-o", os.devnull,
                    "-H", "X-Request-ID: client-1",
                    f"http://127.0.0.1:{client._port}/framed",
                ],
                capture_output=True, text=True, timeout=30, check=True,
            )  # fmt: skip
            head = result.stdout.replace("\r\n", "\n").strip().replace("\n", "\r\n")
            self.assertTrue(head.startswith("HTTP/2 200"), head)
            self.assert_control_headers(header_lines(head))

    @unittest.skipUnless(H2, "requires the h2 package")
    def test_only_the_routers_405_lists_a_paths_methods_on_both_protocols(self):
        from h2.config import H2Configuration
        from h2.connection import H2Connection
        from h2.events import ResponseReceived

        os.environ["BESSEMER_TEST_ALLOW_KEY"] = "k" * 32
        self.addCleanup(os.environ.pop, "BESSEMER_TEST_ALLOW_KEY", None)
        app = App(api_key={"env": "BESSEMER_TEST_ALLOW_KEY"})

        @app.get("/private")
        async def private():
            return {}

        @app.post("/private")
        async def private_post():
            return {}

        def http1(port, path, headers=()):
            with socket.create_connection(("127.0.0.1", port), timeout=10) as raw:
                lines = [f"PUT {path} HTTP/1.1", "Host: a", "Content-Length: 0"]
                lines += [f"{name}: {value}" for name, value in headers]
                raw.sendall(
                    ("\r\n".join([*lines, "Connection: close", "", ""])).encode()
                )
                output = b""
                while chunk := raw.recv(65536):
                    output += chunk
            head = output.decode().partition("\r\n\r\n")[0]
            return int(head.split()[1]), dict(header_lines(head)).get("allow")

        def http2(port, path, headers=()):
            with socket.create_connection(("127.0.0.1", port), timeout=10) as raw:
                connection = H2Connection(
                    config=H2Configuration(client_side=True, header_encoding="utf-8")
                )
                connection.initiate_connection()
                connection.send_headers(
                    1,
                    [
                        (":method", "PUT"),
                        (":scheme", "http"),
                        (":authority", "a"),
                        (":path", path),
                        *headers,
                    ],
                    end_stream=True,
                )
                raw.sendall(connection.data_to_send())
                while True:
                    chunk = raw.recv(65536)
                    self.assertTrue(chunk, "HTTP/2 connection ended")
                    for event in connection.receive_data(chunk):
                        if isinstance(event, ResponseReceived):
                            found = dict(event.headers)
                            return int(found[":status"]), found.get("allow")
                    raw.sendall(connection.data_to_send())

        key = [("x-api-key", "k" * 32)]
        with TestClient(app) as client:
            for send in (http1, http2):
                with self.subTest(protocol=send.__name__):
                    # The control's 401 is the same for a known path with
                    # another method as for an unknown path.
                    self.assertEqual(send(client._port, "/private"), (401, None))
                    self.assertEqual(send(client._port, "/elsewhere"), (401, None))
                    self.assertEqual(
                        send(client._port, "/private", key), (405, "GET,HEAD,POST")
                    )

    @unittest.skipUnless(H2, "requires the h2 package")
    def test_streams_waiting_for_a_body_are_bounded_by_max_connections(self):
        # Streams that leave their bodies open hold no admission slot; at most
        # `max_connections` of them may wait at once, across connections.
        bound = 6
        options = {"max_connections": bound, "request_timeout": 3.0}
        with TestClient(build_app(), server_options=options) as client:
            first, second = _H2Client(client._port), _H2Client(client._port)
            try:
                waiting = [first.open() for _ in range(4)]
                waiting += [second.open() for _ in range(4)]
                # Two are refused at once, whichever connection they are on.
                _answered(waiting, lambda found: list(found.values()).count(503) == 2)
                # Requests whose bodies are complete do not wait, and are served.
                self.assertEqual(second.request("/where")[0], 200)
                self.assertEqual(client.get("/where").status_code, 200)
                found = _answered(waiting, lambda found: len(found) == len(waiting))
                self.assertEqual(sorted(found.values()), [503] * 2 + [504] * bound)
                # Answered streams are closed, the refused ones as well: the
                # server reset each body it did not read, so none stays open.
                self.assertEqual(first.connection.open_outbound_streams, 0)
                self.assertEqual(second.connection.open_outbound_streams, 0)

                # Places came back when those streams timed out.
                again = [first.open() for _ in range(3)]
                again += [second.open() for _ in range(3)]
                found = _answered(again, lambda found: len(found) == len(again))
                self.assertEqual(sorted(found.values()), [504] * bound)

                # And come back when the client resets the streams waiting.
                held = [first.open() for _ in range(3)]
                held += [second.open() for _ in range(4)]
                found = _answered(held, lambda found: 503 in found.values())
                self.assertEqual(list(found.values()).count(503), 1)
                for peer, stream in held:
                    if stream not in peer.ended:
                        peer.reset(stream)
                accepted = 0
                deadline = time.monotonic() + 30
                while accepted < bound and time.monotonic() < deadline:
                    streams = [first.open() for _ in range(bound - accepted)]
                    found = _all_answered(streams)
                    accepted += list(found.values()).count(504)
                    if accepted < bound:
                        time.sleep(0.2)  # Resets not yet processed.
                self.assertEqual(accepted, bound)
            finally:
                first.close()
                second.close()

    def test_http_setting_is_validated(self):
        self.assertEqual(settings(argparse.Namespace())["http"], "auto")
        self.assertEqual(settings(argparse.Namespace(http="1.1"))["http"], "1.1")
        with self.assertRaises(ValueError):
            settings(argparse.Namespace(http="2"))
        with self.assertRaises(ValueError):
            build_app().run(http="h2")


if __name__ == "__main__":
    unittest.main()
