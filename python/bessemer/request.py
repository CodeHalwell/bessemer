"""Compatibility request context and request-scoped background work."""

from __future__ import annotations

import contextvars
import io
import json
import math
import os
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from types import MappingProxyType, SimpleNamespace
from typing import Any
from urllib.parse import parse_qsl

try:
    from ._native import _parse_json as _native_parse_json
except ImportError:
    _native_parse_json = None


class RequestValidationError(ValueError):
    """Request inputs or a raw JSON body failed validation."""

    def __init__(self, details: list[dict[str, Any]]) -> None:
        super().__init__("request input validation failed")
        self.details = details


class State:
    """Annotation marker injecting the worker-local ``app.state`` object."""


class RequestHeaders(Mapping[str, str]):
    def __init__(self, values=()) -> None:
        pairs = values.items() if isinstance(values, Mapping) else values
        self._items = tuple((str(name).lower(), str(value)) for name, value in pairs)

    def __getitem__(self, name: str) -> str:
        for key, value in reversed(self._items):
            if key == name.lower():
                return value
        raise KeyError(name)

    def __iter__(self) -> Iterator[str]:
        return iter(dict(self._items))

    def __len__(self) -> int:
        return len(dict(self._items))

    def getlist(self, name: str) -> list[str]:
        return [value for key, value in self._items if key == name.lower()]


_EMPTY_HEADERS = RequestHeaders()
_EMPTY_MAPPING: Mapping[str, Any] = MappingProxyType({})


class _SpooledPart(io.RawIOBase):
    """A read-only window onto one file part of a spooled request body."""

    def __init__(self, source: io.FileIO, offset: int, size: int) -> None:
        self._source = source
        self._offset = offset
        self._size = size
        self._position = 0

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return True

    def readinto(self, buffer) -> int:
        view = memoryview(buffer).cast("B")
        count = min(len(view), self._size - self._position)
        if count <= 0:
            return 0
        data = os.pread(self._source.fileno(), count, self._offset + self._position)
        view[: len(data)] = data
        self._position += len(data)
        return len(data)

    def seek(self, offset: int, whence: int = io.SEEK_SET) -> int:
        base = {io.SEEK_SET: 0, io.SEEK_CUR: self._position, io.SEEK_END: self._size}
        if whence not in base:
            raise ValueError(f"invalid whence ({whence})")
        position = base[whence] + offset
        if position < 0:
            raise ValueError(f"negative seek position {position}")
        self._position = position
        return position

    def tell(self) -> int:
        return self._position

    def close(self) -> None:
        if not self.closed:
            self._source.close()
        super().close()


class UploadFile:
    """A file from a ``multipart/form-data`` request.

    The server has already received and size-checked the whole body. Usually
    the contents are in memory and ``file`` is a ``BytesIO`` over them; with
    ``max_upload_size`` set, a body too large for memory is spooled to an
    unlinked temporary file and ``file`` is a read-only binary reader over
    this part of it. The async methods mirror FastAPI's ``UploadFile`` so
    handlers port unchanged.
    """

    __slots__ = ("content_type", "file", "filename", "headers", "size")

    def __init__(
        self,
        filename: str,
        content_type: str | None = None,
        headers: Mapping[str, str] | None = None,
        data: bytes = b"",
        *,
        file: Any = None,
        size: int | None = None,
    ) -> None:
        self.filename = filename
        self.content_type = content_type
        self.headers = MappingProxyType(dict(headers or {}))
        self.file = io.BytesIO(data) if file is None else file
        self.size = len(data) if size is None else size

    async def read(self, size: int = -1) -> bytes:
        return self.file.read(size)

    async def seek(self, offset: int) -> None:
        self.file.seek(offset)

    async def write(self, data: bytes) -> None:
        if not self.file.writable():
            raise io.UnsupportedOperation(
                "a spooled upload is read-only; copy it to write to it"
            )
        self.file.write(data)
        self.size = max(self.size, self.file.tell())

    async def close(self) -> None:
        self.file.close()

    def __repr__(self) -> str:
        return (
            f"UploadFile(filename={self.filename!r}, "
            f"content_type={self.content_type!r}, size={self.size})"
        )


def _upload(value: Any) -> UploadFile:
    if "spooled" in value:
        size = value["size"]
        reader = io.BufferedReader(
            _SpooledPart(value["spooled"], value["offset"], size), 65536
        )
        return UploadFile(
            value["filename"],
            value["content_type"],
            dict(value["headers"]),
            file=reader,
            size=size,
        )
    return UploadFile(
        value["filename"], value["content_type"], dict(value["headers"]), value["data"]
    )


@dataclass(frozen=True)
class BackgroundTask:
    call: Callable[..., Any]
    args: tuple[Any, ...]
    kwargs: dict[str, Any]
    kind: str


class BackgroundTasks:
    """Tasks completed after handler work while request resources remain valid.

    The compatibility bridge currently completes them before sending the response.
    Work requiring durable delivery belongs in an external task queue.
    """

    def __init__(self) -> None:
        self._tasks: list[BackgroundTask] = []

    def add_task(self, function: Callable[..., Any], *args, **kwargs) -> None:
        import inspect

        if not callable(function):
            raise TypeError("background task must be callable")
        target = function if inspect.isroutine(function) else type(function).__call__
        if (
            inspect.isgeneratorfunction(function)
            or inspect.isasyncgenfunction(function)
            or inspect.isgeneratorfunction(target)
            or inspect.isasyncgenfunction(target)
        ):
            raise TypeError("background tasks cannot be generator functions")
        kind = (
            "async"
            if inspect.iscoroutinefunction(function)
            or inspect.iscoroutinefunction(target)
            else "sync"
        )
        self._tasks.append(BackgroundTask(function, args, kwargs, kind))


class Request:
    """A buffered view of the admitted HTTP request."""

    def __init__(self, metadata: Mapping[str, Any] | None = None, *, app=None) -> None:
        metadata = {} if metadata is None else metadata
        self.method = metadata.get("method", "GET")
        self.path = metadata.get("path", "/")
        self.scheme = metadata.get("scheme", "http")
        self.query_string = metadata.get("query_string", "")
        if isinstance(self.query_string, bytes):
            self.query_string = self.query_string.decode("ascii", errors="replace")
        # Empty read-only views are immutable and safe to share across requests.
        headers = metadata.get("headers", ())
        self.headers = RequestHeaders(headers) if headers else _EMPTY_HEADERS
        self.request_id = metadata.get("request_id") or self.headers.get("x-request-id")
        self.query_params = (
            MappingProxyType(dict(parse_qsl(self.query_string, keep_blank_values=True)))
            if self.query_string
            else _EMPTY_MAPPING
        )
        path_params = metadata.get("path_params")
        self.path_params = (
            MappingProxyType(dict(path_params)) if path_params else _EMPTY_MAPPING
        )
        self.client = metadata.get("client")
        # The verified TLS client certificate (leaf, DER), under mutual TLS.
        self.client_certificate = metadata.get("client_certificate")
        self.app = app
        self.state = SimpleNamespace()
        self._body = bytes(metadata.get("body", b""))
        # A multipart body spooled to disk reaches the handler as UploadFiles.
        self._spooled = bool(metadata.get("body_spooled"))
        self._cookies = (
            MappingProxyType(dict(metadata["cookies"]))
            if "cookies" in metadata
            else None
        )

    @classmethod
    def _bare(cls, method: str, path: str, app=None) -> Request:
        """A request with no metadata, as ``Request({"method", "path"})`` builds.

        Routes that need no request metadata take this on every call, so it
        sets the same attributes directly and shares the immutable empty views.
        """
        self = cls.__new__(cls)
        self.method = method
        self.path = path
        self.scheme = "http"
        self.query_string = ""
        self.headers = _EMPTY_HEADERS
        self.request_id = None
        self.query_params = _EMPTY_MAPPING
        self.path_params = _EMPTY_MAPPING
        self.client = None
        self.client_certificate = None
        self.app = app
        self.state = SimpleNamespace()
        self._body = b""
        self._spooled = False
        self._cookies = None
        return self

    @property
    def url(self) -> str:
        host = self.headers.get("host", "localhost")
        return f"{self.scheme}://{host}{self.path}" + (
            f"?{self.query_string}" if self.query_string else ""
        )

    @property
    def base_url(self) -> str:
        host = self.headers.get("host", "localhost")
        return f"{self.scheme}://{host}/"

    def url_for(self, name: str, /, **path_params: Any) -> str:
        """Return the absolute URL of the route called ``name``."""
        if self.app is None:
            raise RuntimeError("url_for requires a request bound to an application")
        path, kind = self.app._url_path_and_kind(name, path_params)
        # A mounted app's routes are served under its mount prefixes.
        path = getattr(self.app, "_mount_prefix", lambda: "")() + path
        # The destination's protocol picks the scheme, keeping the caller's
        # security: an HTTP page links to ws(s):// sockets and back.
        secure = self.scheme in ("https", "wss")
        if kind == "websocket":
            scheme = "wss" if secure else "ws"
        else:
            scheme = "https" if secure else "http"
        host = self.headers.get("host", "localhost")
        return f"{scheme}://{host}{path}"

    @property
    def cookies(self) -> Mapping[str, str]:
        if self._cookies is not None:
            return self._cookies
        from http.cookies import CookieError, SimpleCookie

        cookies: SimpleCookie[str] = SimpleCookie()
        try:
            cookies.load("; ".join(self.headers.getlist("cookie")))
        except CookieError:
            return MappingProxyType({})
        return MappingProxyType({key: morsel.value for key, morsel in cookies.items()})

    async def body(self) -> bytes:
        if self._spooled:
            raise RuntimeError(
                "this request's multipart body was spooled to disk; read it"
                " through its UploadFile parameters"
            )
        return self._body

    async def json(self) -> Any:
        """Parse with the installed runtime's JSON contract and safe 422 errors."""
        try:
            if _native_parse_json is not None:
                return _native_parse_json(self._body)
            return self._json_fallback()
        except (ValueError, RecursionError) as error:
            raise RequestValidationError(
                [{"loc": ["body"], "type": "json_invalid", "msg": "Invalid JSON body"}]
            ) from error

    def _json_fallback(self) -> Any:
        # Source-only use without the extension validates retained depth and all
        # original Unicode/numeric values. Native qualification is authoritative,
        # including depth of overwritten duplicate fields.
        def valid_unicode(value):
            # Objects are checked by object_pairs before duplicate keys collapse.
            # Walk arrays lazily so validation needs only depth-sized state.
            pending = [iter((value,))]
            while pending:
                try:
                    item = next(pending[-1])
                except StopIteration:
                    pending.pop()
                    continue
                if isinstance(item, str):
                    try:
                        item.encode("utf-8")
                    except UnicodeEncodeError as error:
                        raise ValueError(
                            "JSON strings must contain valid Unicode scalar values"
                        ) from error
                elif isinstance(item, list):
                    pending.append(iter(item))

        def object_pairs(pairs):
            result = {}
            for key, value in pairs:
                valid_unicode(key)
                valid_unicode(value)
                result[key] = value
            return result

        def invalid_constant(value):
            raise ValueError(f"invalid JSON constant: {value}")

        def finite_float(value):
            number = float(value)
            if not math.isfinite(number):
                raise ValueError(
                    "JSON floating-point value exceeds the supported range"
                )
            return number

        def bounded_integer(value):
            if len(value.lstrip("-")) > 4096:
                raise ValueError(
                    "JSON integers may contain at most 4096 decimal digits"
                )
            return int(value)

        value = json.loads(
            self._body.decode("utf-8"),
            parse_constant=invalid_constant,
            parse_float=finite_float,
            parse_int=bounded_integer,
            object_pairs_hook=object_pairs,
        )
        valid_unicode(value)
        pending = [(iter((value,)), 0)]
        while pending:
            items, depth = pending[-1]
            try:
                item = next(items)
            except StopIteration:
                pending.pop()
                continue
            if depth > 64:
                raise ValueError("JSON nesting exceeds 64 levels")
            if isinstance(item, dict):
                pending.append((iter(item.values()), depth + 1))
            elif isinstance(item, list):
                pending.append((iter(item), depth + 1))
        return value


current_request: contextvars.ContextVar[Request | None] = contextvars.ContextVar(
    "bessemer_request", default=None
)
