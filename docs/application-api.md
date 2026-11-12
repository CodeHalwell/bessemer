# Application API

Bessemer currently provides a bounded Python compatibility API over its native
HTTP runtime. Python routes are **Tier 2**. Native health, prebuilt OpenAPI/docs
responses and native control rejections are **Tier 0**. The generated Rust
handler path, full FastAPI compatibility and the specification's performance
and platform gates remain unfinished; see [production readiness](production-readiness.md).

Coming from FastAPI? The [parity assessment](fastapi-parity.md) maps each
FastAPI import and option to Bessemer's, and lists what is not supported.

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

`app.run()` serves from the calling process, which suits development. Pass
`workers` to use several processes from a script, as `bessemer run` does:
`app.run(workers=4)`, or `workers=0` for one per usable CPU. Keep the call
under `if __name__ == "__main__":`, because each worker imports the script
again; [process supervision](supervision.md#from-a-script) has the details.

For synchronous CPU work inside an async handler, use `await app.run_cpu(...)`.
The worker owns a bounded process pool on GIL builds or thread pool on an
actually GIL-disabled interpreter. [CPU offload](cpu-and-gpu.md) covers custom
executors, queue limits, timeout cleanup and synchronous handler thread sizing.

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
`operation_id`, `summary`, `description`, `include_in_schema`, `name` and the
`response_model_*` options below. Router prefixes,
tags and dependencies compose when routers are included. Path parameters must
occupy a whole segment, such as `/items/{item_id}`. Duplicate or ambiguous
routes, unresolved annotations and unsupported signatures fail at startup.
Literal route paths/prefixes must be ASCII; declare a Unicode literal explicitly
with URI percent encoding, for example `/caf%C3%A9`. Unicode values in named
path parameters are decoded normally and remain supported.

`include_router` takes a router's routes and lifespan. Given another `App`
(or its `.router`, which is the app itself) it takes the same and nothing
else, so it raises `TypeError` when that app has exception handlers,
middleware, mounted apps, dependency overrides or server options, which would
otherwise be dropped without notice. Serve such an app with
[`mount`](#mounting-applications), which keeps them.

### Route names and URLs

Each route is named after its handler unless `name=` is given, as in FastAPI.
`app.url_path_for(name, **params)` builds its path, and `request.url_for(...)`
(or `websocket.url_for(...)`) adds the scheme and host. The scheme follows the
destination: `ws`/`wss` for a WebSocket route, `http`/`https` otherwise, secure
when the calling request is. Values are converted
with `str` and percent-encoded, so `/` inside a value becomes `%2F`. A static
mount takes a name too, and its file as `path`:

```python
app.static("/assets", "static", name="assets")

@app.get("/")
async def home(request: Request):
    return {
        "item": app.url_path_for("read_item", item_id=3),  # /v1/items/3
        "css": request.url_for("assets", path="css/site.css"),
    }
```

A name with no route, or with different parameters, raises `NoMatchFound`.
Routers resolve the routes they hold, with their prefixes applied.

### Mounting applications

`app.mount(prefix, other_app, name=...)` serves another `App` under a literal
prefix, like FastAPI's `app.mount` for sub-applications:

```python
admin = App(title="Admin", docs_url="/docs")

@admin.get("/users/{user_id}", name="user")
async def user(user_id: int, request: Request):
    return {"id": user_id, "self": request.url_for("user", user_id=user_id)}

app.mount("/admin", admin, name="admin")
app.url_path_for("admin:user", user_id=1)  # /admin/users/1
```

The mounted app keeps its own exception handlers, `dependency_overrides`,
`state` (`request.app` is the mounted app), app-level dependencies and its
own instances of `scope="app"` dependencies; the
outer app's dependencies and overrides do not reach its routes. It documents
itself: its OpenAPI document (and `docs_url` page) is served under the prefix
with the prefix as its server URL, and its routes are left out of the outer
document. The outer app's HTTP middleware wraps the mounted routes, outside
the mounted app's own. `request.url_for` inside a mounted app includes the
prefix; `admin.url_path_for(...)` on the app itself does not. Mounts nest,
and `url_path_for("admin:reports:daily")` follows them.

Routing stays a single native table: a mounted route that collides with an
outer one fails at startup like any duplicate route, and an outer route under
the prefix is still served. Server-wide settings (CORS, security headers,
request IDs, JWT, API keys, rate limits, metrics, compression and the rest of
the `App(...)` options that configure the server) and `lifespan` belong on
the outermost app, so mounting an app that sets them fails rather than
ignoring them. An app can be mounted once, and not at `/`. Any other ASGI app
(Starlette, Django, a plain ASGI callable) can be mounted too; every request
under its prefix then enters Python as an ASGI call
([details](fastapi-parity.md#mounted-asgi-apps)).

### Response model options

With Pydantic installed, `response_model_include`, `response_model_exclude`,
`response_model_by_alias`, `response_model_exclude_unset`,
`response_model_exclude_defaults` and `response_model_exclude_none` shape the
validated response as they do in FastAPI. They do not change the OpenAPI
schema. They need a response model (`response_model=` or a return
annotation), and Pydantic; otherwise startup fails instead of ignoring them. Routes that leave them at their defaults keep the fast path.

`app.build()` freezes route and exception-handler registration; calling it again
is harmless. `app.openapi()` builds if needed and returns an independent copy.
Native route/settings exports are also copied. Application state remains mutable
and worker local. Share durable or cross-worker state through an external system.

Async handlers, dependencies and lifespan run on one persistent asyncio loop per
worker. Sync handlers/dependencies share a bounded pool. The automatic GIL
limit starts at eight, with CPU-gated growth to sixteen on Linux/macOS;
explicit `sync_workers` fixes the ceiling. They must not call non-thread-safe
asyncio resources. Do not block the asyncio loop with synchronous network,
database or CPU work. Running sync work cannot be
forcibly cancelled safely by Python.

### Free-threaded Python

Bessemer supports free-threaded CPython 3.14t, and the extension declares it
safe to run without the GIL, so importing it does not turn the GIL back on.
There the sync handler threads run Python in parallel inside one worker
process: a CPU-bound sync route on three cores served 3.2 times the requests
it served on CPython 3.13 with the same single worker
([evidence](../benchmarks/results/2026-11-05/free-threading/)).

Async handlers can do the same with several event loops in one worker:
`app.run(loops=3)`, `bessemer run --loops 3` or `BESSEMER_LOOPS=3`. Each loop
runs in its own thread with its own native request queue, and each request
goes to the loop with the fewest requests in progress. On three cores a
Pydantic body route served 1.8 times the requests with three loops as with one,
about 90% of three worker processes, using 66 MiB instead of 179 MiB
([evidence](../benchmarks/results/2026-11-05/event-loops/)). On builds with the
GIL extra loops add no throughput, so the default stays at one.

With `loops` above 1, lifespan, application-scoped dependencies and WebSocket
sessions stay on the first loop, while HTTP handlers run on any loop. Do not
hand handlers objects bound to one event loop, such as an `asyncio.Lock`
created in lifespan or a client session or connection pool that remembers its
loop; create those per loop or use thread-safe equivalents. A stalled loop
withholds the worker heartbeat, so `--worker-timeout` still replaces the
worker.

### Bessemer's event loop

`event_loop` chooses the loop a worker runs. It takes three values, set with
`app.run(event_loop=...)`, `bessemer run --event-loop`, `BESSEMER_EVENT_LOOP`
or `event_loop` under `[tool.bessemer]`:

| Value | Loop |
| --- | --- |
| `"auto"` (default) | Bessemer's loop on Linux and macOS when the native loop (`bessemer._loop`) can be imported and `loops` is 1; asyncio's loop otherwise. No warning is logged when it picks asyncio. |
| `"bessemer"` | Bessemer's loop. Off Linux and macOS the worker logs a warning and runs asyncio. |
| `"asyncio"` | asyncio's own loop on every platform. |

Bessemer's loop has its scheduler and I/O reactor written in Rust (on epoll
and kqueue respectively). It is a standard asyncio loop to your code, so
`asyncio.get_running_loop()`, tasks, timers, streams, subprocesses and
executors behave as before. Its class is a subclass of asyncio's
`_UnixSelectorEventLoop`, so `isinstance` checks against asyncio's loop
classes pass. The handles that `call_soon`, `call_later` and `call_at`
return are Bessemer's own native type (`bessemer._loop.Handle`), not
subclasses of `asyncio.Handle` or `asyncio.TimerHandle`, because a native type
cannot inherit from those Python classes; uvloop makes the same trade. They
have the same methods (`cancel`, `cancelled`, `when`, `get_context`), so only
code that checks `isinstance(handle, asyncio.TimerHandle)` notices.
`bessemer run` logs the loop it resolved on the `listening` line.

Before this default, workers ran asyncio's loop unless you asked for
Bessemer's. To keep that behaviour, set `event_loop="asyncio"` (or
`--event-loop asyncio`, `BESSEMER_EVENT_LOOP=asyncio`, or
`event_loop = "asyncio"` under `[tool.bessemer]`). The command-line flag, the
environment variable and `[tool.bessemer]` reach only `bessemer run`; a server
started with `app.run()` takes the `event_loop=` argument, and a `TestClient`
takes `TestClient(app, server_options={"event_loop": "asyncio"})`. Use it if a library you
depend on misbehaves on the Rust loop; please report the case.

With one loop per worker (the default `loops=1`) the HTTP connections are
also served on the loop's own thread, so a request no longer crosses between
threads to reach your handler. In our measurements that cut CPU per request by
about 29% at one connection (`GET /json`, 36.3 to 25.7µs) and 12% at 64
connections (92k to 105k requests a second on one worker). With `loops` above
1, `"auto"` runs asyncio's loop, as releases before `"auto"` did, and the
connections keep their own threads. Ask for `event_loop="bessemer"` with
`loops` above 1 to run Bessemer's loop in each lane.

Things to know when the loop is Bessemer's:

- With `loop.set_debug(True)`, a callback that runs for
  `loop.slow_callback_duration` or longer is logged on asyncio's logger as
  `Executing <Handle ...> took 0.123 seconds`, as asyncio's loop does. With
  debug off nothing is timed.
- Synchronous (`def`) routes run on Bessemer's own handler threads, not on
  anyio worker threads, as before. `anyio.from_thread.run()` called without
  a token from such a route raises `NoEventLoopError` on both loops. Save
  `anyio.lowlevel.current_token()` in async code and pass it as
  `anyio.from_thread.run(func, token=token)`. Code running in a thread started
  by `anyio.to_thread.run_sync` is unaffected.
- `asyncio.new_event_loop()` and `asyncio.run()` called from a thread you
  start yourself still create asyncio's loop; only the worker's own loops are
  Bessemer's.
- Under heavy connection counts the Rust loop serves connections inside the
  loop thread, so overload behaviour differs from asyncio's: in one test at
  1,024 connections the asyncio worker answered many requests with 503
  (admission refusal) where the Rust loop answered none.

Libraries that need a specific loop implementation, such as uvloop, are not
affected: they are simply not used.

### io_uring socket I/O

A worker that serves connections on Bessemer's loop (`event_loop="auto"` or
`"bessemer"` resolving to it, with one loop per worker) does its socket I/O
through epoll by default. It can use io_uring instead:
`app.run(io_backend="io_uring")`,
`bessemer run --io-backend io_uring`,
`BESSEMER_IO_BACKEND=io_uring` or `io_backend = "io_uring"` under
`[tool.bessemer]`. The accepted values are `"epoll"` (the default) and
`"io_uring"`.

The setting is off by default and works on Linux only. It needs:

- Bessemer's loop with a single loop (`loops=1`): the default `"auto"` on
  Linux, or `event_loop="bessemer"`. With `event_loop="asyncio"`, with more
  than one loop, or off Linux, the worker logs a warning and serves on epoll.
- Linux 6.1 or later.
- A seccomp profile that allows the `io_uring_setup`, `io_uring_enter` and
  `io_uring_register` system calls. Docker's and containerd's default profiles
  block them, so in a container you need a custom profile that allows them;
  [`deploy/seccomp-io-uring.json`](../deploy/seccomp-io-uring.json) is Docker's
  default profile with those three calls added (see
  [deployment](deployment.md)).

When the kernel or the seccomp profile refuses io_uring, the worker logs a
warning and falls back to epoll, so one configuration still starts everywhere.

### Large request bodies and the garbage collector

CPython starts a collection of its youngest generation when the number of
container objects allocated since the last one, less those freed, passes
`gc.get_threshold()[0]`: 2,000 unless the application changes it. A body
parsed into models allocates that many at once. A list of a thousand small
models is about four thousand: an instance, its `__dict__` and its field set
for each, and each of its lists. None of them can be garbage while the request
that owns them is being served, so the collection they start walks all of them
and frees nothing.

Bessemer keeps those objects out of that budget. When a request body of 4 KiB
or more is parsed into at least 512 container objects, the young threshold is
raised by their number until the request finishes or a collection resets the
young allocation count, whichever comes first. At the start of a collection,
Bessemer retires the existing allowances and restores the application's base
threshold. Ending one of those requests later cannot subtract an allowance
granted to a newer request. Bessemer suspends new allowances from its start
hook until its stop hook, while the collector processes young objects and runs
finalizers. A stop callback that runs after Bessemer's can create fresh young
objects and receive a new allowance, because the collection's processing is
complete. After the collection, the old body allocations no longer raise the
budget for unrelated application allocations.

This is not done on a free-threaded build of Python, whether its GIL is off or
has been switched on (`PYTHON_GIL=1`, or an extension that re-enables it).
That build has a different collector: it does not start a collection on the
young count alone, so moving the threshold does not move its collections, and
without the GIL other threads allocate, free and collect while a body is being
parsed. There nothing is measured, and neither the threshold nor
`gc.callbacks` is touched.

What an application can see of this:

- `gc.get_threshold()[0]` read inside a handler is higher than the configured
  value while qualifying bodies have active allowances. It returns to the
  base when those requests finish or a collection retires their allowances,
  even if the requests still hold their bodies.
- A threshold the application sets with `gc.set_threshold()` is kept. It is
  taken as the new base the next time an allowance is granted, released or
  retired by a collection, so set it to the value you want with no large
  request in mind. For the same reason, do not save `gc.get_threshold()`
  inside a handler and restore it later: the saved value can include
  allowances, and restoring it makes them part of the base. Set thresholds
  at start-up, or use `gc_allowance="none"`.
- A threshold of zero and `gc.disable()` both switch automatic collection off,
  and both are left alone.
- A handler that keeps its body's objects after the request (in a cache or a
  global) loses any remaining allowance when the request ends. If there has
  been no collection, those allocations still count towards the young budget
  and can trigger a subsequent collection. If a collection already reset that
  count, the allowance has already been retired.
- If a handler frees its body's objects early in a long request, the threshold
  stays raised until the request ends or the next collection starts, so up to
  that many more containers can be allocated before the next collection.

Bessemer observes collections through a hook in `gc.callbacks`; keep that hook
installed. It is added after the callbacks already in the list, so it does not
disturb one that is being called. Removing it during a request can leave the
threshold raised until Bessemer next detects the missing hook, which is when
the next large body arrives; the allowances it could not watch are retired
then and the hook is put back. Immediate restoration is not guaranteed.
Applications that replace the callback list should use `gc_allowance="none"`.
Use `gc.freeze()` before workers start serving requests: freezing objects
resets allocation counts without invoking collection callbacks, so freezing
during a request is not covered by the allowance.

The threshold is read and set through the `gc` module's own functions. If
`gc.isenabled`, `gc.get_count`, `gc.get_threshold` or `gc.set_threshold` has
been replaced by the time the first server starts, no allowance is made, no
hook is added, and the server logs a warning that `gc_allowance='body'` has no
effect in that process.

Only bodies Bessemer parses natively for a route's Pydantic model are
measured. A body the handler reads itself, with `await request.json()` or by
validating in the handler, allocates while Python code runs and is not.

`gc_allowance` controls it: `"body"` (the default) or `"none"`, which never
touches the threshold or `gc.callbacks`. It is `app.run(gc_allowance="none")`,
`bessemer run --gc-allowance none`, `BESSEMER_GC_ALLOWANCE=none` or
`gc_allowance = "none"` under `[tool.bessemer]`. The collector belongs to the
process, so the setting does too.

Without the GIL, two sync handlers can really run at the same moment. Guard
shared mutable state in your application (module globals, `app.state`, caches)
with a lock, as you would in any threaded Python program. Native code takes
the locks it needs: for example, a response list or dict that another thread
is still changing is encoded from a consistent snapshot rather than read while
it changes.

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

A route reads one JSON body, including its dependency graph. A single body
parameter is the whole body. Several body parameters, or one declared with
`Body(embed=True)`, are read as the fields of one JSON object keyed by
parameter name, as in FastAPI; this needs Pydantic (see
[FastAPI parity](fastapi-parity.md#parameters)). Use a Pydantic model for typed
container contents. Arbitrary union types and foreign `Annotated` metadata are
rejected or unsupported.

Plain models take a native fast path: when every field is an `int`, `float`,
`str`, `bool`, list, optional or another plain model, with at most an
immutable default and no validators, constraints, custom serializers, computed
fields, private attributes, aliases other than a single string, or config that
changes validation, a body whose values already have their fields' exact JSON
types is built into the model natively (straight from the request bytes when
it is the route's only body and the handler does not take the `Request`), and
a returned instance of exactly the
declared response model is encoded natively. Any other body or value goes
through Pydantic as usual, so coercions, errors and response bytes are the same
either way; only the time taken changes.
JSON body defaults must be `None` with a nullable annotation. Body input accepts
`application/json` and `application/*+json`; a declared body with a different
content type receives `415`. So does one sent with no `Content-Type` at all: a
browser can post such a body to another site without a CORS preflight, so a
body is read as JSON only when the request says it is, as in FastAPI. Pass
`App(strict_content_type=False)` to read a body without a `Content-Type` as
JSON; a mounted application uses its own setting. Body size limits apply to
buffered Request bodies too. A request that declares a `Content-Length` over
the limit is refused with `413` before any of the body is read, and before
`100 Continue` is sent to a client that asked with `Expect: 100-continue`.

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
`BytesIO`), and async `read`, `seek`, `write` and `close`. By default the
whole body is in memory and within `max_body_size`.

Set `max_upload_size` (`--max-upload-size`, or `App.run(max_upload_size=...)`)
to accept larger uploads on routes with `File()` or `UploadFile` parameters.
A multipart body to such a route whose `Content-Length` exceeds
`max_body_size`, or that is chunked, is then parsed as it arrives: field values
stay in memory and must still fit `max_body_size`, and file data goes to an
anonymous temporary file in `--upload-dir` (the system temporary directory by
default), which is unlinked as soon as it is created so nothing is left behind
if the process dies. A spooled `UploadFile.file` is a read-only buffered
reader, so `write` raises `io.UnsupportedOperation`, and `request.body()`
raises `RuntimeError` because the raw body was never kept. A `bytes`
parameter declared with `File()` is read into memory, so on a spooled request
its parts must fit `max_body_size` too; use `UploadFile` for large files. Spool
files are written on a blocking thread, never a Tokio worker. Disk held by
spooled requests across the server is bounded by `max_upload_bytes_in_flight`
(default 4 GiB). Each worker also limits live spooled uploads to its resolved
`max_in_flight`, independently of execution admission. Paused uploads retain
that lifetime slot until their last native or Python spool reader closes;
further spooled requests receive `503` while it is full. A body over
`max_upload_size` or field values over
`max_body_size` receive `413`, and a full disk budget receives `503`. Other
routes keep `max_body_size`. `max_upload_size` must be `0` (off) or at least
`max_body_size`, and at most 64 GiB. A request may have at
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

Request generator cleanup (the code after `yield`) is LIFO. When it runs
depends on the response:

- **A buffered response with no background tasks:** cleanup runs **before the
  response is sent**. If it raises, that error is answered instead of the
  handler's response: with `500`, unless an exception handler takes it.
- **Background tasks are queued** (through `BackgroundTasks` or a response's
  `background=`): the response is sent first, then the tasks run, then cleanup.
  A cleanup failure is logged and cannot change the response, so the client
  has already been told the request succeeded. Do not leave work that decides
  the outcome, such as a commit, to the cleanup of a dependency on such a
  route: do it in the handler.
- **A `StreamingResponse`:** cleanup runs after the last chunk. The status and
  headers have gone by then, so a cleanup failure is logged and truncates the
  body (see [streaming](#streaming-responses-and-server-sent-events)). With
  background tasks queued as well, cleanup follows them after the whole body
  has been delivered, and a failure is only logged.

When the handler raises, cleanup runs first and is given the exception,
whatever answers the error afterwards. The request keeps its capacity slot
until the tasks and cleanup have finished, including after a timeout or
disconnect cancelled it. Use bounded client
timeouts and cooperative async cleanup. Startup/shutdown failure prevents
readiness or is reported to the supervisor; do not rely on indefinite cleanup
during forced termination.

### Security schemes

`bessemer.security` has FastAPI's security dependencies: `HTTPBearer`,
`HTTPBasic`, `OAuth2PasswordBearer`, `OAuth2AuthorizationCodeBearer`,
`APIKeyHeader`, `APIKeyQuery` and `APIKeyCookie`, plus the
`OAuth2PasswordRequestForm` (and `...Strict`) token-endpoint form.

```python
from bessemer.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm

oauth2 = OAuth2PasswordBearer("/token", scopes={"items:read": "Read items"})

@app.post("/token")
async def login(form: Annotated[OAuth2PasswordRequestForm, Depends()]):
    return {"access_token": issue_token(form.username, form.password)}

@app.get("/me")
async def me(token: Annotated[str, Depends(oauth2)]):
    return await user_for(token)
```

A scheme only extracts credentials. Missing or malformed ones get `401` with a
`WWW-Authenticate` challenge (`Bearer`, `Basic` with its realm, or `APIKey`);
with `auto_error=False` the dependency returns `None` instead. Verifying the
token or password is the application's job; `App(jwt=JWTAuth(...))` verifies
JWTs natively before Python runs. Each scheme a route depends on, directly or
through other dependencies, appears in its OpenAPI `security` requirement and
under `components.securitySchemes`, named by `scheme_name` (the class name by
default). They stay documented when `dependency_overrides` replaces them.
Two different schemes with one name fail at startup.

`Security(dependency, scopes=[...])` is `Depends` that also requires OAuth2
scopes, as in FastAPI. A dependency annotated with `SecurityScopes` (from
`bessemer.security`) receives every scope required by the `Security()`
markers above it, outermost first and without repeats, with `scope_str` for a
`WWW-Authenticate` challenge. Checking them against the token is still the
application's job.

```python
from bessemer import Security
from bessemer.security import SecurityScopes

async def current_user(
    scopes: SecurityScopes, token: Annotated[str, Depends(oauth2)]
):
    user = await user_for(token)
    if not set(scopes.scopes) <= user.scopes:
        raise HTTPException(401, "Not enough permissions",
            headers={"WWW-Authenticate": f'Bearer scope="{scopes.scope_str}"'})
    return user

@app.get("/items")
async def items(user: Annotated[User, Security(current_user, scopes=["items:read"])]):
    ...
```

One dependency under different scopes is a separate cached entry, so
`Depends(current_user)` and `Security(current_user, scopes=["a"])` in one
request run it twice. OAuth2 and OpenID Connect requirements in OpenAPI list
the scopes; other schemes keep an empty list. Application-scoped dependencies
are shared across routes, so no route's scopes reach them and they cannot ask
for `SecurityScopes`. With native JWTs, `require_scope` checks scopes before
Python runs.

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
`traceparent` (the request's W3C trace context under `App(otel=...)`, else
`None`), `app` and `state`. `await request.body()` returns buffered bytes;
`await request.json()` parses strict finite JSON. Query mapping values use the
last repeated value. `request.headers.getlist(name)` exposes repeated headers;
scalar bound headers reject duplicates. Cookie binding and Request.cookies
share the native parsed view, including simple quoted values. Each pair is
checked against RFC 6265's octets. A pair that does not fit (a space or a
comma in the value, a backslash escape, no `=`) is left out rather than
guessed at, and the first cookie with a name wins, as a browser sends the
most specific one first. A request is never refused for its cookies: a
browser attaches every cookie in scope, including ones other applications set
on the domain. A required cookie parameter whose cookie was left out is
missing, with the usual `422`.

`bessemer.request.current_request` is a ContextVar available to handlers,
dependencies and background work, including offloaded sync calls. It is reset
when the request finishes. The server only passes a request's metadata to
Python for a route that needs it: one whose handler or dependencies take the
`Request` (or an injected `Response`, or a `Query`/`Header`/`Cookie`/`Form`
model), and every route of an app with exception handlers or middleware. On
every other route `current_request.get()` is a placeholder. Its `method` is
the route's and its `path` is the route template (`/items/{item_id}`, not the
path requested); `headers`, `query_params`, `cookies` and `path_params` are
empty, and `client` and `request_id` are `None`. Declare a `Request`
parameter where the real values are needed.

Background tasks can be sync or async. They run in registration order **after
the response has been delivered**, while the request's dependencies are still
open (their cleanup follows the tasks, as described under
[Dependencies](#dependencies)). They add no latency to the response, but the
request holds its capacity slot until they finish, and they do not run when
the response could not be sent. An error in a task is logged and stops the
tasks after it. They are best effort: not a durable queue or a safe place to
schedule work whose delivery must survive worker failure.

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

A status code can be an `int` or an `http.HTTPStatus` member wherever one is
given: `status_code=` on a route or a response, `HTTPException` and
`exception_handler`.

A `JSONResponse` is encoded natively when it is sent, and `response.body`
returns the same bytes when read. To send your own encoding, subclass it and
override `render(content) -> bytes`, Starlette's extension point: the bytes are
sent as returned, under the class's media type, whether the class is a route's
`response_class` or the handler returns an instance. As a `response_class` it
receives the handler's value already converted to JSON-compatible data, as in
FastAPI. `body` cannot be assigned on a `JSONResponse`; change `content`, or
return a `Response` with the bytes. `render` is only consulted on
`JSONResponse` subclasses.

`Headers(other_headers)`, and so `Response(..., headers=response.headers)`,
copies every field, including repeated `Set-Cookie` fields.

Application response headers are limited to 64 fields and 16 KiB total,
including repeated cookies. Header names must be ASCII tokens; values allow
printable ASCII and tabs. CR/LF and transport/framing headers such as
Content-Length, Transfer-Encoding and Connection are rejected. Rust manages
framing. Text response classes add UTF-8 charset. HEAD and statuses 204, 205 and
304 do not send a response body. HEAD retains a known GET `Content-Length`,
but omits it when native compression could change the representation length.

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
Response, which can be a `StreamingResponse`. Registration freezes at build.
They handle failures that reach Python; native framing, authentication,
body-limit and primitive extraction failures remain native and do not call a
Python exception handler.

Which handler answers an error follows FastAPI:

- An `HTTPException` goes to the handler registered for its status code
  (`@app.exception_handler(404)`), or else to one registered for
  `HTTPException` or a subclass it belongs to. Without either it gets its
  built-in JSON response.
- A `RequestValidationError` goes to a handler registered for it, or else gets
  the built-in `422`.
- Any other exception goes to the handler registered for its class or its
  nearest base class.
- A handler registered for `Exception`, or for status `500`, is the catch-all
  for errors that nothing above handled. It never replaces the built-in
  handling of an `HTTPException` or a request validation error. Neither does a
  handler for `ValueError`, although both validation errors subclass it here.
  A `ResponseValidationError` has no built-in response of its own: without a
  handler registered for it, it is an unhandled error.

An error that a handler answers is not logged. An error that nothing handled is
logged at `ERROR` on the `bessemer` logger, whether the catch-all handler or
the built-in `500` answers it. The line `Unhandled <Class> in <METHOD> <route>`
is followed by a traceback: the file, line and function of each frame and the
exception class names, with chained causes. Exception messages and source
lines are left out on purpose, because either can carry request data or a
secret, and the log record's `exc_info` is not set. A handler that itself
raises is logged the same way and answered with `500`.

### HTTP middleware

Wrap Python routes with FastAPI-style middleware:

```python
import time

@app.middleware("http")
async def timing(request, call_next):
    started = time.perf_counter()
    response = await call_next(request)
    response.headers["Server-Timing"] = f"app;dur={(time.perf_counter() - started) * 1000:.1f}"
    return response
```

A middleware is an async function taking `(request, call_next)`. The last one
registered runs outermost. `await call_next(request)` runs the rest of the
chain and the route, then returns a response whose `status_code` and `headers`
can be changed. Its body stays in native form (encoded JSON, bytes or a live
stream), so middleware never re-encodes or buffers it. Reading `.body` from it
raises `TypeError`. To replace the body, return a new `Response`, which can be
a `StreamingResponse`. Doing this before `call_next` short-circuits the route. If a replaced response was a
stream, it is closed and its dependencies and iterator are cleaned up with the
request. `request.state` is shared with the route's `Request`.

A route error that an exception handler answers comes back from `call_next`
as a response, as in FastAPI. That covers every `HTTPException` and request
validation error, which have built-in handlers, and any exception with a
handler registered for its class. An error that nothing handled is **raised**
from `await call_next(request)`, after the route's dependencies have been
cleaned up. A middleware can catch it to roll back, answer it with a response
of its own, or let it pass:

```python
@app.middleware("http")
async def unit_of_work(request, call_next):
    try:
        response = await call_next(request)
    except Exception:
        await rollback()  # Your code.
        raise
    # A handled error arrives here as a response: check its status.
    if response.status_code < 400:
        await commit()
    else:
        await rollback()
    return response
```

An error that leaves the outermost middleware is logged and answered by the
catch-all handler (registered for `Exception` or `500`) or the built-in `500`.
Middleware never sees that final response. An exception raised by a middleware
itself ends the same way; handlers registered for its class apply to route
errors only.

This differs from FastAPI in three ways:

- An `HTTPException` raised by a middleware becomes its JSON error directly,
  without calling an exception handler. Starlette answers it with `500`.
- A mounted app's unhandled error passes through the mounted app's middleware
  and then the outer app's, and the mounted app's catch-all handler answers it
  at the end. In Starlette the mounted app has answered before the outer
  middleware sees the exception.
- A `SystemExit` or `KeyboardInterrupt` raised by a handler is treated as an
  unhandled error (logged, `500`) and does not stop the worker.

Native controls (hosts, CORS, auth, rate limits, compression) run outside
Python, so they wrap any middleware. Routes that never enter Python are not
seen by middleware. These are static files, health, metrics, the OpenAPI
documents, and WebSockets, matching Starlette's `"http"` scope. Registration
freezes at build.

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

A mount serves at most `max_connections` responses at once per worker and
answers `503` with `Retry-After: 1` beyond that. An HTTP/1.1 connection serves
one response at a time, so it cannot reach the bound; it keeps HTTP/2 streams
from each holding a file open. A path that is not valid percent-encoded UTF-8
is `404`.

With `html=True`, a directory, including the mount root, serves its
`index.html`. A directory URL without its trailing slash gets a `307` redirect
to the slashed URL, so relative links in the page resolve. No redirect is sent
for a path beginning `//` or `/\`, which a browser would follow to another
host. `cache_control` sets a `Cache-Control` value for every file.
Mounts are GET (and HEAD) only and do not appear in OpenAPI. Application routes
keep priority, so a root mount can sit alongside an API. With
`App(compression=...)`, text files without a precompressed sibling are
compressed on the fly. Files up to 1 MiB are compressed whole, and larger ones
are streamed through the encoder. A file compressed whole is held in memory
and counts against `--max-response-bytes-in-flight` until it is sent; when
that budget is full it is streamed through the encoder too.

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
the close handshake are implemented in Rust (RFC 6455; the `permessage-deflate`
extension is optional and off by default, as described below). Python
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
- **Flow control.** Up to 64 received messages, and at most `max_body_size`
  bytes of them (at least 64 KiB) plus one message, wait for the handler
  before Rust stops reading the socket. A handler that is not receiving
  therefore holds a bounded amount per session; the rest stays in the
  client's and the kernel's socket buffers. `send_*` waits only while more than 1 MiB is
  queued for a slow client.

Rust delivers messages that are already readable in one batch per loop
wake-up. The writer combines queued frames into a single socket write. A
16-byte echo sustains about 260,000 messages per second each way on one worker
on the development machine.

Native controls apply to the handshake (host, CORS origin, rate limit, API key
and JWT). Browsers cannot set an `Authorization` header on WebSockets, so
authenticate browser clients some other way, such as a cookie checked in a
dependency. A handshake carrying `Origin` requires an explicit matching origin
in `cors`; a wildcard is not sufficient. Configure the trusted browser origins
for WebSockets. Clients without `Origin` can still connect, so applications
must authenticate them as well.
HTTP/2 clients can open WebSockets by extended CONNECT (RFC 8441) on the same
routes; the handler sees the same request either way.

`--websocket-compression deflate` (or `App.run(websocket_compression=
"deflate")`) accepts `permessage-deflate` (RFC 7692) from clients that offer
it, such as browsers. Messages are compressed one at a time (no context
takeover in either direction), so a session keeps no compression window
between messages; replies under 128 bytes, or that would not shrink, are sent
uncompressed. Inbound messages are limited by `--max-body-size` after
decompression as well as before. It is off by default: compressing data that
mixes secrets with attacker-chosen text can leak the secrets through message
sizes, as with HTTP compression.

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

`client.websocket_connect(path)` opens a WebSocket session through the same
native server, as in Starlette:

```python
with TestClient(app) as client:
    with client.websocket_connect("/rooms/lobby", subprotocols=["chat"]) as ws:
        ws.send_json({"text": "hi"})
        assert ws.receive_json()["room"] == "lobby"
```

The handshake carries the client's default headers and cookies plus
`headers=`, `cookies=` and `params=`. Sessions have `send_text`, `send_bytes`,
`send_json`, `receive`, `receive_text`, `receive_bytes`, `receive_json`,
`close(code, reason)`, `accepted_subprotocol` and the handshake `headers`.
Messages are whole and pings are answered. When the application closes the
connection, the next receive raises `WebSocketDisconnect` with its code and
reason. A handshake the application refuses raises
`bessemer.testclient.WebSocketDenied`, a `WebSocketDisconnect` (code 1008)
whose `response` is the HTTP response, such as the `403` from closing before
`accept()`.

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
`request_id`, `security_headers`, `access_log`, `metrics_path`,
`metrics_token_env` and `otel` on App. See [native production controls](production-controls.md)
for exact policies, including
[OpenTelemetry tracing](production-controls.md#opentelemetry-tracing). Health/docs are protected unless explicitly exempted.
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
| Stalled response write | the longest request timeout |
| Slowest response read | 240 bytes a second beyond that timeout |
| Request header count / bytes | 64 / 32 KiB |
| Normalized request target | 8 KiB |
| Application response headers | 64 / 16 KiB |

Use CLI flags such as `--max-response-size`, `--max-in-flight`,
`--max-request-bytes-in-flight`, `--max-response-bytes-in-flight`,
`--max-connections`, `--max-body-size`, `--max-headers`, `--max-header-size`,
`--max-request-target-size`, `--max-upload-size`,
`--max-upload-bytes-in-flight`, `--upload-dir`, `--header-timeout`, `--request-timeout`,
`--min-send-rate` and `--graceful-timeout`. `--tls-cert` and `--tls-key` (or `App.run(tls_cert=...,
tls_key=...)`) terminate TLS natively with rustls; requests then report the
`https` scheme and WebSockets `wss`. TLS 1.3 is the minimum unless
`--tls-min 1.2` is given. `--tls-client-ca` turns on mutual TLS: clients must
present a certificate chaining to that bundle (or may, with
`--tls-client-auth optional`), and the verified leaf is available as
`request.client_certificate` (DER bytes; also on `WebSocket`).
`--tls-client-crl` refuses revoked client certificates; every client
certificate's issuer then needs a current CRL in that file.
`--tls-ocsp-response` staples a DER OCSP response that something else keeps
fresh. Rotated
certificate files are picked up without a restart, checked every
`--tls-reload-interval` seconds (default 60). Missing files,
a mismatched pair or a lone setting fail at startup. HTTP/2 is served alongside HTTP/1.1 (`--http auto`, by ALPN
over TLS and prior knowledge on plaintext); `--http 1.1` turns it off. Supported runtime settings resolve CLI > `BESSEMER_*`
environment > `[tool.bessemer]` > defaults. Python response normalization is
bounded; user code and Pydantic validation/serialization may allocate before the
runtime rejects an oversized result. Avoid constructing unbounded responses.
The request-target limit measures the normalized `http::Uri`, whose parser may
remove fragments; the complete raw request line/header block remains bounded by
the header-buffer limit.
The request timeout runs from the moment the request head is parsed, so time
spent in controls before the handler (an awaited shared rate-limit decision, a
JWKS fetch for JWT checks) counts against it.
A response write that makes no progress for as long as the longest request
timeout (`--request-timeout`, or a longer per-route `timeout`) closes the
connection, so a client that stops reading cannot hold a connection slot or
response bytes indefinitely. The clock only runs while the socket refuses
bytes: quiet streams are unaffected.
A client that keeps reading, but slowly, is held to a floor as well:
`--min-send-rate` bytes a second, 240 by default. Within an HTTP/1 response,
from its first write that has to wait for the client, the time writes have
spent waiting may not exceed that same timeout plus the time the
bytes taken would need at the floor, so a response of `n` bytes is held for at
most the timeout plus `n / min_send_rate` seconds of waiting; then the
connection is closed. Immediately accepted frames add byte credit without
resetting the response's accounting; it resets between responses.
HTTP/2 applies the same rule to each stream, counting
from when the stream first has data its peer has not taken. Time the
application spends producing the response does not count. The floor measures
what the server is told: bytes the kernel or TLS layer has accepted but not
yet delivered count as taken. The kernel also reports room in steps that grow
with its send buffer, and a client too slow to free one step within the timeout
is disconnected by the idle timeout whatever the floor. Raise the floor to
bound how long large responses can be held (at 64 KiB a second a 4 MiB response
is held for at most the timeout plus 64 seconds of waiting); `0` sets no floor
and leaves only the idle timeout. WebSocket sends go through the same writes.
A WebSocket session over HTTP/1.1 never reaches a response boundary, so it is
counted as one long response: the time its writes have spent waiting, over the
whole session, against the bytes it has taken.
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

Distributed caches and native I/O clients remain outside this API slice. Full production
qualification still requires representative deployment/failure/load testing,
platform verification and the remaining specification gates.
