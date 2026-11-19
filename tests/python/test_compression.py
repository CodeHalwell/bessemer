"""Native gzip and Brotli response compression."""

import asyncio
import gzip
import http.client
import os
import shutil
import tempfile
import threading
import time
import unittest
import zlib
from email.utils import formatdate

from bessemer import (
    App,
    Compression,
    EventSourceResponse,
    FileResponse,
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
        head = self.client.head("/text", headers={"accept-encoding": "identity"})
        self.assertNotIn("content-encoding", head.headers)
        self.assertEqual(head.headers["etag"], '"v1"')

    def test_head_omits_length_only_when_get_would_be_compressed(self):
        for path in ("/json", "/text", "/bytes"):
            for encoding in ("gzip", "br"):
                with self.subTest(path=path, encoding=encoding):
                    headers = {"accept-encoding": encoding}
                    head = self.client.head(path, headers=headers)
                    self.assertEqual(head.status_code, 200)
                    self.assertEqual(head.content, b"")
                    # The coding and validator of the GET; its length is
                    # known only once the body has been compressed.
                    self.assertEqual(head.headers["content-encoding"], encoding)
                    self.assertNotIn("content-length", head.headers)
                    get = self.client.get(path, headers=headers)
                    self.assertEqual(get.status_code, 200)
                    self.assertEqual(get.headers["content-encoding"], encoding)
                    self.assertEqual(head.headers.get("etag"), get.headers.get("etag"))
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
                self.assertEqual(head.headers["content-encoding"], "gzip")
                plain = self.client.head(path)
                self.assertEqual((plain.status_code, plain.content), (200, b""))
                # A stream's length is not known, so none is stated.
                self.assertNotIn("content-length", plain.headers)
                self.assertNotIn("content-encoding", plain.headers)

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


class CompressedFileValidatorTests(unittest.TestCase):
    """A compressed file's 304 and 206 carry the validators of its 200."""

    @classmethod
    def setUpClass(cls):
        cls.root = tempfile.mkdtemp()
        cls.path = os.path.join(cls.root, "notes.txt")
        with open(cls.path, "wb") as file:
            file.write(TEXT.encode() * 4)
        app = App(compression=Compression(min_size=512))
        app.static("/static", cls.root)

        @app.get("/download")
        async def download():
            return FileResponse(cls.path, media_type="text/plain")

        cls.client = TestClient(app)
        cls.client.start()

    @classmethod
    def tearDownClass(cls):
        cls.client.close()
        shutil.rmtree(cls.root)

    def test_revalidating_the_compressed_variant_keeps_its_weak_tag_and_vary(self):
        for path in ("/static/notes.txt", "/download"):
            with self.subTest(path=path):
                identity = self.client.get(path)
                strong = identity.headers["etag"]
                self.assertFalse(strong.startswith("W/"))
                compressed = self.client.get(path, headers={"accept-encoding": "gzip"})
                self.assertEqual(compressed.headers["content-encoding"], "gzip")
                self.assertEqual(compressed.headers["vary"], "Accept-Encoding")
                weak = compressed.headers["etag"]
                self.assertEqual(weak, "W/" + strong)
                for method in ("GET", "HEAD"):
                    again = self.client.request(
                        method,
                        path,
                        headers={"accept-encoding": "gzip", "if-none-match": weak},
                    )
                    self.assertEqual(again.status_code, 304, method)
                    self.assertEqual(again.headers["etag"], weak, method)
                    self.assertEqual(again.headers["vary"], "Accept-Encoding", method)
                # The identity representation's tag is not what a client that
                # accepts gzip is sent, so it does not match: the client is
                # sent the compressed variant...
                held = self.client.get(
                    path, headers={"accept-encoding": "gzip", "if-none-match": strong}
                )
                self.assertEqual(held.status_code, 200)
                self.assertEqual(held.headers["content-encoding"], "gzip")
                self.assertEqual(held.headers["etag"], weak)
                # ...and the compressed variant's is not what an identity
                # client is sent, which revalidates with its own.
                for tag, status in ((weak, 200), (strong, 304)):
                    again = self.client.get(path, headers={"if-none-match": tag})
                    self.assertEqual(again.status_code, status, tag)
                    self.assertEqual(again.headers["etag"], strong)
                    self.assertEqual(again.headers["vary"], "Accept-Encoding")
                    self.assertNotIn("content-encoding", again.headers)
                # A range is of the identity representation, which varies too.
                part = self.client.get(
                    path, headers={"accept-encoding": "gzip", "range": "bytes=0-9"}
                )
                self.assertEqual(part.status_code, 206)
                self.assertEqual(part.content, TEXT.encode()[:10])
                self.assertEqual(part.headers["etag"], strong)
                self.assertEqual(part.headers["vary"], "Accept-Encoding")
                self.assertNotIn("content-encoding", part.headers)


MATRIX_TEXT = b"compress me please " * 400
MATRIX_SMALL = b"hello\n"
MATRIX_IMAGE = b"\x89PNG" + bytes(range(256)) * 16
# Where each kind of resource is in the static root, and its media type.
MATRIX_FILES = {
    "big": ("big.txt", "text/plain"),
    "small": ("small.txt", "text/plain"),
    "image": ("img.png", "image/png"),
    "encoded": ("encoded.gz", "text/plain"),
    "no-transform": ("big.txt", "text/plain"),
    "sibling": ("sib.txt", "text/plain"),
}
MATRIX_SOURCES = ("static", "file", "buffered", "stream")
MATRIX_ENCODINGS = (None, "gzip", "br", "identity")


def build_matrix_app(root):
    """Every kind of resource from a static mount, a FileResponse, a buffered
    and a streaming handler response."""
    app = App(compression=Compression(min_size=512))
    app.static("/static", root)
    app.static("/static-nt", root, cache_control="public, no-transform")

    def extra(kind, tag):
        headers = {"etag": tag}
        if kind == "encoded":
            headers["content-encoding"] = "gzip"
        if kind == "no-transform":
            headers["cache-control"] = "public, no-transform"
        return headers

    def content(kind):
        if kind == "small":
            return MATRIX_SMALL
        if kind == "image":
            return MATRIX_IMAGE
        if kind == "encoded":
            return gzip.compress(MATRIX_TEXT, mtime=0)
        return MATRIX_TEXT

    @app.get("/file/{kind}")
    async def file(kind: str):
        name, media_type = MATRIX_FILES[kind]
        headers = {}
        if kind == "encoded":
            headers["content-encoding"] = "gzip"
        if kind == "no-transform":
            headers["cache-control"] = "public, no-transform"
        return FileResponse(
            os.path.join(root, name), media_type=media_type, headers=headers
        )

    @app.get("/buffered/{kind}")
    async def buffered(kind: str):
        return Response(
            content(kind),
            media_type=MATRIX_FILES[kind][1],
            headers=extra(kind, f'"buffered-{kind}"'),
        )

    @app.get("/stream/{kind}")
    async def stream(kind: str):
        data = content(kind)

        async def chunks():
            for start in range(0, len(data), 1000):
                yield data[start : start + 1000]

        return StreamingResponse(
            chunks(),
            media_type=MATRIX_FILES[kind][1],
            headers=extra(kind, f'"stream-{kind}"'),
        )

    return app


def matrix_path(source, kind):
    if source == "static":
        mount = "/static-nt" if kind == "no-transform" else "/static"
        return f"{mount}/{MATRIX_FILES[kind][0]}"
    return f"/{source}/{kind}"


class BodylessResponseMatrixTests(unittest.TestCase):
    """A 304, a 206 and a HEAD carry the validators and Vary of the 200 that
    the same request headers would get, and If-None-Match matches only the tag
    the server sends that client for that representation.

    Where the answer is not "the same as the 200": a 206 is a range of the
    identity representation, so it keeps that representation's strong ETag
    (If-Range and combining ranges need a strong validator); and a HEAD whose
    GET would be compressed omits Content-Length, which is only known once the
    body has been compressed (RFC 9110 sections 8.6 and 9.3.2).
    """

    @classmethod
    def setUpClass(cls):
        cls.root = tempfile.mkdtemp()
        files = {
            "big.txt": MATRIX_TEXT,
            "small.txt": MATRIX_SMALL,
            "img.png": MATRIX_IMAGE,
            "encoded.gz": gzip.compress(MATRIX_TEXT, mtime=0),
            "sib.txt": MATRIX_TEXT,
            "sib.txt.gz": gzip.compress(MATRIX_TEXT, mtime=0),
        }
        for name, data in files.items():
            with open(os.path.join(cls.root, name), "wb") as file:
                file.write(data)
        cls.client = TestClient(build_matrix_app(cls.root))
        cls.client.start()

    @classmethod
    def tearDownClass(cls):
        cls.client.close()
        shutil.rmtree(cls.root)

    def send(self, method, path, encoding, **extra):
        headers = {} if encoding is None else {"accept-encoding": encoding}
        headers.update({name.replace("_", "-"): value for name, value in extra.items()})
        return self.client.request(method, path, headers=headers)

    def assertSameValidators(self, response, full, etag=None):
        self.assertEqual(
            response.headers.get("etag"),
            full.headers.get("etag") if etag is None else etag,
        )
        self.assertEqual(response.headers.get("vary"), full.headers.get("vary"))

    def cells(self):
        for source in MATRIX_SOURCES:
            for kind in MATRIX_FILES:
                if kind == "sibling" and source in ("buffered", "stream"):
                    continue  # Only files have precompressed siblings.
                for encoding in MATRIX_ENCODINGS:
                    yield source, kind, encoding

    def test_the_full_responses_are_what_the_matrix_assumes(self):
        for source, kind, encoding in self.cells():
            with self.subTest(source=source, kind=kind, encoding=encoding):
                full = self.send("GET", matrix_path(source, kind), encoding)
                self.assertEqual(full.status_code, 200)
                coded = encoding in ("gzip", "br")
                own_sibling = source == "static" and encoding == "gzip"
                compressed = coded and (
                    kind == "big"
                    or (kind == "small" and source == "stream")
                    or (kind == "sibling" and not own_sibling)
                )
                if compressed:
                    self.assertEqual(full.headers.get("content-encoding"), encoding)
                    self.assertTrue(full.headers["etag"].startswith("W/"))
                elif kind == "sibling" and source == "static" and encoding == "gzip":
                    self.assertEqual(full.headers["content-encoding"], "gzip")
                    self.assertTrue(full.headers["etag"].endswith('-gzip"'))
                elif kind == "encoded" and source != "static":
                    self.assertEqual(full.headers["content-encoding"], "gzip")
                else:
                    self.assertNotIn("content-encoding", full.headers)
                    self.assertFalse(full.headers["etag"].startswith("W/"))
                vary = kind in ("big", "small", "sibling")
                self.assertEqual(
                    full.headers.get("vary"), "Accept-Encoding" if vary else None
                )

    def test_head_carries_what_the_get_does(self):
        for source, kind, encoding in self.cells():
            with self.subTest(source=source, kind=kind, encoding=encoding):
                path = matrix_path(source, kind)
                full = self.send("GET", path, encoding)
                head = self.send("HEAD", path, encoding)
                self.assertEqual((head.status_code, head.content), (200, b""))
                self.assertSameValidators(head, full)
                self.assertEqual(
                    head.headers.get("content-encoding"),
                    full.headers.get("content-encoding"),
                )
                compressed_here = full.headers.get("content-encoding") in (
                    "gzip",
                    "br",
                ) and not (
                    kind == "encoded"
                    or (kind == "sibling" and source == "static" and encoding == "gzip")
                )
                if compressed_here:
                    self.assertNotIn("content-length", head.headers)
                else:
                    self.assertEqual(
                        head.headers.get("content-length"),
                        full.headers.get("content-length"),
                    )

    def test_if_none_match_matches_only_the_tag_sent_to_that_client(self):
        for source, kind, encoding in self.cells():
            path = matrix_path(source, kind)
            full = self.send("GET", path, encoding)
            sent = full.headers["etag"]
            strong = sent.removeprefix("W/")
            for method in ("GET", "HEAD"):
                for candidate in (strong, "W/" + strong, f'"other", {sent}'):
                    with self.subTest(
                        source=source,
                        kind=kind,
                        encoding=encoding,
                        method=method,
                        if_none_match=candidate,
                    ):
                        response = self.send(
                            method, path, encoding, if_none_match=candidate
                        )
                        listed = [tag.strip() for tag in candidate.split(",")]
                        matches = sent in listed
                        if source in ("static", "file") and matches:
                            self.assertEqual(response.status_code, 304)
                            self.assertNotIn("content-encoding", response.headers)
                        else:
                            self.assertEqual(response.status_code, 200)
                            self.assertEqual(
                                response.headers.get("content-encoding"),
                                full.headers.get("content-encoding"),
                            )
                        self.assertSameValidators(response, full)

    def test_if_modified_since_revalidates_with_the_validators_of_the_200(self):
        later = formatdate(usegmt=True)
        for source, kind, encoding in self.cells():
            with self.subTest(source=source, kind=kind, encoding=encoding):
                path = matrix_path(source, kind)
                full = self.send("GET", path, encoding)
                response = self.send("GET", path, encoding, if_modified_since=later)
                expected = 304 if source in ("static", "file") else 200
                self.assertEqual(response.status_code, expected)
                self.assertSameValidators(response, full)

    def test_a_range_is_of_the_identity_representation(self):
        for source, kind, encoding in self.cells():
            with self.subTest(source=source, kind=kind, encoding=encoding):
                path = matrix_path(source, kind)
                full = self.send("GET", path, encoding)
                for method in ("GET", "HEAD"):
                    part = self.send(method, path, encoding, range="bytes=0-4")
                    own_coding = (source == "file" and kind == "encoded") or (
                        source == "static" and kind == "sibling" and encoding == "gzip"
                    )
                    if source in ("static", "file") and not own_coding:
                        self.assertEqual(part.status_code, 206, method)
                        self.assertNotIn("content-encoding", part.headers)
                        self.assertEqual(part.headers.get("content-length"), "5")
                        strong = full.headers["etag"].removeprefix("W/")
                        self.assertSameValidators(part, full, etag=strong)
                    else:
                        self.assertEqual(part.status_code, 200, method)
                        self.assertSameValidators(part, full)
                        self.assertEqual(
                            part.headers.get("content-encoding"),
                            full.headers.get("content-encoding"),
                        )

    def test_an_applications_own_not_modified_is_left_alone(self):
        app = App(compression=Compression(min_size=0))

        @app.get("/cached")
        async def cached():
            return Response(status_code=304, headers={"etag": '"app"'})

        with TestClient(app) as client:
            for encoding in MATRIX_ENCODINGS:
                with self.subTest(encoding=encoding):
                    headers = {"if-none-match": 'W/"app"'}
                    if encoding is not None:
                        headers["accept-encoding"] = encoding
                    response = client.get("/cached", headers=headers)
                    self.assertEqual(response.status_code, 304)
                    self.assertEqual(response.headers["etag"], '"app"')
                    self.assertNotIn("vary", response.headers)


class CompressedEventStreamTests(unittest.TestCase):
    def test_events_arrive_as_they_are_produced(self):
        gate = threading.Event()
        large = "x" * (40 * 1024)

        app = App(compression=True)

        @app.get("/events")
        async def events():
            async def messages():
                yield {"data": "first-event"}
                # Held until the client has decoded the event before it.
                deadline = time.monotonic() + 30
                while not gate.is_set() and time.monotonic() < deadline:
                    await asyncio.sleep(0.01)
                yield {"data": large}
                deadline = time.monotonic() + 30
                while gate.is_set() and time.monotonic() < deadline:
                    await asyncio.sleep(0.01)
                yield {"data": "last-event"}

            return EventSourceResponse(messages())

        with TestClient(app) as client:
            connection = http.client.HTTPConnection(
                "127.0.0.1", client._port, timeout=30
            )
            try:
                connection.request(
                    "GET", "/events", headers={"accept-encoding": "gzip"}
                )
                response = connection.getresponse()
                self.assertEqual(response.getheader("content-encoding"), "gzip")
                decoder = zlib.decompressobj(16 + zlib.MAX_WBITS)
                text = ""
                # Each wait below would time out if events were held back.
                while "first-event" not in text:
                    text += decoder.decompress(response.read1(65536)).decode()
                gate.set()
                while large not in text:
                    text += decoder.decompress(response.read1(65536)).decode()
                gate.clear()
                while "last-event" not in text:
                    text += decoder.decompress(response.read1(65536)).decode()
            finally:
                connection.close()


class CompressionConfigTests(unittest.TestCase):
    def test_compression_is_off_by_default(self):
        with TestClient(build_app(None)) as client:
            response = client.get("/text", headers={"accept-encoding": "gzip"})
            self.assertNotIn("content-encoding", response.headers)
            head = client.head("/text", headers={"accept-encoding": "gzip"})
            self.assertEqual(head.content, b"")
            self.assertEqual(head.headers["content-length"], str(len(response.content)))
            # A stream's length is unknown, so a HEAD states none (not 0).
            stream = client.head("/stream")
            self.assertEqual((stream.status_code, stream.content), (200, b""))
            self.assertNotIn("content-length", stream.headers)

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
