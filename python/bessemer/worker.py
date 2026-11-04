"""One persistent asyncio worker, including lifespan and cancellation cleanup."""

from __future__ import annotations

import asyncio
import concurrent.futures
import contextvars
import inspect
import logging
import os
import signal
import threading
from contextlib import AsyncExitStack, asynccontextmanager, contextmanager
from itertools import islice
from typing import Any, Self

from .application import App, HTTPException, _positive_float
from .dependencies import DependencyResolver
from .request import BackgroundTasks, Request, RequestValidationError, current_request
from .responses import Response, jsonable

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
        self._context = contextvars.copy_context()

    def cancel(self) -> bool:
        with self._control:
            if self.done():
                return False
            if self._sync_running:
                # The running call cannot be interrupted. Remember the deadline
                # so later dependencies/awaitables do not start fresh user work.
                self._cancel_requested = True
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

    def _raise_if_cancel_requested(self) -> None:
        with self._control:
            cancelled = self._cancel_requested
        if cancelled:
            raise asyncio.CancelledError

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
        self,
        app: App,
        *,
        graceful_timeout: float = 30.0,
        startup_timeout: float | None = None,
        sync_workers: int = 8,
        heartbeat_fd: int | None = None,
        max_response_size: int = 4_194_304,
    ) -> None:
        app.build()
        self.app = app
        self.graceful_timeout = _positive_float(graceful_timeout, "graceful_timeout")
        self.startup_timeout = (
            self.graceful_timeout
            if startup_timeout is None
            else _positive_float(startup_timeout, "startup_timeout")
        )
        if type(sync_workers) is not int or sync_workers < 1:
            raise ValueError("sync_workers must be a positive integer")
        if type(max_response_size) is not int or max_response_size < 1:
            raise ValueError("max_response_size must be a positive integer")
        self.max_response_size = max_response_size
        if heartbeat_fd is not None:
            if type(heartbeat_fd) is not int or heartbeat_fd < 0:
                raise ValueError(
                    "heartbeat_fd must be an open nonnegative file descriptor"
                )
            os.fstat(heartbeat_fd)
            os.set_blocking(heartbeat_fd, False)
        self._heartbeat_fd = heartbeat_fd
        self._heartbeat_task: asyncio.Task | None = None
        self._executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=sync_workers, thread_name_prefix="bessemer-handler"
        )
        self._loop = asyncio.new_event_loop()
        self._ready = threading.Event()
        self._startup_error: BaseException | None = None
        self._shutdown_error: BaseException | None = None
        self._lifecycle_context: contextvars.Context | None = None
        self._app_dependency_stack = AsyncExitStack()
        self._app_dependency_cache: dict[int, Any] = {}
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
            ready = self._ready.wait(self.startup_timeout)
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
            self._lifecycle_context = contextvars.copy_context()
            self._lifecycle_task = self._loop.create_task(
                self._lifecycle(), context=self._lifecycle_context
            )
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
                await self._app_dependency_stack.enter_async_context(
                    self.app.lifespan(self.app)
                )
            await self._prepare_app_dependencies()
            if self._heartbeat_fd is not None:
                self._heartbeat_task = self._loop.create_task(self._heartbeat())
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

    async def _heartbeat(self) -> None:
        while True:
            try:
                os.write(self._heartbeat_fd, b"h")
            except BlockingIOError:
                pass
            except OSError as error:
                self._shutdown_error = RuntimeError(
                    "supervisor heartbeat channel closed"
                )
                if os.environ.get("BESSEMER_SUPERVISED_WORKER") == "1":
                    os.kill(os.getpid(), signal.SIGTERM)
                else:
                    self._request_stop()
                _LOG.error(
                    "Supervisor heartbeat channel failed: %s", type(error).__name__
                )
                return
            await asyncio.sleep(0.5)

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
            task = self._loop.create_task(
                self._invoke(route_id, arguments, future), context=future._context
            )
            self._tasks[task] = future
            future._bind(task)

            def done(completed: asyncio.Task) -> None:
                self._tasks.pop(completed, None)
                future._finish(completed)

            task.add_done_callback(done)

        self._loop.call_soon_threadsafe(schedule)
        return future

    @staticmethod
    def _details(
        error: ValueError | TypeError, location: list[str], message: str
    ) -> list[dict[str, Any]]:
        errors = getattr(error, "errors", None)
        if callable(errors):
            error_count = getattr(error, "error_count", None)
            if callable(error_count) and error_count() > 100:
                return [{"loc": location, "type": "too_many_errors", "msg": message}]
            return [
                {
                    "loc": [*location, *item.get("loc", ())],
                    "type": item.get("type", "value_error"),
                    # Custom Pydantic error templates may interpolate raw input,
                    # even when include_input/context are disabled.
                    "msg": message,
                }
                for item in islice(
                    errors(
                        include_url=False, include_context=False, include_input=False
                    ),
                    100,
                )
            ]
        return [{"loc": location, "type": "value_error", "msg": message}]

    def _validate_arguments(self, route, arguments: dict[str, Any]) -> dict[str, Any]:
        values = arguments.copy()
        parameters = {item["name"]: item for item in route.declaration["parameters"]}
        for name, parameter in parameters.items():
            location = [parameter["source"], parameter.get("alias", name)]
            if name not in values:
                if parameter["required"]:
                    raise RequestValidationError(
                        [{"loc": location, "type": "missing", "msg": "Field required"}]
                    )
                values[name] = parameter["default"]
        for name, validate in (getattr(route, "input_validators", None) or {}).items():
            parameter = parameters[name]
            location = [parameter["source"], parameter.get("alias", name)]
            try:
                values[name] = validate(values[name])
            except (ValueError, TypeError) as error:
                raise RequestValidationError(
                    self._details(error, location, "Invalid request input")
                ) from error
        for name in (*route.body_models, *route.body_containers):
            if values.get(name) is None and name not in route.nullable_body:
                raise RequestValidationError(
                    [
                        {
                            "loc": ["body"],
                            "type": "value_error",
                            "msg": "Request body cannot be null",
                        }
                    ]
                )
        for name, container in route.body_containers.items():
            value = values.get(name)
            if value is not None and not isinstance(value, container):
                raise RequestValidationError(
                    [
                        {
                            "loc": ["body"],
                            "type": "dict_type" if container is dict else "list_type",
                            "msg": "Expected a JSON object"
                            if container is dict
                            else "Expected a JSON array",
                        }
                    ]
                )
        for name, model in route.body_models.items():
            if values.get(name) is not None:
                try:
                    values[name] = model.model_validate(values[name])
                except ValueError as error:
                    raise RequestValidationError(
                        self._details(error, ["body"], "Invalid request body")
                    ) from error
        return values

    async def _call_sync(
        self,
        function,
        args,
        kwargs,
        future: _InvocationFuture | None,
        *,
        on_result=None,
        execution_context=None,
    ):
        # Thread context mutations do not propagate back into the request Task.
        # A generator retains its own context across acquisition and cleanup so
        # ContextVar tokens can safely be reset in its finalizer.
        if future is not None:
            future._raise_if_cancel_requested()
        context = (
            contextvars.copy_context()
            if execution_context is None
            else execution_context
        )

        def invoke():
            # Queueing is not execution: a deadline may cancel this request
            # while all executor threads are still occupied by earlier calls.
            if future is not None and not future._begin_sync():
                raise asyncio.CancelledError
            try:
                return context.run(function, *args, **kwargs)
            finally:
                if future is not None:
                    future._end_sync()

        work = self._loop.run_in_executor(self._executor, invoke)
        cancellation = None
        try:
            result = await asyncio.shield(work)
        except asyncio.CancelledError as error:
            # A cancelled queued wrapper still owns arguments/context until the
            # executor processes it. Keep request capacity until then, and wait
            # for actual completion when synchronous user work already started.
            cancellation = error
            while not work.done():
                try:
                    await asyncio.shield(work)
                except asyncio.CancelledError:
                    continue
            result = work.result()
        if on_result is not None:
            # Register newly acquired resources even when cancellation arrived
            # while their synchronous acquisition was still running.
            on_result(result)
        if cancellation is not None:
            if on_result is None and inspect.iscoroutine(result):
                result.close()
            raise cancellation
        return result

    @staticmethod
    async def _await_result(result, future):
        if inspect.isawaitable(result):
            if future is not None:
                try:
                    future._raise_if_cancel_requested()
                except asyncio.CancelledError:
                    if inspect.iscoroutine(result):
                        result.close()
                    raise
            return await result
        return result

    async def _execute_plan(self, plan, arguments, stack: AsyncExitStack, future):
        if plan.kind == "async_generator":
            if future is not None:
                future._raise_if_cancel_requested()
            return await stack.enter_async_context(
                asynccontextmanager(plan.call)(**arguments)
            )
        if plan.kind == "generator":
            manager = contextmanager(plan.call)(**arguments)
            context = contextvars.copy_context()

            async def exit_generator(error_type, error, traceback):
                # Cleanup is deliberately not gated by a previously requested
                # cancellation; it must execute even after the handler was cancelled.
                return await self._call_sync(
                    manager.__exit__,
                    (error_type, error, traceback),
                    {},
                    None,
                    execution_context=context,
                )

            return await self._call_sync(
                manager.__enter__,
                (),
                {},
                future,
                on_result=lambda _: stack.push_async_exit(exit_generator),
                execution_context=context,
            )
        if plan.kind == "async":
            if future is not None:
                future._raise_if_cancel_requested()
            return await plan.call(**arguments)
        if plan.kind != "sync":
            raise RuntimeError("unknown compiled callable kind")
        result = await self._call_sync(plan.call, (), arguments, future)
        return await self._await_result(result, future)

    @staticmethod
    async def _finish_exit(
        stack: AsyncExitStack, error: BaseException | None, *, context=None
    ):
        async def close_stack():
            try:
                return True, await stack.__aexit__(
                    type(error) if error else None,
                    error,
                    error.__traceback__ if error else None,
                )
            except BaseException as cleanup_error:  # noqa: BLE001 -- Preserve finalizer failures without terminating the loop.
                # asyncio Tasks rethrow SystemExit/KeyboardInterrupt onto their
                # loop. Deliver user finalizer failures back to the invocation
                # so the normal safe-error boundary still applies.
                return False, cleanup_error

        cleanup = asyncio.create_task(close_stack(), context=context)
        cancellation = None
        while not cleanup.done():
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError as caught:
                cancellation = caught
        succeeded, suppressed = cleanup.result()
        if not succeeded:
            raise suppressed
        if cancellation is not None:
            raise cancellation
        return suppressed

    def _resolver(self, route, arguments, request, background, stack, future):
        return DependencyResolver(
            route,
            arguments,
            state=self.app.state,
            request=request,
            background=background,
            invoke=lambda plan, kwargs, target_stack: self._execute_plan(
                plan, kwargs, target_stack, future
            ),
            request_stack=stack,
            app_stack=self._app_dependency_stack,
            app_cache=self._app_dependency_cache,
        )

    async def _prepare_app_dependencies(self) -> None:
        for route in self.app._compiled:
            if not getattr(route, "dependencies", ()):
                continue
            resolver = self._resolver(
                route, {}, None, None, self._app_dependency_stack, None
            )
            for plan in route.dependencies:
                if plan.scope == "app":
                    await resolver.resolve(plan.id)

    async def _execute_route(self, route, arguments, request, background, future):
        stack = AsyncExitStack()
        try:
            plan = getattr(route, "plan", None)
            if plan is None:
                # Compatibility for applications compiled before callable plans
                # were introduced. New applications classify callables at build.
                from types import SimpleNamespace

                plan = SimpleNamespace(
                    call=route.handler,
                    kind="async"
                    if inspect.iscoroutinefunction(route.handler)
                    else "sync",
                )
                result = await self._execute_plan(plan, arguments, stack, future)
            else:
                resolver = self._resolver(
                    route, arguments, request, background, stack, future
                )
                for binding in getattr(route, "before", ()):
                    await resolver.binding(binding)
                result = await self._execute_plan(
                    plan, await resolver.arguments_for(plan), stack, future
                )
            if isinstance(result, Response):
                response = result.to_native(max_bytes=self.max_response_size)
            else:
                adapter = getattr(route, "response_adapter", None)
                if adapter is not None:
                    result = adapter.dump_python(
                        adapter.validate_python(result),
                        mode="json",
                        by_alias=True,
                        warnings="error",
                    )
                response = (
                    route.declaration["status_code"],
                    jsonable(result, max_bytes=self.max_response_size),
                )
            for task in background._tasks:
                future._raise_if_cancel_requested()
                if task.kind == "async":
                    await task.call(*task.args, **task.kwargs)
                else:
                    result = await self._call_sync(
                        task.call, task.args, task.kwargs, future
                    )
                    await self._await_result(result, future)
        except BaseException as error:
            suppressed = await self._finish_exit(stack, error, context=future._context)
            if suppressed:
                raise RuntimeError(
                    "a dependency suppressed an error without returning a response"
                ) from error
            raise
        else:
            await self._finish_exit(stack, None, context=future._context)
            return response

    async def _invoke(
        self, route_id: int, arguments: dict[str, Any], future: _InvocationFuture
    ):
        route = self.app._compiled[route_id]
        if route.handler is None:
            return 200, {"ok": True}
        metadata = arguments.get("__bessemer_request__") or {
            "path": route.declaration["path"],
            "method": route.declaration["method"],
        }
        request = Request(metadata, app=self.app)
        token = current_request.set(request)
        try:
            arguments = self._validate_arguments(
                route,
                {
                    key: value
                    for key, value in arguments.items()
                    if key != "__bessemer_request__"
                },
            )
            return await self._execute_route(
                route, arguments, request, BackgroundTasks(), future
            )
        except HTTPException as error:
            handled = await self._handle_exception(request, error, future)
            if handled is not None:
                return handled
            try:
                response = (
                    error.status_code,
                    jsonable(
                        {"detail": error.detail}, max_bytes=self.max_response_size
                    ),
                )
                if getattr(error, "headers", None):
                    return (*response, error.headers.raw, "json")
                return response
            except (TypeError, ValueError):
                _LOG.error(
                    "Invalid HTTPException detail in %s %s",
                    route.declaration["method"],
                    route.declaration["path"],
                )
                return 500, {"detail": "Internal Server Error"}
        except RequestValidationError as error:
            handled = await self._handle_exception(request, error, future)
            if handled is not None:
                return handled
            return 422, {"detail": error.details}
        except asyncio.CancelledError:
            raise
        except BaseException as error:  # noqa: BLE001 -- A handler must not terminate the shared worker loop.
            _LOG.error(
                "Unhandled %s in %s %s",
                type(error).__name__,
                route.declaration["method"],
                route.declaration["path"],
            )
            handled = await self._handle_exception(request, error, future)
            if handled is not None:
                return handled
            return 500, {"detail": "Internal Server Error"}
        finally:
            current_request.reset(token)

    async def _handle_exception(self, request, error, future):
        handlers = getattr(self.app, "_exception_handlers", {})
        plan = next(
            (
                handlers[error_type]
                for error_type in type(error).__mro__
                if error_type in handlers
            ),
            None,
        )
        if plan is None:
            return None
        try:
            if plan.kind == "async":
                future._raise_if_cancel_requested()
                response = await plan.call(request, error)
            else:
                response = await self._call_sync(
                    plan.call, (request, error), {}, future
                )
                response = await self._await_result(response, future)
            if not isinstance(response, Response):
                raise TypeError("exception handlers must return a Response")
            return response.to_native(max_bytes=self.max_response_size)
        except asyncio.CancelledError:
            raise
        except BaseException as handler_error:  # noqa: BLE001 -- An exception handler must not terminate the shared loop.
            _LOG.error(
                "Unhandled %s in an application exception handler",
                type(handler_error).__name__,
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
        await self._finish_exit(
            self._app_dependency_stack,
            self._startup_error or self._shutdown_error,
            context=self._lifecycle_context,
        )

    def close(self) -> None:
        if self._closed:
            return
        if self._thread.ident is None:
            self._executor.shutdown(wait=True, cancel_futures=True)
            self._loop.close()
            self._closed = True
            return
        if self._heartbeat_fd is not None and not getattr(
            self, "_drain_announced", False
        ):
            self._drain_announced = True
            try:
                os.write(self._heartbeat_fd, b"d")
            except OSError as error:
                self._shutdown_error = self._shutdown_error or RuntimeError(
                    "supervisor drain notification failed"
                )
                _LOG.error("Worker drain notification failed: %s", type(error).__name__)
                if os.environ.get("BESSEMER_SUPERVISED_WORKER") == "1":
                    # Native service has stopped. Do not leave an unobservable
                    # interpreter hanging in executor atexit with no watchdog.
                    os.kill(os.getpid(), signal.SIGKILL)
        self._schedule_stop()
        self._thread.join(timeout=self.graceful_timeout)
        if self._thread.is_alive():
            raise TimeoutError(
                "application worker did not stop before the shutdown deadline; cleanup will continue, and synchronous handlers cannot be force-stopped"
            )
        self._closed = True
        if self._shutdown_error is not None:
            raise self._shutdown_error
