"""Bessemer's application API. This initial bridge is a compatibility path."""

from .application import App, HTTPException, Router
from .auth import JWTAuth, Principal, require_scope
from .middleware import CORS
from .params import Body, Cookie, Depends, Header, Path, Query
from .request import BackgroundTasks, Request, RequestValidationError, State
from .responses import (
    EventSourceResponse,
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
    Response,
    StreamingResponse,
)
from .websockets import WebSocket, WebSocketDisconnect

__all__ = [
    "CORS",
    "App",
    "BackgroundTasks",
    "Body",
    "Cookie",
    "Depends",
    "EventSourceResponse",
    "HTMLResponse",
    "HTTPException",
    "Header",
    "JSONResponse",
    "JWTAuth",
    "Path",
    "PlainTextResponse",
    "Principal",
    "Query",
    "RedirectResponse",
    "Request",
    "RequestValidationError",
    "Response",
    "Router",
    "State",
    "StreamingResponse",
    "WebSocket",
    "WebSocketDisconnect",
    "require_scope",
]
__version__ = "0.2.0a3"
