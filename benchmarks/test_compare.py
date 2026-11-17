"""Checks for the head-to-head harness that need no servers or load generator."""

from __future__ import annotations

import contextlib
import io
import json
import shutil
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import HTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent / "compare"))

import run_compare
import server_agent
import summarise
from measurement import eligibility_reasons, oha_sample
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
        self.assertIsNone(utilisation((10, 100), (10, 100)))
        self.assertIsNone(utilisation((10, 100), (9, 200)))
        self.assertIsNone(utilisation((10, 100), (200, 200)))


def raw_result():
    return {
        "summary": {"requestsPerSec": 100, "total": 10},
        "statusCodeDistribution": {"200": 800},
        "errorDistribution": {},
        "latencyPercentiles": {"p50": 0.001, "p99": 0.002},
    }


def healthy_sample(framework="bessemer", rate=100_000, **extra):
    raw = raw_result()
    raw["statusCodeDistribution"]["200"] = rate * 10
    observed = oha_sample(raw)
    return (
        observed
        | {
            "config": "4-core",
            "round": 0,
            "framework": framework,
            "route": "json",
            "ok_rps": rate,
            "client_cpu": 0.2,
            "server_cpu": 0.99,
            "client_bound": False,
            "warmup": observed.copy(),
        }
        | extra
    )


class SummariseTests(unittest.TestCase):
    def sample(self, framework, rps, **extra):
        return healthy_sample(framework, rps, **extra)

    def test_ratios_and_client_bound_flag(self):
        samples = [
            self.sample("bessemer", 100_000, client_cpu=0.5, server_cpu=0.99),
            self.sample("fastapi", 25_000, client_cpu=0.2, server_cpu=0.99),
            self.sample("axum", 200_000, client_cpu=0.9, client_bound=True),
        ]
        text = summarise.tables({}, samples)
        row = next(line for line in text.splitlines() if line.startswith("| `/json`"))
        self.assertIn("4.0x", row)
        self.assertIn("withheld", row)
        self.assertNotIn("50%*", row)
        self.assertIn("client-bound", text)

    def test_failed_responses_mark_the_ratio(self):
        samples = [
            self.sample("bessemer", 100_000, non_200=5),
            self.sample("fastapi", 25_000),
        ]
        text = summarise.tables({}, samples)
        row = next(line for line in text.splitlines() if line.startswith("| `/json`"))
        self.assertIn("100.0k (100.0k-100.0k)†", row)
        self.assertIn("withheld", row)
        self.assertNotIn("4.0x", row)
        self.assertIn("non-200", text)

    def test_reads_same_host_format(self):
        path = Path(__file__).resolve().parent / "results/2026-11-05"
        raw = path / "compare-fastapi-axum/raw.json"
        meta, samples = summarise.load(str(raw))
        self.assertEqual(meta, {})
        self.assertIn("## 1-core", summarise.tables(meta, samples))

    def test_transport_errors_and_failed_warmup_withhold_comparisons(self):
        for extra in (
            {"errors": 2, "error_distribution": {"timeout": 2}},
            {
                "warmup": oha_sample(
                    raw_result() | {"errorDistribution": {"connect": 1}}
                )
            },
            {"client_bound": True},
        ):
            with self.subTest(extra=extra):
                base = [healthy_sample("fastapi", 25_000)]
                head = [healthy_sample(**extra)]
                normal = summarise.tables({}, base + head)
                paired = summarise.comparison({}, base, {}, head)
                self.assertIn("withheld", normal)
                self.assertIn("withheld", paired)
                self.assertNotIn("4.0x", normal)
                self.assertNotIn("+300.0%", paired)

    def test_missing_measurements_are_not_zero_failures(self):
        for field in ("errors", "status_codes", "warmup", "client_cpu", "client_bound"):
            with self.subTest(field=field):
                sample = healthy_sample()
                del sample[field]
                self.assertTrue(eligibility_reasons(sample))
                text = summarise.comparison({}, [healthy_sample()], {}, [sample])
                self.assertIn("withheld", text)
                self.assertNotIn("+0.0%", text)

    def test_missing_pair_and_unmatched_rounds_are_withheld(self):
        cases = (
            ({}, [], {}, [healthy_sample()]),
            ({}, [healthy_sample()], {}, [healthy_sample(round=1)]),
            ({"settings": {"rounds": 2}}, [healthy_sample()], {}, [healthy_sample()]),
            ({}, [healthy_sample()] * 2, {}, [healthy_sample()]),
        )
        for args in cases:
            with self.subTest(args=args):
                text = summarise.comparison(*args)
                self.assertIn("withheld", text)
                self.assertNotIn("+0.0%", text)

    def test_paired_round_ratios_are_used(self):
        base = [
            healthy_sample(rate=value, round=i)
            for i, value in enumerate((10, 100, 100))
        ]
        head = [
            healthy_sample(rate=value, round=i)
            for i, value in enumerate((20, 100, 500))
        ]
        text = summarise.comparison({}, base, {}, list(reversed(head)))
        self.assertIn("+100.0%", text)
        self.assertNotIn("withheld |", text)

    def test_invalid_round_types_withhold_without_hashing(self):
        meta = {"settings": {"rounds": 1}}
        for value in ([], {}, None, True, -1):
            with self.subTest(value=value):
                text = summarise.comparison(
                    meta, [healthy_sample()], meta, [healthy_sample(round=value)]
                )
                self.assertIn("withheld", text)
                self.assertIn("round not recorded", text)
                self.assertNotIn("+0.0%", text)

    def test_missing_result_file_reports_unavailable(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            contextlib.redirect_stdout(io.StringIO()) as output,
        ):
            summarise.main(
                ["--base", directory + "/base.json", "--head", directory + "/head.json"]
            )
        self.assertIn("results unavailable (FileNotFoundError)", output.getvalue())
        self.assertNotIn("+0.0%", output.getvalue())

    def test_malformed_result_file_is_unavailable(self):
        for value in (
            {"samples": None},
            {"samples": [{}]},
            {"meta": None, "samples": []},
        ):
            with self.subTest(value=value), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "results.json"
                path.write_text(json.dumps(value))
                meta, samples = summarise.optional_load(path)
                self.assertIn("unavailable", meta["load_error"])
                self.assertEqual(samples, [])


class MeasurementTests(unittest.TestCase):
    def test_successful_rate_excludes_fast_errors(self):
        raw = raw_result()
        raw["statusCodeDistribution"]["503"] = 100
        raw["errorDistribution"] = {"aborted due to deadline": 100}
        sample = oha_sample(raw)
        self.assertEqual(sample["rps"], 100)
        self.assertEqual(sample["ok_rps"], 80)
        self.assertEqual(sample["non_200"], 100)
        self.assertEqual(sample["errors"], 100)
        self.assertEqual(sample["error_distribution"], {"aborted due to deadline": 100})

    def test_absent_distributions_and_latency_stay_missing(self):
        sample = oha_sample({"summary": {"total": 1}})
        for field in ("errors", "non_200", "ok_rps", "p50_ms", "p99_ms"):
            self.assertIsNone(sample[field], field)
        self.assertTrue(eligibility_reasons(sample))

    def test_inconsistent_counters_and_recorded_failures_are_not_qualified(self):
        for extra in (
            {"error_distribution": {"timeout": 1}},
            {"status_codes": {"200": 1}},
            {"eligible": False},
            {"eligibility_reasons": ["response body mismatch"]},
        ):
            with self.subTest(extra=extra):
                self.assertTrue(eligibility_reasons(healthy_sample(**extra)))

    def test_invalid_duration_cannot_report_successful_rate(self):
        for elapsed in (0, -1, float("nan"), float("inf"), None):
            raw = raw_result()
            raw["summary"]["total"] = elapsed
            sample = oha_sample(raw)
            self.assertIsNone(sample["ok_rps"])

    def test_rate_must_match_count_and_elapsed_time(self):
        base = healthy_sample()
        inconsistent = base | {"ok_rps": base["ok_rps"] * 2}
        self.assertIn(
            "inconsistent successful throughput", eligibility_reasons(inconsistent)
        )
        text = summarise.comparison({}, [base], {}, [inconsistent])
        self.assertIn("withheld", text)
        self.assertNotIn("+100.0%", text)
        # A floating-point round trip may change the last representable digits.
        rounded = base | {"ok_rps": base["ok_rps"] * (1 + 1e-14)}
        self.assertEqual(eligibility_reasons(rounded), [])

    def test_first_failure_replaces_previous_results(self):
        for phase in (
            "startup",
            "warmup",
            "host_info",
            "remote_info",
            "remote_constructor",
            "resolve",
        ):
            with self.subTest(phase=phase), tempfile.TemporaryDirectory() as directory:
                output = Path(directory) / "results.json"
                output.write_text(
                    json.dumps({"meta": {"old": True}, "samples": [healthy_sample()]})
                )
                server = MagicMock()
                server.info.return_value = {}
                server.start.return_value = ("127.0.0.1", 8000)
                if phase == "startup":
                    server.start.side_effect = RuntimeError("injected failure")
                if phase == "remote_info":
                    server.info.side_effect = RuntimeError("injected failure")
                argv = [
                    "run_compare.py",
                    "--python",
                    "python",
                    "--axum",
                    "false",
                    "--oha",
                    "oha",
                    "--configs",
                    "1-core",
                    "--frameworks",
                    "bessemer",
                    "--routes",
                    "json",
                    "--out",
                    str(output),
                ]
                if phase.startswith("remote_"):
                    argv += ["--agent", "http://agent.invalid"]
                with (
                    patch.object(sys, "argv", argv),
                    patch.object(run_compare, "LocalServers", return_value=server),
                    patch.object(run_compare, "host_info", return_value={}) as info,
                    patch.object(
                        run_compare, "RemoteServers", return_value=server
                    ) as remote,
                    patch.object(
                        run_compare,
                        "resolve_executable",
                        side_effect=lambda value: value,
                    ) as resolve,
                    patch.object(run_compare, "oha_version", return_value="oha 1.16.0"),
                    patch.object(
                        run_compare, "oha", side_effect=RuntimeError("injected failure")
                    ),
                    contextlib.redirect_stdout(io.StringIO()),
                    self.assertRaisesRegex(RuntimeError, "injected failure"),
                ):
                    if phase == "host_info":
                        info.side_effect = RuntimeError("injected failure")
                    if phase == "remote_constructor":
                        remote.side_effect = RuntimeError("injected failure")
                    if phase == "resolve":
                        resolve.side_effect = RuntimeError("injected failure")
                    run_compare.main()
                current = json.loads(output.read_text())
                self.assertEqual(current["samples"], [])
                self.assertNotIn("old", current["meta"])
                self.assertEqual(current["meta"]["settings"]["frameworks"], "bessemer")
                if phase in (
                    "host_info",
                    "remote_info",
                    "remote_constructor",
                    "resolve",
                ):
                    self.assertNotIn("client", current["meta"])
                    self.assertNotIn("server", current["meta"])

    def test_oha_drains_and_bounds_requests(self):
        args = SimpleNamespace(oha="oha", connections=64)
        with patch.object(
            run_compare.subprocess,
            "run",
            return_value=SimpleNamespace(stdout=json.dumps(raw_result())),
        ) as run:
            sample = run_compare.oha(args, "127.0.0.1", 8000, "json", 5, "3")
        command = run.call_args.args[0]
        self.assertIn("--wait-ongoing-requests-after-deadline", command)
        self.assertIn("--connect-timeout", command)
        self.assertIn("2s", command)
        self.assertEqual(run.call_args.kwargs["timeout"], 20)
        self.assertEqual(sample["command"], command)

    def run_main(self, *, warmup=1, cpu_after=(20, 200), warmup_failure=False):
        server = MagicMock()
        server.start.return_value = ("127.0.0.1", 8000)
        server.info.return_value = {}
        server.cpu.side_effect = [(10, 100), (30, 200)]
        measured = oha_sample(raw_result())
        warmed = (
            oha_sample(raw_result() | {"errorDistribution": {"connect": 1}})
            if warmup_failure
            else measured.copy()
        )
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "results.json"
            argv = [
                "run_compare.py",
                "--python",
                "python",
                "--axum",
                "false",
                "--oha",
                "oha",
                "--configs",
                "1-core",
                "--rounds",
                "1",
                "--routes",
                "json",
                "--frameworks",
                "bessemer",
                "--out",
                str(output),
                "--warmup",
                str(warmup),
            ]
            with (
                patch.object(sys, "argv", argv),
                patch.object(run_compare, "LocalServers", return_value=server),
                patch.object(
                    run_compare, "cpu_times", side_effect=[(10, 100), cpu_after]
                ),
                patch.object(run_compare, "host_info", return_value={}),
                patch.object(run_compare, "oha_version", return_value="oha 1.16.0"),
                patch.object(
                    run_compare,
                    "oha",
                    side_effect=[warmed, measured] if warmup else [measured],
                ) as load,
                contextlib.redirect_stdout(io.StringIO()),
            ):
                run_compare.main()
            sample = json.loads(output.read_text())["samples"][0]
        server.stop.assert_called_once()
        return sample, load.call_count

    def test_main_retains_failed_warmup_and_withholds(self):
        sample, calls = self.run_main(warmup_failure=True)
        self.assertEqual(calls, 2)
        self.assertEqual(sample["warmup"]["error_distribution"], {"connect": 1})
        self.assertFalse(sample["eligible"])
        self.assertIn("warmup transport errors: 1", sample["eligibility_reasons"])

    def test_disabled_warmup_is_explicit(self):
        sample, calls = self.run_main(warmup=0)
        self.assertEqual(calls, 1)
        self.assertEqual(sample["warmup"], {"skipped": True})
        self.assertTrue(sample["eligible"])

    def test_unavailable_cpu_is_not_zero_or_qualified(self):
        sample, _ = self.run_main(cpu_after=(10, 100))
        self.assertIsNone(sample["client_cpu"])
        self.assertIsNone(sample["client_bound"])
        self.assertFalse(sample["eligible"])


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
