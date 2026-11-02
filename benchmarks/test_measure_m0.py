import json
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from unittest.mock import patch

from measure_m0 import Comparison, LoadGenerator, Samples, main


def result(rate=100.0, success=1.0, statuses=None, errors=None):
    return json.dumps(
        {
            "summary": {"requestsPerSec": rate, "successRate": success},
            "statusCodeDistribution": {"200": 10} if statuses is None else statuses,
            "errorDistribution": {} if errors is None else errors,
        }
    )


class LoadGeneratorTests(unittest.TestCase):
    def test_reads_successful_rate(self):
        self.assertEqual(LoadGenerator.rate(result()), 100.0)

    def test_rejects_failed_requests(self):
        with self.assertRaises(ValueError):
            LoadGenerator.rate(result(statuses={"500": 1}))
        with self.assertRaises(ValueError):
            LoadGenerator.rate(result(errors={"connection refused": 1}))
        with self.assertRaises(ValueError):
            LoadGenerator.rate(result(success=0.9))

    def test_rejects_invalid_output(self):
        for output in ("not json", "{}"):
            with self.subTest(output=output), self.assertRaises(ValueError):
                LoadGenerator.rate(output)
        with self.assertRaises(TypeError):
            LoadGenerator.rate(result(statuses=[]))

    def test_rejects_invalid_rate(self):
        for rate in (0, -1, float("inf")):
            with self.subTest(rate=rate), self.assertRaises(ValueError):
                LoadGenerator.rate(result(rate=rate))
        with self.assertRaises(TypeError):
            LoadGenerator.rate(result(rate=True))


class ComparisonTests(unittest.TestCase):
    def test_stop_threshold_is_strictly_below_40_percent(self):
        rust = Samples("rust", (100.0,))
        self.assertTrue(Comparison(rust, Samples("python", (39.9,))).stop)
        self.assertFalse(Comparison(rust, Samples("python", (40.0,))).stop)

    def test_spread_at_3_percent_is_allowed(self):
        Samples("rust", (98.5, 100.0, 101.5)).validate()

    def test_spread_above_3_percent_is_rejected(self):
        with self.assertRaises(ValueError):
            Samples("python", (98.4, 100.0, 101.6)).validate()


class MainTests(unittest.TestCase):
    def run_comparison(self, comparison):
        with (
            patch("measure_m0.arguments"),
            patch("measure_m0.collect", return_value=comparison),
            redirect_stdout(StringIO()),
            redirect_stderr(StringIO()),
        ):
            return main()

    def test_stop_exit_status(self):
        rust = Samples("rust", (100.0,))
        self.assertEqual(
            self.run_comparison(Comparison(rust, Samples("python", (39.0,)))), 1
        )
        self.assertEqual(
            self.run_comparison(Comparison(rust, Samples("python", (40.0,)))), 0
        )

    def test_invalid_spread_exit_status(self):
        rust = Samples("rust", (98.0, 100.0, 102.0))
        self.assertEqual(
            self.run_comparison(Comparison(rust, Samples("python", (50.0,)))), 2
        )


if __name__ == "__main__":
    unittest.main()
