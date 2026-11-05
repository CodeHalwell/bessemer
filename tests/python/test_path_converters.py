"""Starlette-style path converters: ``{name:path|int|float|uuid|str}``."""

import unittest
import uuid

from bessemer import App, Router
from bessemer.routing import APIRoute
from bessemer.testclient import TestClient

ITEM = uuid.UUID("0b5e3c9a-2a1f-4b7e-9c3d-1e2f3a4b5c6d")


def build_app():
    app = App()

    @app.get("/files/{file_path:path}")
    async def read_file(file_path: str):
        return {"path": file_path}

    @app.get("/items/{item_id:int}")
    async def read_item(item_id: int):
        return {"id": item_id}

    @app.get("/as-text/{item_id:int}")
    async def as_text(item_id: str):
        return {"id": item_id}

    @app.get("/prices/{price:float}")
    async def read_price(price: float):
        return {"price": price}

    @app.get("/things/{thing:uuid}")
    async def read_thing(thing: uuid.UUID):
        return {"thing": str(thing), "type": type(thing).__name__}

    @app.get("/names/{name:str}")
    async def read_name(name: str):
        return {"name": name}

    # Literal routes beside converter routes, and a second method on the same
    # converter path.
    @app.get("/items/latest")
    async def latest():
        return {"latest": True}

    @app.delete("/items/{item_id:int}")
    async def delete_item(item_id: int):
        return {"deleted": item_id}

    api = Router(prefix="/api/{version:int}")

    @api.get("/blobs/{key:path}")
    async def blob(version: int, key: str):
        return {"version": version, "key": key}

    app.include_router(api, prefix="/v")

    sub = App()

    @sub.get("/docs/{page:path}")
    async def page(page: str):
        return {"page": page}

    app.mount("/sub", sub, name="sub")
    return app


class ConverterRequestTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(build_app())
        cls.client.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.client.__exit__(None, None, None)

    def get(self, path):
        response = self.client.get(path)
        return response.status_code, response.json()

    def test_path_takes_the_remainder_with_slashes(self):
        self.assertEqual(self.get("/files/a/b/c.txt"), (200, {"path": "a/b/c.txt"}))
        self.assertEqual(self.get("/files/a%20b/c"), (200, {"path": "a b/c"}))
        # As in Starlette, an empty remainder matches; the bare prefix does not.
        self.assertEqual(self.get("/files/"), (200, {"path": ""}))
        self.assertEqual(self.client.get("/files").status_code, 404)

    def test_int(self):
        self.assertEqual(self.get("/items/42"), (200, {"id": 42}))
        self.assertEqual(self.get("/items/latest"), (200, {"latest": True}))
        self.assertEqual(self.client.delete("/items/7").json(), {"deleted": 7})
        for bad in ("/items/abc", "/items/-1", "/items/4.2"):
            self.assertEqual(self.client.get(bad).status_code, 404, bad)
            self.assertEqual(self.client.delete(bad).status_code, 404, bad)

    def test_the_handler_annotation_sets_the_type(self):
        self.assertEqual(self.get("/as-text/0042"), (200, {"id": "0042"}))
        self.assertEqual(self.client.get("/as-text/x").status_code, 404)

    def test_float(self):
        self.assertEqual(self.get("/prices/1.5"), (200, {"price": 1.5}))
        self.assertEqual(self.get("/prices/3"), (200, {"price": 3.0}))
        for bad in ("/prices/abc", "/prices/1e3", "/prices/.5", "/prices/nan"):
            self.assertEqual(self.client.get(bad).status_code, 404, bad)

    def test_uuid(self):
        self.assertEqual(
            self.get(f"/things/{ITEM}"), (200, {"thing": str(ITEM), "type": "UUID"})
        )
        for bad in (
            "/things/nope",
            f"/things/{str(ITEM).upper()}",
            f"/things/{ITEM.hex}",
        ):
            self.assertEqual(self.client.get(bad).status_code, 404, bad)

    def test_str(self):
        self.assertEqual(self.get("/names/ada"), (200, {"name": "ada"}))
        self.assertEqual(self.client.get("/names/a/b").status_code, 404)

    def test_router_prefixes_and_mounts(self):
        self.assertEqual(
            self.get("/v/api/2/blobs/x/y"), (200, {"version": 2, "key": "x/y"})
        )
        self.assertEqual(self.client.get("/v/api/two/blobs/x").status_code, 404)
        self.assertEqual(self.get("/sub/docs/a/b"), (200, {"page": "a/b"}))


class ConverterDeclarationTests(unittest.TestCase):
    def setUp(self):
        self.app = build_app()

    def test_url_path_for(self):
        path_for = self.app.url_path_for
        self.assertEqual(
            path_for("read_file", file_path="a b/c.txt"), "/files/a%20b/c.txt"
        )
        self.assertEqual(path_for("read_item", item_id=5), "/items/5")
        self.assertEqual(path_for("read_price", price=1.5), "/prices/1.5")
        self.assertEqual(path_for("read_thing", thing=ITEM), f"/things/{ITEM}")
        self.assertEqual(path_for("read_name", name="a/b"), "/names/a%2Fb")
        self.assertEqual(path_for("blob", version=1, key="k/v"), "/v/api/1/blobs/k/v")
        self.assertEqual(path_for("sub:page", page="x/y"), "/sub/docs/x/y")

    def test_openapi_paths_drop_converters(self):
        document = self.app.openapi()
        self.assertEqual(
            sorted(document["paths"]),
            [
                "/as-text/{item_id}",
                "/files/{file_path}",
                "/items/latest",
                "/items/{item_id}",
                "/names/{name}",
                "/prices/{price}",
                "/things/{thing}",
                "/v/api/{version}/blobs/{key}",
            ],
        )
        operation = document["paths"]["/files/{file_path}"]["get"]
        self.assertEqual(operation["operationId"], "read_file_get__files__file_path")
        self.assertEqual(
            [(p["name"], p["in"], p["required"]) for p in operation["parameters"]],
            [("file_path", "path", True)],
        )
        thing = document["paths"]["/things/{thing}"]["get"]["parameters"][0]
        self.assertEqual(thing["schema"], {"type": "string", "format": "uuid"})

    def test_routes_keep_the_declared_path_and_format_it(self):
        routes = {
            route.name: route
            for route in self.app.routes
            if isinstance(route, APIRoute)
        }
        self.assertEqual(routes["read_item"].path, "/items/{item_id:int}")
        self.assertEqual(routes["read_item"].path_format, "/items/{item_id}")


class ConverterStartupErrorTests(unittest.TestCase):
    def test_unknown_converters_fail(self):
        app = App()
        with self.assertRaisesRegex(ValueError, "unknown path converter 'slug'"):
            app.get("/a/{x:slug}")

    def test_path_must_be_the_last_segment(self):
        app = App()
        with self.assertRaisesRegex(ValueError, "last segment"):
            app.get("/a/{rest:path}/b")
        with self.assertRaisesRegex(ValueError, "last segment"):
            Router(prefix="/a/{rest:path}")
        router = Router()

        @router.get("/{rest:path}")
        async def handler(rest: str):
            return {}

        # Including it under a prefix is fine; a path after it is not.
        App().include_router(router, prefix="/files")

    def test_colliding_converter_routes_fail(self):
        async def by_id(item_id: int):
            return {}

        async def by_name(name: str):
            return {}

        async def by_rest(rest: str):
            return {}

        for method, first, second in (
            ("get", ("/items/{item_id:int}", by_id), ("/items/{name}", by_name)),
            ("post", ("/items/{item_id:int}", by_id), ("/items/{name}", by_name)),
            ("post", ("/items/{item_id:int}", by_id), ("/items/{item_id}", by_id)),
            ("get", ("/files/{rest:path}", by_rest), ("/files/{name}", by_name)),
        ):
            with self.subTest(method=method, first=first[0], second=second[0]):
                app = App()
                app.get(first[0])(first[1])
                getattr(app, method)(second[0])(second[1])
                with self.assertRaisesRegex(ValueError, "ambiguous"):
                    app.build()

    def test_str_is_the_same_as_no_converter(self):
        app = App()

        @app.get("/n/{name:str}")
        async def get_name(name: str):
            return {"name": name}

        @app.post("/n/{name}")
        async def post_name(name: str):
            return {"name": name}

        with TestClient(app) as client:
            self.assertEqual(client.post("/n/x").json(), {"name": "x"})
            self.assertEqual(client.get("/n/y").json(), {"name": "y"})


if __name__ == "__main__":
    unittest.main()
