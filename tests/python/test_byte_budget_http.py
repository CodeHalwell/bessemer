"""Real HTTP ownership tests for aggregate retained request/response payloads."""

from __future__ import annotations

import http.client
import importlib.util
import json
import socket
import time
import unittest
from concurrent.futures import ThreadPoolExecutor

from test_native_integration import Server

BIG_RESPONSE = 8 * 1024 * 1024
OUTPUT_BUDGET = BIG_RESPONSE + 1024
STATIC_RESPONSE_LIMIT = 9 * 1024 * 1024

APPLICATION = """
import asyncio
from pathlib import Path
from bessemer import App, Request, Response

app = App()
app.health('/healthz')

@app.post('/ingest')
async def ingest(request: Request):
    return {'size': len(await request.body())}

@app.post('/held', timeout=0.05)
async def held(request: Request):
    Path('handler_entered').touch()
    try:
        await asyncio.Event().wait()
    finally:
        Path('cleanup_started').touch()
        while not Path('release_cleanup').exists():
            await asyncio.sleep(0.005)
        Path('cleanup_finished').touch()

@app.get('/tiny')
async def tiny():
    return {'ok': True}

@app.get('/large')
async def large() -> Response:
    return Response(b'x' * 8388608, media_type='application/octet-stream')
"""

STATIC_APPLICATION = """
from bessemer import App
app = App(description='d' * 8388608)
app.health('/healthz')

@app.get('/tiny')
async def tiny():
    return {'ok': True}
"""


def request(server, method, path, body=None):
    connection = http.client.HTTPConnection("127.0.0.1", server.port, timeout=5)
    try:
        connection.request(method, path, body, {"Connection": "close"})
        response = connection.getresponse()
        return response.status, dict(response.getheaders()), response.read()
    finally:
        connection.close()


def wait_until(predicate, *, timeout=3):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = predicate()
        if result:
            return result
        time.sleep(0.005)
    raise AssertionError("byte budget ownership condition timed out")


def held_reader(server, path):
    connection = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        connection.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
        connection.settimeout(3)
        connection.connect(("127.0.0.1", server.port))
        connection.sendall(
            f"GET {path} HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n".encode()
        )
        prefix = bytearray()
        while b"\r\n\r\n" not in prefix:
            part = connection.recv(1024)
            if not part:
                raise AssertionError("large response closed before its headers")
            prefix.extend(part)
            if len(prefix) > 32_768:
                raise AssertionError(
                    "large response header block is unexpectedly large"
                )
        if not prefix.startswith(b"HTTP/1.1 200 "):
            raise AssertionError("held large response did not start with HTTP200")
        return connection
    except BaseException:
        connection.close()
        raise


def read_closed_response(connection):
    wire = bytearray()
    while part := connection.recv(4096):
        wire.extend(part)
        if len(wire) > 65_536:
            raise AssertionError("small native error response exceeds the test guard")
    return bytes(wire)


@unittest.skipUnless(
    importlib.util.find_spec("bessemer._native"),
    "build the byte-budget native wheel first",
)
class ByteBudgetHTTPTests(unittest.TestCase):
    def request_server(self):
        return Server(
            application=APPLICATION,
            max_body_size=1024,
            extra_args=(
                "--max-in-flight",
                "8",
                "--max-request-bytes-in-flight",
                "1024",
            ),
        )

    def test_incomplete_body_deadline_closes_without_waiting_for_more_upload(self):
        server = Server(
            application=APPLICATION,
            max_body_size=1024,
            extra_args=("--request-timeout", "0.05"),
        )
        try:
            with socket.create_connection(
                ("127.0.0.1", server.port), timeout=3
            ) as partial:
                partial.sendall(
                    b"POST /ingest HTTP/1.1\r\nHost: localhost\r\nContent-Length: 10\r\n\r\nabc"
                )
                wire = read_closed_response(partial)
                self.assertTrue(wire.startswith(b"HTTP/1.1 504 "))
                self.assertIn(b"connection: close", wire.lower())
                self.assertEqual(wire.count(b"HTTP/1.1 "), 1)
            self.assertEqual(request(server, "GET", "/healthz")[0], 200)
            self.assertEqual(request(server, "GET", "/tiny")[0], 200)
        finally:
            server.close()

    def test_admission_refusal_closes_before_reading_declared_upload(self):
        server = Server(application=APPLICATION, max_body_size=1024)
        try:
            with ThreadPoolExecutor(max_workers=1) as pool:
                held = pool.submit(request, server, "POST", "/held", b"x" * 768)
                wait_until(lambda: (server.root / "handler_entered").exists())
                self.assertEqual(held.result(timeout=2)[0], 504)
            wait_until(lambda: (server.root / "cleanup_started").exists())
            with socket.create_connection(
                ("127.0.0.1", server.port), timeout=3
            ) as refused:
                refused.sendall(
                    b"POST /ingest HTTP/1.1\r\nHost: localhost\r\nContent-Length: 300\r\n\r\n"
                )
                wire = read_closed_response(refused)
                self.assertTrue(wire.startswith(b"HTTP/1.1 503 "))
                self.assertIn(b"retry-after: 1", wire.lower())
                self.assertIn(b"connection: close", wire.lower())
            self.assertEqual(request(server, "GET", "/healthz")[0], 200)
        finally:
            (server.root / "release_cleanup").touch()
            server.close()

    def test_consumed_body_handler_deadline_preserves_keepalive(self):
        server = self.request_server()
        connection = http.client.HTTPConnection("127.0.0.1", server.port, timeout=3)
        try:
            connection.request("POST", "/held", b"x" * 768)
            response = connection.getresponse()
            first_socket = connection.sock
            self.assertEqual(response.status, 504)
            self.assertNotEqual(response.getheader("Connection"), "close")
            response.read()
            self.assertIsNotNone(first_socket)
            self.assertIs(connection.sock, first_socket)
            connection.request("GET", "/healthz")
            response = connection.getresponse()
            self.assertEqual(
                (response.status, json.loads(response.read())), (200, {"ok": True})
            )
            self.assertIs(connection.sock, first_socket)
        finally:
            connection.close()
            (server.root / "release_cleanup").touch()
            server.close()

    def test_partial_uploads_reserve_actual_chunks_and_refusal_closes_connection(self):
        server = self.request_server()
        upload = socket.create_connection(("127.0.0.1", server.port), timeout=3)
        try:
            upload.sendall(
                b"POST /ingest HTTP/1.1\r\nHost: localhost\r\nTransfer-Encoding: chunked\r\nConnection: close\r\n\r\n"
                + b"300\r\n"
                + b"x" * 768
                + b"\r\n"
            )
            refused = wait_until(
                lambda: (
                    value
                    if (value := request(server, "POST", "/ingest", b"y" * 300))[0]
                    == 503
                    else None
                )
            )
            self.assertEqual(refused[1].get("retry-after"), "1")
            self.assertEqual(refused[1].get("connection"), "close")
            with socket.create_connection(
                ("127.0.0.1", server.port), timeout=3
            ) as rejected:
                rejected.sendall(
                    b"POST /ingest HTTP/1.1\r\nHost: localhost\r\nContent-Length: 300\r\n\r\n"
                    + b"y" * 300
                    + b"GET /healthz HTTP/1.1\r\nHost: localhost\r\n\r\n"
                )
                wire = bytearray()
                while part := rejected.recv(4096):
                    wire.extend(part)
                    self.assertLess(len(wire), 65_536)
                self.assertTrue(wire.startswith(b"HTTP/1.1 503 "))
                self.assertIn(b"connection: close", wire.lower())
                self.assertEqual(wire.count(b"HTTP/1.1 "), 1)
            self.assertEqual(request(server, "GET", "/tiny")[0], 200)
            self.assertEqual(request(server, "GET", "/healthz")[0], 200)
            upload.sendall(b"0\r\n\r\n")
            response = http.client.HTTPResponse(upload)
            response.begin()
            self.assertEqual(
                (response.status, json.loads(response.read())), (200, {"size": 768})
            )
            recovered = wait_until(
                lambda: (
                    value
                    if (value := request(server, "POST", "/ingest", b"y" * 300))[0]
                    == 200
                    else None
                )
            )
            self.assertEqual(json.loads(recovered[2]), {"size": 300})
        finally:
            upload.close()
            server.close()

    def test_timeout_cleanup_keeps_request_byte_reservation_until_actual_completion(
        self,
    ):
        server = self.request_server()
        try:
            with ThreadPoolExecutor(max_workers=1) as pool:
                pending = pool.submit(request, server, "POST", "/held", b"x" * 768)
                wait_until(lambda: (server.root / "handler_entered").exists())
                self.assertEqual(pending.result(timeout=2)[0], 504)
            wait_until(lambda: (server.root / "cleanup_started").exists())
            self.assertFalse((server.root / "cleanup_finished").exists())
            status, headers, _ = request(server, "POST", "/ingest", b"y" * 300)
            self.assertEqual(status, 503)
            self.assertEqual(headers.get("retry-after"), "1")
            self.assertEqual(request(server, "GET", "/tiny")[0], 200)
            self.assertEqual(request(server, "GET", "/healthz")[0], 200)
            (server.root / "release_cleanup").touch()
            wait_until(lambda: (server.root / "cleanup_finished").exists())
            wait_until(lambda: request(server, "POST", "/ingest", b"y" * 300)[0] == 200)
        finally:
            (server.root / "release_cleanup").touch()
            server.close()

    def test_slow_reader_holds_output_bytes_after_python_capacity_is_free(self):
        server = Server(
            application=APPLICATION,
            max_body_size=1024,
            extra_args=(
                "--max-response-size",
                str(BIG_RESPONSE),
                "--max-response-bytes-in-flight",
                str(OUTPUT_BUDGET),
            ),
        )
        held = None
        try:
            held = held_reader(server, "/large")
            self.assertEqual(request(server, "GET", "/tiny")[0], 200)
            reusable = http.client.HTTPConnection("127.0.0.1", server.port, timeout=3)
            try:
                reusable.request("GET", "/large")
                refused = reusable.getresponse()
                first_socket = reusable.sock
                self.assertEqual(refused.status, 503)
                self.assertEqual(refused.getheader("Retry-After"), "1")
                self.assertNotEqual(refused.getheader("Connection"), "close")
                refused.read()
                self.assertIsNotNone(first_socket)
                reusable.request("GET", "/tiny")
                tiny = reusable.getresponse()
                self.assertEqual(
                    (tiny.status, json.loads(tiny.read())), (200, {"ok": True})
                )
                self.assertIs(reusable.sock, first_socket)
            finally:
                reusable.close()
            self.assertEqual(request(server, "GET", "/healthz")[0], 200)
            self.assertEqual(request(server, "GET", "/tiny")[0], 200)
            head = request(server, "HEAD", "/large")
            self.assertEqual((head[0], head[2]), (200, b""))
            held.close()
            held = None
            response = wait_until(
                lambda: (
                    value
                    if (value := request(server, "GET", "/large"))[0] == 200
                    else None
                )
            )
            self.assertEqual(len(response[2]), BIG_RESPONSE)
            self.assertEqual(response[2], b"x" * BIG_RESPONSE)
            self.assertEqual(request(server, "GET", "/tiny")[0], 200)
        finally:
            if held is not None:
                held.close()
            server.close()

    def test_native_static_schema_is_charged_while_shared_body_is_in_transport(self):
        server = Server(
            application=STATIC_APPLICATION,
            max_body_size=1024,
            extra_args=(
                "--max-response-size",
                str(STATIC_RESPONSE_LIMIT),
                "--max-response-bytes-in-flight",
                str(STATIC_RESPONSE_LIMIT + 1024),
            ),
        )
        held = None
        try:
            held = held_reader(server, "/openapi.json")
            self.assertEqual(request(server, "GET", "/tiny")[0], 200)
            self.assertEqual(request(server, "GET", "/openapi.json")[0], 503)
            self.assertEqual(request(server, "GET", "/healthz")[0], 200)
            head = request(server, "HEAD", "/openapi.json")
            self.assertEqual((head[0], head[2]), (200, b""))
            held.close()
            held = None
            recovered = wait_until(
                lambda: (
                    value
                    if (value := request(server, "GET", "/openapi.json"))[0] == 200
                    else None
                )
            )
            document = json.loads(recovered[2])
            self.assertEqual(document["info"]["description"], "d" * BIG_RESPONSE)
        finally:
            if held is not None:
                held.close()
            server.close()


if __name__ == "__main__":
    unittest.main()
