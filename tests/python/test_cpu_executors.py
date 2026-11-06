"""Real threads/spawned processes keep cancelled work inside CPU capacity."""

import asyncio
import contextvars
import gc
import multiprocessing
import os
import pickle
import tempfile
import threading
import time
import unittest
import weakref
from pathlib import Path
from unittest.mock import patch

from bessemer import App, HTTPException
from bessemer._offload import CPUOffload
from bessemer.executors import ProcessPoolExecutor, ThreadPoolExecutor

REQUEST = contextvars.ContextVar("cpu_executor_request", default="unset")


def add(value, *, increment=1):
    return value + increment


def fail():
    raise ValueError("CPU job failed")


def fail_timeout():
    raise TimeoutError("user function timed out")


def identity():
    return os.getpid(), multiprocessing.get_start_method()


def change_context(value):
    previous = REQUEST.get()
    REQUEST.set(value)
    return previous


def touch(path):
    Path(path).touch()
    return "ran"


def gated(started, release):
    Path(started).touch()
    deadline = time.monotonic() + 15
    while not Path(release).exists():
        if time.monotonic() >= deadline:
            raise TimeoutError("test did not release the CPU job")
        time.sleep(0.002)
    return "released"


def gated_failure(started, release):
    gated(started, release)
    raise ValueError("work failed after cancellation")


async def marker(path):
    async with asyncio.timeout(10):
        while not Path(path).exists():
            await asyncio.sleep(0.002)


class PoolContract:
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.pool = self.executor_type(max_workers=1, max_pending=1)
        self.tasks, self.releases = [], []

    async def asyncTearDown(self):
        for release in self.releases:
            release.touch()
        for task in self.tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        await self.pool.shutdown()
        self.directory.cleanup()

    def task(self, awaitable):
        task = asyncio.create_task(awaitable)
        self.tasks.append(task)
        return task

    async def blocked(self, name="job", function=gated):
        started, release = (
            self.root / (name + ".started"),
            self.root / (name + ".release"),
        )
        self.releases.append(release)
        task = self.task(self.pool.submit(function, str(started), str(release)))
        await marker(started)
        return task, release

    async def full(self):
        with self.assertRaises(HTTPException) as raised:
            await self.pool.submit(add, 1)
        self.assertEqual(raised.exception.status_code, 503)
        self.assertEqual(raised.exception.headers["Retry-After"], "1")

    def loop_errors(self):
        loop = asyncio.get_running_loop()
        errors = []
        previous_handler = loop.get_exception_handler()
        loop.set_exception_handler(lambda loop, context: errors.append(context))
        self.addCleanup(loop.set_exception_handler, previous_handler)
        return errors

    def assert_no_loop_errors(self, errors):
        self.assertEqual(
            [error.get("message") for error in errors],
            [],
            "cancelled CPU work must not report an unhandled Future error",
        )

    async def test_results_exceptions_and_capacity_recovery(self):
        self.assertEqual(await self.pool.submit(add, 40, increment=2), 42)
        with self.assertRaisesRegex(ValueError, "CPU job failed"):
            await self.pool.submit(fail)
        self.assertEqual(await self.pool.submit(add, 2), 3)

    async def test_idle_pool_does_not_retain_the_first_request_context(self):
        class Payload:
            pass

        payload = Payload()
        reference = weakref.ref(payload)
        token = REQUEST.set(payload)
        try:
            self.assertEqual(await self.pool.submit(add, 1), 2)
        finally:
            REQUEST.reset(token)
        del payload, token
        # Drain Future hand-off callbacks before checking the still-open pool.
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        gc.collect()
        self.assertIsNone(reference())

    async def test_queued_cancellation_skips_user_code_and_holds_capacity_until_dequeue(
        self,
    ):
        errors = self.loop_errors()
        running, release = await self.blocked()
        unwanted = self.root / "cancelled-function-ran"
        queued = self.task(self.pool.submit(touch, str(unwanted)))
        await asyncio.sleep(0)
        queued.cancel()
        await asyncio.sleep(0)
        self.assertFalse(queued.done())
        await self.full()
        release.touch()
        self.assertEqual(await running, "released")
        with self.assertRaises(asyncio.CancelledError):
            await queued
        self.assertFalse(unwanted.exists())
        self.assertEqual(await self.pool.submit(add, 1), 2)
        await asyncio.sleep(0)
        self.assert_no_loop_errors(errors)

    async def test_cancellation_wins_over_a_later_worker_exception_without_logging(
        self,
    ):
        errors = self.loop_errors()
        running, release = await self.blocked("failure", gated_failure)
        running.cancel()
        await asyncio.sleep(0)
        release.touch()
        with self.assertRaises(asyncio.CancelledError):
            await running
        self.assertEqual(await self.pool.submit(add, 1), 2)
        await asyncio.sleep(0)
        self.assert_no_loop_errors(errors)

    async def test_running_and_repeated_cancellation_hold_capacity(self):
        running, release = await self.blocked()
        queued_marker = self.root / "next-function-ran"
        queued = self.task(self.pool.submit(touch, str(queued_marker)))
        await asyncio.sleep(0)
        running.cancel()
        await asyncio.sleep(0)
        running.cancel()
        await asyncio.sleep(0)
        self.assertFalse(running.done())
        await self.full()
        release.touch()
        with self.assertRaises(asyncio.CancelledError):
            await running
        self.assertEqual(await queued, "ran")
        self.assertTrue(queued_marker.exists())
        self.assertEqual(await self.pool.submit(add, 1), 2)

    async def test_shutdown_waits_through_repeated_cancellation_and_pool_can_be_reused(
        self,
    ):
        running, release = await self.blocked()
        shutdown = self.task(self.pool.shutdown())
        await asyncio.sleep(0)
        shutdown.cancel()
        await asyncio.sleep(0)
        shutdown.cancel()
        await asyncio.sleep(0)
        self.assertFalse(shutdown.done())
        with self.assertRaisesRegex(RuntimeError, "shut down"):
            await self.pool.submit(add, 1)
        release.touch()
        self.assertEqual(await running, "released")
        with self.assertRaises(asyncio.CancelledError):
            await shutdown
        self.pool.configure(2)
        self.assertEqual(await self.pool.submit(add, 1), 2)

    async def test_nonwaiting_shutdown_cannot_reconfigure_running_work(self):
        running, release = await self.blocked()
        await self.pool.shutdown(wait=False)
        self.assertFalse(running.done())
        with self.assertRaisesRegex(RuntimeError, "running work"):
            self.pool.configure(1)
        release.touch()
        self.assertEqual(await running, "released")
        self.pool.configure(1)
        self.assertEqual(await self.pool.submit(add, 1), 2)

    async def test_automatic_size_divides_cpu_budget_across_api_processes(self):
        await self.pool.shutdown()
        self.pool = self.executor_type(max_pending=0)
        self.pool.configure(3)
        with patch("bessemer.executors.cpu_budget", return_value=6):
            first, release_first = await self.blocked("first")
            second, release_second = await self.blocked("second")
        await self.full()
        release_first.touch()
        release_second.touch()
        self.assertEqual(await first, "released")
        self.assertEqual(await second, "released")


class ThreadExecutorTests(PoolContract, unittest.IsolatedAsyncioTestCase):
    executor_type = ThreadPoolExecutor

    async def test_request_context_is_copied_and_worker_mutation_is_isolated(self):
        token = REQUEST.set("request-one")
        try:
            self.assertEqual(
                await self.pool.submit(change_context, "worker-private"), "request-one"
            )
            self.assertEqual(REQUEST.get(), "request-one")
        finally:
            REQUEST.reset(token)
        self.assertEqual(
            await self.pool.submit(change_context, "another-private"), "unset"
        )

    async def test_pool_can_be_shared_by_independent_event_loops(self):
        await self.pool.shutdown()
        self.pool = ThreadPoolExecutor(max_workers=2, max_pending=4)

        def caller(index):
            async def run():
                token = REQUEST.set(f"caller-{index}")
                try:
                    return [
                        await self.pool.submit(change_context, "private")
                        for _ in range(4)
                    ]
                finally:
                    REQUEST.reset(token)

            return asyncio.run(run())

        results = await asyncio.gather(
            *(asyncio.to_thread(caller, index) for index in range(4))
        )
        for index, values in enumerate(results):
            self.assertEqual(values, [f"caller-{index}"] * 4)

    async def test_late_cancellation_does_not_cancel_a_new_job_in_the_reused_slot(self):
        first, release_first = await self.blocked("first")
        second_started, release_second = (
            self.root / "second.started",
            self.root / "second.release",
        )
        self.releases.append(release_second)
        second = self.task(
            self.pool.submit(gated, str(second_started), str(release_second))
        )
        await asyncio.sleep(0)
        ran = self.root / "replacement-ran"
        admitted, finished = threading.Event(), threading.Event()
        results = []

        def other_loop():
            async def submit():
                await marker(second_started)
                task = asyncio.create_task(self.pool.submit(touch, str(ran)))
                await asyncio.sleep(0)
                admitted.set()
                return await task

            try:
                results.append(asyncio.run(submit()))
            except BaseException as error:  # noqa: BLE001 -- Report background-loop failures to the test.
                results.append(error)
            finally:
                finished.set()

        thread = threading.Thread(target=other_loop, daemon=True)
        thread.start()
        try:
            # Keep the original waiter paused while its job finishes and the
            # second event loop reuses that capacity behind the blocked job.
            release_first.touch()
            self.assertTrue(admitted.wait(2))
            first.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await first
            release_second.touch()
            self.assertEqual(await second, "released")
            self.assertTrue(await asyncio.to_thread(finished.wait, 2))
            self.assertEqual(results, ["ran"])
            self.assertTrue(ran.exists())
        finally:
            release_first.touch()
            release_second.touch()
            await asyncio.to_thread(thread.join, 2)

    async def test_async_functions_and_awaitable_results_are_rejected(self):
        async def asynchronous():
            return 1

        with self.assertRaisesRegex(TypeError, "synchronous"):
            await self.pool.submit(asynchronous)
        with self.assertRaisesRegex(TypeError, "synchronous"):
            await self.pool.submit(lambda: asynchronous())
        self.assertEqual(await self.pool.submit(add, 1), 2)


class ProcessExecutorTests(PoolContract, unittest.IsolatedAsyncioTestCase):
    executor_type = ProcessPoolExecutor

    async def test_processes_use_spawn_and_pickle_top_level_functions(self):
        pid, method = await self.pool.submit(identity)
        self.assertNotEqual(pid, os.getpid())
        self.assertEqual(method, "spawn")

    async def test_unpicklable_function_fails_without_losing_capacity(self):
        with self.assertRaises((AttributeError, TypeError, pickle.PicklingError)):
            await self.pool.submit(lambda: 1)
        self.assertEqual(await self.pool.submit(add, 1), 2)

    async def test_process_context_starts_clean_for_each_job(self):
        token = REQUEST.set("parent-private")
        try:
            self.assertEqual(
                await self.pool.submit(change_context, "child-private"), "unset"
            )
            self.assertEqual(
                await self.pool.submit(change_context, "next-private"), "unset"
            )
            self.assertEqual(REQUEST.get(), "parent-private")
        finally:
            REQUEST.reset(token)


class ExecutorValidationTests(unittest.TestCase):
    def test_worker_and_pending_sizes_are_bounded_integers(self):
        for executor in (ThreadPoolExecutor, ProcessPoolExecutor):
            for size in (0, -1, True, 1.5, "1", 257):
                with (
                    self.subTest(executor=executor.__name__, workers=size),
                    self.assertRaises(ValueError),
                ):
                    executor(max_workers=size)
            for size in (-1, True, 1.5, "1", 65537):
                with (
                    self.subTest(executor=executor.__name__, pending=size),
                    self.assertRaises(ValueError),
                ):
                    executor(max_pending=size)
            for size in (0, -1, True, 1.5):
                with (
                    self.subTest(executor=executor.__name__, processes=size),
                    self.assertRaises(ValueError),
                ):
                    executor().configure(size)


class FalseyExecutor:
    def __init__(self):
        self.closed = False

    def __bool__(self):
        return False

    async def submit(self, function, /, *args, **kwargs):
        return function(*args, **kwargs)

    async def shutdown(self, *, wait=True):
        self.closed = True


class DelayedCancellationExecutor(FalseyExecutor):
    def __init__(self):
        super().__init__()
        self.cancelled, self.release = asyncio.Event(), asyncio.Event()

    async def submit(self, function, /, *args, **kwargs):
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled.set()
            await self.release.wait()
            raise


class SuppressedCancellationExecutor(DelayedCancellationExecutor):
    async def submit(self, function, /, *args, **kwargs):
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled.set()
            await self.release.wait()
            return function(*args, **kwargs)


class CPUOffloadTests(unittest.IsolatedAsyncioTestCase):
    async def test_unmanaged_app_call_does_not_create_an_executor(self):
        app = App()
        with patch("bessemer._offload.automatic_executor") as factory:
            with self.assertRaisesRegex(RuntimeError, "running application worker"):
                await app.run_cpu(add, 1)
            factory.assert_not_called()

    async def test_falsey_custom_and_shared_executors_are_retained(self):
        custom, shared = FalseyExecutor(), FalseyExecutor()
        offload = CPUOffload(custom)
        self.assertIs(offload.configure(1, default=shared), shared)
        self.assertEqual(await offload.submit(add, (1,), {}, None), 2)
        await offload.shutdown()
        self.assertTrue(custom.closed)
        self.assertFalse(shared.closed)
        offload = CPUOffload(None)
        self.assertIs(offload.configure(1, default=shared), shared)
        self.assertEqual(await offload.submit(add, (2,), {}, None), 3)
        await offload.shutdown()
        self.assertTrue(shared.closed)

    async def test_user_timeout_error_is_not_mapped_to_http_timeout(self):
        pool = ThreadPoolExecutor(max_workers=1)
        offload = CPUOffload(pool)
        offload.configure(1)
        try:
            with self.assertRaisesRegex(TimeoutError, "user function timed out"):
                await offload.submit(fail_timeout, (), {}, 1)
            self.assertEqual(await offload.submit(add, (1,), {}, 1), 2)
        finally:
            await offload.shutdown()

    async def test_deadline_maps_to_504_after_executor_cancellation_cleanup(self):
        executor = DelayedCancellationExecutor()
        offload = CPUOffload(executor)
        offload.configure(1)
        task = asyncio.create_task(offload.submit(add, (1,), {}, 0.001))
        try:
            await asyncio.wait_for(executor.cancelled.wait(), 2)
            self.assertFalse(task.done())
            executor.release.set()
            with self.assertRaises(HTTPException) as raised:
                await task
            self.assertEqual(raised.exception.status_code, 504)
        finally:
            executor.release.set()
            await asyncio.gather(task, return_exceptions=True)
            await offload.shutdown()

    async def test_invalid_timeouts_and_shutdown_options_fail_before_work(self):
        pool = ThreadPoolExecutor(max_workers=1)
        offload = CPUOffload(pool)
        offload.configure(1)
        try:
            for timeout in (0, -1, True, float("inf"), float("nan"), "1"):
                with self.subTest(timeout=timeout), self.assertRaises(ValueError):
                    await offload.submit(add, (1,), {}, timeout)
            with self.assertRaises(TypeError):
                await pool.shutdown(wait=1)
            self.assertEqual(await offload.submit(add, (1,), {}, None), 2)
        finally:
            await offload.shutdown()

    async def test_custom_executor_cannot_return_success_after_suppressing_timeout(
        self,
    ):
        executor = SuppressedCancellationExecutor()
        offload = CPUOffload(executor)
        offload.configure(1)
        task = asyncio.create_task(offload.submit(add, (1,), {}, 0.001))
        try:
            await asyncio.wait_for(executor.cancelled.wait(), 2)
            self.assertFalse(task.done())
            executor.release.set()
            with self.assertRaises(HTTPException) as raised:
                await task
            self.assertEqual(raised.exception.status_code, 504)
        finally:
            executor.release.set()
            await asyncio.gather(task, return_exceptions=True)
            await offload.shutdown()


if __name__ == "__main__":
    unittest.main()
