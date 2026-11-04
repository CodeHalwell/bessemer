"""Route registration and compilation. Signature inspection occurs at startup."""

from __future__ import annotations

import inspect
import math
import re
import types
import typing
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

_MISSING = inspect.Parameter.empty
_METHODS = frozenset({"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"})
_SCALARS = {str: "str", int: "int", float: "float", bool: "bool"}
_PARAMETER = re.compile(r"\{([a-zA-Z_][a-zA-Z_0-9]*)\}")


class HTTPException(Exception):
    """Return a JSON error with an explicitly chosen HTTP error status."""

    def __init__(self, status_code: int, detail: Any) -> None:
        if type(status_code) is not int or not 400 <= status_code <= 599:
            raise ValueError("HTTPException status_code must be between 400 and 599")
        super().__init__(str(detail))
        self.status_code = status_code
        self.detail = detail


@dataclass(frozen=True)
class _Route:
    method: str
    path: str
    handler: Callable[..., Any] | None
    status_code: int
    timeout: float | None
    kind: str = "python"


@dataclass(frozen=True)
class _CompiledRoute:
    declaration: dict[str, Any]
    handler: Callable[..., Any] | None
    body_models: dict[str, type]
    body_containers: dict[str, type]
    nullable_body: frozenset[str]


def _path(path: str, *, prefix: bool = False) -> str:
    if prefix and path == "":
        return path
    if not isinstance(path, str) or not path.startswith("/"):
        raise ValueError("route paths and nonempty prefixes must begin with /")
    if "?" in path or "#" in path or "//" in path:
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
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f"{name} must be a positive finite number")
    return value


def _optional(annotation: Any) -> tuple[Any, bool]:
    if typing.get_origin(annotation) in (typing.Union, types.UnionType):
        arguments = typing.get_args(annotation)
        non_none = [argument for argument in arguments if argument is not type(None)]
        if len(non_none) == 1 and len(arguments) == 2:
            return non_none[0], True
    return annotation, False


def _compile(route: _Route, route_id: int) -> _CompiledRoute:
    declaration: dict[str, Any] = {
        "id": route_id,
        "method": route.method,
        "path": route.path,
        "status_code": route.status_code,
        "timeout": route.timeout,
        "kind": route.kind,
        "parameters": [],
        "tier": 0 if route.kind == "health" else 2,
        "reason": "native health check"
        if route.kind == "health"
        else "initial Python compatibility bridge",
    }
    if route.handler is None:
        return _CompiledRoute(declaration, None, {}, {}, frozenset())
    try:
        hints = typing.get_type_hints(route.handler, include_extras=True)
    except (NameError, TypeError) as error:
        raise ValueError(
            f"cannot resolve type annotations for {route.path}: {error}"
        ) from error
    path_names = set(_PARAMETER.findall(route.path))
    found_path_names: set[str] = set()
    body_models: dict[str, type] = {}
    body_containers: dict[str, type] = {}
    nullable_body: set[str] = set()
    body_count = 0
    for parameter in inspect.signature(route.handler).parameters.values():
        if parameter.kind not in (
            parameter.POSITIONAL_OR_KEYWORD,
            parameter.KEYWORD_ONLY,
        ):
            raise ValueError(
                f"{route.path}: positional-only and variadic parameters are unsupported"
            )
        annotation, nullable = _optional(hints.get(parameter.name, _MISSING))
        try:
            native_type = _SCALARS.get(annotation)
        except TypeError:
            native_type = None
        is_model = isinstance(annotation, type) and callable(
            getattr(annotation, "model_validate", None)
        )
        is_json = annotation in (dict, list) or typing.get_origin(annotation) in (
            dict,
            list,
        )
        if is_json and typing.get_args(annotation):
            allowed = (str, Any) if typing.get_origin(annotation) is dict else (Any,)
            if typing.get_args(annotation) != allowed:
                raise ValueError(
                    f"{route.path}: typed JSON container contents require a Pydantic model; use dict/list for raw JSON"
                )
        if native_type is None and not is_model and not is_json:
            raise ValueError(
                f"{route.path}: parameter {parameter.name!r} needs a supported scalar, dict, list or Pydantic model annotation"
            )
        if parameter.name in path_names:
            if native_type is None or nullable or parameter.default is not _MISSING:
                raise ValueError(
                    f"{route.path}: path parameters must be required non-null scalars"
                )
            source = "path"
            found_path_names.add(parameter.name)
        elif native_type is not None:
            source = "query"
        else:
            source = "body"
            native_type = "json"
            body_count += 1
            if body_count > 1:
                raise ValueError(
                    f"{route.path}: only one JSON body parameter is supported"
                )
            if is_model:
                body_models[parameter.name] = annotation
            else:
                body_containers[parameter.name] = (
                    typing.get_origin(annotation) or annotation
                )
            if nullable:
                nullable_body.add(parameter.name)
        required = parameter.default is _MISSING
        default = None if required else parameter.default
        if not required:
            if default is None:
                if not nullable:
                    raise ValueError(
                        f"{route.path}: a None default requires an optional annotation"
                    )
            elif (
                native_type in {"str", "int", "bool"}
                and type(default) is not annotation
            ):
                raise ValueError(
                    f"{route.path}: default for {parameter.name!r} does not match its annotation"
                )
            elif native_type == "float" and (
                isinstance(default, bool)
                or not isinstance(default, (int, float))
                or not math.isfinite(default)
            ):
                raise ValueError(f"{route.path}: float defaults must be finite numbers")
            elif source == "body":
                raise ValueError(f"{route.path}: JSON body defaults must be None")
        declaration["parameters"].append(
            {
                "name": parameter.name,
                "source": source,
                "type": native_type,
                "required": required,
                "default": default,
            }
        )
    if path_names != found_path_names:
        missing = ", ".join(sorted(path_names - found_path_names))
        raise ValueError(
            f"{route.path}: path parameters missing from handler: {missing}"
        )
    return _CompiledRoute(
        declaration,
        route.handler,
        body_models,
        body_containers,
        frozenset(nullable_body),
    )


class Router:
    """A group of routes compiled once before the server accepts requests."""

    def __init__(self, *, prefix: str = "") -> None:
        self.prefix = _path(prefix, prefix=True)
        self._routes: list[_Route] = []
        self._built = False

    def route(
        self,
        path: str,
        *,
        methods: typing.Iterable[str],
        status_code: int = 200,
        timeout: float | None = None,
    ):
        full_path = _path(self.prefix + _path(path))
        if type(status_code) is not int or not 200 <= status_code <= 599:
            raise ValueError(
                "status_code must be a final HTTP status between 200 and 599"
            )
        timeout = None if timeout is None else _positive_float(timeout, "timeout")
        selected = tuple(method.upper() for method in methods)
        if (
            not selected
            or len(selected) != len(set(selected))
            or any(method not in _METHODS for method in selected)
        ):
            raise ValueError("methods must be unique supported HTTP methods")

        def register(handler):
            if self._built:
                raise RuntimeError("routes cannot change after app.build()")
            if not callable(handler):
                raise TypeError("a route handler must be callable")
            for method in selected:
                self._routes.append(
                    _Route(method, full_path, handler, status_code, timeout)
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

    def include_router(self, router: Router, *, prefix: str = "") -> None:
        if self._built:
            raise RuntimeError("routes cannot change after app.build()")
        if not isinstance(router, Router) or router is self:
            raise TypeError("include_router expects another Router")
        prefix = self.prefix + _path(prefix, prefix=True)
        for route in router._routes:
            self._routes.append(
                _Route(
                    route.method,
                    _path(prefix + route.path),
                    route.handler,
                    route.status_code,
                    route.timeout,
                    route.kind,
                )
            )


class App(Router):
    """A single-worker application with native routing and an asyncio bridge."""

    def __init__(
        self, *, title: str = "Bessemer", version: str = "0.1.0", lifespan=None
    ) -> None:
        super().__init__()
        self.title = title
        self.version = version
        self.lifespan = lifespan
        self.state = types.SimpleNamespace()
        self._compiled: tuple[_CompiledRoute, ...] = ()

    def health(self, path: str = "/healthz") -> None:
        if self._built:
            raise RuntimeError("routes cannot change after app.build()")
        self._routes.append(_Route("GET", _path(path), None, 200, None, "health"))

    def build(self) -> App:
        if self._built:
            return self
        seen: set[tuple[str, str]] = set()
        paths: dict[str, str] = {}
        compiled = []
        for route_id, route in enumerate(self._routes):
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
            compiled.append(_compile(route, route_id))
        self._compiled = tuple(compiled)
        self._built = True
        return self

    def _export_routes(self) -> list[dict[str, Any]]:
        self.build()
        return [
            dict(
                route.declaration,
                parameters=[
                    dict(parameter) for parameter in route.declaration["parameters"]
                ],
            )
            for route in self._compiled
        ]

    def run(
        self,
        *,
        host: str = "127.0.0.1",
        port: int = 8000,
        max_body_size: int = 1_048_576,
        max_in_flight: int = 128,
        request_timeout: float = 30.0,
        graceful_timeout: float = 30.0,
    ) -> None:
        from . import _native
        from .worker import Worker

        if not isinstance(host, str) or not host:
            raise ValueError("host must be a nonempty string")
        if type(port) is not int or not 0 <= port <= 65535:
            raise ValueError("port must be between 0 and 65535")
        for name, value in (
            ("max_body_size", max_body_size),
            ("max_in_flight", max_in_flight),
        ):
            if type(value) is not int or value < 1:
                raise ValueError(f"{name} must be a positive integer")
        request_timeout = _positive_float(request_timeout, "request_timeout")
        graceful_timeout = _positive_float(graceful_timeout, "graceful_timeout")
        routes = self._export_routes()
        with Worker(self, graceful_timeout=graceful_timeout) as worker:
            _native.serve(
                routes,
                worker.submit,
                host,
                port,
                max_body_size=max_body_size,
                max_in_flight=max_in_flight,
                request_timeout=request_timeout,
                graceful_timeout=graceful_timeout,
            )
