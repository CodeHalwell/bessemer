"""One persistent asyncio worker, including lifespan and cancellation cleanup."""

from __future__ import annotations

import asyncio
import concurrent.futures
import contextvars
import inspect
import logging
import os
import signal
import sys
import threading
import time
from collections.abc import Mapping
from contextlib import (
    AsyncExitStack,
    _AsyncGeneratorContextManager,  # What asynccontextmanager builds.
    contextmanager,
    suppress,
)
from itertools import islice
from types import AsyncGeneratorType, SimpleNamespace
from typing import Any, Self

from . import otel
from ._cpu import cpu_budget
from ._model_plan import model_plan
from ._threads import ContextThreadPoolExecutor
from .application import App, HTTPException, _positive_float
from .auth import Principal
from .config import resolve_event_loop, validate_choice, validate_loops
from .dependencies import DependencyResolver
from .request import (
    BackgroundTasks,
    Request,
    RequestValidationError,
    ResponseValidationError,
    current_request,
)
from .responses import (
    Headers,
    Response,
    StreamingResponse,
    _AsyncEvents,
    _event_bytes,
    background_tasks,
    jsonable,
    validate_status,
)
from .websockets import WebSocket, WebSocketDisconnect, WebSocketException

try:
    from ._native import _encode_model_json, _encode_model_list_json, _encode_plain_json
except ImportError:  # Source-only use without the extension.
    _encode_model_json = _encode_model_list_json = _encode_plain_json = None
try:
    from ._native import Bridge as _NativeBridge
    from ._native import Invoker as _Invoker
except ImportError:  # Source-only use without the extension.
    _NativeBridge = _Invoker = None
try:
    from ._native import Switch as _Switch
except ImportError:  # Source-only use without the extension.

    class _Switch:
        """A boolean the native invoker reads without an attribute lookup."""

        __slots__ = ("on",)

        def __init__(self, on: bool = False) -> None:
            self.on = on


def _default_model_validate(model) -> bool:
    """Whether ``model`` uses BaseModel's own ``model_validate``."""
    try:
        from pydantic import BaseModel
    except ImportError:
        return False
    method = getattr(model.model_validate, "__func__", None)
    return method is BaseModel.model_validate.__func__


# Form fields and files are reported under "body", as FastAPI does.
_LOCATIONS = {"form": "body", "file": "body"}
# Arguments the native server adds for the worker, never for the handler.
_NATIVE_ARGUMENTS = frozenset(
    {"__bessemer_request__", "__bessemer_principal__", "__bessemer_trace__"}
)
# Scalar sources the native invoker passes to handlers unchanged.
_NATIVE_SOURCES = frozenset({"path", "query", "header", "cookie"})
_LOG = logging.getLogger("bessemer")
# Extra loops mark themselves alive this often; the supervisor heartbeat is
# withheld while any of them has missed _LANE_STALL seconds of ticks.
_LANE_TICK = 0.5
_LANE_STALL = 1.5


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
        # Dependency cleanup running inline in the request's own task: an
        # accepted cancellation is deferred until it ends (see _end_cleanup).
        self._cleanup_running = False
        self._cancel_deferred = False
        self._disconnected = False
        self._watching_disconnect = False
        self._disconnect_waiter: asyncio.Future | None = None
        self._context = context

    def _watch_disconnect(self) -> bool:
        with self._control:
            self._watching_disconnect = True
            return self._disconnected

    def _wait_disconnect(self) -> asyncio.Future | None:
        with self._control:
            self._watching_disconnect = True
            if self._disconnected:
                return None
            if self._disconnect_waiter is None:
                self._disconnect_waiter = self._loop.create_future()
            return self._disconnect_waiter

    @staticmethod
    def _resolve_disconnect(waiter: asyncio.Future) -> None:
        if not waiter.done():
            waiter.set_result(None)

    def _disconnect(self) -> bool:
        with self._control:
            self._disconnected = True
            watching = self._watching_disconnect
            waiter = self._disconnect_waiter
            self._disconnect_waiter = None
        if waiter is not None:
            self._loop.call_soon_threadsafe(self._resolve_disconnect, waiter)
        if not watching:
            return self.cancel()
        return True

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
            if should_cancel and self._cleanup_running:
                # Delivered by _end_cleanup once the cleanup has finished.
                self._cancel_deferred = True
                should_cancel = False
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

    def _begin_cleanup(self) -> bool:
        """Start dependency cleanup in the request's own task; False if it
        cannot (cleanup is already running), and the caller shields it."""
        with self._control:
            if self._cleanup_running:
                return False
            self._cleanup_running = True
            return True

    def _end_cleanup(self) -> bool:
        """End inline cleanup; True if a cancellation arrived meanwhile,
        which the caller then raises, as the shielded cleanup would have."""
        with self._control:
            self._cleanup_running = False
            deferred, self._cancel_deferred = self._cancel_deferred, False
            if deferred:
                self._cancel_delivered = True
            return deferred

    def _raise_if_cancel_requested(self) -> None:
        # A single attribute read is atomic, with or without the GIL; the lock
        # only orders the multi-field updates above.
        if self._cancel_requested:
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


class _SyncFuture(asyncio.Future):
    """A pool call retains its waiter until the thread has actually finished."""

    def cancel(self, msg=None):
        # Task.cancel still records cancellation on the waiting Task. Declining
        # to cancel this future postpones delivery until the pool reports back,
        # without a separate shield Future and its completion callbacks.
        return False


_END = object()
_SYNC_STREAM_ITEMS = 64
_SYNC_STREAM_QUANTUM = 0.001


_JSON_CONTENT_TYPE = ("Content-Type", "application/json")
# Where a flattened dependency step's argument comes from.
_FROM_INPUT = "input"
_FROM_REQUEST = "request"
_FROM_BACKGROUND = "background_tasks"
_FROM_STATE = "state"
_FROM_STEP = "step"


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
        if not headers and (kind == "json" or kind == "json_bytes"):
            # The runtime would add this; middleware should see it too.
            self.headers = Headers._validated([_JSON_CONTENT_TYPE])
        else:
            self.headers = Headers(headers)
            if kind in ("json", "json_bytes") and "content-type" not in self.headers:
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


class _Signal:
    """``asyncio.Event`` for one stream on its own loop: set, clear,
    ``is_set`` and ``wait`` with the same semantics, at a fraction of the cost
    to create (a stream makes three)."""

    __slots__ = ("_value", "_waiters")

    def __init__(self, value: bool = False) -> None:
        self._value = value
        self._waiters: list[asyncio.Future] | None = None

    def is_set(self) -> bool:
        return self._value

    def set(self) -> None:
        if not self._value:
            self._value = True
            waiters, self._waiters = self._waiters, None
            for waiter in waiters or ():
                if not waiter.done():
                    waiter.set_result(True)

    def clear(self) -> None:
        self._value = False

    async def wait(self) -> bool:
        if self._value:
            return True
        waiter = asyncio.get_running_loop().create_future()
        if self._waiters is None:
            self._waiters = []
        self._waiters.append(waiter)
        try:
            await waiter
            return True
        finally:
            if self._waiters is not None and waiter in self._waiters:
                self._waiters.remove(waiter)


def _middleware_level(function, call_next, request, future, adopt):
    """The ``call_next`` that runs HTTP middleware ``function`` around the
    rest of the chain, ``call_next``."""

    async def call(_request=None):
        future._raise_if_cancel_requested()
        return await adopt(await function(request, call_next))

    return call


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
        # Every step runs on the loop that started the stream, which is one of
        # the worker's loops when it runs several.
        self._loop = asyncio.get_running_loop()
        self._request = request
        self._closing = False
        content = response.body_iterator
        self._async = hasattr(content, "__aiter__")
        self._iterator = content.__aiter__() if self._async else iter(content)
        # A sync iterator runs in executor threads; every step and its close
        # share one context so ContextVar changes and tokens survive across
        # chunks, as they do for generator dependencies.
        self._sync_context: contextvars.Context | None = None
        if not self._async:
            self._sync_lock = threading.Lock()
            self._sync_pending: list[bytes] = []
            self._sync_reserved = 0
            self._sync_wake = False
            self._sync_stop = threading.Event()
        self._stack = stack
        self._background = background
        self._context = context
        self._limit = min(65_536, worker.max_response_size)
        self._buffer: list[bytes] = []
        self._buffered = 0
        self._ready = _Signal()
        self._space = _Signal(True)
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
        self._done = _Signal()
        # A read from the loop's own thread waiting for the producer's next
        # chunk (see ``next``).
        self._reader: asyncio.Future | None = None

    def _start(self, coroutine) -> asyncio.Task:
        """A task for one of this stream's coroutines, in its context, started
        eagerly (up to its first suspension) unless the loop has a task
        factory, which then creates it as for any other task."""
        loop = self._loop
        if loop.get_task_factory() is None:
            return asyncio.Task(
                coroutine, loop=loop, context=self._context, eager_start=True
            )
        return loop.create_task(coroutine, context=self._context)

    def _submit(self, coroutine_function, *, eager=False):
        if asyncio._get_running_loop() is self._loop:
            # Called from a runtime task on the loop's own thread (the Rust
            # loop serving its connections): the step's task is the future,
            # without a concurrent future's locks or a cross-thread wake-up.
            # A read starts eagerly, so a chunk already buffered (or an
            # iterator that never suspends) is answered before this returns.
            if eager:
                task = self._start(coroutine_function())
            else:
                task = self._loop.create_task(
                    coroutine_function(), context=self._context
                )
            if task.done():
                self._step_done(task)
            else:
                self._worker._stream_tasks.add(task)
                task.add_done_callback(self._step_done)
            return task
        result = concurrent.futures.Future()

        def schedule() -> None:
            task = self._loop.create_task(coroutine_function(), context=self._context)
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
            self._loop.call_soon_threadsafe(schedule)
        except RuntimeError as error:
            result.set_exception(error)
        return result

    def _step_done(self, task: asyncio.Task) -> None:
        self._worker._stream_tasks.discard(task)
        # Retrieve the outcome so a failed close (whose result nothing reads)
        # is not reported as never retrieved, as the concurrent future did.
        if not task.cancelled():
            task.exception()

    def next(self):
        if asyncio._get_running_loop() is not self._loop:
            return self._submit(self._read)
        # From the loop's own thread (the Rust loop serving its connections),
        # a read while the producer runs ahead needs no task: chunks already
        # buffered are returned as bytes, and otherwise a bare future waits
        # for the producer to hand over the next one. The first read, the end
        # of the stream and a closing stream take the full path in ``_next``.
        if self._producer is None and not (self._closing or self._finished):
            # The first pull comes from the runtime, outside the stream's
            # context, so the producer can start eagerly here: chunks the
            # iterator yields without suspending are buffered before the read
            # below, which then needs no wake-up from the producer.
            self._producer = self._start(self._produce())
        if (
            self._ended
            and self._reader is None
            and self._error is None
            and not (self._closing or self._finished)
            and not self._background._tasks
            and not self._stack._exit_callbacks
        ):
            # The iterator has ended and nothing runs after it (no background
            # tasks or dependency cleanup): answer the last batch, with the
            # end, here instead of in a read task.
            if self._buffer:
                data = self._take()
                if self._buffer:
                    return data
                self._finish_quietly()
                return (data,)
            self._finish_quietly()
            return None
        if (
            self._producer is None
            or self._ended
            or self._closing
            or self._finished
            or self._reader is not None
        ):
            return self._submit(self._read, eager=True)
        if self._buffer:
            return self._take()
        reader = self._reader = self._loop.create_future()
        return reader

    def _finish_quietly(self) -> None:
        """``_finish(None)`` for a stream with nothing to run after its last
        chunk: no iterator to close, background tasks or exit callbacks."""
        self._finished = True
        self._discard_buffer()
        self._worker._streams.discard(self)
        self._done.set()

    def _take(self) -> bytes:
        """Remove buffered chunks, up to the size limit, as one batch."""
        # Drain whole chunks up to the size limit; the first always fits.
        buffer = self._buffer
        if len(buffer) == 1:
            data = buffer.pop()
            size = len(data)
        else:
            maximum = self._worker.max_response_size
            taken, size = 0, 0
            for chunk in buffer:
                if taken and size + len(chunk) > maximum:
                    break
                taken += 1
                size += len(chunk)
            data = buffer[0] if taken == 1 else b"".join(buffer[:taken])
            del buffer[:taken]
        self._buffered -= size
        if not self._async:
            with self._sync_lock:
                self._sync_reserved -= size
        if not buffer and not self._ended:
            self._ready.clear()
        if self._buffered < self._limit:
            self._space.set()
        return data

    def _hand_over(self) -> None:
        """Settle a waiting bare read: with the next chunk, or, at the end of
        the stream, with what the full read path returns."""
        reader, self._reader = self._reader, None
        if reader.done():
            return
        if self._buffer and not self._ended:
            reader.set_result(self._take())
            return
        task = self._loop.create_task(self._read(), context=self._context)
        self._worker._stream_tasks.add(task)

        def settle(completed: asyncio.Task) -> None:
            self._step_done(completed)
            if reader.done():
                return
            if completed.cancelled():
                reader.cancel()
            elif completed.exception() is not None:
                reader.set_exception(completed.exception())
            else:
                reader.set_result(completed.result())

        task.add_done_callback(settle)

    def close(self):
        self._request._disconnected = True
        return self._submit(self._close)

    def _chunk_bytes(self, chunk):
        if type(chunk) is bytes:
            # The usual chunk: no failing isinstance checks, each of which
            # also looks up the chunk's __class__.
            if len(chunk) > self._worker.max_response_size:
                raise ValueError("a streaming response chunk exceeds the size limit")
            return chunk
        if isinstance(chunk, str):
            chunk = chunk.encode("utf-8")
        elif isinstance(chunk, (bytearray, memoryview)):
            chunk = bytes(chunk)
        elif not isinstance(chunk, bytes):
            raise TypeError("streaming response chunks must be bytes or str")
        if len(chunk) > self._worker.max_response_size:
            raise ValueError("a streaming response chunk exceeds the size limit")
        return chunk

    def _append_chunk(self, chunk):
        self._buffer.append(chunk)
        self._buffered += len(chunk)
        self._ready.set()
        if self._buffered >= self._limit:
            self._space.clear()
        if self._reader is not None:
            self._hand_over()

    def _queue_sync(self, chunk):
        with self._sync_lock:
            self._sync_pending.append(chunk)
            self._sync_reserved += len(chunk)
            if self._sync_wake:
                return
            self._sync_wake = True
        self._loop.call_soon_threadsafe(self._drain_sync, context=contextvars.Context())

    def _drain_sync(self):
        with self._sync_lock:
            chunks, self._sync_pending = self._sync_pending, []
            self._sync_wake = False
        if self._closing or self._finished:
            return
        for chunk in chunks:
            self._append_chunk(chunk)

    def _discard_buffer(self):
        self._buffer.clear()
        if not self._async:
            with self._sync_lock:
                self._sync_pending.clear()
                self._sync_reserved = 0

    def _sync_batch(self, maximum_items, available):
        deadline = time.monotonic() + _SYNC_STREAM_QUANTUM
        produced = 0
        for _ in range(maximum_items):
            if self._sync_stop.is_set():
                break
            chunk = next(self._iterator, _END)
            if chunk is _END:
                return True
            chunk = self._chunk_bytes(chunk)
            if self._sync_stop.is_set():
                break
            if chunk:
                self._queue_sync(chunk)
                produced += len(chunk)
            if produced >= available or time.monotonic() >= deadline:
                break
        return False

    async def _produce_sync(self):
        # Every batch, the first included, takes up to _SYNC_STREAM_ITEMS
        # steps: each chunk is queued to the loop as soon as it is produced,
        # so a larger first batch does not hold the first chunk back.
        while True:
            with self._sync_lock:
                available = self._limit - self._sync_reserved
            if available <= 0:
                self._space.clear()
                await self._space.wait()
                continue
            ended = await self._worker._call_sync(
                self._sync_batch,
                (_SYNC_STREAM_ITEMS, available),
                {},
                None,
                execution_context=self._sync_context,
            )
            if ended:
                return
            await asyncio.sleep(0)

    async def _produce_async(self):
        space = self._space
        iterator = self._iterator
        convert = None
        if type(iterator) is _AsyncEvents and type(iterator._iterator) is (
            AsyncGeneratorType
        ):
            # EventSourceResponse over an async generator: step the source
            # itself and format each item here, without the wrapper's
            # coroutine per event; closing still goes through the wrapper.
            iterator, convert = iterator._iterator, _event_bytes
        if type(iterator) is AsyncGeneratorType:
            # An async generator's __aiter__ is itself, so ``async for`` steps
            # the same iterator without anext()'s default-value wrapper. The
            # buffer's space is awaited before each step, as below.
            if not space.is_set():
                await space.wait()
            async for chunk in iterator:
                if convert is not None:
                    chunk = convert(chunk)
                chunk = self._chunk_bytes(chunk)
                if chunk:
                    self._append_chunk(chunk)
                else:
                    await asyncio.sleep(0)
                if not space.is_set():
                    await space.wait()
            return
        # Other async iterators are stepped through their type's __anext__,
        # as anext() does, without its default-value wrapper.
        step = type(iterator).__anext__
        while True:
            if not space.is_set():
                await space.wait()
            try:
                chunk = await step(iterator)
            except StopAsyncIteration:
                return
            chunk = self._chunk_bytes(chunk)
            if chunk:
                self._append_chunk(chunk)
            else:
                await asyncio.sleep(0)

    async def _produce(self) -> None:
        # The handler's request binding was reset when _invoke returned; the
        # stream still belongs to that request.
        current_request.set(self._request)
        if not self._async and self._sync_context is None:
            self._sync_context = contextvars.copy_context()
        try:
            if self._async:
                await self._produce_async()
            else:
                await self._produce_sync()
        except asyncio.CancelledError:
            raise
        except BaseException as error:  # noqa: BLE001 -- A stream must not terminate the shared loop.
            _LOG.error("Unhandled %s in a streaming response", type(error).__name__)
            self._error = error
        self._ended = True
        self._ready.set()
        if self._reader is not None:
            self._hand_over()

    def _read(self):
        """A read for the native runtime, which takes the end with the last
        batch."""
        return self._next(last=True)

    async def _next(self, *, last: bool = False) -> bytes | tuple[bytes] | None:
        """The next batch of chunks, or ``None`` at the end. With ``last``, a
        last batch after which the stream has already finished comes as a
        1-tuple."""
        if self._finished:
            return None
        if self._producer is None:
            # Not eager: this read runs in the stream's context, which a task
            # started inside it cannot enter.
            self._producer = self._loop.create_task(
                self._produce(), context=self._context
            )
        await self._ready.wait()
        if self._closing:
            # close() owns cleanup; it woke this pending read.
            await self._done.wait()
            return None
        if self._buffer:
            data = self._take()
            if (
                last
                and not self._buffer
                and self._ended
                and self._error is None
                and not self._background._tasks
                and not self._stack._exit_callbacks
            ):
                # Nothing runs after the last chunk, so finish now and
                # report the end with it, saving the runtime a round trip.
                await self._finish(None)
                return (data,)
            return data
        await self._finish(self._error)
        if self._error is not None or self._cleanup_failed:
            raise RuntimeError("streaming response failed") from None
        return None

    async def _close(self) -> None:
        self._request._disconnected = True
        if self._closing:
            # A close is already running (for example a disconnect, then
            # worker shutdown): wait for it rather than cancelling the
            # producer again, which would interrupt the iterator's cleanup.
            await self._done.wait()
            return
        self._closing = True
        if not self._async:
            self._sync_stop.set()
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
        # A bare read waiting on the cancelled producer ends, as a pending
        # ``_next`` does, once cleanup has finished.
        reader, self._reader = self._reader, None
        if reader is not None and not reader.done():
            reader.set_result(None)

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
        self._discard_buffer()
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


class _AfterResponse:
    """Background work and dependency cleanup owned by the transport."""

    def __init__(self, worker, request, tasks, stack, context, stream=None):
        self._worker = worker
        self._loop = asyncio.get_running_loop()
        self._snapshot = contextvars.copy_context()
        self._context = context if stack._exit_callbacks else self._snapshot
        self._stack_context = context
        self._request = request
        self._tasks = tasks
        self._stack = stack
        self._stream = stream
        self._task = None
        self._entered = False
        self._aborted = False
        worker._after_responses.add(self)

    def start(self, sent):
        if self._task is None:
            # The request Task has finished. Restore values that its finally
            # blocks reset, retaining the original Context so generator tokens
            # acquired in it can still be reset during dependency cleanup.
            if self._context is not self._snapshot:
                for variable, value in self._snapshot.items():
                    self._context.run(variable.set, value)
            self._task = self._loop.create_task(self._run(sent), context=self._context)
        return self._task

    async def close(self):
        self._aborted = True
        task = self.start(False)
        if self._entered and not task.done():
            task.cancel()
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                continue
        if not task.cancelled():
            task.exception()

    async def _run(self, sent):
        self._entered = True
        token = current_request.set(self._request)
        error = None if sent and not self._aborted else asyncio.CancelledError()
        try:
            if self._stream is not None:
                stream = self._stream
                if not stream._done.is_set():
                    await stream._close()
                if stream._error is not None or stream._cleanup_failed:
                    error = RuntimeError("streaming response failed")
            if error is None:
                await self._worker._run_tasks(self._tasks, None)
        except BaseException as failure:  # noqa: BLE001 -- response is already sent
            error = failure
            if not isinstance(failure, asyncio.CancelledError):
                _LOG.error("Unhandled %s after a response", type(failure).__name__)
        finally:
            try:
                await self._worker._finish_exit(
                    self._stack, error, context=self._stack_context
                )
            except BaseException as failure:  # noqa: BLE001 -- cleanup must finish
                _LOG.error(
                    "Unhandled %s in post-response cleanup", type(failure).__name__
                )
            current_request.reset(token)
            self._worker._after_responses.discard(self)


def _resolve_event_loop(event_loop: str, loops: int = 1) -> str:
    """The event loop the worker will run, after the platform fallback.

    ``"asyncio"`` is the standard library's loop. ``"bessemer"`` is the
    loop whose scheduler and reactor are in Rust (``bessemer._loop``). It is
    built for Linux (epoll) and macOS (kqueue); elsewhere the worker logs a
    warning and runs asyncio, so one configuration works on every platform.
    ``"auto"``, the default, runs the Rust loop on Linux and macOS when it
    imports and ``loops`` is 1, and asyncio otherwise, without a warning
    because nothing was asked for.
    """
    validate_choice("event_loop", event_loop)
    resolved = resolve_event_loop(event_loop, loops)
    if event_loop == "bessemer" and resolved != "bessemer":
        _LOG.warning(
            "event_loop 'bessemer' is only available on Linux and macOS; using asyncio"
        )
    return resolved


def _resolve_io_backend(io_backend: str, serves_connections: bool) -> str:
    """The socket I/O backend the native server will use.

    ``"io_uring"`` applies only when the worker serves connections on the
    Rust loop (``event_loop="bessemer"``, or ``"auto"`` where it resolves to
    that, with one loop). Otherwise the worker
    logs a warning and serves on epoll. The native server itself falls back
    to epoll, with a warning, when the kernel or a seccomp profile refuses
    io_uring.
    """
    validate_choice("io_backend", io_backend)
    if io_backend == "io_uring" and not serves_connections:
        _LOG.warning(
            "io_backend 'io_uring' needs event_loop='bessemer' with a single "
            "loop; serving on epoll"
        )
        return "epoll"
    return io_backend


def _loop_factory(event_loop: str):
    """Return the constructor for a resolved event loop name."""
    if event_loop == "bessemer":
        from ._loop import Loop

        return Loop
    if sys.platform == "win32":
        # The worker watches the native bridge's wake socket with
        # add_reader, which the default proactor loop does not support.
        return asyncio.SelectorEventLoop
    return asyncio.new_event_loop


def _sync_worker_count(app, configured):
    if configured is not None:
        if type(configured) is not int or not 1 <= configured <= 256:
            raise ValueError("sync_workers must be an integer between 1 and 256")
        return configured
    if getattr(sys, "_is_gil_enabled", lambda: True)():
        return 8
    return min(256, max(1, cpu_budget() // getattr(app, "_worker_processes", 1)))


class _Lane:
    """One event loop serving requests from its own native queue.

    The main lane owns lifespan and application resources. Further lanes run
    in their own threads; on free-threaded Python they run in parallel.
    """

    def __init__(self, worker: Worker, loop, tasks, invocations) -> None:
        self.worker = worker
        self.loop = loop
        self.tasks: dict[asyncio.Task, _InvocationControl] = tasks
        self.invocations: dict[int, _InvocationControl] = invocations
        self.bridge = None
        self.thread: threading.Thread | None = None
        self.ticked = time.monotonic()
        self.pending_submissions = 0

    def submit(self, route_id, arguments):
        future = _InvocationFuture(self.loop)
        self.loop.call_soon_threadsafe(
            self._start_submission, route_id, arguments, future
        )
        return future

    def _start_submission(self, route_id, arguments, future):
        try:
            if not self.worker._accepting:
                raise RuntimeError("application worker is stopping")
            coroutine = self.worker._invoke(route_id, arguments, future)
            try:
                task = self.loop.create_task(coroutine, context=future._context)
            except BaseException:
                coroutine.close()
                raise
            self.tasks[task] = future
            future._bind(task)

            def done(completed):
                self.tasks.pop(completed, None)
                future._finish(completed)

            task.add_done_callback(done)
        except BaseException as error:  # noqa: BLE001 -- Report scheduling failures to native admission.
            future.set_exception(error)
        finally:
            with self.worker._submission_lock:
                self.pending_submissions -= 1

    def reader(self, bridge):
        """The loop's reader callback for ``bridge``: the native invoker's
        drain, which starts each queued request's task itself."""
        if _Invoker is None or not isinstance(bridge, _NativeBridge):
            raise TypeError("attach_native needs bessemer._native.Bridge objects")
        self.worker._sync_submitters[self.loop] = bridge.call_sync
        return _Invoker(
            bridge,
            self.worker,
            self.loop,
            self.tasks,
            self.invocations,
            self.worker._native_routes,
        ).drain

    def close_bridge(self) -> None:
        """Stop taking work; call on this lane's loop thread."""
        if self.bridge is not None:
            # Stop admission but retain the reader while in-flight pool work
            # returns through this bridge. finish() detaches it after cleanup.
            self.bridge.close()

    def detach_bridge(self) -> None:
        self.worker._sync_submitters.pop(self.loop, None)
        bridge, self.bridge = self.bridge, None
        if bridge is not None:
            self.loop.remove_reader(bridge.fileno())

    async def finish(self) -> None:
        """Cancel this lane's requests and end its streams, on its own loop."""
        self.close_bridge()
        tasks = list(self.tasks)
        for task in tasks:
            invocation = self.tasks.get(task)
            if invocation is not None:
                invocation.cancel()
        native_waiters = []
        for invocation in list(self.invocations.values()):
            if getattr(invocation, "_sync_pending", False):
                invocation.cancel()
                waiter = _SyncFuture(loop=self.loop)
                invocation._wait_sync(waiter)
                native_waiters.append(waiter)
        if tasks or native_waiters:
            # Never abandon work when the caller's shutdown deadline expires.
            # close() reports that deadline; this lifecycle continues cleanup
            # and exits automatically when the actual handler work completes.
            await asyncio.gather(*tasks, *native_waiters, return_exceptions=True)
        streams = [
            stream for stream in list(self.worker._streams) if stream._loop is self.loop
        ]
        if streams:
            # Open streams end at shutdown; their cleanup still runs fully.
            # Each close runs in its stream's own context, as every other step
            # of the stream does, so iterator finalizers see their ContextVars.
            await asyncio.gather(
                *(
                    self.loop.create_task(stream._close(), context=stream._context)
                    for stream in streams
                ),
                return_exceptions=True,
            )

        after = [
            handle
            for handle in list(self.worker._after_responses)
            if handle._loop is self.loop
        ]
        if after:
            await asyncio.gather(*(handle.close() for handle in after))
        self.detach_bridge()

    def _tick(self) -> None:
        self.ticked = time.monotonic()
        self.loop.call_later(_LANE_TICK, self._tick)

    def run(self) -> None:
        """Thread body for a lane other than the main one."""
        asyncio.set_event_loop(self.loop)
        self.loop.set_default_executor(self.worker._executor)
        self.loop.call_soon(self._tick)
        try:
            self.loop.run_forever()
        finally:
            # Not closed here: closing a loop shuts down its default executor,
            # which is shared, so the worker closes every loop only after all
            # of them have finished this cleanup.
            self.close_bridge()
            self.loop.run_until_complete(self.worker._cancel_background_tasks())
            self.loop.run_until_complete(self.loop.shutdown_asyncgens())

    def stop(self) -> None:
        with suppress(RuntimeError):  # Already closed.
            self.loop.call_soon_threadsafe(self.loop.stop)


class Worker:
    """Runs application work and lifespan on one main loop in its own thread.

    With ``loops > 1`` native requests are also served by further event loops,
    each in its own thread. WebSocket sessions with no application resources
    can use those lanes; lifespan and application dependencies stay on main.
    """

    def __init__(
        self,
        app: App,
        *,
        graceful_timeout: float = 30.0,
        startup_timeout: float | None = None,
        sync_workers: int | None = None,
        heartbeat_fd: int | None = None,
        max_response_size: int = 4_194_304,
        loops: int = 1,
        event_loop: str = "auto",
        io_backend: str = "epoll",
    ) -> None:
        validate_loops(loops)
        event_loop = _resolve_event_loop(event_loop, loops)
        validate_choice("io_backend", io_backend)
        new_loop = _loop_factory(event_loop)
        app.build()
        self.app = app
        self._sync_submitters = {}
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
        # Request-scoped dependency graphs flattened into call steps, run
        # without a DependencyResolver (by id(route)).
        self._flat_plans = {
            id(route): flat
            for route in self._compiled
            if (flat := self._flatten_dependencies(route)) is not None
        }
        # Handlers that also take the request or its BackgroundTasks, but no
        # dependency: bound without a DependencyResolver (by id(route)).
        self._simple_bindings = {
            id(route): bindings
            for route in self._compiled
            if (bindings := self._simple_plan_bindings(route)) is not None
        }
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
        # Plain handlers that take only native inputs need no resolver,
        # exit stack or background tasks: they run on the lean path.
        self._lean_plans = [
            route.plan
            if route.handler is not None
            and route.declaration.get("kind", "http") != "websocket"
            and direct is not None
            and getattr(route, "plan", None) is not None
            and route.plan.kind in ("async", "sync")
            else None
            for route, direct in zip(app._compiled, self._direct_bindings, strict=True)
        ]
        # Plain Pydantic models compiled for the native fast path. Request
        # bodies of these models are built natively when they already have
        # their fields' exact types; responses returning exactly the declared
        # model are encoded natively. Everything else keeps Pydantic's path.
        self._body_plans = []
        self._response_plans: dict[int, Any] = {}
        if _encode_model_json is not None:
            for route_id, route in enumerate(self._compiled):
                validators = getattr(route, "input_validators", None) or {}
                if route.embedded_body is not None:
                    # Embedded body parameters share one generated model.
                    key, model, _, _ = route.embedded_body
                    plan = model_plan(model)
                    if plan is not None:
                        self._body_plans.append((route_id, key, plan))
                elif len(route.body_models) == 1:
                    ((key, model),) = route.body_models.items()
                    # Body() constraints validate the raw JSON value before
                    # Pydantic, so those bodies must reach Python unbuilt.
                    # A model_validate override must keep running too.
                    plan = (
                        model_plan(model)
                        if key not in validators and _default_model_validate(model)
                        else None
                    )
                    if plan is not None:
                        self._body_plans.append((route_id, key, plan))
                if (
                    getattr(route, "response_adapter", None) is not None
                    and route.response_model_options is None
                ):
                    plan = model_plan(route.response_model)
                    if plan is not None:
                        self._response_plans[id(route)] = plan
        # HTTP middleware per route, outermost first: the last registered wraps
        # the rest, and a mounted app's middleware runs inside this app's.
        outer = tuple(reversed(getattr(app, "_http_middleware", ())))
        self._route_middleware = [
            outer + getattr(route, "middleware", ()) for route in app._compiled
        ]
        # The App each route belongs to: a mounted app for its routes.
        self._owners = [getattr(route, "app", None) or app for route in app._compiled]
        self._websocket_affinity = [
            self._websocket_needs_main(route, owner)
            for route, owner in zip(self._compiled, self._owners, strict=True)
        ]
        self._cpu_apps = (app, *(mounted for _, mounted in app._mount_tree()))
        self._route_identity = [
            (route.declaration["method"], route.declaration["path"])
            for route in app._compiled
        ]
        self._watches_disconnect = [
            self._route_watches_disconnect(route) for route in self._compiled
        ]
        # Standard ASGI middleware wraps every Python HTTP route; without it
        # nothing below changes and no route takes the ASGI path.
        self._asgi_app = None
        if getattr(app, "_asgi_middleware", None):
            from ._asgi import InnerApp, build_stack

            self._asgi_app = build_stack(app, InnerApp(self))
        asgi = self._asgi_app is not None
        # Lean routes that also take no inputs and run no middleware: a call
        # with no arguments needs only the bare request and the handler.
        self._bare_routes = [
            (route, plan.call, method, path, owner)
            if plan is not None
            and plan.kind == "async"
            and not direct
            and not needs_validation
            and not middleware
            and not asgi
            else None
            for route, plan, direct, needs_validation, middleware, (
                method,
                path,
            ), owner in zip(
                self._compiled,
                self._lean_plans,
                self._direct_bindings,
                self._needs_validation,
                self._route_middleware,
                self._route_identity,
                self._owners,
                strict=True,
            )
        ]
        # A middleware request already owns metadata, trace and cleanup.
        # No-input async calls need none of _invoke's binding machinery.
        self._middleware_calls = [
            plan.call
            if plan is not None
            and plan.kind == "async"
            and not direct
            and not validation
            and not route.declaration.get("needs_principal")
            else None
            for route, plan, direct, validation in zip(
                self._compiled,
                self._lean_plans,
                self._direct_bindings,
                self._needs_validation,
                strict=True,
            )
        ]
        # Routes the native invoker runs without the Python orchestration
        # below: the handler is called straight from Rust in its own task.
        self._native_routes = [
            self._native_route(route_id) for route_id in range(len(self._compiled))
        ]
        self.graceful_timeout = _positive_float(graceful_timeout, "graceful_timeout")
        self.startup_timeout = (
            self.graceful_timeout
            if startup_timeout is None
            else _positive_float(startup_timeout, "startup_timeout")
        )
        sync_workers = _sync_worker_count(app, sync_workers)
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
        self._executor = ContextThreadPoolExecutor(
            max_workers=sync_workers, thread_name_prefix="bessemer-handler"
        )
        self._loop = new_loop()
        # With the Rust loop and a single lane, the HTTP server runs on the
        # loop's own thread in the loop's embedded Tokio runtime: no hand-off
        # between threads per request. The runtime must exist before the
        # loop first runs, so its waits are made in it.
        self._serves_connections = event_loop == "bessemer" and loops == 1
        if self._serves_connections:
            self._loop._core_enable_runtime()
        self._io_backend = _resolve_io_backend(io_backend, self._serves_connections)
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
        self._after_responses: set[_AfterResponse] = set()
        self._native_invocations: dict[int, _InvocationControl] = {}
        self._lane = _Lane(self, self._loop, self._tasks, self._native_invocations)
        self._lanes = [self._lane] + [
            _Lane(self, new_loop(), {}, {}) for _ in range(loops - 1)
        ]
        self._submission_lock = threading.Lock()
        self._next_submission = 0
        self._accepting_switch = _Switch(False)
        self._closed = False
        self._thread = threading.Thread(
            target=self._run, name="bessemer-asyncio", daemon=True
        )

    @property
    def _accepting(self) -> bool:
        """Whether new requests may start; the native invoker reads it per job."""
        return self._accepting_switch.on

    @_accepting.setter
    def _accepting(self, accepting: bool) -> None:
        with self._submission_lock:
            self._accepting_switch.on = bool(accepting)

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
                self._stop_lanes()
                try:
                    self._loop.run_until_complete(self._shutdown_cpu())
                finally:
                    self._executor.shutdown(wait=True, cancel_futures=True)
                    self._loop.close()

    async def _shutdown_cpu(self) -> None:
        for app in reversed(self._cpu_apps):
            try:
                await app._shutdown_cpu()
            except BaseException as error:  # noqa: BLE001 -- Finish releasing every application's pool.
                self._shutdown_error = self._shutdown_error or error

    def _stop_lanes(self) -> None:
        lanes = self._lanes[1:]
        for lane in lanes:
            if lane.thread is not None:
                lane.stop()
        for lane in lanes:
            if lane.thread is not None:
                lane.thread.join()
        for lane in lanes:
            if lane.thread is None or not lane.thread.is_alive():
                lane.loop.close()

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
            processes = getattr(self.app, "_worker_processes", 1)
            default_cpu = self.app._configure_cpu(processes)
            for app in self._cpu_apps[1:]:
                app._configure_cpu(processes, default=default_cpu)
            state = None
            if self.app.lifespan is not None:
                state = await self._app_dependency_stack.enter_async_context(
                    self.app.lifespan(self.app)
                )
            if state is not None:
                if not isinstance(state, Mapping):
                    raise TypeError(
                        "the lifespan must yield a mapping (the lifespan state) "
                        f"or None, not {type(state).__name__}"
                    )
                # Each request's state starts as a shallow copy of this, as in
                # Starlette; mounted apps share the outermost app's state.
                state = dict(state)
            for app in (self.app, *(app for _, app in self.app._mount_tree())):
                app._lifespan_state = state
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
                # A stalled extra loop withholds the heartbeat, so the
                # supervisor replaces the worker as it would for the main loop.
                now = time.monotonic()
                if all(
                    lane.thread is None or now - lane.ticked < _LANE_STALL
                    for lane in self._lanes[1:]
                ):
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
        with self._submission_lock:
            if not self._accepting or self._closed:
                raise RuntimeError("application worker is not accepting requests")
            if type(route_id) is not int or not 0 <= route_id < len(self._compiled):
                raise ValueError("unknown route id")
            lane = self._submission_lane(route_id)
            lane.pending_submissions += 1
            try:
                return lane.submit(route_id, arguments)
            except BaseException:
                lane.pending_submissions -= 1
                raise

    def _websocket_needs_main(self, route, owner):
        if route.declaration.get("kind") != "websocket":
            return True
        if self.app.lifespan is not None or owner.lifespan is not None:
            return True
        plans = (route.plan, *route.dependencies)
        return any(
            plan.scope == "app"
            or any(binding.kind == "state" for binding in plan.bindings)
            for plan in plans
        )

    def _submission_lane(self, route_id):
        owner = self._owners[route_id]
        if (
            len(self._lanes) == 1
            or self._websocket_affinity[route_id]
            or self._app_dependency_cache
            or self._state_needs_main(self.app.state)
            or self._state_needs_main(owner.state)
            or self.app._lifespan_state
        ):
            return self._lane
        lanes = [self._lane] + [
            lane
            for lane in self._lanes[1:]
            if lane.thread is not None and lane.thread.is_alive()
        ]
        start = self._next_submission % len(lanes)
        self._next_submission += 1
        ordered = lanes[start:] + lanes[:start]
        return min(ordered, key=lambda lane: len(lane.tasks) + lane.pending_submissions)

    @staticmethod
    def _state_needs_main(state):
        return type(state) is not SimpleNamespace or bool(state.__dict__)

    @classmethod
    def _callable_watches_disconnect(cls, target) -> bool:
        code = getattr(target, "__code__", None)
        if code is None and callable(target):
            code = getattr(type(target).__call__, "__code__", None)
        if code is None:
            return False
        names = code.co_names
        return "is_disconnected" in names or "receive" in names

    @classmethod
    def _route_watches_disconnect(cls, route) -> bool:
        if cls._callable_watches_disconnect(route.handler):
            return True
        return any(
            cls._callable_watches_disconnect(plan.call)
            for plan in getattr(route, "dependencies", ())
        )

    def attach_native(self, bridge, *more) -> None:
        """Serve requests queued by native code on ``bridge``.

        The loop watches the bridge's descriptor and drains queued requests in
        batches on its own thread, so native threads never enter Python on the
        request path. Call after start() and before the server accepts traffic.
        A worker with ``loops > 1`` takes one further bridge per extra loop.
        """
        if len(more) != len(self._lanes) - 1:
            raise ValueError("attach_native needs one bridge per event loop")
        if self._body_plans:
            for native in (bridge, *more):
                if hasattr(native, "set_body_plans"):
                    native.set_body_plans(self._body_plans)
        installed = threading.Event()
        failure: list[BaseException] = []

        def install() -> None:
            try:
                if not self._accepting or self._closed:
                    raise RuntimeError("application worker is not accepting requests")
                if self._lane.bridge is not None:
                    raise RuntimeError("a native bridge is already attached")
                self._loop.add_reader(bridge.fileno(), self._lane.reader(bridge))
                if self._serves_connections and hasattr(bridge, "set_local_wake"):
                    # Queued work wakes the loop directly, not through the
                    # bridge's socket.
                    bridge.set_local_wake(self._loop)
                self._lane.bridge = bridge
            except BaseException as error:  # noqa: BLE001 -- Report to the calling thread.
                failure.append(error)
            finally:
                installed.set()

        self._loop.call_soon_threadsafe(install)
        if not installed.wait(self.startup_timeout):
            raise TimeoutError("the worker loop did not attach the native bridge")
        if failure:
            raise failure[0]
        for index, (lane, extra) in enumerate(
            zip(self._lanes[1:], more, strict=True), start=1
        ):
            # The loop is not running yet, so it can be set up from here.
            lane.loop.add_reader(extra.fileno(), lane.reader(extra))
            lane.bridge = extra
            lane.thread = threading.Thread(
                target=lane.run, name=f"bessemer-asyncio-{index}", daemon=True
            )
            lane.thread.start()
        if not self._accepting:
            # Shutdown began while the extra loops were starting.
            self._loop.call_soon_threadsafe(self._close_native)

    def _close_native(self) -> None:
        self._lane.close_bridge()
        for lane in self._lanes[1:]:
            bridge = lane.bridge
            if bridge is not None:
                # Thread-safe: refuse new work now; the loop drops its reader.
                bridge.close()
            if lane.thread is not None:
                with suppress(RuntimeError):  # Already closed.
                    lane.loop.call_soon_threadsafe(lane.close_bridge)

    def _native_route(self, route_id: int):
        """The native invoker's spec for a lean route, or None.

        A route qualifies when ``_invoke`` would call its handler with
        the native inputs unchanged: no middleware, request metadata,
        dependencies, or validator other than numeric and length bounds the
        invoker checks itself. A single Pydantic body model qualifies when it
        has a native body plan (so no ``Body()`` validator, embed or
        ``model_validate`` override): the invoker builds the instance from the
        raw body exactly as the bridge does for ``_invoke``, and only an
        instance that needs no further validation is passed. Requests that
        need anything else (a principal, a trace, an input outside its bounds,
        a body the plan cannot build) still take ``_invoke`` at run time.
        An async handler whose sole binding is BackgroundTasks also qualifies;
        its response uses the worker's delivery-owned background boundary.
        """
        plan = self._lean_plans[route_id]
        route = self._compiled[route_id]
        background_name = None
        direct = self._direct_bindings[route_id]
        if plan is None:
            simple = self._simple_bindings.get(id(route))
            if (
                simple is not None
                and len(simple) == 1
                and simple[0][1] == "background_tasks"
                and route.plan.kind == "async"
            ):
                plan = route.plan
                direct = ()
                background_name = simple[0][0]
        if (
            plan is None
            or route.declaration.get("kind") == "websocket"
            or self._asgi_app is not None
            or self._route_middleware[route_id]
            or route.body_containers
            or route.embedded_body is not None
            or route.declaration.get("needs_principal")
            or route.declaration.get("needs_request")
        ):
            return None
        # The single body model's key and native plan; set_body_plans gives
        # the bridge the same plan, so a route without one never gets here
        # with its body as raw bytes.
        body_key = body_plan = None
        if route.body_models:
            ((body_key, _),) = route.body_models.items()
            body_plan = next(
                (
                    found
                    for planned, _, found in self._body_plans
                    if planned == route_id
                ),
                None,
            )
            if body_plan is None:
                return None
        parameters = self._route_parameters[route_id]
        validators = getattr(route, "input_validators", None) or {}
        bindings = []
        body = None
        for name, key in direct:
            if key == body_key:
                if body is not None or key in validators:
                    return None
                body = (key, name, body_plan)
                continue
            parameter = parameters.get(key)
            if parameter is None or parameter["source"] not in _NATIVE_SOURCES:
                return None
            constraints = None
            if key in validators:
                constraints = getattr(validators[key], "native_constraints", None)
                if constraints is None:
                    return None
            bindings.append((key, name, constraints))
        declared = {key for key, _, _ in bindings}
        if body is not None:
            declared.add(body_key)
        if (
            (body_key is not None and body is None)
            or declared != set(parameters)
            or set(validators) - set(parameters)
        ):
            return None
        method, path = self._route_identity[route_id]
        # Request state is created lazily, so the attributes are all shared
        # immutable values and a shallow copy per request is a fresh request.
        template = vars(Request._bare(method, path, self._owners[route_id])).copy()
        response_plan = None
        if getattr(route, "response_adapter", None) is None:
            encode = "plain"
        else:
            response_plan = self._response_plans.get(id(route))
            if response_plan is None:
                encode = "python"
            elif response_plan.is_list:
                encode = "model_list"
            else:
                encode = "model"
        if _encode_plain_json is None:
            encode = "python"
        return (
            plan.call,
            route,
            tuple(bindings),
            template,
            route.declaration["status_code"],
            encode,
            response_plan,
            body,
            plan.kind == "sync",
            background_name,
        )

    async def _lean_sync(self, function, arguments, future):
        result = await self._call_sync(function, (), arguments, future)
        return await self._await_result(result, future)

    async def _background_response(self, route, result, request, future, background):
        """Finish a dependency-free native handler with injected tasks."""
        try:
            if (
                isinstance(result, Response)
                and result.background is not None
                and result.background is not background
            ):
                background._tasks.extend(background_tasks(result))
            if isinstance(result, StreamingResponse):
                return await self._stream_response(
                    result, request, future, AsyncExitStack(), background
                )
            response = self._plain_response(route, result)
            if not self._defer_tasks(future, request, background._tasks):
                await self._run_tasks(background._tasks, future)
            return response
        except asyncio.CancelledError:
            raise
        except BaseException as error:  # noqa: BLE001 -- Same handler boundary.
            return await self._invocation_failed(route, request, error, future)

    async def _sync_response(self, route, result, request, future):
        try:
            result = await self._await_result(result, future)
            return await self._lean_response(route, result, request, future)
        except asyncio.CancelledError:
            raise
        except BaseException as error:  # noqa: BLE001 -- same handler boundary
            return await self._invocation_failed(route, request, error, future)

    async def _lean_response(self, route, result, request, future):
        """Finish a native lean call whose response streams or carries
        background tasks, as the lean path in ``_invoke`` does."""
        try:
            if isinstance(result, StreamingResponse):
                return await self._stream_response(result, request, future)
            response = self._plain_response(route, result)
            tasks = background_tasks(result)
            if not self._defer_tasks(future, request, tasks):
                await self._run_tasks(tasks, future)
            return response
        except asyncio.CancelledError:
            raise
        except BaseException as error:  # noqa: BLE001 -- As in _invoke.
            return await self._invocation_failed(route, request, error, future)

    async def _lean_failed(self, route, request, error, future):
        """Answer a native lean handler's exception through the same boundary
        as ``_invoke``, with the exception being handled while it runs."""
        try:
            raise error
        except BaseException:  # noqa: BLE001 -- As in _invoke.
            try:
                return await self._invocation_failed(route, request, error, future)
            finally:
                # The traceback holds this frame: dropping the local leaves
                # no exception -> traceback -> frame -> exception cycle.
                del error

    @staticmethod
    def _details(
        error: ValueError | TypeError, location: list[str], message: str
    ) -> list[dict[str, Any]]:
        errors = getattr(error, "errors", None)
        if callable(errors):
            error_count = getattr(error, "error_count", None)
            if callable(error_count) and error_count() > 100:
                return [{"type": "too_many_errors", "loc": location, "msg": message}]
            return [
                {
                    "type": item.get("type", "value_error"),
                    "loc": [*location, *item.get("loc", ())],
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
        return [{"type": "value_error", "loc": location, "msg": message}]

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

    @staticmethod
    def _flatten_dependencies(route):
        """The route's dependency graph as ``(steps, handler arguments)``, or
        None where only the DependencyResolver applies.

        Each step is ``(plan, arguments)``; an argument is ``(name, source,
        value)`` with source ``_FROM_INPUT`` (value: argument key),
        ``_FROM_REQUEST``, ``_FROM_BACKGROUND``, ``_FROM_STATE`` or
        ``_FROM_STEP`` (value: the step's index). Steps are in the order the
        resolver calls them, depth first through each callable's bindings,
        with ``before`` dependencies first; a cached dependency is one step
        however often it is used, an uncached one a step per use.
        """
        plan = getattr(route, "plan", None)
        dependencies = getattr(route, "dependencies", ())
        if plan is None or not dependencies:
            return None
        if any(dependency.scope != "request" for dependency in dependencies):
            return None
        steps = []
        cached = {}
        resolving = set()
        sources = {
            "input": _FROM_INPUT,
            "request": _FROM_REQUEST,
            "background_tasks": _FROM_BACKGROUND,
            "state": _FROM_STATE,
        }

        def argument(binding):
            if binding.kind == "dependency":
                return (binding.name, _FROM_STEP, step(binding))
            source = sources.get(binding.kind)
            if source is None:
                raise LookupError(binding.kind)
            return (binding.name, source, binding.key)

        def step(binding):
            identifier = binding.dependency
            if binding.use_cache and identifier in cached:
                return cached[identifier]
            if identifier in resolving:
                raise LookupError("cycle")
            resolving.add(identifier)
            dependency = dependencies[identifier]
            arguments = tuple(argument(item) for item in dependency.bindings)
            resolving.discard(identifier)
            steps.append((dependency, arguments))
            if binding.use_cache:
                cached[identifier] = len(steps) - 1
            return len(steps) - 1

        try:
            for binding in getattr(route, "before", ()):
                if binding.kind != "dependency":
                    return None
                step(binding)
            handler = tuple(argument(binding) for binding in plan.bindings)
        except LookupError:
            return None
        return tuple(steps), handler

    @staticmethod
    def _flat_arguments(arguments, spec, values, request, background, state):
        bound = {}
        for name, source, value in spec:
            if source is _FROM_INPUT:
                bound[name] = arguments[value]
            elif source is _FROM_STEP:
                bound[name] = values[value]
            elif source is _FROM_REQUEST:
                bound[name] = request
            elif source is _FROM_BACKGROUND:
                bound[name] = background
            else:
                bound[name] = state
        return bound

    async def _run_flat(
        self, route, flat, arguments, request, background, stack, future
    ):
        """Run a flattened dependency graph and the handler, as the
        DependencyResolver would."""
        steps, handler = flat
        state = (route.app or self.app).state
        values = []
        bind = self._flat_arguments
        for dependency, spec in steps:
            bound = bind(arguments, spec, values, request, background, state)
            if dependency.kind == "async":
                future._raise_if_cancel_requested()
                values.append(await dependency.call(**bound))
            else:
                values.append(
                    await self._execute_plan(dependency, bound, stack, future)
                )
        return await self._execute_plan(
            route.plan,
            bind(arguments, handler, values, request, background, state),
            stack,
            future,
        )

    @staticmethod
    def _simple_plan_bindings(route):
        """Return ``(name, kind, key)`` handler bindings of only native
        inputs, the request and BackgroundTasks, or None."""
        plan = getattr(route, "plan", None)
        if plan is None or getattr(route, "before", ()):
            return None
        if any(
            binding.kind not in ("input", "request", "background_tasks")
            for binding in plan.bindings
        ):
            return None
        return tuple(
            (binding.name, binding.kind, binding.key) for binding in plan.bindings
        )

    def _validate_arguments(
        self, route, arguments: dict[str, Any], parameters=None
    ) -> dict[str, Any]:
        values = arguments.copy()
        if parameters is None:
            parameters = {
                item["name"]: item for item in route.declaration["parameters"]
            }
        # An error's location is only built when there is an error.
        for name, parameter in parameters.items():
            if name not in values:
                if parameter["required"]:
                    location = [
                        _LOCATIONS.get(parameter["source"], parameter["source"]),
                        parameter.get("alias", name),
                    ]
                    raise RequestValidationError(
                        [{"type": "missing", "loc": location, "msg": "Field required"}]
                    )
                values[name] = parameter["default"]
        for name, validate in (getattr(route, "input_validators", None) or {}).items():
            parameter = parameters[name]
            try:
                values[name] = validate(values[name])
            except (ValueError, TypeError) as error:
                location = [
                    _LOCATIONS.get(parameter["source"], parameter["source"]),
                    parameter.get("alias", name),
                ]
                raise RequestValidationError(
                    self._details(error, location, "Invalid request input")
                ) from error
        for name in (*route.body_models, *route.body_containers):
            if values.get(name) is None and name not in route.nullable_body:
                raise RequestValidationError(
                    [
                        {
                            "type": "value_error",
                            "loc": ["body"],
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
                            "type": "dict_type" if container is dict else "list_type",
                            "loc": ["body"],
                            "msg": "Expected a JSON object"
                            if container is dict
                            else "Expected a JSON array",
                        }
                    ]
                )
        for name, model in route.body_models.items():
            value = values.get(name)
            # The native fast path already built an instance from a body with
            # exact field types; body defaults are always None, so an
            # instance here can only come from there.
            if value is not None and type(value) is not model:
                try:
                    values[name] = model.model_validate(values[name])
                except ValueError as error:
                    raise RequestValidationError(
                        self._details(error, ["body"], "Invalid request body")
                    ) from error
        if route.embedded_body is not None:
            self._embedded_arguments(route.embedded_body, values)
        return values

    def _embedded_arguments(self, embedded, values: dict[str, Any]) -> None:
        """Validate a body shared by embedded parameters, then bind each one."""
        key, model, aliases, keys = embedded
        value = values.pop(key, None)
        if type(value) is not model:
            if value is None:
                value = {}
            elif not isinstance(value, dict):
                # As FastAPI: no field can be read from anything but an object.
                raise RequestValidationError(
                    [
                        {
                            "type": "missing",
                            "loc": ["body", alias],
                            "msg": "Field required",
                        }
                        for alias in aliases
                    ]
                )
            try:
                value = model.model_validate(value)
            except ValueError as error:
                raise RequestValidationError(
                    self._details(error, ["body"], "Invalid request body")
                ) from error
        for attribute, name in keys:
            values[name] = getattr(value, attribute)

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

        loop = asyncio.get_running_loop()
        work = _SyncFuture(loop=loop)

        native = self._sync_submitters.get(loop)
        if native is not None:
            native(
                self._executor, (function, args, kwargs, context, work, future, loop)
            )
        else:

            def settle(result, error):
                if error is None:
                    work.set_result(result)
                else:
                    work.set_exception(error)

            def invoke():
                result = error = None
                try:
                    # Queueing is not execution: a deadline may cancel this request
                    # while executor threads are occupied by earlier calls.
                    if future is not None and not future._begin_sync():
                        raise asyncio.CancelledError
                    try:
                        result = context.run(function, *args, **kwargs)
                    finally:
                        if future is not None:
                            future._end_sync()
                except BaseException as caught:  # noqa: BLE001 -- deliver on the loop
                    error = caught
                    if isinstance(error, StopIteration):
                        error = RuntimeError("sync call raised StopIteration")
                        error.__cause__ = caught
                # Only the loop mutates its future. The executor's concurrent future
                # is deliberately not chained to it: that adds locks, callbacks and
                # another scheduling step to every synchronous call.
                # Like asyncio's executor chaining, a closed destination loop
                # cannot accept completion. Shutdown normally drains the pool first,
                # but an externally closed loop must not fail inside the executor.
                if loop.is_closed():
                    if inspect.iscoroutine(result):
                        result.close()
                    return
                try:
                    loop.call_soon_threadsafe(
                        settle, result, error, context=contextvars.Context()
                    )
                except RuntimeError:
                    # close() can race with the check above. Do not conceal other
                    # scheduling failures from an open loop.
                    if not loop.is_closed():
                        raise
                    if inspect.iscoroutine(result):
                        result.close()

            self._executor.execute(invoke)
        cancellation = None
        try:
            result = await work
        except asyncio.CancelledError as error:
            # A cancelled queued wrapper still owns arguments/context until the
            # executor processes it. Keep request capacity until then, and wait
            # for actual completion when synchronous user work already started.
            cancellation = error
            while not work.done():
                try:
                    await work
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
                _AsyncGeneratorContextManager(plan.call, (), arguments)
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
        stack: AsyncExitStack,
        error: BaseException | None,
        *,
        context=None,
        inline=None,
    ):
        # These stacks are framework-owned. The private callback deque is part
        # of our qualified CPython 3.12/3.13 compatibility surface; nonempty
        # stacks run inline under ``inline``'s deferred cancellation, or else
        # in a shielded cleanup Task in their original context.
        if not stack._exit_callbacks:
            return False
        if inline is not None and inline._begin_cleanup():
            # ``inline`` is the invocation whose task (running in ``context``)
            # is calling: while the cleanup runs here, the invocation defers
            # any cancellation, which is all a separate shielded Task would
            # achieve, without that Task and its loop iterations.
            try:
                suppressed = await stack.__aexit__(
                    type(error) if error else None,
                    error,
                    error.__traceback__ if error else None,
                )
            finally:
                cancelled = inline._end_cleanup()
            if cancelled:
                raise asyncio.CancelledError
            return suppressed

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
            invoke=self._execute_plan,
            future=future,
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
        inline_cleanup=False,
    ):
        # Cleanup runs inline only when nothing but the invocation can cancel
        # this task: never under HTTP or ASGI middleware, which may wrap the
        # route in their own timeouts or tasks.
        inline = future if inline_cleanup else None
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
            elif (flat := self._flat_plans.get(id(route))) is not None:
                result = await self._run_flat(
                    route, flat, arguments, request, background, stack, future
                )
            elif (simple := self._simple_bindings.get(id(route))) is not None:
                values = {}
                for name, kind, key in simple:
                    if kind == "input":
                        values[name] = arguments[key]
                    elif kind == "request":
                        values[name] = request
                    else:
                        values[name] = background
                result = await self._execute_plan(plan, values, stack, future)
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
            if self._defer_tasks(future, request, background._tasks, stack):
                return response
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
            suppressed = await self._finish_exit(
                stack, error, context=future._context, inline=inline
            )
            if suppressed:
                raise RuntimeError(
                    "a dependency suppressed an error without returning a response"
                ) from error
            raise
        else:
            await self._finish_exit(stack, None, context=future._context, inline=inline)
            return response

    def _defer_tasks(self, future, request, tasks, stack=None, stream=None):
        if not tasks or isinstance(future, _InvocationFuture):
            return False
        handle = _AfterResponse(
            self, request, tasks, stack or AsyncExitStack(), future._context, stream
        )
        future._defer_background(self._start_after_response, handle)
        return True

    def _start_after_response(self, handles, sent):
        loop = handles[0]._loop

        async def run():
            for handle in handles:
                # A route's background work and finalizers run before work
                # attached by its outer middleware or exception boundary.
                await handle.start(sent)

        if asyncio._get_running_loop() is loop:
            if len(handles) == 1:
                return handles[0].start(sent)
            return loop.create_task(run())
        return asyncio.run_coroutine_threadsafe(run(), loop)

    async def _run_tasks(self, tasks, future) -> None:
        for task in tasks:
            if future is not None:
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
        if self._defer_tasks(future, request, background._tasks, stack, stream):
            stream._background = BackgroundTasks()
            stream._stack = AsyncExitStack()
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
                plan = self._response_plans.get(id(route))
                if plan is not None and _encode_model_json is not None:
                    if plan.is_list:
                        if type(result) is list:
                            encoded = _encode_model_list_json(
                                plan, result, self.max_response_size
                            )
                            if encoded is not None:
                                return (
                                    route.declaration["status_code"],
                                    encoded,
                                    (),
                                    "json_bytes",
                                )
                    elif type(result) is plan.model:
                        encoded = _encode_model_json(
                            plan, result, self.max_response_size
                        )
                        if encoded is not None:
                            return (
                                route.declaration["status_code"],
                                encoded,
                                (),
                                "json_bytes",
                            )
                options = route.response_model_options
                try:
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
                except (ValueError, TypeError) as error:
                    raise ResponseValidationError(
                        self._details(error, ["response"], "Invalid response"),
                        body=result,
                    ) from error
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
        if request is None and not arguments:
            bare = self._bare_routes[route_id]
            if bare is not None:
                # The lean path below with every check that cannot apply
                # skipped: no inputs, middleware, principal or trace.
                route, call, method, path, owner = bare
                request = Request._bare(method, path, owner)
                token = current_request.set(request)
                try:
                    future._raise_if_cancel_requested()
                    result = await call()
                    if isinstance(result, Response):
                        if isinstance(result, StreamingResponse):
                            return await self._stream_response(result, request, future)
                        if result.background is not None:
                            response = self._plain_response(route, result)
                            await self._run_tasks(background_tasks(result), future)
                            return response
                    return self._plain_response(route, result)
                except asyncio.CancelledError:
                    raise
                except BaseException as error:  # noqa: BLE001 -- As below.
                    return await self._invocation_failed(route, request, error, future)
                finally:
                    current_request.reset(token)
        route = self._compiled[route_id]
        if route.handler is None:
            return 200, {"ok": True}
        if route.declaration["kind"] == "websocket":
            return await self._invoke_websocket(route, arguments, future)
        if request is None and self._asgi_app is not None:
            return await self._invoke_asgi(route_id, arguments, future)
        if request is None and self._route_middleware[route_id]:
            return await self._invoke_through_middleware(route_id, arguments, future)
        # A request that already exists came through a middleware chain.
        inline_cleanup = request is None
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
            if self._watches_disconnect[route_id]:
                request._invocation = future
                future._watch_disconnect()
        else:
            method, path = self._route_identity[route_id]
            request = Request._bare(method, path, self._owners[route_id])
            if self._watches_disconnect[route_id]:
                request._invocation = future
                future._watch_disconnect()
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
                bound = {
                    name: arguments[key]
                    for name, key in self._direct_bindings[route_id]
                }
                if plan.kind == "async":
                    result = await plan.call(**bound)
                else:
                    result = await self._lean_sync(plan.call, bound, future)
                if isinstance(result, Response):
                    if isinstance(result, StreamingResponse):
                        return await self._stream_response(result, request, future)
                    if result.background is not None:
                        response = self._plain_response(route, result)
                        tasks = background_tasks(result)
                        if not self._defer_tasks(future, request, tasks):
                            await self._run_tasks(tasks, future)
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
                inline_cleanup,
            )
        except asyncio.CancelledError:
            raise
        except BaseException as error:  # noqa: BLE001 -- A handler must not terminate the shared worker loop.
            return await self._invocation_failed(route, request, error, future)
        finally:
            if inline_cleanup and self._watches_disconnect[route_id]:
                request._invocation = None
            current_request.reset(token)
            otel.detach(trace_token)

    async def _invocation_failed(self, route, request, error, future):
        """Answer a route's exception (never a cancellation) while its
        request is still current."""
        if isinstance(error, HTTPException):
            handled = await self._handle_exception(request, error, future)
            if handled is not None:
                return handled
            try:
                response = self._json_response(
                    error.status_code, {"detail": error.detail}
                )
                if getattr(error, "headers", None):
                    if len(response) == 4:
                        return (*response[:2], error.headers.raw, "json_bytes")
                    return (*response, error.headers.raw, "json")
                return response
            except (TypeError, ValueError):
                _LOG.error(
                    "Invalid HTTPException detail in %s %s",
                    route.declaration["method"],
                    route.declaration["path"],
                )
                return 500, {"detail": "Internal Server Error"}
        if isinstance(error, RequestValidationError):
            handled = await self._handle_exception(request, error, future)
            if handled is not None:
                return handled
            return 422, {"detail": error.details}
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

    def _routed_request(self, route_id, arguments, future=None):
        """The request of a call that enters a middleware chain, with its
        trace attached; returns the request and the trace token."""
        metadata = arguments.get("__bessemer_request__")
        if metadata:
            request = Request(metadata, app=self._owners[route_id])
        else:
            method, path = self._route_identity[route_id]
            request = Request._bare(method, path, self._owners[route_id])
        if future is not None and self._watches_disconnect[route_id]:
            request._invocation = future
            future._watch_disconnect()
        trace = arguments.get("__bessemer_trace__")
        trace_token = None
        if trace is not None:
            request.traceparent = trace["traceparent"]
            trace_token = otel.attach(trace)
        return request, trace_token

    async def _invoke_routed(self, route_id, arguments, future, request):
        """Run a route for a request that already exists, through its HTTP
        middleware when it has any; the innermost ASGI app calls this."""
        if self._route_middleware[route_id]:
            return await self._invoke_through_middleware(
                route_id, arguments, future, request
            )
        return await self._invoke(route_id, arguments, future, request=request)

    async def _invoke_asgi(self, route_id, arguments, future):
        """Run the route inside the app's standard ASGI middleware.

        The request becomes an ASGI scope whose ``bessemer.call`` entry lets
        the innermost app run the route the native router matched. The
        messages the outermost middleware sends become the response.
        """
        from ._asgi import CALL_KEY, build_scope, call_asgi

        request, trace_token = self._routed_request(route_id, arguments, future)
        route = self._compiled[route_id]
        token = current_request.set(request)
        try:
            scope = build_scope(request)
            scope[CALL_KEY] = (route_id, arguments, future, request)
            request._scope = scope
            response = await call_asgi(self._asgi_app, scope, request._body)
            if isinstance(response, StreamingResponse):
                return await self._stream_response(response, request, future)
            return response.to_native(max_bytes=self.max_response_size)
        except asyncio.CancelledError:
            raise
        except BaseException as error:  # noqa: BLE001 -- Middleware must not terminate the shared worker loop.
            _LOG.error(
                "Unhandled %s in ASGI middleware for %s %s",
                type(error).__name__,
                route.declaration["method"],
                route.declaration["path"],
            )
            return 500, {"detail": "Internal Server Error"}
        finally:
            if self._watches_disconnect[route_id]:
                request._invocation = None
            current_request.reset(token)
            otel.detach(trace_token)

    async def _invoke_through_middleware(
        self, route_id, arguments, future, request=None
    ):
        """Run the route inside the HTTP middleware chain.

        Every stream that crosses a chain boundary, the route's or one a
        middleware created, is started and wrapped as a ``_CalledResponse``
        recorded in ``produced``. Whatever the outcome, each recorded stream
        that is not the final response is closed before the request ends.
        """
        own_request = request is None
        if own_request:
            request, trace_token = self._routed_request(route_id, arguments, future)
        else:
            # Under ASGI middleware, which already attached the trace.
            trace_token = None
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

        call = self._middleware_calls[route_id]
        if (
            arguments.keys() - _NATIVE_ARGUMENTS
            or arguments.get("__bessemer_principal__") is not None
        ):
            call = None

        async def call_route(_request=None):
            if call is None:
                envelope = await self._invoke(
                    route_id, arguments, future, request=request
                )
            else:
                route_token = current_request.set(request)
                try:
                    future._raise_if_cancel_requested()
                    result = await call()
                    if isinstance(result, Response):
                        envelope = await self._lean_response(
                            route, result, request, future
                        )
                    else:
                        envelope = self._plain_response(route, result)
                except asyncio.CancelledError:
                    raise
                except BaseException as error:  # noqa: BLE001 -- Same route boundary as _invoke.
                    envelope = await self._invocation_failed(
                        route, request, error, future
                    )
                finally:
                    current_request.reset(route_token)
            response = _CalledResponse(envelope)
            if response._kind in ("stream", "file"):
                produced.append(response)
            return response

        # Each level's call_next, built innermost first so no closure refers
        # to itself: the chain leaves no reference cycle for the collector.
        call_next = call_route
        for function in reversed(middleware[1:]):
            call_next = _middleware_level(function, call_next, request, future, adopt)

        token = current_request.set(request)
        try:
            future._raise_if_cancel_requested()
            response = await middleware[0](request, call_next)
            if not isinstance(response, Response):
                raise TypeError("HTTP middleware must return a Response")
            if isinstance(response, StreamingResponse):
                response = await adopt(response)
            envelope = response.to_native(max_bytes=self.max_response_size)
            # Tasks a middleware attached, to its own response or to the one
            # call_next returned. A call_next response never carries the
            # route's tasks (those already ran), so nothing runs twice.
            if response.background is not None:
                tasks = background_tasks(response)
                if len(envelope) == 4 and envelope[3] == "stream":
                    # A stream still has its body to send: its tasks, and
                    # anything they clean up, wait until it has finished.
                    stream = envelope[1]
                    if self._defer_tasks(future, request, tasks, stream=stream):
                        pass
                    else:
                        stream._background._tasks.extend(tasks)
                else:
                    if not self._defer_tasks(future, request, tasks):
                        await self._run_tasks(tasks, future)
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
            # Only streams or open files need closing; most responses have none.
            if produced:
                await self._close_unreturned(produced, response)
        finally:
            if own_request and self._watches_disconnect[route_id]:
                request._invocation = None
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
            if response is not returned:
                if response._kind == "stream":
                    cancelled |= await self._close_stream_shielded(
                        response._native_body
                    )
                elif response._kind == "file":
                    response._native_body.close()
        if cancelled:
            if returned in produced:
                if returned._kind == "stream":
                    await self._close_stream_shielded(returned._native_body)
                elif returned._kind == "file":
                    returned._native_body.close()
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
        websocket = WebSocket(
            arguments["__bessemer_websocket__"], request, asyncio.get_running_loop()
        )
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
            plan = route.plan
            arguments = await resolver.arguments_for(plan)
            if plan.kind == "async":
                # As _execute_plan, without its frame on the session's await
                # chain, which every received message resumes.
                if future is not None:
                    future._raise_if_cancel_requested()
                await plan.call(**arguments)
            else:
                await self._execute_plan(plan, arguments, stack, future)
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
        if not handlers:
            return None
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
                tasks = background_tasks(response)
                if len(envelope) == 4 and envelope[3] == "stream":
                    # A stream still has its body to send: its tasks, and
                    # anything they clean up, wait until it has finished.
                    stream = envelope[1]
                    if self._defer_tasks(future, request, tasks, stream=stream):
                        pass
                    else:
                        stream._background._tasks.extend(tasks)
                else:
                    if not self._defer_tasks(future, request, tasks):
                        await self._run_tasks(tasks, future)
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
        # Extra loops finish their own requests and streams in parallel with
        # this one; lifespan cleanup waits for all of them.
        finishing = []
        for lane in self._lanes[1:]:
            if lane.thread is None:
                continue
            coroutine = lane.finish()
            try:
                finishing.append(
                    asyncio.wrap_future(
                        asyncio.run_coroutine_threadsafe(coroutine, lane.loop)
                    )
                )
            except RuntimeError:  # The loop already stopped.
                coroutine.close()
        await self._lane.finish()
        if finishing:
            await asyncio.gather(*finishing, return_exceptions=True)
        await self._finish_exit(
            self._app_dependency_stack,
            self._startup_error or self._shutdown_error,
            context=self._lifecycle_context,
        )

    def close(self) -> None:
        if self._closed:
            return
        if self._thread.ident is None:
            self._stop_lanes()
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
