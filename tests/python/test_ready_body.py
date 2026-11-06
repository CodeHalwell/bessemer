"""Request bodies that arrive with their head, over real sockets.

A small body already in the connection's buffer is handed to the route without
the streaming machinery, and a JSON body bound to a plan-backed model is
parsed once on the loop. Status codes, bodies and their order of precedence
(413, 415, then 422 for invalid JSON, then the field errors) are the same as
for a body that is streamed in.
"""

import importlib.util
import json
import socket
import sys
import time
import unittest

from test_native_integration import Server

NATIVE = importlib.util.find_spec("bessemer._native") is not None
PYDANTIC = importlib.util.find_spec("pydantic") is not None

APPLICATION = """
import hashlib
from pydantic import BaseModel
from bessemer import App, Request

app = App()
app.health('/healthz')

class Item(BaseModel):
    id: int
    name: str

class Order(BaseModel):
    items: list[Item]

@app.post('/echo')
async def echo(order: Order) -> Order:
    return order

@app.post('/echo-query')
async def echo_query(order: Order, q: str) -> Order:
    return order

@app.post('/raw')
async def raw(request: Request):
    body = await request.body()
    return {'size': len(body), 'sha': hashlib.sha256(body).hexdigest()}

@app.post('/ignore')
async def ignore():
    return {'ok': True}
"""

ORDER = json.dumps({"items": [{"id": 1, "name": "a"}, {"id": 2, "name": "b"}]}).encode()
INVALID_422 = (
    b'{"detail":[{"input":null,"loc":["body"],"msg":"Invalid JSON",'
    b'"type":"json_invalid"}]}'
)


def post(path, body, content_type="application/json", extra=b""):
    return (
        b"POST "
        + path
        + b" HTTP/1.1\r\nHost: x\r\nContent-Type: "
        + content_type.encode()
        + b"\r\nContent-Length: "
        + str(len(body)).encode()
        + b"\r\n"
        + extra
        + b"\r\n"
        + body
    )


def read_responses(connection, count):
    """Read `count` responses; returns [(status, headers, body)]."""
    data = b""
    responses = []
    deadline = time.monotonic() + 5
    while len(responses) < count and time.monotonic() < deadline:
        while b"\r\n\r\n" in data:
            head, _, rest = data.partition(b"\r\n\r\n")
            lines = head.split(b"\r\n")
            status = int(lines[0].split()[1])
            headers = {
                key.lower(): value.strip()
                for key, _, value in (line.partition(b":") for line in lines[1:])
            }
            if status == 100:
                responses.append((100, headers, b""))
                data = rest
                continue
            length = int(headers.get(b"content-length", b"0"))
            if len(rest) < length:
                break
            responses.append((status, headers, rest[:length]))
            data = rest[length:]
            if len(responses) >= count:
                break
        if len(responses) >= count:
            break
        try:
            chunk = connection.recv(65536)
        except TimeoutError:
            break
        if not chunk:
            break
        data += chunk
    return responses


@unittest.skipUnless(NATIVE and PYDANTIC, "needs the native extension and pydantic")
class ReadyBodyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = Server(application=APPLICATION, max_body_size=4096)

    @classmethod
    def tearDownClass(cls):
        cls.server.close()

    def connect(self):
        connection = socket.create_connection(
            ("127.0.0.1", self.server.port), timeout=3
        )
        self.addCleanup(connection.close)
        return connection

    def exchange(self, *writes, count=1):
        connection = self.connect()
        for write in writes:
            connection.sendall(write)
            time.sleep(0.05)
        return read_responses(connection, count)

    def test_valid_json_is_echoed(self):
        [(status, headers, body)] = self.exchange(post(b"/echo", ORDER))
        self.assertEqual(status, 200)
        self.assertEqual(headers[b"content-type"], b"application/json")
        self.assertEqual(json.loads(body), json.loads(ORDER))

    def test_invalid_json_is_the_same_422_on_every_route(self):
        bodies = [
            ORDER[:-3],
            ORDER + b" x",
            ORDER + b"{}",
            b'{"items": [{"id": 1, "name": "\xff"}]}',
            b'{"items": [{"id": "x", "name": "a"}], "oops": ',
            b'{"items": [{"id": 1.5, "name": "a"}]} ]',
            b"{",
        ]
        for body in bodies:
            with self.subTest(body=body):
                [(status, _, answer)] = self.exchange(post(b"/echo", body))
                self.assertEqual((status, answer), (422, INVALID_422))
                # With another parameter the old order applies: the body is
                # checked first, so the missing query still reports invalid JSON.
                [(status, _, answer)] = self.exchange(post(b"/echo-query", body))
                self.assertEqual((status, answer), (422, INVALID_422))

    def test_query_error_follows_valid_json(self):
        [(status, _, answer)] = self.exchange(post(b"/echo-query", ORDER))
        self.assertEqual(status, 422)
        self.assertEqual(json.loads(answer)["detail"][0]["loc"], ["query", "q"])

    def test_valid_json_with_wrong_types_reports_the_fields(self):
        body = b'{"items": [{"id": "x", "name": "a"}]}'
        [(status, _, answer)] = self.exchange(post(b"/echo", body))
        self.assertEqual(status, 422)
        self.assertEqual(json.loads(answer)["detail"][0]["type"], "int_parsing")

    def test_content_type_matching(self):
        for content_type in (
            "Application/JSON; charset=utf-8",
            "APPLICATION/JSON",
            "application/vnd.api+json",
        ):
            with self.subTest(content_type=content_type):
                [(status, _, _)] = self.exchange(post(b"/echo", ORDER, content_type))
                self.assertEqual(status, 200)
        for content_type in (
            "text/plain",
            "application/jsonx",
            "application/json; charset=latin-1",
        ):
            with self.subTest(content_type=content_type):
                [(status, _, _)] = self.exchange(post(b"/echo", ORDER, content_type))
                self.assertEqual(status, 415)

    def test_unsupported_media_type_precedes_invalid_json(self):
        [(status, _, _)] = self.exchange(post(b"/echo", b"{", "text/plain"))
        self.assertEqual(status, 415)

    def test_pipelined_posts_in_one_write(self):
        first = post(b"/echo", ORDER)
        second = post(b"/raw", b"hello")
        third = post(b"/echo", ORDER[:-1])
        responses = self.exchange(first + second + third, count=3)
        self.assertEqual([status for status, *_ in responses], [200, 200, 422])
        self.assertEqual(json.loads(responses[1][2])["size"], 5)

    def test_body_split_across_writes(self):
        request = post(b"/echo", ORDER)
        head = request.index(b"\r\n\r\n") + 4
        [(status, _, body)] = self.exchange(request[: head + 10], request[head + 10 :])
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), json.loads(ORDER))
        # Head alone, then the whole body.
        [(status, _, _)] = self.exchange(request[:head], request[head:])
        self.assertEqual(status, 200)

    def test_request_body_bytes_are_unchanged(self):
        import hashlib

        for size in (1, 100, 3000):
            body = bytes(range(256)) * (size // 256 + 1)
            body = body[:size]
            [(status, _, answer)] = self.exchange(
                post(b"/raw", body, "application/octet-stream")
            )
            self.assertEqual(status, 200)
            self.assertEqual(
                json.loads(answer),
                {"size": size, "sha": hashlib.sha256(body).hexdigest()},
            )

    def test_content_length_over_the_limit_is_413(self):
        [(status, *_)] = self.exchange(post(b"/echo", b"x" * 5000))
        self.assertEqual(status, 413)
        [(status, *_)] = self.exchange(post(b"/raw", b"x" * 5000))
        self.assertEqual(status, 413)

    def test_expect_continue_with_an_empty_buffer_sends_100(self):
        request = post(b"/echo", ORDER, extra=b"Expect: 100-continue\r\n")
        head = request.index(b"\r\n\r\n") + 4
        connection = self.connect()
        connection.sendall(request[:head])
        [(status, *_)] = read_responses(connection, 1)
        self.assertEqual(status, 100)
        connection.sendall(request[head:])
        [(status, _, body)] = read_responses(connection, 1)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), json.loads(ORDER))

    def test_connection_is_reused_after_a_ready_body(self):
        connection = self.connect()
        for _ in range(3):
            connection.sendall(post(b"/echo", ORDER))
            [(status, headers, _)] = read_responses(connection, 1)
            self.assertEqual(status, 200)
            self.assertNotEqual(headers.get(b"connection"), b"close")


if __name__ == "__main__":
    sys.exit(unittest.main())
