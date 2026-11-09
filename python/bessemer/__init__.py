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
    ResponseValidationError,
    State,
    UploadFile,
)
from .responses import (
    EventSourceResponse,
    FileResponse,
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
#: FastAPI's name for ``App``, so ``from bessemer import FastAPI`` ports as is.
FastAPI = App

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
    "FastAPI",
    "File",
    "FileResponse",
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
    "ResponseValidationError",
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
__version__ = "1.0.0rc2"
