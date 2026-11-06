import json
import unittest
from pathlib import Path

from metrics import corrected_result, response_rates

ROOT = Path(__file__).resolve().parent.parent
SAMPLE = ROOT / "raw/matrix/branch-rust-iouring/items/c1024/run1"


class ResponseMetricsTests(unittest.TestCase):
    def test_archived_run_uses_completed_responses(self):
        oha = json.loads((SAMPLE / "oha.json").read_text())
        legacy = json.loads((SAMPLE / "result.json").read_text())

        rates = response_rates(oha)
        self.assertEqual(rates["no_response"], 4828)
        self.assertAlmostEqual(rates["req_s"], 666018 / 10.026232196)
        self.assertAlmostEqual(rates["ok_req_s"], 452429 / 10.026232196)

        corrected = corrected_result(legacy, oha)
        self.assertAlmostEqual(
            corrected["us_cpu_per_req"], corrected["server_cpus"] * 1e6 / rates["req_s"]
        )
        self.assertGreater(corrected["us_cpu_per_req"], legacy["us_cpu_per_req"])
        self.assertAlmostEqual(
            corrected_result(corrected, oha)["us_cpu_per_req"],
            corrected["us_cpu_per_req"],
        )

    def test_zero_completed_responses_cannot_produce_cpu_metric(self):
        oha = {
            "summary": {"total": 10, "requestsPerSec": 1},
            "statusCodeDistribution": {},
            "errorDistribution": {"timeout": 10},
        }
        with self.assertRaisesRegex(ValueError, "completed responses"):
            response_rates(oha)


if __name__ == "__main__":
    unittest.main()
