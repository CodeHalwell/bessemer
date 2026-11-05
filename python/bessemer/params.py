"""Declarative inputs and dependencies inspected while an application builds."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, ClassVar


class _Unset:
    def __repr__(self) -> str:
        return "Undefined"


UNSET = _Unset()


@dataclass(frozen=True)
class Parameter:
    default: Any = UNSET
    alias: str | None = None
    description: str | None = None
    ge: int | float | None = None
    gt: int | float | None = None
    le: int | float | None = None
    lt: int | float | None = None
    min_length: int | None = None
    max_length: int | None = None
    pattern: str | None = None
    deprecated: bool = False
    source: ClassVar[str]


@dataclass(frozen=True)
class Path(Parameter):
    source: ClassVar[str] = "path"


@dataclass(frozen=True)
class Query(Parameter):
    source: ClassVar[str] = "query"


@dataclass(frozen=True)
class Header(Parameter):
    convert_underscores: bool = True
    source: ClassVar[str] = "header"


@dataclass(frozen=True)
class Cookie(Parameter):
    source: ClassVar[str] = "cookie"


@dataclass(frozen=True)
class Body(Parameter):
    embed: bool = False
    source: ClassVar[str] = "body"


@dataclass(frozen=True)
class Form(Parameter):
    """A field of an urlencoded or multipart form body."""

    source: ClassVar[str] = "form"


@dataclass(frozen=True)
class File(Parameter):
    """A file part of a multipart form body (``UploadFile`` or ``bytes``)."""

    source: ClassVar[str] = "file"


@dataclass(frozen=True)
class Depends:
    dependency: Callable[..., Any] | None = None
    use_cache: bool = True
    scope: str = "request"

    def __post_init__(self) -> None:
        if self.dependency is not None and not callable(self.dependency):
            raise TypeError("Depends expects a callable dependency")
        if type(self.use_cache) is not bool:
            raise TypeError("Depends use_cache must be a boolean")
        if self.scope not in {"request", "app"}:
            raise ValueError("Depends scope must be 'request' or 'app'")
        if self.scope == "app" and not self.use_cache:
            raise ValueError("application-scoped dependencies must use caching")


@dataclass(frozen=True)
class Security(Depends):
    """A dependency that also requires OAuth2 scopes.

    The scopes add to those of every ``Security`` above it in the graph, and
    the dependency (or anything it depends on) can read the accumulated list
    by asking for ``SecurityScopes``. The security schemes it reaches list
    them in their OpenAPI requirement.
    """

    scopes: Sequence[str] = field(default=(), kw_only=True)

    def __post_init__(self) -> None:
        super().__post_init__()
        if isinstance(self.scopes, str) or not isinstance(self.scopes, Sequence):
            raise TypeError("Security scopes must be a sequence of strings")
        scopes = tuple(self.scopes)
        if any(not isinstance(scope, str) or not scope for scope in scopes):
            raise TypeError("Security scopes must be nonempty strings")
        object.__setattr__(self, "scopes", scopes)


__all__ = [
    "Body",
    "Cookie",
    "Depends",
    "File",
    "Form",
    "Header",
    "Path",
    "Query",
    "Security",
]
