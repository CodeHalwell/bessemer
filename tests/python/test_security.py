"""FastAPI-style security dependencies and their OpenAPI entries."""

import asyncio
import base64
import subprocess
import sys
import textwrap
import unittest
from typing import Annotated

from bessemer import App, Depends
from bessemer.request import Request
from bessemer.responses import HTTPException
from bessemer.security import (
    APIKeyCookie,
    APIKeyHeader,
    APIKeyQuery,
    HTTPAuthorizationCredentials,
    HTTPBasic,
    HTTPBasicCredentials,
    HTTPBearer,
    HTTPDigest,
    OAuth2,
    OAuth2AuthorizationCodeBearer,
    OAuth2PasswordBearer,
    OAuth2PasswordRequestForm,
    OAuth2PasswordRequestFormStrict,
    OpenIdConnect,
    SecurityBase,
)
from bessemer.testclient import TestClient

bearer = HTTPBearer(bearerFormat="opaque", description="Session tokens")
optional_bearer = HTTPBearer(auto_error=False, scheme_name="OptionalBearer")
basic = HTTPBasic(realm="admin")
oauth2 = OAuth2PasswordBearer("/token", scopes={"items:read": "Read items"})
code = OAuth2AuthorizationCodeBearer("https://id.test/authorize", "/token")
header_key = APIKeyHeader(name="X-Key")
query_key = APIKeyQuery(name="key")
cookie_key = APIKeyCookie(name="session")


def build_app():
    app = App()

    @app.get("/bearer")
    async def read_bearer(
        credentials: Annotated[HTTPAuthorizationCredentials, Depends(bearer)],
    ):
        return {"scheme": credentials.scheme, "token": credentials.credentials}

    @app.get("/optional")
    async def read_optional(
        credentials: Annotated[
            HTTPAuthorizationCredentials | None, Depends(optional_bearer)
        ],
    ):
        return {"token": credentials and credentials.credentials}

    @app.get("/basic")
    async def read_basic(
        credentials: Annotated[HTTPBasicCredentials, Depends(basic)],
    ):
        return {"user": credentials.username, "password": credentials.password}

    @app.get("/oauth2")
    async def read_oauth2(token: Annotated[str, Depends(oauth2)]):
        return {"token": token}

    @app.get("/code")
    async def read_code(token: Annotated[str, Depends(code)]):
        return {"token": token}

    @app.get("/keys", dependencies=[Depends(query_key), Depends(cookie_key)])
    async def read_keys(key: Annotated[str, Depends(header_key)]):
        return {"key": key}

    @app.post("/token")
    async def token(form: Annotated[OAuth2PasswordRequestForm, Depends()]):
        return {
            "username": form.username,
            "password": form.password,
            "scopes": form.scopes,
            "client_id": form.client_id,
        }

    @app.post("/strict-token")
    async def strict(form: Annotated[OAuth2PasswordRequestFormStrict, Depends()]):
        return {"grant_type": form.grant_type}

    @app.get("/open")
    async def open_route():
        return {}

    return app


class SecurityDependencyTests(unittest.TestCase):
    def test_duplicate_credential_headers_are_rejected_even_when_optional(self):
        for scheme, name in [
            (HTTPBearer(auto_error=False), "authorization"),
            (APIKeyHeader(name="X-Key", auto_error=False), "x-key"),
        ]:
            request = Request({"headers": [(name, "first"), (name, "second")]})
            with self.subTest(name=name), self.assertRaises(HTTPException) as caught:
                asyncio.run(scheme(request))
            self.assertEqual(caught.exception.status_code, 400)

    @classmethod
    def setUpClass(cls):
        cls.app = build_app()
        cls.client = TestClient(cls.app)
        cls.client.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.client.__exit__(None, None, None)

    def assertChallenge(self, response, challenge):
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.headers["www-authenticate"], challenge)

    def test_bearer(self):
        response = self.client.get("/bearer", headers={"authorization": "Bearer abc"})
        self.assertEqual(response.json(), {"scheme": "Bearer", "token": "abc"})
        self.assertChallenge(self.client.get("/bearer"), "Bearer")
        wrong = self.client.get("/bearer", headers={"authorization": "Basic abc"})
        self.assertChallenge(wrong, "Bearer")
        self.assertEqual(wrong.json(), {"detail": "Invalid authentication credentials"})
        self.assertChallenge(
            self.client.get("/bearer", headers={"authorization": "Bearer"}), "Bearer"
        )

    def test_optional_schemes_return_none(self):
        self.assertEqual(self.client.get("/optional").json(), {"token": None})
        response = self.client.get("/optional", headers={"authorization": "Bearer t"})
        self.assertEqual(response.json(), {"token": "t"})

    def test_basic(self):
        encoded = base64.b64encode(b"ada:pass:word").decode()
        response = self.client.get(
            "/basic", headers={"authorization": f"Basic {encoded}"}
        )
        self.assertEqual(response.json(), {"user": "ada", "password": "pass:word"})
        challenge = 'Basic realm="admin"'
        self.assertChallenge(self.client.get("/basic"), challenge)
        for value in (
            "Basic !!!",
            "Basic " + base64.b64encode(b"nocolon").decode(),
            "Basic é",
            "Basic " + base64.b64encode(b"\xff:\xfe").decode(),
        ):
            self.assertChallenge(
                self.client.get("/basic", headers={"authorization": value}), challenge
            )

    def test_oauth2_bearers(self):
        for path in ("/oauth2", "/code"):
            response = self.client.get(path, headers={"authorization": "bearer tok"})
            self.assertEqual(response.json(), {"token": "tok"})
            self.assertChallenge(self.client.get(path), "Bearer")

    def test_api_keys(self):
        response = self.client.get(
            "/keys?key=q",
            headers={"x-key": "h", "cookie": "session=c"},
        )
        self.assertEqual(response.json(), {"key": "h"})
        self.assertChallenge(
            self.client.get("/keys?key=q", headers={"cookie": "session=c"}), "APIKey"
        )
        self.assertChallenge(
            self.client.get("/keys", headers={"x-key": "h", "cookie": "session=c"}),
            "APIKey",
        )

    def test_password_forms(self):
        response = self.client.post(
            "/token",
            data={
                "username": "ada",
                "password": "pw",
                "scope": "items:read  items:write",
                "client_id": "cli",
            },
        )
        self.assertEqual(
            response.json(),
            {
                "username": "ada",
                "password": "pw",
                "scopes": ["items:read", "items:write"],
                "client_id": "cli",
            },
        )
        self.assertEqual(
            self.client.post("/token", data={"username": "a"}).status_code, 422
        )
        self.assertEqual(
            self.client.post(
                "/token", data={"username": "a", "password": "b", "grant_type": "x"}
            ).status_code,
            422,
        )
        strict = self.client.post(
            "/strict-token", data={"username": "a", "password": "b"}
        )
        self.assertEqual(strict.status_code, 422)
        strict = self.client.post(
            "/strict-token",
            data={"username": "a", "password": "b", "grant_type": "password"},
        )
        self.assertEqual(strict.json(), {"grant_type": "password"})

    def test_openapi_lists_the_schemes(self):
        document = self.app.openapi()
        schemes = document["components"]["securitySchemes"]
        self.assertEqual(
            schemes["HTTPBearer"],
            {
                "type": "http",
                "scheme": "bearer",
                "bearerFormat": "opaque",
                "description": "Session tokens",
            },
        )
        self.assertEqual(schemes["HTTPBasic"], {"type": "http", "scheme": "basic"})
        self.assertEqual(
            schemes["OAuth2PasswordBearer"]["flows"]["password"],
            {"tokenUrl": "/token", "scopes": {"items:read": "Read items"}},
        )
        self.assertIn(
            "authorizationCode", schemes["OAuth2AuthorizationCodeBearer"]["flows"]
        )
        self.assertEqual(
            schemes["APIKeyCookie"],
            {"type": "apiKey", "in": "cookie", "name": "session"},
        )
        paths = document["paths"]
        self.assertEqual(paths["/bearer"]["get"]["security"], [{"HTTPBearer": []}])
        self.assertEqual(
            paths["/keys"]["get"]["security"],
            [{"APIKeyQuery": [], "APIKeyCookie": [], "APIKeyHeader": []}],
        )
        self.assertNotIn("security", paths["/open"]["get"])


class SecuritySchemeEdgeTests(unittest.TestCase):
    def test_overridden_schemes_stay_documented(self):
        app = App()

        @app.get("/")
        async def root(token: Annotated[str, Depends(oauth2)]):
            return {"token": token}

        app.dependency_overrides[oauth2] = lambda: "fake"
        with TestClient(app) as client:
            self.assertEqual(client.get("/").json(), {"token": "fake"})
        self.assertEqual(
            app.openapi()["paths"]["/"]["get"]["security"],
            [{"OAuth2PasswordBearer": []}],
        )

    def test_nested_schemes_stay_documented_when_wrappers_are_overridden(self):
        app = App()

        async def current_user(
            credentials: Annotated[HTTPAuthorizationCredentials, Depends(bearer)],
        ):
            return credentials.credentials

        @app.get("/")
        async def root(user: Annotated[str, Depends(current_user)]):
            return {"user": user}

        app.dependency_overrides[current_user] = lambda: "tester"
        with TestClient(app) as client:
            self.assertEqual(client.get("/").json(), {"user": "tester"})
        self.assertEqual(
            app.openapi()["paths"]["/"]["get"]["security"], [{"HTTPBearer": []}]
        )

    def test_password_forms_build_without_pydantic(self):
        script = textwrap.dedent(
            """
            import sys

            class Block:
                def find_spec(self, name, path=None, target=None):
                    if name.split(".")[0] in ("pydantic", "pydantic_core"):
                        raise ImportError("blocked")

            sys.meta_path.insert(0, Block())
            from typing import Annotated
            from bessemer import App, Depends
            from bessemer.security import (
                OAuth2PasswordRequestForm,
                OAuth2PasswordRequestFormStrict,
            )

            app = App()

            @app.post("/a")
            async def a(form: Annotated[OAuth2PasswordRequestForm, Depends()]):
                return {}

            @app.post("/b")
            async def b(form: Annotated[OAuth2PasswordRequestFormStrict, Depends()]):
                return {}

            app.build()
            """
        )
        result = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_conflicting_scheme_names_fail(self):
        app = App()

        @app.get("/a")
        async def a(key: Annotated[str, Depends(APIKeyHeader(name="A"))]):
            return {}

        @app.get("/b")
        async def b(key: Annotated[str, Depends(APIKeyHeader(name="B"))]):
            return {}

        with self.assertRaises(ValueError):
            app.build()

    def test_invalid_configuration(self):
        with self.assertRaises(ValueError):
            OAuth2PasswordBearer("")
        with self.assertRaises(ValueError):
            APIKeyHeader(name="")
        with self.assertRaises(TypeError):
            HTTPBearer(auto_error="yes")
        with self.assertRaises(ValueError):
            HTTPBasic(realm='a"b')
        with self.assertRaises(TypeError):
            OAuth2PasswordBearer("/t", scopes={"a": 1})
        with self.assertRaises(ValueError):
            OAuth2(flows={"unknown": {}})
        with self.assertRaises(TypeError):
            OAuth2(flows="not-a-dict")
        with self.assertRaises(ValueError):
            OpenIdConnect(openIdConnectUrl="")

    def test_oauth2_base_digest_and_openid_connect(self):
        self.assertTrue(issubclass(OAuth2PasswordBearer, OAuth2))
        self.assertTrue(issubclass(OAuth2AuthorizationCodeBearer, OAuth2))
        self.assertTrue(issubclass(OAuth2, SecurityBase))
        # Positional arguments match FastAPI's signature.
        positional = OAuth2PasswordBearer("/tok", "PositionalOAuth2", {"r": "Read"})
        self.assertEqual(positional.scheme_name, "PositionalOAuth2")

        raw_oauth2 = OAuth2(
            flows={"clientCredentials": {"tokenUrl": "/token", "scopes": {"s": "S"}}},
            description="OAuth2 base",
        )
        opt_oauth2 = OAuth2(auto_error=False, scheme_name="OptOAuth2")
        digest = HTTPDigest(description="Digest auth")
        opt_digest = HTTPDigest(auto_error=False, scheme_name="OptDigest")
        oidc = OpenIdConnect(
            openIdConnectUrl="https://id.test/.well-known/openid-configuration"
        )
        opt_oidc = OpenIdConnect(
            openIdConnectUrl="https://id.test/.well-known/openid-configuration",
            auto_error=False,
            scheme_name="OptOIDC",
        )

        app = App()

        @app.get("/raw-oauth2")
        async def r_oauth2(auth: Annotated[str, Depends(raw_oauth2)]):
            return {"auth": auth}

        @app.get("/opt-oauth2")
        async def r_opt_oauth2(auth: Annotated[str | None, Depends(opt_oauth2)]):
            return {"auth": auth}

        @app.get("/digest")
        async def r_digest(
            creds: Annotated[HTTPAuthorizationCredentials, Depends(digest)],
        ):
            return {"scheme": creds.scheme, "credentials": creds.credentials}

        @app.get("/opt-digest")
        async def r_opt_digest(
            creds: Annotated[HTTPAuthorizationCredentials | None, Depends(opt_digest)],
        ):
            return {"credentials": creds and creds.credentials}

        @app.get("/oidc")
        async def r_oidc(auth: Annotated[str, Depends(oidc)]):
            return {"auth": auth}

        @app.get("/opt-oidc")
        async def r_opt_oidc(auth: Annotated[str | None, Depends(opt_oidc)]):
            return {"auth": auth}

        with TestClient(app) as client:
            schemes = app.openapi()["components"]["securitySchemes"]
            self.assertEqual(
                schemes["OAuth2"],
                {
                    "type": "oauth2",
                    "description": "OAuth2 base",
                    "flows": {
                        "clientCredentials": {
                            "tokenUrl": "/token",
                            "scopes": {"s": "S"},
                        }
                    },
                },
            )
            self.assertEqual(
                schemes["HTTPDigest"],
                {"type": "http", "scheme": "digest", "description": "Digest auth"},
            )
            self.assertEqual(
                schemes["OpenIdConnect"],
                {
                    "type": "openIdConnect",
                    "openIdConnectUrl": "https://id.test/.well-known/openid-configuration",
                },
            )
            self.assertEqual(
                client.get(
                    "/raw-oauth2", headers={"authorization": "Bearer xyz"}
                ).json(),
                {"auth": "Bearer xyz"},
            )
            self.assertEqual(client.get("/raw-oauth2").status_code, 401)
            self.assertEqual(client.get("/opt-oauth2").json(), {"auth": None})
            self.assertEqual(
                client.get(
                    "/digest", headers={"authorization": 'Digest username="ada"'}
                ).json(),
                {"scheme": "Digest", "credentials": 'username="ada"'},
            )
            self.assertEqual(client.get("/digest").status_code, 401)
            self.assertEqual(
                client.get(
                    "/digest", headers={"authorization": "Bearer x"}
                ).status_code,
                401,
            )
            self.assertEqual(client.get("/opt-digest").json(), {"credentials": None})
            self.assertEqual(
                client.get("/oidc", headers={"authorization": "Bearer id-tok"}).json(),
                {"auth": "Bearer id-tok"},
            )
            self.assertEqual(client.get("/oidc").status_code, 401)
            self.assertEqual(client.get("/opt-oidc").json(), {"auth": None})


if __name__ == "__main__":
    unittest.main()
