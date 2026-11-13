"""Adaptive admission sheds for a loop that is overloaded, not one kept waiting.

With `max_in_flight="auto"` a loop whose requests wait too long to be started
is marked congested and new requests are answered 503. CPU-bound sync
handlers make the loop wait too: with the GIL their threads hold it in turn,
and the loop thread gets it back a switch interval at a time. That loop has
next to nothing to do, and refusing requests for it refuses work the server
can do. These tests drive both kinds of wait and look at what is refused.
"""

from __future__ import annotations

import collections
import importlib.util
import json
import subprocess
import sys
import time
import unittest

NATIVE = importlib.util.find_spec("bessemer._native") is not None
# The loop thread's own CPU time is what tells the two apart, and it is read
# with clocks that only these platforms have here.
CPU_CLOCKS = sys.platform in ("linux", "darwin")


# Keeps a number of requests in flight for a while and prints what came back.
#   PORT PATH CONNECTIONS SECONDS STOP_AT_STATUS
LOAD = """
import collections, http.client, json, sys, threading, time

port, path = int(sys.argv[1]), sys.argv[2]
connections, seconds, until = int(sys.argv[3]), float(sys.argv[4]), int(sys.argv[5])
statuses, details = collections.Counter(), set()
lock, finished = threading.Lock(), threading.Event()
stop = time.monotonic() + seconds


def run():
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
    mine = collections.Counter()
    try:
        while time.monotonic() < stop and not finished.is_set():
            connection.request("GET", path)
            response = connection.getresponse()
            body = response.read()
            mine[response.status] += 1
            if response.status != 200:
                details.add(body.decode())
                if response.status == until:
                    finished.set()
    finally:
        connection.close()
        with lock:
            statuses.update(mine)


threads = [threading.Thread(target=run) for _ in range(connections)]
for thread in threads:
    thread.start()
for thread in threads:
    thread.join()
print(json.dumps({"statuses": statuses, "details": sorted(details)}))
"""


def work(rounds=20_000):
    total = 0
    for value in range(rounds):
        total = (total * 31 + value) & 0xFFFFFFFF
    return total


@unittest.skipUnless(NATIVE, "needs the native extension")
class AdmissionDelayTests(unittest.TestCase):
    event_loops = ("asyncio", "bessemer") if CPU_CLOCKS else ("asyncio",)

    def client(self, event_loop):
        from bessemer import App
        from bessemer.testclient import TestClient

        app = App()

        @app.get("/sync")
        def sync():
            return {"total": work()}

        @app.get("/computing")
        async def computing():
            # On the loop, and for longer than the 5 ms target by the loop
            # thread's own clock, so a request is started late whenever
            # another is ahead of it.
            until = time.thread_time() + 0.012
            total = 0
            while time.thread_time() < until:
                total += work(1_000)
            return {"total": total}

        @app.get("/blocking")
        async def blocking():
            time.sleep(0.004)  # noqa: ASYNC251 - blocking the loop is the test
            return {"ok": True}

        client = TestClient(
            app, server_options={"event_loop": event_loop, "sync_workers": 4}
        )
        client.__enter__()
        self.addCleanup(client.__exit__, None, None, None)
        return client

    def load(self, port, path, connections, seconds, until=0):
        """Keep `connections` requests in flight from another process.

        The load must not come from this one: its threads would take turns
        with the handlers' for the GIL, and the server would be measured
        with its own clients competing against it.
        """
        completed = subprocess.run(
            [sys.executable, "-c", LOAD, str(port), path]
            + [str(connections), str(seconds), str(until)],
            capture_output=True,
            text=True,
            timeout=seconds + 60,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        seen = json.loads(completed.stdout)
        statuses = {int(status): count for status, count in seen["statuses"].items()}
        return collections.Counter(statuses), set(seen["details"])

    @unittest.skipUnless(CPU_CLOCKS, "needs per-thread CPU clocks")
    def test_cpu_bound_sync_handlers_are_not_refused_as_loop_overload(self):
        # Four threads of CPU-bound handlers and thirty-two requests in
        # flight. The loop waits for the GIL at every hand-off, far beyond
        # the 5 ms target, and has almost no work of its own. Before the
        # loop's own CPU time was consulted, the first of these was refused
        # within two seconds in each of sixteen runs, eight on each loop.
        # With sixteen in flight it often took longer than ten, so fewer
        # connections or less time would let that fault back in unnoticed.
        for event_loop in self.event_loops:
            with self.subTest(event_loop=event_loop):
                client = self.client(event_loop)
                cpu, wall = time.process_time(), time.monotonic()
                statuses, details = self.load(client._port, "/sync", 32, 4.0)
                share = (time.process_time() - cpu) / (time.monotonic() - wall)
                if statuses[503] and share < 0.8:
                    # The server runs in this process. With the GIL one
                    # handler thread is always running, so the process uses
                    # a whole core: 0.99 to 1.02 of one, measured. With every
                    # core occupied by other processes it got 0.59 to 0.67,
                    # and the loop then waits for a processor as well as for
                    # the GIL. The signal still counts that wait once the
                    # rest of the process runs for less than half of it, so
                    # a refusal here says nothing about the GIL.
                    self.skipTest(f"the host gave the server {share:.2f} of a core")
                self.assertEqual(set(statuses), {200}, (statuses, details, share))
                self.assertGreater(statuses[200], 50)

    def refused(self, event_loop, path):
        client = self.client(event_loop)
        statuses, details = self.load(client._port, path, 64, 10.0, until=503)
        self.assertGreater(statuses[503], 0, statuses)
        self.assertEqual(details, {'{"detail":"Server event loop is overloaded"}'})
        self.assertGreater(statuses[200], 0)

    def test_a_loop_behind_on_its_own_work_is_still_refused(self):
        # Every request waits behind handlers that each compute on the loop
        # for longer than the target. That is what the signal is for, and
        # the loop thread's CPU time says the wait was its own doing.
        #
        # The handler must pass the target in one piece. One that yields
        # between shorter pieces is refused only while its clients' requests
        # arrive out of step: once they fall into rounds, each round begins
        # with a request started on time, which ends the run of late ones.
        # Sixty-four connections to such a handler were refused between 46
        # and 1,508 times in six seconds by the build before the loop's CPU
        # time was consulted and between 55 and 1,311 times by this one
        # (free-threaded CPython 3.14, eight runs each), and went ten
        # seconds without a refusal in one run of 28 here and in two CI
        # runs, one of them on CPython 3.12 with the GIL.
        for event_loop in self.event_loops:
            with self.subTest(event_loop=event_loop):
                self.refused(event_loop, "/computing")

    def test_a_loop_blocked_in_calls_is_still_refused(self):
        # The loop thread sleeps in every handler. It uses no CPU, and nor
        # does the rest of the process, so nothing excuses the wait.
        for event_loop in self.event_loops:
            with self.subTest(event_loop=event_loop):
                self.refused(event_loop, "/blocking")


if __name__ == "__main__":
    unittest.main()
