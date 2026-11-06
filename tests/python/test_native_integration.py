"""Exercise the installed wheel through real HTTP and the public CLI."""

import concurrent.futures
import http.client
import importlib.util
import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

APPLICATION = """
import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
import time
from bessemer import App

@asynccontextmanager
async def lifespan(app):
    yield
    Path("closed").touch()

app = App(lifespan=lifespan)
app.health("/healthz")

@app.get("/items/{item_id}")
async def item(item_id: int, enabled: bool = False):
    await asyncio.sleep(0.001)
    return {"id": item_id, "enabled": enabled}

@app.post("/items", status_code=201)
async def create(item: dict):
    return item

@app.get("/slow", timeout=0.05)
async def slow():
    try:
        await asyncio.sleep(10)
    finally:
        await asyncio.sleep(0.2)

@app.get("/blocking", timeout=0.05)
def blocking():
    time.sleep(0.25)
    return {"done": True}

@app.get("/drain")
async def drain():
    Path("started").touch()
    await asyncio.sleep(0.2)
    return {"drained": True}

@app.get("/fail")
async def fail():
    raise ValueError("private error detail")

@app.get("/invalid")
async def invalid():
    return float("nan")

@app.get("/empty", status_code=204)
async def empty():
    return {"must": "be discarded"}
"""


class Server:
    def __init__(
        self,
        *,
        max_body_size=128,
        extra_args=(),
        application=None,
        environment=None,
    ):
        self.directory = tempfile.TemporaryDirectory(prefix="bessemer-http-")
        self.root = Path(self.directory.name)
        (self.root / "fixture_app.py").write_text(
            APPLICATION if application is None else application,
            encoding="utf-8",
        )
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            self.port = listener.getsockname()[1]
        self.log = tempfile.TemporaryFile(mode="w+")  # noqa: SIM115 -- Owned by the test fixture and closed in tearDown.
        console = Path(sys.executable).parent / "bessemer"
        command = (
            [str(console)] if console.exists() else [sys.executable, "-m", "bessemer"]
        )
        env = os.environ.copy()
        if environment:
            env.update(environment)
        self.process = subprocess.Popen(
            [
                *command,
                "run",
                "fixture_app:app",
                "--port",
                str(self.port),
                "--max-in-flight",
                "1",
                "--workers",
                "1",
                "--max-body-size",
                str(max_body_size),
                "--request-timeout",
                "2",
                "--graceful-timeout",
                "1",
                *extra_args,
            ],
            cwd=self.root,
            stdout=self.log,
            stderr=self.log,
            env=env,
            # Its own group on Windows, so stop() can send it CTRL_BREAK.
            creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
        )
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                break
            try:
                if self.request("GET", "/healthz")[0] == 200:
                    return
            except OSError:
                pass
            time.sleep(0.02)
        self.log.seek(0)
        detail = self.log.read()
        self.close()
        raise AssertionError(f"server failed to start: {detail}")

    def request(
        self, method, path, body=None, *, chunked=False, content_type="application/json"
    ):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            headers = {"Content-Type": content_type} if body is not None else {}
            connection.request(method, path, body, headers, encode_chunked=chunked)
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()

    def stop(self):
        """Ask the server to drain and exit, as SIGTERM does on POSIX.

        On Windows ``terminate()`` is TerminateProcess, which cannot be
        handled; CTRL_BREAK is the graceful equivalent.
        """
        if sys.platform == "win32":
            os.kill(self.process.pid, signal.CTRL_BREAK_EVENT)
        else:
            self.process.terminate()

    def close(self):
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)
        self.log.close()
        self.directory.cleanup()


@unittest.skipUnless(
    importlib.util.find_spec("bessemer._native"), "build the native extension first"
)
class NativeHTTPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = Server()

    @classmethod
    def tearDownClass(cls):
        cls.server.close()

    def test_async_handler_typed_inputs_and_validation(self):
        status, _, body = self.server.request("GET", "/items/42?enabled=true")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), {"id": 42, "enabled": True})
        for path in ("/items/no-number", "/items/42?enabled=invalid"):
            status, _, body = self.server.request("GET", path)
            self.assertEqual(status, 422)
            self.assertIsInstance(json.loads(body)["detail"], list)

    def test_json_body_validation_and_limit_including_chunked(self):
        status, _, body = self.server.request("POST", "/items", b'{"name":"test"}')
        self.assertEqual(status, 201)
        self.assertEqual(json.loads(body), {"name": "test"})
        self.assertEqual(self.server.request("POST", "/items", b"{")[0], 422)
        self.assertEqual(self.server.request("POST", "/items", b"[]")[0], 422)
        self.assertEqual(self.server.request("POST", "/items", b"null")[0], 422)
        self.assertEqual(
            self.server.request("POST", "/items", b"{}", content_type="text/plain")[0],
            415,
        )
        self.assertEqual(
            self.server.request(
                "POST", "/items", b"{}", content_type="application/problem+json"
            )[0],
            201,
        )
        self.assertEqual(self.server.request("POST", "/items", b"x" * 129)[0], 413)
        self.assertEqual(
            self.server.request("POST", "/items", [b"x" * 80, b"x" * 80], chunked=True)[
                0
            ],
            413,
        )

    def test_health_errors_and_http_semantics(self):
        self.assertEqual(
            json.loads(self.server.request("GET", "/healthz")[2]), {"ok": True}
        )
        self.assertEqual(self.server.request("GET", "/missing")[0], 404)
        self.assertEqual(self.server.request("POST", "/healthz")[0], 405)
        status, _, body = self.server.request("HEAD", "/items/1")
        self.assertEqual((status, body), (200, b""))
        status, _, body = self.server.request("GET", "/empty")
        self.assertEqual((status, body), (204, b""))
        for path in ("/fail", "/invalid"):
            status, _, body = self.server.request("GET", path)
            self.assertEqual(status, 500)
            self.assertEqual(json.loads(body), {"detail": "Internal Server Error"})

    def test_timeout_keeps_capacity_until_actual_completion(self):
        for path in ("/slow", "/blocking"):
            self.assertEqual(self.server.request("GET", path)[0], 504)
            # Both cooperative cancellation cleanup and non-cancellable sync
            # work must retain capacity. Health remains available under load.
            self.assertEqual(self.server.request("GET", "/healthz")[0], 200)
            status, headers, _ = self.server.request("GET", "/items/1")
            self.assertEqual(status, 503)
            self.assertEqual(headers.get("retry-after"), "1")
            time.sleep(0.35)
            self.assertEqual(self.server.request("GET", "/items/1")[0], 200)

    @unittest.skipUnless(os.name == "posix", "requires SIGTERM")
    def test_sigterm_drains_inflight_request_and_closes_lifespan(self):
        server = Server()
        try:
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                response = executor.submit(server.request, "GET", "/drain")
                deadline = time.monotonic() + 3
                while (
                    not (server.root / "started").exists()
                    and time.monotonic() < deadline
                ):
                    time.sleep(0.005)
                self.assertTrue((server.root / "started").exists())
                server.process.send_signal(signal.SIGTERM)
                self.assertEqual(response.result(timeout=5)[0], 200)
            self.assertEqual(server.process.wait(timeout=5), 0)
            self.assertTrue((server.root / "closed").exists())
        finally:
            server.close()


if __name__ == "__main__":
    unittest.main()
