"""Explicit responses and validated HTTP headers for the compatibility runtime."""

from __future__ import annotations

import dataclasses
import datetime as dt
import enum
import hashlib
import json
import math
import mimetypes
import os
import re
import stat
from collections.abc import Iterable, Mapping, MutableMapping
from decimal import Decimal
from email.utils import formatdate
from http import HTTPStatus
from http.cookies import Morsel, SimpleCookie
from typing import Any
from urllib.parse import quote
from uuid import UUID

try:
    from ._native import _encode_plain_json
except ImportError:  # Source-only use without the extension.
    _encode_plain_json = None

# The native encoder's byte limit where the caller sets none: when it only
# checks that an event's data is exact JSON, and when a JSON body is rendered
# on request (larger data takes the Python encoder, which has no limit here).
_SSE_LIMIT = 1 << 30

_TOKEN = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+\Z")
# Anything but tab and printable ASCII (32 to 126) is invalid in a value.
_INVALID_VALUE = re.compile(r"[^\t\x20-\x7e]")
_COOKIE_VALUE = re.compile(r"[\x21\x23-\x2b\x2d-\x3a\x3c-\x5b\x5d-\x7e]*\Z")
_DOMAIN_LABEL = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\Z")
_FORBIDDEN_HEADERS = frozenset(
    {
        "connection",
        "content-length",
        "transfer-encoding",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailer",
        "upgrade",
    }
)


def _plain_int(value: Any) -> int | None:
    """``value`` as an exact ``int``, or None when it is not an integer.

    An ``int`` subclass (``http.HTTPStatus`` or another ``IntEnum``) is
    converted, so a status code can be written either way; ``bool`` is not
    an integer here.
    """
    if type(value) is int:
        return value
    if isinstance(value, int) and not isinstance(value, bool):
        # The built-in conversion: a subclass's own __int__ does not run.
        return int.__int__(value)
    return None


def validate_status(status_code: int) -> int:
    if type(status_code) is not int:
        status_code = _plain_int(status_code)
        if status_code is None:
            raise ValueError("response status_code must be between 200 and 599")
    if not 200 <= status_code <= 599:
        raise ValueError("response status_code must be between 200 and 599")
    return status_code


# Short header pairs that passed validation; most responses repeat a few
# (a Content-Type, a Cache-Control), and validation depends only on the text.
_VALID_HEADERS: dict[tuple[str, str], tuple[str, str]] = {}


def validate_header(name: str, value: str) -> tuple[str, str]:
    exact = type(name) is str and type(value) is str
    if exact and (pair := _VALID_HEADERS.get((name, value))) is not None:
        return pair
    if not isinstance(name, str) or str.__len__(name) > 16_384:
        raise ValueError("header names must be valid ASCII HTTP tokens")
    name = str.__str__(name)
    if _TOKEN.fullmatch(name) is None:
        raise ValueError("header names must be valid ASCII HTTP tokens")
    if name.lower() in _FORBIDDEN_HEADERS:
        raise ValueError(f"the runtime manages the {name!r} header")
    if not isinstance(value, str) or str.__len__(value) > 16_384:
        raise ValueError("response header value exceeds the runtime limit")
    value = str.__str__(value)
    if _INVALID_VALUE.search(value) is not None:
        raise ValueError(
            "header values must be printable ASCII without control characters"
        )
    pair = (name, value)
    if exact and len(name) + len(value) <= 256 and len(_VALID_HEADERS) < 512:
        _VALID_HEADERS[pair] = pair
    return pair


class Headers(MutableMapping[str, str]):
    """Case-insensitive mutable headers preserving repeated Set-Cookie fields."""

    def __init__(
        self,
        values: Mapping[str, str] | Iterable[tuple[str, str]] | None = None,
    ) -> None:
        self._items: list[tuple[str, str]] = []
        if values is not None:
            # The usual arguments are told apart without the ABC check.
            kind = type(values)
            if kind is dict:
                pairs = values.items()
            elif kind is list or kind is tuple:
                pairs = values
            elif isinstance(values, Headers):
                # Every field, already validated. As a mapping it yields each
                # name once, which would keep one of several Set-Cookie or
                # WWW-Authenticate fields.
                self._items = values._items.copy()
                return
            else:
                pairs = values.items() if isinstance(values, Mapping) else values
            for name, value in pairs:
                self.append(name, value)

    def append(self, name: str, value: str) -> None:
        pair = validate_header(name, value)
        self._validate_size([*self._items, pair])
        self._items.append(pair)

    @staticmethod
    def _validate_size(items) -> None:
        if len(items) > 64:
            raise ValueError("response headers exceed the configured runtime limits")
        size = 0
        for name, value in items:
            size += len(name) + len(value) + 4
        if size > 16_384:
            raise ValueError("response headers exceed the configured runtime limits")

    def __getitem__(self, name: str) -> str:
        for stored, value in reversed(self._items):
            if stored.lower() == name.lower():
                return value
        raise KeyError(name)

    def __contains__(self, name: object) -> bool:
        # As Mapping's (a lookup through __getitem__), without raising and
        # catching KeyError for a miss.
        for stored, _ in self._items:
            if stored.lower() == name.lower():  # type: ignore[attr-defined]
                return True
        return False

    @classmethod
    def _validated(cls, items: list[tuple[str, str]]) -> Headers:
        """Headers over pairs that already passed validation, kept as given."""
        self = cls.__new__(cls)
        self._items = items
        return self

    def __setitem__(self, name: str, value: str) -> None:
        pair = validate_header(name, value)
        lowered = pair[0].lower()
        current = self._items
        for key, _ in current:
            if key.lower() == lowered:
                break
        else:
            # A new field (the usual case): append it.
            self._validate_size((*current, pair))
            current.append(pair)
            return
        items = [(key, item) for key, item in current if key.lower() != lowered]
        items.append(pair)
        self._validate_size(items)
        self._items[:] = items

    def __delitem__(self, name: str) -> None:
        previous = len(self._items)
        self._items[:] = [
            (key, item) for key, item in self._items if key.lower() != name.lower()
        ]
        if previous == len(self._items):
            raise KeyError(name)

    def __iter__(self):
        seen: set[str] = set()
        for name, _ in self._items:
            normalized = name.lower()
            if normalized not in seen:
                seen.add(normalized)
                yield name

    def __len__(self) -> int:
        return len({name.lower() for name, _ in self._items})

    def getlist(self, name: str) -> list[str]:
        return [value for key, value in self._items if key.lower() == name.lower()]

    @property
    def raw(self) -> list[tuple[str, str]]:
        return self._items.copy()


class HTTPException(Exception):
    """Return an explicit JSON error, optionally with validated response headers."""

    def __init__(
        self,
        status_code: int,
        detail: Any = None,
        headers: Mapping[str, str] | Iterable[tuple[str, str]] | None = None,
    ) -> None:
        if type(status_code) is not int:
            # An http.HTTPStatus member is stored as the plain integer.
            status_code = _plain_int(status_code)
        if status_code is None or not 400 <= status_code <= 599:
            raise ValueError("HTTPException status_code must be between 400 and 599")
        if detail is None:
            # As in FastAPI, the status phrase ("Not Found") is the default.
            try:
                detail = HTTPStatus(status_code).phrase
            except ValueError:
                detail = ""
        super().__init__(str(detail))
        self.status_code = status_code
        self.detail = detail
        self.headers = Headers(headers)


class Response:
    """A buffered byte response. Framing headers are managed by the Rust runtime.

    ``background`` (a ``BackgroundTask`` or ``BackgroundTasks``) runs after
    the handler, alongside the request's own background tasks.
    """

    media_type: str | None = None
    background: Any = None

    def __init__(
        self,
        content: bytes | bytearray | memoryview | str = b"",
        status_code: int = 200,
        headers: Mapping[str, str] | Iterable[tuple[str, str]] | None = None,
        media_type: str | None = None,
        background: Any = None,
    ) -> None:
        if background is not None:
            self.background = _background(background)
        self.status_code = validate_status(status_code)
        self.headers = Headers(headers)
        if isinstance(content, str):
            self.body = content.encode("utf-8")
        elif isinstance(content, (bytes, bytearray, memoryview)):
            self.body = bytes(content)
        else:
            raise TypeError(
                "Response content must be bytes or str; use JSONResponse for JSON"
            )
        self.media_type = self.media_type if media_type is None else media_type
        if self.media_type is not None and "content-type" not in self.headers:
            content_type = self.media_type
            if (
                content_type.startswith("text/")
                and "charset=" not in content_type.lower()
            ):
                content_type += "; charset=utf-8"
            self.headers["Content-Type"] = content_type

    def set_cookie(
        self,
        key: str,
        value: str = "",
        *,
        max_age: int | None = None,
        expires: dt.datetime | str | int | None = None,
        path: str | None = "/",
        domain: str | None = None,
        secure: bool = False,
        httponly: bool = False,
        samesite: str | None = "lax",
        partitioned: bool = False,
    ) -> None:
        if type(partitioned) is not bool:
            raise TypeError("cookie partitioned must be a boolean")
        if partitioned and not secure:
            raise ValueError("Partitioned cookies must also be Secure")
        if (
            not isinstance(key, str)
            or len(key) > 16_384
            or _TOKEN.fullmatch(key) is None
        ):
            raise ValueError("cookie key must be an HTTP token")
        if not isinstance(value, str):
            raise TypeError("cookie value must be a string")
        if len(value) > 16_384 or _COOKIE_VALUE.fullmatch(value) is None:
            raise ValueError("cookie values must contain only RFC6265 cookie octets")
        if type(secure) is not bool or type(httponly) is not bool:
            raise TypeError("cookie secure and httponly must be booleans")
        if path is not None and (
            not isinstance(path, str)
            or len(path) > 16_384
            or any(
                ord(character) < 32 or ord(character) > 126 or character == ";"
                for character in path
            )
        ):
            raise ValueError(
                "cookie paths must not contain controls, non-ASCII characters or semicolons"
            )
        if domain is not None:
            if not isinstance(domain, str) or not domain or len(domain) > 253:
                raise ValueError("cookie domain must be an ASCII domain name")
            normalized_domain = domain.removeprefix(".")
            if not normalized_domain or any(
                _DOMAIN_LABEL.fullmatch(label) is None
                for label in normalized_domain.split(".")
            ):
                raise ValueError("cookie domain must be an ASCII domain name")
        if max_age is not None and type(max_age) is not int:
            raise TypeError("cookie max_age must be an integer")
        if samesite is not None:
            if not isinstance(samesite, str):
                raise TypeError("cookie samesite must be a string or None")
            samesite = samesite.lower()
            if samesite not in {"lax", "strict", "none"}:
                raise ValueError("samesite must be 'lax', 'strict', 'none' or None")
        if samesite == "none" and not secure:
            raise ValueError("SameSite=None cookies must also be Secure")
        if key.startswith("__Secure-") and not secure:
            raise ValueError("__Secure- cookies must be Secure")
        if key.startswith("__Host-") and (
            not secure or domain is not None or path != "/"
        ):
            raise ValueError("__Host- cookies require Secure, Path=/ and no Domain")
        cookie: SimpleCookie[str] = SimpleCookie()
        cookie[key] = value
        morsel = cookie[key]
        if max_age is not None:
            morsel["max-age"] = max_age
        if expires is not None:
            from email.utils import format_datetime, parsedate_to_datetime

            if isinstance(expires, str):
                if len(expires) > 128:
                    raise ValueError("cookie expiry must be a valid HTTP date")
                try:
                    expires = parsedate_to_datetime(expires)
                except (ValueError, TypeError) as error:
                    raise ValueError(
                        "cookie expiry must be a valid HTTP date"
                    ) from error
            if isinstance(expires, dt.datetime):
                if expires.utcoffset() is None:
                    raise ValueError("cookie expiry datetime must be timezone-aware")
                expires = format_datetime(expires.astimezone(dt.UTC), usegmt=True)
            elif type(expires) is not int:
                raise TypeError(
                    "cookie expiry must be an HTTP date, datetime or integer"
                )
            morsel["expires"] = expires
        if path is not None:
            morsel["path"] = path
        if domain is not None:
            morsel["domain"] = domain
        morsel["secure"] = secure
        morsel["httponly"] = httponly
        if samesite is not None:
            morsel["samesite"] = samesite
        # CPython 3.14 knows the attribute; earlier versions need it appended.
        native_partitioned = partitioned and "partitioned" in Morsel._reserved
        if native_partitioned:
            morsel["partitioned"] = True
        output = morsel.OutputString()
        if partitioned and not native_partitioned:
            output += "; Partitioned"
        self.headers.append("Set-Cookie", output)

    def delete_cookie(
        self,
        key: str,
        *,
        path: str | None = "/",
        domain: str | None = None,
        secure: bool = False,
        httponly: bool = False,
        samesite: str | None = "lax",
        partitioned: bool = False,
    ) -> None:
        self.set_cookie(
            key,
            "",
            max_age=0,
            expires=0,
            path=path,
            domain=domain,
            secure=secure,
            httponly=httponly,
            samesite=samesite,
            partitioned=partitioned,
        )

    def to_native(
        self, *, max_bytes: int | None = None
    ) -> tuple[int, bytes, list[tuple[str, str]], str]:
        if max_bytes is not None and len(self.body) > max_bytes:
            raise ValueError("response body exceeds the configured limit")
        return self.status_code, self.body, self.headers.raw, "bytes"


class JSONResponse(Response):
    """A JSON response. The runtime encodes ``content`` natively when the
    response is sent.

    A subclass that overrides ``render`` (Starlette's extension point) sends
    the bytes its ``render`` returns instead, under the class's media type.
    """

    media_type = "application/json"

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        # Decided once per class, so sending a response checks nothing: a
        # class that overrides render sends what it renders, and every other
        # class keeps the native encoder.
        if cls.render is not JSONResponse.render and "to_native" not in cls.__dict__:
            cls.to_native = JSONResponse._rendered_native  # type: ignore[method-assign]

    def __init__(
        self,
        content: Any,
        status_code: int = 200,
        headers=None,
        media_type: str | None = None,
        background: Any = None,
    ) -> None:
        # As Response.__init__, but there are no body bytes to store: they
        # are encoded from ``content`` when the response is sent, and ``body``
        # is a property here.
        if background is not None:
            self.background = _background(background)
        self.status_code = validate_status(status_code)
        self.headers = Headers(headers)
        self.content = content
        self.media_type = self.media_type if media_type is None else media_type
        if self.media_type is not None and "content-type" not in self.headers:
            content_type = self.media_type
            if (
                content_type.startswith("text/")
                and "charset=" not in content_type.lower()
            ):
                content_type += "; charset=utf-8"
            self.headers["Content-Type"] = content_type

    def render(self, content: Any) -> bytes:
        """Encode ``content`` as the body, as Starlette's ``JSONResponse.render``.

        Override it to send another encoding: what it returns is sent as it
        is. The built-in classes are not rendered through here when they are
        sent, since the runtime encodes their content natively, to the same
        bytes; this default is what ``body`` returns.
        """
        value = jsonable(content)
        if _encode_plain_json is not None:
            encoded = _encode_plain_json(value, _SSE_LIMIT)
            if encoded is not None:
                return bytes(encoded)
        return json.dumps(
            value, ensure_ascii=False, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")

    @property
    def body(self) -> bytes:
        """The encoded body, rendered from ``content`` each time it is read."""
        body = self.render(self.content)
        if not isinstance(body, (bytes, bytearray, memoryview)):
            raise TypeError(f"{type(self).__name__}.render() must return bytes")
        return bytes(body)

    @body.setter
    def body(self, value: bytes) -> None:
        # The empty placeholder Response.__init__ stores is accepted, for a
        # subclass that still calls it; bytes nothing would send are not.
        if value:
            raise AttributeError(
                "a JSONResponse encodes its content when it is sent; change its"
                " content, or return a Response with the encoded bytes"
            )

    def to_native(self, *, max_bytes: int | None = None):
        return (
            self.status_code,
            jsonable(self.content, max_bytes=max_bytes),
            self.headers.raw,
            "json",
        )

    def _rendered_native(self, *, max_bytes: int | None = None):
        """``to_native`` for a class that overrides ``render``."""
        body = self.body
        if max_bytes is not None and len(body) > max_bytes:
            raise ValueError("response body exceeds the configured limit")
        return self.status_code, body, self.headers.raw, "bytes"


def _renders_content(response_class: type) -> bool:
    """Whether a response class sends what its own ``render`` returns."""
    return (
        issubclass(response_class, JSONResponse)
        and response_class.render is not JSONResponse.render
    )


class ORJSONResponse(JSONResponse):
    """FastAPI's ORJSONResponse. Bessemer already encodes JSON natively in
    Rust, so this is ``JSONResponse`` and needs no ``orjson``.
    """


class UJSONResponse(JSONResponse):
    """FastAPI's UJSONResponse; Bessemer's native JSON encoder serves it."""


class PlainTextResponse(Response):
    media_type = "text/plain"


class HTMLResponse(Response):
    media_type = "text/html"


class RedirectResponse(Response):
    def __init__(
        self, url: str, status_code: int = 307, headers=None, background: Any = None
    ) -> None:
        if type(status_code) is not int:
            status_code = _plain_int(status_code)
        if status_code not in {301, 302, 303, 307, 308}:
            raise ValueError("redirect status must be 301, 302, 303, 307 or 308")
        if not isinstance(url, str) or any(
            ord(character) < 32 or ord(character) == 127 for character in url
        ):
            raise ValueError("redirect URL must not contain control characters")
        super().__init__(
            b"", status_code=status_code, headers=headers, background=background
        )
        self.headers["Location"] = quote(url, safe=":/%#?=@[]!$&'()*+,;~")


class StreamingResponse(Response):
    """A response whose body is produced incrementally by an iterator.

    ``content`` is an async or sync iterable of ``bytes`` or ``str`` chunks.
    Rust pulls one chunk at a time with one chunk of prefetch, so a slow client
    applies backpressure to the iterator. Generator dependencies and
    background tasks run after the last chunk; when the client disconnects the
    iterator is closed and its cleanup still completes before request capacity
    is released. Each chunk must arrive within the route timeout and be no
    larger than the response size limit.
    """

    def __init__(
        self,
        content,
        status_code: int = 200,
        headers=None,
        media_type: str | None = None,
        background: Any = None,
    ) -> None:
        if isinstance(content, (bytes, bytearray, memoryview, str)) or not (
            hasattr(content, "__aiter__") or hasattr(content, "__iter__")
        ):
            raise TypeError(
                "StreamingResponse content must be an iterable or async iterable of chunks"
            )
        super().__init__(b"", status_code, headers, media_type, background)
        self.body_iterator = content

    def to_native(self, *, max_bytes: int | None = None):
        raise TypeError("streaming responses are returned from route handlers only")


try:
    from ._native import _ResponseFile
except ImportError:  # pragma: no cover - exercised only without the extension.
    _ResponseFile = None


class _OpenFile:
    """Fallback open file handle when the native extension is not loaded."""

    __slots__ = ("_file",)

    def __init__(self, path: str) -> None:
        self._file: Any = open(path, "rb")  # noqa: SIM115 -- Closed by close() / __del__.

    def read_chunk(self) -> bytes:
        if self._file is None:
            return b""
        return self._file.read(65536)

    def close(self) -> None:
        if self._file is not None:
            self._file.close()
            self._file = None

    def __del__(self) -> None:
        self.close()


class FileResponse(Response):
    """Send a file from disk, as FastAPI's ``FileResponse`` does.

    The media type is guessed from ``filename`` or ``path`` unless given, and
    ``filename`` adds a ``Content-Disposition`` header. The runtime serves the
    open file natively on the static-file path with ``ETag``,
    ``Last-Modified``, conditional requests and single byte ranges.
    """

    chunk_size = 64 * 1024

    def __init__(
        self,
        path: str | os.PathLike[str],
        status_code: int = 200,
        headers=None,
        media_type: str | None = None,
        background: Any = None,
        filename: str | None = None,
        stat_result: os.stat_result | None = None,
        method: str | None = None,
        content_disposition_type: str = "attachment",
    ) -> None:
        self.path = os.fspath(path)
        self.filename = filename
        if method is not None and not isinstance(method, str):
            raise TypeError("method must be a string or None")
        self.method = method
        if stat_result is None:
            try:
                stat_result = os.stat(self.path)
            except FileNotFoundError as error:
                raise RuntimeError(
                    f"File at path {self.path} does not exist."
                ) from error
        if not stat.S_ISREG(stat_result.st_mode):
            raise RuntimeError(f"File at path {self.path} is not a file.")
        self.stat_result = stat_result
        if media_type is None:
            media_type = mimetypes.guess_type(filename or self.path)[0] or "text/plain"
        super().__init__(
            b"",
            status_code,
            headers,
            media_type,
            background,
        )
        if filename is not None:
            if content_disposition_type not in ("attachment", "inline"):
                raise ValueError(
                    "content_disposition_type must be attachment or inline"
                )
            quoted = quote(filename)
            if quoted != filename:
                disposition = f"{content_disposition_type}; filename*=utf-8''{quoted}"
            else:
                disposition = f'{content_disposition_type}; filename="{filename}"'
            self.headers.setdefault("Content-Disposition", disposition)
        self.headers.setdefault(
            "Last-Modified", formatdate(stat_result.st_mtime, usegmt=True)
        )
        self.headers.setdefault(
            "ETag",
            '"'
            + hashlib.md5(
                f"{stat_result.st_mtime}-{stat_result.st_size}".encode(),
                usedforsecurity=False,
            ).hexdigest()
            + '"',
        )

    def to_native(self, *, max_bytes: int | None = None):
        handle = (
            _ResponseFile(self.path)
            if _ResponseFile is not None
            else _OpenFile(self.path)
        )
        return (
            validate_status(self.status_code),
            handle,
            self.headers.raw,
            "file",
        )


def _background(value: Any) -> Any:
    """Validate a response's ``background`` task or tasks."""
    from .background import BackgroundTask
    from .request import BackgroundTasks

    if not isinstance(value, (BackgroundTask, BackgroundTasks)):
        raise TypeError("background must be a BackgroundTask or BackgroundTasks")
    return value


def background_tasks(response: Response) -> tuple[Any, ...]:
    """The background tasks a response carries, in order."""
    background = response.background
    if background is None:
        return ()
    from .background import BackgroundTask

    if isinstance(background, BackgroundTask):
        return (background,)
    return tuple(background._tasks)


def _event_field(name: str, value: Any) -> str:
    text = str(value)
    if "\r" in text or (name != "data" and "\n" in text):
        raise ValueError(f"server-sent event {name} cannot contain line breaks")
    return text


# The encoder ``json.dumps(..., ensure_ascii=False, separators=(",", ":"))``
# would build on every call.
_SSE_ENCODER = json.JSONEncoder(ensure_ascii=False, separators=(",", ":"))


def format_event(
    data: Any = None,
    *,
    event: str | None = None,
    id: str | None = None,
    retry: int | None = None,
    comment: str | None = None,
) -> bytes:
    """Encode one server-sent event. Non-string data is sent as JSON."""
    lines = []
    if comment is not None:
        lines.extend(
            f": {line}" for line in _event_field("comment", comment).split("\n")
        )
    if event is not None:
        lines.append(f"event: {_event_field('event', event)}")
    if id is not None:
        lines.append(f"id: {_event_field('id', id)}")
    if retry is not None:
        if type(retry) is not int or retry < 0:
            raise ValueError("server-sent event retry must be a nonnegative integer")
        lines.append(f"retry: {retry}")
    if data is not None:
        if not isinstance(data, str):
            # Exact JSON values (which the native encoder accepts, with the
            # same depth and size checks as jsonable) need no normalizing copy;
            # the json encoder still writes them, so floats keep its format.
            plain = False
            if _encode_plain_json is not None:
                try:
                    plain = _encode_plain_json(data, _SSE_LIMIT) is not None
                except (TypeError, ValueError):
                    plain = False
            data = _SSE_ENCODER.encode(data if plain else jsonable(data))
        lines.extend(f"data: {line}" for line in _event_field("data", data).split("\n"))
    return ("\n".join(lines) + "\n\n").encode("utf-8")


def _event_bytes(item) -> bytes:
    return item if isinstance(item, bytes) else format_event(item)


class _AsyncEvents:
    """Formats a source's items as events and forwards ``aclose`` to the
    source's own iterator (which ``__aiter__`` may return as a separate
    generator), even if iteration never started, so its finalizers run
    before the stream reports completion."""

    __slots__ = ("_closed", "_iterator")

    def __init__(self, content) -> None:
        self._iterator = content.__aiter__()
        self._closed = False

    def __aiter__(self):
        return self

    async def __anext__(self) -> bytes:
        return _event_bytes(await self._iterator.__anext__())

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        close = getattr(self._iterator, "aclose", None)
        if close is not None:
            await close()


class _SyncEvents:
    """The synchronous counterpart of ``_AsyncEvents``."""

    __slots__ = ("_closed", "_iterator")

    def __init__(self, content) -> None:
        self._iterator = iter(content)
        self._closed = False

    def __iter__(self):
        return self

    def __next__(self) -> bytes:
        return _event_bytes(next(self._iterator))

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        close = getattr(self._iterator, "close", None)
        if close is not None:
            close()


class EventSourceResponse(StreamingResponse):
    """Server-sent events. Yield ``bytes`` from ``format_event`` or plain data."""

    media_type = "text/event-stream"

    def __init__(
        self, content, status_code: int = 200, headers=None, background: Any = None
    ) -> None:
        events = (
            _AsyncEvents(content)
            if hasattr(content, "__aiter__")
            else _SyncEvents(content)
        )
        super().__init__(events, status_code, headers, background=background)
        self.headers.setdefault("Cache-Control", "no-cache")
        self.headers.setdefault("X-Accel-Buffering", "no")


@dataclasses.dataclass
class _Budget:
    remaining: int

    def spend(self, count: int) -> None:
        if count > self.remaining:
            raise ValueError("response body exceeds the configured limit")
        self.remaining -= count


def _string_size(value: str, budget: _Budget | None) -> int:
    # Reject oversized strings before allocating their UTF-8 representation.
    if budget is not None and str.__len__(value) + 2 > budget.remaining:
        raise ValueError("response body exceeds the configured limit")
    count = len(str.encode(value, "utf-8")) + 2
    count += str.count(value, '"') + str.count(value, "\\")
    for character in value:
        if ord(character) < 32:
            count += 1 if character in "\b\t\n\f\r" else 5
    return count


def jsonable(
    value: Any,
    *,
    max_bytes: int | None = None,
    _depth: int = 0,
    _budget: _Budget | None = None,
    _conversion_hops: int = 0,
) -> Any:
    """Convert common Python values with bounded recursion and strict JSON keys."""
    if max_bytes is not None and _budget is None:
        if type(max_bytes) is not int or max_bytes < 1:
            raise ValueError("max_bytes must be a positive integer")
        _budget = _Budget(max_bytes)
    if _depth > 64:
        raise ValueError("JSON response nesting exceeds 64 levels")
    if _conversion_hops > 128:
        raise ValueError("JSON response normalization exceeds 128 conversion steps")
    if value is None or isinstance(value, bool):
        if _budget is not None:
            _budget.spend(4 if value is None or value is True else 5)
        return value
    if isinstance(value, str):
        value = str.__str__(value)
        if _budget is not None:
            _budget.spend(_string_size(value, _budget))
        return value
    if isinstance(value, int):
        value = int.__int__(value)
        if abs(value).bit_length() > 13610 or len(str(abs(value))) > 4096:
            raise ValueError("JSON integers may contain at most 4096 decimal digits")
        if _budget is not None:
            _budget.spend(len(str(value)))
        return value
    if isinstance(value, float):
        value = float.__float__(value)
        if not math.isfinite(value):
            raise ValueError("non-finite floats are not valid JSON")
        if _budget is not None:
            # Python repr and the native serializer choose different exponent
            # formats. Native f64 JSON takes at least three bytes (0.0 or 1e9).
            # Count that lower bound to limit copied nodes; native serialization
            # enforces the exact encoded-byte limit.
            _budget.spend(3)
        return value
    if isinstance(value, enum.Enum):
        return jsonable(
            value.value,
            _depth=_depth,
            _budget=_budget,
            _conversion_hops=_conversion_hops + 1,
        )
    if isinstance(value, (dt.datetime, dt.date, dt.time)):
        return jsonable(
            value.isoformat(),
            _depth=_depth,
            _budget=_budget,
            _conversion_hops=_conversion_hops + 1,
        )
    if isinstance(value, dt.timedelta):
        return jsonable(
            value.total_seconds(),
            _depth=_depth,
            _budget=_budget,
            _conversion_hops=_conversion_hops + 1,
        )
    if isinstance(value, (UUID, Decimal)):
        return jsonable(
            str(value),
            _depth=_depth,
            _budget=_budget,
            _conversion_hops=_conversion_hops + 1,
        )
    if isinstance(value, Mapping):
        if _budget is not None:
            _budget.spend(2)
        result = {}
        pairs = dict.items(value) if isinstance(value, dict) else value.items()
        for index, (key, item) in enumerate(pairs):
            if not isinstance(key, str):
                raise TypeError("JSON object keys must be strings")
            key = str.__str__(key)
            if _budget is not None:
                _budget.spend((1 if index else 0) + 1 + _string_size(key, _budget))
            result[key] = jsonable(
                item,
                _depth=_depth + 1,
                _budget=_budget,
                _conversion_hops=_conversion_hops,
            )
        return result
    if isinstance(value, (list, tuple)):
        if _budget is not None:
            _budget.spend(2)
        result = []
        items = (
            list.__iter__(value) if isinstance(value, list) else tuple.__iter__(value)
        )
        for index, item in enumerate(items):
            if _budget is not None and index:
                _budget.spend(1)
            result.append(
                jsonable(
                    item,
                    _depth=_depth + 1,
                    _budget=_budget,
                    _conversion_hops=_conversion_hops,
                )
            )
        return result
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        if _budget is not None:
            _budget.spend(2)
        result = {}
        for index, field in enumerate(dataclasses.fields(value)):
            if _budget is not None:
                _budget.spend(
                    (1 if index else 0) + 1 + _string_size(field.name, _budget)
                )
            result[field.name] = jsonable(
                getattr(value, field.name),
                _depth=_depth + 1,
                _budget=_budget,
                _conversion_hops=_conversion_hops,
            )
        return result
    dump = getattr(value, "model_dump", None)
    if callable(dump):
        return jsonable(
            dump(mode="json", by_alias=True, warnings="error"),
            _depth=_depth,
            _budget=_budget,
            _conversion_hops=_conversion_hops + 1,
        )
    raise TypeError("handler must return JSON-compatible values or a Response")
