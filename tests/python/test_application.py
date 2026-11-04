from __future__ import annotations

import asyncio
import concurrent.futures
import io
import os
import tempfile
import threading
import unittest
from contextlib import asynccontextmanager, redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from bessemer import App, HTTPException, Router
from bessemer.cli import main
from bessemer.worker import Worker

try:
    from pydantic import BaseModel, Field
except ImportError:
    BaseModel = None
else:

    class ItemModel(BaseModel):
        count: int = Field(gt=0)


class ApplicationTests(unittest.TestCase):
    def test_compiles_router_prefix_and_typed_inputs(self):
        app = App()
        router = Router(prefix="/items")

        @router.get("/{item_id}", timeout=2)
        async def read(item_id: int, visible: bool = True, label: str | None = None):
            return {"id": item_id, "visible": visible, "label": label}

        app.include_router(router, prefix="/v1")
        app.health()
        routes = app._export_routes()
        self.assertEqual(routes[0]["path"], "/v1/items/{item_id}")
        self.assertEqual(
            [
                (p["name"], p["source"], p["type"], p["required"])
                for p in routes[0]["parameters"]
            ],
            [
                ("item_id", "path", "int", True),
                ("visible", "query", "bool", False),
                ("label", "query", "str", False),
            ],
        )
        self.assertEqual(routes[1]["kind"], "health")
        self.assertEqual(routes[0]["tier"], 2)
        self.assertEqual(routes[1]["tier"], 0)

    def test_duplicate_parameterized_routes_fail_before_serving(self):
        app = App()

        @app.get("/items/{item_id}")
        async def by_id(item_id: int):
            return item_id

        @app.get("/items/{name}")
        async def by_name(name: str):
            return name

        with self.assertRaisesRegex(ValueError, "ambiguous"):
            app.build()

    def test_invalid_signatures_fail_at_startup(self):
        async def untyped(value):
            return value

        async def missing_path():
            return {}

        async def two_bodies(first: dict, second: list):
            return [first, second]

        async def invalid_default(value: int = "x"):
            return value

        async def variadic(*values: int):
            return values

        for path, handler in (
            ("/", untyped),
            ("/{item_id}", missing_path),
            ("/", two_bodies),
            ("/", invalid_default),
            ("/", variadic),
        ):
            with self.subTest(handler=handler.__name__):
                app = App()
                app.get(path)(handler)
                with self.assertRaises(ValueError):
                    app.build()

    def test_routes_freeze_after_build_and_exports_are_independent(self):
        app = App()

        @app.get("/")
        async def index():
            return {}

        app.build()
        self.assertIs(app.build(), app)
        with self.assertRaises(RuntimeError):
            app.get("/second")(index)
        exported = app._export_routes()
        exported[0]["path"] = "/changed"
        self.assertEqual(app._export_routes()[0]["path"], "/")

    def test_invalid_paths_are_rejected(self):
        for path in (
            "relative",
            "/items/{id}/{id}",
            "/items/{id:int}",
            "/items/{id}.json",
            "/items//all",
            "/items?q=1",
        ):
            with self.subTest(path=path), self.assertRaises(ValueError):
                App().get(path)

    def test_parameter_names_cannot_conflict_across_methods(self):
        app = App()

        @app.get("/{item_id}")
        async def read(item_id: int):
            return item_id

        @app.post("/{name}")
        async def create(name: str):
            return name

        with self.assertRaisesRegex(ValueError, "ambiguous"):
            app.build()

    def test_typed_container_contents_require_a_model(self):
        app = App()

        @app.post("/")
        async def create(item: dict[str, int]):
            return item

        with self.assertRaisesRegex(ValueError, "Pydantic"):
            app.build()


class WorkerTests(unittest.TestCase):
    def test_lifespan_and_suspending_handler_use_the_same_loop(self):
        observed = []

        @asynccontextmanager
        async def lifespan(app):
            observed.append(
                ("start", asyncio.get_running_loop(), threading.get_ident())
            )
            app.state.ready = True
            yield
            observed.append(("stop", asyncio.get_running_loop(), threading.get_ident()))

        app = App(lifespan=lifespan)

        @app.get("/")
        async def index():
            await asyncio.sleep(0.001)
            observed.append(
                ("handler", asyncio.get_running_loop(), threading.get_ident())
            )
            return {"ready": app.state.ready}

        with Worker(app) as worker:
            self.assertEqual(
                worker.submit(0, {}).result(timeout=2), (200, {"ready": True})
            )
        self.assertEqual([entry[0] for entry in observed], ["start", "handler", "stop"])
        self.assertEqual(len({entry[1] for entry in observed}), 1)
        self.assertEqual(len({entry[2] for entry in observed}), 1)

    def test_explicit_error_and_unhandled_errors_have_safe_responses(self):
        app = App()

        @app.get("/missing")
        async def missing():
            raise HTTPException(404, "Item not found")

        @app.get("/failure")
        async def failing():
            raise RuntimeError("sensitive database credentials")

        with Worker(app) as worker:
            self.assertEqual(
                worker.submit(0, {}).result(timeout=2),
                (404, {"detail": "Item not found"}),
            )
            with self.assertLogs("bessemer", level="ERROR"):
                response = worker.submit(1, {}).result(timeout=2)
            self.assertEqual(response, (500, {"detail": "Internal Server Error"}))

    def test_async_cancellation_holds_future_until_cleanup_finishes(self):
        app = App()
        started = threading.Event()
        cleanup_started = threading.Event()
        release_cleanup = threading.Event()

        @app.get("/")
        async def slow():
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleanup_started.set()
                while not release_cleanup.is_set():
                    await asyncio.sleep(0.001)

        with Worker(app) as worker:
            future = worker.submit(0, {})
            self.assertTrue(started.wait(2))
            self.assertTrue(future.cancel())
            self.assertTrue(cleanup_started.wait(2))
            self.assertFalse(future.done())
            # A second deadline/disconnect signal must not interrupt cleanup.
            self.assertTrue(future.cancel())
            self.assertFalse(future.done())
            release_cleanup.set()
            with self.assertRaises(concurrent.futures.CancelledError):
                future.result(timeout=2)
            self.assertTrue(future.cancelled())

    def test_sync_cancellation_does_not_release_running_thread(self):
        app = App()
        started = threading.Event()
        release = threading.Event()

        @app.get("/")
        def blocking():
            started.set()
            if not release.wait(2):
                raise TimeoutError("test failed to release handler")
            return {"done": True}

        with Worker(app, sync_workers=1) as worker:
            future = worker.submit(0, {})
            try:
                self.assertTrue(started.wait(2))
                self.assertFalse(future.cancel())
                self.assertFalse(future.done())
            finally:
                release.set()
            self.assertEqual(future.result(timeout=2), (200, {"done": True}))

    def test_lifespan_startup_error_propagates_before_requests(self):
        @asynccontextmanager
        async def lifespan(app):
            raise RuntimeError("startup failed")
            yield

        worker = Worker(App(lifespan=lifespan))
        with self.assertRaisesRegex(RuntimeError, "startup failed"):
            worker.start()
        worker.close()

    def test_startup_timeout_cancels_lifespan_and_closes_loop(self):
        entered = threading.Event()
        cleaned = threading.Event()

        @asynccontextmanager
        async def lifespan(app):
            entered.set()
            try:
                await asyncio.Event().wait()
                yield
            finally:
                cleaned.set()

        worker = Worker(App(lifespan=lifespan), graceful_timeout=0.05)
        with self.assertRaisesRegex(TimeoutError, "startup timed out"):
            worker.start()
        self.assertTrue(entered.is_set())
        self.assertTrue(cleaned.wait(1))
        self.assertFalse(worker._thread.is_alive())
        self.assertTrue(worker._loop.is_closed())

    def test_shutdown_failure_closes_loop_before_propagating(self):
        @asynccontextmanager
        async def lifespan(app):
            yield
            raise RuntimeError("shutdown failed")

        worker = Worker(App(lifespan=lifespan))
        worker.start()
        with self.assertRaisesRegex(RuntimeError, "shutdown failed"):
            worker.close()
        self.assertFalse(worker._thread.is_alive())
        self.assertTrue(worker._loop.is_closed())

    def test_shutdown_timeout_does_not_abandon_sync_work(self):
        app = App()
        started = threading.Event()
        release = threading.Event()

        @app.get("/")
        def blocking():
            started.set()
            release.wait(2)
            return True

        worker = Worker(app, graceful_timeout=0.05)
        worker.start()
        future = worker.submit(0, {})
        try:
            self.assertTrue(started.wait(1))
            with self.assertRaisesRegex(TimeoutError, "cannot be force-stopped"):
                worker.close()
            self.assertFalse(future.done())
        finally:
            release.set()
        self.assertEqual(future.result(timeout=1), (200, True))
        worker._thread.join(timeout=1)
        self.assertFalse(worker._thread.is_alive())
        worker.close()

    def test_raw_json_body_shape_and_nullability_are_checked(self):
        app = App()

        @app.post("/object")
        async def object_body(item: dict):
            return item

        @app.post("/array")
        async def array_body(items: list | None = None):
            return items

        with Worker(app) as worker:
            for invalid in (None, 42, []):
                self.assertEqual(
                    worker.submit(0, {"item": invalid}).result(timeout=1)[0], 422
                )
            self.assertEqual(
                worker.submit(0, {"item": {"a": 1}}).result(timeout=1), (200, {"a": 1})
            )
            self.assertEqual(
                worker.submit(1, {"items": None}).result(timeout=1), (200, None)
            )
            self.assertEqual(
                worker.submit(1, {"items": [1]}).result(timeout=1), (200, [1])
            )
            self.assertEqual(worker.submit(1, {"items": {}}).result(timeout=1)[0], 422)

    @unittest.skipIf(BaseModel is None, "optional Pydantic v2 is not installed")
    def test_pydantic_model_validation_and_response_serialization(self):
        app = App()

        @app.post("/", status_code=201)
        async def create(item: ItemModel):
            self.assertIsInstance(item, ItemModel)
            return item

        with Worker(app) as worker:
            self.assertEqual(
                worker.submit(0, {"item": {"count": 2}}).result(timeout=1),
                (201, {"count": 2}),
            )
            status, payload = worker.submit(0, {"item": {"count": -1}}).result(
                timeout=1
            )
            self.assertEqual(status, 422)
            self.assertEqual(payload["detail"][0]["loc"], ["body", "count"])
            self.assertEqual(payload["detail"][0]["type"], "greater_than")
            self.assertEqual(worker.submit(0, {"item": None}).result(timeout=1)[0], 422)

    def test_startup_interruption_requests_cleanup(self):
        worker = Worker(App(), graceful_timeout=1)
        with (
            patch.object(worker._ready, "wait", side_effect=KeyboardInterrupt),
            self.assertRaises(KeyboardInterrupt),
        ):
            worker.start()
        worker._thread.join(timeout=1)
        self.assertFalse(worker._thread.is_alive())
        self.assertTrue(worker._loop.is_closed())

    def test_worker_cancels_remaining_lifespan_background_tasks(self):
        cleaned = threading.Event()

        async def background():
            try:
                await asyncio.Event().wait()
            finally:
                cleaned.set()

        @asynccontextmanager
        async def lifespan(app):
            asyncio.create_task(background())
            await asyncio.sleep(0)
            yield

        with Worker(App(lifespan=lifespan)):
            pass
        self.assertTrue(cleaned.is_set())


class CliTests(unittest.TestCase):
    def test_routes_command_imports_application_from_current_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            module = Path(directory, "bessemer_test_app.py")
            module.write_text(
                "from bessemer import App\napp = App()\napp.health('/ready')\n"
            )
            with patch("pathlib.Path.cwd", return_value=Path(directory)):
                output = io.StringIO()
                with redirect_stdout(output):
                    self.assertEqual(main(["routes", "bessemer_test_app:app"]), 0)
            self.assertIn("/ready", output.getvalue())
            self.assertIn("Tier 0", output.getvalue())

    def test_run_config_precedence_and_invalid_values(self):
        app = App()
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory, "pyproject.toml")
            config.write_text("[tool.bessemer]\nport = 9000\nmax_in_flight = 8\n")
            previous = os.getcwd()
            try:
                os.chdir(directory)
                with (
                    patch("bessemer.cli.load_app", return_value=app),
                    patch.object(app, "run") as run,
                    patch.dict(os.environ, {"BESSEMER_PORT": "9001"}, clear=True),
                ):
                    self.assertEqual(main(["run", "app:app", "--port", "9002"]), 0)
                    self.assertEqual(run.call_args.kwargs["port"], 9002)
                    self.assertEqual(run.call_args.kwargs["max_in_flight"], 8)
                with (
                    patch("bessemer.cli.load_app", return_value=app),
                    patch.dict(
                        os.environ, {"BESSEMER_REQUEST_TIMEOUT": "nan"}, clear=True
                    ),
                    redirect_stderr(io.StringIO()),
                ):
                    self.assertEqual(main(["run", "app:app"]), 1)
            finally:
                os.chdir(previous)

    def test_unsupported_options_are_not_silently_accepted(self):
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            main(["run", "examples.basic:app", "--workers", "4"])


if __name__ == "__main__":
    unittest.main()
