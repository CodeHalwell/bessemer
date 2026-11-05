"""OpenTelemetry: W3C Trace Context propagation and OTLP/HTTP JSON export."""

import json
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

from bessemer import App, HTTPException, OpenTelemetry, Request, otel
from bessemer.testclient import TestClient
from opentelemetry import trace as otel_trace

PARENT = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"
TRACE_ID = "4bf92f3577b34da6a3ce929d0e0e4736"


class Collector:
    """A minimal OTLP/HTTP receiver that records each export body."""

    def __init__(self, status=200):
        self.bodies = []
        self.headers = []
        collector = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers["content-length"])
                collector.headers.append(
                    {key.lower(): value for key, value in self.headers.items()}
                )
                collector.bodies.append(json.loads(self.rfile.read(length)))
                self.send_response(status)
                self.send_header("content-length", "2")
                self.end_headers()
                self.wfile.write(b"{}")

            def log_message(self, *_):
                pass

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.endpoint = f"http://127.0.0.1:{self.server.server_port}/v1/traces"

    def spans(self, count, timeout=5):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            spans = [
                span
                for body in self.bodies
                for resource in body["resourceSpans"]
                for scope in resource["scopeSpans"]
                for span in scope["spans"]
            ]
            if len(spans) >= count:
                return spans
            time.sleep(0.02)
        raise AssertionError(f"expected {count} spans, got {self.bodies}")

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def attributes(span):
    return {
        item["key"]: next(iter(item["value"].values())) for item in span["attributes"]
    }


def build(settings):
    app = App(otel=settings)

    @app.get("/items/{item_id}")
    async def item(item_id: int, request: Request):
        current = otel_trace.get_current_span().get_span_context()
        return {
            "traceparent": request.traceparent,
            "sdk": [
                format(current.trace_id, "032x"),
                format(current.span_id, "016x"),
            ],
        }

    @app.get("/plain")
    def plain():
        # A sync handler runs on the executor with the context copied.
        current = otel_trace.get_current_span().get_span_context()
        return {"trace": format(current.trace_id, "032x")}

    @app.get("/boom")
    async def boom():
        raise HTTPException(503, "down")

    @app.middleware("http")
    async def tag(request, call_next):
        response = await call_next(request)
        response.headers["x-traceparent"] = request.traceparent or ""
        return response

    return app


class OpenTelemetryTests(unittest.TestCase):
    def test_server_spans_join_the_callers_trace_and_are_exported(self):
        collector = Collector()
        self.addCleanup(collector.close)
        settings = OpenTelemetry(endpoint=collector.endpoint, service_name="orders")
        with TestClient(build(settings)) as client:
            response = client.get("/items/7", headers={"traceparent": PARENT})
            body = response.json()
            version, trace_id, span_id, flags = body["traceparent"].split("-")
            self.assertEqual((version, trace_id, flags), ("00", TRACE_ID, "01"))
            self.assertNotEqual(span_id, "00f067aa0ba902b7")
            # The OpenTelemetry API sees the server span as the current span.
            self.assertEqual(body["sdk"], [trace_id, span_id])
            self.assertEqual(response.headers["x-traceparent"], body["traceparent"])
            self.assertEqual(client.get("/boom").status_code, 503)
            fresh = client.get("/plain").json()["trace"]
            self.assertNotIn(fresh, (TRACE_ID, "0" * 32))
            spans = collector.spans(3)
        resource = collector.bodies[0]["resourceSpans"][0]["resource"]
        self.assertIn(
            {"key": "service.name", "value": {"stringValue": "orders"}},
            resource["attributes"],
        )
        by_name = {span["name"]: span for span in spans}
        item = by_name["GET /items/{item_id}"]
        self.assertEqual(
            (item["traceId"], item["spanId"], item["parentSpanId"], item["kind"]),
            (TRACE_ID, span_id, "00f067aa0ba902b7", 2),
        )
        self.assertEqual(
            attributes(item),
            {
                "http.request.method": "GET",
                "url.path": "/items/7",
                "http.route": "/items/{item_id}",
                "http.response.status_code": "200",
                "client.address": "127.0.0.1",
            },
        )
        self.assertNotIn("status", item)
        self.assertEqual(by_name["GET /boom"]["status"], {"code": 2})
        root = by_name["GET /plain"]
        self.assertEqual(root["traceId"], fresh)
        self.assertNotIn("parentSpanId", root)
        self.assertLess(int(root["startTimeUnixNano"]), int(root["endTimeUnixNano"]))
        self.assertEqual(
            collector.headers[0]["content-type"].split(";")[0], "application/json"
        )

    def test_sampling_and_invalid_parents(self):
        collector = Collector()
        self.addCleanup(collector.close)
        settings = OpenTelemetry(endpoint=collector.endpoint, sample_ratio=0)
        with TestClient(build(settings)) as client:
            # A root trace is not sampled at ratio 0, but still propagates.
            unsampled = client.get("/items/1").json()["traceparent"]
            self.assertTrue(unsampled.endswith("-00"))
            # A caller's sampling decision wins over the ratio.
            client.get("/items/2", headers={"traceparent": PARENT})
            # An invalid traceparent starts a new trace.
            invalid = client.get(
                "/items/3", headers={"traceparent": PARENT.upper()}
            ).json()["traceparent"]
            self.assertNotIn(TRACE_ID, invalid)
            spans = collector.spans(1)
            time.sleep(1.2)
        self.assertEqual(len(collector.spans(1)), 1)
        self.assertEqual(spans[0]["traceId"], TRACE_ID)

    def test_without_an_endpoint_the_callers_context_is_propagated(self):
        with TestClient(build(OpenTelemetry())) as client:
            body = client.get(
                "/items/1", headers={"traceparent": PARENT, "tracestate": "v=1"}
            ).json()
            self.assertEqual(body["traceparent"], PARENT)
            self.assertEqual(body["sdk"], [TRACE_ID, "00f067aa0ba902b7"])
            self.assertIsNone(client.get("/items/1").json()["traceparent"])
        # Without App(otel=...) nothing is propagated.
        with TestClient(build(None)) as client:
            body = client.get("/items/1", headers={"traceparent": PARENT}).json()
            self.assertIsNone(body["traceparent"])
            self.assertEqual(body["sdk"], ["0" * 32, "0" * 16])

    def test_failed_exports_are_counted(self):
        collector = Collector(status=500)
        self.addCleanup(collector.close)
        app = App(
            otel=OpenTelemetry(endpoint=collector.endpoint),
            metrics_path="/metrics",
            metrics_token_env="BESSEMER_TEST_OTEL_METRICS_TOKEN",
        )

        @app.get("/")
        async def root():
            return {}

        token = "t" * 32
        import os

        os.environ["BESSEMER_TEST_OTEL_METRICS_TOKEN"] = token
        self.addCleanup(os.environ.pop, "BESSEMER_TEST_OTEL_METRICS_TOKEN", None)
        with TestClient(app) as client:
            client.get("/")
            collector.spans(1)
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                metrics = client.get(
                    "/metrics", headers={"authorization": f"Bearer {token}"}
                ).text
                if "bessemer_otel_spans_dropped_total 1" in metrics:
                    break
                time.sleep(0.05)
            self.assertIn("bessemer_otel_spans_dropped_total 1", metrics)

    def test_settings_from_the_environment(self):
        settings = OpenTelemetry.from_env(
            {
                "OTEL_EXPORTER_OTLP_ENDPOINT": "http://collector:4318/",
                "OTEL_SERVICE_NAME": "api",
                "OTEL_TRACES_SAMPLER": "parentbased_traceidratio",
                "OTEL_TRACES_SAMPLER_ARG": "0.25",
                "OTEL_EXPORTER_OTLP_HEADERS": "authorization=Bearer%20x",
            }
        )
        self.assertEqual(
            settings,
            OpenTelemetry(
                endpoint="http://collector:4318/v1/traces",
                service_name="api",
                sample_ratio=0.25,
                headers_env="OTEL_EXPORTER_OTLP_HEADERS",
            ),
        )
        traces = OpenTelemetry.from_env(
            {
                "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT": "https://c/x",
                "OTEL_EXPORTER_OTLP_ENDPOINT": "http://ignored",
                "OTEL_TRACES_SAMPLER": "always_off",
            }
        )
        self.assertEqual((traces.endpoint, traces.sample_ratio), ("https://c/x", 0.0))
        self.assertEqual(OpenTelemetry.from_env({}), OpenTelemetry())
        for environ, message in (
            ({"OTEL_EXPORTER_OTLP_PROTOCOL": "grpc"}, "http/json"),
            ({"OTEL_TRACES_SAMPLER": "jaeger_remote"}, "not supported"),
            (
                {"OTEL_TRACES_SAMPLER": "traceidratio", "OTEL_TRACES_SAMPLER_ARG": "x"},
                "number",
            ),
            ({"OTEL_SDK_DISABLED": "true"}, "OTEL_SDK_DISABLED"),
        ):
            with self.assertRaisesRegex(ValueError, message):
                OpenTelemetry.from_env(environ)

    def test_invalid_settings(self):
        for kwargs, message in (
            ({"endpoint": "collector:4318"}, "http or https"),
            ({"sample_ratio": 1.5}, "sample_ratio"),
            ({"sample_ratio": True}, "sample_ratio"),
            ({"service_name": ""}, "service_name"),
            ({"headers_env": "not a name"}, "environment variable"),
            ({"ca_file": "/ca.pem"}, "need an endpoint"),
        ):
            with self.assertRaisesRegex(ValueError, message):
                OpenTelemetry(**kwargs)
        with self.assertRaisesRegex(ValueError, "unavailable"):
            build(
                OpenTelemetry(
                    endpoint="http://127.0.0.1:9/v1/traces",
                    headers_env="BESSEMER_TEST_OTEL_MISSING_HEADERS",
                )
            ).run(port=0)
        with self.assertRaisesRegex(ValueError, "otel must be OpenTelemetry"):
            build({"endpoint": "http://x", "extra": 1}).build()

    def test_the_handoff_needs_no_opentelemetry_install(self):
        saved = otel._api
        self.addCleanup(setattr, otel, "_api", saved)
        otel._api = False
        self.assertIsNone(otel.attach({"traceparent": PARENT}))
        otel.detach(None)


if __name__ == "__main__":
    unittest.main()
