"""File bodies retain request admission through delivery or abandonment."""

import os
import socket
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from bessemer import App, FileResponse
from bessemer.testclient import TestClient


class FileDeliveryTests(unittest.TestCase):
    @unittest.skipIf(os.name == "nt", "requires bounded loopback send buffers")
    def test_slow_file_holds_admission_until_abandoned(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "large.bin"
            with path.open("wb") as handle:
                handle.truncate(64 * 1024 * 1024)
            app = App(metrics_path="/metrics", metrics_token_env="FILE_METRICS_TOKEN")

            @app.get("/file")
            async def file():
                return FileResponse(path)

            @app.get("/next")
            async def next_request():
                return {"ok": True}

            with (
                patch.dict(
                    os.environ, {"FILE_METRICS_TOKEN": "file-delivery-test-token"}
                ),
                TestClient(app, server_options={"max_in_flight": 1}) as client,
            ):
                slow = socket.socket()
                try:
                    slow.settimeout(5)
                    slow.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1024)
                    slow.connect(("127.0.0.1", client._port))
                    slow.sendall(b"GET /file HTTP/1.1\r\nHost: localhost\r\n\r\n")
                    headers = b""
                    while b"\r\n\r\n" not in headers:
                        headers += slow.recv(512)
                    self.assertTrue(headers.startswith(b"HTTP/1.1 200"))
                    self.assertEqual(client.get("/next").status_code, 503)
                    metrics = client.get(
                        "/metrics",
                        headers={"authorization": "Bearer file-delivery-test-token"},
                    )
                    # Includes the scrape itself and the still-delivering file.
                    self.assertIn("bessemer_http_in_flight 2\n", metrics.text)
                finally:
                    slow.close()
                deadline = time.monotonic() + 3
                while True:
                    response = client.get("/next")
                    if response.status_code != 503 or time.monotonic() >= deadline:
                        break
                    time.sleep(0.01)
                self.assertEqual(response.status_code, 200)
                # Successful file delivery also returns admission, including HEAD.
                self.assertEqual(client.head("/file").status_code, 200)
                self.assertEqual(client.get("/next").status_code, 200)
                small = Path(directory) / "small.txt"
                small.write_bytes(b"delivered")
                path = small
                self.assertEqual(client.get("/file").content, b"delivered")
                self.assertEqual(client.get("/next").status_code, 200)

    @unittest.skipUnless(hasattr(os, "mkfifo"), "requires FIFOs")
    def test_a_path_replaced_by_a_fifo_is_refused_without_waiting(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "report.txt"
            app = App()

            @app.get("/race")
            async def race():
                path.write_bytes(b"report")
                response = FileResponse(path)
                # Replaced between FileResponse's check and the open.
                path.unlink()
                os.mkfifo(path)
                return response

            @app.get("/next")
            async def next_request():
                return {"ok": True}

            with TestClient(app) as client:
                # Were the open to wait for a writer, this one would arrive
                # after eight seconds and let it finish, so the test fails
                # rather than hangs.
                writer = subprocess.Popen(
                    ["sh", "-c", 'sleep 8; exec 3>"$1"', "sh", str(path)]
                )
                try:
                    started = time.monotonic()
                    response = client.get("/race")
                    elapsed = time.monotonic() - started
                finally:
                    writer.kill()
                    writer.wait()
                self.assertEqual(response.status_code, 500)
                self.assertLess(elapsed, 5)
                self.assertEqual(client.get("/next").status_code, 200)
