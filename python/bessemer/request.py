"""Compatibility request context and request-scoped background work."""

from __future__ import annotations

import asyncio
import contextvars
import io
import json
import math
import os
from collections.abc import Callable, Iterator, Mapping
from types import MappingProxyType, SimpleNamespace
from typing import Any
from urllib.parse import parse_qsl

from .background import BackgroundTask
from .datastructures import URL, Address, FormData, QueryParams
from .responses import HTTPException

try:
    from ._native import _parse_json as _native_parse_json
except ImportError:
    _native_parse_json = None


class RequestValidationError(ValueError):
    """Request inputs or a raw JSON body failed validation."""

    def __init__(self, details: list[dict[str, Any]]) -> None:
        super().__init__("request input validation failed")
        self.details = details

    def errors(self) -> list[dict[str, Any]]:
        """The error entries, as FastAPI's ``RequestValidationError.errors()``."""
        return self.details


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
_EMPTY_QUERY = QueryParams()
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
        if isinstance(self.file, io.BytesIO):
            return self.file.read(size)
        return await _off_loop(self.file.read, size)

    async def seek(self, offset: int) -> None:
        if isinstance(self.file, io.BytesIO):
            self.file.seek(offset)
        else:
            await _off_loop(self.file.seek, offset)

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


async def _off_loop(function: Callable[..., Any], *args: Any) -> Any:
    """Run blocking spool-file I/O on the loop's executor.

    A cancelled caller still waits for the call to finish, so the request (and
    its capacity) is not released while a thread is reading its file.
    """
    work = asyncio.get_running_loop().run_in_executor(None, function, *args)
    try:
        return await asyncio.shield(work)
    except asyncio.CancelledError:
        while not work.done():
            try:
                await asyncio.shield(work)
            except asyncio.CancelledError:
                continue
        raise


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


class BackgroundTasks:
    """Tasks completed after handler work while request resources remain valid.

    The compatibility bridge currently completes them before sending the response.
    Work requiring durable delivery belongs in an external task queue.
    """

    def __init__(self) -> None:
        self._tasks: list[BackgroundTask] = []

    def add_task(self, function: Callable[..., Any], *args, **kwargs) -> None:
        self._tasks.append(BackgroundTask(function, *args, **kwargs))


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
        # Parsed on first use: most handlers bind query values natively.
        self._query_params = None if self.query_string else _EMPTY_QUERY
        path_params = metadata.get("path_params")
        self.path_params = (
            MappingProxyType(dict(path_params)) if path_params else _EMPTY_MAPPING
        )
        self._client = metadata.get("client")
        # The verified TLS client certificate (leaf, DER), under mutual TLS.
        self.client_certificate = metadata.get("client_certificate")
        self.app = app
        self.state = SimpleNamespace()
        self._body = bytes(metadata.get("body", b""))
        # A multipart body spooled to disk reaches the handler as UploadFiles.
        self._spooled = bool(metadata.get("body_spooled"))
        # The W3C traceparent of this request's span, under App(otel=...).
        self.traceparent = None
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
        self._query_params = _EMPTY_QUERY
        self.path_params = _EMPTY_MAPPING
        self._client = None
        self.client_certificate = None
        self.app = app
        self.state = SimpleNamespace()
        self._body = b""
        self._spooled = False
        self.traceparent = None
        self._cookies = None
        return self

    @property
    def query_params(self) -> QueryParams:
        """The query string's parameters. Indexing gives a repeated key's
        last value; ``getlist`` gives them all, as in FastAPI.
        """
        params = self._query_params
        if params is None:
            params = self._query_params = QueryParams(self.query_string)
        return params

    @property
    def client(self) -> Address | None:
        """The peer's IP address, with ``.host`` as in FastAPI."""
        client = self._client
        return None if client is None else Address(client)

    @property
    def url(self) -> URL:
        host = self.headers.get("host", "localhost")
        return URL(
            f"{self.scheme}://{host}{self.path}"
            + (f"?{self.query_string}" if self.query_string else "")
        )

    @property
    def base_url(self) -> URL:
        host = self.headers.get("host", "localhost")
        return URL(f"{self.scheme}://{host}/")

    @property
    def path_info(self) -> str:
        return self.path

    def url_for(self, name: str, /, **path_params: Any) -> URL:
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
        return URL(f"{scheme}://{host}{path}")

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

    async def stream(self):
        """Yield the body, then ``b""``, like Starlette's ``request.stream()``.

        The body is already buffered (and size checked) by the server.
        """
        body = await self.body()
        if body:
            yield body
        yield b""

    async def form(self) -> FormData:
        """Parse an urlencoded or multipart form body into ``FormData``.

        Prefer ``Form()``/``File()`` parameters, which the server parses
        natively; this parses the buffered body in Python for code that
        reads the form directly, as FastAPI code often does.
        """
        content_type = self.headers.get("content-type", "")
        media = content_type.split(";", 1)[0].strip().lower()
        if media == "application/x-www-form-urlencoded":
            body = await self.body()
            try:
                fields = parse_qsl(
                    body.decode("latin-1"),
                    keep_blank_values=True,
                    max_num_fields=_MAX_FORM_PARTS,
                )
            except ValueError as error:
                raise HTTPException(400, "Too many form fields") from error
            return FormData(fields)
        if media == "multipart/form-data":
            body = await self.body()
            _check_multipart(content_type, body)
            # The email parser is pure Python: run it off the event loop so a
            # large form does not stall other requests on this worker.
            from .concurrency import run_in_threadpool

            return await run_in_threadpool(_multipart_form, content_type, body)
        return FormData()

    async def close(self) -> None:
        """Release request resources; the server owns the buffered body."""

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


# The native form parser's limits (src/forms.rs), which match Starlette's.
_MAX_FORM_PARTS = 1000
_MAX_PART_HEADER_BYTES = 8192


def _check_multipart(content_type: str, body: bytes) -> None:
    """Refuse a multipart body over the part limits before parsing it.

    ``Form()``/``File()`` parameters get these limits from the native
    parser; ``request.form()`` parses in Python, so it checks them here.
    """
    from email.message import Message

    header = Message()
    header["content-type"] = content_type
    boundary = header.get_param("boundary")
    if not isinstance(boundary, str) or not boundary:
        raise HTTPException(400, "Missing multipart boundary")
    delimiter = b"--" + boundary.encode("latin-1")
    segments = body.split(delimiter, _MAX_FORM_PARTS + 2)
    # Preamble, then one segment per part, then the closing "--" epilogue.
    if len(segments) > _MAX_FORM_PARTS + 2:
        raise HTTPException(400, f"Too many form parts; the limit is {_MAX_FORM_PARTS}")
    for segment in segments[1:-1]:
        end = segment.find(b"\r\n\r\n", 0, _MAX_PART_HEADER_BYTES + 4)
        if end == -1:
            raise HTTPException(400, "Multipart part headers are too large")


def _multipart_form(content_type: str, body: bytes) -> FormData:
    """Parse a buffered ``multipart/form-data`` body with the email parser."""
    from email.parser import BytesParser
    from email.policy import HTTP

    message = BytesParser(policy=HTTP).parsebytes(
        b"Content-Type: " + content_type.encode("latin-1") + b"\r\n\r\n" + body
    )
    if not message.is_multipart():
        raise RequestValidationError(
            [{"loc": ["body"], "type": "value_error", "msg": "Invalid form body"}]
        )
    fields: list[tuple[str, Any]] = []
    for part in message.iter_parts():
        name = part.get_param("name", header="content-disposition")
        if name is None:
            continue
        payload = part.get_payload(decode=True) or b""
        filename = part.get_filename()
        if filename is None:
            charset = part.get_content_charset() or "utf-8"
            fields.append((name, payload.decode(charset, errors="replace")))
        else:
            fields.append(
                (
                    name,
                    UploadFile(
                        filename,
                        part.get_content_type(),
                        {key.lower(): str(value) for key, value in part.items()},
                        payload,
                    ),
                )
            )
    return FormData(fields)


current_request: contextvars.ContextVar[Request | None] = contextvars.ContextVar(
    "bessemer_request", default=None
)
