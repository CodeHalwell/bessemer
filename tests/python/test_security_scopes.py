"""FastAPI-style Security() dependencies and SecurityScopes."""

import unittest
from typing import Annotated

from bessemer import App, Depends, HTTPException, Security
from bessemer.security import HTTPBearer, OAuth2PasswordBearer, SecurityScopes
from bessemer.testclient import TestClient

oauth2 = OAuth2PasswordBearer(
    "/token", scopes={"me": "Read yourself", "items": "Read items"}
)
bearer = HTTPBearer()
# Tokens are "user:scope scope".
calls = []


async def current_user(
    security_scopes: SecurityScopes, token: Annotated[str, Depends(oauth2)]
):
    calls.append(tuple(security_scopes.scopes))
    user, _, granted = token.partition(":")
    for scope in security_scopes.scopes:
        if scope not in granted.split():
            raise HTTPException(
                401,
                "Not enough permissions",
                headers={
                    "WWW-Authenticate": f'Bearer scope="{security_scopes.scope_str}"'
                },
            )
    return user


async def active_user(user: Annotated[str, Security(current_user, scopes=["me"])]):
    return user


def build_app():
    app = App()

    @app.get("/me")
    async def me(user: Annotated[str, Depends(active_user)]):
        return {"user": user}

    @app.get("/items")
    async def items(
        user: Annotated[str, Security(active_user, scopes=["items", "me"])],
    ):
        return {"user": user}

    @app.get("/both")
    async def both(
        plain: Annotated[str, Depends(current_user)],
        scoped: Annotated[str, Security(current_user, scopes=["items"])],
        again: Annotated[str, Security(current_user, scopes=["items"])],
    ):
        return {"users": [plain, scoped, again]}

    @app.get("/scopes")
    async def scopes(security_scopes: SecurityScopes):
        return {"scopes": security_scopes.scopes}

    @app.get("/guarded", dependencies=[Security(current_user, scopes=["admin"])])
    async def guarded():
        return {"ok": True}

    @app.get("/bearer")
    async def plain_bearer(
        credentials: Annotated[object, Security(bearer, scopes=["ignored"])],
    ):
        return {"ok": credentials is not None}

    return app


class SecurityScopesTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = build_app()
        cls.client = TestClient(cls.app)
        cls.client.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.client.__exit__(None, None, None)

    def get(self, path, token=None):
        headers = {"authorization": f"Bearer {token}"} if token else {}
        return self.client.get(path, headers=headers)

    def test_scopes_accumulate_from_the_outermost_security(self):
        calls.clear()
        self.assertEqual(self.get("/me", "ann:me").json(), {"user": "ann"})
        self.assertEqual(self.get("/items", "ann:me items").json(), {"user": "ann"})
        self.assertEqual(calls, [("me",), ("items", "me")])
        denied = self.get("/items", "ann:me")
        self.assertEqual(denied.status_code, 401)
        self.assertEqual(denied.headers["www-authenticate"], 'Bearer scope="items me"')
        self.assertEqual(self.get("/me").status_code, 401)

    def test_each_scope_set_is_its_own_cached_dependency(self):
        calls.clear()
        response = self.get("/both", "bob:items")
        self.assertEqual(response.json(), {"users": ["bob", "bob", "bob"]})
        # Depends() and Security(scopes=["items"]) are two entries; the
        # repeated Security() shares the second.
        self.assertEqual(calls, [(), ("items",)])

    def test_security_scopes_without_security_is_empty(self):
        self.assertEqual(self.get("/scopes").json(), {"scopes": []})

    def test_route_dependencies_accept_security(self):
        self.assertEqual(self.get("/guarded", "cy:admin").json(), {"ok": True})
        self.assertEqual(self.get("/guarded", "cy:me").status_code, 401)

    def test_openapi_requirements_list_the_scopes(self):
        paths = self.app.openapi()["paths"]

        def requirement(path):
            return paths[path]["get"]["security"]

        self.assertEqual(requirement("/me"), [{"OAuth2PasswordBearer": ["me"]}])
        self.assertEqual(
            requirement("/items"), [{"OAuth2PasswordBearer": ["items", "me"]}]
        )
        self.assertEqual(requirement("/both"), [{"OAuth2PasswordBearer": ["items"]}])
        self.assertEqual(requirement("/guarded"), [{"OAuth2PasswordBearer": ["admin"]}])
        # Scopes only belong to OAuth2 and OpenID Connect requirements.
        self.assertEqual(requirement("/bearer"), [{"HTTPBearer": []}])

    def test_overridden_security_keeps_its_scopes_documented(self):
        app = App()

        @app.get("/")
        async def root(user: Annotated[str, Security(current_user, scopes=["me"])]):
            return {"user": user}

        app.dependency_overrides[current_user] = lambda: "fake"
        with TestClient(app) as client:
            self.assertEqual(client.get("/").json(), {"user": "fake"})
        self.assertEqual(
            app.openapi()["paths"]["/"]["get"]["security"],
            [{"OAuth2PasswordBearer": ["me"]}],
        )

    def test_invalid_declarations(self):
        for scopes in ("me", [""], [1], None):
            with self.assertRaises(TypeError, msg=repr(scopes)):
                Security(current_user, scopes=scopes)
        self.assertEqual(Security(current_user, scopes=["a"]).scopes, ("a",))
        app = App()

        async def shared(security_scopes: SecurityScopes):
            return security_scopes.scopes

        @app.get("/")
        async def root(value: Annotated[list, Depends(shared, scope="app")]):
            return value

        with self.assertRaisesRegex(ValueError, "request-scoped"):
            app.openapi()


if __name__ == "__main__":
    unittest.main()
