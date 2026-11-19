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
        # The Content-Type as sent, as natively (and as in Starlette).
        self.assertEqual(upload["content_type"], "Text/CSV; charset=utf-8")
        self.assertEqual(upload["body"], "1,2")
        self.assertEqual(upload["headers"]["x-trace"], "abc")
        self.assertEqual(upload["headers"]["content-type"], "Text/CSV; charset=utf-8")
        native = body.replace(b'name="f"', b'name="file"')
        response = self.client.post("/upload", content=native, headers=headers)
        self.assertEqual(response.json()["content_type"], "Text/CSV; charset=utf-8")

    def test_file_part_without_content_type_has_none(self):
        body, headers = raw_part(
            "Content-Disposition: form-data; name=file; filename=plain.bin"
        )
        ((_, upload),) = self.fields(body, headers)
        self.assertEqual(upload["filename"], "plain.bin")
        self.assertIsNone(upload["content_type"])
        response = self.client.post("/upload", content=body, headers=headers)
        self.assertIsNone(response.json()["content_type"])

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

    def test_extended_filename_control_characters_are_refused_by_both_parsers(self):
        # The quoted form cannot carry NUL, CR or LF; the encoded form may not
        # either. The native parser (/upload) and this one agree.
        for encoded in (
            "..%2F..%2Fetc%2Fpasswd%00.png",
            "a%0D%0Ab.txt",
            "x%7F",
            "%C2%85",
        ):
            body, headers = raw_part(
                'Content-Disposition: form-data; name="file"; filename="ok.txt"; '
                f"filename*=UTF-8''{encoded}"
            )
            for path in ("/python-form", "/upload"):
                response = self.client.post(path, content=body, headers=headers)
                self.assertEqual(
                    (response.status_code, response.json()),
                    (400, {"detail": "Invalid multipart part headers"}),
                    (path, encoded),
                )
        # Separators are the application's concern, as for `filename`.
        body, headers = raw_part(
            "Content-Disposition: form-data; name=\"file\"; filename*=UTF-8''..%2Fa%20b"
        )
        ((_, upload),) = self.fields(body, headers)
        self.assertEqual(upload["filename"], "../a b")
        response = self.client.post("/upload", content=body, headers=headers)
        self.assertEqual(response.json()["filename"], "../a b")

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


# Field names a parser could read from the generated part headers below. The
# native route binds each as a text field and as a list of files.
PARSER_NAMES = (
    "a", "b", "f", "q", "x", "evil", "name", "filename", "file", "A",
    "a b", "a;b", 'a"b', "a=b", "a\\b", "\xe9", "\xa0a", "a\x1c",
)  # fmt: skip


def build_parser_app():
    """One server for both parsers: ``/native`` binds every name in
    ``PARSER_NAMES`` with ``Form()``/``File()``; ``/python`` calls
    ``request.form()``. Both answer in the same shape."""
    app = App()
    parameters = []
    for index, name in enumerate(PARSER_NAMES):
        parameters.append(f"t{index}: str | None = Form(None, alias={name!r})")
        parameters.append(
            f"u{index}: list[UploadFile] | None = File(None, alias={name!r})"
        )
    namespace = {"Form": Form, "File": File, "UploadFile": UploadFile}
    namespace["NAMES"] = len(PARSER_NAMES)
    exec(  # noqa: S102 -- Builds a handler with one parameter per name.
        f"async def native({', '.join(parameters)}):\n"
        "    values = locals()\n"
        "    return [[values[f't{i}'], [[u.filename, u.content_type]\n"
        "             for u in values[f'u{i}'] or ()]] for i in range(NAMES)]\n",
        namespace,
    )
    app.post("/native")(namespace["native"])

    @app.post("/python")
    async def python(request: Request):
        # Projected as natively: a repeated field keeps its last value, files
        # collect in order.
        data = await request.form()
        result = [[None, []] for _ in PARSER_NAMES]
        for key, value in data.multi_items():
            if key not in PARSER_NAMES:
                continue
            slot = result[PARSER_NAMES.index(key)]
            if isinstance(value, UploadFile):
                slot[1].append([value.filename, value.content_type])
            else:
                slot[0] = value
        return {"fields": result}

    return app


def part_body(header: bytes, boundary="BoUnD"):
    delimiter = b"--" + boundary.encode()
    body = delimiter + b"\r\n" + header + b"\r\n\r\nVAL\r\n" + delimiter + b"--\r\n"
    return body, {"content-type": f"multipart/form-data; boundary={boundary}"}


def part_headers(seed, count):
    """Seeded part header blocks built from the pieces parsers disagree on.

    Most have one ``name`` among other parameters, so that about half are
    accepted; the rest are refused for one of many reasons.
    """
    import random

    generator = random.Random(seed)
    choice = generator.choice
    spaces = ("", "", "", "", " ", " ", "  ", "\t", " \t", "\xa0", "\x1c", "\x0b")
    names = ("a", "f", "q", "evil", "A", "a b", "a;b", "a=b", 'a"b', "\xe9", "\xa0a")
    plain = names + ("evil.txt", "x.bin", "caf\xe9", "%41", "a\\b", "")
    inner = (
        "; name=f; filename=evil.txt",
        "; filename=evil.txt",
        "; name=f",
        'name=f; filename="q',
        "a;b=c",
        "=",
        ";",
        '\\"',
        "\\\\",
        "\\x",
        "evil.txt",
        "",
        "\xe9",
        "\t",
        ";" * 70,
    )
    extended = (
        "UTF-8''caf%C3%A9.txt",
        "utf-8'en'a%20b",
        "UTF-8''%41%42",
        "UTF-8''\xe9'x",
        "UTF-8''",
        "UTF-8''bad%ZZ",
        "UTF-8''%2",
        "UTF-8''%FF",
        "UTF-8''%C3",
        "UTF-8''a%0Ab",
        "UTF-8''x%7F",
        "latin-1''x",
        "abc",
        "UTF-8'x",
        "UTF-8''%%41",
        "UTF-8''%+1",
    )
    unknown = ("x", "x y", "foo*", "Name*", "file name", "\xe9", "x\x1c")

    def value(key, names):
        if key.lower() == "filename*" and generator.random() < 0.7:
            text = choice(extended)
            return f'"{text}"' if generator.random() < 0.2 else text
        roll = generator.random()
        if roll < 0.35:
            return choice(names)
        if roll < 0.7:
            escaped = choice(names).replace("\\", "\\\\").replace('"', '\\"')
            return f'"{escaped}"'
        text = "".join(choice(inner + names) for _ in range(generator.randrange(1, 4)))
        roll = generator.random()
        if roll < 0.9:
            return f'"{text}"'
        if roll < 0.95:
            return f'"{text}"' + choice(("x", " x", "\\", '"'))
        return '"' + text  # Unterminated.

    def parameter(key):
        if key is None:
            roll = generator.random()
            if roll < 0.05:
                return choice(("junk", "x y", "", " ", '"', '"a;b"', "name"))
            if roll < 0.08:
                return ";" * generator.randrange(1, 80)
            key = choice(("filename",) * 4 + ("filename*",) * 2 + unknown)
        if generator.random() < 0.1:
            key = choice((key.upper(), key.title(), f" {key}", f"{key} "))
        pool = names if key.strip().lower() == "name" else plain
        return f"{key}{choice(spaces)}={choice(spaces)}{value(key.strip(), pool)}"

    headers = []
    for _ in range(count):
        text = "form-data"
        if generator.random() < 0.05:
            text = choice(("Form-Data", " form-data ", "attachment", "form-data x", ""))
        keys = [None] * generator.randrange(0, 4)
        if generator.random() < 0.9:
            keys.insert(generator.randrange(len(keys) + 1), "name")
        for key in keys:
            text += ";" + choice(spaces[:9]) + parameter(key)
        if generator.random() < 0.05:
            text += choice((";", "; ", '"', "\x00", " ;"))
        name = "Content-Disposition"
        if generator.random() < 0.03:
            name = choice(("content-disposition", "CONTENT-DISPOSITION",
                           "Content-Disposition ", " Content-Disposition"))  # fmt: skip
        lines = [f"{name}:{choice(('', ' ', '  ', chr(9)))}{text}".encode()]
        roll = generator.random()
        if roll < 0.3:
            content_type = choice(
                ("text/plain", " Text/CSV; charset=utf-8 ", "a/b; name=x", "", "\xa0")
            )
            lines.append(f"Content-Type: {content_type}".encode())
        elif roll < 0.33:
            lines.append(choice((b"X-A: 1", b"no colon", b"Content-Type: a\x00",
                                 b"X-\xff: 1", b"X-A: \xc3\xa9", b"Content-Type: a")))  # fmt: skip
        elif roll < 0.35:
            lines.append(lines[0])  # A repeated Content-Disposition.
        if generator.random() < 0.02:
            lines.insert(0, b"X-B: 1")
        headers.append(b"\r\n".join(lines))
    return headers


class ParserAgreementTests(unittest.TestCase):
    """``request.form()`` and ``Form()``/``File()`` read every part alike.

    Two parsers that disagree about a part's name, or about whether it is a
    file, let a proxy, a WAF or one route see a different request from
    another, so the Python parser follows the native grammar exactly.
    """

    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(build_parser_app())
        cls.client.start()

    @classmethod
    def tearDownClass(cls):
        cls.client.close()

    def outcomes(self, header):
        """(status, detail-or-fields) from each parser for one part."""
        body, headers = part_body(header)
        results = []
        for path in ("/native", "/python"):
            response = self.client.post(path, content=body, headers=headers)
            payload = response.json()
            if response.status_code == 200 and path == "/python":
                payload = payload["fields"]
            results.append((response.status_code, payload))
        return results

    def assert_agree(self, header):
        native, python = self.outcomes(header)
        self.assertEqual(python, native, header)
        return native

    def named(self, outcome):
        """{name: (field, files)} for the names an outcome bound."""
        status, fields = outcome
        self.assertEqual(status, 200, fields)
        return {
            PARSER_NAMES[index]: (field, [tuple(item) for item in files])
            for index, (field, files) in enumerate(fields)
            if field is not None or files
        }

    def test_quoted_text_is_never_read_as_parameters(self):
        for header, expected in (
            # The review's cases: a quoted value that looks like parameters.
            (
                'form-data; x y="; name=f; filename=evil.txt"; name="a"',
                {"a": ("VAL", [])},
            ),
            ('form-data; x y="; filename=evil.txt"; name="a"', {"a": ("VAL", [])}),
            ('form-data; name="a"; x="; filename=evil.txt"', {"a": ("VAL", [])}),
            ('form-data; x="name=f"; name=a', {"a": ("VAL", [])}),
            # A key runs to the next "=", so "junk; filename" is one unknown key.
            ('form-data; name="a"; junk; filename="q"', {"a": ("VAL", [])}),
            ("form-data; junk; name=a", None),
            ('form-data; name="a"; filename="q"', {"a": (None, [("q", None)])}),
            ('form-data; name=a; filename=""', {"a": (None, [("", None)])}),
            ('form-data; name="a\\"b"', {'a"b': ("VAL", [])}),
            ('form-data; name="a;b"', {"a;b": ("VAL", [])}),
            ("form-data; name=a b", {"a b": ("VAL", [])}),
            ("form-data; NAME = a ; FileName=q", {"a": (None, [("q", None)])}),
            (
                "form-data; name=f; filename=x; filename*=UTF-8''caf%C3%A9",
                {"f": (None, [("caf\xe9", None)])},
            ),
        ):
            with self.subTest(header=header):
                outcome = self.assert_agree(f"Content-Disposition: {header}".encode())
                if expected is None:
                    self.assertEqual(outcome[0], 400)
                else:
                    self.assertEqual(self.named(outcome), expected)

    def test_malformed_dispositions_are_refused_by_both(self):
        for header in (
            # Unterminated quotes, wherever they are.
            'Content-Disposition: form-data; name="a;filename=x',
            'Content-Disposition: form-data; name="a"; x="; filename=evil.txt',
            'Content-Disposition: form-data; name="a\\',
            # Junk after a quoted value, empty tokens, no "=", a trailing ";".
            'Content-Disposition: form-data; name="a"b',
            'Content-Disposition: form-data; name="a" "b"',
            "Content-Disposition: form-data; name=",
            "Content-Disposition: form-data; name=a; x=",
            "Content-Disposition: form-data; name=a; junk",
            "Content-Disposition: form-data; name=a;",
            "Content-Disposition: form-data; name=a;;",
            "Content-Disposition: form-data; name=a" + ";" * 100,
            # Repeated, missing or empty parameters, and other kinds.
            "Content-Disposition: form-data; name=a; name=b",
            "Content-Disposition: form-data; name=a; filename=x; FILENAME=y",
            "Content-Disposition: form-data; filename=x",
            'Content-Disposition: form-data; name=""',
            "Content-Disposition: form-data",
            "Content-Disposition: attachment; name=a",
            "Content-Disposition: form-data x; name=a",
            # Strict filename*.
            "Content-Disposition: form-data; name=a; filename*=UTF-8''%ZZ",
            "Content-Disposition: form-data; name=a; filename*=UTF-8''%C3",
            "Content-Disposition: form-data; name=a; filename*=UTF-8''%+1",
            "Content-Disposition: form-data; name=a; filename*=latin-1''x",
            # Header lines the native parser refuses.
            "Content-Disposition : form-data; name=a",
            " Content-Disposition: form-data; name=a",
            "Content-Disposition: form-data; name=a\x00",
            "Content-Disposition: form-data; name=a\r\nContent-Type: a\r\nContent-Type: b",
            "Content-Disposition: form-data; name=a\r\nContent-Disposition: form-data; name=a",
            "Content-Disposition: form-data; name=a\r\nno colon",
            "Content-Type: text/plain",
            "\r\n".join(["Content-Disposition: form-data; name=a"] + ["X: 1"] * 16),
        ):
            with self.subTest(header=header):
                status, _ = self.assert_agree(header.encode("utf-8"))
                self.assertEqual(status, 400)
        status, _ = self.assert_agree(b"Content-Disposition: form-data; name=\xff")
        self.assertEqual(status, 400)

    def test_white_space_is_trimmed_as_natively(self):
        # U+00A0 is white space to Rust's trim; U+001C is not, though Python's
        # str.strip() removes it.
        for header in (
            "Content-Disposition: form-data; name=\xa0a",
            "Content-Disposition: form-data; name=\xa0a\xa0; filename=\u3000x",
            "Content-Disposition: form-data; name=a\x1c",
            "Content-Disposition: form-data;\tname=a\x0b",
            "Content-Disposition:\xa0form-data; name=a",
            (
                "Content-Disposition: form-data; name=f; filename=x\r\n"
                "Content-Type: \xa0text/plain "
            ),
        ):
            with self.subTest(header=header):
                status, _ = self.assert_agree(header.encode("utf-8"))
                self.assertEqual(status, 200)

    def test_generated_part_headers_agree(self):
        # Seeded, so a failure reproduces; the header is in the message.
        disagreements = []
        statuses = set()
        for header in part_headers(seed=20261119, count=2000):
            native, python = self.outcomes(header)
            statuses.add(native[0])
            if native != python:
                disagreements.append((header, native, python))
        self.assertEqual(statuses, {200, 400})
        self.assertEqual(
            disagreements[:5], [], f"{len(disagreements)} of 2000 disagree"
        )

    def test_adversarial_part_headers_parse_in_linear_time(self):
        # About 1 MiB of valid part headers, each near the 8 KiB limit, made
        # of what a backtracking or resuming scan would revisit.
        escapes = 'form-data; name="a"; x="' + '\\"' * 4000 + '"'
        quoted = 'form-data; name="a"; x="' + "; name=f; filename=x" * 400 + '"'
        params = "form-data; name=a" + "; x=y" * 1600
        parts = b""
        for index in range(132):
            text = (escapes, quoted, params)[index % 3]
            parts += (
                b"--BoUnD\r\nContent-Disposition: " + text.encode() + b"\r\n\r\nx\r\n"
            )
        body = parts + b"--BoUnD--\r\n"
        self.assertGreater(len(body), 1 << 20)
        from bessemer.request import _multipart_form

        content_type = "multipart/form-data; boundary=BoUnD"
        started = time.monotonic()
        form = _multipart_form(content_type, body)
        self.assertLess(time.monotonic() - started, 1.0)
        self.assertEqual(len(form.multi_items()), 132)
        self.assertEqual(set(form.keys()), {"a"})
        native, python = self.outcomes(b"Content-Disposition: " + escapes.encode())
        self.assertEqual(python, native)


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
