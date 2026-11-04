"""The in-process test client and dependency overrides."""

import signal
import threading
import unittest
from contextlib import asynccontextmanager

from bessemer import App, Depends, HTTPException, Query, RedirectResponse, Request
from bessemer.testclient import TestClient


def database():
    return "real"


def current_user():
    raise HTTPException(401, "Not authenticated")


def build_app(events):
    @asynccontextmanager
    async def lifespan(app):
        events.append("startup")
        yield
        events.append("shutdown")

    app = App(lifespan=lifespan, allowed_hosts=["testserver"])

    @app.get("/items/{item_id}")
    async def read_item(item_id: int, q: str = "none", db=Depends(database)):  # noqa: B008
        return {"id": item_id, "q": q, "db": db}

    @app.get("/me")
    async def me(user=Depends(current_user)):  # noqa: B008
        return {"user": user}

    @app.post("/echo")
    async def echo(payload: dict):
        return payload

    @app.post("/raw")
    async def raw(request: Request):
        return {
            "body": (await request.body()).decode(),
            "type": request.headers.get("content-type"),
            "custom": request.headers.get("x-custom"),
            "cookie": request.cookies.get("theme"),
        }

    @app.post("/login")
    async def login():
        response = RedirectResponse("/session", status_code=303)
        response.set_cookie("session", "abc")
        return response

    @app.get("/session")
    async def session(request: Request):
        return {"session": request.cookies.get("session")}

    @app.post("/logout")
    async def logout():
        response = RedirectResponse("https://elsewhere.example/")
        response.delete_cookie("session")
        return response

    return app


class TestClientTests(unittest.TestCase):
    def setUp(self):
        self.events = []
        self.app = build_app(self.events)

    def test_requests_reach_the_native_server_with_lifespan(self):
        with TestClient(self.app) as client:
            self.assertEqual(self.events, ["startup"])
            response = client.get("/items/3", params={"q": "x"})
            self.assertEqual(response.status_code, 200)
            self.assertTrue(response.is_success)
            self.assertEqual(response.json(), {"id": 3, "q": "x", "db": "real"})
            self.assertEqual(response.headers["content-type"], "application/json")
            # Validation and routing errors come from the native server.
            self.assertEqual(client.get("/items/nope").status_code, 422)
            self.assertEqual(client.get("/missing").status_code, 404)
            self.assertEqual(
                client.post("/echo", json={"a": [1, 2]}).json(), {"a": [1, 2]}
            )
            with self.assertRaises(AssertionError):
                client.get("/missing").raise_for_status()
        self.assertEqual(self.events, ["startup", "shutdown"])

    def test_bodies_headers_and_cookies(self):
        with TestClient(
            self.app, headers={"X-Custom": "client"}, cookies={"theme": "dark"}
        ) as client:
            form = client.post("/raw", data={"a": "1", "b": ["2", "3"]}).json()
            self.assertEqual(form["body"], "a=1&b=2&b=3")
            self.assertEqual(form["type"], "application/x-www-form-urlencoded")
            self.assertEqual(form["custom"], "client")
            self.assertEqual(form["cookie"], "dark")
            text = client.post(
                "/raw",
                content="plain",
                headers={"Content-Type": "text/plain", "X-Custom": "request"},
                cookies={"theme": "light"},
            ).json()
            self.assertEqual(
                text,
                {
                    "body": "plain",
                    "type": "text/plain",
                    "custom": "request",
                    "cookie": "light",
                },
            )
            with self.assertRaises(ValueError):
                client.post("/raw", json={}, content="x")

    def test_redirects_and_the_cookie_jar(self):
        with TestClient(self.app) as client:
            response = client.post("/login")
            self.assertEqual(response.json(), {"session": "abc"})
            self.assertEqual([item.status_code for item in response.history], [303])
            self.assertEqual(response.url, "http://testserver/session")
            self.assertEqual(client.cookies, {"session": "abc"})
            # A redirect off the application host is returned, not followed.
            response = client.post("/logout")
            self.assertEqual(response.status_code, 307)
            self.assertNotIn("session", client.cookies)
            manual = client.post("/login", follow_redirects=False)
            self.assertEqual(manual.status_code, 303)
            self.assertTrue(manual.is_redirect)
            self.assertEqual(manual.cookies, {"session": "abc"})

    def test_a_base_url_prefix_applies_once(self):
        app = App()

        @app.get("/api/start")
        async def start():
            return RedirectResponse("next")

        @app.get("/api/next")
        async def next_page():
            return {"ok": True}

        with TestClient(app, base_url="http://testserver/api") as client:
            response = client.get("/start")
            self.assertEqual(response.json(), {"ok": True})
            self.assertEqual(response.url, "http://testserver/api/next")
            self.assertEqual(response.history[0].url, "http://testserver/api/start")

    def test_host_checks_apply_to_the_base_url(self):
        with TestClient(self.app, base_url="http://other.example") as client:
            self.assertEqual(client.get("/items/1").status_code, 400)

    def test_server_options_reach_the_native_limits(self):
        with TestClient(self.app, server_options={"max_body_size": 16}) as client:
            self.assertEqual(
                client.post("/echo", json={"long": "x" * 32}).status_code, 413
            )
        with self.assertRaises(ValueError):
            TestClient(self.app, server_options={"port": 1})

    def test_requests_need_a_started_client(self):
        client = TestClient(self.app)
        with self.assertRaises(RuntimeError):
            client.get("/items/1")
        client.start()
        try:
            self.assertEqual(client.get("/items/1").status_code, 200)
        finally:
            client.close()
        client.close()  # Idempotent.

    def test_startup_failures_are_raised_from_enter(self):
        @asynccontextmanager
        async def broken(app):
            raise RuntimeError("database unavailable")
            yield

        app = App(lifespan=broken)

        @app.get("/")
        async def index():
            return {}

        with self.assertRaises(RuntimeError), TestClient(app):
            pass

    def test_the_client_leaves_process_signals_alone(self):
        before = signal.getsignal(signal.SIGINT)
        with TestClient(self.app) as client:
            client.get("/items/1")
        self.assertIs(signal.getsignal(signal.SIGINT), before)

    def test_threads_can_share_a_client(self):
        results = []
        with TestClient(self.app) as client:

            def worker():
                for item in range(20):
                    results.append(client.get(f"/items/{item}").json()["id"])

            threads = [threading.Thread(target=worker) for _ in range(4)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
        self.assertEqual(sorted(results), sorted(list(range(20)) * 4))


class DependencyOverrideTests(unittest.TestCase):
    def setUp(self):
        self.app = build_app([])

    def test_overrides_replace_dependencies_until_cleared(self):
        self.app.dependency_overrides[database] = lambda: "fake"
        self.app.dependency_overrides[current_user] = lambda: "alice"
        with TestClient(self.app) as client:
            self.assertEqual(client.get("/items/1").json()["db"], "fake")
            self.assertEqual(client.get("/me").json(), {"user": "alice"})
        self.app.dependency_overrides.clear()
        with TestClient(self.app) as client:
            self.assertEqual(client.get("/items/1").json()["db"], "real")
            self.assertEqual(client.get("/me").status_code, 401)

    def test_an_override_contributes_its_own_inputs(self):
        def database_for(tenant: str = Query("default")):
            return f"db:{tenant}"

        self.app.dependency_overrides[database] = database_for
        with TestClient(self.app) as client:
            self.assertEqual(
                client.get("/items/1", params={"tenant": "acme"}).json()["db"],
                "db:acme",
            )
        schema = self.app.openapi()
        names = [
            parameter["name"]
            for parameter in schema["paths"]["/items/{item_id}"]["get"]["parameters"]
        ]
        self.assertIn("tenant", names)

    def test_overrides_never_change_a_running_server(self):
        with TestClient(self.app) as client:
            self.app.dependency_overrides[database] = lambda: "fake"
            self.app.openapi()  # Rebuilds the app, but not the running worker.
            self.assertEqual(client.get("/items/1").json()["db"], "real")
        with TestClient(self.app) as client:
            self.assertEqual(client.get("/items/1").json()["db"], "fake")

    def test_unhashable_dependencies_still_build(self):
        class Unhashable:
            __hash__ = None

            def __call__(self):
                return "instance"

        dependency = Unhashable()
        app = App()

        @app.get("/")
        async def index(value=Depends(dependency)):  # noqa: B008
            return {"value": value}

        app.dependency_overrides[database] = lambda: "unrelated"
        with TestClient(app) as client:
            self.assertEqual(client.get("/").json(), {"value": "instance"})

    def test_overrides_must_be_callables(self):
        self.app.dependency_overrides[database] = "fake"
        with self.assertRaises(TypeError):
            self.app.build()
        self.app.dependency_overrides = []
        with self.assertRaises(TypeError):
            self.app.build()


if __name__ == "__main__":
    unittest.main()
