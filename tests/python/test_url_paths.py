"""Route names, ``url_path_for``/``url_for`` and ``response_model_*`` options."""

import tempfile
import unittest

from bessemer import App, NoMatchFound, Request, Router, WebSocket
from bessemer.testclient import TestClient

try:
    import pydantic
except ImportError:  # pragma: no cover - the optional dependency is absent
    pydantic = None


def build_app():
    app = App()
    users = Router(prefix="/users")

    @users.get("/{user_id}")
    async def read_user(user_id: int):
        return {"id": user_id}

    @users.get("/{user_id}/posts/{post_id}", name="user-post")
    async def post(user_id: int, post_id: str):
        return {}

    app.include_router(users, prefix="/v1")

    @app.get("/links")
    async def links(request: Request):
        return {
            "user": request.url_for("read_user", user_id=7),
            "file": request.url_for("assets", path="/css/site.css"),
        }

    @app.websocket("/ws/{room}")
    async def chat(websocket: WebSocket):
        await websocket.close()

    root = tempfile.mkdtemp()
    app.static("/assets", root, name="assets")
    return app


class URLPathForTests(unittest.TestCase):
    def setUp(self):
        self.app = build_app()

    def test_paths_are_built_from_names(self):
        self.assertEqual(self.app.url_path_for("read_user", user_id=3), "/v1/users/3")
        self.assertEqual(
            self.app.url_path_for("user-post", user_id=3, post_id="a b/c"),
            "/v1/users/3/posts/a%20b%2Fc",
        )
        self.assertEqual(self.app.url_path_for("links"), "/links")
        self.assertEqual(self.app.url_path_for("chat", room="lobby"), "/ws/lobby")

    def test_static_mounts_take_the_file_as_path(self):
        self.assertEqual(
            self.app.url_path_for("assets", path="/css/site file.css"),
            "/assets/css/site%20file.css",
        )
        self.assertEqual(
            self.app.url_path_for("assets", path="js/app.js"), "/assets/js/app.js"
        )

    def test_unknown_names_and_wrong_parameters_raise(self):
        with self.assertRaises(NoMatchFound):
            self.app.url_path_for("nothing")
        with self.assertRaises(NoMatchFound):
            self.app.url_path_for("read_user")
        with self.assertRaises(NoMatchFound):
            self.app.url_path_for("read_user", user_id=1, extra=2)
        with self.assertRaises(NoMatchFound):
            self.app.url_path_for("assets")

    def test_routers_resolve_their_own_routes(self):
        router = Router(prefix="/r")

        @router.get("/{name}")
        async def item(name: str):
            return {}

        self.assertEqual(router.url_path_for("item", name="x"), "/r/x")

    def test_invalid_names(self):
        app = App()
        with self.assertRaises(ValueError):
            app.get("/", name="")
        with self.assertRaises(ValueError):
            app.static("/s", tempfile.mkdtemp(), name="")

    def test_request_and_websocket_url_for(self):
        with TestClient(self.app) as client:
            response = client.get("/links")
            self.assertEqual(
                response.json(),
                {
                    "user": "http://testserver/v1/users/7",
                    "file": "http://testserver/assets/css/site.css",
                },
            )
        websocket = WebSocket(
            None, Request({"headers": [("host", "chat.test")]}, app=self.app), None
        )
        self.assertEqual(
            websocket.url_for("read_user", user_id=1), "http://chat.test/v1/users/1"
        )
        with self.assertRaises(RuntimeError):
            Request().url_for("read_user", user_id=1)


@unittest.skipIf(pydantic is None, "requires the optional Pydantic dependency")
class ResponseModelOptionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        class Item(pydantic.BaseModel):
            name: str
            description: str | None = None
            price: float = 1.0
            tax: float | None = None
            internal_code: str = pydantic.Field("x", alias="internalCode")

        app = App()
        raw = {"name": "a", "tax": None, "internalCode": "z"}

        @app.get("/unset", response_model=Item, response_model_exclude_unset=True)
        async def unset():
            return raw

        @app.get("/none", response_model=Item, response_model_exclude_none=True)
        async def none():
            return Item(name="a", internalCode="z")

        @app.get("/defaults", response_model=Item, response_model_exclude_defaults=True)
        async def defaults():
            return Item(name="a", price=1.0, internalCode="q")

        @app.get(
            "/include",
            response_model=Item,
            response_model_include={"name", "price"},
        )
        async def include():
            return raw

        @app.get("/exclude", response_model=Item, response_model_exclude={"tax"})
        async def exclude():
            return raw

        @app.get("/names", response_model=Item, response_model_by_alias=False)
        async def names():
            return raw

        @app.get("/plain", response_model=Item)
        async def plain():
            return raw

        cls.client = TestClient(app)
        cls.client.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.client.__exit__(None, None, None)

    def get(self, path):
        response = self.client.get(path)
        self.assertEqual(response.status_code, 200)
        return response.json()

    def test_options_shape_the_serialized_model(self):
        self.assertEqual(
            self.get("/unset"), {"name": "a", "tax": None, "internalCode": "z"}
        )
        self.assertEqual(
            self.get("/none"), {"name": "a", "price": 1.0, "internalCode": "z"}
        )
        self.assertEqual(self.get("/defaults"), {"name": "a", "internalCode": "q"})
        self.assertEqual(self.get("/include"), {"name": "a", "price": 1.0})
        self.assertNotIn("tax", self.get("/exclude"))
        self.assertIn("internal_code", self.get("/names"))
        self.assertEqual(
            self.get("/plain"),
            {
                "name": "a",
                "description": None,
                "price": 1.0,
                "tax": None,
                "internalCode": "z",
            },
        )

    def test_invalid_options(self):
        app = App()
        with self.assertRaises(TypeError):
            app.get("/", response_model_exclude_none=1)
        with self.assertRaises(TypeError):
            app.get("/", response_model_include=["name"])


if __name__ == "__main__":
    unittest.main()
