"""FastAPI-style security dependencies that also document themselves in OpenAPI.

Each scheme is a callable dependency: ``Depends(HTTPBearer())`` reads the
credentials from the request and returns them, or answers ``401`` with a
``WWW-Authenticate`` challenge when they are missing or malformed. With
``auto_error=False`` it returns ``None`` instead, so several schemes can be
tried in turn. The schemes only extract credentials; verifying them is the
application's job (use ``App(jwt=JWTAuth(...))`` for native JWT checks).
Routes that depend on a scheme list it under ``security`` in OpenAPI.
"""

from __future__ import annotations

import base64
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Annotated, Any, Literal

from .params import Form
from .request import Request
from .responses import HTTPException

__all__ = [
    "APIKeyCookie",
    "APIKeyHeader",
    "APIKeyQuery",
    "HTTPAuthorizationCredentials",
    "HTTPBasic",
    "HTTPBasicCredentials",
    "HTTPBearer",
    "OAuth2AuthorizationCodeBearer",
    "OAuth2PasswordBearer",
    "OAuth2PasswordRequestForm",
    "OAuth2PasswordRequestFormStrict",
]


@dataclass(frozen=True)
class HTTPAuthorizationCredentials:
    scheme: str
    credentials: str


@dataclass(frozen=True)
class HTTPBasicCredentials:
    username: str
    password: str = field(repr=False)


def _text(value: Any, name: str, *, optional: bool = True) -> str | None:
    if value is None and optional:
        return None
    if not isinstance(value, str) or not value:
        raise ValueError(f"{name} must be a nonempty string")
    return value


def _flag(value: Any, name: str) -> bool:
    if type(value) is not bool:
        raise TypeError(f"{name} must be a boolean")
    return value


def _scopes(scopes: Mapping[str, str] | None) -> dict[str, str]:
    if scopes is None:
        return {}
    if not isinstance(scopes, Mapping) or any(
        not isinstance(key, str) or not key or not isinstance(value, str)
        for key, value in scopes.items()
    ):
        raise TypeError("scopes must map scope names to descriptions")
    return dict(scopes)


def _authorization(request: Request) -> tuple[str, str] | None:
    value = request.headers.get("authorization")
    if not value:
        return None
    scheme, _, credentials = value.strip().partition(" ")
    credentials = credentials.strip()
    if not scheme or not credentials:
        return None
    return scheme, credentials


class SecurityBase:
    """A dependency that names an OpenAPI security scheme."""

    scheme_name: str
    model: dict[str, Any]
    auto_error: bool

    def _setup(
        self, scheme_name: str | None, description: str | None, auto_error: bool
    ) -> None:
        self.scheme_name = _text(scheme_name, "scheme_name") or type(self).__name__
        description = _text(description, "description")
        if description is not None:
            self.model["description"] = description
        self.auto_error = _flag(auto_error, "auto_error")

    def _reject(self, challenge: str, detail: str = "Not authenticated"):
        if self.auto_error:
            raise HTTPException(401, detail, headers={"WWW-Authenticate": challenge})


class HTTPBearer(SecurityBase):
    """``Authorization: Bearer <token>``, returned as credentials."""

    def __init__(
        self,
        *,
        bearerFormat: str | None = None,
        scheme_name: str | None = None,
        description: str | None = None,
        auto_error: bool = True,
    ) -> None:
        self.model = {"type": "http", "scheme": "bearer"}
        if _text(bearerFormat, "bearerFormat") is not None:
            self.model["bearerFormat"] = bearerFormat
        self._setup(scheme_name, description, auto_error)

    async def __call__(self, request: Request) -> HTTPAuthorizationCredentials | None:
        found = _authorization(request)
        if found is None:
            return self._reject("Bearer")
        if found[0].lower() != "bearer":
            return self._reject("Bearer", "Invalid authentication credentials")
        return HTTPAuthorizationCredentials(*found)


class HTTPBasic(SecurityBase):
    """``Authorization: Basic <base64>``, decoded into a username and password."""

    def __init__(
        self,
        *,
        scheme_name: str | None = None,
        realm: str | None = None,
        description: str | None = None,
        auto_error: bool = True,
    ) -> None:
        self.model = {"type": "http", "scheme": "basic"}
        realm = _text(realm, "realm")
        if realm is not None and ('"' in realm or "\\" in realm):
            raise ValueError("realm must not contain quotes or backslashes")
        self.realm = realm
        self._setup(scheme_name, description, auto_error)

    async def __call__(self, request: Request) -> HTTPBasicCredentials | None:
        challenge = "Basic" if self.realm is None else f'Basic realm="{self.realm}"'
        found = _authorization(request)
        if found is None:
            return self._reject(challenge)
        if found[0].lower() != "basic":
            return self._reject(challenge, "Invalid authentication credentials")
        try:
            decoded = base64.b64decode(found[1], validate=True).decode("utf-8")
        except ValueError:  # Bad base64 (including non-ASCII) or UTF-8.
            return self._reject(challenge, "Invalid authentication credentials")
        username, separator, password = decoded.partition(":")
        if not separator:
            return self._reject(challenge, "Invalid authentication credentials")
        return HTTPBasicCredentials(username, password)


class _OAuth2Bearer(SecurityBase):
    async def __call__(self, request: Request) -> str | None:
        found = _authorization(request)
        if found is None or found[0].lower() != "bearer":
            return self._reject("Bearer")
        return found[1]


class OAuth2PasswordBearer(_OAuth2Bearer):
    """An OAuth2 password-flow bearer token, returned as a string."""

    def __init__(
        self,
        tokenUrl: str,
        *,
        scheme_name: str | None = None,
        scopes: Mapping[str, str] | None = None,
        description: str | None = None,
        auto_error: bool = True,
        refreshUrl: str | None = None,
    ) -> None:
        flow = {"tokenUrl": _text(tokenUrl, "tokenUrl", optional=False)}
        if _text(refreshUrl, "refreshUrl") is not None:
            flow["refreshUrl"] = refreshUrl
        flow["scopes"] = _scopes(scopes)
        self.model = {"type": "oauth2", "flows": {"password": flow}}
        self._setup(scheme_name, description, auto_error)


class OAuth2AuthorizationCodeBearer(_OAuth2Bearer):
    """An OAuth2 authorization-code bearer token, returned as a string."""

    def __init__(
        self,
        authorizationUrl: str,
        tokenUrl: str,
        refreshUrl: str | None = None,
        *,
        scheme_name: str | None = None,
        scopes: Mapping[str, str] | None = None,
        description: str | None = None,
        auto_error: bool = True,
    ) -> None:
        flow = {
            "authorizationUrl": _text(
                authorizationUrl, "authorizationUrl", optional=False
            ),
            "tokenUrl": _text(tokenUrl, "tokenUrl", optional=False),
        }
        if _text(refreshUrl, "refreshUrl") is not None:
            flow["refreshUrl"] = refreshUrl
        flow["scopes"] = _scopes(scopes)
        self.model = {"type": "oauth2", "flows": {"authorizationCode": flow}}
        self._setup(scheme_name, description, auto_error)


class _APIKey(SecurityBase):
    location = ""

    def __init__(
        self,
        *,
        name: str,
        scheme_name: str | None = None,
        description: str | None = None,
        auto_error: bool = True,
    ) -> None:
        self.name = _text(name, "name", optional=False)
        self.model = {"type": "apiKey", "in": self.location, "name": self.name}
        self._setup(scheme_name, description, auto_error)

    def _value(self, request: Request) -> str | None:
        raise NotImplementedError

    async def __call__(self, request: Request) -> str | None:
        value = self._value(request)
        if not value:
            return self._reject("APIKey")
        return value


class APIKeyHeader(_APIKey):
    """An API key read from the named request header."""

    location = "header"

    def _value(self, request: Request) -> str | None:
        return request.headers.get(self.name)


class APIKeyQuery(_APIKey):
    """An API key read from the named query parameter."""

    location = "query"

    def _value(self, request: Request) -> str | None:
        return request.query_params.get(self.name)


class APIKeyCookie(_APIKey):
    """An API key read from the named cookie."""

    location = "cookie"

    def _value(self, request: Request) -> str | None:
        return request.cookies.get(self.name)


class OAuth2PasswordRequestForm:
    """The OAuth2 password-grant form, for a token endpoint.

    Use it as ``form: OAuth2PasswordRequestForm = Depends()``. ``scopes`` is
    the space-separated ``scope`` field split into a list.
    """

    def __init__(
        self,
        username: Annotated[str, Form()],
        password: Annotated[str, Form()],
        grant_type: Annotated[Literal["password"] | None, Form()] = None,
        scope: Annotated[str, Form()] = "",
        client_id: Annotated[str | None, Form()] = None,
        client_secret: Annotated[str | None, Form()] = None,
    ) -> None:
        self.grant_type = grant_type
        self.username = username
        self.password = password
        self.scopes = scope.split()
        self.client_id = client_id
        self.client_secret = client_secret


class OAuth2PasswordRequestFormStrict(OAuth2PasswordRequestForm):
    """The password-grant form, requiring ``grant_type=password``."""

    def __init__(
        self,
        grant_type: Annotated[Literal["password"], Form()],
        username: Annotated[str, Form()],
        password: Annotated[str, Form()],
        scope: Annotated[str, Form()] = "",
        client_id: Annotated[str | None, Form()] = None,
        client_secret: Annotated[str | None, Form()] = None,
    ) -> None:
        super().__init__(
            username=username,
            password=password,
            grant_type=grant_type,
            scope=scope,
            client_id=client_id,
            client_secret=client_secret,
        )
