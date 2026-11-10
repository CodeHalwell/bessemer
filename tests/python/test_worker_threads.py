"""Synchronous handlers share one runtime-aware thread budget per process."""

import asyncio
import contextvars
import queue
import sys
import threading
import time
import unittest
from unittest.mock import patch

from bessemer import App
from bessemer._threads import ContextThreadPoolExecutor, SyncPool
from bessemer.worker import Worker


class WorkerThreadTests(unittest.TestCase):
    def test_gil_runtime_uses_eight_base_threads_and_a_bounded_native_ceiling(self):
        with (
            patch.object(sys, "_is_gil_enabled", return_value=True, create=True),
            patch("bessemer.worker.cpu_budget", return_value=1) as budget,
        ):
            worker = Worker(App())
            try:
                adaptive = SyncPool is not None and SyncPool.adaptive_available()
                self.assertEqual(worker._executor._max_workers, 16 if adaptive else 8)
                if adaptive:
                    self.assertEqual(worker._executor._native_pool.adaptive_base, 8)
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
                    if SyncPool:
                        self.assertIsNone(worker._executor._native_pool.adaptive_base)
                finally:
                    worker.close()

    def test_explicit_gil_limit_stays_fixed(self):
        with patch.object(sys, "_is_gil_enabled", return_value=True, create=True):
            worker = Worker(App(), sync_workers=8)
            try:
                self.assertEqual(worker._executor._max_workers, 8)
                if SyncPool:
                    self.assertIsNone(worker._executor._native_pool.adaptive_base)
            finally:
                worker.close()

    def test_missing_process_cpu_clock_keeps_implicit_pool_fixed_at_eight(self):
        class UnavailablePool:
            @staticmethod
            def adaptive_available():
                return False

        with (
            patch.object(sys, "_is_gil_enabled", return_value=True, create=True),
            patch("bessemer.worker.SyncPool", UnavailablePool),
        ):
            worker = Worker(App())
            try:
                self.assertEqual(worker._executor._max_workers, 8)
                if SyncPool:
                    self.assertIsNone(worker._executor._native_pool.adaptive_base)
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
    async def test_repeated_shutdown_cancellation_waits_for_native_completion(self):
        from types import SimpleNamespace

        from bessemer.worker import _Lane

        registered = asyncio.Event()

        class Invocation:
            _sync_pending = True

            def cancel(self):
                return False

            def _wait_sync(self, waiter):
                self.waiter = waiter
                registered.set()

        invocation = Invocation()
        lane = _Lane(
            SimpleNamespace(_streams=(), _after_responses=()),
            asyncio.get_running_loop(),
            {},
            {1: invocation},
        )
        shutdown = asyncio.create_task(lane.finish())
        await registered.wait()
        shutdown.cancel("first")
        await asyncio.sleep(0)
        shutdown.cancel("again")
        await asyncio.sleep(0)
        self.assertFalse(shutdown.done())
        self.assertFalse(invocation.waiter.cancelled())
        invocation.waiter.set_result(None)
        with self.assertRaises(asyncio.CancelledError):
            await shutdown

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


class NativeSyncPoolTests(unittest.TestCase):
    def setUp(self):
        try:
            from bessemer._native import SyncPool  # noqa: F401
        except ImportError:
            self.skipTest("native extension is not built")

    def test_adaptive_policy_is_configured_before_work_only(self):
        from bessemer._native import SyncPool as NativeSyncPool

        if not NativeSyncPool.adaptive_available():
            self.skipTest("process CPU clock is unavailable")
        executor = ContextThreadPoolExecutor(max_workers=2)
        try:
            native = executor._native_pool
            self.assertIsNone(native.adaptive_base)
            with self.assertRaises(ValueError):
                native.enable_adaptive(2)
            native.enable_adaptive(1)
            self.assertEqual(native.adaptive_base, 1)
            executor.submit(int).result(2)
            with self.assertRaisesRegex(RuntimeError, "before use"):
                native.enable_adaptive(1)
        finally:
            executor.shutdown(wait=True)

    def test_native_thread_starts_with_empty_context(self):
        variable = contextvars.ContextVar("native_thread_context", default="empty")
        seen = queue.Queue()
        executor = ContextThreadPoolExecutor(max_workers=1)
        token = variable.set("caller")
        try:
            executor.execute(lambda: seen.put(variable.get()))
            self.assertEqual(seen.get(timeout=2), "empty")
        finally:
            variable.reset(token)
            executor.shutdown(wait=True)

    def test_adaptive_pool_grows_under_persistent_backlog_without_exceeding_cap(self):
        from bessemer._native import SyncPool as NativeSyncPool

        if not NativeSyncPool.adaptive_available():
            self.skipTest("process CPU clock is unavailable")
        executor = ContextThreadPoolExecutor(max_workers=2)
        native = executor._native_pool
        native.enable_adaptive(1)
        first, second, release = threading.Event(), threading.Event(), threading.Event()
        active = 0
        lock = threading.Lock()

        def hold():
            nonlocal active
            with lock:
                active += 1
                (first if active == 1 else second).set()
            release.wait(10)
            with lock:
                active -= 1

        try:
            executor.execute(hold)
            self.assertTrue(first.wait(2))
            for _ in range(4):
                executor.execute(hold)
            deadline = time.monotonic() + 5
            while not second.is_set() and time.monotonic() < deadline:
                executor.execute(hold)
                second.wait(0.012)
                self.assertLessEqual(native.threads_started, 2)
            self.assertTrue(second.is_set(), "sustained backlog did not grow the pool")
            self.assertEqual(native.threads_started, 2)
        finally:
            release.set()
            executor.shutdown(wait=True)

    def test_adaptive_pool_grows_after_a_finite_burst_without_more_submits(self):
        from bessemer._native import SyncPool as NativeSyncPool

        if not NativeSyncPool.adaptive_available():
            self.skipTest("process CPU clock is unavailable")
        pool = NativeSyncPool(16, "finite-burst")
        pool.enable_adaptive(8)
        release = threading.Event()
        ready = threading.Barrier(9)
        lock = threading.Lock()
        started = 0

        def hold():
            nonlocal started
            with lock:
                started += 1
                number = started
            if number <= 8:
                ready.wait(5)
            release.wait(3)

        try:
            for _ in range(8):
                pool.execute(hold)
            ready.wait(5)
            for _ in range(4):
                pool.execute(hold)
            deadline = time.monotonic() + 1.5
            while time.monotonic() < deadline:
                with lock:
                    count = started
                if count > 8:
                    break
                time.sleep(0.01)
            self.assertGreater(count, 8, "finite backlog never triggered reevaluation")
            self.assertLessEqual(pool.threads_started, 16)
        finally:
            release.set()
            pool.shutdown(wait=True)
        self.assertEqual(pool.threads_alive, 0)

    def test_adaptive_shutdown_cancels_queue_and_joins_growth_observer(self):
        from bessemer._native import SyncPool as NativeSyncPool

        if not NativeSyncPool.adaptive_available():
            self.skipTest("process CPU clock is unavailable")
        executor = ContextThreadPoolExecutor(max_workers=16)
        native = executor._native_pool
        native.enable_adaptive(8)
        started = threading.Barrier(9)
        release = threading.Event()
        queued_release = threading.Event()

        def hold():
            started.wait(5)
            release.wait(5)

        try:
            blockers = [executor.submit(hold) for _ in range(8)]
            started.wait(5)
            queued = [executor.submit(queued_release.wait, 5) for _ in range(32)]
            executor.shutdown(wait=False, cancel_futures=True)
            self.assertGreaterEqual(sum(future.cancelled() for future in queued), 24)
            release.set()
            queued_release.set()
            executor.shutdown(wait=True)
            self.assertTrue(all(future.done() for future in blockers))
            self.assertEqual(native.threads_alive, 0)
            self.assertLessEqual(native.threads_started, 16)
        finally:
            release.set()
            queued_release.set()
            executor.shutdown(wait=True)

    def test_threads_start_as_needed_and_take_the_name_prefix(self):
        executor = ContextThreadPoolExecutor(max_workers=4, thread_name_prefix="unit")
        try:
            native = executor._native_pool
            self.assertEqual(native.threads_started, 0)
            native_id = executor.submit(threading.get_native_id).result(5)
            if sys.platform == "linux":
                # Native threads are named for the OS, not for threading.
                with open(f"/proc/self/task/{native_id}/comm") as comm:
                    self.assertEqual(comm.read().strip(), "unit-0")
            # One thread that is waiting again takes the next call.
            executor.submit(int).result(5)
            self.assertEqual(native.threads_started, 1)
            release = threading.Event()
            started = threading.Barrier(4)

            def hold():
                started.wait(5)
                release.wait(5)

            futures = [executor.submit(hold) for _ in range(3)]
            started.wait(5)
            # The waiting thread took one; two more started for the others.
            self.assertEqual(native.threads_started, 3)
            release.set()
            for future in futures:
                future.result(5)
        finally:
            executor.shutdown(wait=True)

    def test_collected_executors_release_their_pools(self):
        import gc

        from bessemer import _threads

        for _ in range(3):
            executor = ContextThreadPoolExecutor(max_workers=1)
            executor.submit(int).result(5)
            pool = executor._native_pool
            del executor
            gc.collect()
            deadline = time.monotonic() + 5
            while pool.threads_alive and time.monotonic() < deadline:
                time.sleep(0.005)
        ContextThreadPoolExecutor(max_workers=1).shutdown(wait=True)
        with _threads._pools_lock:
            self.assertNotIn(pool, _threads._pools)
            self.assertNotIn(pool, _threads._finalized)

    def test_finalizer_does_not_take_the_registry_lock(self):
        # A collection can run an executor's finalizer while the registry lock
        # is held (for example during an allocation in a constructor). The
        # lock is not reentrant, so the finalizer must never acquire it.
        import gc

        from bessemer import _threads

        executor = ContextThreadPoolExecutor(max_workers=1)
        executor.submit(int).result(5)
        pool = executor._native_pool
        finished = threading.Event()

        def collect_under_lock():
            nonlocal executor
            with _threads._pools_lock:
                del executor
                gc.collect()
            finished.set()

        worker = threading.Thread(target=collect_under_lock, daemon=True)
        worker.start()
        self.assertTrue(finished.wait(5), "finalizer deadlocked on the registry lock")
        deadline = time.monotonic() + 5
        while pool.threads_alive and time.monotonic() < deadline:
            time.sleep(0.005)
        ContextThreadPoolExecutor(max_workers=1).shutdown(wait=True)
        with _threads._pools_lock:
            self.assertNotIn(pool, _threads._pools)
            self.assertNotIn(pool, _threads._finalized)
