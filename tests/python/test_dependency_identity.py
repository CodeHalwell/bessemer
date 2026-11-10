"""A dependency is identified by its callable, compared as FastAPI compares
it: two ``instance.method`` expressions name one dependency."""

from __future__ import annotations

# ruff: noqa: B008 -- Dependency marker defaults are the public application API.
import dataclasses
import unittest

from bessemer import App, Depends, Security
from bessemer.schema import _call_key
from bessemer.testclient import TestClient

try:
    from pydantic import BaseModel
except ImportError:
    BaseModel = None
else:

    class Item(BaseModel):
        name: str
        price: float


class Database:
    def __init__(self, name):
        self.name = name
        self.sessions = 0
        self.connections = 0
        self.closed = 0

    def session(self):
        self.sessions += 1
        return {"database": self.name, "session": self.sessions}

    def connect(self):
        self.connections += 1
        try:
            yield {"database": self.name, "connection": self.connections}
        finally:
            self.closed += 1

    def item(self, item: Item):
        self.sessions += 1
        return item


class DependencyIdentityTests(unittest.TestCase):
    def test_a_bound_method_written_twice_is_one_request_dependency(self):
        primary = Database("primary")
        replica = Database("replica")

        def users(session=Depends(primary.session)):
            return session

        def orders(session=Depends(primary.session)):
            return session

        app = App()

        @app.get("/shared")
        async def shared(
            first=Depends(users),
            second=Depends(orders),
            third=Depends(primary.session),
            other=Depends(replica.session),
        ):
            return {
                "same": first is second is third,
                "session": first["session"],
                "other": other["database"],
            }

        @app.get("/fresh")
        async def fresh(
            first=Depends(primary.session, use_cache=False),
            second=Depends(primary.session, use_cache=False),
        ):
            return {"same": first is second}

        with TestClient(app) as client:
            for request in (1, 2):
                self.assertEqual(
                    client.get("/shared").json(),
                    {"same": True, "session": request, "other": "replica"},
                )
            # One call per request for each instance: they stay distinct.
            self.assertEqual((primary.sessions, replica.sessions), (2, 2))
            self.assertEqual(client.get("/fresh").json(), {"same": False})
            self.assertEqual(primary.sessions, 4)

    def test_a_bound_method_is_one_application_resource_per_instance(self):
        primary = Database("primary")
        replica = Database("replica")

        def repository(connection=Depends(primary.connect, scope="app")):
            return connection

        app = App()

        @app.get("/one")
        async def one(
            connection=Depends(primary.connect, scope="app"),
            through=Depends(repository, scope="app"),
        ):
            return {
                "connection": connection["connection"],
                "same": connection is through,
            }

        @app.get("/two")
        async def two(
            connection=Depends(primary.connect, scope="app"),
            other=Depends(replica.connect, scope="app"),
        ):
            return {"connection": connection["connection"], "other": other["database"]}

        with TestClient(app) as client:
            for _ in range(2):
                self.assertEqual(
                    client.get("/one").json(), {"connection": 1, "same": True}
                )
                self.assertEqual(
                    client.get("/two").json(), {"connection": 1, "other": "replica"}
                )
            self.assertEqual((primary.connections, replica.connections), (1, 1))
            self.assertEqual((primary.closed, replica.closed), (0, 0))
        # Each resource was opened once, so each is closed once.
        self.assertEqual((primary.closed, replica.closed), (1, 1))

    def test_overrides_apply_to_every_spelling_of_a_bound_method(self):
        database = Database("primary")
        calls = []

        def fake():
            calls.append("fake")
            return {"database": "fake"}

        def users(session=Depends(database.session)):
            return session

        app = App()

        @app.get("/")
        async def index(first=Depends(users), second=Depends(database.session)):
            return {"same": first is second, "database": first["database"]}

        app.dependency_overrides[database.session] = fake
        with TestClient(app) as client:
            self.assertEqual(client.get("/").json(), {"same": True, "database": "fake"})
        self.assertEqual((calls, database.sessions), (["fake"], 0))

    def test_callables_are_compared_as_they_compare_themselves(self):
        calls = []

        class Unhashable:
            """Equality without a hash: such an object is its own identity."""

            def __init__(self, name):
                self.name = name

            def __eq__(self, other):
                return isinstance(other, Unhashable)

            def __call__(self):
                calls.append(self.name)
                return self.name

        @dataclasses.dataclass(frozen=True)
        class Limit:
            """Equal instances are one dependency, as in FastAPI."""

            maximum: int

            def __call__(self):
                calls.append(f"limit {self.maximum}")
                return self.maximum

        first, second = Unhashable("first"), Unhashable("second")
        with self.assertRaises(TypeError):
            hash(first)
        self.assertEqual(_call_key(first), _call_key(first))
        self.assertNotEqual(_call_key(first), _call_key(second))
        self.assertEqual(
            len({_call_key(first), _call_key(first), _call_key(second)}), 2
        )
        self.assertIs(_call_key(len), len)
        app = App()

        @app.get("/")
        async def index(
            a=Depends(first),
            b=Depends(first),
            c=Depends(second),
            d=Depends(Limit(10)),
            e=Depends(Limit(10)),
            f=Depends(Limit(20)),
            g=Depends(first, scope="app"),
            h=Depends(second, scope="app"),
        ):
            return [a, b, c, d, e, f, g, h]

        with TestClient(app) as client:
            # The application resources were made once, at startup.
            self.assertEqual(calls, ["first", "second"])
            calls.clear()
            self.assertEqual(
                client.get("/").json(),
                ["first", "first", "second", 10, 10, 20, "first", "second"],
            )
        self.assertEqual(calls, ["first", "second", "limit 10", "limit 20"])

    @unittest.skipIf(BaseModel is None, "requires optional Pydantic")
    def test_a_body_read_by_one_bound_method_is_one_body(self):
        reader = Database("reader")
        app = App()

        @app.post("/twice")
        async def twice(first=Depends(reader.item), second=Depends(reader.item)):
            return {"same": first is second, "name": first.name}

        @app.post("/scoped")
        async def scoped(
            first=Depends(reader.item),
            second=Security(reader.item, scopes=["items"]),
        ):
            return {"name": first.name, "equal": first == second}

        body = {"name": "pen", "price": 1.5}
        with TestClient(app) as client:
            # The body is the model itself, not an object keyed by parameter.
            response = client.post("/twice", json=body)
            self.assertEqual(
                (response.status_code, response.json()),
                (200, {"same": True, "name": "pen"}),
            )
            self.assertEqual(reader.sessions, 1)
            response = client.post("/scoped", json=body)
            self.assertEqual(
                (response.status_code, response.json()),
                (200, {"name": "pen", "equal": True}),
            )
        for path in ("/twice", "/scoped"):
            body = app.openapi()["paths"][path]["post"]["requestBody"]
            schema = body["content"]["application/json"]["schema"]
            self.assertEqual(
                (schema["title"], sorted(schema["properties"])),
                ("Item", ["name", "price"]),
            )


if __name__ == "__main__":
    unittest.main()
