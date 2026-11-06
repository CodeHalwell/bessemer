"""Multipart bodies over max_body_size spooled to disk (max_upload_size)."""

import io
import os
import socket
import tempfile
import unittest
from typing import Annotated

from bessemer import App, File, Form, Request, UploadFile
from bessemer.config import validate_server_limits
from bessemer.testclient import TestClient
from bessemer.testclient import _multipart as multipart

BODY_LIMIT = 4096


def build_app():
    app = App()

    @app.post("/upload")
    async def upload(
        note: Annotated[str, Form()],
        upload: UploadFile,
        request: Request,
    ):
        first = await upload.read(10)
        rest = await upload.read()
        await upload.seek(0)
        again = await upload.read()
        try:
            await request.body()
            body = "readable"
        except RuntimeError:
            body = "spooled"
        try:
            await upload.write(b"x")
            writable = True
        except io.UnsupportedOperation:
            writable = False
        return {
            "note": note,
            "filename": upload.filename,
            "content_type": upload.content_type,
            "size": upload.size,
            "matches": first + rest == again,
            "sum": sum(again) % 65536,
            "in_memory": isinstance(upload.file, io.BytesIO),
            "body": body,
            "writable": writable,
        }

    @app.post("/many")
    async def many(
        files: Annotated[list[UploadFile], File()],
        raw: Annotated[bytes, File()],
    ):
        return {
            "sizes": [item.size for item in files],
            "contents": [(await item.read())[:4].decode() for item in files],
            "raw": len(raw),
        }

    @app.post("/form")
    async def form_only(note: Annotated[str, Form()]):
        return {"note": note}

    return app


def payload(size):
    return bytes(index % 251 for index in range(size))


class UploadSpoolingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory()
        cls.client = TestClient(
            build_app(),
            server_options={
                "max_body_size": BODY_LIMIT,
                "max_upload_size": 1 << 20,
                "max_upload_bytes_in_flight": 4 << 20,
                "upload_dir": cls.directory.name,
            },
        )
        cls.client.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.client.__exit__(None, None, None)
        cls.directory.cleanup()

    def test_a_large_upload_is_spooled_and_readable(self):
        data = payload(300_000)
        response = self.client.post(
            "/upload",
            data={"note": "hello"},
            files={"upload": ("big.bin", data, "application/x-test")},
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(
            response.json(),
            {
                "note": "hello",
                "filename": "big.bin",
                "content_type": "application/x-test",
                "size": 300_000,
                "matches": True,
                "sum": sum(data) % 65536,
                "in_memory": False,
                "body": "spooled",
                "writable": False,
            },
        )
        # Spool files are unlinked as soon as they are created.
        self.assertEqual(os.listdir(self.directory.name), [])

    def test_small_uploads_stay_in_memory(self):
        response = self.client.post(
            "/upload", data={"note": "n"}, files={"upload": ("s.txt", b"small")}
        )
        body = response.json()
        self.assertEqual(
            (body["in_memory"], body["body"], body["writable"]),
            (True, "readable", True),
        )

    def test_several_files_and_bytes_parameters(self):
        files = [
            ("files", ("a", b"AAAA" + payload(10_000))),
            ("files", ("b", b"BBBB")),
            ("raw", ("r", payload(2_000))),
        ]
        response = self.client.post("/many", files=files)
        self.assertEqual(
            response.json(),
            {"sizes": [10_004, 4], "contents": ["AAAA", "BBBB"], "raw": 2_000},
        )
        # A bytes parameter is read into memory, so it keeps max_body_size.
        files[2] = ("raw", ("r", payload(BODY_LIMIT + 1)))
        too_big = self.client.post("/many", files=files)
        self.assertEqual(too_big.status_code, 413)
        self.assertIn("UploadFile", too_big.text)

    def test_a_spooled_body_with_only_empty_files(self):
        body, content_type = multipart({"note": "n" * 10}, {"upload": ("e", b"")})
        # Pad past max_body_size with a preamble so the body is spooled.
        body = b"x" * BODY_LIMIT + b"\r\n" + body
        response = self.client.post(
            "/upload", content=body, headers={"content-type": content_type}
        )
        self.assertEqual(response.status_code, 200, response.text)
        result = response.json()
        # Nothing was read from the empty file (the handler's write probe
        # then succeeds on its in-memory buffer).
        self.assertEqual(
            (result["note"], result["sum"], result["filename"]), ("n" * 10, 0, "e")
        )

    def test_limits(self):
        too_big = self.client.post(
            "/upload", data={"note": "n"}, files={"upload": ("x", payload(2 << 20))}
        )
        self.assertEqual(too_big.status_code, 413)
        self.assertIn("upload limit", too_big.text)
        # Field values still have to fit max_body_size.
        fields = self.client.post(
            "/upload",
            data={"note": "n" * (BODY_LIMIT + 1)},
            files={"upload": ("x", b"x")},
        )
        self.assertEqual(fields.status_code, 413)
        self.assertIn("Form fields", fields.text)
        # Routes without File() parameters keep max_body_size.
        body, content_type = multipart({"note": "n" * (BODY_LIMIT * 2)}, {})
        response = self.client.post(
            "/form", content=body, headers={"content-type": content_type}
        )
        self.assertEqual(response.status_code, 413)

    def chunked_upload(self, body, content_type, size):
        with socket.create_connection(("127.0.0.1", self.client._port)) as connection:
            head = (
                "POST /upload HTTP/1.1\r\nHost: test\r\nConnection: close\r\n"
                f"Content-Type: {content_type}\r\nTransfer-Encoding: chunked\r\n\r\n"
            ).encode()
            chunks = b"".join(
                f"{len(body[i : i + size]):x}\r\n".encode()
                + body[i : i + size]
                + b"\r\n"
                for i in range(0, len(body), size)
            )
            connection.sendall(head + chunks + b"0\r\n\r\n")
            response = b""
            while chunk := connection.recv(65536):
                response += chunk
        return response

    def test_a_chunked_body_is_spooled(self):
        body, content_type = multipart({"note": "chunked"}, {"upload": ("c", b"abc")})
        response = self.chunked_upload(body, content_type, 7)
        self.assertIn(b"200 OK", response.split(b"\r\n", 1)[0])
        self.assertIn(b'"note":"chunked"', response)
        self.assertIn(b'"in_memory":false', response)
        self.assertIn(b'"size":3', response)

    def test_fragmented_large_upload_preserves_file_contents(self):
        data = payload(96_000)
        body, content_type = multipart(
            {"note": "fragmented"}, {"upload": ("tiny-frames.bin", data)}
        )
        response = self.chunked_upload(body, content_type, 7)
        self.assertIn(b"200 OK", response.split(b"\r\n", 1)[0])
        self.assertIn(b'"size":96000', response)
        self.assertIn(f'"sum":{sum(data) % 65536}'.encode(), response)
        self.assertIn(b'"matches":true', response)
        self.assertIn(b'"in_memory":false', response)
        self.assertEqual(os.listdir(self.directory.name), [])

    def test_without_max_upload_size_the_body_limit_applies(self):
        with TestClient(
            build_app(), server_options={"max_body_size": BODY_LIMIT}
        ) as client:
            response = client.post(
                "/upload", data={"note": "n"}, files={"upload": ("x", payload(10_000))}
            )
            self.assertEqual(response.status_code, 413)

    def test_settings_validation(self):
        base = {
            "max_body_size": 1_048_576,
            "max_request_bytes_in_flight": 33_554_432,
            "max_in_flight": 128,
            "max_response_size": 4_194_304,
            "max_response_bytes_in_flight": 33_554_432,
            "max_connections": 1024,
            "max_headers": 64,
            "max_header_size": 32768,
            "max_request_target_size": 8192,
            "max_upload_bytes_in_flight": 4_294_967_296,
            "upload_dir": None,
        }
        with self.assertRaisesRegex(ValueError, "at least max_body_size"):
            validate_server_limits(base | {"max_upload_size": 1000})
        with self.assertRaisesRegex(ValueError, "max_upload_bytes_in_flight"):
            validate_server_limits(
                base
                | {"max_upload_size": 1 << 30, "max_upload_bytes_in_flight": 1 << 20}
            )
        with self.assertRaisesRegex(ValueError, "needs max_upload_size"):
            validate_server_limits(base | {"max_upload_size": 0, "upload_dir": "/tmp"})
        with self.assertRaisesRegex(ValueError, "not a directory"):
            build_app().run(max_upload_size=1 << 21, upload_dir="/nonexistent/dir")


if __name__ == "__main__":
    unittest.main()
