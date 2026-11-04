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


def _texts(value: Any, name: str, maximum: int = 64) -> str | list[str] | None:
    if value is None:
        return None
    if isinstance(value, str):
        if not value or len(value) > 2048:
            raise ValueError(f"jwt {name} must be a nonempty string")
        return value
    if (
        isinstance(value, (list, tuple))
        and 1 <= len(value) <= maximum
        and all(isinstance(item, str) and 0 < len(item) <= 2048 for item in value)
    ):
        return list(value)
    raise ValueError(
        f"jwt {name} must be a nonempty string or a list of 1 to {maximum} strings"
    )


@dataclass(frozen=True)
class JWTAuth:
    """Bearer JWT verification performed natively for every non-excluded path.

    HMAC algorithms read a secret of at least 32 bytes from ``secret_env``.
    Asymmetric algorithms read a JSON Web Key Set from ``jwks_env`` or
    ``jwks_file`` at startup, or fetch it from ``jwks_url``. A remote set is
    refreshed every ``jwks_refresh_seconds`` in the background, and a token
    naming an unknown ``kid`` prompts an early, rate-limited refetch so key
    rotation is picked up without a restart. ``jwks_url`` must be ``https``
    (``http`` only for loopback hosts); ``jwks_ca_file`` adds a private CA.
    """

    algorithms: Iterable[str] = ("RS256",)
    secret_env: str | None = None
    jwks_env: str | None = None
    jwks_file: str | None = None
    jwks_url: str | None = None
    jwks_ca_file: str | None = None
    jwks_refresh_seconds: int | None = None
    issuer: str | Iterable[str] | None = None
    audience: str | Iterable[str] | None = None
    leeway_seconds: int = 30
    require_exp: bool = True
    exclude_paths: Iterable[str] = field(default_factory=tuple)
    cache_size: int = 4096

    @classmethod
    def entra(
        cls,
        tenant_id: str | Iterable[str],
        audience: str | Iterable[str],
        *,
        cloud: str = "global",
        version: int = 2,
        exclude_paths: Iterable[str] = (),
        leeway_seconds: int = 30,
        jwks_refresh_seconds: int | None = None,
        cache_size: int = 4096,
    ) -> JWTAuth:
        """Settings for access tokens issued by Microsoft Entra ID.

        ``tenant_id`` is the directory (tenant) GUID, or for a multi-tenant
        API a list of up to 1024 tenant GUIDs it accepts tokens from. Each
        token issuer embeds its tenant, so only listed tenants are trusted:
        domain names and the ``common``/``organizations`` aliases are
        rejected because they cannot be checked. ``audience`` is the API's
        application (client) ID and/or its Application ID URI. ``version``
        selects the token format the API's app registration issues (``2`` by
        default; ``1`` is available in the global and China clouds). Keys are
        fetched from Entra's published key set and refreshed automatically.
        """
        from uuid import UUID

        authorities = {
            "global": ("https://login.microsoftonline.com", "https://sts.windows.net"),
            "usgov": ("https://login.microsoftonline.us", None),
            "china": ("https://login.chinacloudapi.cn", "https://sts.chinacloudapi.cn"),
        }
        if cloud not in authorities:
            raise ValueError("entra cloud must be 'global', 'usgov' or 'china'")
        requested = [tenant_id] if isinstance(tenant_id, str) else tenant_id
        try:
            requested = list(requested)
        except TypeError:
            requested = [None]
        tenants: list[str] = []
        for value in requested:
            try:
                if not isinstance(value, str) or len(value) != 36:
                    raise ValueError
                tenant = str(UUID(value))
            except ValueError:
                raise ValueError(
                    "entra tenant_id must be directory (tenant) GUIDs; "
                    "the common and organizations aliases and domain names "
                    "are not supported"
                ) from None
            if tenant not in tenants:
                tenants.append(tenant)
        if not 1 <= len(tenants) <= 1024:
            raise ValueError("entra tenant_id must name 1 to 1024 tenants")
        authority, legacy = authorities[cloud]
        # Entra signs every tenant's tokens with the same keys; a single
        # tenant reads them from its own endpoint, several from ``common``.
        keys_tenant = tenants[0] if len(tenants) == 1 else "common"
        if type(version) is not int:
            version = 0
        if version == 2:
            issuers = [f"{authority}/{tenant}/v2.0" for tenant in tenants]
            keys = f"{authority}/{keys_tenant}/discovery/v2.0/keys"
        elif version == 1 and legacy is not None:
            issuers = [f"{legacy}/{tenant}/" for tenant in tenants]
            keys = f"{authority}/{keys_tenant}/discovery/keys"
        else:
            raise ValueError(
                "entra version must be 2, or 1 in the global and china clouds"
            )
        issuer = issuers[0] if len(issuers) == 1 else issuers
        if _texts(audience, "audience") is None:
            raise ValueError("entra audience is required")
        return cls(
            algorithms=("RS256",),
            jwks_url=keys,
            jwks_refresh_seconds=jwks_refresh_seconds,
            issuer=issuer,
            audience=audience,
            leeway_seconds=leeway_seconds,
            exclude_paths=exclude_paths,
            cache_size=cache_size,
        )

    def to_dict(self) -> dict[str, Any]:
        return validate_jwt(
            {
                "algorithms": list(self.algorithms),
                "secret_env": self.secret_env,
                "jwks_env": self.jwks_env,
                "jwks_file": self.jwks_file,
                "jwks_url": self.jwks_url,
                "jwks_ca_file": self.jwks_ca_file,
                "jwks_refresh_seconds": self.jwks_refresh_seconds,
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


def _jwks_url(url: Any) -> str:
    from ipaddress import ip_address
    from urllib.parse import urlsplit

    message = "jwt jwks_url must be an https URL (http only for localhost)"
    if (
        not isinstance(url, str)
        or len(url) > 2048
        or any(not 33 <= ord(character) <= 126 for character in url)
    ):
        raise ValueError(message)
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        raise ValueError(message) from None
    host = (parts.hostname or "").lower()
    if (
        parts.scheme not in {"https", "http"}
        or not host
        or parts.username is not None
        or parts.fragment
        or "#" in url
        or port == 0
    ):
        raise ValueError(message)
    if parts.scheme == "http":
        try:
            loopback = ip_address(host).is_loopback
        except ValueError:
            loopback = host == "localhost"
        if not loopback:
            raise ValueError(message)
    return url


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
        "jwks_url",
        "jwks_ca_file",
        "jwks_refresh_seconds",
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
    jwks_url = value.get("jwks_url")
    sources = [item for item in (jwks_env, jwks_file, jwks_url) if item is not None]
    if len(sources) > 1:
        raise ValueError("jwt accepts only one of jwks_env, jwks_file and jwks_url")
    if bool(sources) != asymmetric:
        raise ValueError(
            "jwks_env, jwks_file or jwks_url is required for, and only for, "
            "asymmetric jwt algorithms"
        )
    ca_file = value.get("jwks_ca_file")
    refresh = value.get("jwks_refresh_seconds")
    if jwks_url is None and (ca_file is not None or refresh is not None):
        raise ValueError("jwt jwks_ca_file and jwks_refresh_seconds require jwks_url")
    if jwks_url is not None:
        result["jwks_url"] = _jwks_url(jwks_url)
        if ca_file is not None:
            if not isinstance(ca_file, str) or not ca_file or "\0" in ca_file:
                raise ValueError("jwt jwks_ca_file must be a file path")
            if not result["jwks_url"].startswith("https://"):
                raise ValueError("jwt jwks_ca_file requires an https jwks_url")
            result["jwks_ca_file"] = ca_file
        if refresh is not None:
            if type(refresh) is not int or not 30 <= refresh <= 86_400:
                raise ValueError(
                    "jwt jwks_refresh_seconds must be an integer between 30 and 86400"
                )
            result["jwks_refresh_seconds"] = refresh
    if jwks_env is not None:
        result["jwks_env"] = _environment(jwks_env, "jwt jwks_env")
    if jwks_file is not None:
        if not isinstance(jwks_file, str) or not jwks_file or "\0" in jwks_file:
            raise ValueError("jwt jwks_file must be a file path")
        result["jwks_file"] = jwks_file
    # A multi-tenant API may trust one issuer per tenant.
    for name, maximum in (("issuer", 1024), ("audience", 64)):
        texts = _texts(value.get(name), name, maximum)
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
