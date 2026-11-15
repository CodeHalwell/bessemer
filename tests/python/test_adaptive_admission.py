"""Installed-package admission: healthy I/O, overload recovery, explicit caps."""

import argparse
import asyncio
import collections
import inspect
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from bessemer import App
from bessemer.cli import main
from bessemer.config import SERVER_DEFAULTS, settings, validate_server_limits
from bessemer.testclient import TestClient


async def exchange(reader, writer, path):
    writer.write(
        f"GET {path} HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n".encode()
    )
    await writer.drain()
    response = await reader.read()
    writer.close()
    await writer.wait_closed()
    return int(response.split(b" ", 2)[1])


async def burst(port, count, path):
    # Establish sockets first, so the test measures one request burst rather
    # than connection creation or the client's own scheduling delay.
    connections = await asyncio.gather(
        *(asyncio.open_connection("127.0.0.1", port) for _ in range(count))
    )
    return await asyncio.gather(*(exchange(*pair, path) for pair in connections))


async def waves(port, count, size, path, spacing):
    """Waves of requests, with when each wave left and each answer came.

    Times are milliseconds from the first wave. A test that fails for a
    reason of timing has nothing else to show for it.
    """
    # Every socket is opened before the first request is sent, so a wave is
    # one write per socket and leaves on time however slow this client is.
    connections = await asyncio.gather(
        *(asyncio.open_connection("127.0.0.1", port) for _ in range(count * size))
    )
    began = time.monotonic()
    sent, answered = [], []

    def elapsed():
        return round((time.monotonic() - began) * 1000)

    async def timed(pair):
        status = await exchange(*pair, path)
        answered.append((elapsed(), status))
        return status

    exchanges = []
    for wave in range(count):
        if wave:
            await asyncio.sleep(spacing)
        sent.append(elapsed())
        exchanges.extend(
            asyncio.ensure_future(timed(pair))
            for pair in connections[wave * size : (wave + 1) * size]
        )
    statuses = await asyncio.gather(*exchanges)
    return {"statuses": statuses, "sent": sent, "answered": sorted(answered)}


def blocking_app(seconds):
    """An application whose `/block` holds the event loop for `seconds`."""
    app = App()

    @app.get("/block")
    async def block():
        time.sleep(seconds)  # noqa: ASYNC251 - deliberate loop overload
        return {"ok": True}

    @app.get("/ok")
    async def ok():
        return {"ok": True}

    return app


def waves_from_another_process(port, count, size, path, spacing):
    """Run `waves` in a process of its own and return what it returns."""
    script = "\n".join(
        (
            "import asyncio, json, sys, time",
            inspect.getsource(exchange),
            inspect.getsource(waves),
            "port, count, size, path, spacing = sys.argv[1:]",
            "seen = asyncio.run(",
            "    waves(int(port), int(count), int(size), path, float(spacing))",
            ")",
            "print(json.dumps(seen))",
        )
    )
    completed = subprocess.run(
        [sys.executable, "-c", script, str(port), str(count), str(size)]
        + [path, str(spacing)],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    if completed.returncode:
        raise AssertionError(completed.stderr)
    return json.loads(completed.stdout)


def timeline(seen):
    """What a burst looked like from its client, for a failure message.

    The loop answers a batch when it has run all of it, so answers arrive in
    groups: each is given as when it came, how many answers and their status.
    """
    groups = []
    for at, status in seen["answered"]:
        if groups and groups[-1][2] == status and at - groups[-1][0] <= 3:
            groups[-1][1] += 1
        else:
            groups.append([at, 1, status])
    answers = ", ".join(f"{count} of {status} at {at}" for at, count, status in groups)
    return f"waves left at {seen['sent']} ms; answers: {answers}"


class AdaptiveConfigTests(unittest.TestCase):
    def test_default_and_explicit_integer_environment(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(settings(argparse.Namespace())["max_in_flight"], "auto")
            os.environ["BESSEMER_MAX_IN_FLIGHT"] = "17"
            self.assertEqual(settings(argparse.Namespace())["max_in_flight"], 17)
            os.environ["BESSEMER_MAX_IN_FLIGHT"] = "auto"
            self.assertEqual(settings(argparse.Namespace())["max_in_flight"], "auto")

    def test_cli_aliases_and_project_setting(self):
        for flag in ("--max-in-flight", "--backpressure"):
            for value in ("auto", "17"):
                with (
                    patch.dict(os.environ, {}, clear=True),
                    patch("bessemer.supervisor.run", return_value=0) as run,
                ):
                    self.assertEqual(main(["run", "sample:app", flag, value]), 0)
                self.assertEqual(
                    run.call_args.args[1]["max_in_flight"],
                    "auto" if value == "auto" else 17,
                )
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory, "pyproject.toml")
            for value, expected in (("'auto'", "auto"), ("17", 17)):
                project.write_text(f"[tool.bessemer]\nmax_in_flight={value}\n")
                with (
                    patch("bessemer.config.Path", return_value=project),
                    patch.dict(os.environ, {}, clear=True),
                ):
                    self.assertEqual(
                        settings(argparse.Namespace())["max_in_flight"], expected
                    )

    def test_auto_is_additive_and_invalid_values_still_fail(self):
        for value in ("auto", 1, 128, 1_000_000):
            validate_server_limits(SERVER_DEFAULTS | {"max_in_flight": value})
        for value in (True, 0, -1, "128", "AUTO", None, 1_000_001):
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_server_limits(SERVER_DEFAULTS | {"max_in_flight": value})


class AdaptiveHTTPTests(unittest.TestCase):
    def test_a_thousand_healthy_suspended_requests_are_admitted(self):
        app = App()

        @app.get("/io")
        async def io():
            await asyncio.sleep(0.001)
            return {"ok": True}

        for loop in ("bessemer", "asyncio"):
            with self.subTest(loop=loop):
                with TestClient(
                    app, timeout=10, server_options={"event_loop": loop}
                ) as client:
                    statuses = asyncio.run(burst(client._port, 1000, "/io"))
                self.assertEqual(set(statuses), {200})

    def test_overload_sheds_then_recovers(self):
        # Waves of requests to a handler that blocks the loop, thirty
        # milliseconds apart so that later waves arrive behind earlier ones.
        # Requests are admitted before the overload they cause is seen, so it
        # is the later waves that are refused.
        #
        # Two handlers. One blocks for 4 ms, in twelve waves of 32. The other
        # blocks for 25 ms, in twenty-four waves of 4, which is longer than
        # the 20 ms a loop stays marked for, and is how long the first one
        # takes on a macOS CI runner, where a sleep of 4 ms lasts 15 to 30.
        # A mark counted from the late request lapsed under such a handler
        # before the next request was started: of 160 bursts on those
        # runners 17 were answered in full, and on Linux every burst to the
        # second handler was on the Rust loop. The mark now holds until the
        # batch ends.
        #
        # A loop is marked when requests have been late for 100 ms, which
        # takes two of them at least, and the waves must still be coming
        # then. On those runners the sleeps last four or five times what
        # they ask for, the 25 ms one about 120, so the waves go on for 350
        # and 720 ms.
        #
        # The waves come from another process. Sent from this one, the
        # client's own threads are work done elsewhere in the server's
        # process, which is what excuses a loop kept waiting, and how much of
        # it there is depends on the machine. A measured amount of such work
        # is the business of
        # test_a_blocked_loop_is_refused_while_other_threads_work.
        for loop in ("bessemer", "asyncio"):
            for blocked, size, count in ((0.004, 32, 12), (0.025, 4, 24)):
                with (
                    self.subTest(loop=loop, blocked=blocked),
                    TestClient(
                        blocking_app(blocked),
                        timeout=10,
                        server_options={"event_loop": loop},
                    ) as client,
                ):
                    cpu, began = time.process_time(), time.monotonic()
                    seen = waves_from_another_process(
                        client._port, count, size, "/block", 0.03
                    )
                    share = (time.process_time() - cpu) / (time.monotonic() - began)
                    statuses = collections.Counter(seen["statuses"])
                    self.assertEqual(
                        set(statuses),
                        {200, 503},
                        f"{dict(statuses)}; the server used {share:.2f} of a core; "
                        + timeline(seen),
                    )
                    # The batch that was late has ended, and its answers
                    # with it: 150 ms on, well past the 20 ms of a mark, a
                    # request is admitted at the first attempt.
                    time.sleep(0.15)
                    self.assertEqual(client.get("/ok").status_code, 200)

    def test_an_explicit_integer_keeps_its_fixed_meaning(self):
        app = App()

        @app.get("/io")
        async def io():
            await asyncio.sleep(0.05)
            return {"ok": True}

        with TestClient(app, timeout=10, server_options={"max_in_flight": 1}) as client:
            statuses = asyncio.run(burst(client._port, 16, "/io"))
        self.assertIn(200, statuses)
        self.assertIn(503, statuses)
