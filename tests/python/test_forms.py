"""Form fields and file uploads, decoded natively."""

import time
import unittest
from typing import Annotated

from bessemer import App, Body, Depends, File, Form, Request, UploadFile
from bessemer.testclient import TestClient


def build_app():
    app = App()

    @app.post("/login")
    async def login(
        username: Annotated[str, Form(min_length=1)],
        age: Annotated[int, Form(ge=0)] = 0,
        remember: bool = Form(False),
    ):
        return {"username": username, "age": age, "remember": remember}

    @app.post("/upload")
    async def upload(
        file: UploadFile,
        note: str = Form(""),
        raw: bytes | None = File(None),
    ):
        return {
            "filename": file.filename,
            "content_type": file.content_type,
            "size": file.size,
            "body": (await file.read()).decode(),
            "header": file.headers.get("x-trace"),
            "note": note,
            "raw": None if raw is None else raw.decode(),
        }

    @app.post("/many")
    async def many(
        files: Annotated[list[UploadFile], File(alias="file")],
        tag: str | None = Form(None),
    ):
        return {
            "names": [item.filename for item in files],
            "sizes": [item.size for item in files],
            "tag": tag,
        }

    @app.post("/optional")
    async def optional(avatar: UploadFile | None = None):
        return {"avatar": None if avatar is None else avatar.filename}

    @app.post("/with-request")
    async def with_request(request: Request, file: UploadFile):
        return {"body_size": len(await request.body()), "file": file.size}

    async def checksum(file: UploadFile):
        return len(await file.read())

    @app.post("/shared")
    async def shared(
        file: UploadFile,
        size: int = Depends(checksum),
        again: bytes | None = File(None, alias="file"),
    ):
        await file.seek(0)
        return {
            "size": size,
            "body": (await file.read()).decode(),
            "again": None if again is None else again.decode(),
        }

    @app.post("/python-form")
    async def python_form(request: Request):
        data = await request.form()
        return {
            "fields": [
                [
                    key,
                    {
                        "filename": value.filename,
                        "content_type": value.content_type,
                        "headers": value.headers,
                        "body": (await value.read()).decode("latin-1"),
                    }
                    if isinstance(value, UploadFile)
                    else value,
                ]
                for key, value in data.multi_items()
            ]
        }

    return app


def multipart(parts, boundary="BoUnD"):
    body = b""
    for name, filename, content_type, data in parts:
        body += f"--{boundary}\r\n".encode()
        disposition = f'Content-Disposition: form-data; name="{name}"'
        if filename is not None:
            disposition += f'; filename="{filename}"'
        body += disposition.encode() + b"\r\n"
        if content_type:
            body += f"Content-Type: {content_type}\r\n".encode()
        body += b"\r\n" + data + b"\r\n"
    body += f"--{boundary}--\r\n".encode()
    return body, {"content-type": f"multipart/form-data; boundary={boundary}"}


class FormTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(build_app())
        cls.client.start()

    @classmethod
    def tearDownClass(cls):
        cls.client.close()

    def test_urlencoded_fields_are_converted_and_validated(self):
        response = self.client.post(
            "/login", data={"username": "dan", "age": "42", "remember": "on"}
        )
        self.assertEqual(
            response.json(), {"username": "dan", "age": 42, "remember": True}
        )
        self.assertEqual(
            self.client.post("/login", data={"username": "dan"}).json()["age"], 0
        )
        for data, location, kind in (
            ({}, ["body", "username"], "missing"),
            ({"username": "dan", "age": "old"}, ["body", "age"], "int_parsing"),
            ({"username": "dan", "age": "-1"}, ["body", "age"], "value_error"),
            ({"username": ""}, ["body", "username"], "value_error"),
        ):
            with self.subTest(data=data):
                response = self.client.post("/login", data=data)
                self.assertEqual(response.status_code, 422)
                detail = response.json()["detail"][0]
                self.assertEqual((detail["loc"], detail["type"]), (location, kind))

    def test_multipart_fields_reach_form_parameters(self):
        body, headers = multipart([("username", None, None, b"caf\xc3\xa9")])
        response = self.client.post("/login", content=body, headers=headers)
        self.assertEqual(response.json()["username"], "café")

    def test_other_media_types_are_rejected(self):
        self.assertEqual(
            self.client.post("/login", json={"username": "x"}).status_code, 415
        )
        response = self.client.post(
            "/login", content="username=x", headers={"content-type": "text/plain"}
        )
        self.assertEqual(response.status_code, 415)

    def test_malformed_bodies_are_bad_requests(self):
        for body in (
            b'--BoUnD\r\nContent-Disposition: form-data; name="a"\r\n\r\nno end',
            b"--BoUnD\r\nContent-Type: text/plain\r\n\r\nx\r\n--BoUnD--\r\n",
        ):
            response = self.client.post(
                "/upload",
                content=body,
                headers={"content-type": "multipart/form-data; boundary=BoUnD"},
            )
            self.assertEqual(response.status_code, 400)
        body, headers = multipart([("username", None, None, b"\xff")])
        self.assertEqual(
            self.client.post("/login", content=body, headers=headers).status_code, 400
        )


class UploadTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(build_app())
        cls.client.start()

    @classmethod
    def tearDownClass(cls):
        cls.client.close()

    def test_files_fields_and_bytes_arrive_together(self):
        body, headers = multipart(
            [
                ("file", "notes.txt", "text/plain", b"line 1\r\nline 2"),
                ("note", None, None, b"hello"),
                ("raw", "raw.bin", None, b"\x00RAW"),
            ]
        )
        response = self.client.post("/upload", content=body, headers=headers)
        self.assertEqual(
            response.json(),
            {
                "filename": "notes.txt",
                "content_type": "text/plain",
                "size": 14,
                "body": "line 1\r\nline 2",
                "header": None,
                "note": "hello",
                "raw": "\x00RAW",
            },
        )

    def test_the_test_client_encodes_files(self):
        response = self.client.post(
            "/upload",
            files={"file": ("a.csv", b"x,y", "text/csv")},
            data={"note": "n"},
        )
        self.assertEqual(response.json()["content_type"], "text/csv")
        self.assertEqual(response.json()["note"], "n")
        response = self.client.post(
            "/many",
            files=[("file", ("1.txt", b"a")), ("file", ("2.txt", b"bb"))],
            data={"tag": "t"},
        )
        self.assertEqual(
            response.json(), {"names": ["1.txt", "2.txt"], "sizes": [1, 2], "tag": "t"}
        )
        with self.assertRaises(ValueError):
            self.client.post("/upload", files={"file": ("a", b"a")}, json={})

    def test_bindings_that_share_a_file_field_all_receive_it(self):
        response = self.client.post("/shared", files={"file": ("a.txt", b"abc")})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json(), {"size": 3, "body": "abc", "again": "abc"})
        schema = self.client.app.openapi()["paths"]["/shared"]["post"]["requestBody"]
        body = schema["content"]["multipart/form-data"]["schema"]
        self.assertEqual(body["required"], ["file"])

    def test_missing_extra_and_empty_files(self):
        response = self.client.post("/upload", data={"note": "only"})
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["detail"][0]["loc"], ["body", "file"])
        response = self.client.post(
            "/upload", files=[("file", ("1", b"a")), ("file", ("2", b"b"))]
        )
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["detail"][0]["type"], "too_many_files")
        # A browser's "no file chosen" part counts as absent.
        body, headers = multipart([("avatar", "", "application/octet-stream", b"")])
        response = self.client.post("/optional", content=body, headers=headers)
        self.assertEqual(response.json(), {"avatar": None})
        response = self.client.post(
            "/optional", files={"avatar": ("me.png", b"\x89PNG")}
        )
        self.assertEqual(response.json(), {"avatar": "me.png"})

    def test_part_headers_and_the_raw_request_body(self):
        body = (
            b'--BoUnD\r\nContent-Disposition: form-data; name="file"; filename="t"\r\n'
            b"X-Trace: abc\r\n\r\ndata\r\n--BoUnD--\r\n"
        )
        headers = {"content-type": "multipart/form-data; boundary=BoUnD"}
        self.assertEqual(
            self.client.post("/upload", content=body, headers=headers).json()["header"],
            "abc",
        )
        response = self.client.post("/with-request", content=body, headers=headers)
        self.assertEqual(response.json(), {"body_size": len(body), "file": 4})

    def test_uploads_respect_the_body_limit(self):
        with TestClient(build_app(), server_options={"max_body_size": 1024}) as client:
            response = client.post("/upload", files={"file": ("big", b"x" * 2048)})
            self.assertEqual(response.status_code, 413)


def raw_part(*headers, data=b"x", boundary="BoUnD"):
    lines = b"".join(header.encode() + b"\r\n" for header in headers)
    body = f"--{boundary}\r\n".encode() + lines + b"\r\n" + data
    body += f"\r\n--{boundary}--\r\n".encode()
    return body, {"content-type": f"multipart/form-data; boundary={boundary}"}


class PythonFormParserTests(unittest.TestCase):
    """``await request.form()`` runs the Python multipart parser."""

    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(build_app())
        cls.client.start()

    @classmethod
    def tearDownClass(cls):
        cls.client.close()

    def fields(self, body, headers):
        response = self.client.post("/python-form", content=body, headers=headers)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["fields"]

    def test_plain_and_utf8_fields(self):
        body, headers = multipart(
            [
                ("a", None, None, b"plain"),
                ("b", None, "text/plain; charset=utf-8", "caf\u00e9 \u4e2d".encode()),
                ("c", None, "text/plain; charset=UTF-8", b"ok"),
                ("d", None, None, b"bad \xff byte"),
            ]
        )
        self.assertEqual(
            self.fields(body, headers),
            [
                ["a", "plain"],
                ["b", "caf\u00e9 \u4e2d"],
                ["c", "ok"],
                ["d", "bad \ufffd byte"],
            ],
        )

    def test_file_parts_keep_filename_type_and_headers(self):
        body, headers = raw_part(
            'Content-Disposition: form-data; name="f"; filename="a \\"b\\".txt"',
            "Content-Type: Text/CSV; charset=utf-8",
            "X-Trace: abc",
            data=b"1,2",
        )
        ((name, upload),) = self.fields(body, headers)
        self.assertEqual(name, "f")
        self.assertEqual(upload["filename"], 'a "b".txt')
        self.assertEqual(upload["content_type"], "text/csv")
        self.assertEqual(upload["body"], "1,2")
        self.assertEqual(upload["headers"]["x-trace"], "abc")
        self.assertEqual(upload["headers"]["content-type"], "Text/CSV; charset=utf-8")

    def test_file_part_without_content_type_is_text_plain(self):
        body, headers = raw_part(
            "Content-Disposition: form-data; name=f; filename=plain.bin"
        )
        ((_, upload),) = self.fields(body, headers)
        self.assertEqual(upload["filename"], "plain.bin")
        self.assertEqual(upload["content_type"], "text/plain")

    def test_extended_filename_wins_and_is_decoded(self):
        body, headers = raw_part(
            'Content-Disposition: form-data; name="f"; filename="fallback.txt"; '
            "filename*=UTF-8''caf%C3%A9%20%E2%82%AC.txt"
        )
        ((_, upload),) = self.fields(body, headers)
        self.assertEqual(upload["filename"], "caf\u00e9 \u20ac.txt")

    def test_semicolon_inside_a_quoted_value_is_kept(self):
        body, headers = raw_part(
            'Content-Disposition: form-data; name="a;b"; filename="x;y=z.txt"'
        )
        ((name, upload),) = self.fields(body, headers)
        self.assertEqual((name, upload["filename"]), ("a;b", "x;y=z.txt"))

    def test_declared_charsets_never_select_a_codec(self):
        # The native parser decodes every text part as UTF-8; so does this one.
        for charset in ("punycode", "undefined", "idna", "latin-1", "bogus", "utf-16"):
            body, headers = raw_part(
                'Content-Disposition: form-data; name="a"',
                f"Content-Type: text/plain; charset={charset}",
                data=b"abc",
            )
            self.assertEqual(self.fields(body, headers), [["a", "abc"]], charset)

    def test_a_large_punycode_part_is_decoded_quickly(self):
        # Several seconds of CPU when the declared codec was honoured.
        payload = b"zzzba" + b"a" * 150_000
        body, headers = raw_part(
            'Content-Disposition: form-data; name="a"',
            "Content-Type: text/plain; charset=punycode",
            data=payload,
        )
        started = time.monotonic()
        fields = self.fields(body, headers)
        self.assertLess(time.monotonic() - started, 2)
        self.assertEqual(fields[0][1], payload.decode())

    def test_extended_filename_charset_must_be_utf8(self):
        for value in ("punycode''abc", "latin-1''abc", "undefined''abc", "abc"):
            body, headers = raw_part(
                f'Content-Disposition: form-data; name="f"; filename*={value}'
            )
            response = self.client.post("/python-form", content=body, headers=headers)
            self.assertEqual(response.status_code, 400, value)

    def test_many_semicolons_are_bounded(self):
        line = 'Content-Disposition: form-data; name="a"' + ";" * 8000
        parts = b""
        for _ in range(128):
            parts += b"--BoUnD\r\n" + line.encode() + b"\r\n\r\nx\r\n"
        body = parts + b"--BoUnD--\r\n"
        self.assertLess(len(body), 1 << 20)
        headers = {"content-type": "multipart/form-data; boundary=BoUnD"}
        started = time.monotonic()
        response = self.client.post("/python-form", content=body, headers=headers)
        elapsed = time.monotonic() - started
        self.assertEqual(response.status_code, 400, response.text)
        self.assertLess(elapsed, 1.0)

    def test_unterminated_quotes_with_many_parameters_are_bounded(self):
        line = 'Content-Disposition: form-data; name="a"' + '; a="' * 60
        body, headers = raw_part(line)
        started = time.monotonic()
        response = self.client.post("/python-form", content=body, headers=headers)
        self.assertLess(time.monotonic() - started, 1.0)
        self.assertEqual(response.status_code, 200, response.text)


class FormSchemaTests(unittest.TestCase):
    def test_openapi_describes_form_and_multipart_bodies(self):
        document = build_app().openapi()
        login = document["paths"]["/login"]["post"]["requestBody"]
        schema = login["content"]["application/x-www-form-urlencoded"]["schema"]
        self.assertEqual(schema["required"], ["username"])
        self.assertEqual(schema["properties"]["age"]["default"], 0)
        upload = document["paths"]["/upload"]["post"]["requestBody"]
        schema = upload["content"]["multipart/form-data"]["schema"]
        self.assertEqual(
            schema["properties"]["file"],
            {"type": "string", "contentMediaType": "application/octet-stream"},
        )
        many = document["paths"]["/many"]["post"]["requestBody"]
        items = many["content"]["multipart/form-data"]["schema"]["properties"]["file"]
        self.assertEqual(items["type"], "array")
        self.assertNotIn("parameters", document["paths"]["/upload"]["post"])

    def test_invalid_declarations_fail_at_build(self):
        cases = []

        def mixed(app):
            @app.post("/x")
            async def handler(payload: dict, name: str = Form("")):
                return {}

        def mixed_file(app):
            @app.post("/x")
            async def handler(file: UploadFile, payload: dict = Body()):  # noqa: B008
                return {}

        def wrong_type(app):
            @app.post("/x")
            async def handler(file: str = File()):
                return {}

        def bad_default(app):
            @app.post("/x")
            async def handler(file: UploadFile | None = File(b"x")):  # noqa: B008
                return {}

        def form_model(app):
            @app.post("/x")
            async def handler(payload: dict = Form()):  # noqa: B008
                return {}

        def constrained(app):
            @app.post("/x")
            async def handler(data: Annotated[bytes, File(max_length=1)]):
                return {}

        cases = [mixed, mixed_file, wrong_type, bad_default, form_model, constrained]
        for case in cases:
            app = App()
            with self.subTest(case=case.__name__), self.assertRaises(ValueError):
                case(app)
                app.build()


if __name__ == "__main__":
    unittest.main()
