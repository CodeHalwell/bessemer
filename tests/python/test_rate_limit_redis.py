"""Rate limits shared through Redis across applications (workers/replicas)."""

import os
import shutil
import socket
import subprocess
import tempfile
import time
import unittest

from bessemer import App
from bessemer.middleware import validate_settings
from bessemer.testclient import TestClient

URL_ENV = "BESSEMER_TEST_RATE_REDIS_URL"
DOWN_ENV = "BESSEMER_TEST_RATE_REDIS_DOWN"
REDIS = shutil.which("redis-server")
# CI installs Redis and sets this so a missing server fails instead of skipping.
REQUIRED = os.environ.get("BESSEMER_REQUIRE_REDIS") == "1"


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def command(port, *arguments, password="secret"):
    """Send RESP commands over a fresh connection and return the last reply."""
    frames = []
    for args in (("AUTH", password), *arguments):
        frame = f"*{len(args)}\r\n"
        for arg in args:
            frame += f"${len(str(arg).encode())}\r\n{arg}\r\n"
        frames.append(frame)
    with socket.create_connection(("127.0.0.1", port), timeout=5) as sock:
        sock.sendall("".join(frames).encode())
        time.sleep(0.1)
        return sock.recv(65536).decode()


def app(prefix="bessemer:rate:", env=URL_ENV, requests=3, window=60, **redis):
    application = App(
        rate_limit={
            "requests": requests,
            "window_seconds": window,
            "exclude_paths": ["/free", "/metrics"],
            "redis": {"url_env": env, "key_prefix": prefix, **redis},
        },
        metrics_path="/metrics",
        metrics_token_env="BESSEMER_TEST_RATE_METRICS_TOKEN",
    )

    @application.get("/")
    async def root():
        return {"ok": True}

    @application.get("/free")
    async def free():
        return {"ok": True}

    return application


@unittest.skipUnless(REDIS or REQUIRED, "redis-server is not installed")
class RedisRateLimitTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.port = free_port()
        cls.directory = tempfile.TemporaryDirectory()
        cls.server = subprocess.Popen(
            [
                REDIS or "redis-server",
                "--port",
                str(cls.port),
                "--bind",
                "127.0.0.1",
                "--save",
                "",
                "--appendonly",
                "no",
                "--requirepass",
                "secret",
                "--dir",
                cls.directory.name,
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        deadline = time.monotonic() + 10
        while True:
            try:
                if "PONG" in command(cls.port, ("PING",)):
                    break
            except OSError:
                pass
            if time.monotonic() > deadline:
                raise AssertionError("redis-server did not start")
            time.sleep(0.05)
        os.environ[URL_ENV] = f"redis://:secret@127.0.0.1:{cls.port}/2"
        os.environ[DOWN_ENV] = f"redis://127.0.0.1:{free_port()}"
        os.environ["BESSEMER_TEST_RATE_METRICS_TOKEN"] = "m" * 32

    @classmethod
    def tearDownClass(cls):
        cls.server.terminate()
        cls.server.wait(10)
        cls.directory.cleanup()
        for name in (URL_ENV, DOWN_ENV, "BESSEMER_TEST_RATE_METRICS_TOKEN"):
            os.environ.pop(name, None)

    def setUp(self):
        command(self.port, ("SELECT", "2"), ("FLUSHDB",))

    def metrics(self, client):
        return client.get(
            "/metrics", headers={"authorization": "Bearer " + "m" * 32}
        ).text

    def test_two_applications_share_one_quota(self):
        first, second = app(), app()
        with TestClient(first) as one, TestClient(second) as two:
            self.assertEqual(one.get("/").status_code, 200)
            self.assertEqual(two.get("/").status_code, 200)
            self.assertEqual(one.get("/").status_code, 200)
            limited = two.get("/")
            self.assertEqual(limited.status_code, 429)
            self.assertEqual(limited.json()["detail"], "Rate limit exceeded")
            self.assertTrue(1 <= int(limited.headers["retry-after"]) <= 60)
            self.assertEqual(one.get("/").status_code, 429)
            # Excluded paths are not counted.
            self.assertEqual(two.get("/free").status_code, 200)
            self.assertIn("bessemer_rate_limit_store_errors_total 0", self.metrics(one))
        # The quota lives in the configured database under the prefix.
        keys = command(self.port, ("SELECT", "2"), ("KEYS", "bessemer:rate:*"))
        self.assertIn("bessemer:rate:127.0.0.1", keys)
        ttl = command(self.port, ("SELECT", "2"), ("PTTL", "bessemer:rate:127.0.0.1"))
        self.assertTrue(0 < int(ttl.strip().split(":")[-1]) <= 60_000, ttl)
        # Another prefix is an independent quota.
        with TestClient(app(prefix="other:")) as client:
            self.assertEqual(client.get("/").status_code, 200)

    def test_the_window_slides(self):
        with TestClient(app(requests=1, window=0.5)) as client:
            self.assertEqual(client.get("/").status_code, 200)
            limited = client.get("/")
            self.assertEqual(limited.status_code, 429)
            self.assertEqual(limited.headers["retry-after"], "1")
            time.sleep(0.6)
            self.assertEqual(client.get("/").status_code, 200)

    def test_the_script_is_reloaded_after_a_flush(self):
        with TestClient(app()) as client:
            self.assertEqual(client.get("/").status_code, 200)
            command(self.port, ("SCRIPT", "FLUSH"))
            self.assertEqual(client.get("/").status_code, 200)
            self.assertIn(
                "bessemer_rate_limit_store_errors_total 0", self.metrics(client)
            )

    def test_an_unreachable_store_fails_closed_unless_told_otherwise(self):
        with TestClient(app(env=DOWN_ENV, timeout_seconds=0.5)) as client:
            response = client.get("/")
            self.assertEqual(response.status_code, 503)
            self.assertEqual(response.json()["detail"], "Rate limiter is unavailable")
            self.assertEqual(response.headers["retry-after"], "1")
            self.assertIn(
                "bessemer_rate_limit_store_errors_total 1", self.metrics(client)
            )
        with TestClient(app(env=DOWN_ENV, fail_open=True)) as client:
            self.assertEqual(client.get("/").status_code, 200)
            self.assertIn(
                "bessemer_rate_limit_store_errors_total 1", self.metrics(client)
            )

    def test_wrong_credentials_fail_closed(self):
        os.environ["BESSEMER_TEST_RATE_REDIS_WRONG"] = (
            f"redis://:wrong@127.0.0.1:{self.port}"
        )
        self.addCleanup(os.environ.pop, "BESSEMER_TEST_RATE_REDIS_WRONG", None)
        with TestClient(app(env="BESSEMER_TEST_RATE_REDIS_WRONG")) as client:
            self.assertEqual(client.get("/").status_code, 503)


class RedisRateLimitSettingsTests(unittest.TestCase):
    def test_settings_are_normalised(self):
        rate = validate_settings(
            {
                "rate_limit": {
                    "requests": 5,
                    "window_seconds": 1,
                    "redis": {"url_env": "RATE_REDIS_URL"},
                }
            }
        )["rate_limit"]
        self.assertNotIn("max_keys", rate)
        self.assertEqual(
            rate["redis"],
            {
                "url_env": "RATE_REDIS_URL",
                "key_prefix": "bessemer:rate:",
                "timeout_seconds": 0.25,
                "pool_size": 16,
                "fail_open": False,
            },
        )

    def test_invalid_settings(self):
        base = {"requests": 1, "window_seconds": 1}
        for redis, extra in (
            ({}, {}),
            ({"url_env": "not a name"}, {}),
            ({"url_env": "U", "extra": 1}, {}),
            ({"url_env": "U", "pool_size": 0}, {}),
            ({"url_env": "U", "pool_size": True}, {}),
            ({"url_env": "U", "timeout_seconds": 0}, {}),
            ({"url_env": "U", "key_prefix": "has space"}, {}),
            ({"url_env": "U", "fail_open": 1}, {}),
            ({"url_env": "U", "ca_file": ""}, {}),
            ({"url_env": "U"}, {"max_keys": 10}),
            ("redis://localhost", {}),
        ):
            config = {**base, **extra, "redis": redis}
            with self.subTest(config=config), self.assertRaises(ValueError):
                validate_settings({"rate_limit": config})

    def test_startup_rejects_a_missing_or_invalid_url(self):
        os.environ["BESSEMER_TEST_RATE_REDIS_BAD"] = "http://:secret@cache"
        self.addCleanup(os.environ.pop, "BESSEMER_TEST_RATE_REDIS_BAD", None)
        for env, message in (
            ("BESSEMER_TEST_RATE_REDIS_ABSENT", "unavailable"),
            ("BESSEMER_TEST_RATE_REDIS_BAD", "redis:// or rediss://"),
        ):
            application = App(
                rate_limit={
                    "requests": 1,
                    "window_seconds": 1,
                    "redis": {"url_env": env},
                }
            )
            with (
                self.subTest(env=env),
                self.assertRaisesRegex(ValueError, message) as raised,
            ):
                application.run(port=0)
            # The URL's password never reaches the error.
            self.assertNotIn("secret", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
