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
Multiple bodies, `Body(embed=True)`, arbitrary union types and foreign
`Annotated` metadata are rejected or unsupported.
JSON body defaults must be `None` with a nullable annotation. Body input accepts
`application/json` and `application/*+json`; a declared body with a different
content type receives `415`. Body size limits apply to buffered Request bodies too.

### Forms and file uploads

```python
from typing import Annotated
from bessemer import File, Form, UploadFile

@app.post("/login")
async def login(username: Annotated[str, Form()], remember: bool = Form(False)):
    ...

@app.post("/upload")
async def upload(
    file: UploadFile,
    attachments: list[UploadFile] | None = None,
    checksum: bytes | None = File(None),
    note: str = Form(""),
):
    contents = await file.read()
```

The server decodes `application/x-www-form-urlencoded` and
`multipart/form-data` natively. The multipart parser is strict (RFC 7578), is
single pass and never copies part data until it hands a file to Python.
`Form()` fields are scalars with the same conversion and constraints as query
parameters; a repeated field keeps its last value. `UploadFile` (or
`File()`) binds a file part. `bytes` with `File()` binds its contents.
`list[UploadFile]` binds every part with that name, and a single-file
parameter that receives several parts returns `422`. An optional file
parameter needs `| None` and a `None` default. A browser's empty "no file
chosen" part counts as no file.

`UploadFile` has `filename`, `content_type`, `headers`, `size` and `file` (a
`BytesIO`), and async `read`, `seek`, `write` and `close`. The whole body is
already in memory and within `max_body_size`, so there is no spooling to disk;
raise `max_body_size` for routes that take large files. A request may have at
most 1000 parts with up to 8 KiB of headers each. Other content types receive
`415`, and malformed bodies receive `400`. Validation errors use
`["body", name]` locations, as in FastAPI. A route cannot mix a JSON body with
form fields or files. OpenAPI describes the fields as one
`application/x-www-form-urlencoded` or `multipart/form-data` request body.

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
safe generic message. Raw input values, validator context and arbitrary validator
messages are omitted. Locations retain field names, list indices and submitted
dictionary keys. Internal exceptions and response-validation errors return
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

### Streaming responses and server-sent events

Return `StreamingResponse(iterable, media_type=...)` to send a body
incrementally. The iterable can be async or sync and yields `bytes` or `str`.
`EventSourceResponse(iterable)` sends server-sent events: plain items are
encoded as `data:` lines (non-strings as JSON), and `format_event(...)` from
`bessemer.responses` builds events with `event`, `id`, `retry` or comments.

```python
from bessemer import EventSourceResponse, StreamingResponse

@app.get("/export")
async def export(db=Depends(session)):
    async def rows():
        async for row in db.stream("select ..."):
            yield row.to_csv_line()
    return StreamingResponse(rows(), media_type="text/csv")

@app.get("/ticks")
async def ticks():
    async def source():
        for n in range(10):
            yield {"n": n}
            await asyncio.sleep(1)
    return EventSourceResponse(source())
```

Rust pulls from the iterator through a bounded buffer (64 KiB or the response
limit, whichever is smaller). Chunks that are already available are batched
into one frame, and a chunk is never held back waiting for more. A slow client
applies backpressure all the way to the iterator. A 1 KiB-chunk stream reaches
about 190,000–300,000 chunks per second on one worker on the development
machine. The details:

- Generator dependencies and background tasks run after the last chunk, and the
  stream holds its request capacity slot until they finish.
- If the client disconnects, the iterator is closed and its finalizers and
  dependency cleanup still complete before the slot is released. Background
  tasks run only after a complete stream.
- HEAD requests never iterate the body but still run cleanup.
- Each chunk must arrive within the route timeout of the previous one and be no
  larger than `max_response_size`. Chunks are charged to the response byte
  budget, waiting for capacity instead of failing.
- An exception mid-stream is logged and the response is truncated (the chunked
  body has no terminator), so clients can detect the failure.

Because a stream holds a capacity slot for its whole life, size
`--max-in-flight` for long-lived event streams.

### Static files

`app.static(path, directory)` serves the files under a directory natively,
like FastAPI's `StaticFiles` mount, without calling Python:

```python
app.static("/assets", "static", cache_control="public, max-age=3600")
app.static("/", "frontend/dist", html=True)  # a single-page app's files
```

Requests resolve under the directory's real path. `..`, absolute paths,
dotfiles such as `.env`, and symlinks that point outside the directory all get
`404`. Responses carry a media type from the file extension, plus `ETag`,
`Last-Modified` and `Accept-Ranges: bytes`. `If-None-Match` and
`If-Modified-Since` answer `304`. A single `Range` gets `206`, an unsatisfiable
one gets `416`, and `If-Range` falls back to the whole file when the file has
changed. When the client accepts it, a precompressed `name.br` or `name.gz`
sibling is served with `Content-Encoding` and its own `ETag`, which conditional
requests are checked against. Ranges apply to the uncompressed file. Files stream from
disk in 64 KiB chunks, so large files use bounded memory.

With `html=True`, a directory, including the mount root, serves its
`index.html`. A directory URL without its trailing slash gets a `307` redirect
to the slashed URL, so relative links in the page resolve. `cache_control` sets a `Cache-Control` value for every file.
Mounts are GET (and HEAD) only and do not appear in OpenAPI. Application routes
keep priority, so a root mount can sit alongside an API. With
`App(compression=...)`, text files without a precompressed sibling are
compressed on the fly. Files up to 1 MiB are compressed whole, and larger ones
are streamed through the encoder.

## WebSockets

```python
from bessemer import App, WebSocket, WebSocketDisconnect

@app.websocket("/rooms/{room}")
async def room(websocket: WebSocket):
    await websocket.accept()
    async for message in websocket.iter_json():
        await websocket.send_json({"room": websocket.path_params["room"], **message})
```

The handshake, framing, masking, fragmentation, UTF-8 validation, ping/pong and
the close handshake are implemented in Rust (RFC 6455, no extensions). Python
only ever sees whole messages: `receive()` returns `str` or `bytes`, and
`receive_text`, `receive_bytes` and `receive_json` check the type. A peer close
or lost connection raises `WebSocketDisconnect(code)`. The `iter_*` helpers
stop quietly when that happens.

- **Accept and deny.** Call `await websocket.accept(subprotocol=None)` to
  complete the handshake. Calling `close()` first, raising `HTTPException`, or
  returning without accepting denies it with `403`. The handler must accept
  within the route timeout, otherwise the handshake gets a `504`.
- **Closing.** When the handler returns, the server closes with `1000`. An
  unhandled exception closes with `1011` and is logged. Worker shutdown sends
  `1001`. The peer has 5 seconds to answer a close before the socket is
  dropped.
- **Inputs and dependencies.** Handlers are `async def` and take a `WebSocket`
  parameter. Read path, query, header and cookie values from it
  (`websocket.path_params`, `websocket.query_params`, `websocket.headers`).
  `Depends`, `State`, `Request` and `Principal` work as for HTTP routes.
  Generator dependencies stay open for the whole session.
- **Limits.** Inbound messages larger than `max_body_size` close the session
  with `1009`. Sessions count against `max_connections`, not
  `max_in_flight`.
- **Flow control.** Up to 64 received messages wait for the handler before
  Rust stops reading the socket. `send_*` waits only while more than 1 MiB is
  queued for a slow client.

Rust delivers messages that are already readable in one batch per loop
wake-up. The writer combines queued frames into a single socket write. A
16-byte echo sustains about 260,000 messages per second each way on one worker
on the development machine.

Native controls apply to the handshake (host, CORS origin, rate limit, API key
and JWT). Browsers cannot set an `Authorization` header on WebSockets, so
authenticate browser clients some other way, such as a cookie checked in a
dependency. Without a CORS configuration the `Origin` header is not checked.
`permessage-deflate` compression and HTTP/2 WebSockets are not supported.

## Testing

`bessemer.testclient.TestClient` serves the application in-process on a
private loopback socket. It uses the same native server, controls and worker
as `App.run`, so tests see real routing, validation, limits, CORS, JWT and
host checks. Use it as a context manager so lifespan startup and shutdown run:

```python
from bessemer.testclient import TestClient

def test_read_item():
    app.dependency_overrides[get_database] = lambda: FakeDatabase()
    with TestClient(app) as client:
        response = client.get("/items/1", params={"q": "x"})
        assert response.status_code == 200
        assert response.json()["id"] == 1
    app.dependency_overrides.clear()
```

`get`, `post`, `put`, `patch`, `delete`, `head` and `options` take `params`,
`headers`, `cookies` and one body: `json=`, `data=` (form encoded),
`files=` (multipart, with optional `data=` fields) or `content=`. A file is
`content`, `(filename, content)` or `(filename, content, content_type)`; pass a
list of `(name, file)` pairs to send several under one name. Responses have `status_code`, `headers`, `content`, `text`,
`json()`, `cookies`, `history` and `raise_for_status()`. The client keeps
cookies across requests, scoped by path, domain and `Secure` as a browser
scopes them (a `Secure` cookie needs an `https://` base URL), and follows
redirects to its own host
(`follow_redirects=False` turns this off). `base_url` sets the Host header,
`http://testserver` by default; add that host to `allowed_hosts` if the app
restricts hosts. `server_options={"max_body_size": 4096}` passes `App.run`
limits. `with` returns once the native server accepts connections, and any
startup failure, including a limit the server rejects, is raised from `with`, and the client never installs
signal handlers, so a test run's Ctrl-C still works. One client may be shared
between threads.

`app.dependency_overrides` maps a dependency callable to its replacement, as
in FastAPI. A replacement is compiled like any dependency, so its own
parameters become route inputs. Overrides apply when the app next starts
serving (a test client or `run`) and never change a running server.
WebSocket test sessions are not provided yet; test WebSocket routes with a
WebSocket client against `TestClient`'s server or a running app.

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
| Retained request payloads in flight | 32 MiB per worker |
| Retained response payloads in flight | 32 MiB per worker |
| Admitted Python requests | 128 per worker |
| Connections | 1024 per worker |
| Request / graceful drain timeout | 30 seconds each |
| Request header deadline | 10 seconds |
| Request header count / bytes | 64 / 32 KiB |
| Normalized request target | 8 KiB |
| Application response headers | 64 / 16 KiB |

Use CLI flags such as `--max-response-size`, `--max-in-flight`,
`--max-request-bytes-in-flight`, `--max-response-bytes-in-flight`,
`--max-connections`, `--max-body-size`, `--max-headers`, `--max-header-size`,
`--max-request-target-size`, `--header-timeout`, `--request-timeout` and
`--graceful-timeout`. Supported runtime settings resolve CLI > `BESSEMER_*`
environment > `[tool.bessemer]` > defaults. Python response normalization is
bounded; user code and Pydantic validation/serialization may allocate before the
runtime rejects an oversized result. Avoid constructing unbounded responses.
The request-target limit measures the normalized `http::Uri`, whose parser may
remove fragments; the complete raw request line/header block remains bounded by
the header-buffer limit.
Native integer ranges are checked before the CLI imports application code and
before direct `App.run()` starts lifespan: body size up to 1 GiB, response size
up to 64 MiB, connections up to 100,000, header count up to 1024, header buffer
8192–1,048,576 bytes and normalized target up to 1 MiB. These sizes/counts must
be positive integers; booleans are rejected.

`max_request_bytes_in_flight` and `max_response_bytes_in_flight` are aggregate
logical payload budgets, separate from each request's body/response size limit.
Each defaults to 33,554,432 bytes and must be a positive integer no greater than
1,073,741,824 bytes (1 GiB). The request budget must be at least `max_body_size`;
the response budget must be at least `max_response_size`. Raising an individual
size limit above its aggregate budget requires raising that budget too.

Set them through `App.run(...)`, the CLI flags above,
`BESSEMER_MAX_REQUEST_BYTES_IN_FLIGHT` /
`BESSEMER_MAX_RESPONSE_BYTES_IN_FLIGHT`, or matching `[tool.bessemer]` keys.
For example:

```sh
bessemer run examples.basic:app --workers 2 \
  --max-request-bytes-in-flight 33554432 \
  --max-response-bytes-in-flight 33554432
```

Request accounting charges actual retained incoming body chunks and keeps those
reservations through Python completion, including timeout/disconnect cleanup.
Response reservations follow serialized body ownership while the transport
writes or drops the bytes. Concurrent native OpenAPI/docs responses each consume
their full payload length, even though their startup data is shared. Small
native health/error responses are exempt; metrics uses its separate 4 MiB,
single-scrape guard.
Metrics duration and the in-flight gauge stop at response readiness and exclude
socket drain. Response byte reservations can remain occupied during that drain,
after the metrics in-flight gauge has decreased.

Exhausting either aggregate budget returns `503` with `Retry-After: 1`.
Request-budget refusal closes the connection because an upload may remain unread;
response-budget refusal returns a small native error after the handler and its
cleanup have completed. The individual request body limit still returns `413`.
Admission refusal also returns `503` with `Retry-After: 1` and closes the
connection before reading a declared upload. A `504` closes the connection when
the request body is incomplete; after a fully consumed body, a handler timeout
preserves an otherwise reusable connection.

The processing deadline covers body reading, validation, submission, handler
work, before-send cleanup and encoding up to response readiness. It excludes
final socket drain/slow-reader ownership. Synchronous GIL-held work cannot be
preempted; cancellation cleanup can continue after the timeout response while
holding request capacity and input-byte reservations. The deadline is therefore
not a hard bound on process/resource lifetime; the supervisor enforces its own
shutdown/worker replacement policy.

These budgets bound logical retained payloads, not total process heap. Startup
schema storage, Python request/model objects, application allocations, temporary
serialization buffers, headers, transport metadata and kernel/socket buffers
remain outside this accounting. Slow readers and cancellation cleanup can keep
reservations occupied. Budgets are worker local; overlapping replacements and
multiple replicas have independent budgets. Keep container/process memory limits
and bounded application/client behavior alongside these settings.

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

File responses, ASGI mounts, multipart uploads, arbitrary
Python middleware, distributed caches/limits, native I/O clients and a
free-threaded worker topology remain outside this API slice. Full production
qualification still requires representative deployment/failure/load testing,
platform verification and the remaining specification gates.
