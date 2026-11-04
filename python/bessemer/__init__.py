"""Bessemer's application API. This initial bridge is a compatibility path."""

from .application import App, HTTPException, Router
from .auth import JWTAuth, Principal, require_scope
from .middleware import CORS, Compression
from .params import Body, Cookie, Depends, File, Form, Header, Path, Query
from .request import (
    BackgroundTasks,
    Request,
    RequestValidationError,
    State,
    UploadFile,
)
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
    "Compression",
    "Cookie",
    "Depends",
    "EventSourceResponse",
    "File",
    "Form",
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
    "UploadFile",
    "WebSocket",
    "WebSocketDisconnect",
    "require_scope",
]
__version__ = "1.0.0rc1"
