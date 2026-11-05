"""An in-process HTTP test client that exercises the real native server.

``TestClient`` starts the application on a private loopback socket with the
same native request path, controls and worker as ``App.run``, and sends real
HTTP/1.1 requests to it. Use it as a context manager so lifespan startup and
shutdown run around the tests::

    from bessemer.testclient import TestClient

    with TestClient(app) as client:
        response = client.get("/items/1")
        assert response.status_code == 200
        assert response.json() == {"id": 1}
"""

from __future__ import annotations

import base64
import hashlib
import http.client
import http.cookiejar
import json as _json
import os
import select
import socket
import struct
import threading
import types
import urllib.parse
import urllib.request
import uuid
from collections.abc import Iterable, Iterator, Mapping, MutableMapping
from email.message import Message
from http.cookies import SimpleCookie
from typing import Any, Self

from .websockets import WebSocketDisconnect

__all__ = [
    "TestClient",
    "TestResponse",
    "WebSocketDenied",
    "WebSocketTestSession",
]

_MISSING: Any = object()
_REDIRECTS = frozenset({301, 302, 303, 307, 308})
# Only these may be sent again after a kept-alive connection drops mid-request.
_IDEMPOTENT = frozenset({"GET", "HEAD", "OPTIONS", "TRACE", "PUT", "DELETE"})
_STALE = (
    http.client.RemoteDisconnected,
    ConnectionResetError,
    BrokenPipeError,
    ConnectionAbortedError,
)


def _multipart(data: Mapping[str, Any], files) -> tuple[bytes, str]:
    """Encode fields and files as multipart/form-data, like httpx's ``files=``.

    Each file is ``content``, ``(filename, content)`` or
    ``(filename, content, content_type)``; content is bytes, str or a binary
    file object. Pass a list of ``(name, file)`` pairs to repeat a name.
    """
    boundary = uuid.uuid4().hex
    chunks: list[bytes] = []

    def part(disposition: str, content: bytes, content_type: str | None) -> None:
        chunks.append(
            f"--{boundary}\r\nContent-Disposition: form-data; {disposition}\r\n".encode()
        )
        if content_type:
            chunks.append(f"Content-Type: {content_type}\r\n".encode())
        chunks.extend((b"\r\n", content, b"\r\n"))

    def quoted(value: str) -> str:
        return value.replace("\\", "\\\\").replace('"', '\\"')

    for name, value in data.items():
        for item in value if isinstance(value, (list, tuple)) else [value]:
            part(f'name="{quoted(name)}"', str(item).encode(), None)
    pairs = files.items() if isinstance(files, Mapping) else files
    for name, value in pairs:
        filename, content, content_type = "upload", value, None
        if isinstance(value, tuple):
            filename, content, *rest = value
            content_type = rest[0] if rest else None
        if hasattr(content, "read"):
            content = content.read()
        if isinstance(content, str):
            content = content.encode()
        part(
            f'name="{quoted(name)}"; filename="{quoted(filename)}"',
            bytes(content),
            content_type or "application/octet-stream",
        )
    chunks.append(f"--{boundary}--\r\n".encode())
    return b"".join(chunks), f"multipart/form-data; boundary={boundary}"


class TestResponse:
    """A buffered response: status, headers, body and helpers."""

    __test__ = False  # Not a pytest test class.

    def __init__(
        self,
        method: str,
        url: str,
        status_code: int,
        reason: str,
        headers: Message,
        content: bytes,
        history: list[TestResponse],
    ) -> None:
        self.method = method
        self.url = url
        self.status_code = status_code
        self.reason_phrase = reason
        self.headers = headers
        self.content = content
        self.history = history

    @property
    def text(self) -> str:
        charset = self.headers.get_content_charset() or "utf-8"
        return self.content.decode(charset, errors="replace")

    def json(self) -> Any:
        return _json.loads(self.content)

    @property
    def cookies(self) -> dict[str, str]:
        jar: dict[str, str] = {}
        for value in self.headers.get_all("set-cookie") or ():
            parsed = SimpleCookie()
            parsed.load(value)
            for name, morsel in parsed.items():
                jar[name] = morsel.value
        return jar

    @property
    def is_success(self) -> bool:
        return 200 <= self.status_code < 300

    @property
    def is_redirect(self) -> bool:
        return self.status_code in _REDIRECTS and "location" in self.headers

    def raise_for_status(self) -> TestResponse:
        if self.status_code >= 400:
            raise AssertionError(
                f"{self.method} {self.url} returned {self.status_code}: "
                f"{self.text[:200]}"
            )
        return self

    def __repr__(self) -> str:
        return f"<TestResponse [{self.status_code}]>"


def _peer_closed(connection: http.client.HTTPConnection) -> bool:
    """Whether an idle kept-alive socket was closed by the server."""
    sock = connection.sock
    if sock is None:
        return False
    try:
        readable, _, _ = select.select([sock], [], [], 0)
    except (OSError, ValueError):
        return True
    # An idle HTTP connection has nothing to read unless the peer closed it.
    return bool(readable)


class Cookies(MutableMapping[str, str]):
    """The client's cookie jar, scoped by domain, path and Secure like a browser.

    Reading by name returns the most specific cookie with that name; setting
    one stores it for the client's host and every path, and deleting a name
    removes it from every scope.
    """

    def __init__(self, host: str) -> None:
        self.jar = http.cookiejar.CookieJar()
        # The stdlib jar files host-only cookies for a dotless host under
        # "<host>.local", so cookies set here must use the same key.
        self._host = host if "." in host else f"{host}.local"

    def _named(self, name: str) -> list[http.cookiejar.Cookie]:
        found = [cookie for cookie in self.jar if cookie.name == name]
        return sorted(found, key=lambda cookie: len(cookie.path), reverse=True)

    def __getitem__(self, name: str) -> str:
        found = self._named(name)
        if not found:
            raise KeyError(name)
        return found[0].value or ""

    def __setitem__(self, name: str, value: str) -> None:
        self.jar.set_cookie(
            http.cookiejar.Cookie(
                0, name, str(value), None, False, self._host, False, False,
                "/", True, False, None, False, None, None, {},
            )
        )  # fmt: skip

    def __delitem__(self, name: str) -> None:
        found = self._named(name)
        if not found:
            raise KeyError(name)
        for cookie in found:
            self.jar.clear(cookie.domain, cookie.path, cookie.name)

    def __iter__(self) -> Iterator[str]:
        return iter(dict.fromkeys(cookie.name for cookie in self.jar))

    def __len__(self) -> int:
        return len(dict.fromkeys(cookie.name for cookie in self.jar))

    def __repr__(self) -> str:
        return f"<Cookies {dict(self)!r}>"


class _Received:
    """The response shape ``CookieJar.extract_cookies`` reads."""

    def __init__(self, headers: Message) -> None:
        self._headers = headers

    def info(self) -> Message:
        return self._headers


def _drain(fd: int) -> None:
    try:
        while os.read(fd, 4096):
            pass
    finally:
        os.close(fd)


class TestClient:
    """Send requests to an application served in this process.

    ``base_url`` sets the Host header and the URL that relative paths resolve
    against; add its host to ``allowed_hosts`` when the app restricts hosts.
    Cookies set by responses are kept and sent on later requests whose path,
    domain and scheme match, as a browser would; ``Secure`` cookies need an
    ``https`` base URL. Redirects to the same host are followed unless
    ``follow_redirects=False``. Keyword arguments in ``server_options`` are
    passed to the server like ``App.run`` limits, for example
    ``{"max_body_size": 4096}``. ``app.dependency_overrides`` take effect when
    the client starts.
    """

    __test__ = False  # Not a pytest test class.

    def __init__(
        self,
        app,
        base_url: str = "http://testserver",
        *,
        headers: Mapping[str, str] | None = None,
        cookies: Mapping[str, str] | None = None,
        follow_redirects: bool = True,
        timeout: float = 30.0,
        server_options: Mapping[str, Any] | None = None,
    ) -> None:
        parsed = urllib.parse.urlsplit(base_url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise ValueError("base_url must be an absolute http(s) URL")
        self.app = app
        self.base_url = base_url.rstrip("/")
        self._host = parsed.netloc
        self._origin = f"{parsed.scheme}://{parsed.netloc}"
        self._prefix = parsed.path.rstrip("/")
        self.headers: dict[str, str] = dict(headers or {})
        self.cookies = Cookies(parsed.hostname or "testserver")
        self.cookies.update(cookies or {})
        self.follow_redirects = follow_redirects
        self.timeout = timeout
        self._options = dict(server_options or {})
        for reserved in ("host", "port", "listener_fd", "ready_fd", "max_requests"):
            if reserved in self._options:
                raise ValueError(f"server_options cannot set {reserved}")
        self._lock = threading.Lock()
        self._connection: http.client.HTTPConnection | None = None
        self._thread: threading.Thread | None = None
        self._stop: socket.socket | None = None
        self._port: int | None = None
        self._error: BaseException | None = None

    # Lifecycle -----------------------------------------------------------

    def __enter__(self) -> Self:
        self.start()
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    def start(self) -> None:
        """Run lifespan startup and begin serving; ``with`` calls this."""
        if self._thread is not None:
            return
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", 0))
        listener.listen(1024)
        self._port = listener.getsockname()[1]
        stop, server_end = socket.socketpair()
        # The native server writes "1" here once it accepts connections, and
        # the worker writes heartbeats; EOF means the server thread finished.
        ready_read, ready_write = os.pipe()

        def serve() -> None:
            try:
                self.app._run(
                    **self._options,
                    host="127.0.0.1",
                    port=self._port,
                    listener_fd=listener.fileno(),
                    ready_fd=ready_write,
                    shutdown_fd=server_end.fileno(),
                )
            except BaseException as error:  # noqa: BLE001 -- Reported to the caller.
                self._error = error
            finally:
                listener.close()
                server_end.close()
                os.close(ready_write)

        self._stop = stop
        self._thread = threading.Thread(
            target=serve, name="bessemer-testclient", daemon=True
        )
        self._thread.start()
        ready = False
        while not ready:
            chunk = os.read(ready_read, 64)
            if not chunk:
                break
            ready = b"1" in chunk
        if not ready:
            os.close(ready_read)
            self._thread.join()
            self._thread = None
            self._stop = None
            stop.close()
            error, self._error = self._error, None
            raise error or RuntimeError("the server stopped before it was ready")
        # Keep draining heartbeats so the worker never sees a closed channel.
        threading.Thread(
            target=_drain, args=(ready_read,), name="bessemer-testclient-ready",
            daemon=True,
        ).start()  # fmt: skip

    def close(self) -> None:
        """Stop serving and run lifespan shutdown."""
        with self._lock:
            if self._connection is not None:
                self._connection.close()
                self._connection = None
        if self._thread is None:
            return
        assert self._stop is not None
        try:
            self._stop.sendall(b"x")
        except OSError:
            pass
        self._thread.join()
        self._stop.close()
        self._thread = None
        self._stop = None
        if self._error is not None:
            error, self._error = self._error, None
            raise error

    # Requests ------------------------------------------------------------

    def request(
        self,
        method: str,
        url: str,
        *,
        params: Mapping[str, Any] | Iterable[tuple[str, Any]] | None = None,
        headers: Mapping[str, str] | None = None,
        cookies: Mapping[str, str] | None = None,
        json: Any = _MISSING,
        content: bytes | str | None = None,
        data: Mapping[str, Any] | None = None,
        files: Mapping[str, Any] | Iterable[tuple[str, Any]] | None = None,
        follow_redirects: bool | None = None,
        timeout: float | None = None,
    ) -> TestResponse:
        if self._thread is None:
            raise RuntimeError(
                "TestClient is not running; use it as a context manager "
                "(with TestClient(app) as client: ...) or call start()"
            )
        if (
            sum(value is not None for value in (content, data)) + (json is not _MISSING)
            > 1
        ):
            raise ValueError("pass at most one of json, content and data")
        if files is not None and (content is not None or json is not _MISSING):
            raise ValueError("files combine only with data, not json or content")
        request_headers = {name.lower(): value for name, value in self.headers.items()}
        request_headers.update(
            {name.lower(): value for name, value in (headers or {}).items()}
        )
        body: bytes | None = None
        if json is not _MISSING:
            body = _json.dumps(json, separators=(",", ":")).encode()
            request_headers.setdefault("content-type", "application/json")
        elif files is not None:
            body, content_type = _multipart(data or {}, files)
            request_headers["content-type"] = content_type
        elif data is not None:
            body = urllib.parse.urlencode(data, doseq=True).encode()
            request_headers.setdefault(
                "content-type", "application/x-www-form-urlencoded"
            )
        elif content is not None:
            body = content.encode() if isinstance(content, str) else bytes(content)
        origin, target = self._target(url, params)
        follow = self.follow_redirects if follow_redirects is None else follow_redirects
        history: list[TestResponse] = []
        method = method.upper()
        while True:
            response = self._send(
                method, origin, target, request_headers, body, cookies, timeout,
                history,
            )  # fmt: skip
            if not (follow and response.is_redirect) or len(history) >= 20:
                return response
            location = urllib.parse.urljoin(
                origin + target, response.headers["location"]
            )
            parsed = urllib.parse.urlsplit(location)
            if parsed.netloc != self._host or parsed.scheme not in ("http", "https"):
                return response  # Never leave the application under test.
            # A same-host redirect may change scheme, which scopes Secure cookies.
            origin = f"{parsed.scheme}://{parsed.netloc}"
            history.append(response)
            target = urllib.parse.urlunsplit(
                ("", "", parsed.path or "/", parsed.query, "")
            )
            if response.status_code == 303 or (
                response.status_code in (301, 302) and method == "POST"
            ):
                method = "GET"
                body = None
                for name in ("content-type", "content-length", "transfer-encoding"):
                    request_headers.pop(name, None)

    def get(self, url: str, **kwargs) -> TestResponse:
        return self.request("GET", url, **kwargs)

    def head(self, url: str, **kwargs) -> TestResponse:
        return self.request("HEAD", url, **kwargs)

    def options(self, url: str, **kwargs) -> TestResponse:
        return self.request("OPTIONS", url, **kwargs)

    def post(self, url: str, **kwargs) -> TestResponse:
        return self.request("POST", url, **kwargs)

    def put(self, url: str, **kwargs) -> TestResponse:
        return self.request("PUT", url, **kwargs)

    def patch(self, url: str, **kwargs) -> TestResponse:
        return self.request("PATCH", url, **kwargs)

    def delete(self, url: str, **kwargs) -> TestResponse:
        return self.request("DELETE", url, **kwargs)

    def websocket_connect(
        self,
        url: str,
        *,
        subprotocols: Iterable[str] | None = None,
        headers: Mapping[str, str] | None = None,
        cookies: Mapping[str, str] | None = None,
        params: Mapping[str, Any] | Iterable[tuple[str, Any]] | None = None,
        timeout: float | None = None,
    ) -> WebSocketTestSession:
        """Open a WebSocket session to the application, as in Starlette.

        The handshake goes through the real native server on its own
        connection, with the client's default headers and cookies. A
        handshake the application refuses raises ``WebSocketDenied``, which
        carries the HTTP response. Use the session as a context manager so
        it is closed afterwards.
        """
        if self._thread is None:
            raise RuntimeError(
                "TestClient is not running; use it as a context manager "
                "(with TestClient(app) as client: ...) or call start()"
            )
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme in ("ws", "wss"):
            scheme = "https" if parsed.scheme == "wss" else "http"
            url = urllib.parse.urlunsplit((scheme, *parsed[1:]))
        origin, target = self._target(url, params)
        request_headers = {name.lower(): value for name, value in self.headers.items()}
        request_headers.update(
            {name.lower(): value for name, value in (headers or {}).items()}
        )
        cookie = self._cookie_header(origin, target, cookies)
        if cookie is not None:
            request_headers["cookie"] = cookie
        if subprotocols is not None:
            if isinstance(subprotocols, str):
                raise TypeError("subprotocols must be an iterable of strings")
            request_headers["sec-websocket-protocol"] = ", ".join(subprotocols)
        return WebSocketTestSession(
            self._port,
            self._host,
            origin + target,
            target,
            request_headers,
            self.timeout if timeout is None else timeout,
        )

    # Internals -----------------------------------------------------------

    def _target(self, url: str, params) -> tuple[str, str]:
        """The request's origin, scheme included, and its origin-form target."""
        origin = self._origin
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme or parsed.netloc:
            if parsed.netloc != self._host or parsed.scheme not in ("http", "https"):
                raise ValueError("TestClient only sends requests to its base_url host")
            origin = f"{parsed.scheme}://{parsed.netloc}"
            path = parsed.path
        else:
            path = self._prefix + ("/" + url.lstrip("/") if url else "/")
            parsed = urllib.parse.urlsplit(path)
            path = parsed.path
        query = parsed.query
        if params:
            extra = urllib.parse.urlencode(
                list(params.items()) if isinstance(params, Mapping) else list(params),
                doseq=True,
            )
            query = f"{query}&{extra}" if query else extra
        return origin, urllib.parse.urlunsplit(("", "", path or "/", query, ""))

    def _cookie_header(self, origin, target, cookies) -> str | None:
        scoped = urllib.request.Request(origin + target)
        self.cookies.jar.add_cookie_header(scoped)
        pairs = [
            tuple(pair.split("=", 1)) if "=" in pair else (pair, "")
            for pair in filter(None, (scoped.get_header("Cookie") or "").split("; "))
        ]
        if cookies:
            pairs = [(name, value) for name, value in pairs if name not in cookies]
            pairs.extend(cookies.items())
        if not pairs:
            return None
        return "; ".join(f"{name}={value}" for name, value in pairs)

    def _send(self, method, origin, target, headers, body, cookies, timeout, history):
        headers = dict(headers)
        headers["host"] = self._host
        cookie = self._cookie_header(origin, target, cookies)
        if cookie is not None:
            headers["cookie"] = cookie
        if body is not None:
            headers["content-length"] = str(len(body))
        with self._lock:
            for attempt in (0, 1):
                connection = self._connection
                if connection is not None and _peer_closed(connection):
                    connection.close()
                    connection = self._connection = None
                fresh = connection is None
                effective = self.timeout if timeout is None else timeout
                if connection is None:
                    connection = http.client.HTTPConnection(
                        "127.0.0.1", self._port, timeout=effective
                    )
                    self._connection = connection
                else:
                    connection.timeout = effective
                    if connection.sock is not None:
                        connection.sock.settimeout(effective)
                try:
                    connection.request(method, target, body=body, headers=headers)
                    raw = connection.getresponse()
                    content = raw.read()
                except _STALE:
                    connection.close()
                    self._connection = None
                    if fresh or attempt or method.upper() not in _IDEMPOTENT:
                        raise
                    continue  # A kept-alive connection the server had closed.
                except BaseException:
                    connection.close()
                    self._connection = None
                    raise
                if raw.will_close:
                    connection.close()
                    self._connection = None
                break
        response = TestResponse(
            method,
            origin + target,
            raw.status,
            raw.reason,
            raw.headers,
            content,
            list(history),
        )
        self.cookies.jar.extract_cookies(
            _Received(response.headers),
            urllib.request.Request(origin + target, method=method),
        )
        return response


_WEBSOCKET_GUID = b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
_TEXT, _BINARY, _CLOSE, _PING, _PONG = 0x1, 0x2, 0x8, 0x9, 0xA


class WebSocketDenied(WebSocketDisconnect):
    """The application refused the WebSocket handshake with an HTTP response.

    It is a ``WebSocketDisconnect`` with code 1008, so tests written for
    either kind of refusal catch it; ``response`` holds the HTTP response.
    """

    def __init__(self, response: TestResponse) -> None:
        super().__init__(1008, f"HTTP {response.status_code}")
        self.response = response


class WebSocketTestSession:
    """A client WebSocket connection to the application under test.

    Messages are whole: fragmented messages are reassembled and pings are
    answered. Receiving after the application closes raises
    ``WebSocketDisconnect`` with its close code and reason.
    """

    __test__ = False

    def __init__(self, port, host, url, target, headers, timeout) -> None:
        self._socket = socket.create_connection(("127.0.0.1", port), timeout=timeout)
        self._closed = False
        self._close_sent = False
        try:
            key = base64.b64encode(os.urandom(16)).decode()
            lines = [
                f"GET {target} HTTP/1.1",
                f"Host: {host}",
                "Upgrade: websocket",
                "Connection: Upgrade",
                f"Sec-WebSocket-Key: {key}",
                "Sec-WebSocket-Version: 13",
            ]
            reserved = {
                "host",
                "upgrade",
                "connection",
                "sec-websocket-key",
                "sec-websocket-version",
            }
            lines += [
                f"{name}: {value}"
                for name, value in headers.items()
                if name not in reserved
            ]
            self._socket.sendall(("\r\n".join(lines) + "\r\n\r\n").encode("latin-1"))
            self._file = self._socket.makefile("rb")
            status_line = self._file.readline(65537).decode("latin-1")
            parts = status_line.split(" ", 2)
            if len(parts) < 2 or not parts[1].isdigit():
                raise RuntimeError(f"invalid handshake response: {status_line!r}")
            status = int(parts[1])
            reason = parts[2].strip() if len(parts) > 2 else ""
            response_headers = http.client.parse_headers(self._file)
            if status != 101:
                length = response_headers.get("content-length")
                content = self._file.read(int(length)) if length else b""
                response = TestResponse(
                    "GET", url, status, reason, response_headers, content, []
                )
                raise WebSocketDenied(response)
            raw = types.SimpleNamespace(headers=response_headers)
            expected = base64.b64encode(
                hashlib.sha1(key.encode() + _WEBSOCKET_GUID).digest()
            ).decode()
            if raw.headers.get("sec-websocket-accept") != expected:
                raise RuntimeError("the server sent an invalid WebSocket accept key")
            self.headers = raw.headers
            self.accepted_subprotocol = raw.headers.get("sec-websocket-protocol")
        except BaseException:
            if hasattr(self, "_file"):
                self._file.close()
            self._socket.close()
            self._closed = True
            raise

    # Context management --------------------------------------------------

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    # Sending ---------------------------------------------------------------

    def send_text(self, data: str) -> None:
        self._send_frame(_TEXT, data.encode("utf-8"))

    def send_bytes(self, data: bytes) -> None:
        self._send_frame(_BINARY, bytes(data))

    def send_json(self, data: Any, mode: str = "text") -> None:
        encoded = _json.dumps(data, separators=(",", ":"))
        if mode == "text":
            self.send_text(encoded)
        elif mode == "binary":
            self.send_bytes(encoded.encode("utf-8"))
        else:
            raise ValueError('mode must be "text" or "binary"')

    def close(self, code: int = 1000, reason: str = "") -> None:
        """Send a close frame, wait briefly for the reply, and disconnect."""
        if self._closed:
            return
        try:
            if not self._close_sent:
                self._send_close(code, reason)
            # Read until the server's close frame or EOF so it sees a clean
            # close rather than a reset.
            while True:
                header, _ = self._read_frame()
                if header is None or header & 0x0F == _CLOSE:
                    break
        except (OSError, ValueError):
            pass
        finally:
            self._closed = True
            self._file.close()
            self._socket.close()

    # Receiving -------------------------------------------------------------

    def receive(self) -> str | bytes:
        """The next whole message: ``str`` for text and ``bytes`` for binary."""
        if self._closed:
            raise RuntimeError("the WebSocket session is closed")
        while True:
            opcode, payload = self._read_message()
            if opcode == _TEXT:
                return payload.decode("utf-8")
            if opcode == _BINARY:
                return payload
            # A close frame (or a lost connection) ends the session.
            code, reason = 1006, ""
            if opcode == _CLOSE:
                code = (
                    struct.unpack("!H", payload[:2])[0] if len(payload) >= 2 else 1005
                )
                reason = payload[2:].decode("utf-8", "replace")
                if not self._close_sent:
                    try:
                        self._send_close(code if code not in (1005, 1006) else 1000)
                    except OSError:
                        pass
            self._closed = True
            self._file.close()
            self._socket.close()
            raise WebSocketDisconnect(code, reason)

    def receive_text(self) -> str:
        message = self.receive()
        if not isinstance(message, str):
            raise TypeError("expected a text message, received binary")
        return message

    def receive_bytes(self) -> bytes:
        message = self.receive()
        if not isinstance(message, bytes):
            raise TypeError("expected a binary message, received text")
        return message

    def receive_json(self, mode: str = "text") -> Any:
        if mode == "text":
            return _json.loads(self.receive_text())
        if mode == "binary":
            return _json.loads(self.receive_bytes())
        raise ValueError('mode must be "text" or "binary"')

    # Framing -----------------------------------------------------------

    def _send_close(self, code: int, reason: str = "") -> None:
        self._close_sent = True
        self._send_frame(_CLOSE, struct.pack("!H", code) + reason.encode("utf-8"))

    def _send_frame(self, opcode: int, payload: bytes) -> None:
        if self._closed or (self._close_sent and opcode != _CLOSE):
            raise RuntimeError("the WebSocket session is closed")
        length = len(payload)
        if length < 126:
            header = struct.pack("!BB", 0x80 | opcode, 0x80 | length)
        elif length < 1 << 16:
            header = struct.pack("!BBH", 0x80 | opcode, 0x80 | 126, length)
        else:
            header = struct.pack("!BBQ", 0x80 | opcode, 0x80 | 127, length)
        # Client frames are always masked (RFC 6455, section 5.3).
        mask = os.urandom(4)
        repeated = (mask * (length // 4 + 1))[:length]
        masked = (
            int.from_bytes(payload, "big") ^ int.from_bytes(repeated, "big")
        ).to_bytes(length, "big")
        self._socket.sendall(header + mask + masked)

    def _read_exact(self, size: int) -> bytes | None:
        data = self._file.read(size)
        if len(data) < size:
            return None
        return data

    def _read_frame(self) -> tuple[int | None, bytes]:
        head = self._read_exact(2)
        if head is None:
            return None, b""
        first, second = head
        length = second & 0x7F
        if length == 126:
            extended = self._read_exact(2)
            if extended is None:
                return None, b""
            length = struct.unpack("!H", extended)[0]
        elif length == 127:
            extended = self._read_exact(8)
            if extended is None:
                return None, b""
            length = struct.unpack("!Q", extended)[0]
        if second & 0x80:
            raise ValueError("the server sent a masked frame")
        payload = self._read_exact(length) if length else b""
        if payload is None:
            return None, b""
        return first, payload

    def _read_message(self) -> tuple[int | None, bytes]:
        opcode: int | None = None
        parts: list[bytes] = []
        while True:
            header, payload = self._read_frame()
            if header is None:
                return None, b""
            frame_opcode, final = header & 0x0F, bool(header & 0x80)
            if frame_opcode == _PING:
                self._send_frame(_PONG, payload)
                continue
            if frame_opcode == _PONG:
                continue
            if frame_opcode == _CLOSE:
                return _CLOSE, payload
            if frame_opcode != 0:
                opcode = frame_opcode
            parts.append(payload)
            if final:
                return opcode, b"".join(parts)
