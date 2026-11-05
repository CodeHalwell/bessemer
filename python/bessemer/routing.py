"""Route descriptions and router names matching ``fastapi.routing``."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from .application import Router

#: FastAPI's name for a router; Bessemer's ``Router`` has the same options.
APIRouter = Router


@dataclass(frozen=True)
class APIRoute:
    """A read-only description of an HTTP route, like FastAPI's ``APIRoute``.

    ``generate_unique_id_function`` receives one of these, and ``app.routes``
    lists them. Routes compile once at startup, so these are not editable:
    set ``operation_id=`` or ``generate_unique_id_function=`` instead.
    """

    path: str
    endpoint: Callable[..., Any]
    methods: frozenset[str]
    name: str | None
    tags: tuple[str, ...]
    summary: str | None
    description: str | None
    operation_id: str | None
    status_code: int
    response_model: Any
    response_class: Any
    responses: Mapping[Any, Any]
    deprecated: bool
    include_in_schema: bool
    dependencies: tuple[Any, ...]

    @property
    def path_format(self) -> str:
        return self.path

    @classmethod
    def _of(cls, route: Any) -> APIRoute:
        from .responses import JSONResponse

        model = route.response_model
        return cls(
            path=route.path,
            endpoint=route.handler,
            methods=frozenset({route.method}),
            name=route.name,
            tags=route.tags,
            summary=route.summary,
            description=route.description,
            operation_id=route.operation_id,
            status_code=route.status_code,
            response_model=None if model is _missing() else model,
            response_class=route.response_class or JSONResponse,
            responses=dict(route.responses or {}),
            deprecated=route.deprecated,
            include_in_schema=route.include_in_schema,
            dependencies=route.dependencies,
        )


@dataclass(frozen=True)
class APIWebSocketRoute:
    """A read-only description of a WebSocket route."""

    path: str
    endpoint: Callable[..., Any]
    name: str | None
    dependencies: tuple[Any, ...]

    @property
    def path_format(self) -> str:
        return self.path

    @classmethod
    def _of(cls, route: Any) -> APIWebSocketRoute:
        return cls(route.path, route.handler, route.name, route.dependencies)


def _missing() -> Any:
    import inspect

    return inspect.Parameter.empty


__all__ = ["APIRoute", "APIRouter", "APIWebSocketRoute"]
