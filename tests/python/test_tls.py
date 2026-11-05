"""Native TLS termination with ``tls_cert``/``tls_key``."""

import argparse
import http.client
import os
import ssl
import subprocess
import tempfile
import unittest
from unittest import mock

from bessemer import App, Request
from bessemer.config import settings
from bessemer.testclient import TestClient


def certificate(directory):
    cert = os.path.join(directory, "cert.pem")
    key = os.path.join(directory, "key.pem")
    subprocess.run(
        [
            "openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
            "-keyout", key, "-out", cert, "-days", "1",
            "-subj", "/CN=localhost", "-addext", "subjectAltName=DNS:localhost",
        ],
        check=True,
        capture_output=True,
    )  # fmt: skip
    return cert, key


def build_app():
    app = App()

    @app.get("/scheme")
    async def scheme(request: Request):
        return {"scheme": request.scheme, "url": request.url}

    return app


class TLSTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.mkdtemp()
        cls.cert, cls.key = certificate(cls.directory)

    def test_requests_are_served_over_tls(self):
        client = TestClient(
            build_app(),
            server_options={"tls_cert": self.cert, "tls_key": self.key},
        )
        with client:
            context = ssl.create_default_context(cafile=self.cert)
            connection = http.client.HTTPSConnection(
                "localhost", client._port, context=context, timeout=10
            )
            connection.request("GET", "/scheme")
            response = connection.getresponse()
            self.assertEqual(response.status, 200)
            body = response.read()
            self.assertIn(b'"scheme":"https"', body)
            self.assertIn(b'"url":"https://localhost:', body)
            self.assertEqual(connection.sock.version()[:4], "TLSv")
            connection.close()
            # The port does not answer plaintext HTTP.
            plain = http.client.HTTPConnection("localhost", client._port, timeout=10)
            with self.assertRaises((http.client.HTTPException, OSError)):
                plain.request("GET", "/scheme")
                plain.getresponse().read()
            plain.close()
            # TLS 1.2 is refused unless tls_min opts in.
            legacy = ssl.create_default_context(cafile=self.cert)
            legacy.maximum_version = ssl.TLSVersion.TLSv1_2
            connection = http.client.HTTPSConnection(
                "localhost", client._port, context=legacy, timeout=10
            )
            with self.assertRaises(ssl.SSLError):
                connection.request("GET", "/scheme")
            connection.close()

    def test_tls_1_2_can_be_opted_in(self):
        client = TestClient(
            build_app(),
            server_options={
                "tls_cert": self.cert,
                "tls_key": self.key,
                "tls_min": "1.2",
            },
        )
        with client:
            legacy = ssl.create_default_context(cafile=self.cert)
            legacy.maximum_version = ssl.TLSVersion.TLSv1_2
            connection = http.client.HTTPSConnection(
                "localhost", client._port, context=legacy, timeout=10
            )
            connection.request("GET", "/scheme")
            self.assertEqual(connection.getresponse().status, 200)
            self.assertEqual(connection.sock.version(), "TLSv1.2")
            connection.close()

    def test_plain_servers_report_http(self):
        with TestClient(build_app()) as client:
            self.assertEqual(client.get("/scheme").json()["scheme"], "http")

    def test_invalid_tls_configuration_fails_at_startup(self):
        app = build_app()
        with self.assertRaises(ValueError):
            app.run(tls_cert=self.cert)
        with self.assertRaises(ValueError):
            app.run(tls_cert=self.cert, tls_key="/does/not/exist.pem")
        with self.assertRaises(TypeError):
            app.run(tls_cert=self.cert, tls_key=3)
        with self.assertRaises(ValueError):
            app.run(tls_cert=self.cert, tls_key=self.key, tls_min="1.1")
        # A certificate given as its own key is rejected natively at startup.
        client = TestClient(
            build_app(),
            server_options={"tls_cert": self.cert, "tls_key": self.cert},
        )
        with self.assertRaisesRegex(ValueError, "tls_key"):
            client.start()

    def test_settings_accept_paths_from_the_cli_and_environment(self):
        arguments = argparse.Namespace(tls_cert=self.cert, tls_key=self.key)
        resolved = settings(arguments)
        self.assertEqual(
            (resolved["tls_cert"], resolved["tls_key"]), (self.cert, self.key)
        )
        environment = {
            "BESSEMER_TLS_CERT": self.cert,
            "BESSEMER_TLS_KEY": self.key,
        }
        with mock.patch.dict(os.environ, environment):
            resolved = settings(argparse.Namespace())
        self.assertEqual(resolved["tls_key"], self.key)
        with self.assertRaises(ValueError):
            settings(argparse.Namespace(tls_cert=self.cert))
        self.assertIsNone(settings(argparse.Namespace())["tls_cert"])
        self.assertEqual(settings(argparse.Namespace())["tls_min"], "1.3")
        with mock.patch.dict(os.environ, {"BESSEMER_TLS_MIN": "1.2"}):
            self.assertEqual(settings(argparse.Namespace())["tls_min"], "1.2")
        with self.assertRaises(ValueError):
            settings(argparse.Namespace(tls_min="1.0"))


if __name__ == "__main__":
    unittest.main()
