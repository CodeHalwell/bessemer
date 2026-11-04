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

import http.client
import http.cookiejar
import json as _json
import os
import select
import socket
import threading
import urllib.parse
import urllib.request
from collections.abc import Iterable, Iterator, Mapping, MutableMapping
from email.message import Message
from http.cookies import SimpleCookie
from typing import Any, Self

__all__ = ["TestClient", "TestResponse"]

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
        request_headers = {name.lower(): value for name, value in self.headers.items()}
        request_headers.update(
            {name.lower(): value for name, value in (headers or {}).items()}
        )
        body: bytes | None = None
        if json is not _MISSING:
            body = _json.dumps(json, separators=(",", ":")).encode()
            request_headers.setdefault("content-type", "application/json")
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

    def _send(self, method, origin, target, headers, body, cookies, timeout, history):
        headers = dict(headers)
        headers["host"] = self._host
        scoped = urllib.request.Request(origin + target)
        self.cookies.jar.add_cookie_header(scoped)
        pairs = [
            tuple(pair.split("=", 1)) if "=" in pair else (pair, "")
            for pair in filter(None, (scoped.get_header("Cookie") or "").split("; "))
        ]
        if cookies:
            pairs = [(name, value) for name, value in pairs if name not in cookies]
            pairs.extend(cookies.items())
        if pairs:
            headers["cookie"] = "; ".join(f"{name}={value}" for name, value in pairs)
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
