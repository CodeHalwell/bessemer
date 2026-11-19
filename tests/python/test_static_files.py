"""Native static file serving."""

import gzip
import importlib.util
import os
import select
import shutil
import socket
import tempfile
import time
import unittest
from email.utils import formatdate

from bessemer import App, Compression, FileResponse
from bessemer.testclient import TestClient

H2 = importlib.util.find_spec("h2") is not None
CSS = b"body { color: red; }\n" * 200


class StaticFileTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = tempfile.mkdtemp()
        cls.outside = tempfile.mkdtemp()
        os.makedirs(os.path.join(cls.root, "css"))
        os.makedirs(os.path.join(cls.root, "docs"))
        with open(os.path.join(cls.root, "css", "site.css"), "wb") as file:
            file.write(CSS)
        with open(os.path.join(cls.root, "css", "site.css.gz"), "wb") as file:
            file.write(gzip.compress(CSS))
        with open(os.path.join(cls.root, "logo.png"), "wb") as file:
            file.write(b"\x89PNG" + bytes(range(256)) * 4)
        with open(os.path.join(cls.root, "docs", "index.html"), "wb") as file:
            file.write(b"<h1>docs</h1>")
        with open(os.path.join(cls.root, "index.html"), "wb") as file:
            file.write(b"<h1>home</h1>")
        with open(os.path.join(cls.root, ".env"), "wb") as file:
            file.write(b"SECRET=1")
        with open(os.path.join(cls.outside, "secret.txt"), "wb") as file:
            file.write(b"outside")
        os.symlink(
            os.path.join(cls.outside, "secret.txt"), os.path.join(cls.root, "leak.txt")
        )
        # Visible aliases of hidden files and directories inside the root.
        os.makedirs(os.path.join(cls.root, ".git"))
        with open(os.path.join(cls.root, ".git", "config"), "wb") as file:
            file.write(b"[core]")
        os.symlink(os.path.join(cls.root, ".env"), os.path.join(cls.root, "config"))
        os.symlink(os.path.join(cls.root, ".git"), os.path.join(cls.root, "repository"))
        app = App(compression=Compression(min_size=64))
        app.static("/assets", cls.root, cache_control="public, max-age=60")
        app.static("/site", cls.root, html=True)

        @app.get("/assets-api")
        async def api():
            return {"ok": True}

        cls.client = TestClient(app)
        cls.client.start()

    @classmethod
    def tearDownClass(cls):
        cls.client.close()
        shutil.rmtree(cls.root)
        shutil.rmtree(cls.outside)

    def test_files_are_served_with_validators_and_types(self):
        response = self.client.get("/assets/css/site.css")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, CSS)
        self.assertEqual(response.headers["content-type"], "text/css; charset=utf-8")
        self.assertEqual(response.headers["accept-ranges"], "bytes")
        self.assertEqual(response.headers["cache-control"], "public, max-age=60")
        self.assertEqual(int(response.headers["content-length"]), len(CSS))
        self.assertIn("etag", response.headers)
        self.assertIn("last-modified", response.headers)
        png = self.client.get("/assets/logo.png")
        self.assertEqual(png.headers["content-type"], "image/png")
        self.assertEqual(len(png.content), 4 + 1024)
        head = self.client.head("/assets/logo.png")
        self.assertEqual(head.content, b"")
        self.assertEqual(head.headers["content-length"], str(4 + 1024))
        # Routes sharing the prefix's spelling are unaffected.
        self.assertEqual(self.client.get("/assets-api").json(), {"ok": True})

    def test_conditional_requests_get_not_modified(self):
        first = self.client.get("/assets/logo.png")
        etag = first.headers["etag"]
        response = self.client.get("/assets/logo.png", headers={"if-none-match": etag})
        self.assertEqual(response.status_code, 304)
        self.assertEqual(response.content, b"")
        self.assertEqual(response.headers["etag"], etag)
        later = formatdate(usegmt=True)
        response = self.client.get(
            "/assets/logo.png", headers={"if-modified-since": later}
        )
        self.assertEqual(response.status_code, 304)
        response = self.client.get(
            "/assets/logo.png",
            headers={"if-modified-since": "Mon, 01 Jan 1990 00:00:00 GMT"},
        )
        self.assertEqual(response.status_code, 200)

    def test_byte_ranges(self):
        whole = self.client.get("/assets/logo.png").content
        response = self.client.get("/assets/logo.png", headers={"range": "bytes=4-13"})
        self.assertEqual(response.status_code, 206)
        self.assertEqual(response.content, whole[4:14])
        self.assertEqual(response.headers["content-range"], f"bytes 4-13/{len(whole)}")
        suffix = self.client.get("/assets/logo.png", headers={"range": "bytes=-5"})
        self.assertEqual(suffix.content, whole[-5:])
        response = self.client.get("/assets/logo.png", headers={"range": "bytes=9999-"})
        self.assertEqual(response.status_code, 416)
        self.assertEqual(response.headers["content-range"], f"bytes */{len(whole)}")
        stale = self.client.get(
            "/assets/logo.png", headers={"range": "bytes=0-1", "if-range": '"old"'}
        )
        self.assertEqual(stale.status_code, 200)
        self.assertEqual(stale.content, whole)

    def test_precompressed_siblings_are_preferred(self):
        response = self.client.get(
            "/assets/css/site.css", headers={"accept-encoding": "gzip"}
        )
        self.assertEqual(response.headers["content-encoding"], "gzip")
        self.assertEqual(response.headers["vary"], "Accept-Encoding")
        self.assertEqual(gzip.decompress(response.content), CSS)
        self.assertTrue(response.headers["etag"].endswith('-gzip"'))
        identity = self.client.get("/assets/css/site.css")
        self.assertNotIn("content-encoding", identity.headers)
        self.assertEqual(identity.headers["vary"], "Accept-Encoding")

    def test_precompressed_representations_revalidate_with_their_own_tag(self):
        first = self.client.get(
            "/assets/css/site.css", headers={"accept-encoding": "gzip"}
        )
        again = self.client.get(
            "/assets/css/site.css",
            headers={"accept-encoding": "gzip", "if-none-match": first.headers["etag"]},
        )
        self.assertEqual(again.status_code, 304)
        self.assertEqual(again.headers["etag"], first.headers["etag"])
        self.assertEqual(again.headers["vary"], "Accept-Encoding")
        # The identity representation does not match the gzip tag.
        identity = self.client.get(
            "/assets/css/site.css", headers={"if-none-match": first.headers["etag"]}
        )
        self.assertEqual(identity.status_code, 200)
        self.assertEqual(identity.content, CSS)

    def test_paths_never_leave_the_directory(self):
        for path in (
            "/assets/../secret.txt",
            "/assets/%2e%2e/secret.txt",
            "/assets/css/%2e%2e/%2e%2e/etc/passwd",
            "/assets/.env",
            "/assets/leak.txt",
            "/assets/config",
            "/assets/repository/config",
            "/assets/missing.css",
            "/assets/css",
            "/assets/%00",
        ):
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).status_code, 404)

    def test_html_mode_serves_index_files(self):
        self.assertEqual(self.client.get("/site/").text, "<h1>home</h1>")
        self.assertEqual(self.client.get("/site/docs/").text, "<h1>docs</h1>")
        self.assertEqual(
            self.client.get("/site/docs/").headers["content-type"],
            "text/html; charset=utf-8",
        )
        # A directory without its slash redirects, so relative links resolve.
        bare = self.client.get("/site/docs?x=1", follow_redirects=False)
        self.assertEqual(bare.status_code, 307)
        self.assertEqual(bare.headers["location"], "/site/docs/?x=1")
        self.assertEqual(self.client.get("/site/docs").text, "<h1>docs</h1>")
        mount = self.client.get("/site?x=1", follow_redirects=False)
        self.assertEqual(mount.status_code, 307)
        self.assertEqual(mount.headers["location"], "/site/?x=1")
        self.assertEqual(self.client.get("/site").text, "<h1>home</h1>")
        self.assertEqual(self.client.get("/assets/docs").status_code, 404)

    def test_a_root_mount_leaves_application_routes_alone(self):
        app = App()

        @app.get("/api/items")
        async def items():
            return [1]

        app.static("/", self.root, html=True)
        with TestClient(app) as client:
            self.assertEqual(client.get("/api/items").json(), [1])
            self.assertEqual(client.get("/").text, "<h1>home</h1>")
            self.assertEqual(client.get("/openapi.json").status_code, 200)
            self.assertEqual(client.get("/nope").status_code, 404)

    def test_an_application_index_route_keeps_priority_over_a_root_mount(self):
        for static_first in (False, True):
            app = App()
            if static_first:
                app.static("/", self.root, html=True)

            @app.get("/")
            async def home():
                return {"home": "app"}

            if not static_first:
                app.static("/", self.root, html=True)
            with self.subTest(static_first=static_first), TestClient(app) as client:
                self.assertEqual(client.get("/").json(), {"home": "app"})
                self.assertEqual(client.get("/docs/").text, "<h1>docs</h1>")

    def test_regenerated_siblings_get_new_validators(self):
        root = tempfile.mkdtemp()
        path = os.path.join(root, "app.js")
        with open(path, "wb") as file:
            file.write(b"console.log(1)" * 20)
        with open(path + ".gz", "wb") as file:
            file.write(gzip.compress(b"console.log(1)" * 20))
        app = App()
        app.static("/static", root)
        with TestClient(app) as client:
            gz = {"accept-encoding": "gzip"}
            first = client.get("/static/app.js", headers=gz)
            # Rewrite only the sibling, with a later mtime and different bytes.
            with open(path + ".gz", "wb") as file:
                file.write(gzip.compress(b"console.log(2)" * 21, mtime=0))
            later = os.stat(path + ".gz").st_mtime + 10
            os.utime(path + ".gz", (later, later))
            again = client.get(
                "/static/app.js",
                headers={**gz, "if-none-match": first.headers["etag"]},
            )
            self.assertEqual(again.status_code, 200)
            self.assertNotEqual(again.headers["etag"], first.headers["etag"])
            self.assertEqual(gzip.decompress(again.content), b"console.log(2)" * 21)

    def test_symlinks_are_typed_by_their_requested_name(self):
        root = tempfile.mkdtemp()
        os.makedirs(os.path.join(root, "blobs"))
        with open(os.path.join(root, "blobs", "abc123"), "wb") as file:
            file.write(b"body { color: red }")
        os.symlink(
            os.path.join(root, "blobs", "abc123"), os.path.join(root, "site.css")
        )
        app = App()
        app.static("/static", root)
        with TestClient(app) as client:
            response = client.get("/static/site.css")
            self.assertEqual(response.status_code, 200)
            self.assertTrue(response.headers["content-type"].startswith("text/css"))

    def test_invalid_declarations(self):
        app = App()
        with self.assertRaises(ValueError):
            app.static("/x", os.path.join(self.root, "missing"))
        with self.assertRaises(ValueError):
            app.static("/x/{name}", self.root)
        with self.assertRaises(ValueError):
            app.static("/x", self.root, cache_control="bad\nvalue")
        with self.assertRaises(TypeError):
            app.static("/x", self.root, html="yes")


class _StalledH2:
    """A prior-knowledge HTTP/2 connection that grants no stream window, so
    every response body it asks for stays unsent."""

    def __init__(self, port):
        from h2.config import H2Configuration
        from h2.connection import H2Connection
        from h2.settings import SettingCodes

        self.socket = socket.create_connection(("127.0.0.1", port), timeout=10)
        self.connection = H2Connection(
            config=H2Configuration(client_side=True, header_encoding="utf-8")
        )
        self.connection.initiate_connection()
        self.connection.update_settings({SettingCodes.INITIAL_WINDOW_SIZE: 0})
        self.statuses = {}
        self.socket.sendall(self.connection.data_to_send())

    def request(self, path):
        stream = self.connection.get_next_available_stream_id()
        headers = [(":method", "GET"), (":scheme", "http")]
        headers += [(":authority", "t"), (":path", path)]
        self.connection.send_headers(stream, headers, end_stream=True)
        self.socket.sendall(self.connection.data_to_send())
        return stream

    def statuses_of(self, streams, timeout=30):
        """Read until every stream has its response head."""
        from h2.events import ResponseReceived

        deadline = time.monotonic() + timeout
        while not all(stream in self.statuses for stream in streams):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AssertionError(f"responses missing: {self.statuses}")
            if select.select([self.socket], [], [], remaining)[0]:
                chunk = self.socket.recv(65536)
                if not chunk:
                    raise AssertionError("HTTP/2 connection ended")
                for event in self.connection.receive_data(chunk):
                    if isinstance(event, ResponseReceived):
                        status = dict(event.headers)[":status"]
                        self.statuses[event.stream_id] = int(status)
                self.socket.sendall(self.connection.data_to_send())
        return [self.statuses[stream] for stream in streams]

    def close(self):
        self.socket.close()


def _descriptors_on(path):
    """Descriptors this process holds on ``path``, or None without /proc."""
    if not os.path.isdir("/proc/self/fd"):
        return None
    count = 0
    for name in os.listdir("/proc/self/fd"):
        try:
            count += os.readlink(f"/proc/self/fd/{name}") == path
        except OSError:
            pass
    return count


class OpenFileBoundTests(unittest.TestCase):
    @unittest.skipUnless(H2, "requires the h2 package")
    def test_stalled_streams_share_descriptors_and_the_bound_is_on_distinct_files(self):
        root = os.path.realpath(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root)
        large = os.path.join(root, "large.bin")
        payload = os.urandom(256 * 1024)
        with open(large, "wb") as file:
            file.write(payload)
        for index in range(4):
            with open(os.path.join(root, f"f{index}.txt"), "wb") as file:
                file.write(b"file %d\n" % index)
        app = App()
        app.static("/assets", root)

        @app.get("/download")
        async def download():
            return FileResponse(large)

        # The bound is `max_connections` distinct files per process.
        options = {"max_connections": 4, "request_timeout": 30.0}
        with TestClient(app, server_options=options) as client:
            tag = client.get("/assets/f3.txt").headers["etag"]
            stalled = _StalledH2(client._port)
            try:
                # Sixty bodies of one file, from a mount and from a route,
                # none of them sent: more than the bound, all answered, and
                # all reading through one descriptor.
                streams = [stalled.request("/assets/large.bin") for _ in range(30)]
                streams += [stalled.request("/download") for _ in range(30)]
                self.assertEqual(stalled.statuses_of(streams), [200] * 60)
                held = _descriptors_on(large)
                if held is not None:
                    self.assertEqual(held, 1)
                self.assertEqual(client.get("/assets/f0.txt").status_code, 200)
                # Three more distinct files fill the bound. Another file is
                # refused while they are open; a file already open, a
                # conditional request and HEAD are not.
                streams = [stalled.request(f"/assets/f{i}.txt") for i in range(3)]
                self.assertEqual(stalled.statuses_of(streams), [200] * 3)
                refused = client.get("/assets/f3.txt")
                self.assertEqual(refused.status_code, 503)
                self.assertEqual(
                    refused.json(), {"detail": "Static file capacity is full"}
                )
                self.assertEqual(client.get("/assets/f1.txt").status_code, 200)
                self.assertEqual(client.get("/download").content, payload)
                revalidated = client.get(
                    "/assets/f3.txt", headers={"If-None-Match": tag}
                )
                self.assertEqual(revalidated.status_code, 304)
                self.assertEqual(client.head("/assets/f3.txt").status_code, 200)
            finally:
                stalled.close()
            # The files come back when the streams are gone.
            deadline = time.monotonic() + 30
            while client.get("/assets/f3.txt").status_code != 200:
                self.assertLess(time.monotonic(), deadline)
                time.sleep(0.05)
            if _descriptors_on(large) is not None:
                while _descriptors_on(large):
                    self.assertLess(time.monotonic(), deadline)
                    time.sleep(0.05)


if __name__ == "__main__":
    unittest.main()
