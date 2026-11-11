"""A large body's objects stay out of the collector's young budget.

Parsing a body into models allocates thousands of container objects at once.
They cannot be garbage while their request runs, so the collection they would
start is put off: the young threshold rises by their number for the request
and comes back when it ends. These tests read the collector from inside the
handler, where the allowance is in force, and from outside once it is not.
"""

from __future__ import annotations

import asyncio
import gc
import importlib.util
import json
import sys
import threading
import time
import unittest

NATIVE = importlib.util.find_spec("bessemer._native") is not None
PYDANTIC = importlib.util.find_spec("pydantic") is not None
GIL = getattr(sys, "_is_gil_enabled", lambda: True)()

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

# Without the GIL other threads allocate, free and collect while a body is
# parsed, so what the parse allocated cannot be read from the collector's
# count, and no allowance is made.
NEEDS_GIL = unittest.skipUnless(GIL, "the allowance is made only with the GIL")


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
        # A base no other test's leftovers can be mistaken for.
        self.base = 2500
        gc.set_threshold(self.base, thresholds[1], thresholds[2])
        self.kept = []
        self.inside = threading.Event()
        self.release = threading.Event()
        self.addCleanup(self.release.set)

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

    @NEEDS_GIL
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

    @unittest.skipIf(GIL, "with the GIL a large body gets an allowance")
    def test_without_the_gil_no_body_gets_an_allowance(self):
        for event_loop in self.event_loops:
            with self.subTest(event_loop=event_loop):
                client = self.client(event_loop)
                self.assertEqual(self.native._gc_allowance(), (True, 0))
                for _ in range(3):
                    seen = self.post(client, "/seen").json()
                    self.assertEqual(
                        seen, {"threshold": self.base, "granted": 0, "items": LARGE}
                    )
                self.assertEqual(self.post(client, "/fails").status_code, 500)
                self.assertEqual(self.native._gc_allowance(), (True, 0))
                self.assertEqual(gc.get_threshold()[0], self.base)

    def test_a_small_body_changes_nothing(self):
        for event_loop in self.event_loops:
            with self.subTest(event_loop=event_loop):
                client = self.client(event_loop)
                seen = self.post(client, "/seen", SMALL).json()
                self.assertEqual(
                    seen, {"threshold": self.base, "granted": 0, "items": SMALL}
                )
                self.settled()

    @NEEDS_GIL
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

    @NEEDS_GIL
    def test_a_failing_request_gives_its_allowance_back(self):
        for event_loop in self.event_loops:
            with self.subTest(event_loop=event_loop):
                client = self.client(event_loop)
                for _ in range(3):
                    self.assertEqual(self.post(client, "/fails").status_code, 500)
                self.settled()

    @NEEDS_GIL
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

    @NEEDS_GIL
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

    @NEEDS_GIL
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

    @NEEDS_GIL
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

    @NEEDS_GIL
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

    @NEEDS_GIL
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

    @NEEDS_GIL
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

    @NEEDS_GIL
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

    @NEEDS_GIL
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

    @NEEDS_GIL
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
