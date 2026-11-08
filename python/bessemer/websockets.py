"""WebSocket sessions. Framing, ping/pong and the close handshake run in Rust."""

from __future__ import annotations

import asyncio
import collections
import enum
import functools
import json
from collections.abc import AsyncIterator, Iterable, Mapping
from typing import Any

from .request import Request
from .responses import jsonable

try:
    from ._native import _parse_json as _native_parse_json
except ImportError:
    _native_parse_json = None

_TEXT, _BINARY, _CLOSE = 1, 2, 8


class WebSocketState(enum.IntEnum):
    """Connection state matching Starlette's ``WebSocketState``."""

    CONNECTING = 0
    CONNECTED = 1
    DISCONNECTED = 2
    RESPONSE = 3


class WebSocketDisconnect(Exception):
    """The peer closed the connection, or it was lost (code 1006)."""

    def __init__(self, code: int = 1000, reason: str = "") -> None:
        super().__init__(code, reason)
        self.code = code
        self.reason = reason


class WebSocketException(Exception):
    """Raise in a WebSocket handler or dependency to close with ``code``.

    Before ``accept`` this refuses the handshake, as in FastAPI.
    """

    def __init__(self, code: int, reason: str | None = None) -> None:
        if type(code) is not int or not (
            # The codes the native transport sends (src/websocket.rs).
            1000 <= code <= 1003 or 1007 <= code <= 1014 or 3000 <= code <= 4999
        ):
            raise ValueError("WebSocketException needs a valid close code")
        reason = reason or ""
        if len(reason.encode("utf-8")) > 123:
            raise ValueError("WebSocket close reasons are at most 123 bytes")
        super().__init__(code, reason)
        self.code = code
        self.reason = reason


def _resolve(future: asyncio.Future) -> None:
    if not future.done():
        future.set_result(None)


def _on_loop(loop, callback, *args) -> None:
    # The Rust loop also serves the transport on its own thread. Native
    # delivery there can update loop-owned state directly; other I/O threads
    # still schedule the callback through the thread-safe queue.
    if asyncio._get_running_loop() is loop:
        callback(*args)
    else:
        loop.call_soon_threadsafe(callback, *args)


class WebSocket:
    """One accepted (or pending) WebSocket connection.

    Messages arrive whole and already validated: text is UTF-8 checked in Rust
    and fragmented messages are reassembled natively. ``send_*`` waits only
    when more than 1 MiB is queued for a slow client.
    """

    def __init__(self, channel, request: Request, loop: asyncio.AbstractEventLoop):
        self._channel = channel
        self.request = request
        self._loop = loop
        self._queue: collections.deque[tuple[int, Any]] = collections.deque()
        # Set when messages arrive; every concurrent receiver waits on it, so
        # none is stranded when another one starts waiting.
        self._arrived = asyncio.Event()
        self._accepted = False
        self._close_code: int | None = None
        self.client_state = WebSocketState.CONNECTING
        self.application_state = WebSocketState.CONNECTING

    @property
    def scope(self) -> dict[str, Any]:
        scope = getattr(self, "_scope", None)
        if scope is None:
            scope = dict(self.request.scope)
            scope["type"] = "websocket"
            scope["scheme"] = "wss" if self.request.scheme in ("https", "wss") else "ws"
            scope["subprotocols"] = [
                item.strip()
                for header in self.headers.getlist("sec-websocket-protocol")
                for item in header.split(",")
                if item.strip()
            ]
            self._scope = scope
        return scope

    @property
    def headers(self):
        return self.request.headers

    @property
    def path(self) -> str:
        return self.request.path

    @property
    def path_params(self) -> Mapping[str, str]:
        return self.request.path_params

    @property
    def query_params(self) -> Mapping[str, str]:
        return self.request.query_params

    @property
    def cookies(self) -> Mapping[str, str]:
        return self.request.cookies

    @property
    def client(self):
        return self.request.client

    @property
    def url(self):
        return self.request.url

    @property
    def base_url(self):
        return self.request.base_url

    @property
    def app(self):
        return self.request.app

    @property
    def state(self):
        """Per-connection state, as ``request.state`` is per request."""
        return self.request.state

    @property
    def client_certificate(self) -> bytes | None:
        """The verified TLS client certificate (leaf, DER), under mutual TLS."""
        return self.request.client_certificate

    def url_for(self, name: str, /, **path_params: Any) -> str:
        """Return the absolute URL of the route called ``name``."""
        return self.request.url_for(name, **path_params)

    @property
    def accepted(self) -> bool:
        return self._accepted

    async def accept(
        self,
        subprotocol: str | None = None,
        headers: Iterable[tuple[bytes | str, bytes | str]]
        | Mapping[str, str]
        | None = None,
    ) -> None:
        if self._accepted:
            raise RuntimeError("WebSocket is already accepted")
        raw_headers: list[tuple[bytes, bytes]] | None = None
        if headers is not None:
            pairs = headers.items() if isinstance(headers, Mapping) else headers
            raw_headers = [
                (
                    k.encode("latin-1") if isinstance(k, str) else bytes(k),
                    v.encode("latin-1") if isinstance(v, str) else bytes(v),
                )
                for k, v in pairs
            ]
        deliver = functools.partial(_on_loop, self._loop, self._push)
        if raw_headers:
            self._channel.accept(deliver, subprotocol, raw_headers)
        else:
            self._channel.accept(deliver, subprotocol)
        self._accepted = True
        self.client_state = WebSocketState.CONNECTED
        self.application_state = WebSocketState.CONNECTED

    def _push(self, batch) -> None:
        self._queue.extend(batch)
        self._arrived.set()

    async def _receive(self) -> tuple[int, Any]:
        if not self._accepted:
            raise RuntimeError("accept the WebSocket before receiving")
        while not self._queue:
            if self._close_code is not None:
                self.client_state = WebSocketState.DISCONNECTED
                raise WebSocketDisconnect(self._close_code)
            self._arrived.clear()
            await self._arrived.wait()
        kind, payload = self._queue.popleft()
        if kind == _CLOSE:
            self._close_code = payload
            self.client_state = WebSocketState.DISCONNECTED
            raise WebSocketDisconnect(payload)
        self._channel.ack(1)
        return kind, payload

    async def receive(self) -> str | bytes:
        """Return the next message as ``str`` (text) or ``bytes`` (binary)."""
        return (await self._receive())[1]

    async def receive_text(self) -> str:
        kind, payload = await self._receive()
        if kind != _TEXT:
            raise TypeError("expected a text WebSocket message")
        return payload

    async def receive_bytes(self) -> bytes:
        kind, payload = await self._receive()
        if kind != _BINARY:
            raise TypeError("expected a binary WebSocket message")
        return payload

    async def receive_json(self, mode: str = "text") -> Any:
        if mode not in {"text", "binary"}:
            raise ValueError("mode must be 'text' or 'binary'")
        kind, payload = await self._receive()
        data = payload.encode("utf-8") if kind == _TEXT else payload
        if _native_parse_json is not None:
            return _native_parse_json(data)
        return json.loads(data)

    async def _send(self, opcode: int, data: bytes) -> None:
        try:
            wait = self._channel.send(opcode, data)
        except RuntimeError:
            self.application_state = WebSocketState.DISCONNECTED
            raise WebSocketDisconnect(self._close_code or 1006) from None
        if wait:
            future = self._loop.create_future()
            self._channel.on_drain(
                functools.partial(_on_loop, self._loop, _resolve, future)
            )
            await future

    async def send(self, message: Mapping[str, Any]) -> None:
        """Send an ASGI WebSocket message (``websocket.accept``, ``websocket.send`` or ``websocket.close``)."""
        msg_type = message.get("type")
        if msg_type == "websocket.accept":
            await self.accept(
                subprotocol=message.get("subprotocol"),
                headers=message.get("headers"),
            )
            return
        if msg_type == "websocket.close":
            await self.close(
                code=message.get("code", 1000),
                reason=message.get("reason") or "",
            )
            return
        if msg_type == "websocket.send":
            if message.get("text") is not None:
                await self.send_text(message["text"])
            elif message.get("bytes") is not None:
                await self.send_bytes(message["bytes"])
            else:
                raise ValueError("websocket.send message requires 'text' or 'bytes'")
            return
        raise ValueError(f"Unsupported WebSocket ASGI message type: {msg_type!r}")

    async def send_text(self, data: str) -> None:
        if not isinstance(data, str):
            raise TypeError("send_text requires str")
        await self._send(_TEXT, data.encode("utf-8"))

    async def send_bytes(self, data: bytes | bytearray | memoryview) -> None:
        if not isinstance(data, (bytes, bytearray, memoryview)):
            raise TypeError("send_bytes requires bytes")
        await self._send(_BINARY, bytes(data))

    async def send_json(self, data: Any, mode: str = "text") -> None:
        if mode not in {"text", "binary"}:
            raise ValueError("mode must be 'text' or 'binary'")
        encoded = json.dumps(
            jsonable(data), ensure_ascii=False, allow_nan=False, separators=(",", ":")
        ).encode("utf-8")
        await self._send(_TEXT if mode == "text" else _BINARY, encoded)

    async def close(self, code: int = 1000, reason: str = "") -> None:
        """Close the session; before ``accept`` this denies the handshake (403)."""
        try:
            self._channel.close(code, reason)
        except RuntimeError as error:
            raise ValueError(str(error)) from None
        self.application_state = WebSocketState.DISCONNECTED

    async def iter_text(self) -> AsyncIterator[str]:
        try:
            while True:
                yield await self.receive_text()
        except WebSocketDisconnect:
            return

    async def iter_bytes(self) -> AsyncIterator[bytes]:
        try:
            while True:
                yield await self.receive_bytes()
        except WebSocketDisconnect:
            return

    async def iter_json(self) -> AsyncIterator[Any]:
        try:
            while True:
                yield await self.receive_json()
        except WebSocketDisconnect:
            return
