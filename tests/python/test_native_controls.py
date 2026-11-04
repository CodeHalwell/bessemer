"""Production-control behavior through the installed native HTTP runtime."""

import http.client
import importlib.util
import json
import os
import socket
import subprocess
import sys
import time
import unittest
from concurrent.futures import ThreadPoolExecutor

from test_native_integration import Server

SECRET_ENV = "BESSEMER_TEST_CONTROL_API_KEY"
METRICS_ENV = "BESSEMER_TEST_CONTROL_METRICS_KEY"
API_SECRET = "test-api-secret-material-1234"
METRICS_SECRET = "test-metrics-secret-material-1234"


def application(settings):
    return f"""
from bessemer import App
app = App({settings})
app.health("/healthz")

@app.get("/items/{{item_id}}")
async def item(item_id: int):
    return {{"id": item_id}}
"""


def request(server, method, path, headers=None):
    connection = http.client.HTTPConnection("127.0.0.1", server.port, timeout=5)
    try:
        connection.request(method, path, headers=headers or {})
        response = connection.getresponse()
        return (
            response.status,
            {name.lower(): value for name, value in response.getheaders()},
            response.read(),
        )
    finally:
        connection.close()


@unittest.skipUnless(
    importlib.util.find_spec("bessemer._native"), "build the native extension first"
)
class NativeControlsHTTPTests(unittest.TestCase):
    @unittest.skipUnless(hasattr(os, "pread"), "requires positioned log-file reads")
    def test_concurrent_worker_logs_are_complete_json_records(self):
        server = Server(
            application=application("access_log=True"), extra_args=("--workers", "2")
        )
        try:
            expected = {f"parallel-log-{index}" for index in range(200)}
            with ThreadPoolExecutor(max_workers=16) as clients:
                statuses = list(
                    clients.map(
                        lambda identifier: request(
                            server, "GET", "/items/1", {"X-Request-ID": identifier}
                        )[0],
                        sorted(expected),
                    )
                )
            self.assertTrue(all(status in (200, 503) for status in statuses))
            deadline = time.monotonic() + 3
            while True:
                logs = os.pread(server.log.fileno(), 2_000_000, 0)
                complete = logs.rsplit(b"\n", 1)[0]
                records = [
                    json.loads(line)
                    for line in complete.decode().splitlines()
                    if line.startswith('{"') and '"event":"http_request"' in line
                ]
                actual = {record["request_id"] for record in records}
                if expected <= actual or time.monotonic() >= deadline:
                    break
                time.sleep(0.01)
            self.assertTrue(
                expected <= actual,
                f"missing {len(expected - actual)} complete access log records",
            )
        finally:
            server.close()

    def test_metrics_scrapes_are_byte_bounded_and_slow_clients_hold_admission(self):
        source = f"""
from bessemer import App
app = App(metrics_path='/metrics', metrics_token_env={METRICS_ENV!r}, openapi_url=None)
app.health('/healthz')
async def value():
    return {{'ok': True}}
for index in range(300):
    app.get('/metric-' + str(index) + '-' + 'x' * 1000)(value)
"""
        server = Server(application=source, environment={METRICS_ENV: METRICS_SECRET})
        slow = socket.socket()
        try:
            for index in range(300):
                self.assertEqual(
                    request(server, "GET", f"/metric-{index}-" + "x" * 1000)[0], 200
                )
            slow.settimeout(5)
            slow.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1024)
            slow.connect(("127.0.0.1", server.port))
            slow.sendall(
                f"GET /metrics HTTP/1.1\r\nHost: 127.0.0.1\r\nAuthorization: Bearer {METRICS_SECRET}\r\n\r\n".encode()
            )
            headers = b""
            while b"\r\n\r\n" not in headers:
                headers += slow.recv(512)
            self.assertTrue(headers.startswith(b"HTTP/1.1 200"), headers[:100])
            status, headers, _ = request(
                server, "GET", "/metrics", {"Authorization": "Bearer " + METRICS_SECRET}
            )
            self.assertEqual(status, 503)
            self.assertEqual(headers["retry-after"], "1")
            slow.close()
            deadline = time.monotonic() + 2
            while True:
                status, _, body = request(
                    server,
                    "GET",
                    "/metrics",
                    {"Authorization": "Bearer " + METRICS_SECRET},
                )
                if status != 503 or time.monotonic() >= deadline:
                    break
                time.sleep(0.01)
            self.assertEqual(status, 200)
            self.assertLessEqual(len(body), 4 * 1024 * 1024)
            lines = body.splitlines()
            total = sum(
                line.startswith(b"bessemer_http_requests_total{") for line in lines
            )
            buckets = sum(
                line.startswith(b"bessemer_http_request_duration_seconds_bucket{")
                for line in lines
            )
            self.assertEqual(buckets, total * 13)
            omitted = next(
                line
                for line in lines
                if line.startswith(b"bessemer_metrics_scrape_omitted_series ")
            )
            self.assertGreater(int(omitted.split()[-1]), 0)
        finally:
            slow.close()
            server.close()

    def test_allowed_hosts_accept_canonical_names_ports_and_subdomains(self):
        server = Server(
            application=application(
                "allowed_hosts=['127.0.0.1', 'good.test', '*.example.test', '::1']"
            )
        )
        try:
            for host in (
                "GOOD.TEST:8000",
                "good.test.",
                "sub.example.test",
                "many.sub.example.test:443",
                "[::1]:8000",
            ):
                with self.subTest(host=host):
                    self.assertEqual(
                        request(server, "GET", "/items/1", {"Host": host})[0], 200
                    )
            for host in (
                "evil.test",
                "example.test",
                "badexample.test",
                "good.test.evil",
                "good.test:invalid",
                "good.test@evil.test",
            ):
                with self.subTest(host=host):
                    status, _, _ = request(server, "GET", "/items/1", {"Host": host})
                    self.assertEqual(status, 400)
        finally:
            server.close()

    def test_api_auth_cors_preflight_and_error_headers_are_native(self):
        settings = f"api_key={{'env': {SECRET_ENV!r}, 'exclude_paths': ['/healthz']}}, cors={{'origins': ['https://app.example.test'], 'allow_methods': ['GET', 'POST'], 'allow_headers': ['X-API-Key'], 'allow_credentials': True}}"
        server = Server(
            application=application(settings), environment={SECRET_ENV: API_SECRET}
        )
        try:
            status, headers, body = request(
                server, "GET", "/items/1", {"Origin": "https://app.example.test"}
            )
            self.assertEqual(status, 401)
            self.assertEqual(json.loads(body), {"detail": "Unauthorized"})
            self.assertEqual(
                headers["access-control-allow-origin"], "https://app.example.test"
            )
            self.assertEqual(headers["access-control-allow-credentials"], "true")
            self.assertEqual(headers["x-content-type-options"], "nosniff")
            self.assertEqual(headers["x-frame-options"], "DENY")
            self.assertIn("x-request-id", headers)
            status, headers, body = request(
                server,
                "OPTIONS",
                "/no-python-route",
                {
                    "Origin": "https://app.example.test",
                    "Access-Control-Request-Method": "POST",
                    "Access-Control-Request-Headers": "X-API-Key, Content-Type",
                },
            )
            self.assertEqual((status, body), (204, b""))
            self.assertIn("POST", headers["access-control-allow-methods"])
            self.assertEqual(
                request(server, "GET", "/items/3", {"X-API-Key": API_SECRET})[0], 200
            )
            self.assertEqual(
                request(
                    server,
                    "GET",
                    "/items/3",
                    {"Origin": "https://evil.example.test", "X-API-Key": API_SECRET},
                )[0],
                403,
            )
            self.assertEqual(
                request(server, "GET", "/items/3", {"X-API-Key": "wrong-key-material"})[
                    0
                ],
                401,
            )
        finally:
            server.close()

    def test_metrics_are_opt_in_protected_and_use_route_templates(self):
        server = Server(
            application=application(
                f"metrics_path='/metrics', metrics_token_env={METRICS_ENV!r}"
            ),
            environment={METRICS_ENV: METRICS_SECRET},
        )
        try:
            self.assertEqual(
                request(server, "GET", "/items/98765?private=secret-query")[0], 200
            )
            for identifier in (
                "private-user-one",
                "private-user-two",
                "private-user-three",
            ):
                self.assertEqual(request(server, "GET", "/" + identifier)[0], 404)
            self.assertEqual(request(server, "GET", "/metrics")[0], 401)
            self.assertEqual(
                request(
                    server, "GET", "/metrics", {"Authorization": "Bearer wrong-secret"}
                )[0],
                401,
            )
            status, headers, body = request(
                server, "GET", "/metrics", {"Authorization": "bEaReR " + METRICS_SECRET}
            )
            self.assertEqual(status, 200)
            self.assertEqual(headers["cache-control"], "no-store")
            self.assertTrue(
                headers["content-type"].startswith("text/plain; version=0.0.4")
            )
            self.assertIn(
                b'bessemer_http_requests_total{route="/items/{item_id}"', body
            )
            self.assertIn(b'route="<unmatched>"', body)
            self.assertIn(b"bessemer_http_request_duration_seconds_bucket", body)
            for private in (
                b"98765",
                b"secret-query",
                b"private-user",
                METRICS_SECRET.encode(),
            ):
                self.assertNotIn(private, body)
            self.assertEqual(
                request(
                    server,
                    "HEAD",
                    "/metrics",
                    {"Authorization": "Bearer " + METRICS_SECRET},
                )[2],
                b"",
            )
            status, headers, _ = request(
                server,
                "POST",
                "/metrics",
                {"Authorization": "Bearer " + METRICS_SECRET},
            )
            self.assertEqual(status, 405)
            self.assertEqual(headers["allow"], "GET, HEAD")
        finally:
            server.close()

    def test_rate_limit_returns_retry_after_and_health_can_be_exempt(self):
        server = Server(
            application=application(
                "rate_limit={'requests': 2, 'window_seconds': 1, 'max_keys': 2, 'exclude_paths': ['/healthz']}"
            )
        )
        try:
            self.assertEqual(request(server, "GET", "/items/1")[0], 200)
            self.assertEqual(request(server, "GET", "/items/2")[0], 200)
            status, headers, _ = request(server, "GET", "/items/3")
            self.assertEqual(status, 429)
            self.assertGreaterEqual(int(headers["retry-after"]), 1)
            self.assertEqual(request(server, "GET", "/healthz")[0], 200)
            time.sleep(1.1)
            self.assertEqual(request(server, "GET", "/items/3")[0], 200)
        finally:
            server.close()

    def test_request_id_validation_and_controls_can_be_disabled(self):
        server = Server(application=application(""))
        try:
            self.assertEqual(
                request(server, "GET", "/items/1", {"X-Request-ID": "correlation-123"})[
                    1
                ]["x-request-id"],
                "correlation-123",
            )
            _, headers, _ = request(
                server, "GET", "/items/1", {"X-Request-ID": "invalid value with spaces"}
            )
            self.assertNotIn(" ", headers["x-request-id"])
            self.assertNotEqual(headers["x-request-id"], "invalid value with spaces")
        finally:
            server.close()
        server = Server(
            application=application("security_headers=False, request_id=False")
        )
        try:
            _, headers, _ = request(server, "GET", "/items/1")
            self.assertNotIn("x-request-id", headers)
            self.assertNotIn("x-frame-options", headers)
        finally:
            server.close()

    def test_metrics_and_preflight_obey_the_configured_rate_limit(self):
        settings = f"metrics_path='/metrics', metrics_token_env={METRICS_ENV!r}, cors={{'origins': ['https://app.example.test']}}, rate_limit={{'requests': 2, 'window_seconds': 30, 'exclude_paths': ['/healthz']}}"
        server = Server(
            application=application(settings), environment={METRICS_ENV: METRICS_SECRET}
        )
        try:
            self.assertEqual(
                request(
                    server,
                    "OPTIONS",
                    "/items/1",
                    {
                        "Origin": "https://app.example.test",
                        "Access-Control-Request-Method": "GET",
                    },
                )[0],
                204,
            )
            self.assertEqual(
                request(
                    server,
                    "GET",
                    "/metrics",
                    {"Authorization": "Bearer " + METRICS_SECRET},
                )[0],
                200,
            )
            status, headers, _ = request(
                server,
                "GET",
                "/metrics",
                {
                    "Authorization": "Bearer " + METRICS_SECRET,
                    "Origin": "https://app.example.test",
                },
            )
            self.assertEqual(status, 429)
            self.assertEqual(
                headers["access-control-allow-origin"], "https://app.example.test"
            )
        finally:
            server.close()

    @unittest.skipUnless(hasattr(os, "pread"), "requires positioned log-file reads")
    def test_access_logs_omit_raw_paths_queries_headers_and_body(self):
        server = Server(application=application("access_log=True"))
        try:
            self.assertEqual(
                request(
                    server,
                    "GET",
                    "/items/871236?password=query-secret",
                    {
                        "Authorization": "Bearer authorization-secret",
                        "Cookie": "session=cookie-secret",
                        "X-API-Key": "apikey-secret",
                        "X-Request-ID": "log-correlation",
                    },
                )[0],
                200,
            )
            self.assertEqual(request(server, "GET", "/raw-private-identifier")[0], 404)
            deadline = time.monotonic() + 2
            logs = b""
            while time.monotonic() < deadline:
                logs = os.pread(server.log.fileno(), 100_000, 0)
                if b'"route":"<unmatched>"' in logs and b"log-correlation" in logs:
                    break
                time.sleep(0.01)
            self.assertIn(b'"route":"/items/{item_id}"', logs)
            self.assertIn(b"log-correlation", logs)
            for private in (
                b"871236",
                b"query-secret",
                b"authorization-secret",
                b"cookie-secret",
                b"apikey-secret",
                b"raw-private-identifier",
            ):
                self.assertNotIn(private, logs)
            events = [
                json.loads(line)
                for line in logs.decode().splitlines()
                if line.startswith('{"') and '"event":"http_request"' in line
            ]
            self.assertTrue(events)
            self.assertTrue(
                all(
                    set(event)
                    == {
                        "event",
                        "timestamp",
                        "method",
                        "route",
                        "status",
                        "duration_ms",
                        "request_id",
                        "client_ip",
                    }
                    for event in events
                )
            )
        finally:
            server.close()

    def test_missing_and_invalid_secret_environment_values_fail_before_listening(self):
        code = f"from bessemer import App; app=App(api_key={{'env': {SECRET_ENV!r}}}); app.health('/healthz'); app.run(port=0, graceful_timeout=0.1)"
        for secret in (None, "short", "secret value with spaces"):
            environment = os.environ.copy()
            environment.pop(SECRET_ENV, None)
            if secret is not None:
                environment[SECRET_ENV] = secret
            result = subprocess.run(
                [sys.executable, "-c", code],
                env=environment,
                capture_output=True,
                timeout=5,
                check=False,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn(SECRET_ENV.encode(), result.stderr)
            if secret:
                self.assertNotIn(secret.encode(), result.stderr)


if __name__ == "__main__":
    unittest.main()
