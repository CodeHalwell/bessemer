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
from contextlib import AsyncExitStack, asynccontextmanager, contextmanager, suppress
from itertools import islice
from typing import Any, Self

from . import otel
from .application import App, HTTPException, _positive_float
from .auth import Principal
from .dependencies import DependencyResolver
from .request import BackgroundTasks, Request, RequestValidationError, current_request
from .responses import (
    Headers,
    Response,
    StreamingResponse,
    background_tasks,
    jsonable,
    validate_status,
)
from .websockets import WebSocket, WebSocketDisconnect, WebSocketException

try:
    from ._native import _encode_plain_json
except ImportError:  # Source-only use without the extension.
    _encode_plain_json = None

# Form fields and files are reported under "body", as FastAPI does.
_LOCATIONS = {"form": "body", "file": "body"}
# Arguments the native server adds for the worker, never for the handler.
_NATIVE_ARGUMENTS = frozenset(
    {"__bessemer_request__", "__bessemer_principal__", "__bessemer_trace__"}
)
_LOG = logging.getLogger("bessemer")


class _InvocationControl:
    """A cancellation request completes only after the actual work finishes.

    cancel() here does not release native admission capacity while an
    asyncio handler is still in its cancellation cleanup. Python threads
    cannot be interrupted, so cancellation is declined after synchronous
    execution starts.
    """

    def _init_control(
        self, loop: asyncio.AbstractEventLoop, context: contextvars.Context
    ) -> None:
        self._loop = loop
        self._control = threading.Lock()
        self._task: asyncio.Task | None = None
        self._cancel_requested = False
        self._cancel_delivered = False
        self._sync_running = False
        self._context = context

    def done(self) -> bool:  # pragma: no cover - provided by subclasses
        raise NotImplementedError

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


class _InvocationFuture(_InvocationControl, concurrent.futures.Future):
    """Invocation handle returned by Worker.submit for thread-safe callers."""

    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        concurrent.futures.Future.__init__(self)
        self._init_control(loop, contextvars.copy_context())

    def done(self) -> bool:
        return concurrent.futures.Future.done(self)

    def _finish(self, task: asyncio.Task) -> None:
        if task.cancelled():
            concurrent.futures.Future.cancel(self)
        else:
            error = task.exception()
            if error is not None:
                self.set_exception(error)
            else:
                self.set_result(task.result())


_END = object()


class _CalledResponse(Response):
    """What ``call_next`` returns to HTTP middleware.

    Status and headers can be changed; the body stays in its native form
    (already-encoded JSON, bytes or a live stream), so wrapping a route in
    middleware does not re-encode or buffer it.
    """

    def __init__(self, envelope) -> None:
        if len(envelope) == 2:
            status, body = envelope
            headers, kind = (), "json"
        else:
            status, body, headers, kind = envelope
        self.status_code = status
        self.headers = Headers(headers)
        if kind in ("json", "json_bytes") and "content-type" not in self.headers:
            # The runtime would add this; middleware should see it too.
            self.headers["Content-Type"] = "application/json"
        self._native_body = body
        self._kind = kind

    @property
    def media_type(self) -> str | None:
        return self.headers.get("content-type")

    @property
    def body(self) -> bytes:
        raise TypeError(
            "a call_next response keeps its body native; return a new Response to replace it"
        )

    def to_native(self, *, max_bytes: int | None = None):
        return (
            validate_status(self.status_code),
            self._native_body,
            self.headers.raw,
            self._kind,
        )


async def _close_iterator(worker, iterator) -> None:
    # Closing a body that never started is best effort. A sync close may
    # block, so it runs on the executor like every other sync call.
    with suppress(Exception):
        if (aclose := getattr(iterator, "aclose", None)) is not None:
            await aclose()
        elif (close := getattr(iterator, "close", None)) is not None:
            await worker._call_sync(close, (), {}, None)


class _ResponseStream:
    """A streaming body pulled by the native runtime.

    A producer task runs the iterator ahead into a bounded buffer (64 KiB or
    the response size limit). Each native ``next()`` drains everything already
    buffered in one thread hop, so small chunks are batched without delaying
    any chunk that is ready. ``next()`` and ``close()`` are called from Tokio
    threads and return concurrent futures that complete only after the work on
    the worker loop has finished, so native admission is held through iterator
    finalizers, generator-dependency cleanup and background tasks.
    """

    def __init__(self, worker, response, stack, background, context, request) -> None:
        self._worker = worker
        self._request = request
        self._closing = False
        content = response.body_iterator
        self._async = hasattr(content, "__aiter__")
        self._iterator = content.__aiter__() if self._async else iter(content)
        # A sync iterator runs in executor threads; every step and its close
        # share one context so ContextVar changes and tokens survive across
        # chunks, as they do for generator dependencies.
        self._sync_context: contextvars.Context | None = None
        self._stack = stack
        self._background = background
        self._context = context
        self._limit = min(65_536, worker.max_response_size)
        self._buffer: list[bytes] = []
        self._buffered = 0
        self._ready = asyncio.Event()
        self._space = asyncio.Event()
        self._space.set()
        self._producer: asyncio.Task | None = None
        self._ended = False
        self._error: BaseException | None = None
        # A failure in background tasks or dependency cleanup after the last
        # chunk; it truncates the body rather than ending it successfully.
        self._cleanup_failed = False
        self._finished = False
        # Background tasks run as their own task so a close (disconnect,
        # idle timeout, shutdown) after EOF can still interrupt them.
        self._background_run: asyncio.Task | None = None
        self._done = asyncio.Event()

    def _submit(self, coroutine_function) -> concurrent.futures.Future:
        result = concurrent.futures.Future()

        def schedule() -> None:
            task = self._worker._loop.create_task(
                coroutine_function(), context=self._context
            )
            self._worker._stream_tasks.add(task)

            def done(completed: asyncio.Task) -> None:
                self._worker._stream_tasks.discard(completed)
                if completed.cancelled():
                    result.set_exception(RuntimeError("stream step cancelled"))
                elif completed.exception() is not None:
                    result.set_exception(completed.exception())
                else:
                    result.set_result(completed.result())

            task.add_done_callback(done)

        try:
            self._worker._loop.call_soon_threadsafe(schedule)
        except RuntimeError as error:
            result.set_exception(error)
        return result

    def next(self) -> concurrent.futures.Future:
        return self._submit(self._next)

    def close(self) -> concurrent.futures.Future:
        return self._submit(self._close)

    async def _produce(self) -> None:
        # The handler's request binding was reset when _invoke returned; the
        # stream still belongs to that request.
        current_request.set(self._request)
        if not self._async and self._sync_context is None:
            self._sync_context = contextvars.copy_context()
        maximum = self._worker.max_response_size
        try:
            while True:
                await self._space.wait()
                if self._async:
                    chunk = await anext(self._iterator, _END)
                else:
                    chunk = await self._worker._call_sync(
                        next,
                        (self._iterator, _END),
                        {},
                        None,
                        execution_context=self._sync_context,
                    )
                if chunk is _END:
                    break
                if isinstance(chunk, str):
                    chunk = chunk.encode("utf-8")
                elif isinstance(chunk, (bytearray, memoryview)):
                    chunk = bytes(chunk)
                elif not isinstance(chunk, bytes):
                    raise TypeError("streaming response chunks must be bytes or str")
                if len(chunk) > maximum:
                    raise ValueError(
                        "a streaming response chunk exceeds the size limit"
                    )
                if chunk:
                    self._buffer.append(chunk)
                    self._buffered += len(chunk)
                    self._ready.set()
                    if self._buffered >= self._limit:
                        self._space.clear()
                else:
                    # Empty chunks never fill the buffer, so yield to the loop
                    # to keep an iterator that never suspends from starving it.
                    await asyncio.sleep(0)
        except asyncio.CancelledError:
            raise
        except BaseException as error:  # noqa: BLE001 -- A stream must not terminate the shared loop.
            _LOG.error("Unhandled %s in a streaming response", type(error).__name__)
            self._error = error
        self._ended = True
        self._ready.set()

    async def _next(self) -> bytes | None:
        if self._finished:
            return None
        if self._producer is None:
            self._producer = (
                asyncio.current_task()
                .get_loop()
                .create_task(self._produce(), context=self._context)
            )
        await self._ready.wait()
        if self._closing:
            # close() owns cleanup; it woke this pending read.
            await self._done.wait()
            return None
        if self._buffer:
            # Drain whole chunks up to the size limit; the first always fits.
            maximum = self._worker.max_response_size
            taken, size = 0, 0
            for chunk in self._buffer:
                if taken and size + len(chunk) > maximum:
                    break
                taken += 1
                size += len(chunk)
            data = self._buffer[0] if taken == 1 else b"".join(self._buffer[:taken])
            del self._buffer[:taken]
            self._buffered -= size
            if not self._buffer and not self._ended:
                self._ready.clear()
            if self._buffered < self._limit:
                self._space.set()
            return data
        await self._finish(self._error)
        if self._error is not None or self._cleanup_failed:
            raise RuntimeError("streaming response failed") from None
        return None

    async def _close(self) -> None:
        if self._closing:
            # A close is already running (for example a disconnect, then
            # worker shutdown): wait for it rather than cancelling the
            # producer again, which would interrupt the iterator's cleanup.
            await self._done.wait()
            return
        self._closing = True
        producer = self._producer
        if producer is not None and not producer.done():
            # The client left while chunks were still being produced.
            producer.cancel()
            try:
                await producer
            except BaseException:  # noqa: BLE001, S110 -- Only completion matters here.
                pass
        # Wake a read blocked on the cancelled producer so its future settles.
        self._ready.set()
        if not self._finished:
            await self._finish(asyncio.CancelledError())
        elif self._background_run is not None and not self._background_run.done():
            # The body reached EOF and background tasks are still running:
            # interrupt them; dependency cleanup still runs, shielded.
            self._background_run.cancel()
        await self._done.wait()

    async def _run_background(self) -> None:
        for task in self._background._tasks:
            if task.kind == "async":
                await task.call(*task.args, **task.kwargs)
            else:
                result = await self._worker._call_sync(
                    task.call, task.args, task.kwargs, None
                )
                await self._worker._await_result(result, None)

    async def _finish(self, error: BaseException | None) -> None:
        if self._finished:
            await self._done.wait()
            return
        self._finished = True
        self._buffer.clear()
        current_request.set(self._request)
        try:
            if error is not None:
                close = getattr(
                    self._iterator, "aclose" if self._async else "close", None
                )
                if close is not None:
                    try:
                        if self._async:
                            await close()
                        else:
                            await self._worker._call_sync(
                                close,
                                (),
                                {},
                                None,
                                execution_context=self._sync_context,
                            )
                    except BaseException:  # noqa: BLE001
                        _LOG.error("Streaming response iterator failed to close")
            elif self._background._tasks:
                self._background_run = asyncio.get_running_loop().create_task(
                    self._run_background(), context=self._context
                )
                await self._background_run
        except BaseException as failure:  # noqa: BLE001
            _LOG.error(
                "Unhandled %s after a streaming response", type(failure).__name__
            )
            self._cleanup_failed = True
            error = error or failure
        finally:
            try:
                # A cancelled stream (disconnect, timeout, shutdown) unwinds
                # dependencies through their error path, as routes do.
                await self._worker._finish_exit(
                    self._stack, error, context=self._context
                )
            except BaseException as failure:  # noqa: BLE001
                _LOG.error(
                    "Unhandled %s in streaming dependency cleanup",
                    type(failure).__name__,
                )
                self._cleanup_failed = True
            self._worker._streams.discard(self)
            self._done.set()


class _NativeInvocation(_InvocationControl):
    """Invocation started from the native queue on the loop thread.

    Completion publishes directly through the native ticket, so no
    concurrent.futures.Future or cross-thread callback is involved. Like a
    request entering from a fresh native thread, it starts in an empty context.
    """

    def __init__(self, loop: asyncio.AbstractEventLoop, ticket) -> None:
        self._init_control(loop, contextvars.Context())
        self._ticket = ticket
        self._done = False

    def done(self) -> bool:
        return self._done

    def _finish(self, task: asyncio.Task) -> None:
        self._done = True
        self._ticket.complete(task)


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
        # The routes this worker serves. A later rebuild (for example after
        # dependency_overrides change) never alters a running worker.
        self._compiled = app._compiled
        # Per-route lookups computed once instead of on every request.
        self._route_parameters = [
            {item["name"]: item for item in route.declaration["parameters"]}
            for route in self._compiled
        ]
        self._direct_bindings = [
            self._direct_plan_bindings(route) for route in self._compiled
        ]
        # Routes with no declared inputs, validators or body bindings have
        # nothing for _validate_arguments to check or fill in.
        self._needs_validation = [
            bool(
                route.declaration["parameters"]
                or getattr(route, "input_validators", None)
                or route.body_models
                or route.body_containers
            )
            for route in self._compiled
        ]
        # Plain async handlers that take only native inputs need no resolver,
        # exit stack or background tasks: they run on the lean path.
        self._lean_plans = [
            route.plan
            if route.handler is not None
            and route.declaration.get("kind", "http") != "websocket"
            and direct is not None
            and getattr(route, "plan", None) is not None
            and route.plan.kind == "async"
            else None
            for route, direct in zip(app._compiled, self._direct_bindings, strict=True)
        ]
        # HTTP middleware per route, outermost first: the last registered wraps
        # the rest, and a mounted app's middleware runs inside this app's.
        outer = tuple(reversed(getattr(app, "_http_middleware", ())))
        self._route_middleware = [
            outer + getattr(route, "middleware", ()) for route in app._compiled
        ]
        # The App each route belongs to: a mounted app for its routes.
        self._owners = [getattr(route, "app", None) or app for route in app._compiled]
        self._route_identity = [
            (route.declaration["method"], route.declaration["path"])
            for route in app._compiled
        ]
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
        self._app_dependency_cache: dict[tuple[int, int], Any] = {}
        self._lifecycle_task: asyncio.Task | None = None
        self._stop_event: asyncio.Event | None = None
        self._stop_requested = threading.Event()
        self._tasks: dict[asyncio.Task, _InvocationControl] = {}
        self._streams: set[_ResponseStream] = set()
        self._stream_tasks: set[asyncio.Task] = set()
        self._bridge = None
        self._native_invocations: dict[int, _NativeInvocation] = {}
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
            self._close_native()
            self._ready.set()
            await self._shutdown()

    def _request_stop(self) -> None:
        self._accepting = False
        self._close_native()
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
        if type(route_id) is not int or not 0 <= route_id < len(self._compiled):
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

    def attach_native(self, bridge) -> None:
        """Serve requests queued by native code on ``bridge``.

        The loop watches the bridge's descriptor and drains queued requests in
        batches on its own thread, so native threads never enter Python on the
        request path. Call after start() and before the server accepts traffic.
        """
        installed = threading.Event()
        failure: list[BaseException] = []

        def install() -> None:
            try:
                if not self._accepting or self._closed:
                    raise RuntimeError("application worker is not accepting requests")
                if self._bridge is not None:
                    raise RuntimeError("a native bridge is already attached")
                self._loop.add_reader(bridge.fileno(), self._drain_native)
                self._bridge = bridge
            except BaseException as error:  # noqa: BLE001 -- Report to the calling thread.
                failure.append(error)
            finally:
                installed.set()

        self._loop.call_soon_threadsafe(install)
        if not installed.wait(self.startup_timeout):
            raise TimeoutError("the worker loop did not attach the native bridge")
        if failure:
            raise failure[0]

    def _close_native(self) -> None:
        bridge, self._bridge = self._bridge, None
        if bridge is None:
            return
        try:
            self._loop.remove_reader(bridge.fileno())
        finally:
            # Answers anything still queued; later submissions fail fast.
            bridge.close()

    def _drain_native(self) -> None:
        bridge = self._bridge
        if bridge is None:
            return
        jobs, cancels = bridge.drain()
        loop = self._loop
        invocations = self._native_invocations
        for ticket, route_id, arguments in jobs:
            if not self._accepting:
                ticket.reject()
                continue
            invocation = _NativeInvocation(loop, ticket)
            # Start eagerly: the handler runs now, inside its own Task and
            # context, up to its first real suspension. A handler that never
            # suspends completes here and is answered without being scheduled
            # on the loop or waiting for a done callback.
            # A task factory installed by the application still gets every task.
            coroutine = self._invoke(route_id, arguments, invocation)
            if loop.get_task_factory() is None:
                task = asyncio.Task(
                    coroutine,
                    loop=loop,
                    context=invocation._context,
                    eager_start=True,
                )
            else:
                task = loop.create_task(coroutine, context=invocation._context)
            if task.done():
                invocation._finish(task)
                continue
            self._tasks[task] = invocation
            invocations[ticket.id] = invocation
            invocation._bind(task)
            task.add_done_callback(self._native_done)
        for identifier in cancels:
            invocation = invocations.get(identifier)
            if invocation is not None:
                invocation.cancel()

    def _native_done(self, task: asyncio.Task) -> None:
        invocation = self._tasks.pop(task)
        self._native_invocations.pop(invocation._ticket.id, None)
        invocation._finish(task)

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

    @staticmethod
    def _direct_plan_bindings(route):
        """Return handler bindings that need no resolver, or None.

        A route without before-dependencies whose handler only receives native
        inputs can be called without constructing a DependencyResolver.
        """
        plan = getattr(route, "plan", None)
        if plan is None or getattr(route, "before", ()):
            return None
        if any(binding.kind != "input" for binding in plan.bindings):
            return None
        return tuple((binding.name, binding.key) for binding in plan.bindings)

    def _validate_arguments(
        self, route, arguments: dict[str, Any], parameters=None
    ) -> dict[str, Any]:
        values = arguments.copy()
        if parameters is None:
            parameters = {
                item["name"]: item for item in route.declaration["parameters"]
            }
        for name, parameter in parameters.items():
            location = [
                _LOCATIONS.get(parameter["source"], parameter["source"]),
                parameter.get("alias", name),
            ]
            if name not in values:
                if parameter["required"]:
                    raise RequestValidationError(
                        [{"loc": location, "type": "missing", "msg": "Field required"}]
                    )
                values[name] = parameter["default"]
        for name, validate in (getattr(route, "input_validators", None) or {}).items():
            parameter = parameters[name]
            location = [
                _LOCATIONS.get(parameter["source"], parameter["source"]),
                parameter.get("alias", name),
            ]
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
        # These stacks are framework-owned. The private callback deque is part
        # of our qualified CPython 3.12/3.13 compatibility surface; nonempty
        # stacks still use the shielded cleanup Task and its original context.
        if not stack._exit_callbacks:
            return False

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

    def _resolver(
        self, route, arguments, request, background, stack, future, principal=None
    ):
        return DependencyResolver(
            route,
            arguments,
            state=(getattr(route, "app", None) or self.app).state,
            request=request,
            background=background,
            principal=principal,
            invoke=lambda plan, kwargs, target_stack: self._execute_plan(
                plan, kwargs, target_stack, future
            ),
            request_stack=stack,
            app_stack=self._app_dependency_stack,
            app_cache=self._app_dependency_cache,
        )

    async def _prepare_app_dependencies(self) -> None:
        for route in self._compiled:
            if not getattr(route, "dependencies", ()):
                continue
            resolver = self._resolver(
                route, {}, None, None, self._app_dependency_stack, None
            )
            for plan in route.dependencies:
                if plan.scope == "app":
                    await resolver.resolve(plan.id)

    async def _execute_route(
        self,
        route,
        arguments,
        request,
        background,
        future,
        direct=None,
        principal=None,
    ):
        stack = AsyncExitStack()
        try:
            plan = getattr(route, "plan", None)
            if plan is not None and direct is not None:
                result = await self._execute_plan(
                    plan, {name: arguments[key] for name, key in direct}, stack, future
                )
            elif plan is None:
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
                    route, arguments, request, background, stack, future, principal
                )
                for binding in getattr(route, "before", ()):
                    await resolver.binding(binding)
                result = await self._execute_plan(
                    plan, await resolver.arguments_for(plan), stack, future
                )
            # A response may carry the request's own injected BackgroundTasks;
            # merge only other collections so no task runs twice.
            if (
                isinstance(result, Response)
                and result.background is not None
                and result.background is not background
            ):
                background._tasks.extend(background_tasks(result))
            if isinstance(result, StreamingResponse):
                return await self._stream_response(
                    result, request, future, stack, background
                )
            response = self._plain_response(route, result)
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

    async def _run_tasks(self, tasks, future) -> None:
        for task in tasks:
            future._raise_if_cancel_requested()
            if task.kind == "async":
                await task.call(*task.args, **task.kwargs)
            else:
                result = await self._call_sync(
                    task.call, task.args, task.kwargs, future
                )
                await self._await_result(result, future)

    async def _stream_response(
        self, result, request, future, stack=None, background=None
    ):
        """Start a streaming response, which takes ownership of the exit stack
        and background tasks; the lean path creates empty ones only here.
        """
        if stack is None:
            stack = AsyncExitStack()
            background = BackgroundTasks()
            background._tasks.extend(background_tasks(result))
        # A deadline that passed while a sync handler ran was declined;
        # honour it now rather than starting a stream nobody will read.
        try:
            future._raise_if_cancel_requested()
        except asyncio.CancelledError:
            await _close_iterator(self, result.body_iterator)
            raise
        # Dependencies and background tasks outlive the handler: the
        # stream owns the exit stack and finishes it after the last chunk.
        stream = _ResponseStream(
            self, result, stack, background, future._context, request
        )
        self._streams.add(stream)
        return (result.status_code, stream, result.headers.raw, "stream")

    def _plain_response(self, route, result):
        """Turn a non-streaming handler result into the native envelope.

        Synchronous, so the common case costs no extra coroutine.
        """
        if isinstance(result, Response):
            response = result.to_native(max_bytes=self.max_response_size)
        else:
            adapter = getattr(route, "response_adapter", None)
            if adapter is not None:
                options = route.response_model_options
                if options is None:
                    result = adapter.dump_python(
                        adapter.validate_python(result),
                        mode="json",
                        by_alias=True,
                        warnings="error",
                    )
                else:
                    result = adapter.dump_python(
                        adapter.validate_python(result),
                        mode="json",
                        warnings="error",
                        **options,
                    )
            response = self._json_response(route.declaration["status_code"], result)
        return response

    def _json_response(self, status: int, result):
        # Exact built-in JSON values are encoded natively in one pass, with the
        # same depth and byte limits. Anything else (subclasses, tuples, dates,
        # models, values over a limit) takes the original normalizing path so
        # its conversion and error behaviour stay unchanged.
        if _encode_plain_json is not None:
            encoded = _encode_plain_json(result, self.max_response_size)
            if encoded is not None:
                return (status, encoded, (), "json_bytes")
        return (status, jsonable(result, max_bytes=self.max_response_size))

    async def _invoke(
        self,
        route_id: int,
        arguments: dict[str, Any],
        future: _InvocationFuture,
        request: Request | None = None,
    ):
        route = self._compiled[route_id]
        if route.handler is None:
            return 200, {"ok": True}
        if route.declaration["kind"] == "websocket":
            return await self._invoke_websocket(route, arguments, future)
        if request is None and self._route_middleware[route_id]:
            return await self._invoke_through_middleware(route_id, arguments, future)
        metadata = claims = trace = None
        if (
            "__bessemer_request__" in arguments
            or "__bessemer_principal__" in arguments
            or "__bessemer_trace__" in arguments
        ):
            metadata = arguments.get("__bessemer_request__")
            claims = arguments.get("__bessemer_principal__")
            trace = arguments.get("__bessemer_trace__")
            arguments = {
                key: value
                for key, value in arguments.items()
                if key not in _NATIVE_ARGUMENTS
            }
        trace_token = None
        if request is not None:
            # The middleware chain already attached the request's trace.
            trace = None
        elif metadata:
            request = Request(metadata, app=self._owners[route_id])
        else:
            method, path = self._route_identity[route_id]
            request = Request._bare(method, path, self._owners[route_id])
        if trace is not None:
            request.traceparent = trace["traceparent"]
            trace_token = otel.attach(trace)
        token = current_request.set(request)
        try:
            if self._needs_validation[route_id] or arguments:
                arguments = self._validate_arguments(
                    route, arguments, self._route_parameters[route_id]
                )
            plan = self._lean_plans[route_id]
            if plan is not None and claims is None:
                future._raise_if_cancel_requested()
                result = await plan.call(
                    **{
                        name: arguments[key]
                        for name, key in self._direct_bindings[route_id]
                    }
                )
                if isinstance(result, Response):
                    if isinstance(result, StreamingResponse):
                        return await self._stream_response(result, request, future)
                    if result.background is not None:
                        response = self._plain_response(route, result)
                        await self._run_tasks(background_tasks(result), future)
                        return response
                return self._plain_response(route, result)
            principal = None if claims is None else Principal(claims)
            return await self._execute_route(
                route,
                arguments,
                request,
                BackgroundTasks(),
                future,
                self._direct_bindings[route_id],
                principal,
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
            otel.detach(trace_token)

    async def _invoke_through_middleware(self, route_id, arguments, future):
        """Run the route inside the HTTP middleware chain.

        Every stream that crosses a chain boundary, the route's or one a
        middleware created, is started and wrapped as a ``_CalledResponse``
        recorded in ``produced``. Whatever the outcome, each recorded stream
        that is not the final response is closed before the request ends.
        """
        metadata = arguments.get("__bessemer_request__")
        if metadata:
            request = Request(metadata, app=self._owners[route_id])
        else:
            method, path = self._route_identity[route_id]
            request = Request._bare(method, path, self._owners[route_id])
        trace = arguments.get("__bessemer_trace__")
        trace_token = None
        if trace is not None:
            request.traceparent = trace["traceparent"]
            trace_token = otel.attach(trace)
        route = self._compiled[route_id]
        middleware = self._route_middleware[route_id]
        produced: list[_CalledResponse] = []

        async def adopt(response):
            if not isinstance(response, Response):
                raise TypeError("HTTP middleware must return a Response")
            if isinstance(response, StreamingResponse):
                envelope = await self._stream_response(response, request, future)
                response = _CalledResponse(envelope)
                produced.append(response)
            return response

        def chain(index):
            async def call_next(_request=None):
                if index == len(middleware):
                    envelope = await self._invoke(
                        route_id, arguments, future, request=request
                    )
                    response = _CalledResponse(envelope)
                    produced.append(response)
                    return response
                future._raise_if_cancel_requested()
                return await adopt(await middleware[index](request, chain(index + 1)))

            return call_next

        token = current_request.set(request)
        try:
            response = await chain(0)()
            envelope = response.to_native(max_bytes=self.max_response_size)
            # Tasks a middleware attached, to its own response or to the one
            # call_next returned. A call_next response never carries the
            # route's tasks (those already ran), so nothing runs twice.
            if response.background is not None:
                await self._run_tasks(background_tasks(response), future)
        except asyncio.CancelledError:
            await self._close_unreturned(produced, None)
            raise
        except HTTPException as error:
            await self._close_unreturned(produced, None)
            envelope = (
                error.status_code,
                jsonable({"detail": error.detail}, max_bytes=self.max_response_size),
                error.headers.raw,
                "json",
            )
        except BaseException as error:  # noqa: BLE001 -- Middleware must not terminate the shared worker loop.
            await self._close_unreturned(produced, None)
            _LOG.error(
                "Unhandled %s in HTTP middleware for %s %s",
                type(error).__name__,
                route.declaration["method"],
                route.declaration["path"],
            )
            envelope = 500, {"detail": "Internal Server Error"}
        else:
            await self._close_unreturned(produced, response)
        finally:
            current_request.reset(token)
            otel.detach(trace_token)
        return envelope

    async def _close_unreturned(self, produced, returned) -> None:
        """Close the streams of route responses that middleware discarded.

        Each close is shielded and awaited to completion even if the request
        is cancelled meanwhile, so a discarded stream never outlives the
        request's capacity accounting. If cancellation arrives, the returned
        stream will never reach the runtime either, so it is closed too before
        the cancellation is re-raised.
        """
        cancelled = False
        for response in produced:
            if response is not returned and response._kind == "stream":
                cancelled |= await self._close_stream_shielded(response._native_body)
        if cancelled:
            if returned in produced and returned._kind == "stream":
                await self._close_stream_shielded(returned._native_body)
            raise asyncio.CancelledError

    @staticmethod
    async def _close_stream_shielded(stream) -> bool:
        """Close a stream to completion; report whether cancellation arrived."""
        # Close in the stream's own context, as every other step of the
        # stream runs, so iterator finalizers see their ContextVars.
        task = asyncio.get_running_loop().create_task(
            stream._close(), context=stream._context
        )
        cancelled = False
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                if not task.done():
                    cancelled = True
        if not task.cancelled() and task.exception() is not None:
            _LOG.error(
                "Unhandled %s closing a discarded streaming response",
                type(task.exception()).__name__,
            )
        return cancelled

    async def _invoke_websocket(self, route, arguments, future):
        metadata = arguments["__bessemer_request__"]
        request = Request(metadata, app=getattr(route, "app", None) or self.app)
        claims = metadata.get("claims")
        principal = None if claims is None else Principal(claims)
        websocket = WebSocket(arguments["__bessemer_websocket__"], request, self._loop)
        background = BackgroundTasks()
        token = current_request.set(request)
        stack = AsyncExitStack()
        failure: BaseException | None = None
        try:
            resolver = self._resolver(
                route, {}, request, background, stack, future, principal
            )
            resolver.websocket = websocket
            for binding in route.before:
                await resolver.binding(binding)
            await self._execute_plan(
                route.plan, await resolver.arguments_for(route.plan), stack, future
            )
            for task in background._tasks:
                if task.kind == "async":
                    await task.call(*task.args, **task.kwargs)
                else:
                    result = await self._call_sync(
                        task.call, task.args, task.kwargs, None
                    )
                    await self._await_result(result, None)
        except WebSocketDisconnect as error:
            # The peer left before the handler finished; dependencies unwind
            # through their error path. A handler that catches the disconnect
            # itself completes normally.
            failure = error
        except asyncio.CancelledError as error:
            # Worker shutdown: tell the peer the server is going away.
            failure = error
            if websocket.accepted:
                websocket._channel.close(1001, "")
            raise
        except HTTPException as error:
            # Before accept this denies the handshake with 403. Dependencies
            # still unwind through their error path, as for HTTP routes.
            failure = error
            websocket._channel.close(1008, "")
        except WebSocketException as error:
            # FastAPI's explicit close; before accept it denies the handshake.
            failure = error
            websocket._channel.close(error.code, error.reason)
        except BaseException as error:  # noqa: BLE001 -- A session must not terminate the shared loop.
            failure = error
            _LOG.error(
                "Unhandled %s in WebSocket %s",
                type(error).__name__,
                route.declaration["path"],
            )
            websocket._channel.close(1011, "")
        finally:
            try:
                await self._finish_exit(stack, failure, context=future._context)
            except BaseException as error:  # noqa: BLE001 -- A session must not terminate the shared loop.
                # A failed dependency finalizer must not look like a clean
                # close to the peer.
                _LOG.error(
                    "Unhandled %s in WebSocket %s dependency cleanup",
                    type(error).__name__,
                    route.declaration["path"],
                )
                websocket._channel.close(1011, "")
            finally:
                current_request.reset(token)
        return 200, None

    async def _handle_exception(self, request, error, future):
        # The handlers of the App that owns the route (a mounted app's own).
        handlers = getattr(
            getattr(request, "app", None) or self.app, "_exception_handlers", {}
        )
        plan = None
        if isinstance(error, HTTPException):
            # A handler registered for the status code wins, as in Starlette.
            plan = handlers.get(error.status_code)
        if plan is None:
            plan = next(
                (
                    handlers[error_type]
                    for error_type in type(error).__mro__
                    if error_type in handlers
                ),
                None,
            )
        if plan is None and not isinstance(
            error, (HTTPException, RequestValidationError)
        ):
            # Starlette sends unhandled errors to a 500 handler.
            plan = handlers.get(500)
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
            envelope = response.to_native(max_bytes=self.max_response_size)
            if response.background is not None:
                await self._run_tasks(background_tasks(response), future)
            return envelope
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
        streams = list(self._streams)
        if streams:
            # Open streams end at shutdown; their cleanup still runs fully.
            # Each close runs in its stream's own context, as every other step
            # of the stream does, so iterator finalizers see their ContextVars.
            await asyncio.gather(
                *(
                    asyncio.get_running_loop().create_task(
                        stream._close(), context=stream._context
                    )
                    for stream in streams
                ),
                return_exceptions=True,
            )
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
