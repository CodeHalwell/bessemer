"""Declarative inputs and dependencies inspected while an application builds."""

from __future__ import annotations

import re
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
    # OpenAPI-only metadata, as in FastAPI. None of it affects validation.
    title: str | None = None
    examples: list[Any] | None = None
    openapi_examples: dict[str, Any] | None = None
    json_schema_extra: dict[str, Any] | None = None
    include_in_schema: bool = True
    # FastAPI's deprecated spellings: ``regex`` is ``pattern`` and
    # ``example`` is a one-item ``examples``.
    regex: str | None = None
    example: Any = UNSET
    source: ClassVar[str]

    def __post_init__(self) -> None:
        pattern = self.pattern
        if self.regex is not None:
            if pattern is not None:
                raise ValueError("declare a pattern with pattern= or regex=, not both")
            pattern = self.regex
        if isinstance(pattern, re.Pattern):
            pattern = pattern.pattern
        object.__setattr__(self, "pattern", pattern)
        object.__setattr__(self, "regex", None)
        if self.example is not UNSET:
            if self.examples is not None:
                raise ValueError("declare examples= or example=, not both")
            object.__setattr__(self, "examples", [self.example])
            object.__setattr__(self, "example", UNSET)
        if self.title is not None and not isinstance(self.title, str):
            raise TypeError("parameter title must be a string")
        if self.examples is not None and not isinstance(self.examples, list):
            raise TypeError("parameter examples must be a list")
        for option in ("openapi_examples", "json_schema_extra"):
            value = getattr(self, option)
            if value is not None and not isinstance(value, dict):
                raise TypeError(f"parameter {option} must be a dict")
        if type(self.include_in_schema) is not bool:
            raise TypeError("parameter include_in_schema must be a boolean")


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
    media_type: str = "application/json"
    source: ClassVar[str] = "body"

    def __post_init__(self) -> None:
        super().__post_init__()
        media = self.media_type
        if not isinstance(media, str) or not (
            media == "application/json"
            or (media.startswith("application/") and media.endswith("+json"))
        ):
            raise ValueError(
                "Body media_type must be application/json or an application/*+json type"
            )


# The form encodings the server parses; documentation follows the choice.
_FORM_MEDIA_TYPES = frozenset(
    {"application/x-www-form-urlencoded", "multipart/form-data"}
)


@dataclass(frozen=True)
class Form(Parameter):
    """A field of an urlencoded or multipart form body."""

    media_type: str = "application/x-www-form-urlencoded"
    source: ClassVar[str] = "form"

    def __post_init__(self) -> None:
        super().__post_init__()
        if self.media_type not in _FORM_MEDIA_TYPES:
            raise ValueError(
                "Form media_type must be application/x-www-form-urlencoded"
                " or multipart/form-data"
            )


@dataclass(frozen=True)
class File(Parameter):
    """A file part of a multipart form body (``UploadFile`` or ``bytes``)."""

    media_type: str = "multipart/form-data"
    source: ClassVar[str] = "file"

    def __post_init__(self) -> None:
        super().__post_init__()
        if self.media_type != "multipart/form-data":
            raise ValueError("File media_type must be multipart/form-data")


@dataclass(frozen=True)
class Depends:
    dependency: Callable[..., Any] | None = None
    use_cache: bool = True
    scope: str | None = "request"

    def __post_init__(self) -> None:
        if self.dependency is not None and not callable(self.dependency):
            raise TypeError("Depends expects a callable dependency")
        if type(self.use_cache) is not bool:
            raise TypeError("Depends use_cache must be a boolean")
        # FastAPI's scope=None (its default) and scope="function" (exit right
        # after the handler returns) both mean Bessemer's request scope, whose
        # generator dependencies already exit before the response is sent.
        if self.scope is None or self.scope == "function":
            object.__setattr__(self, "scope", "request")
        if self.scope not in {"request", "app"}:
            raise ValueError("Depends scope must be 'request', 'function' or 'app'")
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
