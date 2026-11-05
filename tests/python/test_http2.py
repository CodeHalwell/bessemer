"""HTTP/2 through the native transport, over TLS and by prior knowledge."""

import argparse
import json
import shutil
import socket
import ssl
import subprocess
import tempfile
import unittest

from bessemer import App, Request
from bessemer.config import settings
from bessemer.testclient import TestClient
from test_tls import certificate


def curl_speaks_http2():
    curl = shutil.which("curl")
    if curl is None:
        return False
    version = subprocess.run(
        [curl, "--version"], capture_output=True, text=True, check=False
    ).stdout
    return "HTTP2" in version


def build_app():
    app = App()

    @app.get("/where")
    async def where(request: Request):
        return {"scheme": request.scheme, "host": request.headers["host"]}

    @app.post("/echo")
    async def echo(request: Request):
        return {"length": len(await request.body())}

    return app


def curl(*arguments):
    result = subprocess.run(
        ["curl", "--silent", "--show-error", "--max-time", "10", *arguments],
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    body, _, version = result.stdout.rpartition("\n")
    return json.loads(body), version


class HTTP2Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.mkdtemp()
        cls.cert, cls.key = certificate(cls.directory)

    def alpn(self, port, offered):
        context = ssl.create_default_context(cafile=self.cert)
        context.set_alpn_protocols(offered)
        with (
            socket.create_connection(("localhost", port), timeout=10) as raw,
            context.wrap_socket(raw, server_hostname="localhost") as tls,
        ):
            return tls.selected_alpn_protocol()

    def test_tls_offers_h2_unless_turned_off(self):
        options = {"tls_cert": self.cert, "tls_key": self.key}
        with TestClient(build_app(), server_options=options) as client:
            self.assertEqual(self.alpn(client._port, ["h2", "http/1.1"]), "h2")
            self.assertEqual(self.alpn(client._port, ["http/1.1"]), "http/1.1")
        options = {**options, "http": "1.1"}
        with TestClient(build_app(), server_options=options) as client:
            self.assertEqual(self.alpn(client._port, ["h2", "http/1.1"]), "http/1.1")

    @unittest.skipUnless(curl_speaks_http2(), "requires curl with HTTP/2")
    def test_requests_reach_the_application_over_http2(self):
        options = {"tls_cert": self.cert, "tls_key": self.key}
        with TestClient(build_app(), server_options=options) as client:
            url = f"https://localhost:{client._port}"
            body, version = curl(
                "--http2", "--cacert", self.cert, "-w", "\n%{http_version}",
                f"{url}/where",
            )  # fmt: skip
            self.assertEqual(version, "2")
            self.assertEqual(
                body, {"scheme": "https", "host": f"localhost:{client._port}"}
            )
            body, version = curl(
                "--http2", "--cacert", self.cert, "-w", "\n%{http_version}",
                "--data-binary", "x" * 70_000, f"{url}/echo",
            )  # fmt: skip
            self.assertEqual((body, version), ({"length": 70_000}, "2"))
        with TestClient(build_app()) as client:
            body, version = curl(
                "--http2-prior-knowledge", "-w", "\n%{http_version}",
                f"http://127.0.0.1:{client._port}/where",
            )  # fmt: skip
            self.assertEqual(version, "2")
            self.assertEqual(body["scheme"], "http")
            # HTTP/1.1 clients share the port.
            self.assertEqual(client.get("/where").json()["scheme"], "http")

    def test_http_setting_is_validated(self):
        self.assertEqual(settings(argparse.Namespace())["http"], "auto")
        self.assertEqual(settings(argparse.Namespace(http="1.1"))["http"], "1.1")
        with self.assertRaises(ValueError):
            settings(argparse.Namespace(http="2"))
        with self.assertRaises(ValueError):
            build_app().run(http="h2")


if __name__ == "__main__":
    unittest.main()
