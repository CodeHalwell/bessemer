# Application API

Bessemer currently provides a bounded Python compatibility API over its native
HTTP runtime. Python routes are **Tier 2**. Native health, prebuilt OpenAPI/docs
responses and native control rejections are **Tier 0**. The generated Rust
handler path, full FastAPI compatibility and the specification's performance
and platform gates remain unfinished; see [production readiness](production-readiness.md).

Build and install the wheel before running an application:

```sh
python -m pip install '.[pydantic]'
bessemer run examples.basic:app --workers 1
bessemer routes examples.basic:app
```

Pydantic v2 is optional. Install the extra when using its models, richer response
models or parameter regular expressions. The CLI imports `module:app`, builds
it before listening, then starts supervised worker processes. Each worker has
its own lifespan, state, dependency resources and rate quota. The server binds
to `127.0.0.1:8000` by default; explicitly choose your deployment interface.

## Routes and application lifetime

```python
from contextlib import asynccontextmanager
from bessemer import App, Router

@asynccontextmanager
async def lifespan(app):
    app.state.ready = True
    try:
        yield
    finally:
        app.state.ready = False

app = App(title="Inventory", version="1.0.0", lifespan=lifespan)
app.health("/healthz")
items = Router(prefix="/items", tags=["items"])

@items.get("/{item_id}", summary="Read an item", timeout=2)
async def read_item(item_id: int, include_deleted: bool = False):
    return {"id": item_id, "include_deleted": include_deleted}

app.include_router(items, prefix="/v1")
```

`get`, `post`, `put`, `patch`, `delete`, `head` and `options` accept the same
route options. Use `route(path, methods=[...])` for several methods. Options are
`status_code`, `timeout`, `dependencies`, `response_model`, `tags`,
`operation_id`, `summary`, `description` and `include_in_schema`. Router prefixes,
tags and dependencies compose when routers are included. Path parameters must
occupy a whole segment, such as `/items/{item_id}`. Duplicate or ambiguous
routes, unresolved annotations and unsupported signatures fail at startup.
Literal route paths/prefixes must be ASCII; declare a Unicode literal explicitly
with URI percent encoding, for example `/caf%C3%A9`. Unicode values in named
path parameters are decoded normally and remain supported.

`app.build()` freezes route and exception-handler registration; calling it again
is harmless. `app.openapi()` builds if needed and returns an independent copy.
Native route/settings exports are also copied. Application state remains mutable
and worker local. Share durable or cross-worker state through an external system.

Async handlers, dependencies and lifespan run on one persistent asyncio loop per
worker. Sync handlers/dependencies run in a bounded pool of eight threads; they
must not call non-thread-safe asyncio resources. Do not block the asyncio loop
with synchronous network, database or CPU work. Running sync work cannot be
forcibly cancelled safely by Python.

`app.health()` is a native process-health response, `{"ok": true}`. It does not
prove a database or external service is ready. Add an application readiness
route and explicitly exempt both probe paths from authentication/rate limits
when they should be public.

## Inputs

Unmarked scalar parameters bind to a same-named path placeholder when present,
and otherwise to the query string. Unmarked model/dict/list parameters bind to
the whole JSON body. An optional annotation permits null; a default makes the
input optional. `str | None` without a default is still required.

```python
from typing import Annotated
from bessemer import Body, Cookie, Header, Path, Query

@app.post("/items/{identifier}")
async def update(
    identifier: Annotated[int, Path(ge=1)],
    token: Annotated[str, Header(alias="X-Token", min_length=16)],
    body: Annotated[dict, Body()],
    page: Annotated[int, Query(alias="page-size", ge=1, le=100)] = 20,
    session: Annotated[str | None, Cookie(alias="sid")] = None,
):
    return {"id": identifier, "page": page}
```

Markers also work as parameter defaults, for example `page: int = Query(20,
ge=1)`. Declare metadata once, using either style. Supported metadata is
`alias`, `description`, `deprecated`, `ge`, `gt`, `le`, `lt`, `min_length`,
`max_length` and `pattern`. Constraints must match the input type. Patterns use
pydantic-core's non-backtracking Rust regex engine; unsupported expressions,
including lookarounds/backreferences, fail startup. Header names are case
insensitive. `Header()` changes underscores to hyphens by default;
`Header(convert_underscores=False)` retains them. Header and cookie aliases
must be valid ASCII tokens.

| Annotation | Input contract |
| --- | --- |
| `str`, `bool`, `int`, `float` | Native scalar extraction; integers preserve Python precision up to 4096 decimal digits and floats must be finite. |
| `UUID`, `date`, `datetime` | String extraction followed by a compiled Python converter. |
| Uniform scalar `Enum` or `Literal` | Native underlying scalar extraction followed by declared-value validation. |
| `dict`, `list`, `dict[str, Any]`, `list[Any]` | Raw JSON object/array with outer shape validation. |
| Pydantic v2 model | Whole JSON body with Pydantic validation. |
| Scalar with `Body()` | Strict JSON scalar type, then constraints/conversion; a JSON string does not stand in for an integer. |

One whole JSON body is supported per route, including its dependency graph.
Use a Pydantic model for typed container contents or several logical fields.
Multiple bodies, `Body(embed=True)`, arbitrary union types, foreign `Annotated`
metadata, multipart/form input and uploaded files are rejected or unsupported.
JSON body defaults must be `None` with a nullable annotation. Body input accepts
`application/json` and `application/*+json`; a declared body with a different
content type receives `415`. Body size limits apply to buffered Request bodies too.

## Dependencies

```python
from typing import Annotated
from bessemer import Depends, State

async def database(state: State):
    client = await open_database()  # Your client implementation.
    try:
        yield client
    finally:
        await client.close()

@app.get("/count")
async def count(client: Annotated[Database, Depends(database)]):
    return {"count": await client.count()}
```

Dependency signatures are inspected at build time. Dependencies can themselves
request supported inputs, framework values or other dependencies. Sync/async
functions, callable instances, classes with typed constructors, and sync/async
generator dependencies are supported. `Depends()` can infer a callable class
from the parameter annotation. Dependency cycles fail at startup.

The default scope is `request`. Repeated uses of the same callable in one route's
graph share one result. `Depends(call, use_cache=False)` performs a fresh call
for that binding. `dependencies=[Depends(check)]` on App, Router, inclusion or a
route executes a dependency without passing its return value to the handler.

`Depends(call, scope="app")` creates one cached resource **per worker process**,
after lifespan startup and before the worker is ready. It may request `State`
and other application dependencies, but cannot request headers/query/body,
`Request`, background tasks or request-scoped dependencies. App scope requires
caching. Application dependencies close before lifespan exits.

Request generator cleanup is LIFO. In the current compatibility bridge,
background tasks and dependency cleanup complete **before the response is sent**.
Timeout/disconnect cancellation retains the request capacity slot until actual
work and cleanup finish. This differs from the specification's intended
post-response cleanup path. Use bounded client timeouts and cooperative async
cleanup. Startup/shutdown failure prevents readiness or is reported to the
supervisor; do not rely on indefinite cleanup during forced termination.

## Request context and background work

Annotate parameters with `Request`, `State` or `BackgroundTasks` to inject them.
They are framework values, not public OpenAPI parameters, and cannot have input
markers/defaults. `State` supplies `app.state`; `request.state` is a separate
mutable object for that request.

```python
from bessemer import BackgroundTasks, Request

@app.post("/inspect")
async def inspect(request: Request, tasks: BackgroundTasks):
    value = await request.json()
    tasks.add_task(record_event, request.request_id)  # Your callable.
    return {"method": request.method, "value": value}
```

Request exposes `method`, raw URI `path`, decoded `path_params`, `query_string`,
`query_params`, `headers`, `cookies`, `client`, `scheme`, `url`, `request_id`,
`app` and `state`. `await request.body()` returns buffered bytes;
`await request.json()` parses strict finite JSON. Query mapping values use the
last repeated value. `request.headers.getlist(name)` exposes repeated headers;
scalar bound headers reject duplicates. Cookie binding and Request.cookies
share the native parsed view, including simple quoted values.
Malformed, backslash-escaped or duplicate cookie inputs receive `400`.

`bessemer.request.current_request` is a ContextVar available to handlers,
dependencies and background work, including offloaded sync calls. It is reset
when the request finishes. Background tasks can be sync or async, execute in
registration order while dependencies remain available, and are best effort;
they currently add response latency. They are not a durable queue or a safe
place to schedule work whose delivery must survive worker failure.

## Responses and errors

Ordinary Python JSON values use the route's status code and JSON content type.
`response_model=Model` or a supported return annotation compiles response
validation once. Pydantic response models filter undeclared fields and serialize
with output aliases. Invalid return values produce a generic `500`, rather than
returning partially valid/private data. `response_model=None` explicitly disables
inferred response-model validation. Without Pydantic, strict primitive/null and
raw container response annotations are supported; richer models fail startup.

```python
from bessemer import HTTPException, JSONResponse, PlainTextResponse, RedirectResponse

@app.get("/session")
async def session():
    response = JSONResponse({"ready": True}, headers={"X-Feature": "session"})
    response.set_cookie("sid", "opaque-token", secure=True, httponly=True,
                        samesite="strict")
    return response

@app.get("/missing")
async def missing():
    raise HTTPException(404, "Item not found", headers={"Cache-Control": "no-store"})
```

Available classes are `Response` for buffered bytes/strings, `JSONResponse`,
`PlainTextResponse`, `HTMLResponse` and `RedirectResponse`. An explicit Response
controls its status, headers and media type. Keep the route's declared status
aligned with explicit response status for accurate OpenAPI. RedirectResponse
defaults to `307` and percent-encodes its Location. `set_cookie` and
`delete_cookie` preserve separate Set-Cookie fields. SameSite=None requires
Secure; `__Secure-` and `__Host-` cookie rules are validated. Expiry datetimes
must be timezone aware.
Cookie values must contain RFC 6265 cookie octets: encode structured, Unicode
or whitespace-containing values before setting them, for example with base64url.

Application response headers are limited to 64 fields and 16 KiB total,
including repeated cookies. Header names must be ASCII tokens; values allow
printable ASCII and tabs. CR/LF and transport/framing headers such as
Content-Length, Transfer-Encoding and Connection are rejected. Rust manages
framing. Text response classes add UTF-8 charset. HEAD and statuses 204, 205 and
304 do not send a response body.

Input validation uses a `422` JSON `detail` list with location, error type and a
safe generic message. Raw inputs, validator context and exception text are not
included. Internal exceptions and response-validation errors return
`{"detail":"Internal Server Error"}` with `500`. Deliberate HTTPException detail
is application-provided public data; keep secrets out of it.

Register a custom Python error handler with:

```python
from bessemer import JSONResponse, RequestValidationError

@app.exception_handler(RequestValidationError)
async def invalid(request, error):
    return JSONResponse({"detail": "Check the submitted values"}, status_code=422)
```

Handlers receive `(request, exception)`, may be sync or async, and must return a
Response. Registration freezes at build. They handle failures that reach Python;
native framing, authentication, body-limit and primitive extraction failures
remain native and do not call a Python exception handler.

## OpenAPI and native controls

`App` serves a startup-built OpenAPI 3.1 document at `/openapi.json` by default.
`App(docs_url="/docs")` adds a bundled API reference; all HTML/CSS/JavaScript is
local, with no CDN dependencies. It is a readable reference, not Swagger UI or
an interactive API client. Set `openapi_url=None` to disable the HTTP schema
endpoint; docs require an enabled schema URL. The document can still be read
through `app.openapi()`. Schema/docs URLs must be literal unoccupied paths.

Native schema/docs routes support GET/HEAD without a Python call per request.
Nested/recursive Pydantic refs are registered once; input/output components have
separate names where serialization needs differ. Conflicting model definitions
fail startup. API-key security includes the public header name, not secret
values or environment variable names. Native health is hidden by default;
`app.health(include_in_schema=True)` includes it.

Configure `cors`, `allowed_hosts`, `api_key`, `rate_limit`, `trusted_proxies`,
`request_id`, `security_headers`, `access_log`, `metrics_path` and
`metrics_token_env` on App. See [native production controls](production-controls.md)
for exact policies. Health/docs are protected unless explicitly exempted.
With header-based API-key protection, public browser docs require explicit
schema/docs exemptions or another authenticated delivery arrangement.

## Runtime bounds and deployment example

| Setting | Default |
| --- | --- |
| Request body | 1 MiB |
| Buffered response body | 4 MiB |
| Admitted Python requests | 128 per worker |
| Connections | 1024 per worker |
| Request / graceful drain timeout | 30 seconds each |
| Request header deadline | 10 seconds |
| Request header count / bytes | 64 / 32 KiB |
| Request target | 8 KiB |
| Application response headers | 64 / 16 KiB |

Use CLI flags such as `--max-response-size`, `--max-in-flight`,
`--max-connections`, `--max-body-size`, `--max-headers`, `--max-header-size`,
`--max-request-target-size`, `--header-timeout`, `--request-timeout` and
`--graceful-timeout`. Supported runtime settings resolve CLI > `BESSEMER_*`
environment > `[tool.bessemer]` > defaults. Python response normalization is
bounded; user code and Pydantic validation/serialization may allocate before the
runtime rejects an oversized result. Avoid constructing unbounded responses.

[examples/production.py](../examples/production.py) provides API-key protection,
Host restrictions, per-client rate limits, public probes, a worker-scoped resource,
validated models and optional public docs. Its catalog is read-only sample data,
not a persistence layer:

```sh
python -m pip install '.[pydantic]'
export BESSEMER_EXAMPLE_API_KEY="$(python -c 'import secrets; print(secrets.token_urlsafe(32))')"
bessemer run examples.production:app --workers 2 --max-in-flight 128
```

Request `/items/1` with the `X-API-Key` header. Public `/healthz` and `/readyz`
need no key. Documentation is disabled by default; setting
`BESSEMER_EXAMPLE_DOCS=1` explicitly makes `/docs` and `/openapi.json` public.
Replace `api.example.com` and the sample catalog for your service. Configure a
trusted TLS ingress; forwarded addresses are ignored unless its source networks
are explicitly trusted. Metrics currently require one supervised worker.

Streaming/file responses, WebSockets, ASGI mounts, multipart uploads, arbitrary
Python middleware, distributed caches/limits, native I/O clients and a
free-threaded worker topology remain outside this API slice. Full production
qualification still requires representative deployment/failure/load testing,
platform verification and the remaining specification gates.
