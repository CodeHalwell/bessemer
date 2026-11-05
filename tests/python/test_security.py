"""FastAPI-style security dependencies and their OpenAPI entries."""

import base64
import subprocess
import sys
import textwrap
import unittest
from typing import Annotated

from bessemer import App, Depends
from bessemer.security import (
    APIKeyCookie,
    APIKeyHeader,
    APIKeyQuery,
    HTTPAuthorizationCredentials,
    HTTPBasic,
    HTTPBasicCredentials,
    HTTPBearer,
    OAuth2AuthorizationCodeBearer,
    OAuth2PasswordBearer,
    OAuth2PasswordRequestForm,
    OAuth2PasswordRequestFormStrict,
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


if __name__ == "__main__":
    unittest.main()
