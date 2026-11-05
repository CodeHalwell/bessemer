"""Mutual TLS: client certificates verified against ``tls_client_ca``."""

import argparse
import http.client
import os
import ssl
import subprocess
import tempfile
import time
import unittest

from bessemer import App, Request
from bessemer.config import settings
from bessemer.testclient import TestClient
from test_tls import certificate


def openssl(*arguments):
    subprocess.run(["openssl", *arguments], check=True, capture_output=True)


def client_identity(directory, name):
    """A CA and a client certificate it signed: (CA PEM, client PEM, key)."""
    ca = os.path.join(directory, f"{name}-ca.pem")
    ca_key = os.path.join(directory, f"{name}-ca.key")
    openssl(
        "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
        "-keyout", ca_key, "-out", ca, "-subj", f"/CN={name} CA",
    )  # fmt: skip
    key = os.path.join(directory, f"{name}-client.key")
    request = os.path.join(directory, f"{name}-client.csr")
    cert = os.path.join(directory, f"{name}-client.pem")
    extensions = os.path.join(directory, f"{name}-client.ext")
    with open(extensions, "w") as file:
        file.write("extendedKeyUsage=clientAuth\n")
    openssl(
        "req", "-newkey", "rsa:2048", "-nodes", "-keyout", key,
        "-out", request, "-subj", "/CN=client.test",
    )  # fmt: skip
    openssl(
        "x509", "-req", "-in", request, "-CA", ca, "-CAkey", ca_key,
        "-CAcreateserial", "-out", cert, "-days", "1", "-extfile", extensions,
    )  # fmt: skip
    return ca, cert, key


def certificate_revocation_list(directory, name, revoke=()):
    """A PEM CRL from the ``name`` CA, listing the certificates in ``revoke``."""
    config = os.path.join(directory, f"{name}-crl.cnf")
    index = os.path.join(directory, f"{name}-index.txt")
    number = os.path.join(directory, f"{name}-crlnumber")
    with open(index, "w"):
        pass
    with open(number, "w") as file:
        file.write("01\n")
    with open(config, "w") as file:
        file.write(
            "[ca]\ndefault_ca = default\n[default]\n"
            f"database = {index}\ncrlnumber = {number}\n"
            "default_md = sha256\ndefault_crl_days = 30\n"
        )
    authority = (
        "-cert", os.path.join(directory, f"{name}-ca.pem"),
        "-keyfile", os.path.join(directory, f"{name}-ca.key"),
    )  # fmt: skip
    for certificate_path in revoke:
        openssl("ca", "-config", config, *authority, "-revoke", certificate_path)
    crl = os.path.join(directory, f"{name}-{len(revoke)}.crl.pem")
    openssl("ca", "-config", config, *authority, "-gencrl", "-out", crl)
    return crl


def build_app():
    app = App()

    @app.get("/who")
    async def who(request: Request):
        certificate = request.client_certificate
        return {"certificate": certificate.hex() if certificate else None}

    return app


class MutualTLSTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.mkdtemp()
        cls.cert, cls.key = certificate(cls.directory)
        cls.ca, cls.client_cert, cls.client_key = client_identity(
            cls.directory, "trusted"
        )
        _, cls.stranger_cert, cls.stranger_key = client_identity(
            cls.directory, "stranger"
        )
        with open(cls.client_cert) as file:
            cls.client_der = ssl.PEM_cert_to_DER_cert(file.read())

    def get(self, port, identity=None):
        context = ssl.create_default_context(cafile=self.cert)
        if identity is not None:
            context.load_cert_chain(*identity)
        connection = http.client.HTTPSConnection(
            "localhost", port, context=context, timeout=10
        )
        try:
            connection.request("GET", "/who")
            response = connection.getresponse()
            return response.status, response.read()
        finally:
            connection.close()

    def options(self, **extra):
        return {
            "tls_cert": self.cert,
            "tls_key": self.key,
            "tls_client_ca": self.ca,
            **extra,
        }

    def test_required_client_certificates(self):
        trusted = (self.client_cert, self.client_key)
        stranger = (self.stranger_cert, self.stranger_key)
        with TestClient(build_app(), server_options=self.options()) as client:
            status, body = self.get(client._port, trusted)
            self.assertEqual(status, 200)
            self.assertIn(self.client_der.hex().encode(), body)
            for identity in (None, stranger):
                with self.assertRaises(
                    (ssl.SSLError, OSError, http.client.HTTPException)
                ):
                    self.get(client._port, identity)

    def test_optional_client_certificates(self):
        options = self.options(tls_client_auth="optional")
        with TestClient(build_app(), server_options=options) as client:
            self.assertEqual(self.get(client._port), (200, b'{"certificate":null}'))
            status, body = self.get(client._port, (self.client_cert, self.client_key))
            self.assertEqual(status, 200)
            self.assertIn(self.client_der.hex().encode(), body)
            with self.assertRaises((ssl.SSLError, OSError, http.client.HTTPException)):
                self.get(client._port, (self.stranger_cert, self.stranger_key))

    def test_revoked_client_certificates_are_refused(self):
        trusted = (self.client_cert, self.client_key)
        current = certificate_revocation_list(self.directory, "trusted")
        revoked = certificate_revocation_list(
            self.directory, "trusted", revoke=[self.client_cert]
        )
        crl = os.path.join(self.directory, "client.crl.pem")
        with open(current, "rb") as source, open(crl, "wb") as target:
            target.write(source.read())
        options = self.options(tls_client_crl=crl, tls_reload_interval=0.1)
        with TestClient(build_app(), server_options=options) as client:
            status, body = self.get(client._port, trusted)
            self.assertEqual(status, 200)
            self.assertIn(self.client_der.hex().encode(), body)
            # Revoking the certificate takes effect without a restart.
            with open(revoked, "rb") as source, open(crl, "wb") as target:
                target.write(source.read())
            deadline = time.monotonic() + 10
            while True:
                try:
                    self.get(client._port, trusted)
                except (ssl.SSLError, OSError, http.client.HTTPException):
                    break
                self.assertLess(time.monotonic(), deadline, "revocation not applied")
                time.sleep(0.1)

    def test_invalid_configuration(self):
        app = build_app()
        with self.assertRaises(ValueError):
            app.run(tls_client_ca=self.ca)
        with self.assertRaises(ValueError):
            app.run(tls_cert=self.cert, tls_key=self.key, tls_client_ca="/missing.pem")
        with self.assertRaises(ValueError):
            app.run(
                tls_cert=self.cert,
                tls_key=self.key,
                tls_client_ca=self.ca,
                tls_client_auth="sometimes",
            )
        # A file without certificates is rejected natively at startup.
        client = TestClient(
            build_app(), server_options=self.options(tls_client_ca=self.key)
        )
        with self.assertRaisesRegex(ValueError, "tls_client_ca"):
            client.start()
        with self.assertRaises(ValueError):
            settings(argparse.Namespace(tls_client_ca=self.ca))
        # A CRL only applies alongside a client CA, and must exist and parse.
        crl = certificate_revocation_list(self.directory, "stranger")
        with self.assertRaisesRegex(ValueError, "tls_client_crl needs"):
            app.run(tls_cert=self.cert, tls_key=self.key, tls_client_crl=crl)
        with self.assertRaisesRegex(ValueError, "tls_client_crl"):
            app.run(**self.options(tls_client_crl="/missing.crl"))
        client = TestClient(
            build_app(), server_options=self.options(tls_client_crl=self.ca)
        )
        with self.assertRaisesRegex(ValueError, "tls_client_crl"):
            client.start()
        with self.assertRaisesRegex(ValueError, "tls_client_crl needs"):
            settings(
                argparse.Namespace(
                    tls_cert=self.cert, tls_key=self.key, tls_client_crl=crl
                )
            )
        resolved = settings(
            argparse.Namespace(
                tls_cert=self.cert,
                tls_key=self.key,
                tls_client_ca=self.ca,
                tls_client_auth="optional",
            )
        )
        self.assertEqual(
            (resolved["tls_client_ca"], resolved["tls_client_auth"]),
            (self.ca, "optional"),
        )


if __name__ == "__main__":
    unittest.main()
