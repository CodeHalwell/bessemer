"""A lifespan yielding a state mapping, as in Starlette and FastAPI."""

import unittest
from contextlib import asynccontextmanager
from types import MappingProxyType

from bessemer import App, Depends, Request, WebSocket
from bessemer.testclient import TestClient


class Database:
    def __init__(self) -> None:
        self.queries = []


def build(state):
    @asynccontextmanager
    async def lifespan(app):
        yield state

    app = App(lifespan=lifespan, allowed_hosts=["testserver"])

    def database(request: Request):
        return request.state.db

    @app.get("/handler")
    async def handler(request: Request):
        return {"name": request.state.name}

    @app.get("/dependency")
    async def dependency(db=Depends(database)):  # noqa: B008
        db.queries.append("select")
        return {"queries": len(db.queries)}

    @app.get("/isolation")
    async def isolation(request: Request):
        seen = getattr(request.state, "mark", None)
        request.state.mark = "set"
        request.state.name = "changed"
        return {"seen": seen}

    @app.get("/plain")
    async def plain(request: Request):
        return {"attributes": sorted(vars(request.state))}

    @app.websocket("/ws")
    async def ws(websocket: WebSocket):
        await websocket.accept()
        await websocket.send_json({"name": websocket.state.name})
        await websocket.close()

    return app


class LifespanStateTests(unittest.TestCase):
    def test_state_reaches_handlers_and_dependencies(self):
        db = Database()
        with TestClient(build({"db": db, "name": "primary"})) as client:
            self.assertEqual(client.get("/handler").json(), {"name": "primary"})
            self.assertEqual(client.get("/dependency").json(), {"queries": 1})
            self.assertEqual(client.get("/dependency").json(), {"queries": 2})
        # The object itself is shared, not copied.
        self.assertEqual(db.queries, ["select", "select"])

    def test_each_request_gets_a_shallow_copy(self):
        state = {"db": Database(), "name": "primary"}
        with TestClient(build(state)) as client:
            self.assertEqual(client.get("/isolation").json(), {"seen": None})
            self.assertEqual(client.get("/isolation").json(), {"seen": None})
            self.assertEqual(client.get("/handler").json(), {"name": "primary"})
        self.assertEqual(set(state), {"db", "name"})

    def test_any_mapping_is_accepted(self):
        state = MappingProxyType({"db": Database(), "name": "proxied"})
        with TestClient(build(state)) as client:
            self.assertEqual(client.get("/handler").json(), {"name": "proxied"})

    def test_yielding_none_leaves_state_empty(self):
        with TestClient(build(None)) as client:
            self.assertEqual(client.get("/plain").json(), {"attributes": []})
            self.assertEqual(client.get("/isolation").json(), {"seen": None})
            self.assertEqual(client.get("/isolation").json(), {"seen": None})

    def test_yielding_a_non_mapping_fails_startup(self):
        app = build(["db"])
        with self.assertRaises(TypeError) as raised, TestClient(app):
            pass
        self.assertIn("mapping", str(raised.exception))

    def test_mounted_apps_see_the_outermost_state(self):
        parent = build({"db": Database(), "name": "outer"})
        child = App()

        @child.get("/inner")
        async def inner(request: Request):
            return {"name": request.state.name}

        grandchild = App()

        @grandchild.get("/deep")
        async def deep(request: Request):
            return {"name": request.state.name}

        child.mount("/g", grandchild)
        parent.mount("/v1", child)
        with TestClient(parent) as client:
            self.assertEqual(client.get("/v1/inner").json(), {"name": "outer"})
            self.assertEqual(client.get("/v1/g/deep").json(), {"name": "outer"})

    def test_websockets_see_the_state(self):
        with (
            TestClient(build({"name": "socket"})) as client,
            client.websocket_connect("/ws") as websocket,
        ):
            self.assertEqual(websocket.receive_json(), {"name": "socket"})

    def test_startup_handlers_still_run(self):
        events = []
        app = App(
            on_startup=[lambda: events.append("up")], allowed_hosts=["testserver"]
        )

        @app.get("/")
        async def index(request: Request):
            request.state.seen = True
            return {"attributes": sorted(vars(request.state))}

        with TestClient(app) as client:
            self.assertEqual(client.get("/").json(), {"attributes": ["seen"]})
        self.assertEqual(events, ["up"])

    def test_state_is_lazy_and_assignable(self):
        request = Request._bare("GET", "/")
        self.assertIsNone(request._state)
        request.state.x = 1
        self.assertEqual(request.state.__dict__, {"x": 1})
        replacement = object()
        request.state = replacement
        self.assertIs(request.state, replacement)


if __name__ == "__main__":
    unittest.main()
