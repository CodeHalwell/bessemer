"""Synchronous handlers share one runtime-aware thread budget per process."""

import asyncio
import sys
import threading
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


class ClosedLoopCompletionTests(unittest.TestCase):
    def test_pool_completion_tolerates_closed_loop_and_close_race(self):
        for race in (False, True):
            with self.subTest(race=race):
                loop = asyncio.new_event_loop()
                worker = Worker(App(), sync_workers=1)
                started, release = threading.Event(), threading.Event()
                submitted = []
                submit = worker._executor.submit

                def track_submit(function):
                    result = submit(function)
                    submitted.append(result)
                    return result

                def blocking():
                    started.set()
                    release.wait(5)
                    return 42

                task = loop.create_task(worker._call_sync(blocking, (), {}, None))
                try:
                    with patch.object(worker._executor, "submit", track_submit):
                        loop.run_until_complete(asyncio.sleep(0))
                    self.assertTrue(started.wait(2))
                    loop.close()
                    # Exercise both an already closed destination and closure
                    # between is_closed() and call_soon_threadsafe().
                    checks = [False, True] if race else [True]
                    with patch.object(loop, "is_closed", side_effect=checks):
                        release.set()
                        self.assertIsNone(submitted[0].result(2))
                finally:
                    release.set()
                    worker.close()
                    task.get_coro().close()
                    task._log_destroy_pending = False
                    if not loop.is_closed():
                        loop.close()


class SyncCompletionTests(unittest.IsolatedAsyncioTestCase):
    async def test_repeated_task_cancellation_waits_and_registers_acquired_resource(
        self,
    ):
        started = threading.Event()
        release = threading.Event()
        resource = object()
        acquired = []
        worker = Worker(App(), sync_workers=1)

        def acquire():
            started.set()
            release.wait(5)
            return resource

        task = asyncio.create_task(
            worker._call_sync(acquire, (), {}, None, on_result=acquired.append)
        )
        try:
            self.assertTrue(await asyncio.to_thread(started.wait, 2))
            task.cancel("first")
            await asyncio.sleep(0)
            task.cancel("second")
            await asyncio.sleep(0)
            self.assertFalse(task.done())
            self.assertEqual(acquired, [])
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertEqual(acquired, [resource])
        finally:
            release.set()
            await asyncio.gather(task, return_exceptions=True)
            worker.close()


if __name__ == "__main__":
    unittest.main()
