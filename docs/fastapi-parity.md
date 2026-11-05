# FastAPI parity

This is an assessment of how closely Bessemer's public API matches FastAPI's,
and of what changed to bring the two closer. The goal is that a FastAPI
application ports by changing its imports, without giving up Bessemer's
native request path.

Compared against FastAPI 0.162.0 and Starlette 1.7.0 (which FastAPI
re-exports), from Bessemer 1.0.0rc1 at `7c2f7f6`. The signatures were
compared mechanically with `inspect.signature` across every public class,
method and parameter marker, then checked by hand for behaviour.

## Porting in one change

```python
# Before
from fastapi import FastAPI, APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import HTMLResponse
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI(title="Shop")

# After
from bessemer import App as FastAPI, APIRouter, Depends, HTTPException, Query, status
from bessemer.responses import HTMLResponse
from bessemer.middleware import CORSMiddleware

app = FastAPI(title="Shop")
```

The application class is called `App` rather than `FastAPI`. Everything else
in the example keeps its FastAPI name. Where FastAPI spreads names across
`fastapi.middleware.cors`, `fastapi.middleware.gzip` and so on, Bessemer keeps
them in one module, `bessemer.middleware`.

| FastAPI import | Bessemer import |
| --- | --- |
| `fastapi` (top-level names) | `bessemer` |
| `fastapi.FastAPI` | `bessemer.App` |
| `fastapi.APIRouter`, `fastapi.routing` | `bessemer.APIRouter`, `bessemer.routing` |
| `fastapi.status` | `bessemer.status` |
| `fastapi.responses` | `bessemer.responses` |
| `fastapi.exceptions` | `bessemer.exceptions` |
| `fastapi.security` | `bessemer.security` |
| `fastapi.encoders` | `bessemer.encoders` |
| `fastapi.concurrency` | `bessemer.concurrency` |
| `fastapi.background` | `bessemer.background` |
| `fastapi.datastructures` | `bessemer.datastructures` |
| `fastapi.staticfiles` | `bessemer.staticfiles` |
| `fastapi.middleware.*` | `bessemer.middleware` |
| `fastapi.testclient` | `bessemer.testclient` |
| `fastapi.utils.generate_unique_id` | `bessemer.utils.generate_unique_id` |

## Status key

- **Same**: it was already there with FastAPI's name and behaviour.
- **Added**: added by this change.
- **Differs**: available, with a difference that is deliberate (usually for
  speed or safety) and described.
- **No**: not supported. Passing it fails at startup rather than being
  silently ignored, as the project's rules require.

## Application (`FastAPI` / `App`)

| Feature | Status | Notes |
| --- | --- | --- |
| `title`, `description`, `version`, `openapi_url`, `docs_url`, `dependencies`, `lifespan` | Same | `docs_url` defaults to `None` (FastAPI: `/docs`). Bessemer serves its own bundled reference page, never a CDN script. |
| `summary`, `terms_of_service`, `contact`, `license_info`, `openapi_tags`, `servers`, `openapi_external_docs` | Added | Written to the OpenAPI document as FastAPI does. |
| `exception_handlers={...}`, `middleware=[Middleware(...)]` | Added | |
| `on_startup`, `on_shutdown`, `app.on_event(...)`, `add_event_handler` | Added | Run as one lifespan. As in Starlette, they cannot be combined with `lifespan=`. |
| `default_response_class`, `responses`, `deprecated`, `include_in_schema`, `generate_unique_id_function` | Added | Same precedence as FastAPI: route, then router, then inclusion, then app. |
| `redoc_url` | Differs | Only `None` is accepted. There is no ReDoc page. |
| `debug` | Differs | Only `False`. Errors are logged by the worker, never sent to clients. |
| `root_path`, `root_path_in_servers`, `routes=`, `redirect_slashes`, `webhooks`, `callbacks`, `swagger_ui_*`, `separate_input_output_schemas`, `strict_content_type` | No | |
| `app.get/post/put/patch/delete/head/options`, `app.api_route`, `app.add_api_route` | Same/Added | `api_route` and `add_api_route` added. `trace` is not supported. |
| `app.websocket`, `app.add_api_websocket_route` | Same/Added | |
| `app.include_router(...)` | Same | Now also takes `default_response_class`, `responses`, `deprecated`, `include_in_schema`, `generate_unique_id_function`. |
| `app.mount(path, app, name=)` | Differs | Takes a Bessemer `App` or `StaticFiles` (added). Arbitrary ASGI apps cannot be mounted. |
| `app.exception_handler(...)`, `app.add_exception_handler(...)` | Same/Added | Status-code keys (`@app.exception_handler(404)`) added. A `500` handler receives unhandled exceptions. |
| `app.middleware("http")` | Same | |
| `app.add_middleware(cls, **options)` | Added | See [middleware](#middleware). |
| `app.routes` | Differs | Read-only `APIRoute` descriptions. Routes compile once, so setting `route.operation_id` raises; use `generate_unique_id_function`. |
| `app.url_path_for`, `app.dependency_overrides`, `app.state`, `app.openapi()` | Same | Replacing `app.openapi` with a function does not change the served document. |
| `app.router` | No | `App` is itself the router. |
| Lifespan yielding a state dict | No | Set `app.state` in the lifespan instead. |

## Routers and route options (`APIRouter`)

`APIRouter` is `bessemer.Router` under FastAPI's name.

| Feature | Status | Notes |
| --- | --- | --- |
| `prefix`, `tags`, `dependencies` | Same | |
| `default_response_class`, `responses`, `deprecated`, `include_in_schema`, `generate_unique_id_function`, `on_startup`, `on_shutdown` | Added | A router's startup and shutdown handlers join the app's when it is included. |
| `lifespan`, `routes`, `route_class`, `callbacks`, `redirect_slashes` | No | |
| Route `status_code`, `dependencies`, `response_model`, `response_model_*`, `tags`, `summary`, `description`, `operation_id`, `include_in_schema`, `name` | Same | |
| Route `response_class` | Added | `JSONResponse`, `ORJSONResponse` and `UJSONResponse` keep the native JSON encoder. Other classes wrap return values that are not already a `Response`, as FastAPI does. |
| Route `responses`, `deprecated`, `response_description`, `openapi_extra`, `generate_unique_id_function` | Added | `responses` accepts `{"model": Model}` entries. |
| Route `callbacks` | No | |
| Route `timeout` | Bessemer only | A per-route deadline. |
| `{name:path}` path converters | No | Parameters occupy a whole segment. Use `app.static` for file trees. |
| Several methods on one route | Differs | Each method is its own route, so each gets its own operation ID. |

The default `operationId` is Bessemer's own (`read_item_get__items__item_id`,
where FastAPI has `read_item_items__item_id__get`);
pass `App(generate_unique_id_function=generate_unique_id)` from
`bessemer.utils` to keep FastAPI's IDs, and so the method names of clients
generated from the document.

## Parameters

| Feature | Status | Notes |
| --- | --- | --- |
| `Path`, `Query`, `Header`, `Cookie`, `Body`, `Form`, `File`, as defaults or in `Annotated` | Same | Required `...` defaults work. |
| `alias`, `description`, `deprecated`, `gt`, `ge`, `lt`, `le`, `min_length`, `max_length`, `pattern`, `convert_underscores` | Same | Constraints run natively in Rust. |
| `title`, `examples`, `openapi_examples`, `json_schema_extra`, `include_in_schema` | Added | Documentation only. |
| `regex`, `example` | Added | FastAPI's deprecated spellings of `pattern` and `examples`. |
| `pattern` as a compiled `re.Pattern` | Added | Flags are refused, since only the source string reaches the validator; use inline flags such as `(?i)`. |
| `Body(media_type=...)` | Added | Must be JSON (`application/json` or `application/*+json`). |
| `Form(media_type=...)`, `File(media_type=...)` | Added | Sets the documented request encoding. `Form` takes urlencoded or `multipart/form-data`, and `File` only multipart; anything else is refused. The server parses either encoding whatever is declared. |
| `default_factory`, `multiple_of`, `strict`, `max_digits`, `decimal_places`, `allow_inf_nan`, `discriminator`, `validation_alias`, `serialization_alias`, `alias_priority`, `union_mode`, `fail_fast`, `validate_default` | No | Passing one raises `TypeError`. |
| `Body(embed=True)`, several body parameters | No | One body per route: group fields in a Pydantic model. |
| `Annotated[..., pydantic.Field(...)]` on a parameter | No | Use the Bessemer marker's constraints. |
| Pydantic model query parameters (`Annotated[Model, Query()]`) | No | |

## Dependencies

| Feature | Status | Notes |
| --- | --- | --- |
| `Depends(dependency, use_cache=)`, sub-dependencies, classes, `yield` dependencies, `dependencies=[...]`, `dependency_overrides` | Same | |
| `Depends(scope=None or "function")` | Added | Both mean Bessemer's request scope. |
| `Depends(scope="request")` | Differs | Cleanup runs before the response is sent, which is FastAPI's `"function"` timing, not after it. |
| `Depends(scope="app")` | Bessemer only | One cached instance per worker. |
| `Security(dependency, scopes=)`, `SecurityScopes` | Same | |

## Request

| Feature | Status | Notes |
| --- | --- | --- |
| `method`, `headers`, `cookies`, `path_params`, `state`, `app`, `body()`, `json()`, `url_for()` | Same | `url_for` now returns a `URL`. |
| `url`, `base_url` | Added | Now a `URL`: a `str` subclass with `scheme`, `hostname`, `port`, `path`, `query`, `replace()` and `include_query_params()`. |
| `query_params` | Added | Now a `QueryParams` with `getlist()` and `multi_items()`. It is parsed on first use, which is also slightly faster for handlers that never read it. |
| `client` | Differs | An `Address`, a `str` subclass with `.host`. `.port` is `None` because the native transport passes only the IP. |
| `form()` | Added | Parses the buffered body off the event loop with the native grammar and limits (CRLF line endings, 1,000 parts, 8 KiB and 16 headers per part), or a 400. No `Content-Transfer-Encoding` decoding, which RFC 7578 deprecates. Prefer `Form()`/`File()` parameters, which are parsed natively. |
| `stream()` | Added | Yields the already-buffered body. |
| `is_disconnected()`, `scope`, `receive`, `send`, `session`, `auth`, `user`, `send_push_promise` | No | There is no ASGI scope. Use security dependencies for authentication. |

## Responses

| Feature | Status | Notes |
| --- | --- | --- |
| `Response`, `JSONResponse`, `HTMLResponse`, `PlainTextResponse`, `RedirectResponse`, `StreamingResponse` | Same | |
| `background=` on every response | Added | Runs with the request's background tasks. |
| `JSONResponse(media_type=)` | Added | |
| `set_cookie(partitioned=)`, `delete_cookie(partitioned=)` | Added | Partitioned cookies must be `Secure`. |
| `FileResponse` | Added | Streams the file in 64 KiB chunks with `Content-Disposition`, `ETag` and `Last-Modified`. For directories, `app.static` serves ranges and conditional requests natively. |
| `ORJSONResponse`, `UJSONResponse` | Added | Names only: Bessemer already encodes JSON natively in Rust, with no extra package. |
| `EventSourceResponse` | Bessemer only | Server-sent events. |
| Return a value, return a `Response` directly, `response_model` | Same | |

## Exceptions

| Feature | Status | Notes |
| --- | --- | --- |
| `HTTPException(status_code, detail, headers)` | Same | `detail` now defaults to the status phrase, as in FastAPI. |
| `RequestValidationError` | Same | `errors()` added. Entries carry `loc`, `type` and a generic `msg`. The submitted `input` is deliberately not echoed back. |
| `WebSocketException(code, reason)` | Added | |
| `ResponseValidationError` | No | An invalid response returns a generic `500`. |

## Middleware

`app.add_middleware(cls, **options)` and `App(middleware=[Middleware(cls, ...)])`
accept these classes from `bessemer.middleware`.

| Class | Status | Notes |
| --- | --- | --- |
| `CORSMiddleware` | Added | Configures native CORS, which runs in Rust. A wildcard origin cannot be combined with credentials; `allow_origin_regex` and `allow_private_network` are not supported. |
| `GZipMiddleware` | Added | Configures native gzip. `minimum_size` is honoured; `compresslevel` is fixed. `App(compression=...)` adds Brotli. |
| `TrustedHostMiddleware` | Added | Configures native host checks. The `www.` redirect is not performed. |
| `BaseHTTPMiddleware` subclasses | Added | `dispatch(request, call_next)` runs as `@app.middleware("http")`. |
| `HTTPSRedirectMiddleware`, `SessionMiddleware`, `AuthenticationMiddleware` and other ASGI middleware | No | ASGI middleware cannot run on the native transport. |

## WebSockets

| Feature | Status | Notes |
| --- | --- | --- |
| `accept(subprotocol=)`, `send_text/bytes/json`, `receive_text/bytes/json`, `iter_*`, `close`, `WebSocketDisconnect` | Same | |
| `url`, `base_url`, `app`, `state` | Added | |
| `accept(headers=)`, raw `send(message)`/`receive()` ASGI messages, `client_state` | No | `receive()` returns the message's `str` or `bytes`. |

## Other modules

| Feature | Status | Notes |
| --- | --- | --- |
| `BackgroundTasks.add_task` | Same | Tasks currently finish before the response is sent. |
| `bessemer.background.BackgroundTask` | Added | |
| `bessemer.security`: `HTTPBearer`, `HTTPBasic`, `OAuth2PasswordBearer`, `OAuth2AuthorizationCodeBearer`, `APIKeyHeader/Query/Cookie`, `OAuth2PasswordRequestForm(Strict)` | Same | |
| `HTTPDigest`, `OAuth2`, `OpenIdConnect` | No | |
| `UploadFile` | Same | Constructor arguments differ; it is normally built by the framework. |
| `status` | Added | Every Starlette name, plus older names such as `HTTP_422_UNPROCESSABLE_ENTITY`. |
| `jsonable_encoder` | Added | Pydantic models, dataclasses, enums, dates, UUIDs, decimals, paths and containers, with the same options. |
| `run_in_threadpool`, `iterate_in_threadpool` | Added | |
| `StaticFiles` | Added | Mounting it calls `app.static`. `packages` and `follow_symlink` are not supported. |
| `TestClient` | Differs | Runs the real server in a thread rather than calling an ASGI app. Server exceptions become `500` responses instead of being raised in the test (no `raise_server_exceptions`). |

## Performance

None of this touches the per-request hot path for routes that do not use it:

- Every new option is read when routes are registered or when the app
  builds, and stored in the compiled route or the OpenAPI document.
- `response_class` wraps only the routes that set a non-JSON class.
- Response `background` reuses the existing `isinstance` check on the
  lean path, so a handler returning a `dict` does the same work as before.
- Status-code exception handlers are looked up only after an exception.
- `CORSMiddleware`, `GZipMiddleware` and `TrustedHostMiddleware` configure
  native controls instead of adding Python middleware.
- `request.query_params` is now parsed on first use rather than on every
  request with a query string.

Measured with the `benchmarks/compare` harness, main and this change were
within run-to-run noise on every route, with unchanged p99 latency
([results](../benchmarks/results/2026-11-05/fastapi-parity/)).

## Remaining gaps worth doing next

1. `{name:path}` converters, since file-serving and proxy routes use them.
2. `Body(embed=True)` and several body parameters, which are common in
   FastAPI tutorials.
3. A native `FileResponse` that uses the static-file path (ranges and
   conditional requests).
4. Pydantic models as query parameters.
