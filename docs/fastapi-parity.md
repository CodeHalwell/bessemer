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
from bessemer import FastAPI, APIRouter, Depends, HTTPException, Query, status
from bessemer.responses import HTMLResponse
from bessemer.middleware import CORSMiddleware

app = FastAPI(title="Shop")
```

The application class is called `App`; `FastAPI` is an alias for it, so the
example keeps every FastAPI name. Where FastAPI spreads names across
`fastapi.middleware.cors`, `fastapi.middleware.gzip` and so on, Bessemer keeps
them in one module, `bessemer.middleware`.

| FastAPI import | Bessemer import |
| --- | --- |
| `fastapi` (top-level names) | `bessemer` |
| `fastapi.FastAPI` | `bessemer.FastAPI` (an alias of `bessemer.App`) |
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
| `fastapi.websockets` | `bessemer.websockets` |
| `fastapi.middleware.*` | `bessemer.middleware` (and `bessemer.middleware.{cors,gzip,trustedhost,httpsredirect}`) |
| `fastapi.exception_handlers` | `bessemer.exception_handlers` |
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
| `default_response_class`, `responses`, `deprecated`, `include_in_schema`, `generate_unique_id_function`, `redirect_slashes`, `root_path`, `root_path_in_servers` | Added | Same precedence as FastAPI: route, then router, then inclusion, then app. `root_path` sets `request.scope["root_path"]`, `request.base_url`, `request.url_for`, the docs page OpenAPI URL, and (unless `root_path_in_servers=False`) the first OpenAPI `servers` entry. |
| `redoc_url` | Differs | Only `None` is accepted. There is no ReDoc page. |
| `debug` | Differs | Only `False`. Errors are logged by the worker, never sent to clients. |
| `strict_content_type` | Same | On `App` only. True by default: a JSON body sent without a `Content-Type` is refused. Bessemer answers `415`, as it does for a wrong type; FastAPI passes the raw bytes to validation and answers `422`. |
| `routes=`, `webhooks`, `callbacks`, `swagger_ui_*`, `separate_input_output_schemas` | No | |
| `app.get/post/put/patch/delete/head/options`, `app.api_route`, `app.add_api_route` | Same/Added | `api_route` and `add_api_route` added. `trace` is not supported. |
| `app.websocket`, `app.add_api_websocket_route` | Same/Added | |
| `app.include_router(...)` | Same | Now also takes `default_response_class`, `responses`, `deprecated`, `include_in_schema`, `generate_unique_id_function`. Including another `App` (or its `.router`) takes its routes and lifespan only, as in FastAPI, but raises `TypeError` when that app has exception handlers, middleware, mounts, dependency overrides or server options, which FastAPI drops silently; use `mount` for those. |
| `app.mount(path, app, name=)` | Same/Added | Takes a Bessemer `App`, `StaticFiles` or any ASGI app (Starlette, a FastAPI app, Django, a plain ASGI callable). See [mounted ASGI apps](#mounted-asgi-apps). |
| `app.exception_handler(...)`, `app.add_exception_handler(...)` | Same/Added | Status-code keys (`@app.exception_handler(404)`, or an `http.HTTPStatus` member) added. A handler for `Exception` or `500` is the catch-all for unhandled exceptions; like one for `ValueError`, it does not replace the built-in handling of `HTTPException` and `RequestValidationError`. A handler may return a `StreamingResponse`. |
| `app.middleware("http")` | Same | |
| `app.add_middleware(cls, **options)` | Added | Native, `BaseHTTPMiddleware` and standard ASGI middleware. See [middleware](#middleware). |
| `app.routes` | Differs | Read-only `APIRoute` descriptions. Routes compile once, so setting `route.operation_id` raises; use `generate_unique_id_function`. |
| `app.url_path_for`, `app.dependency_overrides`, `app.state`, `app.openapi()` | Same | Replacing `app.openapi` with a function does not change the served document. |
| `app.router` | Added | Returns `self` (`App` is a `Router` subclass), exposing `routes`, `lifespan_context`, `on_startup`, `on_shutdown`, `dependencies`, `redirect_slashes`, `url_path_for` and `include_router`. |
| Lifespan yielding a state dict | Added | Each request's `request.state` (and `websocket.state`) starts as a shallow copy of the yielded mapping, as in Starlette. Mounted apps see the outermost app's state. Yielding anything but a mapping or `None` fails startup. |

## Routers and route options (`APIRouter`)

`APIRouter` is `bessemer.Router` under FastAPI's name.

| Feature | Status | Notes |
| --- | --- | --- |
| `prefix`, `tags`, `dependencies` | Same | |
| `default_response_class`, `responses`, `deprecated`, `include_in_schema`, `generate_unique_id_function`, `on_startup`, `on_shutdown`, `redirect_slashes`, `lifespan` | Added | A router's `lifespan` (and `on_startup`/`on_shutdown` handlers) joins the app's when it is included, preserving FastAPI's entry/exit order and state merging. |
| `routes=`, `route_class`, `callbacks` | No | `router.routes` is a read-only property of registered route descriptions. |
| Route `status_code`, `dependencies`, `response_model`, `response_model_*`, `tags`, `summary`, `description`, `operation_id`, `include_in_schema`, `name` | Same | `tags` accepts `str` and `Enum` values. `response_model_include`/`response_model_exclude` require a `set` or `dict` (`list` raises `TypeError`). |
| Route `response_class` | Added | `JSONResponse`, `ORJSONResponse` and `UJSONResponse` keep the native JSON encoder. Other classes wrap return values that are not already a `Response`, as FastAPI does. |
| Route `responses`, `deprecated`, `response_description`, `openapi_extra`, `generate_unique_id_function` | Added | `responses` accepts `{"model": Model}` entries. |
| Route `callbacks` | No | |
| Route `timeout` | Bessemer only | A per-route deadline. |
| `{name:path}`, `{name:int}`, `{name:float}`, `{name:uuid}`, `{name:str}` path converters | Added | A value that does not match `int`, `float` or `uuid` is a 404, checked natively after the route lookup. The handler's annotation decides the value's type, as in FastAPI. `{name:path}` must be the last segment and matches slashes and an empty remainder (`/files/`). Unlike Starlette, routes do not fall through: `/items/{id:int}` and `/items/{name}` together fail at startup. The OpenAPI document writes plain `{name}`. Custom converters (`register_url_convertor`) are not supported. |
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
| `default_factory`, `multiple_of`, `strict`, `discriminator`, `validation_alias`, `serialization_alias` | Added | Supported on `Parameter` markers and `pydantic.Field(...)`. `validation_alias` must be a `str` on parameters. |
| `max_digits`, `decimal_places`, `allow_inf_nan`, `alias_priority`, `union_mode`, `fail_fast`, `validate_default` | No | Passing one raises `TypeError` on markers or `ValueError` on `Field(...)`. |
| `Body(embed=True)`, several body parameters | Added | Embedded parameters share one generated `Body_<operationId>` model, documented as FastAPI does, with errors at `["body", "<name>", ...]`. Bodies with exact field types still take the native model path; constraints on an embedded field send that route through Pydantic. Embedded scalars are coerced as in FastAPI. Requires Pydantic. |
| `Annotated[..., pydantic.Field(...)]` on a parameter | Added | Converted to the matching `Path`, `Query` or `Body` marker. |
| `Annotated[..., AfterValidator/BeforeValidator/PlainValidator/WrapValidator]` | Added | Validated through Pydantic's `TypeAdapter` on scalar, sequence and model inputs. |
| `Query()` and `Header()` sequence parameters (`list[str]`, `list[int]`, `list`, `set[str]`, `tuple[int, ...]`) | Added | Reads repeated query/header values via `getlist()`, validates items and `min_length`/`max_length` constraints, and documents an OpenAPI `array` parameter. Bare `list[str]` without `Query()`/`Header()` remains a JSON body input; use `Query()` or `Header()` for query/header sequences. |
| Pydantic model parameters (`Annotated[Model, Query()]`, `Header()`, `Cookie()`, `Form()`) | Added | Extracts scalar and sequence fields from `query_params`, `headers` (honouring `convert_underscores`), `cookies` or `await request.form()`, supports optional `Query(default=None)` models and `model_config = {"extra": "forbid"}`, and documents fields in OpenAPI. |
| `list[Model]` and `dict[str, Model]` JSON body containers | Added | Validated via Pydantic `TypeAdapter` and documented in OpenAPI. Non-model typed containers such as `dict[str, int]` still fail loudly with `ValueError`. |
| `datetime.time`, `datetime.timedelta`, `decimal.Decimal` scalar inputs | Added | Validated and serialized (`timedelta` as total seconds, `time` as ISO 8601, `Decimal` as string/number) in parameters, bodies and `jsonable()`. |
| `response: Response` parameter injection | Added | Handlers and dependencies declaring `response: Response` receive an injectable `Response` whose headers, cookies and explicit `status_code` merge into the final response. |

## Dependencies

| Feature | Status | Notes |
| --- | --- | --- |
| `Depends(dependency, use_cache=)`, sub-dependencies, classes, `yield` dependencies, `dependencies=[...]`, `dependency_overrides` | Same | |
| `Depends(scope=None or "function")` | Added | Both mean Bessemer's request scope. |
| `Depends(dependency)` written with a bound method | Same | The callable itself identifies a dependency, so `Depends(db.session)` written in several places is one dependency per request (and one `scope="app"` resource), while methods of different instances stay separate. An unhashable callable is identified by the object. |
| `Depends(scope="request")` | Differs | Cleanup runs before the response is sent, which is FastAPI's `"function"` timing, unless background tasks are queued or the response streams. With queued tasks it runs after the response has been delivered and the tasks have run, and a cleanup failure is logged without changing the response. A stream's cleanup runs after its last chunk, where a failure is logged and truncates the body. |
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
| `scope`, `session`, `auth`, `user` | Added | `scope` is the ASGI scope as ASGI middleware passed it on, or one built from the request on first use. `session` needs `SessionMiddleware`, and `auth`/`user` need `AuthenticationMiddleware`, as in Starlette. |
| `state` | Added | Now created on first use, seeded from the lifespan state. |
| `is_disconnected()`, `receive` | Added | `receive()` yields `{"type": "http.request", "body": ..., "more_body": False}` then waits for the native disconnect signal to return `{"type": "http.disconnect"}`. `is_disconnected()` reads the native invocation's `disconnected` flag (`True` once the client disconnects) while deadline, shutdown and lane cancellations still propagate as `CancelledError`. |
| `send`, `send_push_promise` | No | |

## Responses

| Feature | Status | Notes |
| --- | --- | --- |
| `Response`, `JSONResponse`, `HTMLResponse`, `PlainTextResponse`, `RedirectResponse`, `StreamingResponse` | Same | `status_code` takes an `int` or an `http.HTTPStatus` member. `Headers(other_headers)` copies repeated fields. |
| `JSONResponse.render()`, `JSONResponse.body` | Differs | A subclass's `render(content) -> bytes` override is what is sent, from `response_class=` or a returned instance. `body` is encoded from `content` each time it is read and cannot be assigned. The built-in classes are encoded natively without calling `render`, and `render` on a class that is not a `JSONResponse` is not consulted. |
| `background=` on every response | Added | Runs with the request's background tasks. |
| `JSONResponse(media_type=)` | Added | |
| `set_cookie(partitioned=)`, `delete_cookie(partitioned=)` | Added | Partitioned cookies must be `Secure`. |
| `FileResponse` | Added | Served natively on the static-file path in 64 KiB chunks with `Content-Disposition`, `ETag`, `Last-Modified`, `Accept-Ranges: bytes`, conditional requests (`If-None-Match`, `If-Modified-Since` -> `304`) and single byte ranges (`Range`, `If-Range` -> `206` / `416`). |
| `ORJSONResponse`, `UJSONResponse` | Added | Names only: Bessemer already encodes JSON natively in Rust, with no extra package. |
| `EventSourceResponse` | Bessemer only | Server-sent events. |
| Return a value, return a `Response` directly, `response_model` | Same | |

## Exceptions

| Feature | Status | Notes |
| --- | --- | --- |
| `HTTPException(status_code, detail, headers)` | Same | `detail` now defaults to the status phrase, as in FastAPI. `status_code` takes an `int` or an `http.HTTPStatus` member. `@app.exception_handler(StarletteHTTPException)` also handles `bessemer.HTTPException`. |
| Logging of unhandled exceptions | Differs | Only an error that no handler for its class answered is logged, once, at `ERROR`. The traceback lists each frame's file, line and function and the exception class names (chained causes included) but no exception messages and no source lines, and `record.exc_info` is not set, so a secret or request data in a message does not reach the log. |
| `RequestValidationError` | Same | `errors()` and `.body` added. Entries carry `loc`, `type` and a generic `msg`. The submitted `input` is deliberately not echoed back. |
| `WebSocketException(code, reason)`, `WebSocketRequestValidationError`, `FastAPIError` | Added | |
| `ResponseValidationError` | Added | Raised with `errors()` (`loc` starting at `["response"]`) and `body` when `response_model` or return-annotation validation fails; answers `500` unless handled by `@app.exception_handler(ResponseValidationError)` or the catch-all handler. A `ValueError` handler does not take it. |
| `bessemer.exception_handlers` (`http_exception_handler`, `request_validation_exception_handler`, `websocket_request_validation_exception_handler`) | Added | Matches `fastapi.exception_handlers` for custom exception handlers that delegate back to the default formatter. |

## Middleware

`app.add_middleware(cls, **options)` and `App(middleware=[Middleware(cls, ...)])`
accept these classes from `bessemer.middleware` (and submodules `bessemer.middleware.{cors,gzip,trustedhost,httpsredirect}`).

| Class | Status | Notes |
| --- | --- | --- |
| `CORSMiddleware` | Added | Configures native CORS, which runs in Rust. A wildcard origin cannot be combined with credentials; `allow_origin_regex` and `allow_private_network` are not supported. |
| `GZipMiddleware` | Added | Configures native gzip. `minimum_size` is honoured; `compresslevel` is fixed. `App(compression=...)` adds Brotli. |
| `TrustedHostMiddleware` | Added | Configures native host checks. The `www.` redirect is not performed. |
| `HTTPSRedirectMiddleware` | Differs | Redirects `http` requests for Python routes to `https` with `307 Temporary Redirect`. It runs as HTTP middleware, so requests that never enter HTTP middleware are not redirected: WebSocket handshakes, static files, the OpenAPI document and docs page, health and metrics. Redirect those at the proxy or load balancer. |
| `BaseHTTPMiddleware` subclasses | Added | `dispatch(request, call_next)` runs as `@app.middleware("http")`. As in Starlette, `call_next` returns the response of an error that an exception handler answered and raises an error that none did; see [HTTP middleware](application-api.md#http-middleware) for the differences. |
| Any other ASGI middleware (`SessionMiddleware`, `AuthenticationMiddleware`, Starlette's own `GZipMiddleware`, third-party classes) | Added | See [ASGI middleware](#asgi-middleware). |
| `Middleware` from `starlette.middleware` / `fastapi.middleware` in `App(middleware=[...])` | Added | Accepted as well as Bessemer's. |

### ASGI middleware

Any class that is not one of the above is treated as standard ASGI
middleware and instantiated once per worker as `cls(app, *args, **kwargs)`,
the last added outermost, as in Starlette. It wraps every Python HTTP route,
and differs from Starlette in these ways:

- It runs outside all `@app.middleware("http")` and `BaseHTTPMiddleware`
  middleware, whatever order they were added in.
- The native router has already matched the route and parsed its
  parameters, so a middleware that rewrites `scope["path"]`, headers or the
  body does not change which route runs or what it receives. What it adds to
  the scope (`session`, `user`, `auth`, `state`) reaches the route through
  `request.scope`, `request.session` and so on.
- The request body arrives as one `http.request` message. A multipart body
  spooled to disk for `File()` parameters is not passed through it.
- Native routes (static files, health, metrics, the OpenAPI documents) and
  WebSockets do not pass through it.
- A route error that no exception handler answered is raised out of the
  application it wraps, as from a Starlette app. There is no
  `ServerErrorMiddleware` in the stack: if no middleware answers the error,
  the catch-all handler (for `Exception` or `500`) or the built-in `500`
  does, after the outermost middleware has seen it. A middleware that sends
  a response and then re-raises the error, as Starlette's own
  `ServerErrorMiddleware` does, has failed as far as the runtime can tell:
  its response is discarded and the error is answered the same way. A
  middleware that answers an error should not re-raise it.
- It can only be added to the outermost app, not to a mounted `App`.
- It moves every Python route of the app off the native fast path, since
  each request becomes an ASGI call. An app without ASGI middleware is
  unaffected. Prefer the native `CORSMiddleware`, `GZipMiddleware` and
  `TrustedHostMiddleware` from `bessemer.middleware` where they fit.

### Mounted ASGI apps

`app.mount("/legacy", asgi_app, name="legacy")` serves every request under
`/legacy` (any method but `TRACE`) through the ASGI app, with `path` the full
path and `root_path` the prefix, as Starlette's `Mount` sets them. The app's
HTTP and ASGI middleware wrap it, and `url_path_for("legacy", path="/x")`
builds its URLs. A response sent in several body messages is streamed to the
client. As in Starlette, the mounted app's own lifespan does not run; give
its startup work to the outermost app. Requests to the mount enter Python,
so it is a compatibility path rather than a fast one.

## WebSockets

| Feature | Status | Notes |
| --- | --- | --- |
| `accept(subprotocol=)`, `send_text/bytes/json`, `receive_text/bytes/json`, `iter_*`, `close`, `WebSocketDisconnect` | Same | `receive_json(mode=)` accepts `"text"` or `"binary"`, matching `send_json(mode=)`. |
| `url`, `base_url`, `app`, `state`, `scope`, `accept(headers=)`, `send(message)`, `client_state`, `application_state`, `WebSocketState` | Added | `accept(headers=)` writes extra handshake headers natively. `send(message)` accepts ASGI `websocket.accept`, `websocket.send` and `websocket.close` dicts. `WebSocketState` is in `bessemer.websockets`. |
| Raw `receive()` ASGI dict | Differs | `receive()` returns the message's `str` or `bytes` payload directly. |

## Other modules

| Feature | Status | Notes |
| --- | --- | --- |
| `BackgroundTasks.add_task` | Same | Runs after response delivery, in registration order. Sync tasks use the bounded pool; errors are logged. Admission remains held through tasks and dependency cleanup. |
| `bessemer.background.BackgroundTask` | Added | |
| `bessemer.security`: `HTTPBearer`, `HTTPBasic`, `OAuth2PasswordBearer`, `OAuth2AuthorizationCodeBearer`, `APIKeyHeader/Query/Cookie`, `OAuth2PasswordRequestForm(Strict)` | Same | |
| `HTTPDigest`, `OAuth2`, `OpenIdConnect`, `SecurityBase` | Added | |
| `UploadFile` | Same | Constructor arguments differ; it is normally built by the framework. |
| `status` | Added | Every Starlette name, plus older names such as `HTTP_422_UNPROCESSABLE_ENTITY`. |
| `jsonable_encoder` | Added | Pydantic models, dataclasses, enums, dates, UUIDs, decimals, paths and containers, with the same options. |
| `run_in_threadpool`, `iterate_in_threadpool` | Added | |
| `StaticFiles` | Added | Mounting it calls `app.static`. `packages` and `follow_symlink` are not supported. |
| `TestClient` | Differs | Runs the real server in a thread rather than calling an ASGI app. Server exceptions become `500` responses instead of being raised in the test (no `raise_server_exceptions`). `client.cookies.set(name, value)` stores a cookie for the client's host and every path; another `domain=` or `path=` raises `ValueError`. |

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
- Path converters are compiled into the route table; a route without them
  pays one emptiness check after the lookup.
- Embedded body parameters add one `is None` check to argument validation
  for routes that validate inputs.

Measured with the `benchmarks/compare` harness, main and this change were
within run-to-run noise on every route, with unchanged p99 latency
([results](../benchmarks/results/2026-11-05/fastapi-parity/)).
Adding ASGI middleware, ASGI mounts and lifespan state was checked the same
way and was also within noise
([results](../benchmarks/results/2026-11-05/asgi-interop/)), as were path
converters, embedded bodies and the `FastAPI` alias
([results](../benchmarks/results/2026-11-05/fastapi-gaps/)).

## Remaining gaps kept failing loudly

1. `separate_input_output_schemas` on `App`, `webhooks` and `callbacks` in OpenAPI, and `swagger_ui_*` options (`TypeError`).
2. Unannotated route/dependency parameters, non-model typed JSON containers (`dict[str, int]`), bare `list[str]` query parameters without `Query()`, and `yield` route handlers (`ValueError` at `app.build()`).
3. WebSocket handler parameter injection for path/query/header/cookie inputs (`ValueError`; read from the `WebSocket` object or dependencies instead).
4. `response_model_include`/`response_model_exclude` passed as a `list` instead of `set` or `dict` (`TypeError`), and `GZipMiddleware(compresslevel=...)` (`ValueError`).
