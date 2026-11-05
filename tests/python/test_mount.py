"""Mounting one App inside another (FastAPI's app.mount for sub-applications)."""

import unittest
from typing import Annotated

from bessemer import (
    App,
    Depends,
    HTTPException,
    JSONResponse,
    NoMatchFound,
    Request,
    State,
)
from bessemer.testclient import TestClient

seen = []


def record_parent():
    seen.append("parent")


def record_child():
    seen.append("child")


def greeting():
    return "hello"


class Teapot(Exception):
    pass


def build():
    parent = App(title="Parent", dependencies=[Depends(record_parent)])
    child = App(title="Child", docs_url="/docs", dependencies=[Depends(record_child)])
    child.state.name = "child state"
    parent.state.name = "parent state"
    grandchild = App(title="Grandchild", openapi_url=None)

    @parent.get("/")
    async def parent_root():
        return {"app": "parent"}

    @parent.get("/v1/shadow")
    async def shadow():
        return {"app": "parent"}

    @parent.middleware("http")
    async def outer(request, call_next):
        response = await call_next(request)
        response.headers["x-order"] = response.headers.get("x-order", "") + "outer"
        return response

    @child.middleware("http")
    async def inner(request, call_next):
        response = await call_next(request)
        response.headers["x-order"] = "inner,"
        return response

    @child.exception_handler(Teapot)
    async def teapot(request, error):
        return JSONResponse({"detail": "short and stout"}, status_code=418)

    @child.get("/items/{item_id}", name="item")
    async def item(item_id: int, words: Annotated[str, Depends(greeting)]):
        return {"item": item_id, "words": words}

    @child.get("/state")
    async def state(request: Request, app_state: State):
        return {
            "state": app_state.name,
            "request_app": request.app.title,
            "url": request.url_for("item", item_id=3),
        }

    @child.get("/teapot")
    async def raise_teapot():
        raise Teapot

    @child.get("/missing")
    async def missing():
        raise HTTPException(404, "nope")

    @grandchild.get("/deep")
    async def deep():
        return {"app": "grandchild"}

    child.mount("/g", grandchild, name="g")
    parent.mount("/v1", child, name="v1")

    @parent.get("/after")
    async def after_mount():
        return {"app": "parent"}

    return parent, child, grandchild


class MountTests(unittest.TestCase):
    def test_mounted_routes_are_served_under_the_prefix(self):
        parent, _, _ = build()
        with TestClient(parent) as client:
            seen.clear()
            self.assertEqual(
                client.get("/v1/items/7").json(), {"item": 7, "words": "hello"}
            )
            # The mounted app's dependencies run, the parent's do not.
            self.assertEqual(seen, ["child"])
            seen.clear()
            self.assertEqual(client.get("/").json(), {"app": "parent"})
            self.assertEqual(seen, ["parent"])
            self.assertEqual(client.get("/after").json(), {"app": "parent"})
            self.assertEqual(client.get("/v1/g/deep").json(), {"app": "grandchild"})
            self.assertEqual(client.get("/v1/shadow").json(), {"app": "parent"})
            self.assertEqual(client.get("/items/7").status_code, 404)

    def test_owner_state_handlers_and_middleware(self):
        parent, _, _ = build()
        with TestClient(parent) as client:
            response = client.get("/v1/state")
            self.assertEqual(
                response.json(),
                {
                    "state": "child state",
                    "request_app": "Child",
                    "url": "http://testserver/v1/items/3",
                },
            )
            # The parent's middleware wraps the child's.
            self.assertEqual(response.headers["x-order"], "inner,outer")
            self.assertEqual(client.get("/").headers["x-order"], "outer")
            teapot = client.get("/v1/teapot")
            self.assertEqual(teapot.status_code, 418)
            self.assertEqual(client.get("/v1/missing").json(), {"detail": "nope"})
            self.assertEqual(client.get("/v1/g/deep").headers["x-order"], "inner,outer")

    def test_each_app_documents_its_own_routes(self):
        parent, _, _ = build()
        self.assertEqual(
            sorted(parent.openapi()["paths"]), ["/", "/after", "/v1/shadow"]
        )
        with TestClient(parent) as client:
            document = client.get("/v1/openapi.json").json()
            self.assertEqual(document["info"]["title"], "Child")
            self.assertEqual(document["servers"], [{"url": "/v1"}])
            self.assertEqual(
                sorted(document["paths"]),
                ["/items/{item_id}", "/missing", "/state", "/teapot"],
            )
            docs = client.get("/v1/docs")
            self.assertIn("/v1/openapi.json", docs.text)
            # The grandchild turned its document off.
            self.assertEqual(client.get("/v1/g/openapi.json").status_code, 404)

    def test_url_path_for_reaches_mounts_by_name(self):
        parent, child, _ = build()
        self.assertEqual(parent.url_path_for("v1:item", item_id=5), "/v1/items/5")
        self.assertEqual(parent.url_path_for("v1:g:deep"), "/v1/g/deep")
        self.assertEqual(child.url_path_for("item", item_id=5), "/items/5")
        with self.assertRaises(NoMatchFound):
            parent.url_path_for("item", item_id=5)

    def test_mounted_overrides_apply_to_mounted_routes(self):
        parent, child, _ = build()
        with TestClient(parent) as client:
            self.assertEqual(client.get("/v1/items/1").json()["words"], "hello")
        child.dependency_overrides[greeting] = lambda: "overridden"
        # The parent's own overrides do not reach a mounted app's routes.
        parent.dependency_overrides[greeting] = lambda: "parent"
        with TestClient(parent) as client:
            self.assertEqual(client.get("/v1/items/1").json()["words"], "overridden")

    def test_invalid_mounts(self):
        parent = App()
        with self.assertRaises(TypeError):
            parent.mount("/x", object())
        with self.assertRaisesRegex(ValueError, "itself"):
            parent.mount("/x", parent)
        for path in ("/", "", "/{name}"):
            with self.assertRaisesRegex(ValueError, "literal|must begin with /"):
                parent.mount(path, App())
        with self.assertRaisesRegex(ValueError, "lifespan"):
            parent.mount("/x", App(lifespan=lambda app: None))
        with self.assertRaisesRegex(ValueError, "request_id, security_headers"):
            parent.mount("/x", App(request_id=False, security_headers=False))
        child = App()
        parent.mount("/x", child, name="x")
        with self.assertRaisesRegex(ValueError, "once"):
            App().mount("/y", child)
        with self.assertRaisesRegex(ValueError, "already mounted"):
            parent.mount("/x/", App())
        with self.assertRaisesRegex(ValueError, "duplicate mount name"):
            parent.mount("/z", App(), name="x")
        with self.assertRaisesRegex(ValueError, "itself"):
            child.mount("/p", parent)
        with self.assertRaisesRegex(ValueError, "':'"):
            parent.mount("/w", App(), name="a:b")

    def test_conflicting_routes_fail_at_build(self):
        parent = App()
        child = App()

        @parent.get("/v1/items")
        async def mine():
            return {}

        @child.get("/items")
        async def theirs():
            return {}

        parent.mount("/v1", child)
        with self.assertRaisesRegex(ValueError, "duplicate or ambiguous route"):
            parent.build()


if __name__ == "__main__":
    unittest.main()
