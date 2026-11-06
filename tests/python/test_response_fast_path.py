"""Raw response bytes and byte-budget accounting for the settled-JSON fast path.

The HTTP/1 writer writes a small JSON result straight to the socket when no
middleware, compression or CORS can change it. The bytes must be exactly those
of the general path, which the golden file records (captured on the commit
before the fast path existed; the date and generated request id are masked).
"""

from __future__ import annotations

import http.client
import json
import re
import socket
import unittest
from pathlib import Path

from test_native_integration import Server

GOLDEN = json.loads(
    (Path(__file__).parent.parent / "fixtures" / "response_golden.json").read_text()
)

APPLICATION = """
from bessemer import App, Compression, JSONResponse

app = App(compression=COMPRESSION)
app.health('/healthz')

@app.get('/json')
async def json_route():
    return {"ok": True, "count": 42}

@app.get('/items/{item_id}')
async def item(item_id: int, q: str = "x"):
    return {"id": item_id, "q": q}

@app.post('/echo')
async def echo(body: dict):
    return body

@app.get('/empty', status_code=204)
async def empty():
    return None

@app.get('/created', status_code=201)
async def created():
    return {"id": 1}

@app.get('/hdr')
async def hdr():
    return JSONResponse({"ok": True}, headers={"x-custom": "1"})

@app.get('/big')
async def big():
    return {"d": "x" * 2000}
"""

CASES = {
    "json": b"GET /json HTTP/1.1\r\nhost: a\r\n\r\n",
    "items": b"GET /items/7?q=abc HTTP/1.1\r\nhost: a\r\n\r\n",
    "echo": b"POST /echo HTTP/1.1\r\nhost: a\r\ncontent-type: application/json\r\n"
    b'content-length: 17\r\n\r\n{"a":1,"b":[2,3]}',
    "head": b"HEAD /json HTTP/1.1\r\nhost: a\r\n\r\n",
    "empty": b"GET /empty HTTP/1.1\r\nhost: a\r\n\r\n",
    "created": b"GET /created HTTP/1.1\r\nhost: a\r\n\r\n",
    "hdr": b"GET /hdr HTTP/1.1\r\nhost: a\r\n\r\n",
    "big": b"GET /big HTTP/1.1\r\nhost: a\r\n\r\n",
    "close": b"GET /json HTTP/1.1\r\nhost: a\r\nconnection: close\r\n\r\n",
    "http10": b"GET /json HTTP/1.0\r\n\r\n",
    "http10ka": b"GET /json HTTP/1.0\r\nconnection: keep-alive\r\n\r\n",
    "reqid": b"GET /json HTTP/1.1\r\nhost: a\r\nx-request-id: abc-1\r\n\r\n",
    "pipelined": b"GET /json HTTP/1.1\r\nhost: a\r\n\r\n"
    b"GET /items/1 HTTP/1.1\r\nhost: a\r\nconnection: close\r\n\r\n",
    "gzip": b"GET /big HTTP/1.1\r\nhost: a\r\naccept-encoding: gzip\r\n\r\n",
    "gzipjson": b"GET /json HTTP/1.1\r\nhost: a\r\naccept-encoding: gzip\r\n\r\n",
}


def exchange(port, data):
    with socket.create_connection(("127.0.0.1", port), timeout=5) as connection:
        connection.sendall(data)
        connection.settimeout(1)
        received = b""
        try:
            while chunk := connection.recv(65536):
                received += chunk
        except socket.timeout:
            pass
    received = re.sub(rb"date: [^\r]*\r\n", b"date: DATE\r\n", received)
    return received.decode("latin1")


def masked(name, text):
    if name == "reqid":
        return text
    return re.sub(r"x-request-id: [^\r]*\r\n", "x-request-id: RID\r\n", text)


class GoldenBytesTests(unittest.TestCase):
    def check(self, prefix, compression):
        server = Server(
            application=APPLICATION.replace("COMPRESSION", compression),
            max_body_size=1024,
        )
        try:
            for name, data in CASES.items():
                with self.subTest(case=f"{prefix}:{name}"):
                    self.assertEqual(
                        masked(name, exchange(server.port, data)),
                        GOLDEN[f"{prefix}:{name}"],
                    )
        finally:
            server.stop()

    def test_plain_server_writes_the_recorded_bytes(self):
        self.check("p", "False")

    def test_compressing_server_keeps_the_general_path(self):
        self.check("c", "Compression(min_size=512)")


BUDGET_APPLICATION = """
from bessemer import App

app = App()
app.health('/healthz')

@app.get('/tiny')
async def tiny():
    return {"ok": True}

@app.get('/wide')
async def wide():
    return {"d": "x" * 7000}
"""


class ResponseBudgetTests(unittest.TestCase):
    def test_response_bytes_in_flight_return_to_zero(self):
        server = Server(
            application=BUDGET_APPLICATION,
            extra_args=(
                "--max-response-size",
                "8192",
                "--max-response-bytes-in-flight",
                "8192",
            ),
        )
        try:
            connection = http.client.HTTPConnection("127.0.0.1", server.port, timeout=5)
            try:
                for _ in range(10_000):
                    connection.request("GET", "/tiny")
                    response = connection.getresponse()
                    self.assertEqual(response.status, 200)
                    response.read()
                # A leak of even one response's bytes per request would have
                # exhausted the 8192-byte budget long ago; the whole budget is
                # still available to a response nearly that size.
                connection.request("GET", "/wide")
                response = connection.getresponse()
                self.assertEqual(len(response.read()), 7008)
                self.assertEqual(response.status, 200)
                connection.request("GET", "/tiny")
                response = connection.getresponse()
                self.assertEqual(json.loads(response.read()), {"ok": True})
            finally:
                connection.close()
        finally:
            server.stop()


if __name__ == "__main__":
    unittest.main()
