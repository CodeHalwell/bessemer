"""Route registration and compilation. Signature inspection occurs at startup."""

from __future__ import annotations

import copy
import dataclasses
import inspect
import math
import os
import re
import sys
import types
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote

from .params import Depends
from .responses import HTTPException, JSONResponse, ORJSONResponse, UJSONResponse
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
# A placeholder, ``{name}`` or Starlette's ``{name:converter}``. The first group
# is the name; ``_CONVERTER`` reads the converter.
_PARAMETER = re.compile(r"\{([a-zA-Z_][a-zA-Z_0-9]*)(?::[^{}/]*)?\}")
_CONVERTER = re.compile(r"\{([a-zA-Z_][a-zA-Z_0-9]*)(?::([^{}/]*))?\}")
# Starlette's converters. ``str`` and ``path`` accept any value; Rust checks
# the others after the route lookup, and a value that does not match is 404.
_CONVERTERS = ("str", "path", "int", "float", "uuid")
# The methods a mounted ASGI app is served for (TRACE is not routed).
_ASGI_MOUNT_METHODS = ("GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS")
_SEGMENT_SAFE = "-._~!$&'()*+,;=:@"
# Response classes the native JSON encoder stands in for exactly.
_NATIVE_JSON = frozenset({JSONResponse, ORJSONResponse, UJSONResponse})
__all__ = ["App", "HTTPException", "NoMatchFound", "Router"]


class NoMatchFound(Exception):
    """Raised by ``url_path_for`` when no route has the name and parameters."""

    def __init__(self, name: str, path_params: Mapping[str, Any]) -> None:
        params = ", ".join(sorted(path_params))
        super().__init__(f'No route exists for name "{name}" and params "{params}".')


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
    files: dict[str, Any] | None = None
    name: str | None = None
    response_model_options: dict[str, Any] | None = None
    # A mounted application's route: the App that owns it (its exception
    # handlers, dependency_overrides and state apply) and the middleware of
    # every mounted App around it, outermost first.
    app: Any = None
    middleware: tuple[Callable[..., Any], ...] = ()
    # The route as its own app declared it, for that app's OpenAPI document.
    source: Any = None
    # FastAPI's per-route options. A ``response_class`` wraps return values
    # that are not already a Response; ``None`` keeps native JSON encoding.
    response_class: Any = None
    status_explicit: bool = False
    responses: Mapping[Any, Any] | None = None
    deprecated: bool = False
    response_description: str = "Successful Response"
    openapi_extra: dict[str, Any] | None = None
    generate_unique_id_function: Callable[..., str] | None = None


_RESPONSE_MODEL_DEFAULTS = {
    "include": None,
    "exclude": None,
    "by_alias": True,
    "exclude_unset": False,
    "exclude_defaults": False,
    "exclude_none": False,
}


def _response_model_options(
    include, exclude, by_alias, exclude_unset, exclude_defaults, exclude_none
) -> dict[str, Any] | None:
    """Validate FastAPI's ``response_model_*`` options; ``None`` when unused."""
    for option, value in (
        ("response_model_include", include),
        ("response_model_exclude", exclude),
    ):
        if value is not None and not isinstance(value, (set, frozenset, dict)):
            raise TypeError(f"{option} must be a set or dict of field names")
    for option, value in (
        ("response_model_by_alias", by_alias),
        ("response_model_exclude_unset", exclude_unset),
        ("response_model_exclude_defaults", exclude_defaults),
        ("response_model_exclude_none", exclude_none),
    ):
        if type(value) is not bool:
            raise TypeError(f"{option} must be a boolean")
    options = {
        "include": include,
        "exclude": exclude,
        "by_alias": by_alias,
        "exclude_unset": exclude_unset,
        "exclude_defaults": exclude_defaults,
        "exclude_none": exclude_none,
    }
    return None if options == _RESPONSE_MODEL_DEFAULTS else options


def _route_name(name: str | None, handler: Callable[..., Any]) -> str | None:
    if name is None:
        return getattr(handler, "__name__", None)
    if not isinstance(name, str) or not name:
        raise ValueError("route names must be nonempty strings")
    return name


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
    response_model_options: dict[str, Any] | None = None
    security: tuple[Any, ...] = ()
    app: Any = None
    middleware: tuple[Callable[..., Any], ...] = ()
    embedded_body: tuple | None = None


def _ancestry(app: Any) -> list[Any]:
    """Every App mounted, directly or not, inside ``app``."""
    found = []
    for _, mounted, _ in getattr(app, "_mounts", ()):
        found.append(mounted)
        found.extend(_ancestry(mounted))
    return found


def _mounted_settings(settings: dict[str, Any], prefix: str) -> dict[str, Any]:
    """Settings as a mounted app's document sees them, relative to ``prefix``."""
    settings = copy.deepcopy(settings)
    for key in ("api_key", "jwt"):
        value = settings.get(key)
        if isinstance(value, dict) and value.get("exclude_paths"):
            value["exclude_paths"] = [
                path[len(prefix) :]
                for path in value["exclude_paths"]
                if path.startswith(prefix + "/")
            ]
    return settings


def _overrides(app: Any) -> dict[Callable[..., Any], Callable[..., Any]]:
    overrides = app.dependency_overrides
    if not isinstance(overrides, dict) or any(
        not callable(original) or not callable(replacement)
        for original, replacement in overrides.items()
    ):
        raise TypeError("dependency_overrides must map callables to callables")
    return dict(overrides)


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
    segments = path.split("/")
    for index, segment in enumerate(segments):
        if "{" in segment and _PARAMETER.fullmatch(segment) is None:
            raise ValueError("a path parameter must occupy an entire segment")
        match = _CONVERTER.fullmatch(segment)
        converter = match and match.group(2)
        if converter is None:
            continue
        if converter not in _CONVERTERS:
            raise ValueError(
                f"unknown path converter {converter!r} in {path!r};"
                f" supported converters are {', '.join(_CONVERTERS)}"
            )
        if converter == "path" and (prefix or index != len(segments) - 1):
            raise ValueError(
                f"a {{name:path}} parameter must be the last segment of a route"
                f" path, not inside a prefix or before other segments: {path!r}"
            )
    names = _PARAMETER.findall(path)
    if len(names) != len(set(names)):
        raise ValueError("path parameter names must be unique within a route")
    return path


def _path_format(path: str) -> str:
    """``path`` without converters, as FastAPI's ``path_format`` and OpenAPI show it."""
    return _PARAMETER.sub(r"{\1}", path)


def _converters(path: str) -> list[list[str]]:
    """The ``[name, converter]`` pairs Rust must check or route specially."""
    return [
        [name, converter]
        for name, converter in _CONVERTER.findall(path)
        if converter not in ("", "str")
    ]


def _matched_path(path: str) -> str:
    """``path`` in ``matchit`` syntax: ``{name:path}`` is the catch-all ``{*name}``."""
    return _CONVERTER.sub(
        lambda match: (
            f"{{*{match.group(1)}}}"
            if match.group(2) == "path"
            else f"{{{match.group(1)}}}"
        ),
        path,
    )


def _importable_target(app: Any) -> tuple[str, str | None]:
    """Name ``module:variable`` for ``app`` so a fresh interpreter can import it.

    Also returns a directory that interpreter needs on its import path: the
    directory of a script run as ``python script.py``, whose module is
    ``__main__`` here but is imported by file name in the worker.
    """
    main = sys.modules.get("__main__")
    main_file = getattr(main, "__file__", None)
    main_spec = getattr(main, "__spec__", None)
    script_directory = (
        str(Path(main_file).resolve().parent)
        if main_file is not None and main_spec is None
        else None
    )
    # Prefer an importable module that holds the app over __main__: a launcher
    # may bind it only inside its if __name__ == "__main__": block, which a
    # worker importing the launcher would skip.
    modules = [
        (name, module)
        for name, module in list(sys.modules.items())
        if name != "__main__" and module is not None
    ]
    if main is not None:
        modules.append(("__main__", main))
    for name, module in modules:
        namespace = getattr(module, "__dict__", None)
        if not isinstance(namespace, dict):
            continue
        variable = next(
            (
                key
                for key, value in list(namespace.items())
                if value is app and key.isidentifier()
            ),
            None,
        )
        if variable is None:
            continue
        if name != "__main__":
            return f"{name}:{variable}", script_directory
        if main_spec is not None and main_spec.name != "__main__":
            # Started with python -m package.module.
            return f"{main_spec.name}:{variable}", None
        if main_file is not None:
            stem = Path(main_file).stem
            if stem.isidentifier() and stem != "__main__":
                # A package of the same name beside the script would be
                # imported instead of it, so refuse rather than serve the
                # wrong application.
                import importlib.machinery

                found = importlib.machinery.PathFinder.find_spec(
                    stem, [script_directory]
                )
                origin = getattr(found, "origin", None)
                if (
                    origin is None
                    or Path(origin).resolve() != Path(main_file).resolve()
                ):
                    raise RuntimeError(
                        f"workers would import {stem!r} as something other than "
                        f"{main_file}; rename the script or use "
                        "bessemer run module:app"
                    )
                return f"{stem}:{variable}", script_directory
    raise RuntimeError(
        "app.run(workers=...) needs the application in a module-level variable "
        "of a file Python can import, because each worker imports it afresh; "
        "otherwise use workers=1 or bessemer run module:app"
    )


def _positive_float(value: float, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{name} must be a positive finite number")
    value = float(value)
    if not math.isfinite(value) or value < 1e-9 or value > 86_400:
        raise ValueError(f"{name} must be between one nanosecond and 86400 seconds")
    return value


def _tls_files(certfile, keyfile) -> tuple[str | None, str | None]:
    """Validate the TLS file pair before any application resources start."""
    if (certfile is None) != (keyfile is None):
        raise ValueError("tls_cert and tls_key must be given together")
    if certfile is None:
        return None, None
    paths = []
    for name, value in (("tls_cert", certfile), ("tls_key", keyfile)):
        if not isinstance(value, (str, os.PathLike)) or not os.fspath(value):
            raise TypeError(f"{name} must be a path")
        path = os.fspath(value)
        if not os.path.isfile(path):
            raise ValueError(f"{name} does not exist: {path}")
        paths.append(path)
    return paths[0], paths[1]


def _tls_client_file(name, path, needs, needed) -> str | None:
    """Validate a TLS file path that only applies alongside another setting."""
    if path is None:
        return None
    if needs is None:
        raise ValueError(f"{name} needs {needed}")
    if not isinstance(path, (str, os.PathLike)) or not os.fspath(path):
        raise TypeError(f"{name} must be a path")
    path = os.fspath(path)
    if not os.path.isfile(path):
        raise ValueError(f"{name} does not exist: {path}")
    return path


def _response_class(value: Any) -> Any:
    """A validated ``response_class``; ``None`` when it was not given.

    JSON classes are kept, so they can override a non-JSON default. The
    built-in ones are never wrapped (see ``_wraps_response``): the native
    encoder already sends exactly what they would.
    """
    from .responses import Response

    if value is None:
        return None
    if not isinstance(value, type) or not issubclass(value, Response):
        raise TypeError("response_class must be a Response subclass")
    return value


def _wraps_response(response_class: Any) -> bool:

    # Only the built-in JSON classes ride the native encoder. Any subclass is
    # built, so its media type, default status and headers reach the client.
    return response_class is not None and response_class not in _NATIVE_JSON


def _responses(value: Mapping[Any, Any] | None) -> dict[Any, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping) or any(
        not isinstance(response, dict) for response in value.values()
    ):
        raise TypeError("responses must map status codes to OpenAPI response dicts")
    return dict(value)


def _optional_bool(value: Any, name: str) -> bool | None:
    if value is not None and type(value) is not bool:
        raise TypeError(f"{name} must be a boolean")
    return value


def _unique_id_function(value: Any) -> Callable[..., str] | None:
    if value is not None and not callable(value):
        raise TypeError("generate_unique_id_function must be callable")
    return value


def _hooks(values: Iterable[Callable[..., Any]] | None, name: str) -> list:
    hooks = list(values or ())
    if any(not callable(hook) for hook in hooks):
        raise TypeError(f"{name} handlers must be callable")
    return hooks


def _hooks_lifespan(startup: list, shutdown: list):
    """A lifespan running FastAPI-style startup and shutdown handlers."""
    from contextlib import asynccontextmanager

    async def run(hook) -> None:
        result = hook()
        if inspect.isawaitable(result):
            await result

    @asynccontextmanager
    async def lifespan(_app):
        for hook in startup:
            await run(hook)
        try:
            yield
        finally:
            # Shutdown hooks run even when teardown elsewhere failed.
            for hook in shutdown:
                await run(hook)

    return lifespan


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
        "path": _matched_path(route.path),
        "converters": _converters(route.path),
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
        else "native static files"
        if route.kind == "files"
        else "pre-built native response",
        "needs_request": False,
        "needs_principal": False,
    }
    if route.kind == "static":
        declaration.update(content=route.content, content_type=route.content_type)
    if route.kind == "files":
        declaration.update(route.files or {})
    if route.handler is None:
        return _CompiledRoute(
            declaration,
            None,
            {},
            {},
            frozenset(),
            input_validators={},
            app=route.app,
            middleware=route.middleware,
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
    options = route.response_model_options
    if (
        options is not None
        and adapter is not None
        and not hasattr(adapter, "serializer")
    ):
        raise ValueError(
            f"response_model_* options on {route.path} require the optional"
            " Pydantic v2 dependency"
        )
    if options is not None and adapter is None:
        raise ValueError(
            f"response_model_* options on {route.path} need a response model:"
            " declare response_model= or a return annotation"
        )
    plan = inputs.plan
    if _wraps_response(route.response_class) and not websocket:
        plan = _wrap_response_class(plan, route, adapter, options)
    return _CompiledRoute(
        declaration,
        route.handler,
        inputs.body_models,
        inputs.body_containers,
        inputs.nullable_body,
        plan,
        inputs.dependencies,
        inputs.before,
        model,
        adapter,
        inputs.input_validators,
        options,
        inputs.security,
        route.app,
        route.middleware,
        inputs.embedded_body,
    )


def _wrap_response_class(
    plan: CallablePlan,
    route: _Route,
    adapter: Any = None,
    model_options: dict[str, Any] | None = None,
) -> CallablePlan:
    """Wrap a handler so values it returns become ``route.response_class``.

    Only routes that declare a non-JSON response class take the wrapper;
    every other route keeps the native JSON path untouched. A response
    model validates and shapes the value first, as it does for JSON routes.
    """
    from .responses import Response

    response_class = route.response_class
    options = {"status_code": route.status_code} if route.status_explicit else {}
    dump_options = {"by_alias": True} if model_options is None else dict(model_options)

    def build(result):
        if isinstance(result, Response):
            return result
        if adapter is not None:
            result = adapter.dump_python(
                adapter.validate_python(result),
                mode="json",
                warnings="error",
                **dump_options,
            )
        return response_class(result, **options)

    call = plan.call
    if plan.kind == "async":

        async def respond(**arguments):
            return build(await call(**arguments))

    elif plan.kind == "sync":

        async def resolve(awaitable):
            return build(await awaitable)

        def respond(**arguments):
            result = call(**arguments)
            # A sync handler may hand back an awaitable (a sync decorator over
            # an async function). The worker awaits whatever comes back, so
            # defer the conversion until the awaitable has resolved.
            if inspect.isawaitable(result):
                return resolve(result)
            return build(result)

    else:
        raise ValueError(f"{route.path}: a response_class needs a plain handler")
    return dataclasses.replace(plan, call=respond)


class Router:
    """A group of routes compiled once before the server accepts requests."""

    def __init__(
        self,
        *,
        prefix: str = "",
        tags: Iterable[str] = (),
        dependencies: Iterable[Depends] = (),
        default_response_class: Any = None,
        responses: Mapping[Any, Any] | None = None,
        deprecated: bool | None = None,
        include_in_schema: bool = True,
        generate_unique_id_function: Callable[..., str] | None = None,
        on_startup: Iterable[Callable[..., Any]] | None = None,
        on_shutdown: Iterable[Callable[..., Any]] | None = None,
    ) -> None:
        self.prefix = _path(prefix, prefix=True)
        self.tags = _tags(tags)
        self.dependencies = _dependencies(dependencies)
        self.default_response_class = _response_class(default_response_class)
        self.responses = _responses(responses)
        self.deprecated = _optional_bool(deprecated, "deprecated")
        if type(include_in_schema) is not bool:
            raise TypeError("include_in_schema must be a boolean")
        self.include_in_schema = include_in_schema
        self.generate_unique_id_function = _unique_id_function(
            generate_unique_id_function
        )
        self.on_startup: list[Callable[..., Any]] = _hooks(on_startup, "on_startup")
        self.on_shutdown: list[Callable[..., Any]] = _hooks(on_shutdown, "on_shutdown")
        self._routes: list[_Route] = []
        self._built = False
        # Set when an app this one is mounted in builds: its routes are then
        # part of that app's routing table and can no longer change.
        self._frozen = False

    def route(
        self,
        path: str,
        *,
        methods: Iterable[str],
        status_code: int | None = None,
        timeout: float | None = None,
        dependencies: Iterable[Depends] = (),
        response_model: Any = _MISSING,
        tags: Iterable[str] = (),
        operation_id: str | None = None,
        summary: str | None = None,
        description: str | None = None,
        include_in_schema: bool = True,
        name: str | None = None,
        response_model_include: set | dict | None = None,
        response_model_exclude: set | dict | None = None,
        response_model_by_alias: bool = True,
        response_model_exclude_unset: bool = False,
        response_model_exclude_defaults: bool = False,
        response_model_exclude_none: bool = False,
        response_class: Any = None,
        responses: Mapping[Any, Any] | None = None,
        deprecated: bool | None = None,
        response_description: str = "Successful Response",
        openapi_extra: dict[str, Any] | None = None,
        generate_unique_id_function: Callable[..., str] | None = None,
    ):
        """Register a handler for ``methods`` at ``path``.

        The options match FastAPI's ``api_route``, plus ``timeout``.
        """
        full_path = _path(self.prefix + _path(path))
        if name is not None and (not isinstance(name, str) or not name):
            raise ValueError("route names must be nonempty strings")
        status_explicit = status_code is not None
        if status_code is None:
            status_code = 200
        response_class = _response_class(response_class)
        if response_class is None:
            response_class = self.default_response_class
        route_responses = {**self.responses, **_responses(responses)}
        route_deprecated = bool(_optional_bool(deprecated, "deprecated")) or bool(
            self.deprecated
        )
        if not isinstance(response_description, str) or not response_description:
            raise ValueError("response_description must be a nonempty string")
        if openapi_extra is not None and not isinstance(openapi_extra, dict):
            raise TypeError("openapi_extra must be a dict")
        unique_id = (
            _unique_id_function(generate_unique_id_function)
            or self.generate_unique_id_function
        )
        model_options = _response_model_options(
            response_model_include,
            response_model_exclude,
            response_model_by_alias,
            response_model_exclude_unset,
            response_model_exclude_defaults,
            response_model_exclude_none,
        )
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
        for option, value in (
            ("operation_id", operation_id),
            ("summary", summary),
            ("description", description),
        ):
            if value is not None and (not isinstance(value, str) or not value):
                raise ValueError(f"{option} must be a nonempty string when supplied")
        if type(include_in_schema) is not bool:
            raise TypeError("include_in_schema must be a boolean")
        include_in_schema = include_in_schema and self.include_in_schema

        def register(handler):
            if self._built or self._frozen:
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
                        name=_route_name(name, handler),
                        response_model_options=model_options,
                        response_class=response_class,
                        status_explicit=status_explicit,
                        responses=route_responses or None,
                        deprecated=route_deprecated,
                        response_description=response_description,
                        openapi_extra=openapi_extra,
                        generate_unique_id_function=unique_id,
                    )
                )
            return handler

        return register

    def api_route(self, path: str, *, methods: Iterable[str] | None = None, **options):
        """FastAPI's ``api_route``: ``route`` with ``GET`` as the default method."""
        return self.route(
            path, methods=("GET",) if methods is None else methods, **options
        )

    def add_api_route(
        self,
        path: str,
        endpoint: Callable[..., Any],
        *,
        methods: Iterable[str] | None = None,
        **options,
    ) -> None:
        """Register ``endpoint`` without a decorator, as FastAPI does."""
        self.api_route(path, methods=methods, **options)(endpoint)

    def add_api_websocket_route(
        self,
        path: str,
        endpoint: Callable[..., Any],
        name: str | None = None,
        *,
        dependencies: Iterable[Depends] | None = None,
    ) -> None:
        """Register a WebSocket ``endpoint`` without a decorator."""
        self.websocket(path, name=name, dependencies=dependencies or ())(endpoint)

    def on_event(self, event_type: str):
        """Register a ``"startup"`` or ``"shutdown"`` hook, as FastAPI's
        (deprecated) ``on_event`` does. Prefer ``lifespan``.
        """
        if event_type not in ("startup", "shutdown"):
            raise ValueError('event_type must be "startup" or "shutdown"')

        def register(function):
            if self._built or self._frozen:
                raise RuntimeError("event handlers cannot change after app.build()")
            hooks = self.on_startup if event_type == "startup" else self.on_shutdown
            hooks.extend(_hooks((function,), f"on_event({event_type!r})"))
            return function

        return register

    def add_event_handler(self, event_type: str, function: Callable[..., Any]):
        self.on_event(event_type)(function)

    @property
    def routes(self) -> list[Any]:
        """Read-only descriptions of the registered routes, like FastAPI's
        ``app.routes``. Change a route through its declaration instead:
        ``operation_id=`` or ``generate_unique_id_function=``.
        """
        from .routing import APIRoute, APIWebSocketRoute

        described = []
        for route in self._routes:
            if route.kind == "websocket":
                described.append(APIWebSocketRoute._of(route))
            elif route.kind == "python":
                described.append(APIRoute._of(route))
        return described

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

    def websocket(
        self,
        path: str,
        *,
        dependencies: Iterable[Depends] = (),
        name: str | None = None,
    ):
        """Register an ``async def handler(websocket: WebSocket)`` route."""
        full_path = _path(self.prefix + _path(path))
        if name is not None and (not isinstance(name, str) or not name):
            raise ValueError("route names must be nonempty strings")
        route_dependencies = self.dependencies + _dependencies(dependencies)

        def register(handler):
            if self._built or self._frozen:
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
                    name=_route_name(name, handler),
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
        default_response_class: Any = None,
        responses: Mapping[Any, Any] | None = None,
        deprecated: bool | None = None,
        include_in_schema: bool = True,
        generate_unique_id_function: Callable[..., str] | None = None,
    ) -> None:
        if self._built or self._frozen:
            raise RuntimeError("routes cannot change after app.build()")
        if not isinstance(router, Router) or router is self:
            raise TypeError("include_router expects another Router")
        prefix = self.prefix + _path(prefix, prefix=True)
        included_dependencies = self.dependencies + _dependencies(dependencies)
        included_tags = _tags((*self.tags, *_tags(tags)))
        # As in FastAPI, a route's own option wins, then its router's, then
        # this inclusion's, then this router's defaults.
        response_class = (
            _response_class(default_response_class) or self.default_response_class
        )
        included_responses = {**self.responses, **_responses(responses)}
        included_deprecated = bool(_optional_bool(deprecated, "deprecated")) or bool(
            self.deprecated
        )
        if type(include_in_schema) is not bool:
            raise TypeError("include_in_schema must be a boolean")
        included_schema = include_in_schema and self.include_in_schema
        unique_id = (
            _unique_id_function(generate_unique_id_function)
            or self.generate_unique_id_function
        )
        for route in router._routes:
            values = dict(route.__dict__)
            values.update(
                path=_path(prefix + route.path),
                dependencies=included_dependencies + route.dependencies,
                tags=_tags((*included_tags, *route.tags)),
            )
            if route.kind in ("python", "websocket"):
                values.update(
                    response_class=route.response_class or response_class,
                    responses={**included_responses, **(route.responses or {})} or None,
                    deprecated=route.deprecated or included_deprecated,
                    include_in_schema=route.include_in_schema and included_schema,
                    generate_unique_id_function=route.generate_unique_id_function
                    or unique_id,
                )
            self._routes.append(_Route(**values))
        self.on_startup.extend(router.on_startup)
        self.on_shutdown.extend(router.on_shutdown)

    def url_path_for(self, name: str, /, **path_params: Any) -> str:
        """Return the path of the route called ``name``, as in FastAPI.

        A route's name defaults to its handler's ``__name__``; a static mount
        is named by ``static(..., name=...)`` and takes the file as ``path``.
        Values are converted with ``str`` and percent-encoded. Raises
        ``NoMatchFound`` when no route has this name and exactly these
        parameters.
        """
        return self._url_path_and_kind(name, path_params)[0]

    def _url_path_and_kind(
        self, name: str, path_params: Mapping[str, Any]
    ) -> tuple[str, str]:
        for route in self._routes:
            if route.name != name:
                continue
            path = _format_path(route, path_params)
            if path is not None:
                return path, route.kind
        mount_name, separator, inner = name.partition(":")
        if separator:
            for prefix, app, mounted_name in getattr(self, "_mounts", ()):
                if mounted_name != mount_name:
                    continue
                try:
                    path, kind = app._url_path_and_kind(inner, path_params)
                except NoMatchFound:
                    continue
                return prefix + path, kind
        raise NoMatchFound(name, path_params)


def _format_path(route: _Route, path_params: Mapping[str, Any]) -> str | None:
    if route.kind == "files" or route.path.endswith("{*path}"):
        # Static files, or a mounted ASGI app named as in Starlette's Mount.
        if not route.path.endswith("{*path}") or set(path_params) != {"path"}:
            return None
        file_path = str(path_params["path"]).lstrip("/")
        return route.path[: -len("{*path}")] + quote(
            file_path, safe=_SEGMENT_SAFE + "/"
        )
    if set(_PARAMETER.findall(route.path)) != set(path_params):
        return None
    return _CONVERTER.sub(
        lambda match: quote(
            str(path_params[match.group(1)]),
            # A {name:path} value keeps its slashes, as in Starlette.
            safe=_SEGMENT_SAFE + "/" if match.group(2) == "path" else _SEGMENT_SAFE,
        ),
        route.path,
    )


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
        compression=None,
        otel=None,
        summary: str | None = None,
        terms_of_service: str | None = None,
        contact: dict[str, Any] | None = None,
        license_info: dict[str, Any] | None = None,
        openapi_tags: list[dict[str, Any]] | None = None,
        servers: list[dict[str, Any]] | None = None,
        openapi_external_docs: dict[str, Any] | None = None,
        redoc_url: None = None,
        debug: bool = False,
        exception_handlers: Mapping[Any, Callable[..., Any]] | None = None,
        middleware: Iterable[Any] | None = None,
        on_startup: Iterable[Callable[..., Any]] | None = None,
        on_shutdown: Iterable[Callable[..., Any]] | None = None,
        default_response_class: Any = None,
        responses: Mapping[Any, Any] | None = None,
        deprecated: bool | None = None,
        include_in_schema: bool = True,
        generate_unique_id_function: Callable[..., str] | None = None,
    ) -> None:
        super().__init__(
            dependencies=dependencies,
            default_response_class=default_response_class,
            responses=responses,
            deprecated=deprecated,
            include_in_schema=include_in_schema,
            generate_unique_id_function=generate_unique_id_function,
            on_startup=on_startup,
            on_shutdown=on_shutdown,
        )
        # Explicitly unsupported FastAPI options fail rather than being ignored.
        if redoc_url is not None:
            raise ValueError(
                "Bessemer has no ReDoc page; serve its bundled reference with docs_url"
            )
        if debug is not False:
            raise ValueError(
                "debug tracebacks are not served; errors are logged by the worker"
            )
        for option, value in (
            ("summary", summary),
            ("terms_of_service", terms_of_service),
        ):
            if value is not None and not isinstance(value, str):
                raise TypeError(f"{option} must be a string")
        for option, value in (
            ("contact", contact),
            ("license_info", license_info),
            ("openapi_external_docs", openapi_external_docs),
        ):
            if value is not None and not isinstance(value, dict):
                raise TypeError(f"{option} must be a dict")
        for option, value in (("openapi_tags", openapi_tags), ("servers", servers)):
            if value is not None and (
                not isinstance(value, list)
                or any(not isinstance(item, dict) for item in value)
            ):
                raise TypeError(f"{option} must be a list of dicts")
        self.summary = summary
        self.terms_of_service = terms_of_service
        self.contact = contact
        self.license_info = license_info
        self.openapi_tags = openapi_tags
        self.servers = servers
        self.openapi_external_docs = openapi_external_docs
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
        if lifespan is not None and not callable(lifespan):
            raise TypeError("lifespan must be an async context-manager factory")
        self.lifespan = lifespan
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
        # The mapping the running lifespan yielded, if any; see Request.state.
        self._lifespan_state: dict[Any, Any] | None = None
        # Replacement callables for dependencies, keyed by the original, as in
        # FastAPI. They apply the next time the app starts serving (a test
        # client or run), never to a running worker.
        self.dependency_overrides: dict[Callable[..., Any], Callable[..., Any]] = {}
        self._built_overrides: dict[Callable[..., Any], Callable[..., Any]] = {}
        self._built_mount_overrides: list[Any] = []
        self._compiled: tuple[_CompiledRoute, ...] = ()
        self._openapi_document: dict[str, Any] | None = None
        self._built_settings: dict[str, Any] | None = None
        self._exception_handlers: dict[type[Exception], CallablePlan] = {}
        self._http_middleware: list[Callable[..., Any]] = []
        # Standard ASGI middleware as (class, args, kwargs), the last outermost.
        self._asgi_middleware: list[tuple[type, tuple, dict[str, Any]]] = []
        # Mounted applications: (prefix, App, name).
        self._mounts: list[tuple[str, App, str | None]] = []
        # (parent App, prefix) once this app is mounted.
        self._mounted: tuple[App, str] | None = None
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
            "compression": compression,
            "otel": otel,
        }
        for exception_type, handler in (exception_handlers or {}).items():
            self.add_exception_handler(exception_type, handler)
        # As in Starlette, the first entry is the outermost middleware, and
        # the last one added runs outermost, so add them in reverse.
        for entry in reversed(list(middleware or ())):
            from .middleware import Middleware

            # Starlette's (and so FastAPI's) Middleware has the same fields.
            if not isinstance(entry, Middleware) and not (
                type(entry).__name__ == "Middleware"
                and hasattr(entry, "cls")
                and isinstance(getattr(entry, "args", None), (tuple, list))
                and isinstance(getattr(entry, "kwargs", None), dict)
            ):
                raise TypeError("middleware entries must be Middleware(...)")
            self.add_middleware(entry.cls, *entry.args, **entry.kwargs)

    @property
    def lifespan(self):
        """The application lifespan; ``on_startup``/``on_shutdown`` handlers
        (and ``on_event``) run as one when no explicit lifespan is given.
        """
        if self._lifespan is not None:
            if self.on_startup or self.on_shutdown:
                raise ValueError(
                    "use either lifespan or on_startup/on_shutdown handlers, not both"
                )
            return self._lifespan
        if self.on_startup or self.on_shutdown:
            return _hooks_lifespan(self.on_startup, self.on_shutdown)
        return None

    @lifespan.setter
    def lifespan(self, value) -> None:
        self._lifespan = value

    def health(
        self, path: str = "/healthz", *, include_in_schema: bool = False
    ) -> None:
        if self._built or self._frozen:
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

    def static(
        self,
        path: str,
        directory: str | os.PathLike[str],
        *,
        html: bool = False,
        cache_control: str | None = None,
        name: str | None = None,
    ) -> None:
        """Serve the files under ``directory`` natively at ``path``.

        Rust resolves each request under the directory (never outside it, and
        never dotfiles), answers conditional and single-range requests, and
        serves precompressed ``.br``/``.gz`` siblings when accepted. With
        ``html=True`` a directory serves its ``index.html``. A ``name`` lets
        ``url_path_for(name, path="css/site.css")`` build file URLs.
        """
        if self._built or self._frozen:
            raise RuntimeError("routes cannot change after app.build()")
        if type(html) is not bool:
            raise TypeError("html must be a boolean")
        if cache_control is not None and (
            not isinstance(cache_control, str)
            or not cache_control
            or any(
                ord(character) < 32 or ord(character) == 127
                for character in cache_control
            )
        ):
            raise ValueError("cache_control must be a nonempty header value")
        if name is not None and (not isinstance(name, str) or not name):
            raise ValueError("route names must be nonempty strings")
        prefix = _path(path).rstrip("/")
        if _PARAMETER.search(prefix) or "{" in prefix or "}" in prefix:
            raise ValueError("static paths must be literal prefixes")
        root = os.path.realpath(os.fspath(directory))
        if not os.path.isdir(root):
            raise ValueError(f"static directory does not exist: {directory}")
        files = {"directory": root, "html": html, "cache_control": cache_control}
        paths = [f"{prefix}/{{*path}}"]
        if html:
            paths.append(f"{prefix}/")
            if prefix:
                # The bare prefix redirects to the slashed index URL.
                paths.append(prefix)
        for route_path in paths:
            self._routes.append(
                _Route(
                    "GET",
                    route_path,
                    None,
                    200,
                    None,
                    "files",
                    include_in_schema=False,
                    files=files,
                    name=name,
                )
            )

    def mount(self, path: str, app: Any, *, name: str | None = None) -> None:
        """Serve another ``App`` under the literal prefix ``path``.

        As with FastAPI's ``app.mount``, the mounted app keeps its own
        exception handlers, ``dependency_overrides``, ``state``, app-level
        dependencies and OpenAPI document (served under the prefix, with the
        prefix as its server URL); its routes are left out of this app's
        document and this app's dependencies do not apply to them. This app's
        HTTP middleware wraps the mounted routes, outside the mounted app's
        own. ``url_path_for("name:route")`` reaches a route of a mount named
        ``name``. Settings that configure the whole server (CORS, JWT, API
        keys, rate limits, compression and the like) and ``lifespan`` belong
        on the outermost app, so a mounted app must leave them unset.
        """
        from .staticfiles import StaticFiles

        if isinstance(app, StaticFiles):
            # FastAPI's app.mount("/static", StaticFiles(directory=...)).
            self.static(path, app.directory, html=app.html, name=name)
            return
        if self._built or self._frozen:
            raise RuntimeError("routes cannot change after app.build()")
        if not isinstance(app, App):
            if not callable(app):
                raise TypeError(
                    "mount expects a bessemer App, StaticFiles or an ASGI app"
                )
            self._mount_asgi(path, app, name)
            return
        if app is self or self in _ancestry(app):
            raise ValueError("an app cannot be mounted inside itself")
        if app._mounted:
            raise ValueError("an app can only be mounted once")
        if name is not None and (not isinstance(name, str) or not name or ":" in name):
            raise ValueError("mount names must be nonempty strings without ':'")
        prefix = _path(path).rstrip("/")
        if not prefix or _PARAMETER.search(prefix) or "{" in prefix or "}" in prefix:
            raise ValueError("mount paths must be literal, non-root prefixes")
        if any(existing == prefix for existing, _, _ in self._mounts):
            raise ValueError(f"an app is already mounted at {prefix}")
        if name is not None and any(name == other for _, _, other in self._mounts):
            raise ValueError(f"duplicate mount name: {name}")
        if app.lifespan is not None:
            raise ValueError(
                "a mounted app cannot have a lifespan; give it to the outermost app"
            )
        if app._asgi_middleware:
            raise ValueError(
                "a mounted app cannot have ASGI middleware; give it to the outermost app"
            )
        defaults = App()._settings
        changed = sorted(
            key for key, value in app._settings.items() if value != defaults[key]
        )
        if changed:
            raise ValueError(
                f"a mounted app cannot set server-wide options ({', '.join(changed)});"
                " set them on the outermost app"
            )
        app._mounted = (self, prefix)
        self._mounts.append((prefix, app, name))

    def _mount_asgi(self, path: str, app: Any, name: str | None) -> None:
        """Serve any ASGI application (Starlette, Django, a plain ASGI
        callable) below the literal prefix ``path``.

        Every request under the prefix enters Python and reaches the app as
        an ASGI ``http`` call with the whole body already read; ``path`` is
        the full path and ``root_path`` the prefix, as Starlette's ``Mount``
        sets them. This app's HTTP and ASGI middleware wrap it. The mounted
        app's own lifespan does not run, as in Starlette.
        """
        from ._asgi import mount_endpoint

        if name is not None and (not isinstance(name, str) or not name or ":" in name):
            raise ValueError("mount names must be nonempty strings without ':'")
        prefix = _path(path).rstrip("/")
        if not prefix or _PARAMETER.search(prefix) or "{" in prefix or "}" in prefix:
            raise ValueError("mount paths must be literal, non-root prefixes")
        if any(existing == prefix for existing, _, _ in self._mounts) or any(
            getattr(route.handler, "_bessemer_asgi_mount", None) == prefix
            for route in self._routes
        ):
            raise ValueError(f"an app is already mounted at {prefix}")
        if name is not None and (
            any(name == other for _, _, other in self._mounts)
            or any(route.name == name for route in self._routes)
        ):
            raise ValueError(f"duplicate mount name: {name}")
        endpoint = mount_endpoint(app, prefix)
        endpoint._bessemer_asgi_mount = prefix
        for method in _ASGI_MOUNT_METHODS:
            for route_path in (prefix, f"{prefix}/", f"{prefix}/{{*path}}"):
                self._routes.append(
                    _Route(
                        method,
                        route_path,
                        endpoint,
                        200,
                        None,
                        include_in_schema=False,
                        name=name
                        if method == "GET" and route_path.endswith("{*path}")
                        else None,
                    )
                )

    def _mount_prefix(self) -> str:
        """The path this app is served under, through every mount above it."""
        if not self._mounted:
            return ""
        parent, prefix = self._mounted
        return parent._mount_prefix() + prefix

    def _served_routes(self) -> list[_Route]:
        """This app's routes, then those of its mounts under their prefixes."""
        explicit = {
            (route.method, _PARAMETER.sub("{}", route.path))
            for route in self._routes
            if route.kind != "files"
        }
        # An application route at a mount's index keeps priority, in either
        # registration order, as it does over the files below the mount.
        routes = [
            route
            for route in self._routes
            if not (
                route.kind == "files"
                and not route.path.endswith("{*path}")
                and (route.method, route.path) in explicit
            )
        ]
        for prefix, app, _ in self._mounts:
            middleware = tuple(reversed(app._http_middleware))
            for route in app._served_routes():
                values = dict(route.__dict__)
                values.update(
                    path=prefix + route.path,
                    include_in_schema=False,
                    app=route.app or app,
                    middleware=middleware + route.middleware,
                    source=route.source or route,
                )
                routes.append(_Route(**values))
        return routes

    def _mount_tree(self, outer: str = "") -> list[tuple[str, App]]:
        """Every mounted app, nested ones included, with its full prefix."""
        found = []
        for prefix, app, _ in self._mounts:
            found.append((outer + prefix, app))
            found.extend(app._mount_tree(outer + prefix))
        return found

    def _mount_documents(
        self,
        routes: list[_Route],
        compiled: list[_CompiledRoute],
        settings: dict[str, Any],
    ) -> list[_Route]:
        """The OpenAPI and docs routes of each mounted app, under its prefix.

        A mounted app's document is built from its routes as this server
        compiled them, under this server's settings (its authentication
        included), with paths relative to the mount and the full prefix as
        the server URL.
        """
        from .openapi import build_document, documentation_page, encode_document

        documents = []
        for prefix, app in self._mount_tree():
            if app.openapi_url is None:
                continue
            owned = [
                (route.source, plan)
                for route, plan in zip(routes, compiled, strict=True)
                if route.app is app
            ]
            document = build_document(
                app,
                [route for route, _ in owned],
                tuple(plan for _, plan in owned),
                _mounted_settings(settings, prefix),
            )
            document["servers"] = [{"url": prefix}]
            documents.append(
                _Route(
                    "GET",
                    prefix + app.openapi_url,
                    None,
                    200,
                    None,
                    "static",
                    include_in_schema=False,
                    content=encode_document(document),
                )
            )
            if app.docs_url is not None:
                documents.append(
                    _Route(
                        "GET",
                        prefix + app.docs_url,
                        None,
                        200,
                        None,
                        "static",
                        include_in_schema=False,
                        content=documentation_page(app.title, prefix + app.openapi_url),
                        content_type="text/html; charset=utf-8",
                    )
                )
        return documents

    def exception_handler(self, exception_type: type[Exception] | int):
        """Register a handler for an exception class or, as in FastAPI, for
        the ``HTTPException`` status code given as an ``int``. A ``500``
        handler also receives unhandled exceptions.
        """
        if type(exception_type) is int:
            if not 400 <= exception_type <= 599:
                raise ValueError("exception handler status codes must be 400-599")
        elif not isinstance(exception_type, type) or not issubclass(
            exception_type, Exception
        ):
            raise TypeError(
                "exception handlers require an Exception subclass or status code"
            )

        def register(handler):
            if self._built or self._frozen:
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

    def add_exception_handler(
        self, exc_class_or_status_code: type[Exception] | int, handler
    ) -> None:
        """Register an exception handler without a decorator, as in FastAPI."""
        self.exception_handler(exc_class_or_status_code)(handler)

    def add_middleware(self, middleware_class: type, *args, **kwargs) -> None:
        """Add FastAPI/Starlette-style middleware by class.

        ``CORSMiddleware``, ``GZipMiddleware`` and ``TrustedHostMiddleware``
        from ``bessemer.middleware`` configure the native controls, so they
        run in Rust before Python. A ``BaseHTTPMiddleware`` subclass runs its
        ``dispatch`` as ``@app.middleware("http")``. Any other class is
        standard ASGI middleware (Starlette's ``SessionMiddleware``, for
        example), instantiated once per worker as ``cls(app, *args,
        **kwargs)``. ASGI middleware wraps every Python HTTP route, outside
        the HTTP middleware, and moves those routes off the native fast path;
        native routes and WebSockets are served without it.
        """
        from .middleware import BaseHTTPMiddleware, _NativeMiddleware

        if self._built or self._frozen:
            raise RuntimeError("middleware cannot change after app.build()")
        if not isinstance(middleware_class, type) and not callable(middleware_class):
            raise TypeError("middleware must be a class or a callable factory")
        if isinstance(middleware_class, type) and issubclass(
            middleware_class, (_NativeMiddleware, BaseHTTPMiddleware)
        ):
            middleware_class(self, *args, **kwargs)
            return
        if self._mounted:
            raise ValueError(
                "a mounted app cannot have ASGI middleware; give it to the outermost app"
            )
        self._asgi_middleware.append((middleware_class, args, dict(kwargs)))

    def middleware(self, middleware_type: str):
        """Register an ``async def middleware(request, call_next)`` function.

        As in FastAPI, ``await call_next(request)`` runs the rest of the chain
        and the route, and returns a response whose ``status_code`` and
        ``headers`` can be changed; the middleware may instead return its own
        ``Response``. The last registered middleware runs outermost. HTTP
        middleware runs around Python routes; WebSockets and native routes
        (static files, health, metrics and the OpenAPI documents) are served
        without entering Python.
        """
        if middleware_type != "http":
            raise ValueError('only "http" middleware is supported')

        def register(function):
            if self._built or self._frozen:
                raise RuntimeError("middleware cannot change after app.build()")
            if not inspect.iscoroutinefunction(function):
                raise TypeError("HTTP middleware must be an async function")
            parameters = tuple(inspect.signature(function).parameters.values())
            if len(parameters) != 2 or any(
                parameter.kind
                not in (parameter.POSITIONAL_ONLY, parameter.POSITIONAL_OR_KEYWORD)
                for parameter in parameters
            ):
                raise ValueError(
                    "HTTP middleware must accept exactly two positional arguments: request and call_next"
                )
            self._http_middleware.append(function)
            return function

        return register

    def _export_settings(self) -> dict[str, Any]:
        from .middleware import validate_settings

        if self._built_settings is not None:
            return copy.deepcopy(self._built_settings)
        return validate_settings(self._settings)

    def build(self) -> App:
        self.lifespan  # noqa: B018 -- Rejects a lifespan alongside event handlers.
        for prefix, app in self._mount_tree():
            if app.lifespan is not None:
                raise ValueError(
                    f"the app mounted at {prefix} has startup or shutdown handlers;"
                    " give them to the outermost app"
                )
        overrides = self.dependency_overrides
        if not isinstance(overrides, dict):
            raise TypeError("dependency_overrides must be a dict")
        mounted = [
            dict(app.dependency_overrides)
            if isinstance(app.dependency_overrides, dict)
            else app.dependency_overrides
            for app in _ancestry(self)
        ]
        if (
            self._built
            and overrides == self._built_overrides
            and mounted == self._built_mount_overrides
        ):
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
            # Routes sharing a structure must agree on names and converters;
            # ``{name:str}`` is the same as ``{name}``.
            template = route.path.replace(":str}", "}")
            if structural_path in paths and paths[structural_path] != template:
                raise ValueError(
                    "ambiguous path parameter names or converters:"
                    f" {paths[structural_path]} and {route.path}"
                )
            paths[structural_path] = template
            key = (route.method, structural_path)
            if key in seen:
                raise ValueError(
                    f"duplicate or ambiguous route: {route.method} {route.path}"
                )
            seen.add(key)
            owner = route.app or self
            compiled.append(
                _compile(
                    route,
                    len(compiled),
                    overrides if route.app is None else _overrides(route.app),
                )
            )
            if route.kind == "python" and (
                owner._exception_handlers
                or self._http_middleware
                or route.middleware
                or self._asgi_middleware
            ):
                compiled[-1].declaration["needs_request"] = True

        routes = self._served_routes()
        for route in routes:
            append(route)
        if settings.get("jwt") is None and any(
            route.declaration.get("needs_principal") for route in compiled
        ):
            raise ValueError("Principal parameters require App(jwt=JWTAuth(...))")
        metrics_path = settings.get("metrics_path")
        if metrics_path is not None and ("GET", metrics_path) in seen:
            raise ValueError("metrics_path conflicts with an application route")
        document = build_document(self, routes, tuple(compiled), settings)
        mount_documents = self._mount_documents(routes, compiled, settings)
        for _, app in self._mount_tree():
            app._frozen = True
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
        for route in mount_documents:
            append(route)
        if metrics_path is not None and ("GET", metrics_path) in seen:
            raise ValueError("metrics_path conflicts with a documentation route")
        self._compiled = tuple(compiled)
        self._openapi_document = document
        self._built_settings = settings
        self._built_overrides = overrides
        self._built_mount_overrides = mounted
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
        tls_cert: str | os.PathLike[str] | None = None,
        tls_key: str | os.PathLike[str] | None = None,
        tls_min: str = "1.3",
        tls_client_ca: str | os.PathLike[str] | None = None,
        tls_client_crl: str | os.PathLike[str] | None = None,
        tls_ocsp_response: str | os.PathLike[str] | None = None,
        tls_client_auth: str = "required",
        tls_reload_interval: float = 60.0,
        http: str = "auto",
        websocket_compression: str = "none",
        max_upload_size: int = 0,
        max_upload_bytes_in_flight: int = 4_294_967_296,
        upload_dir: str | os.PathLike[str] | None = None,
        loops: int = 1,
        event_loop: str = "asyncio",
        io_backend: str = "epoll",
        workers: int = 1,
    ) -> None:
        """Serve the application until a signal stops it.

        ``workers`` (1 by default) is the number of worker processes. With 1
        the application is served from this process. Any other value starts
        the same process supervisor as ``bessemer run``: 0 selects one worker
        per usable CPU, and 2 to 256 that many workers. Each worker is a fresh
        interpreter that imports this application by its module and variable
        name, so the application must be a module-level variable and the
        ``app.run(...)`` call must sit under ``if __name__ == "__main__":``.

        With ``tls_cert`` (a PEM certificate chain) and ``tls_key``
        (its PEM private key), connections are TLS terminated natively and
        requests see the ``https``/``wss`` scheme. ``tls_min`` is the lowest
        protocol version accepted: ``"1.3"`` by default, ``"1.2"`` to opt in.
        ``http="auto"`` serves HTTP/2 alongside HTTP/1.1 (by ALPN over TLS, by
        prior knowledge on plaintext); ``"1.1"`` serves HTTP/1.1 only.
        ``tls_client_ca`` (a PEM CA bundle) turns on mutual TLS: clients must
        present a certificate that chains to it, or with
        ``tls_client_auth="optional"`` may present one. The verified leaf
        certificate is ``request.client_certificate`` (DER bytes).
        ``tls_client_crl`` (PEM CRLs) refuses revoked client certificates;
        every client certificate's issuer then needs a current CRL in it.
        ``tls_ocsp_response`` (a DER OCSP response for ``tls_cert``) is stapled
        to every handshake; refresh the file and the reload picks it up.
        The TLS files are checked every ``tls_reload_interval`` seconds (60 by
        default, 0 never) and new connections use changed files once they
        form a valid configuration, so certificates rotate without a restart.
        ``websocket_compression="deflate"`` accepts ``permessage-deflate``
        (RFC 7692) when a WebSocket client offers it; ``"none"`` (the default)
        never compresses.
        ``max_upload_size`` (0, off, by default) lets routes with ``File()``
        parameters accept ``multipart/form-data`` bodies up to that size: a
        body without a ``Content-Length`` within ``max_body_size`` is parsed
        as it arrives and its files are spooled to an unlinked temporary file
        in ``upload_dir`` (the system temp directory by default), with at most
        ``max_upload_bytes_in_flight`` bytes on disk across the server.
        ``loops`` (1 by default) runs that many asyncio event loops in the
        process, each in its own thread, and sends each request to the least
        busy one. On free-threaded Python they run handlers in parallel; on
        builds with the GIL extra loops add no throughput. Lifespan,
        application dependencies and WebSocket sessions stay on the first
        loop, so objects tied to one event loop must not be shared with
        handlers when ``loops`` is above 1.
        ``event_loop="bessemer"`` runs the worker on bessemer's own event
        loop, written in Rust, instead of asyncio's. With one loop it also
        serves the connections on the same thread, which removes a thread
        hop per request. It is available on Linux; elsewhere the worker logs
        a warning and runs asyncio.
        ``io_backend="io_uring"`` does the socket I/O of a worker that serves
        connections on bessemer's loop (``event_loop="bessemer"`` with one
        loop) through io_uring instead of epoll. It needs Linux 6.1 or later
        and a seccomp profile that allows io_uring; otherwise, or with any
        other loop configuration, the worker logs a warning and uses epoll.
        """
        if os.environ.get("BESSEMER_SUPERVISED_WORKER") == "1" and listener_fd is None:
            raise ValueError(
                "app.run() was called while a bessemer worker imported the "
                'application; put the call under if __name__ == "__main__":'
            )
        if type(workers) is not int or not 0 <= workers <= 256:
            raise ValueError("workers must be an integer between 0 and 256")
        if workers != 1:
            if listener_fd is not None or ready_fd is not None:
                raise ValueError("listener_fd and ready_fd need workers=1")
            self._run_workers(
                workers,
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
                max_requests=max_requests,
                tls_cert=tls_cert,
                tls_key=tls_key,
                tls_min=tls_min,
                tls_client_ca=tls_client_ca,
                tls_client_crl=tls_client_crl,
                tls_ocsp_response=tls_ocsp_response,
                tls_client_auth=tls_client_auth,
                tls_reload_interval=tls_reload_interval,
                http=http,
                websocket_compression=websocket_compression,
                max_upload_size=max_upload_size,
                max_upload_bytes_in_flight=max_upload_bytes_in_flight,
                upload_dir=upload_dir,
                loops=loops,
                event_loop=event_loop,
                io_backend=io_backend,
            )
            return
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
            tls_cert=tls_cert,
            tls_key=tls_key,
            tls_min=tls_min,
            tls_client_ca=tls_client_ca,
            tls_client_crl=tls_client_crl,
            tls_ocsp_response=tls_ocsp_response,
            tls_client_auth=tls_client_auth,
            tls_reload_interval=tls_reload_interval,
            http=http,
            websocket_compression=websocket_compression,
            max_upload_size=max_upload_size,
            max_upload_bytes_in_flight=max_upload_bytes_in_flight,
            upload_dir=upload_dir,
            loops=loops,
            event_loop=event_loop,
            io_backend=io_backend,
        )

    def _run_workers(self, workers: int, **server: Any) -> None:
        """Serve through the process supervisor that ``bessemer run`` uses."""
        import logging

        from .config import SERVER_DEFAULTS, SUPERVISOR_DEFAULTS, validate_settings
        from .supervisor import Supervisor

        target, import_path = _importable_target(self)
        # Check the files here so a typo fails before any worker starts.
        tls_cert, _ = _tls_files(server["tls_cert"], server["tls_key"])
        tls_client_ca = _tls_client_file(
            "tls_client_ca", server["tls_client_ca"], tls_cert, "tls_cert and tls_key"
        )
        _tls_client_file(
            "tls_client_crl", server["tls_client_crl"], tls_client_ca, "tls_client_ca"
        )
        _tls_client_file(
            "tls_ocsp_response",
            server["tls_ocsp_response"],
            tls_cert,
            "tls_cert and tls_key",
        )
        for name in (
            "tls_cert",
            "tls_key",
            "tls_client_ca",
            "tls_client_crl",
            "tls_ocsp_response",
            "upload_dir",
        ):
            if server[name] is not None:
                server[name] = os.fspath(server[name])
        if server["upload_dir"] is not None and not os.path.isdir(server["upload_dir"]):
            raise ValueError(f"upload_dir {server['upload_dir']} is not a directory")
        settings = validate_settings(
            SUPERVISOR_DEFAULTS
            | {name: server[name] for name in SERVER_DEFAULTS}
            | {"workers": workers, "startup_timeout": server["startup_timeout"]}
        )
        # Report route and model errors here rather than once per worker.
        self.build()
        supervisor = Supervisor(target, settings, import_path=import_path)
        if supervisor.workers > 1 and self._export_settings().get("metrics_path"):
            raise ValueError(
                "process-local metrics require workers=1 until multi-process "
                "aggregation is supported"
            )
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s %(name)s %(levelname)s %(message)s",
        )
        exit_code = supervisor.run()
        if exit_code not in (0, 130):
            raise RuntimeError(f"bessemer workers stopped with exit code {exit_code}")

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
        tls_cert: str | os.PathLike[str] | None = None,
        tls_key: str | os.PathLike[str] | None = None,
        tls_min: str = "1.3",
        tls_client_ca: str | os.PathLike[str] | None = None,
        tls_client_crl: str | os.PathLike[str] | None = None,
        tls_ocsp_response: str | os.PathLike[str] | None = None,
        tls_client_auth: str = "required",
        tls_reload_interval: float = 60.0,
        http: str = "auto",
        websocket_compression: str = "none",
        max_upload_size: int = 0,
        max_upload_bytes_in_flight: int = 4_294_967_296,
        upload_dir: str | os.PathLike[str] | None = None,
        loops: int = 1,
        event_loop: str = "asyncio",
        io_backend: str = "epoll",
    ) -> None:
        from . import _native
        from .config import validate_choices, validate_loops, validate_server_limits
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
                "max_upload_size": max_upload_size,
                "max_upload_bytes_in_flight": max_upload_bytes_in_flight,
                "upload_dir": upload_dir,
            }
        )
        validate_loops(loops)
        validate_choices(
            {
                "tls_min": tls_min,
                "tls_client_auth": tls_client_auth,
                "http": http,
                "websocket_compression": websocket_compression,
                "event_loop": event_loop,
                "io_backend": io_backend,
            }
        )
        if upload_dir is not None:
            upload_dir = os.fspath(upload_dir)
            if not os.path.isdir(upload_dir):
                raise ValueError(f"upload_dir {upload_dir} is not a directory")
        for name, value in (
            ("listener_fd", listener_fd),
            ("ready_fd", ready_fd),
            ("shutdown_fd", shutdown_fd),
        ):
            if value is not None and (type(value) is not int or value < 0):
                raise ValueError(f"{name} must be a nonnegative file descriptor")
        if type(max_requests) is not int or max_requests < 0:
            raise ValueError("max_requests must be a nonnegative integer")
        tls_cert, tls_key = _tls_files(tls_cert, tls_key)
        tls_client_ca = _tls_client_file(
            "tls_client_ca", tls_client_ca, tls_cert, "tls_cert and tls_key"
        )
        tls_client_crl = _tls_client_file(
            "tls_client_crl", tls_client_crl, tls_client_ca, "tls_client_ca"
        )
        tls_ocsp_response = _tls_client_file(
            "tls_ocsp_response", tls_ocsp_response, tls_cert, "tls_cert and tls_key"
        )
        if (
            isinstance(tls_reload_interval, bool)
            or not isinstance(tls_reload_interval, (int, float))
            or not 0 <= tls_reload_interval <= 86_400
        ):
            raise ValueError("tls_reload_interval must be between 0 and 86400 seconds")
        tls_reload_interval = float(tls_reload_interval)
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
            loops=loops,
            event_loop=event_loop,
            io_backend=io_backend,
        ) as worker:
            # Requests reach the worker through a native batched queue, so the
            # server's threads never take the GIL on the request path. Paths
            # that hold a Python future, such as WebSocket sessions, use submit.
            # Each extra event loop gets a queue of its own.
            bridges = [_native.Bridge(worker.submit)] + [
                _native.Bridge() for _ in range(loops - 1)
            ]
            worker.attach_native(*bridges)
            _native.serve(
                routes,
                bridges[0] if loops == 1 else bridges,
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
                ready_fd=_native_handle(ready_fd),
                max_requests=max_requests,
                controls=self._export_settings(),
                shutdown_fd=shutdown_fd,
                tls_cert=tls_cert,
                tls_key=tls_key,
                tls_min=tls_min,
                tls_client_ca=tls_client_ca,
                tls_client_crl=tls_client_crl,
                tls_ocsp_response=tls_ocsp_response,
                tls_client_auth=tls_client_auth,
                tls_reload_interval=tls_reload_interval,
                http=http,
                websocket_compression=websocket_compression,
                max_upload_size=max_upload_size,
                max_upload_bytes_in_flight=max_upload_bytes_in_flight,
                upload_dir=upload_dir,
                event_loop=worker._loop if worker._serves_connections else None,
                io_backend=worker._io_backend,
            )


def _native_handle(fd: int | None) -> int | None:
    """The OS handle the native server writes to for a C runtime descriptor.

    Sockets are passed as they are on every platform. A pipe descriptor is
    already the OS handle on POSIX; on Windows the native side needs the
    HANDLE behind the C runtime descriptor.
    """
    if fd is None or sys.platform != "win32":
        return fd
    import msvcrt

    return msvcrt.get_osfhandle(fd)
