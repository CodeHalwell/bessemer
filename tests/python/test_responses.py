from __future__ import annotations

import asyncio
import datetime as dt
import enum
import json
import unittest
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from http import HTTPStatus
from uuid import UUID

from bessemer.exception_handlers import http_exception_handler
from bessemer.request import (
    BackgroundTasks,
    Request,
    RequestHeaders,
    RequestValidationError,
)
from bessemer.responses import (
    Headers,
    HTMLResponse,
    HTTPException,
    JSONResponse,
    ORJSONResponse,
    PlainTextResponse,
    RedirectResponse,
    Response,
    UJSONResponse,
    jsonable,
)


class ResponseTests(unittest.TestCase):
    def test_explicit_byte_text_and_json_responses(self):
        response = Response(
            b"\x00\xff",
            status_code=201,
            headers={"X-Trace": "one"},
            media_type="application/octet-stream",
        )
        self.assertEqual(
            response.to_native(),
            (
                201,
                b"\x00\xff",
                [("X-Trace", "one"), ("Content-Type", "application/octet-stream")],
                "bytes",
            ),
        )
        text = PlainTextResponse("caf\N{LATIN SMALL LETTER E WITH ACUTE}")
        self.assertEqual(
            text.to_native()[1], "caf\N{LATIN SMALL LETTER E WITH ACUTE}".encode()
        )
        self.assertEqual(text.headers["content-type"], "text/plain; charset=utf-8")
        self.assertEqual(
            HTMLResponse("<b>ready</b>").headers["Content-Type"],
            "text/html; charset=utf-8",
        )
        self.assertEqual(
            JSONResponse({"count": 2}, status_code=202).to_native()[::3], (202, "json")
        )

    def test_unsafe_response_headers_are_rejected_at_creation_and_mutation(self):
        invalid = [
            ("X-Trace\r\nInjected", "one"),
            ("X-Trace", "one\r\nInjected: yes"),
            ("X-Trace", "\x00"),
            ("X-Trace", "\x7f"),
            ("\N{SNOWMAN}", "one"),
            ("Content-Length", "2"),
            ("Transfer-Encoding", "chunked"),
            ("Connection", "close"),
            ("Keep-Alive", "timeout=5"),
            ("TE", "trailers"),
            ("Trailer", "X-Foo"),
            ("Upgrade", "websocket"),
        ]
        for name, value in invalid:
            with self.subTest(name=name, value=value):
                with self.assertRaises(ValueError):
                    Response(headers={name: value})
                response = Response()
                with self.assertRaises(ValueError):
                    response.headers[name] = value

    def test_header_lookup_mutation_and_cookie_duplicates(self):
        headers = Headers([("X-Trace", "one"), ("x-trace", "two")])
        self.assertEqual(headers["X-TRACE"], "two")
        self.assertEqual(headers.getlist("x-trace"), ["one", "two"])
        headers["X-Trace"] = "three"
        self.assertEqual(headers.getlist("x-trace"), ["three"])
        response = Response()
        response.set_cookie("session", "opaque", secure=True, httponly=True)
        response.set_cookie("preference", "dark", samesite="strict")
        self.assertEqual(len(response.headers.getlist("set-cookie")), 2)
        self.assertIn("HttpOnly", response.headers.getlist("set-cookie")[0])
        self.assertIn("Secure", response.headers.getlist("set-cookie")[0])
        self.assertEqual(
            len([name for name, _ in response.to_native()[2] if name == "Set-Cookie"]),
            2,
        )

    def test_header_limits_are_enforced_before_copying_a_native_envelope(self):
        headers = Headers(("Set-Cookie", f"key{index}=value") for index in range(64))
        with self.assertRaises(ValueError):
            headers.append("X-Extra", "one")
        self.assertEqual(len(headers.raw), 64)
        headers["Set-Cookie"] = "replacement=value"
        self.assertEqual(headers.raw, [("Set-Cookie", "replacement=value")])
        with self.assertRaises(ValueError):
            headers["X-Extra"] = "x" * 16_384
        self.assertEqual(headers.raw, [("Set-Cookie", "replacement=value")])

    def test_cookie_prefixes_and_cross_site_security_requirements(self):
        response = Response()
        for options in (
            {"key": "__Secure-session"},
            {"key": "__Host-session", "secure": True, "domain": "example.com"},
            {"key": "__Host-session", "secure": True, "path": "/other"},
            {"key": "session", "samesite": "none"},
            {"key": "session", "samesite": "bogus"},
            {"key": "session\r\nX-Injected"},
        ):
            with self.subTest(options=options), self.assertRaises(ValueError):
                response.set_cookie(**options)
        response.set_cookie("__Host-session", "ok", secure=True, samesite="none")
        response.delete_cookie("__Host-session", secure=True)
        self.assertIn("Max-Age=0", response.headers.getlist("set-cookie")[-1])

    def test_cookie_values_and_attributes_cannot_inject_additional_attributes(self):
        response = Response()
        for value in (
            "has space",
            'has"quote',
            "has,comma",
            "has;semicolon",
            "has\\escape",
            "\x00",
            "\N{SNOWMAN}",
        ):
            with self.subTest(value=value), self.assertRaises(ValueError):
                response.set_cookie("session", value)
        for options in (
            {"path": "/; SameSite=None"},
            {"domain": "example.com; Secure"},
            {"domain": "bad..example"},
            {"domain": "-bad.example"},
            {"domain": "\N{SNOWMAN}.example"},
            {"path": "/\n"},
        ):
            with self.subTest(options=options), self.assertRaises(ValueError):
                response.set_cookie("session", "value", **options)
        for options in (
            {"secure": "false"},
            {"httponly": 1},
            {"samesite": True},
            {"expires": True},
        ):
            with self.subTest(options=options), self.assertRaises(TypeError):
                response.set_cookie("session", "value", **options)
        with self.assertRaises(ValueError):
            response.set_cookie(
                "session",
                "value",
                expires="Thu, 01 Jan 1970 00:00:00 GMT; SameSite=None",
            )
        response.set_cookie(
            "session",
            "encoded%20value",
            domain=".example.com",
            expires="Thu, 01 Jan 1970 00:00:00 GMT",
        )
        cookie = response.headers["Set-Cookie"]
        self.assertIn("encoded%20value", cookie)
        self.assertIn("Domain=.example.com", cookie)
        self.assertIn("expires=Thu, 01 Jan 1970 00:00:00 GMT", cookie)
        self.assertNotIn("SameSite=None", cookie)

    def test_redirects_encode_unicode_and_reject_injection(self):
        response = RedirectResponse(
            "/items/caf\N{LATIN SMALL LETTER E WITH ACUTE}?next=%2F"
        )
        self.assertEqual(response.status_code, 307)
        self.assertEqual(response.headers["location"], "/items/caf%C3%A9?next=%2F")
        for url in ("/ok\r\nX-Injected: true", "/ok\x00"):
            with self.assertRaises(ValueError):
                RedirectResponse(url)
        with self.assertRaises(ValueError):
            RedirectResponse("/", status_code=200)

    def test_error_status_and_headers_validation(self):
        error = HTTPException(
            401, "Authentication required", headers={"WWW-Authenticate": "Bearer"}
        )
        self.assertEqual(error.headers["www-authenticate"], "Bearer")
        for status in (199, 200, 399, 600, True):
            with self.assertRaises(ValueError):
                HTTPException(status, "error")
        for status in (100, 199, 600, True):
            with self.assertRaises(ValueError):
                Response(status_code=status)

    def test_http_status_members_are_accepted_as_plain_integer_status_codes(self):
        error = HTTPException(status_code=HTTPStatus.NOT_FOUND)
        self.assertIs(type(error.status_code), int)
        self.assertEqual((error.status_code, error.detail), (404, "Not Found"))
        response = Response(status_code=HTTPStatus.NO_CONTENT)
        self.assertIs(type(response.status_code), int)
        self.assertEqual(response.to_native()[0], 204)
        created = JSONResponse({"created": True}, status_code=HTTPStatus.CREATED)
        self.assertIs(type(created.status_code), int)
        self.assertEqual(created.to_native()[0], 201)
        redirect = RedirectResponse("/next", status_code=HTTPStatus.SEE_OTHER)
        self.assertIs(type(redirect.status_code), int)
        self.assertEqual(redirect.status_code, 303)

        class Loud(int):
            def __int__(self):
                raise AssertionError("custom integer conversion must not run")

        self.assertIs(type(Response(status_code=Loud(202)).status_code), int)
        # A member outside the range is refused like its number; a bool, a
        # float or text is still not a status code.
        for status in (HTTPStatus.CONTINUE, True, 200.0, "200", None):
            with self.subTest(status=status), self.assertRaises(ValueError):
                Response(status_code=status)
        for status in (HTTPStatus.OK, True, 404.0, "404", None):
            with self.subTest(status=status), self.assertRaises(ValueError):
                HTTPException(status)
        for status in (HTTPStatus.OK, True, 307.0):
            with self.subTest(status=status), self.assertRaises(ValueError):
                RedirectResponse("/", status_code=status)

    def test_copying_headers_keeps_every_repeated_field(self):
        source = Response()
        source.set_cookie("a", "1")
        source.set_cookie("b", "2")
        copied = Headers(source.headers)
        self.assertEqual(copied.raw, source.headers.raw)
        self.assertEqual(len(copied.getlist("set-cookie")), 2)
        # The copy is its own list.
        copied.append("X-Extra", "1")
        self.assertNotIn("x-extra", source.headers)
        rebuilt = Response(b"body", headers=source.headers)
        self.assertEqual(
            rebuilt.headers.getlist("set-cookie"),
            source.headers.getlist("set-cookie"),
        )
        error = HTTPException(
            401,
            headers=[("WWW-Authenticate", "Basic"), ("WWW-Authenticate", "Bearer")],
        )
        for status in (401, 204):
            error.status_code = status
            response = asyncio.run(http_exception_handler(Request(), error))
            self.assertEqual(
                response.headers.getlist("www-authenticate"), ["Basic", "Bearer"]
            )

    def test_headers_accept_each_argument_form(self):
        class Lookup(Mapping):
            def __getitem__(self, name):
                return {"X-A": "1"}[name]

            def __iter__(self):
                return iter(("X-A",))

            def __len__(self):
                return 1

        pairs = [("X-A", "1"), ("X-A", "2")]
        self.assertEqual(Headers({"X-A": "1"}).raw, [("X-A", "1")])
        self.assertEqual(Headers(Lookup()).raw, [("X-A", "1")])
        self.assertEqual(Headers(pairs).raw, pairs)
        self.assertEqual(Headers(tuple(pairs)).raw, pairs)
        self.assertEqual(Headers(iter(pairs)).raw, pairs)
        self.assertEqual(Headers().raw, [])
        for values in ({"Content-Length": "1"}, [("X-A", "\n")], (("\n", "1"),)):
            with self.subTest(values=values), self.assertRaises(ValueError):
                Headers(values)

    def test_json_response_body_is_the_encoded_content(self):
        response = JSONResponse({"name": "caf\N{LATIN SMALL LETTER E WITH ACUTE}"})
        self.assertEqual(
            response.body,
            '{"name":"caf\N{LATIN SMALL LETTER E WITH ACUTE}"}'.encode(),
        )
        self.assertIs(type(response.body), bytes)
        # Read from the content each time, with the conversions sending applies.
        response.content = {"day": dt.date(2026, 11, 9), "values": (1, 2.5, None)}
        self.assertEqual(
            json.loads(response.body), {"day": "2026-11-09", "values": [1, 2.5, None]}
        )
        self.assertEqual(JSONResponse(None).body, b"null")
        with self.assertRaises(TypeError):
            JSONResponse({"value": object()}).body  # noqa: B018 -- Reading it encodes.
        # The body cannot be assigned: nothing would send it.
        with self.assertRaises(AttributeError) as caught:
            response.body = b"{}"
        self.assertIn("content", str(caught.exception))

        class Legacy(JSONResponse):
            """Initialized through Response, which stores an empty body."""

            def __init__(self, content):
                Response.__init__(self, b"", status_code=201)
                self.content = content

        legacy = Legacy({"a": 1})
        self.assertEqual(legacy.body, b'{"a":1}')
        self.assertEqual(legacy.to_native()[:2], (201, {"a": 1}))

    def test_only_a_render_override_leaves_the_native_json_encoding(self):
        class Vendor(JSONResponse):
            media_type = "application/vnd.example+json"

        content = {"a": [1, "two"]}
        for response_class in (JSONResponse, ORJSONResponse, UJSONResponse, Vendor):
            with self.subTest(response_class=response_class.__name__):
                response = response_class(content, status_code=202)
                status, body, headers, kind = response.to_native(max_bytes=64)
                self.assertEqual((status, body, kind), (202, content, "json"))
                self.assertIsNot(body, content)
                self.assertEqual(headers, [("Content-Type", response_class.media_type)])
                self.assertEqual(response.body, b'{"a":[1,"two"]}')

    def test_a_render_override_is_what_the_response_sends(self):
        class Pretty(JSONResponse):
            def render(self, content):
                return json.dumps(content, indent=1).encode()

        class VendorPretty(Pretty):
            media_type = "application/vnd.example+json"

        class Custom(Pretty):
            def to_native(self, *, max_bytes=None):
                return (self.status_code, b"custom", self.headers.raw, "bytes")

        response = Pretty({"a": 1}, status_code=201, headers={"X-A": "1"})
        self.assertEqual(
            response.to_native(max_bytes=64),
            (
                201,
                b'{\n "a": 1\n}',
                [("X-A", "1"), ("Content-Type", "application/json")],
                "bytes",
            ),
        )
        self.assertEqual(response.body, b'{\n "a": 1\n}')
        vendor = VendorPretty({"a": 1})
        self.assertEqual(vendor.to_native()[1], b'{\n "a": 1\n}')
        self.assertEqual(vendor.headers["content-type"], "application/vnd.example+json")
        # A class that also defines to_native keeps its own.
        self.assertEqual(Custom({"a": 1}).to_native()[1], b"custom")
        with self.assertRaises(ValueError):
            Pretty({"a": "x" * 64}).to_native(max_bytes=32)

        class Text(JSONResponse):
            def render(self, content):
                return "not bytes"

        with self.assertRaises(TypeError):
            Text({"a": 1}).to_native()

    def test_json_conversion_handles_common_values_and_exact_large_integer(self):
        class Kind(enum.Enum):
            RED = "red"

        @dataclass
        class Item:
            identifier: UUID
            count: int

        value = {
            "item": Item(UUID(int=1), 2**100),
            "kind": Kind.RED,
            "price": Decimal("0.10"),
            "created": dt.date(2026, 11, 4),
        }
        self.assertEqual(
            jsonable(value),
            {
                "item": {"identifier": str(UUID(int=1)), "count": 2**100},
                "kind": "red",
                "price": "0.10",
                "created": "2026-11-04",
            },
        )

    def test_json_conversion_rejects_cycles_nonfinite_values_and_invalid_keys(self):
        cycle = []
        cycle.append(cycle)
        for value in (
            cycle,
            float("nan"),
            float("inf"),
            {1: "invalid"},
            10**4096,
            object(),
        ):
            with (
                self.subTest(type=type(value).__name__),
                self.assertRaises((TypeError, ValueError)),
            ):
                jsonable(value)

    def test_scalar_wrapper_conversion_does_not_consume_json_nesting(self):
        class Kind(enum.Enum):
            READY = "ready"

        class ModelWrapper:
            def model_dump(self, **kwargs):
                return {"value": "ready"}

        cases = (
            (dt.date(2026, 11, 4), "2026-11-04", 64),
            (UUID(int=1), str(UUID(int=1)), 64),
            (Decimal("0.1"), "0.1", 64),
            (Kind.READY, "ready", 64),
            (ModelWrapper(), {"value": "ready"}, 63),
        )
        for value, expected, nesting in cases:
            for _ in range(nesting):
                value, expected = [value], [expected]
            self.assertEqual(jsonable(value, max_bytes=1024), expected)

        class RecursiveWrapper:
            def model_dump(self, **kwargs):
                return self

        with self.assertRaisesRegex(ValueError, "normalization"):
            jsonable(RecursiveWrapper(), max_bytes=1024)

    def test_json_conversion_enforces_the_budget_while_normalizing(self):
        value = {"text": "\N{SNOWMAN}\n", "items": [True, None, 12]}
        encoded_size = len(
            json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()
        )
        self.assertEqual(jsonable(value, max_bytes=encoded_size), value)
        with self.assertRaises(ValueError):
            jsonable(value, max_bytes=encoded_size - 1)
        for value in ("x" * 100, [None] * 100, {"x" * 100: 1}):
            with self.subTest(type=type(value).__name__), self.assertRaises(ValueError):
                jsonable(value, max_bytes=16)

    def test_float_budget_allows_native_exponent_boundary_and_bounds_copied_nodes(self):
        native_empty_body = b'{"payload":1.192092896e-7,"padding":""}'
        value = {
            "payload": 1.192092896e-07,
            "padding": "x" * (256 - len(native_empty_body)),
        }
        self.assertEqual(jsonable(value, max_bytes=256), value)
        with self.assertRaises(ValueError):
            jsonable([0.0] * 1000, max_bytes=256)

    def test_json_conversion_uses_builtin_values_for_container_and_scalar_subclasses(
        self,
    ):
        class HostileList(list):
            def __iter__(self):
                raise AssertionError("custom iteration must not run")

        class HostileDict(dict):
            def items(self):
                raise AssertionError("custom items must not run")

        class HostileInt(int):
            def __int__(self):
                raise AssertionError("custom integer conversion must not run")

        class HostileFloat(float):
            def __float__(self):
                raise AssertionError("custom float conversion must not run")

        class HostileStr(str):
            def __str__(self):
                raise AssertionError("custom string conversion must not run")

        value = HostileDict(
            {
                HostileStr("values"): HostileList(
                    [HostileInt(3), HostileFloat(0.5), HostileStr("ok")]
                )
            }
        )
        self.assertEqual(jsonable(value, max_bytes=64), {"values": [3, 0.5, "ok"]})


class RequestTests(unittest.TestCase):
    def test_raw_json_errors_are_generic_validation_errors_and_depth_is_bounded(self):
        for body in (b'{"private-secret":invalid}', b"[" * 65 + b"0" + b"]" * 65):
            with (
                self.subTest(body=body[:30]),
                self.assertRaises(RequestValidationError) as caught,
            ):
                asyncio.run(Request({"body": body}).json())
            self.assertEqual(
                caught.exception.details,
                [{"loc": ["body"], "type": "json_invalid", "msg": "Invalid JSON body"}],
            )
            self.assertNotIn("private-secret", str(caught.exception))
        body = b"[" * 64 + b"0" + b"]" * 64
        self.assertIsInstance(asyncio.run(Request({"body": body}).json()), list)

    def test_raw_json_rejects_surrogates_in_values_keys_arrays_and_overwritten_fields(
        self,
    ):
        invalid = (
            b'"\\ud800"',
            b'"\\udfff"',
            b'{"key":"\\ud800"}',
            b'{"\\udc00":"value"}',
            b'{"nested":[{"value":["\\ud800"]}]}',
            b'{"key":"\\ud800","key":"valid replacement"}',
            b'{"key":["\\ud800"],"key":"valid replacement"}',
            b'"\\ud800\\ud800"',
            b'"\xed\xa0\x80"',
            '"valid text"'.encode("utf-16"),
        )
        for body in invalid:
            with self.subTest(body=body), self.assertRaises(ValueError):
                asyncio.run(Request({"body": body}).json())
        value = {"\N{GRINNING FACE}": [{"text": "\N{LINEAR B SYLLABLE B008 A}"}, "a"]}
        for ensure_ascii in (False, True):
            body = json.dumps(value, ensure_ascii=ensure_ascii).encode("utf-8")
            self.assertEqual(asyncio.run(Request({"body": body}).json()), value)

    def test_request_preserves_native_parsed_cookie_metadata(self):
        parsed = {"session": "quoted value", "theme": "dark"}
        request = Request(
            {"headers": [("Cookie", "unused=fallback")], "cookies": parsed}
        )
        parsed["session"] = "changed after construction"
        self.assertEqual(request.cookies, {"session": "quoted value", "theme": "dark"})
        with self.assertRaises(TypeError):
            request.cookies["session"] = "mutated"
        self.assertEqual(
            Request(
                {"headers": [("Cookie", "unused=fallback")], "cookies": {}}
            ).cookies,
            {},
        )

    def test_background_tasks_reject_generator_functions(self):
        def synchronous_generator():
            yield "unused"

        async def asynchronous_generator():
            yield "unused"

        tasks = BackgroundTasks()
        for call in (synchronous_generator, asynchronous_generator):
            with self.assertRaises(TypeError):
                tasks.add_task(call)

    def test_raw_json_uses_strict_finite_numbers_and_bounded_integers(self):
        self.assertEqual(
            asyncio.run(Request({"body": b'{"count":2}'}).json()), {"count": 2}
        )
        exact = b"9" * 4096
        self.assertEqual(asyncio.run(Request({"body": exact}).json()), int(exact))
        for body in (b"NaN", b"Infinity", b"-Infinity", b"1e9999", b"9" * 4097):
            with self.subTest(body=body[:20]), self.assertRaises(ValueError):
                asyncio.run(Request({"body": body}).json())

    def test_request_preserves_duplicate_headers_and_buffered_body(self):
        request = Request(
            {
                "method": "POST",
                "path": "/items",
                "query_string": "tag=a&tag=b",
                "headers": [
                    ("Host", "example.test"),
                    ("X-Tag", "a"),
                    ("x-tag", "b"),
                    ("X-Request-ID", "req-123"),
                    ("Cookie", "session=opaque; theme=dark"),
                ],
                "body": b'{"count":2}',
            }
        )
        self.assertEqual(request.headers.getlist("X-TAG"), ["a", "b"])
        self.assertEqual(request.query_params["tag"], "b")
        self.assertEqual(request.url, "http://example.test/items?tag=a&tag=b")
        self.assertEqual(request.request_id, "req-123")
        self.assertEqual(request.cookies, {"session": "opaque", "theme": "dark"})
        with self.assertRaises(TypeError):
            request.query_params["tag"] = "changed"
        self.assertEqual(RequestHeaders({"X-Test": "one"})["x-test"], "one")


if __name__ == "__main__":
    unittest.main()
