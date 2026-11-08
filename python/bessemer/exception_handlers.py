"""Default exception handlers matching ``fastapi.exception_handlers``."""

from __future__ import annotations

from typing import Any

from .encoders import jsonable_encoder
from .exceptions import WebSocketRequestValidationError
from .request import Request, RequestValidationError
from .responses import HTTPException, JSONResponse, Response
from .websockets import WebSocket

__all__ = [
    "http_exception_handler",
    "request_validation_exception_handler",
    "websocket_request_validation_exception_handler",
]


async def http_exception_handler(request: Request, exc: HTTPException) -> Response:
    """Default handler for ``HTTPException``, matching FastAPI."""
    headers = getattr(exc, "headers", None)
    if exc.status_code in {204, 205, 304}:
        return Response(status_code=exc.status_code, headers=headers)
    return JSONResponse(
        {"detail": exc.detail}, status_code=exc.status_code, headers=headers
    )


async def request_validation_exception_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    """Default handler for ``RequestValidationError``, matching FastAPI."""
    return JSONResponse(
        status_code=422,
        content={"detail": jsonable_encoder(exc.errors())},
    )


async def websocket_request_validation_exception_handler(
    websocket: WebSocket, exc: WebSocketRequestValidationError | Any
) -> None:
    """Default handler for ``WebSocketRequestValidationError``, matching FastAPI."""
    await websocket.close(code=1008)
