"""Installed-package admission: healthy I/O, overload recovery, explicit caps."""

import argparse
import asyncio
import os
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
    # Every socket is opened before the first request is sent, so a wave is
    # one write per socket and leaves on time however slow this client is.
    connections = await asyncio.gather(
        *(asyncio.open_connection("127.0.0.1", port) for _ in range(count * size))
    )
    exchanges = []
    for wave in range(count):
        if wave:
            await asyncio.sleep(spacing)
        exchanges.extend(
            asyncio.ensure_future(exchange(*pair, path))
            for pair in connections[wave * size : (wave + 1) * size]
        )
    return await asyncio.gather(*exchanges)


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
        for loop in ("bessemer", "asyncio"):
            with self.subTest(loop=loop):
                app = App()

                @app.get("/block")
                async def block():
                    time.sleep(0.004)  # noqa: ASYNC251 - deliberate loop overload
                    return {"ok": True}

                @app.get("/ok")
                async def ok():
                    return {"ok": True}

                with TestClient(
                    app, timeout=10, server_options={"event_loop": loop}
                ) as client:
                    # Requests are admitted before the overload they cause is
                    # seen, and the loop stays marked for 20 ms after its last
                    # late request, so it is later waves that are refused.
                    # They must overlap the earlier ones. Sent one after the
                    # other was answered, they reached the server inside
                    # those 20 ms only from a fast client: with 40 ms between
                    # waves nothing was refused, and a slow CI runner saw
                    # 256 answers of 200. Thirty milliseconds apart, the
                    # fifth wave arrives while the first four, half a second
                    # of blocked loop, are still being served.
                    statuses = asyncio.run(waves(client._port, 8, 32, "/block", 0.03))
                    self.assertIn(503, statuses)
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
