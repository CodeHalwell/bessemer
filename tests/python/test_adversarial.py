"""Independent malformed-protocol and hostile-value regression tests."""

import http.client
import importlib.util
import json
import random
import socket
import time
import unittest

import test_native_integration


def assert_closed(connection, follow_up=b""):
    """Assert that the server closed the connection.

    A close with unread request bytes resets the connection on Windows
    instead of ending it cleanly, which is closure all the same.
    """
    try:
        if follow_up:
            connection.sendall(follow_up)
        data = connection.recv(1)
    except (ConnectionResetError, ConnectionAbortedError):
        return
    if data != b"":
        raise AssertionError(f"connection stayed open and sent {data!r}")


ADVERSARIAL_ROUTES = """
from contextvars import ContextVar
from bessemer import Cookie, Depends, Header, Request

request_resource = ContextVar("adversarial_resource", default=None)

async def async_resource(request: Request):
    token = request_resource.set(request.request_id)
    try:
        yield request_resource.get()
    finally:
        await asyncio.sleep(0.12)
        request_resource.reset(token)
        Path("async-resource-closed").touch()

def sync_resource(request: Request):
    token = request_resource.set(request.request_id)
    try:
        yield request_resource.get()
    finally:
        time.sleep(0.12)
        request_resource.reset(token)
        Path("sync-resource-closed").touch()

async def teardown_resource(request: Request):
    token = request_resource.set(request.request_id)
    try:
        yield request_resource.get()
    finally:
        # The deadline passes during this cleanup, after the handler returned.
        await asyncio.sleep(0.12)
        request_resource.reset(token)
        Path("teardown-resource-closed").touch()

@app.get("/adversarial/resource/teardown", timeout=0.05)
async def teardown_resource_route(resource=Depends(teardown_resource)):
    return {"resource": resource}

@app.get("/adversarial/resource/async", timeout=0.05)
async def async_resource_route(resource=Depends(async_resource)):
    await asyncio.sleep(10)

@app.get("/adversarial/resource/sync", timeout=0.05)
async def sync_resource_route(resource=Depends(sync_resource)):
    await asyncio.sleep(10)

@app.get("/adversarial/cookies")
async def adversarial_cookies(request: Request, session: str = Cookie()):
    return {"bound": session, "request": request.cookies.get("session")}

@app.get("/adversarial/opaque-header")
async def adversarial_opaque_header(request: Request, value: str = Header(alias="X-Value")):
    return {"bound": value, "request": request.headers["x-value"],
            "all": request.headers.getlist("x-value")}

@app.get("/adversarial/integer-header")
async def adversarial_integer_header(value: int = Header(alias="X-Value")):
    return {"value": value}

@app.get("/adversarial/budget/{kind}/{count}")
async def adversarial_budget(kind: str, count: int):
    from bessemer import Response
    count = max(0, min(count, 1024))
    if kind == "bytes":
        return Response(b"x" * count)
    if kind == "strings":
        return [""] * count
    if kind == "quotes":
        return chr(34) * count
    if kind == "float":
        return {"payload": 1.192092896e-7, "padding": "x" * count}
    return "x" * count

@app.get("/adversarial/return/{case}")
async def adversarial_return(case: str):
    if case == "cycle":
        value = []
        value.append(value)
        return value
    if case == "deep":
        value = None
        for _ in range(70):
            value = [value]
        return value
    if case == "integer":
        return 2 ** 100
    if case == "key":
        return {1: "not a JSON object key"}
    if case == "infinity":
        return float("inf")
    if case == "surrogate":
        return chr(0xD800)
    if case == "integer-bound":
        return 10 ** 4096
    return {"valid": True}

@app.post("/adversarial/slow-body", timeout=0.08)
async def adversarial_slow_body(item: dict):
    return item

@app.post("/adversarial/raw-json")
async def adversarial_raw_json(request: Request):
    return await request.json()

@app.get("/adversarial/response/{case}")
async def adversarial_response(case: str):
    from bessemer import Response
    if case == "reset":
        return Response(b"must be discarded", status_code=205)
    if case == "cookies":
        response = Response(b"cookies")
        response.set_cookie("first", "one", secure=True, httponly=True)
        response.set_cookie("second", "two", secure=True, httponly=True)
        return response
    class InvalidEnvelope(Response):
        def to_native(self, *, max_bytes=None):
            if case == "framing":
                headers = [("Content-Length", "999")]
            elif case == "injection":
                headers = [("X-Test", "value\\r\\nInjected: value")]
            else:
                headers = [("X-Test", "value")] * 65
            return 200, b"must be rejected", headers, "bytes"
    return InvalidEnvelope()

try:
    from pydantic import BaseModel, field_validator
except ImportError:
    pass
else:
    class SensitiveItem(BaseModel):
        secret: str = ""
        entries: list[int] = []

        @field_validator("secret")
        @classmethod
        def reject_secret(cls, value):
            raise ValueError("secret=" + value)

    @app.post("/adversarial/sensitive")
    async def adversarial_sensitive(item: SensitiveItem):
        return {"valid": True}
"""

API_KEY = "adversarial-test-api-key"
METRICS_TOKEN = "adversarial-test-metrics-token"
CONTROL_ENVIRONMENT = {
    "BESSEMER_ADVERSARIAL_KEY": API_KEY,
    "BESSEMER_ADVERSARIAL_METRICS": METRICS_TOKEN,
}


def controls_application(configuration):
    return f"""
from bessemer import App, Request
app = App(**{configuration!r})
app.health("/healthz")

@app.get("/client")
async def client(request: Request):
    return {{"client": request.client, "request_id": request.request_id}}
"""


def request_with_headers(server, path, headers=(), method="GET"):
    request = (
        f"{method} {path} HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n"
        + "".join(f"{name}: {value}\r\n" for name, value in headers)
        + "\r\n"
    ).encode("ascii")
    with socket.create_connection(("127.0.0.1", server.port), timeout=3) as connection:
        connection.sendall(request)
        response = http.client.HTTPResponse(connection)
        response.begin()
        return response.status, dict(response.getheaders()), response.read()


@unittest.skipUnless(
    importlib.util.find_spec("bessemer._native"), "build the native extension first"
)
class AdversarialHTTPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = test_native_integration.Server(
            max_body_size=16384,
            application=test_native_integration.APPLICATION + ADVERSARIAL_ROUTES,
        )

    @classmethod
    def tearDownClass(cls):
        cls.server.close()

    def raw_response(self, request):
        with socket.create_connection(
            ("127.0.0.1", self.server.port), timeout=3
        ) as connection:
            connection.sendall(request)
            response = http.client.HTTPResponse(connection)
            response.begin()
            return response.status, dict(response.getheaders()), response.read()

    def assert_server_recovers(self):
        self.assertEqual(self.server.request("GET", "/healthz")[0], 200)
        self.assertEqual(self.server.request("GET", "/items/1")[0], 200)

    def test_parser_rejects_ambiguous_content_lengths(self):
        for lengths in ((b"2", b"3"), (b"2", b"-2"), (b"2", b"x")):
            with self.subTest(lengths=lengths):
                request = (
                    b"POST /items HTTP/1.1\r\nHost: localhost\r\n"
                    b"Content-Type: application/json\r\nContent-Length: "
                    + lengths[0]
                    + b"\r\nContent-Length: "
                    + lengths[1]
                    + b"\r\nConnection: close\r\n\r\n{}"
                )
                status, headers, _ = self.raw_response(request)
                self.assertEqual(status, 400)
                self.assertEqual(headers.get("connection"), "close")
        self.assert_server_recovers()

    def test_transfer_encoding_precedence_forces_connection_close(self):
        with socket.create_connection(
            ("127.0.0.1", self.server.port), timeout=3
        ) as connection:
            connection.sendall(
                b"POST /items HTTP/1.1\r\nHost: localhost\r\n"
                b"Content-Type: application/json\r\nContent-Length: 50\r\n"
                b"Transfer-Encoding: chunked\r\n\r\n2\r\n{}\r\n0\r\n\r\n"
            )
            response = http.client.HTTPResponse(connection)
            response.begin()
            # A strict implementation may reject TE+CL; Hyper may normalize it
            # using Transfer-Encoding. RFC 9112 requires connection closure if
            # processing it, preventing reuse with competing framing rules.
            self.assertIn(response.status, (201, 400))
            self.assertEqual(response.getheader("Connection"), "close")
            response.read()
            assert_closed(
                connection, b"GET /healthz HTTP/1.1\r\nHost: localhost\r\n\r\n"
            )
        self.assert_server_recovers()

    def test_unsupported_transfer_coding_chains_are_rejected_before_dispatch(self):
        cases = (
            b"Transfer-Encoding: gzip, chunked\r\n",
            b"Transfer-Encoding: chunked, chunked\r\n",
            b"Transfer-Encoding: gzip\r\nTransfer-Encoding: chunked\r\n",
            b"Transfer-Encoding: chunked\r\nTransfer-Encoding: chunked\r\n",
        )
        for encoding in cases:
            with (
                self.subTest(encoding=encoding),
                socket.create_connection(
                    ("127.0.0.1", self.server.port), timeout=3
                ) as connection,
            ):
                connection.sendall(
                    b"POST /items HTTP/1.1\r\nHost: localhost\r\n"
                    b"Content-Type: application/json\r\n"
                    + encoding
                    + b"\r\n2\r\n{}\r\n0\r\n\r\n"
                    b"GET /healthz HTTP/1.1\r\nHost: localhost\r\n\r\n"
                )
                response = http.client.HTTPResponse(connection)
                response.begin()
                self.assertEqual(response.status, 400)
                response.read()
                assert_closed(connection)
        for encoding in (b"chunked", b"Chunked"):
            with self.subTest(valid_encoding=encoding):
                status, _, body = self.raw_response(
                    b"POST /items HTTP/1.1\r\nHost: localhost\r\n"
                    b"Content-Type: application/json\r\nTransfer-Encoding: "
                    + encoding
                    + b"\r\nConnection: close\r\n\r\n2\r\n{}\r\n0\r\n\r\n"
                )
                self.assertEqual((status, json.loads(body)), (201, {}))
        self.assert_server_recovers()

    def test_http11_host_is_single_and_unambiguous(self):
        cases = (
            (b"/healthz", b""),
            (b"/healthz", b"Host: localhost\r\nHost: other.example\r\n"),
            (b"/healthz", b"Host: localhost\r\nHost: localhost\r\n"),
            (b"http://other.example/healthz", b"Host: localhost\r\n"),
        )
        for target, headers in cases:
            with self.subTest(target=target, headers=headers):
                status, _, _ = self.raw_response(
                    b"GET "
                    + target
                    + b" HTTP/1.1\r\n"
                    + headers
                    + b"Connection: close\r\n\r\n"
                )
                self.assertEqual(status, 400)
        self.assert_server_recovers()

    def test_host_authority_syntax_is_validated(self):
        for host in (
            "",
            ":80",
            "localhost:abc",
            "localhost:99999",
            "localhost:",
            "[nonsense]",
            "[::1]:abc",
        ):
            with self.subTest(host=host):
                status, _, _ = self.raw_response(
                    f"GET /healthz HTTP/1.1\r\nHost: {host}\r\nConnection: close\r\n\r\n".encode()
                )
                self.assertEqual(status, 400)
        for host in (
            "localhost",
            "localhost:8080",
            "example.test",
            "127.0.0.1:8080",
            "[::1]:8080",
        ):
            with self.subTest(host=host):
                status, _, _ = self.raw_response(
                    f"GET /healthz HTTP/1.1\r\nHost: {host}\r\nConnection: close\r\n\r\n".encode()
                )
                self.assertEqual(status, 200)

    def test_parser_rejects_invalid_header_syntax(self):
        cases = (
            b"Host : localhost\r\n",
            b"Host: localhost\r\nX-Test: first\r\n second\r\n",
            b"Host: localhost\r\nX-Test: first\x00second\r\n",
        )
        for headers in cases:
            with self.subTest(headers=headers):
                status, _, _ = self.raw_response(
                    b"GET /healthz HTTP/1.1\r\n"
                    + headers
                    + b"Connection: close\r\n\r\n"
                )
                self.assertEqual(status, 400)
        self.assert_server_recovers()

    def test_parser_bounds_header_count(self):
        status, _, _ = self.raw_response(
            b"GET /healthz HTTP/1.1\r\nHost: localhost\r\n"
            + b"X-Test: x\r\n" * 110
            + b"Connection: close\r\n\r\n"
        )
        self.assertEqual(status, 431)
        self.assert_server_recovers()

    def test_malformed_chunk_body_is_bad_request_and_closes_connection(self):
        with socket.create_connection(
            ("127.0.0.1", self.server.port), timeout=3
        ) as connection:
            connection.sendall(
                b"POST /items HTTP/1.1\r\nHost: localhost\r\n"
                b"Content-Type: application/json\r\nTransfer-Encoding: chunked\r\n"
                b"\r\nZ\r\n{}\r\n0\r\n\r\n"
            )
            response = http.client.HTTPResponse(connection)
            response.begin()
            self.assertEqual(response.status, 400)
            self.assertNotIn(b"Traceback", response.read())
            assert_closed(connection)
        self.assert_server_recovers()

    def test_invalid_json_request_does_not_desynchronize_keepalive(self):
        connection = http.client.HTTPConnection(
            "127.0.0.1", self.server.port, timeout=3
        )
        try:
            connection.request(
                "POST", "/items", b"{", {"Content-Type": "application/json"}
            )
            response = connection.getresponse()
            self.assertEqual(response.status, 422)
            response.read()
            connection.request(
                "POST",
                "/items",
                b'{"still":"valid"}',
                {"Content-Type": "application/json"},
            )
            response = connection.getresponse()
            self.assertEqual(response.status, 201)
            self.assertEqual(json.loads(response.read()), {"still": "valid"})
        finally:
            connection.close()

    def test_completed_responses_release_the_single_admission_slot(self):
        for index in range(1000):
            status, _, body = self.server.request("POST", "/items", b"{}")
            self.assertEqual(status, 201, f"sequential request {index}: {body!r}")
        self.assert_server_recovers()

    def test_json_content_type_is_single_and_syntactically_valid(self):
        body = b'{"valid":true}'
        for values in (
            ("application/json", "text/plain"),
            ("text/plain", "application/json"),
            ("application/json", "application/json"),
        ):
            with self.subTest(values=values):
                status, _, _ = self.raw_response(
                    b"POST /items HTTP/1.1\r\nHost: localhost\r\n"
                    + b"".join(
                        f"Content-Type: {value}\r\n".encode() for value in values
                    )
                    + f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode()
                    + body
                )
                self.assertEqual(status, 400)
        for value in (
            "application/not valid+json",
            "application/\tbad+json",
            "application/x?+json",
            "application/x, text/html+json",
            "application/json, application/x+json",
        ):
            with self.subTest(value=value):
                status, _, _ = self.raw_response(
                    b"POST /items HTTP/1.1\r\nHost: localhost\r\n"
                    + f"Content-Type: {value}\r\nContent-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode()
                    + body
                )
                self.assertIn(status, (400, 415))
        for value in (
            "application/json",
            "Application/JSON; charset=utf-8",
            "application/problem+json; charset=UTF-8",
        ):
            with self.subTest(value=value):
                status, _, result = self.raw_response(
                    b"POST /items HTTP/1.1\r\nHost: localhost\r\n"
                    + f"Content-Type: {value}\r\nContent-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode()
                    + body
                )
                self.assertEqual(status, 201)
                self.assertEqual(json.loads(result), {"valid": True})
        self.assert_server_recovers()

    def test_request_and_bound_string_headers_preserve_latin1_octets(self):
        for raw, expected in (
            (b"abc\x80\xff", "abc\u0080\u00ff"),
            (b"caf\xc3\xa9", "caf\u00c3\u00a9"),
        ):
            with self.subTest(raw=raw):
                status, _, body = self.raw_response(
                    b"GET /adversarial/opaque-header HTTP/1.1\r\nHost: localhost\r\n"
                    b"X-Value: " + raw + b"\r\nConnection: close\r\n\r\n"
                )
                self.assertEqual(status, 200)
                self.assertEqual(
                    json.loads(body),
                    {"bound": expected, "request": expected, "all": [expected]},
                )
                status, _, _ = self.raw_response(
                    b"GET /adversarial/integer-header HTTP/1.1\r\nHost: localhost\r\n"
                    b"X-Value: " + raw + b"\r\nConnection: close\r\n\r\n"
                )
                self.assertEqual(status, 422)
        self.assert_server_recovers()

    def test_body_read_deadline_releases_admission_capacity(self):
        with socket.create_connection(
            ("127.0.0.1", self.server.port), timeout=3
        ) as connection:
            connection.sendall(
                b"POST /adversarial/slow-body HTTP/1.1\r\nHost: localhost\r\n"
                b"Content-Type: application/json\r\nContent-Length: 100\r\n\r\n{"
            )
            began = time.monotonic()
            response = http.client.HTTPResponse(connection)
            response.begin()
            self.assertEqual(response.status, 504)
            response.read()
            self.assertLess(time.monotonic() - began, 1)
        self.assert_server_recovers()

    def test_invalid_handler_values_fail_safely_without_leaking_capacity(self):
        for case in ("cycle", "deep", "key", "infinity", "surrogate", "integer-bound"):
            with self.subTest(case=case):
                status, _, body = self.server.request(
                    "GET", f"/adversarial/return/{case}"
                )
                self.assertEqual(status, 500)
                self.assertEqual(json.loads(body), {"detail": "Internal Server Error"})
                self.assert_server_recovers()

    def test_large_json_integers_roundtrip_without_float_corruption(self):
        for value in (18446744073709551617, -18446744073709551617, 2**100):
            with self.subTest(value=value):
                status, _, result = self.server.request(
                    "POST", "/items", json.dumps({"amount": value}).encode()
                )
                self.assertEqual(status, 201)
                actual = json.loads(result)["amount"]
                self.assertIs(type(actual), int)
                self.assertEqual(actual, value)
        status, _, result = self.server.request("GET", "/adversarial/return/integer")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(result), 2**100)

    def test_json_integer_digit_bound_and_nonfinite_float_literals(self):
        for sign in (b"", b"-"):
            with self.subTest(sign=sign):
                permitted = sign + b"1" * 4096
                status, _, result = self.server.request(
                    "POST", "/items", b'{"amount":' + permitted + b"}"
                )
                self.assertEqual(status, 201)
                self.assertEqual(json.loads(result)["amount"], int(permitted))
                rejected = sign + b"1" * 4097
                self.assertEqual(
                    self.server.request(
                        "POST", "/items", b'{"amount":' + rejected + b"}"
                    )[0],
                    422,
                )
        for literal in (b"1e9999", b"-1e9999", b"NaN", b"Infinity", b"-Infinity"):
            with self.subTest(literal=literal):
                status, _, result = self.server.request(
                    "POST", "/items", b'{"amount":' + literal + b"}"
                )
                self.assertEqual(status, 422)
                self.assertNotIn(b"Traceback", result)
        self.assert_server_recovers()

    def test_large_scalar_integer_does_not_overflow(self):
        for value in (2**100, -(2**100)):
            with self.subTest(value=value):
                status, _, result = self.server.request("GET", f"/items/{value}")
                self.assertEqual(status, 200)
                self.assertEqual(json.loads(result)["id"], value)

    def test_internal_json_number_marker_remains_an_ordinary_object_key(self):
        marker = "$serde_json::private::Number"
        cases = (
            {marker: "123"},
            {marker: "not-a-number", "other": 7},
            {"nested": {marker: "18446744073709551617"}},
            {"nested": [{marker: "-1e9999"}, {marker: {"value": 3}}]},
        )
        for value in cases:
            with self.subTest(value=value):
                status, _, body = self.server.request(
                    "POST", "/items", json.dumps(value).encode()
                )
                self.assertEqual(status, 201)
                self.assertEqual(json.loads(body), value)
        escaped = b'{"\\u0024serde_json::private::Number":"123"}'
        status, _, body = self.server.request("POST", "/items", escaped)
        self.assertEqual(status, 201)
        self.assertEqual(json.loads(body), {marker: "123"})

    def test_seeded_json_roundtrips_preserve_values_and_object_keys(self):
        generator = random.Random(0xB355E)
        atoms = [
            None,
            True,
            False,
            0,
            -(2**100),
            2**100,
            1.25,
            -0.0,
            "",
            "line\nbreak",
            'quote"slash\\',
            "caf\u00e9\u2603",
        ]
        keys = [
            "",
            "value",
            "nested",
            "$serde_json::private::Number",
            "caf\u00e9",
            "line\nkey",
        ]

        def value(depth):
            if depth == 0 or generator.randrange(3) == 0:
                return generator.choice(atoms)
            if generator.randrange(2):
                return [value(depth - 1) for _ in range(generator.randrange(5))]
            return {
                key: value(depth - 1)
                for key in generator.sample(keys, generator.randrange(5))
            }

        for case in range(50):
            expected = {"payload": value(4)}
            encoded = json.dumps(expected, ensure_ascii=bool(case % 2)).encode()
            if case % 3 == 0:
                encoded = b" \r\n\t" + encoded + b"\t\r\n "
            with self.subTest(case=case):
                status, _, body = self.server.request("POST", "/items", encoded)
                self.assertEqual(status, 201)
                self.assertEqual(json.loads(body), expected)

    def test_json_depth_limit_is_enforced_at_the_boundary(self):
        for nested_arrays, expected_status in ((63, 201), (64, 422)):
            with self.subTest(nested_arrays=nested_arrays):
                body = (
                    b'{"value":'
                    + b"[" * nested_arrays
                    + b"0"
                    + b"]" * nested_arrays
                    + b"}"
                )
                status, _, result = self.server.request("POST", "/items", body)
                self.assertEqual(status, expected_status)
                if status == 201:
                    self.assertEqual(json.loads(result), json.loads(body))
        self.assert_server_recovers()

    def test_duplicate_keys_cannot_hide_invalid_json_literals_or_excess_depth(self):
        cases = (
            b'{"value":"\\ud800","value":1}',
            b'{"value":"\\udfff","value":1}',
            b'{"value":{"nested":"\\ud800"},"value":1}',
            b'{"\\u0076alue":"\\ud800","value":1}',
            b'{"value":1e9999,"value":1}',
            b'{"value":' + b"9" * 4097 + b',"value":1}',
            b'{"value":' + b"[" * 65 + b"0" + b"]" * 65 + b',"value":1}',
        )
        for body in cases:
            with self.subTest(body=body[:70]):
                self.assertEqual(self.server.request("POST", "/items", body)[0], 422)
        self.assert_server_recovers()

    def test_raw_request_json_matches_typed_body_parser(self):
        cases = (
            (b'{"value":"\\ud800","value":1}', False),
            (b'{"value":1e9999,"value":1}', False),
            (b'{"value":' + b"9" * 4097 + b',"value":1}', False),
            (b'{"value":' + b"[" * 64 + b"0" + b"]" * 64 + b"}", False),
            (b'{"value":' + b"[" * 63 + b"0" + b"]" * 63 + b"}", True),
            (b'{"value":"\\ud83d\\ude00","value":2}', True),
            (b'{"value":' + b"9" * 4096 + b"}", True),
            (b'{"value":"\xff"}', False),
            (b'{"$serde_json::private::Number":"ordinary","x":-0.0}', True),
        )
        for body, valid in cases:
            with self.subTest(body=body[:70]):
                responses = [
                    self.server.request("POST", path, body)
                    for path in ("/items", "/adversarial/raw-json")
                ]
                self.assertEqual(
                    [response[0] for response in responses],
                    [201, 200] if valid else [422, 422],
                )
                if valid:
                    self.assertEqual(
                        json.loads(responses[0][2]), json.loads(responses[1][2])
                    )
        self.assert_server_recovers()

    def test_native_envelope_rejects_framing_headers_and_injection(self):
        for case in ("framing", "injection", "too-many"):
            with self.subTest(case=case):
                status, _, body = self.server.request(
                    "GET", f"/adversarial/response/{case}"
                )
                self.assertEqual(status, 500)
                self.assertEqual(json.loads(body), {"detail": "Internal Server Error"})
                self.assert_server_recovers()

    def test_response205_has_no_body_and_keeps_next_response_framed(self):
        connection = http.client.HTTPConnection(
            "127.0.0.1", self.server.port, timeout=3
        )
        try:
            connection.request("GET", "/adversarial/response/reset")
            response = connection.getresponse()
            self.assertEqual((response.status, response.read()), (205, b""))
            connection.request("GET", "/healthz")
            response = connection.getresponse()
            self.assertEqual(response.status, 200)
            self.assertEqual(json.loads(response.read()), {"ok": True})
        finally:
            connection.close()

    def test_repeated_set_cookie_fields_are_preserved(self):
        connection = http.client.HTTPConnection(
            "127.0.0.1", self.server.port, timeout=3
        )
        try:
            connection.request("GET", "/adversarial/response/cookies")
            response = connection.getresponse()
            self.assertEqual(response.status, 200)
            cookies = [
                value
                for name, value in response.getheaders()
                if name.lower() == "set-cookie"
            ]
            self.assertEqual(len(cookies), 2)
            self.assertTrue(cookies[0].startswith("first=one;"))
            self.assertTrue(cookies[1].startswith("second=two;"))
            self.assertEqual(response.read(), b"cookies")
        finally:
            connection.close()

    def test_invalid_utf8_and_excessive_json_nesting_are_rejected(self):
        for body in (b'{"text":"\xff"}', b"[" * 130 + b"]" * 130):
            with self.subTest(body=body[:20]):
                status, _, result = self.server.request("POST", "/items", body)
                # The explicit small fixture body cap may reject the nested
                # value before the JSON parser; both paths must be safe.
                self.assertIn(status, (413, 422))
                self.assertNotIn(b"Traceback", result)
                self.assert_server_recovers()

    def test_cookie_binding_and_request_view_share_one_parser(self):
        for cookie in ('session="abc"', "session=abc"):
            with self.subTest(cookie=cookie):
                status, _, body = request_with_headers(
                    self.server, "/adversarial/cookies", [("Cookie", cookie)]
                )
                self.assertEqual(status, 200)
                self.assertEqual(json.loads(body), {"bound": "abc", "request": "abc"})
        # The first cookie with a name wins, in one header or across several.
        for cookies in (
            [("Cookie", "session=one; session=two")],
            [("Cookie", "session=one"), ("Cookie", "session=two")],
        ):
            with self.subTest(cookies=cookies):
                status, _, body = request_with_headers(
                    self.server, "/adversarial/cookies", cookies
                )
                self.assertEqual(status, 200)
                self.assertEqual(json.loads(body), {"bound": "one", "request": "one"})
        # A cookie that is not well formed is left out, never guessed at: the
        # required parameter is then missing, and nothing of it is bound.
        for cookie in ('session="a;b"', 'session="unterminated', 'session="a\\\\b"'):
            with self.subTest(cookie=cookie):
                status, _, body = request_with_headers(
                    self.server, "/adversarial/cookies", [("Cookie", cookie)]
                )
                self.assertEqual(status, 422)
                self.assertEqual(
                    json.loads(body)["detail"][0]["loc"], ["cookie", "session"]
                )
        # Cookies another application set on the domain cannot lock a user
        # out: the malformed ones are ignored and the good one still binds.
        status, _, body = request_with_headers(
            self.server,
            "/adversarial/cookies",
            [("Cookie", 'prefs={"theme":"dark"}; flag; other=a b; session=abc')],
        )
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), {"bound": "abc", "request": "abc"})
        self.assert_server_recovers()

    def test_timeout_holds_capacity_until_generator_context_cleanup_finishes(self):
        for kind in ("async", "sync", "teardown"):
            with self.subTest(kind=kind):
                marker = self.server.root / f"{kind}-resource-closed"
                self.assertEqual(
                    self.server.request("GET", f"/adversarial/resource/{kind}")[0], 504
                )
                self.assertEqual(self.server.request("GET", "/healthz")[0], 200)
                self.assertEqual(self.server.request("GET", "/items/1")[0], 503)
                deadline = time.monotonic() + 2
                while not marker.exists() and time.monotonic() < deadline:
                    time.sleep(0.005)
                self.assertTrue(
                    marker.exists(), f"{kind} ContextVar token cleanup did not complete"
                )
                self.assert_server_recovers()

    @unittest.skipUnless(importlib.util.find_spec("pydantic"), "requires pydantic")
    def test_model_validation_errors_do_not_echo_validator_secrets_or_grow_without_bound(
        self,
    ):
        secret = "do-not-echo-this-sensitive-value"
        status, _, body = self.server.request(
            "POST", "/adversarial/sensitive", json.dumps({"secret": secret}).encode()
        )
        self.assertEqual(status, 422)
        self.assertNotIn(secret.encode(), body)
        self.assertNotIn(b"secret=", body)
        status, _, body = self.server.request(
            "POST",
            "/adversarial/sensitive",
            json.dumps({"entries": ["invalid"] * 250}).encode(),
        )
        self.assertEqual(status, 422)
        self.assertLessEqual(len(json.loads(body)["detail"]), 100)
        self.assertLess(len(body), 16384)


@unittest.skipUnless(
    importlib.util.find_spec("bessemer._native"), "build the native extension first"
)
class AdversarialControlsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = test_native_integration.Server(
            application=controls_application(
                {
                    "cors": {
                        "origins": ["https://trusted.example"],
                        "allow_methods": ["GET"],
                        "allow_headers": ["X-API-Key"],
                        "allow_credentials": True,
                    },
                    "api_key": {
                        "env": "BESSEMER_ADVERSARIAL_KEY",
                        "exclude_paths": ["/healthz", "/metrics"],
                    },
                    "metrics_path": "/metrics",
                    "metrics_token_env": "BESSEMER_ADVERSARIAL_METRICS",
                    "trusted_proxies": ["127.0.0.0/8"],
                }
            ),
            environment=CONTROL_ENVIRONMENT,
        )

    @classmethod
    def tearDownClass(cls):
        cls.server.close()

    def test_duplicate_security_headers_are_rejected(self):
        for path, duplicates in (
            ("/client", [("X-API-Key", API_KEY), ("X-API-Key", API_KEY)]),
            (
                "/client",
                [
                    ("Origin", "https://trusted.example"),
                    ("Origin", "https://trusted.example"),
                ],
            ),
            (
                "/metrics",
                [("Authorization", f"Bearer {METRICS_TOKEN}")] * 2,
            ),
        ):
            with self.subTest(path=path, duplicates=[name for name, _ in duplicates]):
                status, _, body = request_with_headers(self.server, path, duplicates)
                self.assertEqual(status, 400)
                self.assertNotIn(API_KEY.encode(), body)
                self.assertNotIn(METRICS_TOKEN.encode(), body)
        self.assertEqual(self.server.request("GET", "/healthz")[0], 200)

    def test_preflight_does_not_allow_actual_unauthenticated_request(self):
        origin = [("Origin", "https://trusted.example")]
        preflight = [*origin, ("Access-Control-Request-Method", "GET")]
        self.assertEqual(
            request_with_headers(self.server, "/client", preflight, "OPTIONS")[0], 204
        )
        self.assertEqual(
            request_with_headers(self.server, "/client", preflight)[0], 401
        )
        self.assertEqual(request_with_headers(self.server, "/client", origin)[0], 401)
        for change in (
            [("Access-Control-Request-Method", "GET")],
            [*origin, ("Access-Control-Request-Method", "DELETE")],
            [*preflight, ("Access-Control-Request-Headers", "X-Not-Allowed")],
            [*preflight, ("Access-Control-Request-Headers", "X-API-Key,,")],
        ):
            with self.subTest(change=change):
                self.assertIn(
                    request_with_headers(self.server, "/client", change, "OPTIONS")[0],
                    (400, 403),
                )

    def test_disallowed_and_malformed_origins_cannot_match_allowed_prefix(self):
        for origin in (
            "https://trusted.example.evil.test",
            "https://trusted.example/path",
            "https://trusted.example@evil.test",
            "https://trusted.example, https://evil.test",
            "null",
        ):
            with self.subTest(origin=origin):
                self.assertEqual(
                    request_with_headers(
                        self.server,
                        "/client",
                        [("X-API-Key", API_KEY), ("Origin", origin)],
                    )[0],
                    403,
                )

    def test_metrics_exclusion_and_global_key_cannot_bypass_metrics_token(self):
        for headers in (
            [],
            [("X-API-Key", API_KEY)],
            [("Authorization", f"Bearer {METRICS_TOKEN}junk")],
        ):
            with self.subTest(header_names=[name for name, _ in headers]):
                self.assertEqual(
                    request_with_headers(self.server, "/metrics", headers)[0], 401
                )
        status, _, body = request_with_headers(
            self.server, "/metrics", [("Authorization", f"Bearer {METRICS_TOKEN}")]
        )
        self.assertEqual(status, 200)
        self.assertNotIn(API_KEY.encode(), body)
        self.assertNotIn(METRICS_TOKEN.encode(), body)

    def test_trusted_proxy_chain_stops_at_first_untrusted_hop(self):
        status, _, body = request_with_headers(
            self.server,
            "/client",
            [
                ("X-API-Key", API_KEY),
                ("X-Forwarded-For", "198.51.100.77, 203.0.113.8, 127.0.0.5"),
            ],
        )
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["client"], "203.0.113.8")
        for value in (
            "not-an-ip",
            "203.0.113.1,",
            ",203.0.113.1",
            ",".join(["203.0.113.1"] * 33),
        ):
            with self.subTest(value=value[:50]):
                self.assertEqual(
                    request_with_headers(
                        self.server,
                        "/client",
                        [("X-API-Key", API_KEY), ("X-Forwarded-For", value)],
                    )[0],
                    400,
                )

    def test_untrusted_peer_cannot_supply_client_address(self):
        with_server = test_native_integration.Server(
            application=controls_application({})
        )
        try:
            for value in ("203.0.113.8", "not-an-ip"):
                with self.subTest(value=value):
                    status, _, body = request_with_headers(
                        with_server, "/client", [("X-Forwarded-For", value)]
                    )
                    self.assertEqual(status, 200)
                    self.assertEqual(json.loads(body)["client"], "127.0.0.1")
        finally:
            with_server.close()

    def test_rate_keys_are_bounded_and_forged_leftmost_ip_does_not_rotate_quota(self):
        server = test_native_integration.Server(
            application=controls_application(
                {
                    "trusted_proxies": ["127.0.0.0/8"],
                    "rate_limit": {
                        "requests": 1,
                        "window_seconds": 60,
                        "max_keys": 2,
                        "exclude_paths": ["/healthz"],
                    },
                }
            )
        )
        try:
            cases = (
                ("198.51.100.1, 203.0.113.8", 200),
                ("198.51.100.2, 203.0.113.8", 429),
                ("203.0.113.9", 200),
                ("203.0.113.10", 503),
            )
            for chain, expected in cases:
                with self.subTest(chain=chain):
                    status, headers, _ = request_with_headers(
                        server, "/client", [("X-Forwarded-For", chain)]
                    )
                    self.assertEqual(status, expected)
                    if expected != 200:
                        self.assertGreaterEqual(int(headers["retry-after"]), 1)
            self.assertEqual(server.request("GET", "/healthz")[0], 200)
        finally:
            server.close()


@unittest.skipUnless(
    importlib.util.find_spec("bessemer._native"), "build the native extension first"
)
class TransportLimitsHTTPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = test_native_integration.Server(
            application="""
import asyncio
import time
from pathlib import Path
from bessemer import App
app = App(openapi_url=None)
app.health("/healthz")
@app.get("/items/{item_id}")
async def item(item_id: int):
    return {"id": item_id}
"""
            + ADVERSARIAL_ROUTES,
            extra_args=(
                "--max-connections",
                "4",
                "--header-timeout",
                "0.2",
                "--max-headers",
                "16",
                "--max-header-size",
                "8192",
                "--max-request-target-size",
                "128",
                "--max-response-size",
                "256",
            ),
        )

    @classmethod
    def tearDownClass(cls):
        cls.server.close()

    def test_incomplete_headers_have_a_wall_clock_deadline(self):
        with socket.create_connection(
            ("127.0.0.1", self.server.port), timeout=2
        ) as connection:
            connection.sendall(b"GET /healthz HTTP/1.1\r\nHost: localhost\r\nX-Test:")
            began = time.monotonic()
            received = connection.recv(4096)
            self.assertLess(time.monotonic() - began, 1)
            if received:
                self.assertTrue(received.startswith(b"HTTP/1.1 408 "), received)
        self.assertEqual(self.server.request("GET", "/healthz")[0], 200)

    def test_target_header_bytes_and_header_count_have_independent_bounds(self):
        cases = (
            (b"GET /" + b"x" * 128 + b" HTTP/1.1\r\nHost: localhost\r\n\r\n", 414),
            (
                b"GET /healthz HTTP/1.1\r\nHost: localhost\r\nX-Fill: "
                + b"x" * 9000
                + b"\r\n\r\n",
                431,
            ),
            (
                b"GET /healthz HTTP/1.1\r\nHost: localhost\r\n"
                + b"X-Fill: x\r\n" * 20
                + b"\r\n",
                431,
            ),
        )
        for request, expected in cases:
            with (
                self.subTest(expected=expected, size=len(request)),
                socket.create_connection(
                    ("127.0.0.1", self.server.port), timeout=2
                ) as connection,
            ):
                connection.sendall(request)
                response = http.client.HTTPResponse(connection)
                response.begin()
                self.assertEqual(response.status, expected)
                self.assertEqual(response.getheader("Connection"), "close")
                response.read()
        self.assertEqual(self.server.request("GET", "/healthz")[0], 200)

    def test_idle_keepalive_connections_cannot_exceed_connection_capacity(self):
        held = []
        try:
            for _ in range(4):
                connection = socket.create_connection(
                    ("127.0.0.1", self.server.port), timeout=2
                )
                held.append(connection)
                connection.sendall(b"GET /healthz HTTP/1.1\r\nHost: localhost\r\n\r\n")
                response = http.client.HTTPResponse(connection)
                response.begin()
                self.assertEqual(response.status, 200)
                response.read()
            with socket.create_connection(
                ("127.0.0.1", self.server.port), timeout=2
            ) as connection:
                # At capacity the accept loop makes a best-effort nonblocking503
                # write, then closes, without spawning another connection task.
                received = connection.recv(4096)
                if received:
                    self.assertTrue(received.startswith(b"HTTP/1.1 503 "), received)
        finally:
            for connection in held:
                connection.close()
        deadline = time.monotonic() + 1
        while time.monotonic() < deadline:
            if self.server.request("GET", "/healthz")[0] == 200:
                break
        else:
            self.fail("closed connections did not release connection capacity")

    def test_response_byte_limit_accepts_exact_boundary_and_rejects_overflow(self):
        float_padding = 256 - len(b'{"payload":1.192092896e-7,"padding":""}')
        for kind, permitted, rejected in (
            ("bytes", 256, 257),
            ("string", 254, 255),
            ("strings", 85, 86),
            ("quotes", 127, 128),
            ("float", float_padding, float_padding + 1),
        ):
            with self.subTest(kind=kind):
                status, _, body = self.server.request(
                    "GET", f"/adversarial/budget/{kind}/{permitted}"
                )
                self.assertEqual(status, 200)
                self.assertEqual(len(body), 256)
                status, _, body = self.server.request(
                    "GET", f"/adversarial/budget/{kind}/{rejected}"
                )
                self.assertEqual(status, 500)
                self.assertEqual(json.loads(body), {"detail": "Internal Server Error"})
                self.assertEqual(self.server.request("GET", "/items/1")[0], 200)


@unittest.skipUnless(
    importlib.util.find_spec("bessemer._native"), "build the native extension first"
)
class StartupBudgetHTTPTests(unittest.TestCase):
    APPLICATION = """
import asyncio
from contextlib import asynccontextmanager
from bessemer import App

@asynccontextmanager
async def lifespan(app):
    await asyncio.sleep(0.25)
    yield

app = App(lifespan=lifespan, openapi_url=None)
app.health("/healthz")
"""

    def test_startup_budget_is_independent_of_the_shorter_shutdown_budget(self):
        server = test_native_integration.Server(
            application=self.APPLICATION,
            extra_args=(
                "--startup-timeout",
                "2",
                "--graceful-timeout",
                "0.1",
                "--restart-limit",
                "0",
            ),
        )
        try:
            self.assertEqual(server.request("GET", "/healthz")[0], 200)
        finally:
            server.close()


if __name__ == "__main__":
    unittest.main()
