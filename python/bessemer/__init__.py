"""Bessemer's application API. This initial bridge is a compatibility path."""

from . import status as status  # As ``from fastapi import status``.
from .application import App, HTTPException, NoMatchFound, Router
from .auth import JWTAuth, Principal, require_scope
from .middleware import CORS, Compression
from .otel import OpenTelemetry
from .params import Body, Cookie, Depends, File, Form, Header, Path, Query, Security
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
from .websockets import WebSocket, WebSocketDisconnect, WebSocketException

#: FastAPI's name for ``Router``.
APIRouter = Router

__all__ = [
    "CORS",
    "APIRouter",
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
    "NoMatchFound",
    "OpenTelemetry",
    "Path",
    "PlainTextResponse",
    "Principal",
    "Query",
    "RedirectResponse",
    "Request",
    "RequestValidationError",
    "Response",
    "Router",
    "Security",
    "State",
    "StreamingResponse",
    "UploadFile",
    "WebSocket",
    "WebSocketDisconnect",
    "WebSocketException",
    "require_scope",
]
__version__ = "1.0.0rc1"
