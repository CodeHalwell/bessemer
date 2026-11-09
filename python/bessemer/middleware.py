"""Startup configuration for native request controls.

Secret configuration names environment variables; configuration values and
route/schema exports never contain resolved secret material.
"""

from __future__ import annotations

import ipaddress
import math
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

_HEADER = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+\Z", re.ASCII)
_ENVIRONMENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,127}\Z", re.ASCII)
_HOST_LABEL = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\Z", re.ASCII)
_METHODS = frozenset({"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"})
_SETTINGS = frozenset(
    {
        "cors",
        "security_headers",
        "request_id",
        "access_log",
        "trusted_proxies",
        "allowed_hosts",
        "api_key",
        "jwt",
        "rate_limit",
        "metrics_path",
        "metrics_token_env",
        "compression",
        "otel",
    }
)


def _string_list(value: Any, name: str, *, maximum: int = 1024) -> list[str]:
    if (
        not isinstance(value, (list, tuple))
        or len(value) > maximum
        or any(not isinstance(item, str) for item in value)
    ):
        raise ValueError(f"{name} must be a list of at most {maximum} strings")
    return list(value)


def _path(value: Any, name: str) -> str:
    if (
        not isinstance(value, str)
        or not value.startswith("/")
        or value.startswith("//")
        or any(character in value for character in "?#{}*\\")
        or any(ord(character) <= 32 or ord(character) >= 127 for character in value)
    ):
        raise ValueError(f"{name} must be an exact absolute path")
    return value


def _environment(value: Any, name: str) -> str:
    if not isinstance(value, str) or not _ENVIRONMENT.fullmatch(value):
        raise ValueError(f"{name} must name a valid environment variable")
    return value


def _header(value: Any, name: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 128
        or not _HEADER.fullmatch(value)
    ):
        raise ValueError(
            f"{name} must be a valid header name of at most 128 characters"
        )
    return value


def _allowed_host(value: str) -> str:
    wildcard = value.startswith("*.")
    host = value[2:] if wildcard else value
    bracketed = host.startswith("[") and host.endswith("]")
    literal = host[1:-1] if bracketed else host
    if "%" in literal:
        raise ValueError("allowed_hosts cannot contain scoped IP addresses")
    try:
        address = ipaddress.ip_address(literal)
    except ValueError:
        pass
    else:
        if wildcard:
            raise ValueError("allowed_hosts wildcards require a DNS suffix")
        if bracketed and address.version != 6:
            raise ValueError("allowed_hosts brackets require an IPv6 address")
        return str(address)
    host = host.removesuffix(".")
    if (
        not host
        or len(host) > 253
        or any(not _HOST_LABEL.fullmatch(label) for label in host.split("."))
    ):
        raise ValueError(
            "allowed_hosts must contain DNS names, IPs or *.DNS suffixes without ports"
        )
    return ("*." if wildcard else "") + host.lower()


def _origin(value: str) -> bool:
    if value in {"*", "null"}:
        return True
    if "?" in value or "#" in value:
        return False
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        return False
    if parsed.netloc.endswith(":") or "%" in parsed.netloc:
        return False
    if parsed.netloc.startswith("["):
        try:
            ipaddress.IPv6Address(parsed.hostname)
        except ValueError:
            return False
    return bool(
        parsed.scheme in {"http", "https"}
        and parsed.netloc
        and parsed.hostname
        and parsed.username is None
        and parsed.password is None
        and not parsed.path
        and not parsed.query
        and not parsed.fragment
        and all(32 < ord(character) < 127 for character in value)
        and (port is None or 0 <= port <= 65535)
    )


def _cors_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, CORS):
        value = value.to_dict()
    if not isinstance(value, dict) or set(value) - {
        "origins",
        "allow_methods",
        "allow_headers",
        "expose_headers",
        "allow_credentials",
        "max_age",
    }:
        raise ValueError("cors must be CORS or a dictionary of supported CORS settings")
    origins = _string_list(value.get("origins", []), "cors origins")
    if any(len(origin) > 2048 or not _origin(origin) for origin in origins):
        raise ValueError("cors origins must be HTTP(S) origins, null or *")
    credentials = value.get("allow_credentials", False)
    if type(credentials) is not bool:
        raise ValueError("cors allow_credentials must be a bool")
    if "*" in origins and (len(origins) != 1 or credentials):
        raise ValueError(
            "wildcard cors origins must be used alone and without credentials"
        )
    methods = _string_list(
        value.get("allow_methods", ["GET"]), "cors allow_methods", maximum=8
    )
    if not methods or (
        methods != ["*"] and any(method not in _METHODS for method in methods)
    ):
        raise ValueError("cors allow_methods contains an unsupported method")
    headers = _string_list(
        value.get("allow_headers", []), "cors allow_headers", maximum=128
    )
    if "*" in headers and headers != ["*"]:
        raise ValueError("wildcard cors allow_headers must be used alone")
    for header in headers:
        _header(header, "cors allow_headers")
    exposed = _string_list(
        value.get("expose_headers", []), "cors expose_headers", maximum=32
    )
    for header in exposed:
        _header(header, "cors expose_headers")
    if credentials and "*" in exposed:
        raise ValueError("credentialed cors expose_headers cannot contain a wildcard")
    age = value.get("max_age", 600)
    if type(age) is not int or not 0 <= age <= 86_400:
        raise ValueError("cors max_age must be an integer between 0 and 86400")
    return {
        "origins": origins,
        "allow_methods": methods,
        "allow_headers": headers,
        "expose_headers": exposed,
        "allow_credentials": credentials,
        "max_age": age,
    }


@dataclass(frozen=True)
class CORS:
    """Native CORS policy. Origins are exact and credentialed wildcards fail."""

    origins: Sequence[str] = field(default_factory=tuple)
    allow_methods: Sequence[str] = ("GET",)
    allow_headers: Sequence[str] = field(default_factory=tuple)
    expose_headers: Sequence[str] = field(default_factory=tuple)
    allow_credentials: bool = False
    max_age: int = 600

    def __post_init__(self) -> None:
        validated = _cors_dict(
            {
                "origins": self.origins,
                "allow_methods": self.allow_methods,
                "allow_headers": self.allow_headers,
                "expose_headers": self.expose_headers,
                "allow_credentials": self.allow_credentials,
                "max_age": self.max_age,
            }
        )
        for name in ("origins", "allow_methods", "allow_headers", "expose_headers"):
            object.__setattr__(self, name, tuple(validated[name]))

    def to_dict(self) -> dict[str, Any]:
        # Validate at App construction too; copying lists keeps later caller
        # mutations out of the frozen native runtime settings.
        return {
            "origins": list(self.origins),
            "allow_methods": list(self.allow_methods),
            "allow_headers": list(self.allow_headers),
            "expose_headers": list(self.expose_headers),
            "allow_credentials": self.allow_credentials,
            "max_age": self.max_age,
        }


@dataclass(frozen=True)
class Compression:
    """Native gzip and Brotli response compression.

    The coding is negotiated from ``Accept-Encoding`` (Brotli is preferred at
    equal quality). Only text-like media types at least ``min_size`` bytes
    long are compressed; streaming responses are compressed frame by frame.
    """

    gzip: bool = True
    br: bool = True
    min_size: int = 1024

    def __post_init__(self) -> None:
        _compression_dict(self.to_dict())

    def to_dict(self) -> dict[str, Any]:
        return {"gzip": self.gzip, "br": self.br, "min_size": self.min_size}


def _compression_dict(value: Any) -> dict[str, Any]:
    if value is True:
        value = {}
    if isinstance(value, Compression):
        value = value.to_dict()
    if not isinstance(value, dict) or set(value) - {"gzip", "br", "min_size"}:
        raise ValueError(
            "compression must be Compression, True or a dictionary of gzip, br "
            "and min_size"
        )
    result: dict[str, Any] = {}
    for name in ("gzip", "br"):
        setting = value.get(name, True)
        if type(setting) is not bool:
            raise ValueError(f"compression {name} must be a bool")
        result[name] = setting
    if not (result["gzip"] or result["br"]):
        raise ValueError("compression must enable gzip or br")
    size = value.get("min_size", 1024)
    if type(size) is not int or not 0 <= size <= 1 << 30:
        raise ValueError("compression min_size must be an integer from 0 to 2**30")
    result["min_size"] = size
    return result


def validate_settings(value: Any) -> dict[str, Any]:
    """Return copied JSON settings; validate without reading any secret value."""
    if not isinstance(value, dict) or set(value) - _SETTINGS:
        raise ValueError("native controls contain unsupported settings")
    result: dict[str, Any] = {}
    for name, default in (
        ("security_headers", True),
        ("request_id", True),
        ("access_log", False),
    ):
        setting = value.get(name, default)
        if type(setting) is not bool:
            raise ValueError(f"{name} must be a bool")
        result[name] = setting
    cors = value.get("cors")
    result["cors"] = None if cors is None else _cors_dict(cors)
    hosts = value.get("allowed_hosts")
    result["allowed_hosts"] = (
        None
        if hosts is None
        else [_allowed_host(host) for host in _string_list(hosts, "allowed_hosts")]
    )
    proxies_setting = value.get("trusted_proxies")
    proxies = _string_list(
        [] if proxies_setting is None else proxies_setting, "trusted_proxies"
    )
    try:
        result["trusted_proxies"] = [
            str(ipaddress.ip_network(proxy, strict=False)) for proxy in proxies
        ]
    except ValueError as error:
        raise ValueError("trusted_proxies must contain valid CIDRs") from error
    key = value.get("api_key")
    if key is not None:
        if not isinstance(key, dict) or set(key) - {"env", "header", "exclude_paths"}:
            raise ValueError("api_key must contain only env, header and exclude_paths")
        header = _header(key.get("header", "X-API-Key"), "api_key header")
        header_lower = header.lower()
        if header_lower in {
            "host",
            "content-length",
            "transfer-encoding",
            "connection",
            "origin",
            "x-request-id",
            "forwarded",
        } or header_lower.startswith(("access-control-", "x-forwarded-")):
            raise ValueError("api_key cannot use an HTTP framing or metadata header")
        key = {
            "env": _environment(key.get("env"), "api_key env"),
            "header": header,
            "exclude_paths": [
                _path(path, "api_key exclude_paths")
                for path in _string_list(
                    key.get("exclude_paths", []), "api_key exclude_paths"
                )
            ],
        }
    result["api_key"] = key
    jwt = value.get("jwt")
    if jwt is not None:
        from .auth import validate_jwt

        jwt = validate_jwt(jwt)
    result["jwt"] = jwt
    rate = value.get("rate_limit")
    if rate is not None:
        if not isinstance(rate, dict) or set(rate) - {
            "requests",
            "window_seconds",
            "max_keys",
            "exclude_paths",
            "redis",
        }:
            raise ValueError("rate_limit contains unsupported settings")
        redis = rate.get("redis")
        if redis is not None:
            if "max_keys" in rate:
                raise ValueError(
                    "rate_limit max_keys bounds the local table and does not apply with redis"
                )
            redis = _redis(redis)
        requests = rate.get("requests")
        maximum = rate.get("max_keys", 10_000)
        seconds = rate.get("window_seconds")
        if (
            type(requests) is not int
            or not 1 <= requests <= 10_000
            or type(maximum) is not int
            or not 1 <= maximum <= 100_000
            or (redis is None and requests * maximum > 5_000_000)
        ):
            raise ValueError(
                "rate_limit requires positive bounded requests/max_keys with product at most 5000000"
            )
        if (
            isinstance(seconds, bool)
            or not isinstance(seconds, (int, float))
            or not math.isfinite(seconds)
            or not 0.001 <= seconds <= 86_400
        ):
            raise ValueError(
                "rate_limit window_seconds must be between 0.001 and 86400"
            )
        rate = {
            "requests": requests,
            "window_seconds": float(seconds),
            "exclude_paths": [
                _path(path, "rate_limit exclude_paths")
                for path in _string_list(
                    rate.get("exclude_paths", []), "rate_limit exclude_paths"
                )
            ],
        }
        if redis is None:
            rate["max_keys"] = maximum
        else:
            rate["redis"] = redis
    result["rate_limit"] = rate
    path = value.get("metrics_path")
    token = value.get("metrics_token_env")
    result["metrics_path"] = None if path is None else _path(path, "metrics_path")
    result["metrics_token_env"] = (
        None if token is None else _environment(token, "metrics_token_env")
    )
    if path is not None and key is None and token is None:
        raise ValueError(
            "metrics_path requires api_key or metrics_token_env authentication"
        )
    if path is None and token is not None:
        raise ValueError("metrics_token_env requires metrics_path")
    compression = value.get("compression")
    result["compression"] = (
        None if compression in (None, False) else _compression_dict(compression)
    )
    otel = value.get("otel")
    if otel is not None:
        from .otel import otel_dict

        otel = otel_dict(otel)
    result["otel"] = otel
    return result


_REDIS = {
    "url_env",
    "key_prefix",
    "timeout_seconds",
    "pool_size",
    "ca_file",
    "fail_open",
}


def _redis(value: Any) -> dict[str, Any]:
    """Validate the shared Redis store for ``rate_limit``."""
    if not isinstance(value, dict) or set(value) - _REDIS:
        raise ValueError(
            "rate_limit redis must be a dictionary of url_env, key_prefix, "
            "timeout_seconds, pool_size, ca_file and fail_open"
        )
    if "url_env" not in value:
        raise ValueError("rate_limit redis url_env is required")
    prefix = value.get("key_prefix", "bessemer:rate:")
    if (
        not isinstance(prefix, str)
        or not 1 <= len(prefix) <= 128
        or not all("!" <= character <= "~" for character in prefix)
    ):
        raise ValueError(
            "rate_limit redis key_prefix must have 1 to 128 visible ASCII characters"
        )
    timeout = value.get("timeout_seconds", 0.25)
    if (
        isinstance(timeout, bool)
        or not isinstance(timeout, (int, float))
        or not math.isfinite(timeout)
        or not 0.001 <= timeout <= 10
    ):
        raise ValueError(
            "rate_limit redis timeout_seconds must be between 0.001 and 10"
        )
    pool = value.get("pool_size", 16)
    if type(pool) is not int or not 1 <= pool <= 1024:
        raise ValueError(
            "rate_limit redis pool_size must be an integer between 1 and 1024"
        )
    fail_open = value.get("fail_open", False)
    if fail_open is not True and fail_open is not False:
        raise ValueError("rate_limit redis fail_open must be a bool")
    ca_file = value.get("ca_file")
    if ca_file is not None and (not isinstance(ca_file, str) or not ca_file):
        raise ValueError("rate_limit redis ca_file must be a path")
    result = {
        "url_env": _environment(value["url_env"], "rate_limit redis url_env"),
        "key_prefix": prefix,
        "timeout_seconds": float(timeout),
        "pool_size": pool,
        "fail_open": fail_open,
    }
    if ca_file is not None:
        result["ca_file"] = ca_file
    return result


# FastAPI/Starlette-style middleware classes -----------------------------------
#
# ``app.add_middleware(CORSMiddleware, allow_origins=[...])`` ports unchanged.
# The CORS, GZip and trusted-host classes configure Bessemer's native
# controls, which run in Rust before any Python, so they cost nothing per
# request in Python. They are applied when added and are not ASGI apps.


class Middleware:
    """An entry for ``App(middleware=[Middleware(cls, **options)])``."""

    def __init__(self, cls: type, *args: Any, **kwargs: Any) -> None:
        self.cls = cls
        self.args = args
        self.kwargs = kwargs

    def __iter__(self):
        return iter((self.cls, self.args, self.kwargs))


class _NativeMiddleware:
    """Base of the classes that configure a native control on an App."""

    @staticmethod
    def _configure(app: Any, setting: str, value: Any) -> None:
        current = app._settings[setting]
        if current is not None and current != value:
            raise ValueError(
                f"{setting} is already configured on this app; configure it once"
            )
        app._settings[setting] = value


class CORSMiddleware(_NativeMiddleware):
    """Starlette's ``CORSMiddleware`` options, served by native CORS.

    Unlike Starlette, a wildcard origin cannot be combined with credentials:
    list the exact origins instead. ``allow_origin_regex`` and
    ``allow_private_network`` are not supported and fail.
    """

    def __init__(
        self,
        app: Any,
        allow_origins: Sequence[str] = (),
        allow_methods: Sequence[str] = ("GET",),
        allow_headers: Sequence[str] = (),
        allow_credentials: bool = False,
        allow_origin_regex: str | None = None,
        allow_private_network: bool = False,
        expose_headers: Sequence[str] = (),
        max_age: int = 600,
    ) -> None:
        if allow_origin_regex is not None:
            raise ValueError(
                "allow_origin_regex is not supported; list allow_origins exactly"
            )
        if allow_private_network:
            raise ValueError("allow_private_network is not supported")
        methods = list(allow_methods)
        if "*" in methods:
            methods = ["*"]
        self._configure(
            app,
            "cors",
            CORS(
                origins=tuple(allow_origins),
                allow_methods=tuple(methods),
                allow_headers=tuple(allow_headers),
                expose_headers=tuple(expose_headers),
                allow_credentials=allow_credentials,
                max_age=max_age,
            ),
        )


class GZipMiddleware(_NativeMiddleware):
    """Starlette's ``GZipMiddleware``, served by native gzip compression.

    ``minimum_size`` is honoured; Bessemer picks its own compression level,
    so ``compresslevel`` must stay at Starlette's default.
    """

    def __init__(
        self, app: Any, minimum_size: int = 500, compresslevel: int = 9
    ) -> None:
        if compresslevel != 9:
            raise ValueError("compresslevel is not configurable in Bessemer")
        self._configure(
            app, "compression", Compression(gzip=True, br=False, min_size=minimum_size)
        )


class TrustedHostMiddleware(_NativeMiddleware):
    """Starlette's ``TrustedHostMiddleware``, served by native host checks.

    ``["*"]`` allows every host. Requests for other hosts are refused; the
    ``www.`` redirect Starlette offers is not performed.
    """

    def __init__(
        self,
        app: Any,
        allowed_hosts: Sequence[str] | None = None,
        www_redirect: bool = True,
    ) -> None:
        if type(www_redirect) is not bool:
            raise TypeError("www_redirect must be a boolean")
        if isinstance(allowed_hosts, str):
            raise TypeError("allowed_hosts must be a sequence of host names")
        hosts = None if allowed_hosts is None else list(allowed_hosts)
        if hosts is None or "*" in hosts:
            if hosts not in (None, ["*"]):
                raise ValueError("a wildcard allowed host must be used alone")
            return
        self._configure(app, "allowed_hosts", hosts)


class BaseHTTPMiddleware:
    """Starlette's ``BaseHTTPMiddleware``: subclass it and define
    ``async def dispatch(self, request, call_next)``, or pass ``dispatch=``.

    ``app.add_middleware(MyMiddleware)`` registers ``dispatch`` exactly like
    ``@app.middleware("http")``, with the same ordering.
    """

    def __init__(self, app: Any, dispatch: Any = None) -> None:
        self.app = app
        self.dispatch_func = self.dispatch if dispatch is None else dispatch
        dispatch_func = self.dispatch_func

        async def middleware(request, call_next):
            return await dispatch_func(request, call_next)

        app.middleware("http")(middleware)

    async def dispatch(self, request: Any, call_next: Any) -> Any:
        raise NotImplementedError(
            "subclass BaseHTTPMiddleware and define dispatch, or pass dispatch="
        )


class HTTPSRedirectMiddleware(BaseHTTPMiddleware):
    """Starlette's ``HTTPSRedirectMiddleware``, redirecting HTTP requests to HTTPS."""

    async def dispatch(self, request: Any, call_next: Any) -> Any:
        from .responses import RedirectResponse

        if request.url.scheme in ("http", "ws"):
            redirect_scheme = {"http": "https", "ws": "wss"}[request.url.scheme]
            if request.url.port in (80, None):
                hostname = request.url.hostname or ""
                netloc = f"[{hostname}]" if ":" in hostname else hostname
            else:
                netloc = request.url.netloc
            url = request.url.replace(scheme=redirect_scheme, netloc=netloc)
            # Behind a prefix-stripping proxy the external path starts with
            # root_path, as the slash redirects' Location does.
            root = _root_path(request)
            if root:
                url = url.replace(path=root + url.path)
            return RedirectResponse(str(url), status_code=307)
        return await call_next(request)


def _root_path(request: Any) -> str:
    """The outermost application's ``root_path``, through any mounts."""
    app = getattr(request, "app", None)
    while getattr(app, "_mounted", None):
        app = app._mounted[0]
    return getattr(app, "root_path", "") or ""


def _register_submodules() -> None:
    import sys
    import types

    for subname, attr, cls in (
        ("cors", "CORSMiddleware", CORSMiddleware),
        ("gzip", "GZipMiddleware", GZipMiddleware),
        ("trustedhost", "TrustedHostMiddleware", TrustedHostMiddleware),
        ("httpsredirect", "HTTPSRedirectMiddleware", HTTPSRedirectMiddleware),
    ):
        fullname = f"{__name__}.{subname}"
        mod = types.ModuleType(fullname)
        setattr(mod, attr, cls)
        mod.__all__ = [attr]
        sys.modules[fullname] = mod
        globals()[subname] = mod


__path__: list[str] = []
_register_submodules()
