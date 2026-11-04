"""Route registration and compilation. Signature inspection occurs at startup."""

from __future__ import annotations

import copy
import inspect
import math
import re
import types
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from .params import Depends
from .responses import HTTPException
from .schema import (
    Binding,
    CallablePlan,
    _callable_hints,
    _callable_kind,
    compile_inputs,
    response_adapter,
)

_MISSING = inspect.Parameter.empty
_METHODS = frozenset({"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"})
_PARAMETER = re.compile(r"\{([a-zA-Z_][a-zA-Z_0-9]*)\}")
__all__ = ["App", "HTTPException", "Router"]


@dataclass(frozen=True)
class _Route:
    method: str
    path: str
    handler: Callable[..., Any] | None
    status_code: int
    timeout: float | None
    kind: str = "python"
    dependencies: tuple[Depends, ...] = ()
    response_model: Any = _MISSING
    tags: tuple[str, ...] = ()
    operation_id: str | None = None
    summary: str | None = None
    description: str | None = None
    include_in_schema: bool = True
    content: bytes = b""
    content_type: str = "application/json"


@dataclass(frozen=True)
class _CompiledRoute:
    declaration: dict[str, Any]
    handler: Callable[..., Any] | None
    body_models: dict[str, type]
    body_containers: dict[str, type]
    nullable_body: frozenset[str]
    plan: CallablePlan | None = None
    dependencies: tuple[CallablePlan, ...] = ()
    before: tuple[Binding, ...] = ()
    response_model: Any = None
    response_adapter: Any = None
    input_validators: dict[str, Callable] | None = None


def _path(path: str, *, prefix: bool = False) -> str:
    if prefix and path == "":
        return path
    if not isinstance(path, str) or not path.startswith("/"):
        raise ValueError("route paths and nonempty prefixes must begin with /")
    if not path.isascii():
        raise ValueError(
            "literal route paths must be ASCII; percent-encode non-ASCII segments"
        )
    if (
        any(character in path for character in "?#\\")
        or any(ord(character) <= 32 or ord(character) == 127 for character in path)
        or "//" in path
    ):
        raise ValueError(f"invalid route path {path!r}")
    if prefix and path.endswith("/"):
        raise ValueError("router prefixes must not end with /")
    without_parameters = _PARAMETER.sub("", path)
    if "{" in without_parameters or "}" in without_parameters or "*" in path:
        raise ValueError(
            "only named path parameters such as /items/{item_id} are supported"
        )
    for segment in path.split("/"):
        if "{" in segment and _PARAMETER.fullmatch(segment) is None:
            raise ValueError("a path parameter must occupy an entire segment")
    names = _PARAMETER.findall(path)
    if len(names) != len(set(names)):
        raise ValueError("path parameter names must be unique within a route")
    return path


def _positive_float(value: float, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{name} must be a positive finite number")
    value = float(value)
    if not math.isfinite(value) or value < 1e-9 or value > 86_400:
        raise ValueError(f"{name} must be between one nanosecond and 86400 seconds")
    return value


def _dependencies(values: Iterable[Depends]) -> tuple[Depends, ...]:
    values = tuple(values)
    if any(not isinstance(value, Depends) for value in values):
        raise TypeError("dependencies must be Depends instances")
    return values


def _tags(values: Iterable[str]) -> tuple[str, ...]:
    if isinstance(values, str):
        raise TypeError("tags must be an iterable of strings, not a single string")
    values = tuple(values)
    if any(not isinstance(value, str) or not value for value in values):
        raise ValueError("tags must be nonempty strings")
    return tuple(dict.fromkeys(values))


def _compile(
    route: _Route, route_id: int, overrides: Mapping[Callable, Callable] | None = None
) -> _CompiledRoute:
    declaration: dict[str, Any] = {
        "id": route_id,
        "method": route.method,
        "path": route.path,
        "status_code": route.status_code,
        "timeout": route.timeout,
        "kind": route.kind,
        "parameters": [],
        "tier": 2 if route.kind in ("python", "websocket") else 0,
        "reason": "Python compatibility bridge"
        if route.kind == "python"
        else "native WebSocket session"
        if route.kind == "websocket"
        else "native health check"
        if route.kind == "health"
        else "pre-built native response",
        "needs_request": False,
        "needs_principal": False,
    }
    if route.kind == "static":
        declaration.update(content=route.content, content_type=route.content_type)
    if route.handler is None:
        return _CompiledRoute(
            declaration, None, {}, {}, frozenset(), input_validators={}
        )
    websocket = route.kind == "websocket"
    inputs = compile_inputs(
        route.handler,
        route.path,
        route.dependencies,
        websocket=websocket,
        overrides=overrides,
    )
    declaration["parameters"] = list(inputs.parameters)
    declaration["needs_request"] = inputs.needs_request or websocket
    declaration["needs_principal"] = inputs.needs_principal
    model = route.response_model
    if model is _MISSING:
        try:
            model = _callable_hints(route.handler).get("return")
        except (TypeError, NameError) as error:
            raise ValueError(
                f"cannot resolve response annotation for {route.path}: {error}"
            ) from error
    adapter = response_adapter(model)
    return _CompiledRoute(
        declaration,
        route.handler,
        inputs.body_models,
        inputs.body_containers,
        inputs.nullable_body,
        inputs.plan,
        inputs.dependencies,
        inputs.before,
        model,
        adapter,
        inputs.input_validators,
    )


class Router:
    """A group of routes compiled once before the server accepts requests."""

    def __init__(
        self,
        *,
        prefix: str = "",
        tags: Iterable[str] = (),
        dependencies: Iterable[Depends] = (),
    ) -> None:
        self.prefix = _path(prefix, prefix=True)
        self.tags = _tags(tags)
        self.dependencies = _dependencies(dependencies)
        self._routes: list[_Route] = []
        self._built = False

    def route(
        self,
        path: str,
        *,
        methods: Iterable[str],
        status_code: int = 200,
        timeout: float | None = None,
        dependencies: Iterable[Depends] = (),
        response_model: Any = _MISSING,
        tags: Iterable[str] = (),
        operation_id: str | None = None,
        summary: str | None = None,
        description: str | None = None,
        include_in_schema: bool = True,
    ):
        full_path = _path(self.prefix + _path(path))
        if type(status_code) is not int or not 200 <= status_code <= 599:
            raise ValueError(
                "status_code must be a final HTTP status between 200 and 599"
            )
        timeout = None if timeout is None else _positive_float(timeout, "timeout")
        if isinstance(methods, str):
            raise TypeError("methods must be an iterable of HTTP method strings")
        selected = tuple(method.upper() for method in methods)
        if (
            not selected
            or len(selected) != len(set(selected))
            or any(method not in _METHODS for method in selected)
        ):
            raise ValueError("methods must be unique supported HTTP methods")
        route_dependencies = self.dependencies + _dependencies(dependencies)
        route_tags = _tags((*self.tags, *_tags(tags)))
        for name, value in (
            ("operation_id", operation_id),
            ("summary", summary),
            ("description", description),
        ):
            if value is not None and (not isinstance(value, str) or not value):
                raise ValueError(f"{name} must be a nonempty string when supplied")
        if type(include_in_schema) is not bool:
            raise TypeError("include_in_schema must be a boolean")

        def register(handler):
            if self._built:
                raise RuntimeError("routes cannot change after app.build()")
            if not callable(handler):
                raise TypeError("a route handler must be callable")
            for method in selected:
                self._routes.append(
                    _Route(
                        method,
                        full_path,
                        handler,
                        status_code,
                        timeout,
                        dependencies=route_dependencies,
                        response_model=response_model,
                        tags=route_tags,
                        operation_id=operation_id,
                        summary=summary,
                        description=description,
                        include_in_schema=include_in_schema,
                    )
                )
            return handler

        return register

    def get(self, path: str, **options):
        return self.route(path, methods=("GET",), **options)

    def post(self, path: str, **options):
        return self.route(path, methods=("POST",), **options)

    def put(self, path: str, **options):
        return self.route(path, methods=("PUT",), **options)

    def patch(self, path: str, **options):
        return self.route(path, methods=("PATCH",), **options)

    def delete(self, path: str, **options):
        return self.route(path, methods=("DELETE",), **options)

    def head(self, path: str, **options):
        return self.route(path, methods=("HEAD",), **options)

    def options(self, path: str, **options):
        return self.route(path, methods=("OPTIONS",), **options)

    def websocket(self, path: str, *, dependencies: Iterable[Depends] = ()):
        """Register an ``async def handler(websocket: WebSocket)`` route."""
        full_path = _path(self.prefix + _path(path))
        route_dependencies = self.dependencies + _dependencies(dependencies)

        def register(handler):
            if self._built:
                raise RuntimeError("routes cannot change after app.build()")
            if not inspect.iscoroutinefunction(handler):
                raise TypeError("WebSocket handlers must be async functions")
            self._routes.append(
                _Route(
                    "GET",
                    full_path,
                    handler,
                    200,
                    None,
                    "websocket",
                    dependencies=route_dependencies,
                    include_in_schema=False,
                )
            )
            return handler

        return register

    def include_router(
        self,
        router: Router,
        *,
        prefix: str = "",
        tags: Iterable[str] = (),
        dependencies: Iterable[Depends] = (),
    ) -> None:
        if self._built:
            raise RuntimeError("routes cannot change after app.build()")
        if not isinstance(router, Router) or router is self:
            raise TypeError("include_router expects another Router")
        prefix = self.prefix + _path(prefix, prefix=True)
        included_dependencies = self.dependencies + _dependencies(dependencies)
        included_tags = _tags((*self.tags, *_tags(tags)))
        for route in router._routes:
            values = dict(route.__dict__)
            values.update(
                path=_path(prefix + route.path),
                dependencies=included_dependencies + route.dependencies,
                tags=_tags((*included_tags, *route.tags)),
            )
            self._routes.append(_Route(**values))


class App(Router):
    """An application with native routing and startup-compiled Python plans."""

    def __init__(
        self,
        *,
        title: str = "Bessemer",
        version: str = "0.1.0",
        description: str | None = None,
        lifespan=None,
        dependencies: Iterable[Depends] = (),
        openapi_url: str | None = "/openapi.json",
        docs_url: str | None = None,
        cors=None,
        security_headers: bool = True,
        request_id: bool = True,
        access_log: bool = False,
        trusted_proxies: Iterable[str] | None = None,
        allowed_hosts: Iterable[str] | None = None,
        api_key: dict | None = None,
        jwt=None,
        rate_limit: dict | None = None,
        metrics_path: str | None = None,
        metrics_token_env: str | None = None,
    ) -> None:
        super().__init__(dependencies=dependencies)
        if (
            not isinstance(title, str)
            or not title
            or not isinstance(version, str)
            or not version
        ):
            raise ValueError("application title and version must be nonempty strings")
        if description is not None and not isinstance(description, str):
            raise TypeError("application description must be a string")
        self.title = title
        self.version = version
        self.description = description
        self.lifespan = lifespan
        if lifespan is not None and not callable(lifespan):
            raise TypeError("lifespan must be an async context-manager factory")
        self.openapi_url = None if openapi_url is None else _path(openapi_url)
        self.docs_url = None if docs_url is None else _path(docs_url)
        if any(
            value is not None and _PARAMETER.search(value)
            for value in (self.openapi_url, self.docs_url)
        ):
            raise ValueError(
                "documentation URLs must be exact paths without placeholders"
            )
        if self.docs_url is not None and self.openapi_url is None:
            raise ValueError("docs_url requires an enabled openapi_url")
        self.state = types.SimpleNamespace()
        # Replacement callables for dependencies, keyed by the original, as in
        # FastAPI. They apply the next time the app starts serving (a test
        # client or run), never to a running worker.
        self.dependency_overrides: dict[Callable[..., Any], Callable[..., Any]] = {}
        self._built_overrides: dict[Callable[..., Any], Callable[..., Any]] = {}
        self._compiled: tuple[_CompiledRoute, ...] = ()
        self._openapi_document: dict[str, Any] | None = None
        self._built_settings: dict[str, Any] | None = None
        self._exception_handlers: dict[type[Exception], CallablePlan] = {}
        if isinstance(allowed_hosts, str):
            raise TypeError("allowed_hosts must be an iterable of host names")
        if isinstance(trusted_proxies, str):
            raise TypeError(
                "trusted_proxies must be an iterable of addresses or networks"
            )
        self._settings = {
            "cors": cors,
            "security_headers": security_headers,
            "request_id": request_id,
            "access_log": access_log,
            "trusted_proxies": [] if trusted_proxies is None else list(trusted_proxies),
            "allowed_hosts": None if allowed_hosts is None else list(allowed_hosts),
            "api_key": api_key,
            "jwt": jwt,
            "rate_limit": rate_limit,
            "metrics_path": metrics_path,
            "metrics_token_env": metrics_token_env,
        }

    def health(
        self, path: str = "/healthz", *, include_in_schema: bool = False
    ) -> None:
        if self._built:
            raise RuntimeError("routes cannot change after app.build()")
        if type(include_in_schema) is not bool:
            raise TypeError("include_in_schema must be a boolean")
        self._routes.append(
            _Route(
                "GET",
                _path(path),
                None,
                200,
                None,
                "health",
                include_in_schema=include_in_schema,
            )
        )

    def exception_handler(self, exception_type: type[Exception]):
        if not isinstance(exception_type, type) or not issubclass(
            exception_type, Exception
        ):
            raise TypeError("exception handlers require an Exception subclass")

        def register(handler):
            if self._built:
                raise RuntimeError("exception handlers cannot change after app.build()")
            if not callable(handler):
                raise TypeError("an exception handler must be callable")
            parameters = tuple(inspect.signature(handler).parameters.values())
            if len(parameters) != 2 or any(
                parameter.kind
                not in (parameter.POSITIONAL_ONLY, parameter.POSITIONAL_OR_KEYWORD)
                for parameter in parameters
            ):
                raise ValueError(
                    "exception handlers must accept exactly two positional arguments: request and exception"
                )
            kind = _callable_kind(handler)
            if kind in {"generator", "async_generator"}:
                raise ValueError("exception handlers cannot yield")
            self._exception_handlers[exception_type] = CallablePlan(
                -1, handler, kind, ()
            )
            return handler

        return register

    def _export_settings(self) -> dict[str, Any]:
        from .middleware import validate_settings

        if self._built_settings is not None:
            return copy.deepcopy(self._built_settings)
        return validate_settings(self._settings)

    def build(self) -> App:
        overrides = self.dependency_overrides
        if not isinstance(overrides, dict):
            raise TypeError("dependency_overrides must be a dict")
        if self._built and overrides == self._built_overrides:
            return self
        overrides = dict(overrides)
        for original, replacement in overrides.items():
            if not callable(original) or not callable(replacement):
                raise TypeError("dependency_overrides must map callables to callables")
        from .openapi import build_document, documentation_page, encode_document

        settings = self._export_settings()
        seen: set[tuple[str, str]] = set()
        paths: dict[str, str] = {}
        compiled = []

        def append(route: _Route) -> None:
            structural_path = _PARAMETER.sub("{}", route.path)
            if structural_path in paths and paths[structural_path] != route.path:
                raise ValueError(f"ambiguous path parameter names: {route.path}")
            paths[structural_path] = route.path
            key = (route.method, structural_path)
            if key in seen:
                raise ValueError(
                    f"duplicate or ambiguous route: {route.method} {route.path}"
                )
            seen.add(key)
            compiled.append(_compile(route, len(compiled), overrides))
            if route.kind == "python" and self._exception_handlers:
                compiled[-1].declaration["needs_request"] = True

        for route in self._routes:
            append(route)
        if settings.get("jwt") is None and any(
            route.declaration.get("needs_principal") for route in compiled
        ):
            raise ValueError("Principal parameters require App(jwt=JWTAuth(...))")
        metrics_path = settings.get("metrics_path")
        if metrics_path is not None and ("GET", metrics_path) in seen:
            raise ValueError("metrics_path conflicts with an application route")
        document = build_document(self, self._routes, tuple(compiled), settings)
        if self.openapi_url is not None:
            append(
                _Route(
                    "GET",
                    self.openapi_url,
                    None,
                    200,
                    None,
                    "static",
                    include_in_schema=False,
                    content=encode_document(document),
                )
            )
        if self.docs_url is not None:
            append(
                _Route(
                    "GET",
                    self.docs_url,
                    None,
                    200,
                    None,
                    "static",
                    include_in_schema=False,
                    content=documentation_page(self.title, self.openapi_url),
                    content_type="text/html; charset=utf-8",
                )
            )
        if metrics_path is not None and ("GET", metrics_path) in seen:
            raise ValueError("metrics_path conflicts with a documentation route")
        self._compiled = tuple(compiled)
        self._openapi_document = document
        self._built_settings = settings
        self._built_overrides = overrides
        self._built = True
        return self

    def openapi(self) -> dict[str, Any]:
        self.build()
        return copy.deepcopy(self._openapi_document)

    def _export_routes(self) -> list[dict[str, Any]]:
        self.build()
        return [copy.deepcopy(route.declaration) for route in self._compiled]

    def run(
        self,
        *,
        host: str = "127.0.0.1",
        port: int = 8000,
        max_body_size: int = 1_048_576,
        max_request_bytes_in_flight: int = 33_554_432,
        max_in_flight: int = 128,
        request_timeout: float = 30.0,
        graceful_timeout: float = 30.0,
        startup_timeout: float = 30.0,
        max_response_size: int = 4_194_304,
        max_response_bytes_in_flight: int = 33_554_432,
        max_connections: int = 1024,
        header_timeout: float = 10.0,
        max_headers: int = 64,
        max_header_size: int = 32768,
        max_request_target_size: int = 8192,
        listener_fd: int | None = None,
        ready_fd: int | None = None,
        max_requests: int = 0,
    ) -> None:
        self._run(
            host=host,
            port=port,
            max_body_size=max_body_size,
            max_request_bytes_in_flight=max_request_bytes_in_flight,
            max_in_flight=max_in_flight,
            request_timeout=request_timeout,
            graceful_timeout=graceful_timeout,
            startup_timeout=startup_timeout,
            max_response_size=max_response_size,
            max_response_bytes_in_flight=max_response_bytes_in_flight,
            max_connections=max_connections,
            header_timeout=header_timeout,
            max_headers=max_headers,
            max_header_size=max_header_size,
            max_request_target_size=max_request_target_size,
            listener_fd=listener_fd,
            ready_fd=ready_fd,
            max_requests=max_requests,
        )

    def _run(
        self,
        *,
        host: str = "127.0.0.1",
        port: int = 8000,
        max_body_size: int = 1_048_576,
        max_request_bytes_in_flight: int = 33_554_432,
        max_in_flight: int = 128,
        request_timeout: float = 30.0,
        graceful_timeout: float = 30.0,
        startup_timeout: float = 30.0,
        max_response_size: int = 4_194_304,
        max_response_bytes_in_flight: int = 33_554_432,
        max_connections: int = 1024,
        header_timeout: float = 10.0,
        max_headers: int = 64,
        max_header_size: int = 32768,
        max_request_target_size: int = 8192,
        listener_fd: int | None = None,
        ready_fd: int | None = None,
        max_requests: int = 0,
        shutdown_fd: int | None = None,
        started: Callable[[], None] | None = None,
    ) -> None:
        from . import _native
        from .config import validate_server_limits
        from .worker import Worker

        if not isinstance(host, str) or not host:
            raise ValueError("host must be a nonempty string")
        if type(port) is not int or not 0 <= port <= 65535:
            raise ValueError("port must be between 0 and 65535")
        validate_server_limits(
            {
                "max_body_size": max_body_size,
                "max_request_bytes_in_flight": max_request_bytes_in_flight,
                "max_in_flight": max_in_flight,
                "max_response_size": max_response_size,
                "max_response_bytes_in_flight": max_response_bytes_in_flight,
                "max_connections": max_connections,
                "max_headers": max_headers,
                "max_header_size": max_header_size,
                "max_request_target_size": max_request_target_size,
            }
        )
        for name, value in (
            ("listener_fd", listener_fd),
            ("ready_fd", ready_fd),
            ("shutdown_fd", shutdown_fd),
        ):
            if value is not None and (type(value) is not int or value < 0):
                raise ValueError(f"{name} must be a nonnegative file descriptor")
        if type(max_requests) is not int or max_requests < 0:
            raise ValueError("max_requests must be a nonnegative integer")
        request_timeout = _positive_float(request_timeout, "request_timeout")
        graceful_timeout = _positive_float(graceful_timeout, "graceful_timeout")
        header_timeout = _positive_float(header_timeout, "header_timeout")
        startup_timeout = _positive_float(startup_timeout, "startup_timeout")
        routes = self._export_routes()
        with Worker(
            self,
            graceful_timeout=graceful_timeout,
            startup_timeout=startup_timeout,
            heartbeat_fd=ready_fd,
            max_response_size=max_response_size,
        ) as worker:
            # Requests reach the worker through a native batched queue, so the
            # server's threads never take the GIL on the request path. Paths
            # that hold a Python future, such as WebSocket sessions, use submit.
            bridge = _native.Bridge(worker.submit)
            worker.attach_native(bridge)
            if started is not None:
                started()
            _native.serve(
                routes,
                bridge,
                host,
                port,
                max_body_size=max_body_size,
                max_request_bytes_in_flight=max_request_bytes_in_flight,
                max_in_flight=max_in_flight,
                request_timeout=request_timeout,
                graceful_timeout=graceful_timeout,
                max_response_size=max_response_size,
                max_response_bytes_in_flight=max_response_bytes_in_flight,
                max_connections=max_connections,
                header_timeout=header_timeout,
                max_headers=max_headers,
                max_header_size=max_header_size,
                max_request_target_size=max_request_target_size,
                listener_fd=listener_fd,
                ready_fd=ready_fd,
                max_requests=max_requests,
                controls=self._export_settings(),
                shutdown_fd=shutdown_fd,
            )
