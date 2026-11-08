"""Native gzip and Brotli response compression."""

import gzip
import http.client
import unittest

from bessemer import (
    App,
    Compression,
    EventSourceResponse,
    PlainTextResponse,
    Response,
    StreamingResponse,
)
from bessemer.testclient import TestClient

TEXT = "compress me please " * 200
CODED = gzip.compress(TEXT.encode(), mtime=0)


def build_app(compression=True):
    app = App(compression=compression)

    @app.get("/text")
    async def text():
        return PlainTextResponse(TEXT, headers={"etag": '"v1"'})

    @app.get("/json")
    async def json_items():
        return {"items": [TEXT] * 4}

    @app.get("/small")
    async def small():
        return {"ok": True}

    @app.get("/png")
    async def png():
        return Response(b"\x89PNG" + b"\x00" * 4096, media_type="image/png")

    @app.get("/bytes")
    async def bytes_response():
        return Response(b"x" * 4096, media_type="text/plain")

    @app.get("/coded")
    async def coded():
        return Response(
            CODED, media_type="text/plain", headers={"content-encoding": "gzip"}
        )

    @app.get("/empty")
    async def empty():
        return Response(b"", media_type="text/plain")

    @app.get("/no-transform")
    async def no_transform():
        return PlainTextResponse(TEXT, headers={"cache-control": "no-transform"})

    @app.get("/stream")
    async def stream():
        async def chunks():
            for index in range(3):
                yield f"chunk {index} " * 100

        return StreamingResponse(chunks(), media_type="text/plain")

    @app.get("/events")
    async def events():
        async def messages():
            for index in range(3):
                yield {"data": f"message {index}"}

        return EventSourceResponse(messages())

    return app


def brotli_decode(data: bytes) -> bytes | None:
    """Decode with an installed Brotli module; the Rust tests always decode."""
    try:
        import brotli
    except ImportError:
        try:
            import brotlicffi as brotli
        except ImportError:
            return None
    return brotli.decompress(data)


class CompressionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(build_app(Compression(min_size=512)))
        cls.client.start()

    @classmethod
    def tearDownClass(cls):
        cls.client.close()

    def get(self, path, encoding):
        return self.client.get(path, headers={"accept-encoding": encoding})

    def test_gzip_bodies_round_trip_with_cache_headers(self):
        response = self.get("/text", "gzip")
        self.assertEqual(response.headers["content-encoding"], "gzip")
        self.assertEqual(response.headers["vary"], "Accept-Encoding")
        self.assertEqual(response.headers["etag"], 'W/"v1"')
        self.assertEqual(int(response.headers["content-length"]), len(response.content))
        self.assertLess(len(response.content), len(TEXT))
        self.assertEqual(gzip.decompress(response.content).decode(), TEXT)

    def test_brotli_is_preferred_when_both_are_accepted(self):
        response = self.get("/json", "gzip, deflate, br")
        self.assertEqual(response.headers["content-encoding"], "br")
        self.assertLess(len(response.content), len(TEXT) * 4)
        decoded = brotli_decode(response.content)
        if decoded is not None:
            self.assertIn(TEXT, decoded.decode())

    def test_uncompressible_responses_are_left_alone(self):
        for path in ("/small", "/png", "/no-transform"):
            with self.subTest(path=path):
                response = self.get(path, "gzip, br")
                self.assertNotIn("content-encoding", response.headers)
        self.assertEqual(self.get("/small", "gzip").headers["vary"], "Accept-Encoding")
        self.assertNotIn("vary", self.get("/png", "gzip").headers)
        plain = self.get("/text", "identity")
        self.assertNotIn("content-encoding", plain.headers)
        self.assertEqual(plain.text, TEXT)
        head = self.client.head("/text", headers={"accept-encoding": "gzip"})
        self.assertNotIn("content-encoding", head.headers)

    def test_head_omits_length_only_when_get_would_be_compressed(self):
        for path in ("/json", "/text", "/bytes"):
            for encoding in ("gzip", "br"):
                with self.subTest(path=path, encoding=encoding):
                    headers = {"accept-encoding": encoding}
                    head = self.client.head(path, headers=headers)
                    self.assertEqual(head.status_code, 200)
                    self.assertEqual(head.content, b"")
                    self.assertNotIn("content-encoding", head.headers)
                    self.assertNotIn("content-length", head.headers)
                    get = self.client.get(path, headers=headers)
                    self.assertEqual(get.status_code, 200)
                    self.assertEqual(get.headers["content-encoding"], encoding)
                    self.assertEqual(
                        int(get.headers["content-length"]), len(get.content)
                    )

    def test_head_keeps_exact_length_when_get_is_not_transformed(self):
        cases = (
            ("/text", "identity"),
            ("/text", "gzip;q=0, br;q=0"),
            ("/small", "gzip"),
            ("/png", "br"),
            ("/no-transform", "gzip"),
            ("/coded", "br"),
            ("/empty", "gzip"),
        )
        for path, encoding in cases:
            with self.subTest(path=path, encoding=encoding):
                headers = {"accept-encoding": encoding}
                head = self.client.head(path, headers=headers)
                get = self.client.get(path, headers=headers)
                self.assertEqual((head.status_code, head.content), (200, b""))
                self.assertEqual(get.status_code, 200)
                self.assertEqual(
                    head.headers.get("content-length"), str(len(get.content))
                )
                self.assertEqual(
                    head.headers.get("content-encoding"),
                    get.headers.get("content-encoding"),
                )

    def test_streaming_head_omits_unknown_compressed_length(self):
        for path in ("/stream", "/events"):
            with self.subTest(path=path):
                head = self.client.head(path, headers={"accept-encoding": "gzip"})
                self.assertEqual((head.status_code, head.content), (200, b""))
                self.assertNotIn("content-length", head.headers)
                self.assertNotIn("content-encoding", head.headers)

    def test_head_then_get_on_one_keepalive_connection(self):
        connection = http.client.HTTPConnection(
            "127.0.0.1", self.client._port, timeout=2
        )
        try:
            connection.request("HEAD", "/text", headers={"accept-encoding": "gzip"})
            head = connection.getresponse()
            self.assertEqual(head.status, 200)
            self.assertEqual(head.read(), b"")
            self.assertIsNone(head.getheader("content-length"))
            socket = connection.sock
            self.assertIsNotNone(socket)
            connection.request("GET", "/text", headers={"accept-encoding": "gzip"})
            self.assertIs(connection.sock, socket)
            get = connection.getresponse()
            self.assertEqual(get.status, 200)
            self.assertEqual(get.getheader("content-encoding"), "gzip")
            self.assertEqual(gzip.decompress(get.read()).decode(), TEXT)
        finally:
            connection.close()

    def test_streams_are_compressed_frame_by_frame(self):
        response = self.get("/stream", "gzip")
        self.assertEqual(response.headers["content-encoding"], "gzip")
        self.assertNotIn("content-length", response.headers)
        expected = "".join(f"chunk {index} " * 100 for index in range(3))
        self.assertEqual(gzip.decompress(response.content).decode(), expected)
        events = self.get("/events", "gzip")
        self.assertEqual(events.headers["content-encoding"], "gzip")
        self.assertIn("message 2", gzip.decompress(events.content).decode())


class CompressionConfigTests(unittest.TestCase):
    def test_compression_is_off_by_default(self):
        with TestClient(build_app(None)) as client:
            response = client.get("/text", headers={"accept-encoding": "gzip"})
            self.assertNotIn("content-encoding", response.headers)
            head = client.head("/text", headers={"accept-encoding": "gzip"})
            self.assertEqual(head.content, b"")
            self.assertEqual(head.headers["content-length"], str(len(response.content)))

    def test_gzip_only_and_dictionary_settings(self):
        with TestClient(build_app({"br": False, "min_size": 0})) as client:
            response = client.get("/small", headers={"accept-encoding": "br, gzip"})
            # Compressing a tiny body would grow it, so it is sent as is.
            self.assertNotIn("content-encoding", response.headers)
            response = client.get("/text", headers={"accept-encoding": "br, gzip"})
            self.assertEqual(response.headers["content-encoding"], "gzip")

    def test_invalid_settings_are_rejected(self):
        for bad in (
            {"gzip": False, "br": False},
            {"zstd": True},
            {"min_size": -1},
            {"gzip": "yes"},
            "gzip",
        ):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                App(compression=bad).build()
        with self.assertRaises(ValueError):
            Compression(gzip=False, br=False)


if __name__ == "__main__":
    unittest.main()
