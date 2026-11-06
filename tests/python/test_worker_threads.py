"""Synchronous handlers share one runtime-aware thread budget per process."""

import sys
import unittest
from unittest.mock import patch

from bessemer import App
from bessemer.worker import Worker


class WorkerThreadTests(unittest.TestCase):
    def test_gil_runtime_preserves_eight_io_workers(self):
        with (
            patch.object(sys, "_is_gil_enabled", return_value=True, create=True),
            patch("bessemer.worker.cpu_budget", return_value=1) as budget,
        ):
            worker = Worker(App())
            try:
                self.assertEqual(worker._executor._max_workers, 8)
                budget.assert_not_called()
            finally:
                worker.close()

    def test_free_threaded_runtime_shares_cpu_entitlement_across_processes(self):
        for budget, processes, expected in ((10, 3, 3), (2, 8, 1), (1024, 1, 256)):
            with (
                self.subTest(budget=budget, processes=processes),
                patch.object(sys, "_is_gil_enabled", return_value=False, create=True),
                patch("bessemer.worker.cpu_budget", return_value=budget),
            ):
                app = App()
                app._worker_processes = processes
                worker = Worker(app, loops=4)
                try:
                    self.assertEqual(worker._executor._max_workers, expected)
                finally:
                    worker.close()

    def test_explicit_worker_limit_overrides_automatic_sizing(self):
        for limit in (1, 8, 256):
            with (
                self.subTest(limit=limit),
                patch.object(sys, "_is_gil_enabled", return_value=False, create=True),
                patch("bessemer.worker.cpu_budget", return_value=1) as budget,
            ):
                worker = Worker(App(), sync_workers=limit)
                try:
                    self.assertEqual(worker._executor._max_workers, limit)
                    budget.assert_not_called()
                finally:
                    worker.close()

    def test_invalid_explicit_worker_limits_are_rejected(self):
        for limit in (0, -1, 257, 1.5, True, "auto"):
            with (
                self.subTest(limit=limit),
                self.assertRaisesRegex(ValueError, "sync_workers"),
            ):
                Worker(App(), sync_workers=limit)


if __name__ == "__main__":
    unittest.main()
