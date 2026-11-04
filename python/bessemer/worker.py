"""One persistent asyncio worker, including lifespan and cancellation cleanup."""

from __future__ import annotations

import asyncio
import concurrent.futures
import inspect
import logging
import threading
from typing import Any, Self

from .application import App, HTTPException, _positive_float

_LOG = logging.getLogger("bessemer")


class _InvocationFuture(concurrent.futures.Future):
    """A cancellation request completes only after the actual work finishes.

    Unlike Future.cancel(), cancel() here does not release native admission
    capacity while an asyncio handler is still in its cancellation cleanup.
    Python threads cannot be interrupted, so cancellation is declined after
    synchronous execution starts.
    """

    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        super().__init__()
        self._loop = loop
        self._control = threading.Lock()
        self._task: asyncio.Task | None = None
        self._cancel_requested = False
        self._cancel_delivered = False
        self._sync_running = False

    def cancel(self) -> bool:
        with self._control:
            if self.done() or self._sync_running:
                return False
            if self._cancel_requested:
                return True
            self._cancel_requested = True
            task = self._task
        if task is not None:
            self._loop.call_soon_threadsafe(self._cancel_task)
        return True

    def _cancel_task(self) -> None:
        with self._control:
            task = self._task
            should_cancel = (
                self._cancel_requested
                and not self._sync_running
                and not self._cancel_delivered
            )
            if should_cancel:
                self._cancel_delivered = True
        if task is not None and should_cancel and not task.done():
            task.cancel()

    def _bind(self, task: asyncio.Task) -> None:
        with self._control:
            self._task = task
        self._cancel_task()

    def _begin_sync(self) -> bool:
        with self._control:
            if self._cancel_requested:
                return False
            self._sync_running = True
            return True

    def _end_sync(self) -> None:
        with self._control:
            self._sync_running = False

    def _finish(self, task: asyncio.Task) -> None:
        if task.cancelled():
            super().cancel()
        else:
            error = task.exception()
            if error is not None:
                self.set_exception(error)
            else:
                self.set_result(task.result())


class Worker:
    """Runs application work and lifespan on the same loop in one thread."""

    def __init__(
        self, app: App, *, graceful_timeout: float = 30.0, sync_workers: int = 8
    ) -> None:
        app.build()
        self.app = app
        self.graceful_timeout = _positive_float(graceful_timeout, "graceful_timeout")
        if type(sync_workers) is not int or sync_workers < 1:
            raise ValueError("sync_workers must be a positive integer")
        self._executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=sync_workers, thread_name_prefix="bessemer-handler"
        )
        self._loop = asyncio.new_event_loop()
        self._ready = threading.Event()
        self._startup_error: BaseException | None = None
        self._shutdown_error: BaseException | None = None
        self._lifespan = None
        self._entered_lifespan = False
        self._lifecycle_task: asyncio.Task | None = None
        self._stop_event: asyncio.Event | None = None
        self._stop_requested = threading.Event()
        self._tasks: dict[asyncio.Task, _InvocationFuture] = {}
        self._accepting = False
        self._closed = False
        self._thread = threading.Thread(
            target=self._run, name="bessemer-asyncio", daemon=True
        )

    def __enter__(self) -> Self:
        self.start()
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.close()

    def start(self) -> None:
        if self._closed or self._thread.ident is not None:
            raise RuntimeError("a Worker can only be started once")
        self._thread.start()
        try:
            ready = self._ready.wait(self.graceful_timeout)
        except BaseException:
            self._schedule_stop()
            self._thread.join(timeout=self.graceful_timeout)
            self._closed = not self._thread.is_alive()
            raise
        if not ready:
            self._schedule_stop()
            self._thread.join(timeout=self.graceful_timeout)
            self._closed = not self._thread.is_alive()
            raise TimeoutError(
                "application lifespan startup timed out; cancellation has been requested"
            )
        if self._startup_error is not None:
            self._thread.join(timeout=self.graceful_timeout)
            self._closed = not self._thread.is_alive()
            if not self._closed:
                raise TimeoutError(
                    "application startup failed; resource cleanup is still running"
                ) from self._startup_error
            raise self._startup_error

    def _run(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.set_default_executor(self._executor)
        try:
            self._lifecycle_task = self._loop.create_task(self._lifecycle())
            if self._stop_requested.is_set():
                self._lifecycle_task.cancel()
            self._loop.run_until_complete(self._lifecycle_task)
        except BaseException as error:  # noqa: BLE001 -- Send worker failures to the calling thread after cleanup.
            if self._startup_error is None:
                self._shutdown_error = error
        finally:
            self._accepting = False
            self._ready.set()
            try:
                self._loop.run_until_complete(self._cancel_background_tasks())
                self._loop.run_until_complete(self._loop.shutdown_asyncgens())
            finally:
                self._executor.shutdown(wait=True, cancel_futures=True)
                self._loop.close()

    async def _cancel_background_tasks(self) -> None:
        current = asyncio.current_task()
        tasks = [task for task in asyncio.all_tasks() if task is not current]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def _schedule_stop(self) -> None:
        self._stop_requested.set()
        if self._thread.is_alive():
            try:
                self._loop.call_soon_threadsafe(self._request_stop)
            except RuntimeError:
                if not self._loop.is_closed():
                    raise

    async def _lifecycle(self) -> None:
        self._stop_event = asyncio.Event()
        try:
            if self.app.lifespan is not None:
                self._lifespan = self.app.lifespan(self.app)
                await self._lifespan.__aenter__()
                self._entered_lifespan = True
            self._accepting = not self._stop_requested.is_set()
            self._ready.set()
            if self._accepting:
                await self._stop_event.wait()
        except BaseException as error:  # noqa: BLE001 -- Startup cancellation also requires lifespan cleanup.
            if not self._ready.is_set():
                self._startup_error = error
            else:
                self._shutdown_error = error
        finally:
            self._accepting = False
            self._ready.set()
            await self._shutdown()

    def _request_stop(self) -> None:
        self._accepting = False
        if self._lifecycle_task is not None and not self._ready.is_set():
            self._lifecycle_task.cancel()
        elif self._stop_event is not None:
            self._stop_event.set()

    def submit(
        self, route_id: int, arguments: dict[str, Any]
    ) -> concurrent.futures.Future:
        if not self._accepting or self._closed:
            raise RuntimeError("application worker is not accepting requests")
        if type(route_id) is not int or not 0 <= route_id < len(self.app._compiled):
            raise ValueError("unknown route id")
        future = _InvocationFuture(self._loop)

        def schedule() -> None:
            if not self._accepting:
                future.set_exception(RuntimeError("application worker is stopping"))
                return
            task = self._loop.create_task(self._invoke(route_id, arguments, future))
            self._tasks[task] = future
            future._bind(task)

            def done(completed: asyncio.Task) -> None:
                self._tasks.pop(completed, None)
                future._finish(completed)

            task.add_done_callback(done)

        self._loop.call_soon_threadsafe(schedule)
        return future

    async def _invoke(
        self, route_id: int, arguments: dict[str, Any], future: _InvocationFuture
    ):
        route = self.app._compiled[route_id]
        handler = route.handler
        if handler is None:
            return 200, {"ok": True}
        try:
            for name in (*route.body_models, *route.body_containers):
                if arguments.get(name) is None and name not in route.nullable_body:
                    return 422, {
                        "detail": [
                            {
                                "loc": ["body"],
                                "type": "value_error",
                                "msg": "Request body cannot be null",
                            }
                        ]
                    }
            for name, container in route.body_containers.items():
                value = arguments.get(name)
                if value is not None and not isinstance(value, container):
                    return 422, {
                        "detail": [
                            {
                                "loc": ["body"],
                                "type": "dict_type"
                                if container is dict
                                else "list_type",
                                "msg": "Expected a JSON object"
                                if container is dict
                                else "Expected a JSON array",
                            }
                        ]
                    }
            for name, model in route.body_models.items():
                if name in arguments and arguments[name] is not None:
                    try:
                        arguments[name] = model.model_validate(arguments[name])
                    except ValueError as error:
                        errors = getattr(error, "errors", None)
                        if not callable(errors):
                            return 422, {
                                "detail": [
                                    {
                                        "loc": ["body", name],
                                        "type": "value_error",
                                        "msg": "Invalid request body",
                                    }
                                ]
                            }
                        details = []
                        for item in errors(
                            include_url=False,
                            include_context=False,
                            include_input=False,
                        ):
                            details.append(
                                {
                                    "loc": ["body", *item.get("loc", ())],
                                    "type": item.get("type", "value_error"),
                                    "msg": item.get("msg", "Invalid request body"),
                                }
                            )
                        return 422, {"detail": details}
            if inspect.iscoroutinefunction(handler):
                result = await handler(**arguments)
            else:
                if not future._begin_sync():
                    raise asyncio.CancelledError
                try:
                    result = await self._loop.run_in_executor(
                        self._executor, lambda: handler(**arguments)
                    )
                finally:
                    future._end_sync()
                if inspect.isawaitable(result):
                    result = await result
            if callable(getattr(result, "model_dump", None)):
                result = result.model_dump(mode="json")
            return route.declaration["status_code"], result
        except HTTPException as error:
            return error.status_code, {"detail": error.detail}
        except asyncio.CancelledError:
            raise
        except BaseException:  # noqa: BLE001 -- A handler must not terminate the shared worker loop.
            _LOG.exception(
                "Unhandled error in %s %s",
                route.declaration["method"],
                route.declaration["path"],
            )
            return 500, {"detail": "Internal Server Error"}

    async def _shutdown(self) -> None:
        self._accepting = False
        tasks = list(self._tasks)
        for task in tasks:
            self._tasks[task].cancel()
        if tasks:
            # Never abandon work when the caller's shutdown deadline expires.
            # close() reports that deadline; this lifecycle continues cleanup
            # and exits automatically when the actual handler work completes.
            await asyncio.gather(*tasks, return_exceptions=True)
        if self._entered_lifespan:
            await self._lifespan.__aexit__(None, None, None)

    def close(self) -> None:
        if self._closed:
            return
        if self._thread.ident is None:
            self._executor.shutdown(wait=True, cancel_futures=True)
            self._loop.close()
            self._closed = True
            return
        self._schedule_stop()
        self._thread.join(timeout=self.graceful_timeout)
        if self._thread.is_alive():
            raise TimeoutError(
                "application worker did not stop before the shutdown deadline; cleanup will continue, and synchronous handlers cannot be force-stopped"
            )
        self._closed = True
        if self._shutdown_error is not None:
            raise self._shutdown_error
