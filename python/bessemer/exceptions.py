"""Exceptions with the names ``fastapi.exceptions`` uses."""

from __future__ import annotations

from .request import RequestValidationError, ResponseValidationError
from .responses import HTTPException
from .websockets import WebSocketDisconnect, WebSocketException


class FastAPIError(RuntimeError, ValueError):
    """Application declaration or runtime error, as in ``fastapi.exceptions``."""


class WebSocketRequestValidationError(RequestValidationError):
    """WebSocket request input validation failed."""


__all__ = [
    "FastAPIError",
    "HTTPException",
    "RequestValidationError",
    "ResponseValidationError",
    "WebSocketDisconnect",
    "WebSocketException",
    "WebSocketRequestValidationError",
]
