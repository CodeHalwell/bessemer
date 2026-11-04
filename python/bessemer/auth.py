"""Native JWT bearer authentication and the verified principal.

Signatures, ``exp``/``nbf``/``iss``/``aud`` and the algorithm allow-list are
checked in Rust before any Python runs; invalid or missing tokens get a native
401. Handlers receive the verified claims by annotating a parameter with
``Principal``. Configuration names environment variables or files and never
contains key material.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any

_HMAC = frozenset({"HS256", "HS384", "HS512"})
_ALGORITHMS = _HMAC | frozenset(
    {
        "RS256",
        "RS384",
        "RS512",
        "PS256",
        "PS384",
        "PS512",
        "ES256",
        "ES384",
        "EdDSA",
    }
)


def _texts(value: Any, name: str) -> str | list[str] | None:
    if value is None:
        return None
    if isinstance(value, str):
        if not value or len(value) > 2048:
            raise ValueError(f"jwt {name} must be a nonempty string")
        return value
    if (
        isinstance(value, (list, tuple))
        and 1 <= len(value) <= 64
        and all(isinstance(item, str) and 0 < len(item) <= 2048 for item in value)
    ):
        return list(value)
    raise ValueError(
        f"jwt {name} must be a nonempty string or a list of 1 to 64 strings"
    )


@dataclass(frozen=True)
class JWTAuth:
    """Bearer JWT verification performed natively for every non-excluded path.

    HMAC algorithms read a secret of at least 32 bytes from ``secret_env``.
    Asymmetric algorithms read a JSON Web Key Set from ``jwks_env`` or
    ``jwks_file`` at startup; remote JWKS URLs are not fetched yet.
    """

    algorithms: Iterable[str] = ("RS256",)
    secret_env: str | None = None
    jwks_env: str | None = None
    jwks_file: str | None = None
    issuer: str | Iterable[str] | None = None
    audience: str | Iterable[str] | None = None
    leeway_seconds: int = 30
    require_exp: bool = True
    exclude_paths: Iterable[str] = field(default_factory=tuple)
    cache_size: int = 4096

    def to_dict(self) -> dict[str, Any]:
        return validate_jwt(
            {
                "algorithms": list(self.algorithms),
                "secret_env": self.secret_env,
                "jwks_env": self.jwks_env,
                "jwks_file": self.jwks_file,
                "issuer": self.issuer
                if self.issuer is None or isinstance(self.issuer, str)
                else list(self.issuer),
                "audience": self.audience
                if self.audience is None or isinstance(self.audience, str)
                else list(self.audience),
                "leeway_seconds": self.leeway_seconds,
                "require_exp": self.require_exp,
                "exclude_paths": list(self.exclude_paths),
                "cache_size": self.cache_size,
            }
        )


def validate_jwt(value: Any) -> dict[str, Any]:
    """Validate JWT settings without reading any secret or key material."""
    from .middleware import _environment, _path, _string_list

    if isinstance(value, JWTAuth):
        return value.to_dict()
    allowed = {
        "algorithms",
        "secret_env",
        "jwks_env",
        "jwks_file",
        "issuer",
        "audience",
        "leeway_seconds",
        "require_exp",
        "exclude_paths",
        "cache_size",
    }
    if not isinstance(value, dict) or set(value) - allowed:
        raise ValueError("jwt contains unsupported settings")
    algorithms = _string_list(value.get("algorithms"), "jwt algorithms", maximum=12)
    if not algorithms or set(algorithms) - _ALGORITHMS:
        raise ValueError(
            "jwt algorithms must be a nonempty list of supported JWS algorithms; none is never accepted"
        )
    symmetric = bool(set(algorithms) & _HMAC)
    asymmetric = bool(set(algorithms) - _HMAC)
    result: dict[str, Any] = {"algorithms": algorithms}
    secret = value.get("secret_env")
    if (secret is not None) != symmetric:
        raise ValueError(
            "secret_env is required for, and only for, HMAC jwt algorithms"
        )
    if secret is not None:
        result["secret_env"] = _environment(secret, "jwt secret_env")
    jwks_env, jwks_file = value.get("jwks_env"), value.get("jwks_file")
    if jwks_env is not None and jwks_file is not None:
        raise ValueError("jwt accepts only one of jwks_env and jwks_file")
    if (jwks_env is not None or jwks_file is not None) != asymmetric:
        raise ValueError(
            "jwks_env or jwks_file is required for, and only for, asymmetric jwt algorithms"
        )
    if jwks_env is not None:
        result["jwks_env"] = _environment(jwks_env, "jwt jwks_env")
    if jwks_file is not None:
        if not isinstance(jwks_file, str) or not jwks_file or "\0" in jwks_file:
            raise ValueError("jwt jwks_file must be a file path")
        result["jwks_file"] = jwks_file
    for name in ("issuer", "audience"):
        texts = _texts(value.get(name), name)
        if texts is not None:
            result[name] = texts
    leeway = value.get("leeway_seconds", 30)
    if type(leeway) is not int or not 0 <= leeway <= 300:
        raise ValueError("jwt leeway_seconds must be an integer between 0 and 300")
    result["leeway_seconds"] = leeway
    require_exp = value.get("require_exp", True)
    if type(require_exp) is not bool:
        raise ValueError("jwt require_exp must be a bool")
    result["require_exp"] = require_exp
    result["exclude_paths"] = [
        _path(path, "jwt exclude_paths")
        for path in _string_list(value.get("exclude_paths", []), "jwt exclude_paths")
    ]
    cache_size = value.get("cache_size", 4096)
    if type(cache_size) is not int or not 0 <= cache_size <= 1_000_000:
        raise ValueError("jwt cache_size must be an integer between 0 and 1000000")
    result["cache_size"] = cache_size
    return result


class Principal:
    """The verified JWT claims of the current request.

    Annotate a handler or dependency parameter with ``Principal`` to receive
    it. The claims were verified natively and are read-only.
    """

    __slots__ = ("_claims", "_scopes")

    def __init__(self, claims: Mapping[str, Any]) -> None:
        self._claims = MappingProxyType(dict(claims))
        self._scopes: frozenset[str] | None = None

    @property
    def claims(self) -> Mapping[str, Any]:
        return self._claims

    @property
    def subject(self) -> str | None:
        subject = self._claims.get("sub")
        return subject if isinstance(subject, str) else None

    @property
    def scopes(self) -> frozenset[str]:
        """Scopes from OAuth ``scope``/``scp`` and Entra ID ``roles`` claims."""
        if self._scopes is None:
            values: set[str] = set()
            for name in ("scope", "scp", "roles"):
                claim = self._claims.get(name)
                if isinstance(claim, str):
                    values.update(claim.split())
                elif isinstance(claim, (list, tuple)):
                    values.update(item for item in claim if isinstance(item, str))
            self._scopes = frozenset(values)
        return self._scopes

    def __getitem__(self, name: str) -> Any:
        return self._claims[name]

    def get(self, name: str, default: Any = None) -> Any:
        return self._claims.get(name, default)

    def __repr__(self) -> str:
        return f"Principal(subject={self.subject!r})"


def require_scope(*scopes: str):
    """A dependency returning the principal, or raising 403 without every scope."""
    if not scopes or any(not isinstance(scope, str) or not scope for scope in scopes):
        raise ValueError("require_scope needs at least one nonempty scope")
    required = frozenset(scopes)

    def dependency(principal: Principal) -> Principal:
        if not required <= principal.scopes:
            from .application import HTTPException

            raise HTTPException(403, "Insufficient scope")
        return principal

    dependency.__name__ = f"require_scope_{'_'.join(sorted(required))}"
    return dependency
