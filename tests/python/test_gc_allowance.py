"""A large body's objects stay out of the collector's young budget.

Parsing a body into models allocates thousands of container objects at once.
They cannot be garbage while their request runs, so the collection they would
start is put off: the young threshold rises by their number, and comes back
when the request ends or a collection runs. These tests read the collector
from inside the handler, where the allowance is in force, and from outside
once it is not.
"""

from __future__ import annotations

import asyncio
import gc
import importlib.util
import json
import os
import subprocess
import sys
import sysconfig
import threading
import time
import unittest

NATIVE = importlib.util.find_spec("bessemer._native") is not None
PYDANTIC = importlib.util.find_spec("pydantic") is not None
# A property of the build, not of the run: a free-threaded build keeps its own
# collector when it is started with the GIL switched on.
FREE_THREADED = bool(sysconfig.get_config_var("Py_GIL_DISABLED"))

if PYDANTIC:
    from pydantic import BaseModel

    class Item(BaseModel):
        id: int
        name: str
        price: float
        tags: list[str]

    class Order(BaseModel):
        items: list[Item]

    class Note(BaseModel):
        text: str


def order(size):
    return json.dumps(
        {
            "items": [
                {"id": i, "name": f"item-{i}", "price": i * 1.25, "tags": ["a", "b"]}
                for i in range(size)
            ]
        }
    )


# A thousand items are 4,004 containers: an instance, its dict, its field set
# and its tag list for each, and the same four for the order. The collector
# counts a few hundred fewer, because lists and dicts it takes from its free
# lists were counted when they were first made.
LARGE = 1000
FEWEST, MOST = 3000, 4100
SMALL = 20

# A free-threaded build has another collector. It does not decide to collect
# on the young count alone, and without the GIL other threads allocate, free
# and collect while a body is parsed. No allowance is made there, whether its
# GIL is on or off.
NEEDS_ALLOWANCE = unittest.skipIf(
    FREE_THREADED, "no allowance is made on a free-threaded build"
)

# A fresh interpreter that serves one large body and reports what it saw.
# What replaces BEFORE runs ahead of the first import of Bessemer.
PROBE = """
import gc, io, json, logging, sys
BEFORE
from pydantic import BaseModel
from bessemer import App, _native
from bessemer.testclient import TestClient


class Item(BaseModel):
    id: int
    name: str
    price: float
    tags: list[str]


class Order(BaseModel):
    items: list[Item]


log = io.StringIO()
logging.getLogger("bessemer").addHandler(logging.StreamHandler(log))
callbacks = list(gc.callbacks)
gc.set_threshold(50_000, *gc.get_threshold()[1:])
app = App()


@app.post("/seen")
async def look(order: Order):
    return {"threshold": gc.get_threshold()[0], "granted": _native._gc_allowance()[1]}


body = json.dumps({"items": [
    {"id": i, "name": f"item-{i}", "price": i * 1.25, "tags": ["a", "b"]}
    for i in range(1000)
]})
with TestClient(app) as client:
    seen = client.post(
        "/seen", content=body, headers={"content-type": "application/json"}
    ).json()
    status = list(_native._gc_allowance())
    gc.collect()
print(json.dumps({
    "seen": seen,
    "status": status,
    "callbacks_unchanged": list(gc.callbacks) == callbacks,
    "threshold": gc.get_threshold()[0],
    "gil": getattr(sys, "_is_gil_enabled", lambda: True)(),
    "log": log.getvalue(),
}))
"""


def probe(before="", **environment):
    completed = subprocess.run(
        [sys.executable, "-c", PROBE.replace("BEFORE", before)],
        capture_output=True,
        text=True,
        timeout=120,
        env=os.environ | environment,
        check=False,
    )
    if completed.returncode:
        raise AssertionError(completed.stderr)
    return json.loads(completed.stdout.splitlines()[-1])


@unittest.skipUnless(NATIVE and PYDANTIC, "needs the native extension and Pydantic")
class GcAllowanceTests(unittest.TestCase):
    event_loops = (
        ("asyncio", "bessemer") if sys.platform in ("linux", "darwin") else ("asyncio",)
    )

    def setUp(self):
        from bessemer import _native

        self.native = _native
        thresholds = gc.get_threshold()
        self.addCleanup(gc.set_threshold, *thresholds)
        self.addCleanup(_native._set_gc_allowance, True)
        if not gc.isenabled():
            self.addCleanup(gc.disable)
            gc.enable()
        # These tests inspect grants while requests are still in flight.
        # An unrelated automatic collection may legitimately retire them, so
        # leave room for fixture and client allocations. Collection tests
        # below explicitly collect, or choose a small budget to provoke one.
        self.base = 50_000
        gc.set_threshold(self.base, thresholds[1], thresholds[2])
        self.kept = []
        self.inside = threading.Event()
        self.release = threading.Event()
        self.inside_new = threading.Event()
        self.release_new = threading.Event()
        self.addCleanup(self.release.set)
        self.addCleanup(self.release_new.set)

    def app(self):
        from bessemer import App

        app = App()
        native = self.native

        def seen():
            return {
                "threshold": gc.get_threshold()[0],
                "granted": native._gc_allowance()[1],
            }

        @app.post("/echo")
        async def echo(order: Order) -> Order:
            return order

        @app.post("/seen")
        async def look(order: Order):
            return seen() | {"items": len(order.items)}

        @app.post("/note")
        async def note(note: Note):
            return seen() | {"length": len(note.text)}

        @app.post("/fails")
        async def fails(order: Order):
            raise RuntimeError("after the body was parsed")

        @app.post("/sets")
        async def sets(order: Order):
            gc.set_threshold(7000, *gc.get_threshold()[1:])
            return seen()

        @app.post("/keeps")
        async def keeps(order: Order):
            self.kept.append(order)
            return seen()

        @app.post("/sleeps")
        async def sleeps(order: Order):
            self.inside.set()
            await asyncio.sleep(30)
            return seen()

        @app.post("/waits")
        def waits(order: Order):
            first = seen()
            self.inside.set()
            self.release.wait(10)
            return {"first": first, "later": seen()}

        @app.post("/collects/{generation}")
        async def collects(order: Order, generation: int, new_base: int = 0):
            if new_base:
                gc.set_threshold(new_base, *gc.get_threshold()[1:])
            before = seen()
            gc.collect(generation)
            return {"before": before, "after": seen(), "items": len(order.items)}

        @app.post("/automatic-collects")
        async def automatic_collects(order: Order):
            before = seen()
            collections = gc.get_stats()[0]["collections"]
            allocated = 0
            # Reach the young budget through ordinary Python allocations.
            # Keep the body alive: this is promotion, not early freeing.
            for _ in range(512):
                for _ in range(64):
                    cycle = []
                    cycle.append(cycle)
                del cycle
                allocated += 64
                if gc.get_stats()[0]["collections"] > collections:
                    break
            return {
                "before": before,
                "after": seen(),
                "collections": gc.get_stats()[0]["collections"] - collections,
                "allocated": allocated,
                "items": len(order.items),
                "last_id": order.items[-1].id,
            }

        @app.post("/collects-in-except")
        async def collects_in_except(order: Order):
            before = seen()
            try:
                raise ValueError("kept while the collector runs")
            except ValueError as error:
                gc.collect(0)
                return {
                    "before": before,
                    "after": seen(),
                    "same_exception": sys.exception() is error,
                    "message": str(error),
                    "items": len(order.items),
                }

        @app.post("/waits/{slot}")
        def waits_in_slot(order: Order, slot: int):
            first = seen()
            (self.inside, self.inside_new)[slot].set()
            (self.release, self.release_new)[slot].wait(10)
            return {"first": first, "later": seen(), "items": len(order.items)}

        return app

    def client(self, event_loop, **options):
        from bessemer.testclient import TestClient

        client = TestClient(
            self.app(), server_options={"event_loop": event_loop} | options
        )
        client.__enter__()
        self.addCleanup(client.__exit__, None, None, None)
        return client

    def post(self, client, path, size=LARGE):
        return client.post(
            path, content=order(size), headers={"content-type": "application/json"}
        )

    def hooks(self):
        """Bessemer's entries in ``gc.callbacks``."""
        return [
            callback
            for callback in gc.callbacks
            if getattr(callback, "__name__", "") == "on_collection"
        ]

    def settled(self, threshold=None):
        """Wait for the last request's objects to be let go."""
        threshold = self.base if threshold is None else threshold
        deadline = time.monotonic() + 5
        while self.native._gc_allowance()[1] != 0 or gc.get_threshold()[0] != threshold:
            self.assertLess(
                time.monotonic(),
                deadline,
                f"allowance {self.native._gc_allowance()}, threshold {gc.get_threshold()}",
            )
            time.sleep(0.005)

    @NEEDS_ALLOWANCE
    def test_a_large_body_raises_the_threshold_for_its_request_only(self):
        for event_loop in self.event_loops:
            with self.subTest(event_loop=event_loop):
                client = self.client(event_loop)
                self.assertEqual(self.native._gc_allowance(), (True, 0))
                for _ in range(3):
                    seen = self.post(client, "/seen").json()
                    self.assertEqual(seen["items"], LARGE)
                    self.assertTrue(FEWEST <= seen["granted"] <= MOST, seen)
                    self.assertEqual(seen["threshold"], self.base + seen["granted"])
                    self.settled()
                # The model is still returned whole.
                response = self.post(client, "/echo")
                self.assertEqual(response.status_code, 200)
                self.assertEqual(len(response.json()["items"]), LARGE)
                self.settled()

    @unittest.skipUnless(FREE_THREADED, "this build makes the allowance")
    def test_a_free_threaded_build_gets_no_allowance(self):
        callbacks = list(gc.callbacks)
        for event_loop in self.event_loops:
            with self.subTest(event_loop=event_loop):
                client = self.client(event_loop)
                # Asked for, and reported as not made.
                self.assertEqual(self.native._gc_allowance(), (False, 0))
                for _ in range(3):
                    seen = self.post(client, "/seen").json()
                    self.assertEqual(
                        seen, {"threshold": self.base, "granted": 0, "items": LARGE}
                    )
                self.assertEqual(self.post(client, "/fails").status_code, 500)
                self.assertEqual(self.native._gc_allowance(), (False, 0))
                self.assertEqual(gc.get_threshold()[0], self.base)
                # Nothing was added to watch collections that move no threshold.
                self.assertEqual(list(gc.callbacks), callbacks)

    @unittest.skipUnless(FREE_THREADED, "this build always has its GIL")
    def test_a_free_threaded_build_gets_none_with_its_gil_switched_on(self):
        # The collector is the build's, whatever the GIL does: with it on,
        # collections still do not follow the young count alone.
        seen = probe(PYTHON_GIL="1")
        self.assertTrue(seen["gil"], seen)
        self.assertEqual(seen["status"], [False, 0], seen)
        self.assertEqual(seen["seen"], {"threshold": 50_000, "granted": 0}, seen)
        self.assertTrue(seen["callbacks_unchanged"], seen)
        self.assertEqual(seen["threshold"], 50_000, seen)
        self.assertNotIn("gc_allowance", seen["log"])

    @NEEDS_ALLOWANCE
    def test_replaced_gc_functions_leave_it_off_and_the_server_says_so(self):
        # The threshold is set through the interpreter's own functions with
        # no Python code running in between. Wrapped ones cannot promise
        # that, so nothing is measured, and the start-up log gives the reason.
        seen = probe("_real = gc.get_threshold\ngc.get_threshold = lambda: _real()")
        self.assertEqual(seen["status"], [False, 0], seen)
        self.assertEqual(seen["seen"], {"threshold": 50_000, "granted": 0}, seen)
        self.assertTrue(seen["callbacks_unchanged"], seen)
        self.assertIn("gc_allowance='body' has no effect", seen["log"])
        self.assertIn("gc module have been replaced", seen["log"])
        # Unwrapped, the same interpreter measures the body and stays quiet.
        seen = probe()
        self.assertEqual(seen["status"], [True, 0], seen)
        self.assertGreaterEqual(seen["seen"]["granted"], FEWEST, seen)
        self.assertFalse(seen["callbacks_unchanged"], seen)
        self.assertEqual(seen["log"], "")

    def test_a_small_body_changes_nothing(self):
        for event_loop in self.event_loops:
            with self.subTest(event_loop=event_loop):
                client = self.client(event_loop)
                seen = self.post(client, "/seen", SMALL).json()
                self.assertEqual(
                    seen, {"threshold": self.base, "granted": 0, "items": SMALL}
                )
                self.settled()

    @NEEDS_ALLOWANCE
    def test_a_large_body_starts_no_collection_of_its_own(self):
        def collections():
            return sum(generation["collections"] for generation in gc.get_stats())

        for event_loop in self.event_loops:
            with self.subTest(event_loop=event_loop):
                client = self.client(event_loop)
                # Leave room in the budget for everything but the bodies,
                # which alone are several times the default threshold.
                gc.collect()
                self.base = 3000
                gc.set_threshold(self.base, *gc.get_threshold()[1:])
                self.post(client, "/seen", SMALL)
                gc.collect()
                before = collections()
                for _ in range(5):
                    self.assertEqual(self.post(client, "/echo").status_code, 200)
                self.assertEqual(collections(), before)
                self.settled()
                # With no allowance the same requests are collected.
                self.native._set_gc_allowance(False)
                gc.collect()
                before = collections()
                for _ in range(5):
                    self.assertEqual(self.post(client, "/echo").status_code, 200)
                self.assertGreaterEqual(collections() - before, 5)
                self.native._set_gc_allowance(True)

    @NEEDS_ALLOWANCE
    def test_a_collection_retires_the_allowance_of_a_live_body(self):
        for event_loop in self.event_loops:
            client = self.client(event_loop)
            for generation in (0, 1, 2):
                with self.subTest(event_loop=event_loop, generation=generation):
                    seen = self.post(client, f"/collects/{generation}").json()
                    self.assertGreaterEqual(seen["before"]["granted"], FEWEST)
                    self.assertEqual(
                        seen["after"], {"threshold": self.base, "granted": 0}
                    )
                    self.assertEqual(seen["items"], LARGE)
                    self.settled()

    @NEEDS_ALLOWANCE
    def test_an_automatic_collection_restores_the_application_budget(self):
        self.base = 2500
        gc.set_threshold(self.base, *gc.get_threshold()[1:])
        for event_loop in self.event_loops:
            with self.subTest(event_loop=event_loop):
                client = self.client(event_loop)
                gc.collect()
                seen = self.post(client, "/automatic-collects").json()
                self.assertGreaterEqual(seen["before"]["granted"], FEWEST)
                self.assertGreaterEqual(seen["collections"], 1, seen)
                self.assertEqual(seen["after"], {"threshold": self.base, "granted": 0})
                self.assertEqual((seen["items"], seen["last_id"]), (LARGE, LARGE - 1))
                self.settled()

    @NEEDS_ALLOWANCE
    def test_collection_preserves_a_threshold_set_by_the_application(self):
        for event_loop in self.event_loops:
            with self.subTest(event_loop=event_loop):
                gc.set_threshold(self.base, *gc.get_threshold()[1:])
                client = self.client(event_loop)
                seen = self.post(client, "/collects/0?new_base=7000").json()
                self.assertGreaterEqual(seen["before"]["granted"], FEWEST)
                self.assertEqual(seen["before"]["threshold"], 7000)
                self.assertEqual(seen["after"], {"threshold": 7000, "granted": 0})
                self.assertEqual(seen["items"], LARGE)
                self.settled(7000)

    @NEEDS_ALLOWANCE
    def test_collection_preserves_the_exception_being_handled(self):
        for event_loop in self.event_loops:
            with self.subTest(event_loop=event_loop):
                client = self.client(event_loop)
                seen = self.post(client, "/collects-in-except").json()
                self.assertGreaterEqual(seen["before"]["granted"], FEWEST)
                self.assertEqual(seen["after"], {"threshold": self.base, "granted": 0})
                self.assertTrue(seen["same_exception"])
                self.assertEqual(seen["message"], "kept while the collector runs")
                self.assertEqual(seen["items"], LARGE)
                self.settled()

    @NEEDS_ALLOWANCE
    def test_a_retired_request_cannot_release_a_later_requests_allowance(self):
        import http.client

        # Leave headroom for the test's own orchestration, so only the explicit
        # collection retires an allowance while the requests are blocked.
        self.base = 50_000
        body = order(LARGE)
        for event_loop in self.event_loops:
            with self.subTest(event_loop=event_loop):
                gc.set_threshold(self.base, *gc.get_threshold()[1:])
                self.inside.clear()
                self.release.clear()
                self.inside_new.clear()
                self.release_new.clear()
                client = self.client(event_loop)
                gc.collect()
                results = [None, None]
                errors = []

                def waiting(slot, port=client._port, results=results, errors=errors):
                    connection = http.client.HTTPConnection(
                        "127.0.0.1", port, timeout=20
                    )
                    try:
                        connection.request(
                            "POST",
                            f"/waits/{slot}",
                            body=body,
                            headers={"content-type": "application/json"},
                        )
                        response = connection.getresponse()
                        results[slot] = (response.status, json.loads(response.read()))
                    except (
                        http.client.HTTPException,
                        OSError,
                        json.JSONDecodeError,
                    ) as error:
                        errors.append(error)
                    finally:
                        connection.close()

                old = threading.Thread(target=waiting, args=(0,))
                new = threading.Thread(target=waiting, args=(1,))
                old.start()
                try:
                    self.assertTrue(self.inside.wait(10))
                    self.assertGreaterEqual(self.native._gc_allowance()[1], FEWEST)
                    gc.collect(0)
                    self.assertEqual(self.native._gc_allowance()[1], 0)
                    self.assertEqual(gc.get_threshold()[0], self.base)
                    new.start()
                    self.assertTrue(self.inside_new.wait(10))
                    later = self.native._gc_allowance()[1]
                    self.assertGreaterEqual(later, FEWEST)
                    self.assertEqual(gc.get_threshold()[0], self.base + later)
                    self.release.set()
                    old.join(10)
                    self.assertFalse(old.is_alive())
                    self.assertEqual(errors, [])
                    self.assertEqual(results[0][0], 200)
                    self.assertEqual(results[0][1]["items"], LARGE)
                    self.assertEqual(self.native._gc_allowance()[1], later)
                    self.assertEqual(gc.get_threshold()[0], self.base + later)
                finally:
                    self.release.set()
                    self.release_new.set()
                    old.join(10)
                    if new.ident is not None:
                        new.join(10)
                self.assertFalse(new.is_alive())
                self.assertEqual(errors, [])
                self.assertEqual(results[1][0], 200)
                self.assertEqual(results[1][1]["items"], LARGE)
                self.assertEqual(results[1][1]["first"]["granted"], later)
                self.assertEqual(results[1][1]["later"]["granted"], later)
                self.settled()

    @NEEDS_ALLOWANCE
    def test_the_hook_is_put_back_after_it_is_removed(self):
        import http.client

        body = order(LARGE)
        for event_loop in self.event_loops:
            with self.subTest(event_loop=event_loop):
                self.inside.clear()
                self.release.clear()
                client = self.client(event_loop)
                gc.collect()

                def waiting(port=client._port):
                    connection = http.client.HTTPConnection(
                        "127.0.0.1", port, timeout=20
                    )
                    try:
                        connection.request(
                            "POST",
                            "/waits/0",
                            body=body,
                            headers={"content-type": "application/json"},
                        )
                        connection.getresponse().read()
                    finally:
                        connection.close()

                thread = threading.Thread(target=waiting)
                thread.start()
                try:
                    self.assertTrue(self.inside.wait(10))
                    held = self.native._gc_allowance()[1]
                    self.assertGreaterEqual(held, FEWEST)
                    for hook in self.hooks():
                        gc.callbacks.remove(hook)
                    # Unwatched, a collection retires nothing.
                    gc.collect(0)
                    self.assertEqual(self.native._gc_allowance()[1], held)
                    self.assertEqual(gc.get_threshold()[0], self.base + held)
                    # The next measured body finds the hook gone. What it
                    # could not watch is retired, so this body's allowance
                    # is its own and no more, and the hook is back, last.
                    seen = self.post(client, "/seen").json()
                    self.assertTrue(FEWEST <= seen["granted"] <= MOST, seen)
                    self.assertEqual(seen["threshold"], self.base + seen["granted"])
                    (hook,) = self.hooks()
                    self.assertIs(gc.callbacks[-1], hook)
                finally:
                    self.release.set()
                    thread.join(10)
                self.assertFalse(thread.is_alive())
                self.settled()

    @NEEDS_ALLOWANCE
    def test_installing_the_hook_calls_no_other_callback_twice(self):
        # CPython walks gc.callbacks by index while a collection's callbacks
        # run. A hook put in front of the one being called would move it up
        # a place, and it would be called again for the same phase.
        calls = []
        entered, proceed, done = (threading.Event() for _ in range(3))

        def watcher(phase, info):
            if done.is_set():
                return
            calls.append(phase)
            if phase == "start" and not entered.is_set():
                entered.set()
                proceed.wait(10)
            elif phase == "stop":
                done.set()

        def forget():
            if watcher in gc.callbacks:
                gc.callbacks.remove(watcher)

        for hook in self.hooks():
            gc.callbacks.remove(hook)
        gc.callbacks.append(watcher)
        self.addCleanup(forget)
        self.addCleanup(proceed.set)
        collection = threading.Thread(target=gc.collect)
        collection.start()
        try:
            self.assertTrue(entered.wait(10))
            # The other thread is part-way through the start callbacks.
            self.native._set_gc_allowance(True)
        finally:
            proceed.set()
            collection.join(10)
        self.assertFalse(collection.is_alive())
        self.assertEqual(calls, ["start", "stop"])
        (hook,) = self.hooks()
        self.assertIs(gc.callbacks[-1], hook)
        self.assertEqual(self.native._gc_allowance(), (True, 0))

    @NEEDS_ALLOWANCE
    def test_a_failing_request_gives_its_allowance_back(self):
        for event_loop in self.event_loops:
            with self.subTest(event_loop=event_loop):
                client = self.client(event_loop)
                for _ in range(3):
                    self.assertEqual(self.post(client, "/fails").status_code, 500)
                self.settled()

    @NEEDS_ALLOWANCE
    def test_an_invalid_body_gets_none(self):
        for event_loop in self.event_loops:
            with self.subTest(event_loop=event_loop):
                client = self.client(event_loop)
                cut = order(LARGE)[:-20]
                wrong = order(LARGE).replace('"id": 7,', '"id": "seven",')
                for content in (cut, wrong):
                    response = client.post(
                        "/seen",
                        content=content,
                        headers={"content-type": "application/json"},
                    )
                    self.assertEqual(response.status_code, 422)
                    self.settled()

    @NEEDS_ALLOWANCE
    def test_a_threshold_the_application_sets_becomes_the_base(self):
        for event_loop in self.event_loops:
            with self.subTest(event_loop=event_loop):
                gc.set_threshold(self.base, *gc.get_threshold()[1:])
                client = self.client(event_loop)
                seen = self.post(client, "/sets").json()
                # Set while the allowance was in force: read back as set, and
                # still the application's value when the request has ended.
                self.assertEqual(seen["threshold"], 7000)
                self.settled(7000)
                again = self.post(client, "/seen").json()
                self.assertEqual(again["threshold"], 7000 + again["granted"])
                self.settled(7000)

    @NEEDS_ALLOWANCE
    def test_a_collector_that_is_off_is_left_alone(self):
        for event_loop in self.event_loops:
            with self.subTest(event_loop=event_loop, off="threshold"):
                self.base = 0
                gc.set_threshold(0, *gc.get_threshold()[1:])
                client = self.client(event_loop)
                self.assertEqual(self.post(client, "/seen").json()["threshold"], 0)
                self.settled()
            with self.subTest(event_loop=event_loop, off="disabled"):
                self.base = 2500
                gc.set_threshold(self.base, *gc.get_threshold()[1:])
                gc.disable()
                try:
                    seen = self.post(client, "/seen").json()
                finally:
                    gc.enable()
                self.assertEqual((seen["threshold"], seen["granted"]), (self.base, 0))
                self.settled()

    @NEEDS_ALLOWANCE
    def test_the_setting_switches_it_off(self):
        for event_loop in self.event_loops:
            with self.subTest(event_loop=event_loop):
                client = self.client(event_loop, gc_allowance="none")
                self.assertEqual(self.native._gc_allowance(), (False, 0))
                seen = self.post(client, "/seen").json()
                self.assertEqual((seen["threshold"], seen["granted"]), (self.base, 0))
                # The next server in the process has it again by default.
                client = self.client(event_loop)
                self.assertTrue(self.native._gc_allowance()[0])
                self.assertGreater(self.post(client, "/seen").json()["granted"], 0)
                self.settled()

    def test_the_setting_is_checked(self):
        import argparse

        from bessemer import App
        from bessemer.config import settings

        with self.assertRaisesRegex(
            ValueError, "gc_allowance must be 'body' or 'none'"
        ):
            App().run(port=0, gc_allowance="all")
        with self.assertRaisesRegex(
            ValueError, "gc_allowance must be 'body' or 'none'"
        ):
            settings(argparse.Namespace(gc_allowance="all"))
        self.assertEqual(settings(argparse.Namespace())["gc_allowance"], "body")
        self.assertEqual(
            settings(argparse.Namespace(gc_allowance="none"))["gc_allowance"], "none"
        )

    @NEEDS_ALLOWANCE
    def test_objects_kept_after_the_request_are_counted_again(self):
        for event_loop in self.event_loops:
            with self.subTest(event_loop=event_loop):
                self.kept.clear()
                client = self.client(event_loop)
                seen = self.post(client, "/keeps").json()
                self.assertGreaterEqual(seen["granted"], FEWEST)
                # The allowance ends with the request; the objects live on
                # and count against the application's own threshold.
                self.settled()
                self.assertEqual(len(self.kept[0].items), LARGE)
                self.kept.clear()

    @NEEDS_ALLOWANCE
    def test_requests_in_flight_together_each_have_their_own(self):
        import http.client

        for event_loop in self.event_loops:
            with self.subTest(event_loop=event_loop):
                self.inside.clear()
                self.release.clear()
                client = self.client(event_loop)
                results = []

                def waiting(port=client._port, results=results):
                    # Its own connection: the test client is one caller's.
                    connection = http.client.HTTPConnection(
                        "127.0.0.1", port, timeout=20
                    )
                    try:
                        connection.request(
                            "POST",
                            "/waits",
                            body=order(LARGE),
                            headers={"content-type": "application/json"},
                        )
                        results.append(json.loads(connection.getresponse().read()))
                    finally:
                        connection.close()

                thread = threading.Thread(target=waiting)
                thread.start()
                try:
                    self.assertTrue(self.inside.wait(10))
                    # One request holds its allowance; another comes and goes.
                    held = self.native._gc_allowance()[1]
                    self.assertGreaterEqual(held, FEWEST)
                    seen = self.post(client, "/seen").json()
                    self.assertGreaterEqual(seen["granted"], held + FEWEST)
                    self.assertEqual(seen["threshold"], self.base + seen["granted"])
                    deadline = time.monotonic() + 5
                    while self.native._gc_allowance()[1] != held:
                        self.assertLess(time.monotonic(), deadline)
                        time.sleep(0.005)
                    self.assertEqual(gc.get_threshold()[0], self.base + held)
                finally:
                    self.release.set()
                    thread.join(10)
                self.assertEqual(results[0]["first"]["granted"], held)
                self.assertEqual(results[0]["later"]["granted"], held)
                self.settled()

    @NEEDS_ALLOWANCE
    def test_a_long_body_of_few_objects_leaves_other_allowances_alone(self):
        import http.client

        # Long enough to be measured, and three containers: no allowance of
        # its own, and nothing taken from the one a request in flight holds.
        text = json.dumps({"text": "x" * 8192})
        for event_loop in self.event_loops:
            with self.subTest(event_loop=event_loop):
                self.inside.clear()
                self.release.clear()
                client = self.client(event_loop)
                alone = client.post(
                    "/note", content=text, headers={"content-type": "application/json"}
                ).json()
                self.assertEqual(
                    alone, {"threshold": self.base, "granted": 0, "length": 8192}
                )
                self.settled()

                def waiting(port=client._port):
                    connection = http.client.HTTPConnection(
                        "127.0.0.1", port, timeout=20
                    )
                    try:
                        connection.request(
                            "POST",
                            "/waits",
                            body=order(LARGE),
                            headers={"content-type": "application/json"},
                        )
                        connection.getresponse().read()
                    finally:
                        connection.close()

                thread = threading.Thread(target=waiting)
                thread.start()
                try:
                    self.assertTrue(self.inside.wait(10))
                    held = self.native._gc_allowance()[1]
                    self.assertGreaterEqual(held, FEWEST)
                    for _ in range(3):
                        seen = client.post(
                            "/note",
                            content=text,
                            headers={"content-type": "application/json"},
                        ).json()
                        self.assertEqual(seen["granted"], held)
                        self.assertEqual(seen["threshold"], self.base + held)
                    self.assertEqual(self.native._gc_allowance()[1], held)
                finally:
                    self.release.set()
                    thread.join(10)
                self.settled()

    @NEEDS_ALLOWANCE
    def test_a_request_that_times_out_gives_its_allowance_back(self):
        for event_loop in self.event_loops:
            with self.subTest(event_loop=event_loop):
                self.inside.clear()
                client = self.client(event_loop, request_timeout=0.2)
                response = self.post(client, "/sleeps")
                self.assertEqual(response.status_code, 504)
                self.assertTrue(self.inside.is_set())
                # The handler was cancelled in its sleep, and its model and
                # the allowance for it went with the task.
                self.settled()

    @NEEDS_ALLOWANCE
    def test_a_cancelled_request_gives_its_allowance_back(self):
        import socket

        for event_loop in self.event_loops:
            with self.subTest(event_loop=event_loop):
                self.inside.clear()
                client = self.client(event_loop)
                body = order(LARGE).encode()
                connection = socket.create_connection(("127.0.0.1", client._port))
                try:
                    connection.sendall(
                        b"POST /sleeps HTTP/1.1\r\nHost: t\r\n"
                        b"Content-Type: application/json\r\n"
                        b"Content-Length: %d\r\n\r\n" % len(body) + body
                    )
                    self.assertTrue(self.inside.wait(10))
                    self.assertGreaterEqual(self.native._gc_allowance()[1], FEWEST)
                finally:
                    connection.close()
                # The client has gone, so the awaiting handler is cancelled.
                self.settled()

    @NEEDS_ALLOWANCE
    def test_a_client_that_leaves_gives_its_allowance_back(self):
        import socket

        for event_loop in self.event_loops:
            with self.subTest(event_loop=event_loop):
                self.inside.clear()
                self.release.clear()
                client = self.client(event_loop)
                body = order(LARGE).encode()
                connection = socket.create_connection(("127.0.0.1", client._port))
                try:
                    connection.sendall(
                        b"POST /waits HTTP/1.1\r\nHost: t\r\n"
                        b"Content-Type: application/json\r\n"
                        b"Content-Length: %d\r\n\r\n" % len(body) + body
                    )
                    self.assertTrue(self.inside.wait(10))
                    self.assertGreaterEqual(self.native._gc_allowance()[1], FEWEST)
                finally:
                    connection.close()
                # The handler is still running on its thread and still holds
                # the model. Once it returns, nothing of the request is left
                # and the allowance goes with it, with nobody to answer.
                self.release.set()
                self.settled()


if __name__ == "__main__":
    unittest.main()
