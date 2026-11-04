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
import json as _json
import socket
import threading
import urllib.parse
from collections.abc import Iterable, Mapping
from email.message import Message
from http.cookies import SimpleCookie
from typing import Any, Self

__all__ = ["TestClient", "TestResponse"]

_MISSING: Any = object()
_REDIRECTS = frozenset({301, 302, 303, 307, 308})
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


class TestClient:
    """Send requests to an application served in this process.

    ``base_url`` sets the Host header and the URL that relative paths resolve
    against; add its host to ``allowed_hosts`` when the app restricts hosts.
    Cookies set by responses are kept and sent on later requests, as a
    browser would. Redirects to the same host are followed unless
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
        self.cookies: dict[str, str] = dict(cookies or {})
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
        started = threading.Event()

        def serve() -> None:
            try:
                self.app._run(
                    **self._options,
                    host="127.0.0.1",
                    port=self._port,
                    listener_fd=listener.fileno(),
                    shutdown_fd=server_end.fileno(),
                    started=started.set,
                )
            except BaseException as error:  # noqa: BLE001 -- Reported to the caller.
                self._error = error
            finally:
                started.set()
                listener.close()
                server_end.close()

        self._stop = stop
        self._thread = threading.Thread(
            target=serve, name="bessemer-testclient", daemon=True
        )
        self._thread.start()
        started.wait()
        if self._error is not None:
            self._thread.join()
            self._thread = None
            stop.close()
            error, self._error = self._error, None
            raise error

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
        target = self._target(url, params)
        follow = self.follow_redirects if follow_redirects is None else follow_redirects
        history: list[TestResponse] = []
        method = method.upper()
        while True:
            response = self._send(
                method, target, request_headers, body, cookies, timeout, history
            )
            if not (follow and response.is_redirect) or len(history) >= 20:
                return response
            location = urllib.parse.urljoin(
                self._origin + target, response.headers["location"]
            )
            parsed = urllib.parse.urlsplit(location)
            if parsed.netloc != self._host:
                return response  # Never leave the application under test.
            history.append(response)
            target = urllib.parse.urlunsplit(
                ("", "", parsed.path or "/", parsed.query, "")
            )
            if response.status_code == 303 or (
                response.status_code in (301, 302) and method == "POST"
            ):
                method = "GET"
                body = None
                request_headers.pop("content-type", None)

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

    def _target(self, url: str, params) -> str:
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme or parsed.netloc:
            if parsed.netloc != self._host:
                raise ValueError("TestClient only sends requests to its base_url host")
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
        return urllib.parse.urlunsplit(("", "", path or "/", query, ""))

    def _send(self, method, target, headers, body, cookies, timeout, history):
        headers = dict(headers)
        headers["host"] = self._host
        jar = {**self.cookies, **(cookies or {})}
        if jar:
            headers["cookie"] = "; ".join(
                f"{name}={value}" for name, value in jar.items()
            )
        if body is not None:
            headers["content-length"] = str(len(body))
        with self._lock:
            for attempt in (0, 1):
                connection = self._connection
                fresh = connection is None
                if connection is None:
                    connection = http.client.HTTPConnection(
                        "127.0.0.1",
                        self._port,
                        timeout=self.timeout if timeout is None else timeout,
                    )
                    self._connection = connection
                elif timeout is not None:
                    connection.timeout = timeout
                    if connection.sock is not None:
                        connection.sock.settimeout(timeout)
                try:
                    connection.request(method, target, body=body, headers=headers)
                    raw = connection.getresponse()
                    content = raw.read()
                except _STALE:
                    connection.close()
                    self._connection = None
                    if fresh or attempt:
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
            self._origin + target,
            raw.status,
            raw.reason,
            raw.headers,
            content,
            list(history),
        )
        self._store_cookies(response.headers)
        return response

    def _store_cookies(self, headers: Message) -> None:
        for value in headers.get_all("set-cookie") or ():
            parsed = SimpleCookie()
            try:
                parsed.load(value)
            except Exception:  # noqa: BLE001, S112 -- Ignore cookies the stdlib cannot parse.
                continue
            for name, morsel in parsed.items():
                expired = morsel["max-age"] in ("0", "-1") or (
                    morsel["expires"] and "1970" in morsel["expires"]
                )
                if expired or morsel.value == "":
                    self.cookies.pop(name, None)
                else:
                    self.cookies[name] = morsel.value
