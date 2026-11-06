"""Checks for the head-to-head harness that need no servers or load generator."""

from __future__ import annotations

import json
import shutil
import sys
import threading
import unittest
import urllib.error
import urllib.request
from http.server import HTTPServer
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent / "compare"))

import server_agent
import summarise
from servers import cpu_list, cpu_times, parse_config, utilisation

DIRECT = urllib.request.build_opener(urllib.request.ProxyHandler({}))


class ConfigTests(unittest.TestCase):
    def test_parse_config(self):
        self.assertEqual(parse_config("1-core"), ("0", 1))
        self.assertEqual(parse_config("8-core"), ("0-7", 8))
        for bad in ("core", "0-core", "4-cores", "x-core"):
            with self.assertRaises(ValueError):
                parse_config(bad)

    def test_cpu_list(self):
        self.assertEqual(cpu_list("0-2,5"), [0, 1, 2, 5])

    @unittest.skipUnless(sys.platform == "linux", "requires Linux /proc CPU counters")
    def test_cpu_times(self):
        busy, total = cpu_times([0])
        self.assertGreaterEqual(total, busy)

    def test_utilisation(self):
        self.assertEqual(utilisation((10, 100), (60, 200)), 0.5)
        self.assertEqual(utilisation((10, 100), (10, 100)), 0.0)


class SummariseTests(unittest.TestCase):
    def sample(self, framework, rps, **extra):
        return {
            "config": "4-core",
            "round": 0,
            "framework": framework,
            "route": "json",
            "rps": rps,
            "non_200": 0,
        } | extra

    def test_ratios_and_client_bound_flag(self):
        samples = [
            self.sample("bessemer", 100_000, client_cpu=0.5, server_cpu=0.99),
            self.sample("fastapi", 25_000, client_cpu=0.2, server_cpu=0.99),
            self.sample("axum", 200_000, client_cpu=0.9, client_bound=True),
        ]
        text = summarise.tables({}, samples)
        row = next(line for line in text.splitlines() if line.startswith("| `/json`"))
        self.assertIn("4.0x*", row)
        self.assertIn("50%*", row)
        self.assertIn("client-bound", text)

    def test_failed_responses_mark_the_ratio(self):
        samples = [
            self.sample("bessemer", 100_000, non_200=5),
            self.sample("fastapi", 25_000),
        ]
        text = summarise.tables({}, samples)
        row = next(line for line in text.splitlines() if line.startswith("| `/json`"))
        self.assertIn("100.0k (100.0k-100.0k)!", row)
        self.assertIn("4.0x*", row)
        self.assertIn("non-200", text)

    def test_reads_same_host_format(self):
        path = Path(__file__).resolve().parent / "results/2026-11-05"
        raw = path / "compare-fastapi-axum/raw.json"
        meta, samples = summarise.load(str(raw))
        self.assertEqual(meta, {})
        self.assertIn("## 1-core", summarise.tables(meta, samples))


class AgentTests(unittest.TestCase):
    TOKEN = "t" * 24

    @classmethod
    def setUpClass(cls):
        args = SimpleNamespace(python=sys.executable, axum="false", bind="127.0.0.1")
        cls.agent = server_agent.Agent(args)
        handler = server_agent.handler_for(cls.agent, cls.TOKEN)
        cls.http = HTTPServer(("127.0.0.1", 0), handler)
        cls.base = f"http://127.0.0.1:{cls.http.server_address[1]}"
        threading.Thread(target=cls.http.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.http.shutdown()
        cls.http.server_close()

    def call(self, method, path, body=None, token=TOKEN):
        data = None if body is None else json.dumps(body).encode()
        request = urllib.request.Request(self.base + path, data=data, method=method)
        request.add_header("authorization", f"Bearer {token}")
        try:
            with DIRECT.open(request, timeout=10) as response:
                return response.status, json.load(response)
        except urllib.error.HTTPError as error:
            return error.code, json.load(error)

    def test_rejects_a_wrong_token(self):
        self.assertEqual(self.call("GET", "/info", token="wrong")[0], 401)

    def test_info(self):
        status, info = self.call("GET", "/info")
        self.assertEqual(status, 200)
        self.assertIn("cpus", info)
        self.assertEqual(info["versions"]["python"], sys.version.split()[0])

    @unittest.skipUnless(sys.platform == "linux", "requires Linux /proc CPU counters")
    def test_cpu(self):
        status, cpu = self.call("GET", "/cpu")
        self.assertEqual(status, 200)
        self.assertGreaterEqual(cpu["total"], cpu["busy"])

    def test_refuses_unknown_framework_and_oversized_config(self):
        status, _ = self.call("POST", "/start", {"framework": "sh", "config": "1-core"})
        self.assertEqual(status, 400)
        status, reply = self.call(
            "POST", "/start", {"framework": "axum", "config": "4096-core"}
        )
        self.assertEqual(status, 400)
        self.assertIn("more CPUs", reply["error"])

    @unittest.skipUnless(shutil.which("taskset"), "requires taskset CPU affinity")
    def test_reports_a_server_that_fails_to_start(self):
        # "false" exits at once, so the agent must say so rather than hang.
        status, reply = self.call(
            "POST", "/start", {"framework": "axum", "config": "1-core"}
        )
        self.assertEqual(status, 400)
        self.assertIn("exited", reply["error"])


if __name__ == "__main__":
    unittest.main()
