"""Idle worker threads release request contexts on GIL and free-threaded Python."""

import asyncio
import contextvars
import gc
import time
import unittest
import weakref

from bessemer import App, Depends, Request
from bessemer.request import current_request
from bessemer.worker import Worker
from envelopes import payload


class ExecutorContextTests(unittest.TestCase):
    def test_sync_handler_threads_release_the_first_request_context(self):
        self._check_worker("sync")

    def test_to_thread_keeps_per_call_context_without_retaining_the_first(self):
        self._check_worker("to_thread")

    def test_default_executor_does_not_inherit_a_request_context(self):
        self._check_worker("run_in_executor")

    def _check_worker(self, mode):
        marker = contextvars.ContextVar("worker_thread_payload", default=None)
        references = []
        app = App()

        class Held:
            def __init__(self, identity):
                self.identity = identity

        async def stamp(request: Request):
            held = Held(request.request_id)
            references.append(weakref.ref(held))
            marker.set(held)
            return request.request_id

        def capture(identity):
            held, request = marker.get(), current_request.get()
            return {
                "argument": identity,
                "marker": None if held is None else held.identity,
                "request": None if request is None else request.request_id,
            }

        dependency = Depends(stamp)
        if mode == "sync":

            def handler(identity=dependency):
                return capture(identity)

        else:

            async def handler(identity=dependency):
                if mode == "to_thread":
                    return await asyncio.to_thread(capture, identity)
                return await asyncio.get_running_loop().run_in_executor(
                    None, capture, identity
                )

        app.get("/context")(handler)
        with Worker(app, sync_workers=1) as worker:
            for identity in ("first", "second"):
                expected = None if mode == "run_in_executor" else identity
                self.assertEqual(
                    payload(
                        worker.submit(
                            0,
                            {"__bessemer_request__": {"request_id": identity}},
                        ).result(timeout=5)
                    ),
                    (
                        200,
                        {"argument": identity, "marker": expected, "request": expected},
                    ),
                )
                self.assertEqual(
                    worker._executor.submit(
                        lambda: (marker.get(), current_request.get())
                    ).result(timeout=5),
                    (None, None),
                )
                self._assert_collected(references[-1])
            self.assertTrue(
                any(thread.is_alive() for thread in worker._executor._threads)
            )

    def _assert_collected(self, reference):
        deadline = time.monotonic() + 2
        while reference() is not None and time.monotonic() < deadline:
            gc.collect()
            time.sleep(0.002)
        self.assertIsNone(
            reference(), "an idle pool thread retained its request payload"
        )


if __name__ == "__main__":
    unittest.main()
