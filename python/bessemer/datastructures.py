"""Request data structures with the interfaces of FastAPI's (Starlette's).

``URL`` and ``Address`` are ``str`` subclasses, so code that treats
``request.url`` or ``request.client`` as text keeps working, while
``request.url.path`` and ``request.client.host`` work as they do in FastAPI.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping
from typing import Any
from urllib.parse import SplitResult, parse_qsl, urlencode, urlsplit


class URL(str):
    """An absolute URL whose parts are parsed on first use."""

    @property
    def components(self) -> SplitResult:
        # Read through __dict__: a missing attribute would raise and format
        # an AttributeError on every first use.
        parts = self.__dict__.get("_parts")
        if parts is None:
            parts = self._parts = urlsplit(self)
        return parts

    @property
    def scheme(self) -> str:
        return self.components.scheme

    @property
    def netloc(self) -> str:
        return self.components.netloc

    @property
    def path(self) -> str:
        return self.components.path

    @property
    def query(self) -> str:
        return self.components.query

    @property
    def fragment(self) -> str:
        return self.components.fragment

    @property
    def username(self) -> str | None:
        return self.components.username

    @property
    def password(self) -> str | None:
        return self.components.password

    @property
    def hostname(self) -> str | None:
        return self.components.hostname

    @property
    def port(self) -> int | None:
        return self.components.port

    @property
    def is_secure(self) -> bool:
        return self.scheme in ("https", "wss")

    def replace(self, **kwargs: Any) -> URL:
        """A copy with parts replaced: ``scheme``, ``netloc``, ``path``,
        ``query``, ``fragment``, or ``hostname``/``port`` for the netloc.
        """
        if "hostname" in kwargs or "port" in kwargs:
            hostname = kwargs.pop("hostname", None) or self.hostname or ""
            port = kwargs.pop("port", self.port)
            if ":" in hostname and not hostname.startswith("["):
                hostname = f"[{hostname}]"  # An IPv6 literal.
            userinfo, at, _ = self.components.netloc.rpartition("@")
            netloc = hostname if port is None else f"{hostname}:{port}"
            kwargs["netloc"] = f"{userinfo}{at}{netloc}"
        return URL(self.components._replace(**kwargs).geturl())

    def include_query_params(self, **kwargs: Any) -> URL:
        params = MultiDict(parse_qsl(self.query, keep_blank_values=True))
        params.update({key: str(value) for key, value in kwargs.items()})
        return self.replace(query=urlencode(params.multi_items()))

    def remove_query_params(self, keys: str | Iterable[str]) -> URL:
        removed = {keys} if isinstance(keys, str) else set(keys)
        pairs = parse_qsl(self.query, keep_blank_values=True)
        return self.replace(
            query=urlencode(
                [(key, value) for key, value in pairs if key not in removed]
            )
        )


class Address(str):
    """The client's address. Bessemer reports the peer IP; ``port`` is
    ``None`` because the native transport does not pass it to Python.
    """

    @property
    def host(self) -> str:
        return str(self)

    @property
    def port(self) -> int | None:
        return None


class ImmutableMultiDict(Mapping[str, Any]):
    """A read-only mapping that keeps every value of a repeated key.

    Indexing returns the last value, as Starlette's does; ``getlist``
    returns them all.
    """

    __slots__ = ("_dict", "_list")

    def __init__(self, items: Iterable[tuple[str, Any]] | Mapping[str, Any] = ()):
        if type(items) is list:
            # The common case, pairs: skip the Mapping ABC check.
            pairs = items.copy()
        elif isinstance(items, ImmutableMultiDict):
            pairs = items.multi_items()
        elif isinstance(items, Mapping):
            pairs = list(items.items())
        else:
            pairs = list(items)
        self._list = pairs
        self._dict = dict(pairs)

    def __getitem__(self, key: str) -> Any:
        return self._dict[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._dict)

    def __len__(self) -> int:
        return len(self._dict)

    def __contains__(self, key: object) -> bool:
        return key in self._dict

    def get(self, key: str, default: Any = None) -> Any:
        # As Mapping.get, without raising and catching KeyError for a miss.
        return self._dict.get(key, default)

    def __eq__(self, other: object) -> bool:
        if isinstance(other, ImmutableMultiDict):
            return sorted(self._list) == sorted(other._list)
        return Mapping.__eq__(self, other)

    __hash__ = None  # type: ignore[assignment]

    def getlist(self, key: str) -> list[Any]:
        return [value for name, value in self._list if name == key]

    def multi_items(self) -> list[tuple[str, Any]]:
        return list(self._list)

    def __repr__(self) -> str:
        return f"{type(self).__name__}({self._list!r})"


class MultiDict(ImmutableMultiDict):
    """A mutable multi-value mapping, as in Starlette."""

    def __setitem__(self, key: str, value: Any) -> None:
        self.setlist(key, [value])

    def __delitem__(self, key: str) -> None:
        self._list = [(name, value) for name, value in self._list if name != key]
        del self._dict[key]

    def setlist(self, key: str, values: list[Any]) -> None:
        if not values:
            self._list = [(k, v) for k, v in self._list if k != key]
            self._dict.pop(key, None)
            return
        self._list = [(k, v) for k, v in self._list if k != key]
        self._list.extend((key, value) for value in values)
        self._dict[key] = values[-1]

    def append(self, key: str, value: Any) -> None:
        self._list.append((key, value))
        self._dict[key] = value

    def update(self, *args: Any, **kwargs: Any) -> None:
        values = MultiDict(*args)
        values._list.extend(kwargs.items())
        for key in {*dict(values._list)}:
            self._list = [(k, v) for k, v in self._list if k != key]
        self._list.extend(values._list)
        self._dict.update(values._list)


class QueryParams(ImmutableMultiDict):
    """Query string parameters; build from a query string or pairs."""

    __slots__ = ()

    def __init__(
        self, query: str | bytes | Iterable[tuple[str, Any]] | Mapping[str, Any] = ""
    ) -> None:
        if isinstance(query, bytes):
            query = query.decode("latin-1")
        if isinstance(query, str):
            pairs = parse_qsl(query, keep_blank_values=True)
            self._list = pairs
            self._dict = dict(pairs)
            return
        super().__init__(query)

    def __str__(self) -> str:
        return urlencode(self._list)


class FormData(ImmutableMultiDict):
    """Form fields and files returned by ``await request.form()``."""

    __slots__ = ()

    async def close(self) -> None:
        for _, value in self._list:
            close = getattr(value, "close", None)
            if close is not None:
                await close()


def __getattr__(name: str) -> Any:
    if name == "UploadFile":
        from .request import UploadFile

        return UploadFile
    if name == "Headers":
        from .request import RequestHeaders

        return RequestHeaders
    raise AttributeError(name)


__all__ = [  # noqa: F822 -- UploadFile and Headers resolve lazily.
    "URL",
    "Address",
    "FormData",
    "Headers",
    "ImmutableMultiDict",
    "MultiDict",
    "QueryParams",
    "UploadFile",
]
