"""ASGI interoperability: standard ASGI middleware and mounted ASGI apps.

Bessemer's transport is native, so there is no ASGI server underneath it.
These helpers translate one admitted request into an ASGI ``http`` scope and
its single ``http.request`` message, and translate the ASGI messages an app
sends back into a Bessemer ``Response``. Nothing here runs unless an app adds
ASGI middleware or mounts an ASGI app; every other route keeps its native path.
"""

from __future__ import annotations

import asyncio
import json
import logging
from contextlib import suppress
from typing import Any
from urllib.parse import unquote

from .responses import _FORBIDDEN_HEADERS, Response, StreamingResponse

try:
    from ._native import _encode_plain_json
except ImportError:  # pragma: no cover - exercised only without the extension.
    _encode_plain_json = None

_LOG = logging.getLogger("bessemer.asgi")

_ASGI = {"version": "3.0", "spec_version": "2.4"}
_DONE = object()
# The scope key the innermost app reads to find the route a request belongs to.
CALL_KEY = "bessemer.call"
_JSON_TYPE = (b"content-type", b"application/json")


def build_scope(request, *, root_path: str | None = None) -> dict[str, Any]:
    """The ASGI ``http`` scope of an admitted request.

    ``state`` is the request's own ``request.state`` namespace dictionary, so
    a middleware that writes ``scope["state"]`` is seen through
    ``request.state``, as in Starlette.
    """
    path = request.path
    query = request.query_string
    client = request._client
    if root_path is None:
        root_path = getattr(request.app, "_mount_prefix", lambda: "")()
    return {
        "type": "http",
        "asgi": _ASGI,
        "http_version": "1.1",
        "method": request.method,
        "scheme": request.scheme,
        # The native request path keeps its percent-escapes; ASGI's ``path``
        # is decoded and ``raw_path`` keeps the bytes as sent.
        "path": unquote(path),
        "raw_path": path.encode("latin-1", errors="replace"),
        "query_string": query.encode("latin-1") if query else b"",
        "root_path": root_path,
        "headers": [
            (name.encode("latin-1"), value.encode("latin-1"))
            for name, value in request.headers._items
        ],
        "client": None if client is None else (str(client), 0),
        "server": None,
        "state": request.state.__dict__,
        "extensions": {},
    }


def _response_headers(raw) -> list[tuple[str, str]]:
    headers = []
    for name, value in raw:
        name = bytes(name).decode("latin-1")
        if name.lower() in _FORBIDDEN_HEADERS:
            # The runtime frames the body itself.
            continue
        headers.append((name, bytes(value).decode("latin-1")))
    return headers


class _Exchange:
    """One call of an ASGI app: its receive and send channels.

    ``send`` waits until the previous message has been taken, so a streaming
    app is paced by the client. Once the response is complete or abandoned,
    ``receive`` reports ``http.disconnect``.
    """

    def __init__(self, app, scope: dict[str, Any], body: bytes) -> None:
        self._body = body
        self._received = False
        self._queue: asyncio.Queue = asyncio.Queue(maxsize=1)
        self._finished = asyncio.Event()
        self.error: BaseException | None = None
        self.task = asyncio.get_running_loop().create_task(self._run(app, scope))

    async def _run(self, app, scope) -> None:
        try:
            await app(scope, self.receive, self.send)
        except asyncio.CancelledError:
            raise
        except BaseException as error:  # noqa: BLE001 -- Reported to the reader.
            self.error = error
        await self._queue.put(_DONE)

    async def receive(self) -> dict[str, Any]:
        if not self._received:
            self._received = True
            return {"type": "http.request", "body": self._body, "more_body": False}
        await self._finished.wait()
        return {"type": "http.disconnect"}

    async def send(self, message: dict[str, Any]) -> None:
        if self._finished.is_set():
            raise OSError("the response has already finished")
        await self._queue.put(message)

    async def _next(self):
        message = await self._queue.get()
        if message is _DONE and self.error is not None:
            # Raised once, and held nowhere afterwards: the traceback names
            # this exchange's frames, so keeping the error (here or in a
            # local) would leave a cycle through the request.
            error, self.error = self.error, None
            try:
                raise error
            finally:
                del error
        return message

    async def _close(self) -> None:
        self._finished.set()
        task = self.task
        if not task.done():
            task.cancel()
            with suppress(BaseException):
                await task

    async def _complete(self) -> None:
        """Let the app finish (Starlette runs response background tasks after
        the last body message) before the response is handed back."""
        self._finished.set()
        while (message := await self._next()) is not _DONE:
            if message.get("type") == "http.response.body" and message.get("body"):
                raise RuntimeError("an ASGI app sent body after the response ended")

    async def response(self) -> Response:
        try:
            return await self._response()
        except BaseException:
            await self._close()
            raise

    async def _response(self) -> Response:
        start = await self._next()
        if start is _DONE:
            raise RuntimeError("an ASGI app returned without sending a response")
        if start.get("type") != "http.response.start":
            raise RuntimeError("an ASGI app must send http.response.start first")
        status = start["status"]
        headers = _response_headers(start.get("headers", ()))
        chunks: list[bytes] = []
        while True:
            message = await self._next()
            if message is _DONE:
                break
            if message.get("type") != "http.response.body":
                raise RuntimeError("an ASGI app sent an unexpected response message")
            chunk = bytes(message.get("body", b""))
            if not message.get("more_body", False):
                chunks.append(chunk)
                await self._complete()
                break
            if chunk:
                chunks.append(chunk)
                return StreamingResponse(
                    _Body(self, chunks), status_code=status, headers=headers
                )
        response = Response(b"".join(chunks), status_code=status, headers=headers)
        return response


class _Body:
    """The rest of a streamed ASGI response, as an async iterator.

    Closing it, even before the first chunk was read, stops the app.
    """

    def __init__(self, exchange: _Exchange, first: list[bytes]) -> None:
        self._exchange = exchange
        self._first = first
        self._ended = False

    def __aiter__(self):
        return self

    async def __anext__(self) -> bytes:
        if self._first:
            return self._first.pop(0)
        exchange = self._exchange
        try:
            while not self._ended:
                message = await exchange._next()
                if message is _DONE:
                    break
                if message.get("type") != "http.response.body":
                    raise RuntimeError(
                        "an ASGI app sent an unexpected response message"
                    )
                chunk = bytes(message.get("body", b""))
                if not message.get("more_body", False):
                    self._ended = True
                    await exchange._complete()
                if chunk:
                    return chunk
        except BaseException:
            await exchange._close()
            raise
        self._ended = True
        await exchange._close()
        raise StopAsyncIteration

    async def aclose(self) -> None:
        self._ended = True
        await self._exchange._close()


async def call_asgi(app, scope: dict[str, Any], body: bytes) -> Response:
    """Run an ASGI app for one request and return its response.

    A response sent in one body message comes back buffered; one sent in
    several comes back as a ``StreamingResponse`` that keeps pulling from the
    app as the client reads. An exception before the response started is
    raised here, so it becomes the route's error.
    """
    return await _Exchange(app, scope, body).response()


def _encode_json(value: Any, limit: int | None) -> bytes:
    if _encode_plain_json is not None:
        encoded = _encode_plain_json(value, limit)
        if encoded is not None:
            return encoded
    return json.dumps(
        value, ensure_ascii=False, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")


async def send_envelope(
    envelope,
    send,
    limit: int | None,
    close_stream,
    *,
    worker=None,
    future=None,
    scope=None,
) -> None:
    """Send a route's native response envelope as ASGI messages.

    This is the innermost app under ASGI middleware, so the middleware sees a
    normal ASGI response: JSON is encoded here and a stream is pulled chunk
    by chunk.
    """
    if len(envelope) == 2:
        status, body = envelope
        raw, kind = (), "json"
    else:
        status, body, raw, kind = envelope
    headers = [
        (name.lower().encode("latin-1"), value.encode("latin-1")) for name, value in raw
    ]
    if kind in ("json", "json_bytes") and not any(
        name == b"content-type" for name, _ in headers
    ):
        headers.append(_JSON_TYPE)
    if kind == "stream":
        # The stream owns the route's dependency exit stack: if the client
        # leaves or middleware fails at any send, close it to completion
        # before the failure propagates.
        try:
            await send(
                {"type": "http.response.start", "status": status, "headers": headers}
            )
            while (chunk := await body._next()) is not None:
                await send(
                    {"type": "http.response.body", "body": chunk, "more_body": True}
                )
            await send({"type": "http.response.body", "body": b"", "more_body": False})
        except BaseException:
            await close_stream(body)
            raise
        return
    if kind == "file":
        try:
            # Select the representation before middleware sees response.start.
            # Both selection (including range seek) and reads use the worker's
            # cancellation-safe pool path: cancellation waits for actual I/O
            # completion before the finally block can close the handle.
            status, headers = await worker._call_sync(
                body.prepare,
                (status, headers, scope["headers"], scope["method"] == "HEAD"),
                {},
                future,
            )
            await send(
                {"type": "http.response.start", "status": status, "headers": headers}
            )
            while chunk := await worker._call_sync(body.read_chunk, (), {}, future):
                await send(
                    {
                        "type": "http.response.body",
                        "body": bytes(chunk),
                        "more_body": True,
                    }
                )
            await send({"type": "http.response.body", "body": b"", "more_body": False})
        finally:
            body.close()
        return
    if kind == "json":
        body = _encode_json(body, limit)
    else:
        body = bytes(body)
    headers.append((b"content-length", str(len(body)).encode("latin-1")))
    await send({"type": "http.response.start", "status": status, "headers": headers})
    await send({"type": "http.response.body", "body": body, "more_body": False})


class InnerApp:
    """The ASGI app at the centre of an ASGI middleware stack.

    It runs the route the native router already matched (with its HTTP
    middleware) for the request carried in the scope, after exposing the
    scope as middleware left it through ``request.scope``. An error that no
    exception handler answered is raised from here, after the route's
    dependencies have been torn down, so the middleware around it sees the
    exception as it would from a Starlette application.
    """

    def __init__(self, worker) -> None:
        self._worker = worker

    async def __call__(self, scope, receive, send) -> None:
        call = scope.get(CALL_KEY) if scope.get("type") == "http" else None
        if call is None:
            raise RuntimeError("Bessemer's ASGI middleware stack serves HTTP routes")
        route_id, arguments, future, request = call
        request._scope = scope
        request._receive = receive
        worker = self._worker
        # The route runs in this task, whose context is a copy of the
        # invocation's. Its dependency cleanup and any stream it starts must
        # run in the same context, or a ContextVar token reset fails.
        outer = future._context
        future._context = asyncio.current_task().get_context()
        try:
            envelope = await worker._invoke_routed(route_id, arguments, future, request)
        finally:
            request._receive = None
            future._context = outer
        await send_envelope(
            envelope,
            send,
            worker.max_response_size,
            worker._close_stream_shielded,
            worker=worker,
            future=future,
            scope=scope,
        )


def build_stack(app, inner):
    """Wrap ``inner`` in ``app``'s ASGI middleware, the last added outermost."""
    wrapped = inner
    for cls, args, kwargs in app._asgi_middleware:
        wrapped = cls(wrapped, *args, **kwargs)
    return wrapped


def mount_endpoint(asgi_app, prefix: str):
    """A route handler serving a mounted ASGI app below ``prefix``.

    As in Starlette, ``path`` stays the full path and ``root_path`` gains the
    mount prefix (with any prefix of the mounts above it).
    """
    from .request import Request

    async def endpoint(request):
        owner = request.app
        outer = getattr(request, "_scope", None)
        if outer is None:
            root = (owner._mount_prefix() if owner is not None else "") + prefix
            scope = build_scope(request, root_path=root)
        else:
            # Under ASGI middleware: keep what it added (session, user, ...).
            scope = dict(outer)
            scope["root_path"] = outer.get("root_path", "") + prefix
            scope.pop(CALL_KEY, None)
        return await call_asgi(asgi_app, scope, await request.body())

    endpoint.__annotations__ = {"request": Request}
    endpoint.__name__ = f"mount_{prefix.strip('/').replace('/', '_') or 'root'}"
    return endpoint
