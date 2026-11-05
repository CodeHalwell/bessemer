"""Exceptions with the names ``fastapi.exceptions`` uses."""

from __future__ import annotations

from .request import RequestValidationError
from .responses import HTTPException
from .websockets import WebSocketDisconnect, WebSocketException

__all__ = [
    "HTTPException",
    "RequestValidationError",
    "WebSocketDisconnect",
    "WebSocketException",
]
