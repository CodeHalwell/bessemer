"""Native JWT bearer authentication: configuration, principal and HTTP contract."""

import base64
import hashlib
import hmac
import importlib.util
import json
import time
import unittest
from typing import Annotated

from bessemer import App, Depends, JWTAuth, Principal, require_scope
from bessemer.auth import validate_jwt
from test_native_controls import request
from test_native_integration import Server

SECRET_ENV = "BESSEMER_TEST_JWT_SECRET"
SECRET = "jwt-test-secret-material-0123456789abcdef"


def encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def token(claims, *, header=None, secret=SECRET, algorithm=hashlib.sha256) -> str:
    header = {"alg": "HS256", "typ": "JWT"} if header is None else header
    signing = (
        encode(json.dumps(header).encode()) + "." + encode(json.dumps(claims).encode())
    )
    tag = hmac.new(secret.encode(), signing.encode(), algorithm).digest()
    return signing + "." + encode(tag)


class JWTConfigurationTests(unittest.TestCase):
    def test_settings_validate_without_reading_secrets(self):
        settings = JWTAuth(
            algorithms=["HS256"],
            secret_env="UNSET_JWT_SECRET",
            issuer="https://issuer.test",
            audience=["api"],
        ).to_dict()
        self.assertEqual(settings["secret_env"], "UNSET_JWT_SECRET")
        self.assertEqual(settings["audience"], ["api"])
        self.assertNotIn("jwks_env", settings)
        for invalid in (
            {"algorithms": ["none"], "secret_env": "S"},
            {"algorithms": ["HS256"]},
            {"algorithms": ["RS256"], "secret_env": "S"},
            {"algorithms": ["RS256"]},
            {"algorithms": ["RS256"], "jwks_env": "A", "jwks_file": "b"},
            {"algorithms": ["HS256"], "secret_env": "S", "jwks_url": "https://x"},
            {"algorithms": ["RS256"], "jwks_url": "http://issuer.test/keys"},
            {"algorithms": ["RS256"], "jwks_url": "https://user@x/keys"},
            {"algorithms": ["RS256"], "jwks_url": "https://x/k", "jwks_file": "f"},
            {"algorithms": ["RS256"], "jwks_file": "f", "jwks_ca_file": "ca.pem"},
            {
                "algorithms": ["RS256"],
                "jwks_url": "https://x/k",
                "jwks_refresh_seconds": 5,
            },
            {
                "algorithms": ["RS256"],
                "jwks_url": "http://127.0.0.1/k",
                "jwks_ca_file": "c",
            },
            {"algorithms": ["HS256"], "secret_env": "S", "leeway_seconds": 301},
            {"algorithms": ["HS256"], "secret_env": "S", "audience": []},
            {"algorithms": ["HS256"], "secret_env": "S", "exclude_paths": ["x"]},
        ):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                validate_jwt(invalid)

    def test_remote_jwks_settings(self):
        settings = JWTAuth(
            algorithms=["RS256"],
            jwks_url="https://login.example.com/keys",
            jwks_ca_file="/etc/ssl/private-ca.pem",
            jwks_refresh_seconds=600,
        ).to_dict()
        self.assertEqual(settings["jwks_url"], "https://login.example.com/keys")
        self.assertEqual(settings["jwks_refresh_seconds"], 600)
        self.assertNotIn("jwks_file", settings)
        local = validate_jwt(
            {"algorithms": ["ES256"], "jwks_url": "http://127.0.0.1:9/k"}
        )
        self.assertEqual(local["jwks_url"], "http://127.0.0.1:9/k")

    def test_principal_scopes_and_require_scope(self):
        principal = Principal(
            {"sub": "u1", "scope": "items.read items.write", "roles": ["Admin"]}
        )
        self.assertEqual(principal.subject, "u1")
        self.assertEqual(principal.scopes, {"items.read", "items.write", "Admin"})
        self.assertEqual(principal["sub"], "u1")
        with self.assertRaises(TypeError):
            principal.claims["sub"] = "other"
        self.assertIs(require_scope("items.read")(principal), principal)
        from bessemer import HTTPException

        with self.assertRaises(HTTPException) as caught:
            require_scope("items.delete")(principal)
        self.assertEqual(caught.exception.status_code, 403)
        with self.assertRaises(ValueError):
            require_scope()

    def test_principal_requires_configured_jwt_and_documents_bearer(self):
        app = App()

        @app.get("/me")
        async def me(user: Principal):
            return {}

        with self.assertRaisesRegex(ValueError, "require App"):
            app.build()
        app = App(
            jwt=JWTAuth(
                algorithms=["HS256"], secret_env=SECRET_ENV, exclude_paths=["/public"]
            )
        )

        @app.get("/me")
        async def secured(
            user: Annotated[Principal, Depends(require_scope("items.read"))],
        ):
            return {}

        @app.get("/public")
        async def public():
            return {}

        document = app.openapi()
        self.assertEqual(
            document["components"]["securitySchemes"]["BearerJWT"],
            {"type": "http", "scheme": "bearer", "bearerFormat": "JWT"},
        )
        self.assertEqual(
            document["paths"]["/me"]["get"]["security"], [{"BearerJWT": []}]
        )
        self.assertEqual(document["paths"]["/public"]["get"]["security"], [])
        self.assertTrue(app._compiled[0].declaration["needs_principal"])
        self.assertFalse(app._compiled[0].declaration["needs_request"])


APPLICATION = f"""
from typing import Annotated
from bessemer import App, Depends, JWTAuth, Principal, require_scope

app = App(jwt=JWTAuth(
    algorithms=["HS256"], secret_env={SECRET_ENV!r}, issuer="https://issuer.test",
    audience="api", exclude_paths=["/healthz", "/public"],
))
app.health("/healthz")

@app.get("/me")
async def me(user: Principal):
    return {{"sub": user.subject, "claims": dict(user.claims)}}

@app.get("/write", dependencies=[Depends(require_scope("items.write"))])
async def write():
    return {{"written": True}}

@app.get("/plain")
async def plain():
    return {{"plain": True}}

@app.get("/public")
async def public(user: Principal):
    return {{"sub": user.subject}}
"""


@unittest.skipUnless(
    importlib.util.find_spec("bessemer._native"), "build the native extension first"
)
class JWTHTTPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = Server(application=APPLICATION, environment={SECRET_ENV: SECRET})

    @classmethod
    def tearDownClass(cls):
        cls.server.close()

    def claims(self, **extra):
        now = int(time.time())
        return {
            "sub": "user-1",
            "iss": "https://issuer.test",
            "aud": "api",
            "exp": now + 60,
            "nbf": now - 1,
            "scope": "items.read",
            **extra,
        }

    def get(self, path, bearer=None, **headers):
        if bearer is not None:
            headers["Authorization"] = f"Bearer {bearer}"
        return request(self.server, "GET", path, headers)

    def test_valid_tokens_reach_handlers_with_their_claims(self):
        claims = self.claims(nested={"tenant": "t1", "ids": [1, 2]})
        good = token(claims)
        for _ in range(2):  # The second request is a verified-cache hit.
            status, _, body = self.get("/me", good)
            self.assertEqual(status, 200)
            self.assertEqual(json.loads(body), {"sub": "user-1", "claims": claims})
        self.assertEqual(self.get("/plain", good)[0], 200)

    def test_missing_and_invalid_tokens_are_rejected_natively(self):
        status, headers, body = self.get("/plain")
        self.assertEqual(status, 401)
        self.assertEqual(headers["www-authenticate"], "Bearer")
        self.assertEqual(headers["cache-control"], "no-store")
        self.assertEqual(json.loads(body), {"detail": "Unauthorized"})
        invalid = [
            token(self.claims(), secret="wrong-secret-material-0123456789abcdef"),
            token(self.claims(exp=int(time.time()) - 120)),
            token(self.claims(nbf=int(time.time()) + 120)),
            token(self.claims(iss="https://evil.test")),
            token(self.claims(aud="other")),
            token(self.claims(), header={"alg": "none"}),
            token(self.claims(), header={"alg": "HS512"}, algorithm=hashlib.sha512),
            "not-a-token",
        ]
        for value in invalid:
            with self.subTest(token=value[:40]):
                status, headers, _ = self.get("/plain", value)
                self.assertEqual(status, 401)
                self.assertEqual(
                    headers["www-authenticate"], 'Bearer error="invalid_token"'
                )
        self.assertEqual(self.get("/plain", Authorization="Basic abc")[0], 401)

    def test_scopes_and_excluded_paths(self):
        self.assertEqual(self.get("/write", token(self.claims()))[0], 403)
        self.assertEqual(
            self.get("/write", token(self.claims(scope="items.write")))[0], 200
        )
        self.assertEqual(self.get("/healthz")[0], 200)
        # Excluded paths are public: a valid token still yields a principal,
        # and a Principal parameter without one is a 401 from Python.
        status, _, body = self.get("/public", token(self.claims()))
        self.assertEqual((status, json.loads(body)), (200, {"sub": "user-1"}))
        status, _, body = self.get("/public", "invalid")
        self.assertEqual(
            (status, json.loads(body)), (401, {"detail": "Not authenticated"})
        )
        self.assertEqual(self.get("/public")[0], 401)


VECTORS = json.loads(
    (
        __import__("pathlib").Path(__file__).parents[1]
        / "fixtures"
        / "jwt_vectors.json"
    ).read_text()
)


def vector(name):
    return ".".join(VECTORS[name])


REMOTE_APPLICATION = """
import os
from bessemer import App, JWTAuth, Principal

app = App(jwt=JWTAuth(
    algorithms=["RS256", "ES256"], jwks_url=os.environ["TEST_JWKS_URL"],
    issuer="https://issuer.test", audience="api", exclude_paths=["/healthz"],
))
app.health("/healthz")

@app.get("/me")
async def me(user: Principal):
    return {"sub": user.subject}
"""


@unittest.skipUnless(
    importlib.util.find_spec("bessemer._native"), "build the native extension first"
)
class RemoteJWKSHTTPTests(unittest.TestCase):
    def test_rotated_keys_are_fetched_without_a_restart(self):
        import http.server
        import threading

        keys = VECTORS["jwks"]["keys"]
        published = {
            "body": json.dumps({"keys": [k for k in keys if k["kid"] == "ec1"]})
        }

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                body = published["body"].encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        issuer = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=issuer.serve_forever, daemon=True).start()
        url = f"http://127.0.0.1:{issuer.server_address[1]}/.well-known/jwks.json"
        server = Server(
            application=REMOTE_APPLICATION, environment={"TEST_JWKS_URL": url}
        )
        try:

            def get(bearer):
                return request(
                    server, "GET", "/me", {"Authorization": f"Bearer {bearer}"}
                )

            status, _, body = get(vector("es256"))
            self.assertEqual((status, json.loads(body)), (200, {"sub": "vector"}))
            # The issuer rotates in an RSA key. The first token naming it is
            # rejected and prompts a refetch; later requests verify.
            published["body"] = json.dumps({"keys": keys})
            self.assertEqual(get(vector("rs256"))[0], 401)
            deadline = time.monotonic() + 10
            while get(vector("rs256"))[0] != 200 and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertEqual(get(vector("rs256"))[0], 200)
        finally:
            server.close()
            issuer.shutdown()
            issuer.server_close()


if __name__ == "__main__":
    unittest.main()
