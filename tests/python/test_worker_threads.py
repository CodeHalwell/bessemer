"""Synchronous handlers share one runtime-aware thread budget per process."""

import asyncio
import sys
import threading
import unittest
from unittest.mock import patch

from bessemer import App
from bessemer._threads import ContextThreadPoolExecutor
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


class NativeQueueProtocolTests(unittest.TestCase):
    def test_generic_executor_calls_share_the_native_budget_and_isolate_context(self):
        import contextvars

        variable = contextvars.ContextVar("native_pool_context", default="empty")
        started, release, called = (
            threading.Event(),
            threading.Event(),
            threading.Event(),
        )
        executor = ContextThreadPoolExecutor(max_workers=1)

        def blocking():
            started.set()
            release.wait(5)

        try:
            executor.execute(blocking)
            self.assertTrue(started.wait(2))
            queued = executor.submit(called.set)
            self.assertTrue(queued.cancel())
            self.assertFalse(called.is_set())
            release.set()
            executor.submit(variable.set, "changed").result(2)
            self.assertEqual(executor.submit(variable.get).result(2), "empty")
            self.assertFalse(called.is_set())
        finally:
            release.set()
            executor.shutdown()

    def test_worker_cannot_join_itself_and_outer_shutdown_still_joins(self):
        executor = ContextThreadPoolExecutor(max_workers=1)
        try:
            with self.assertRaisesRegex(RuntimeError, "cannot join current thread"):
                executor.submit(executor.shutdown).result(2)
        finally:
            executor.shutdown()

    def test_shutdown_cancels_queued_native_items_and_rejects_new_work(self):
        started, release, cancelled = (
            threading.Event(),
            threading.Event(),
            threading.Event(),
        )

        class Item:
            def __init__(self, block):
                self.future = self
                self.block = block

            def run(self, *context):
                if self.block:
                    started.set()
                    release.wait(5)
                else:
                    raise AssertionError("queued work must have been cancelled")

            def cancel(self):
                cancelled.set()
                return True

        executor = ContextThreadPoolExecutor(max_workers=1)
        try:
            executor.execute(Item(True))
            self.assertTrue(started.wait(2))
            executor.execute(Item(False))
            executor.shutdown(wait=False, cancel_futures=True)
            self.assertTrue(cancelled.is_set())
            with self.assertRaisesRegex(RuntimeError, "after shutdown"):
                executor.execute(Item(False))
        finally:
            release.set()
            executor.shutdown(wait=True)


class ClosedLoopCompletionTests(unittest.TestCase):
    def test_pool_completion_tolerates_closed_loop_and_close_race(self):
        for race in (False, True):
            with self.subTest(race=race):
                self._check_closed_loop(race)

    def _check_closed_loop(self, race):
        loop = asyncio.new_event_loop()
        worker = Worker(App(), sync_workers=1)
        started, release = threading.Event(), threading.Event()
        submitted = []
        execute = worker._executor.execute

        def track_execute(function):
            finished = threading.Event()
            submitted.append(finished)

            def run():
                try:
                    function()
                finally:
                    finished.set()

            return execute(run)

        def blocking():
            started.set()
            release.wait(5)
            return 42

        task = loop.create_task(worker._call_sync(blocking, (), {}, None))
        try:
            with patch.object(worker._executor, "execute", track_execute):
                loop.run_until_complete(asyncio.sleep(0))
            self.assertTrue(started.wait(2))
            loop.close()
            # Exercise both an already closed destination and closure
            # between is_closed() and call_soon_threadsafe().
            checks = [False, True] if race else [True]
            with patch.object(loop, "is_closed", side_effect=checks):
                release.set()
                self.assertTrue(submitted[0].wait(2))
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
